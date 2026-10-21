"""
AI 团队编排工具（team_broadcast / team_assign / team_report / team_request_review / team_read_sop）

这五个工具**不走全局插件注册**：它们只由团队 run 执行器（team_runtime）动态拼进
工具列表并本地执行，宿主校验天然成立——只有团队 run 的 tool_input 里才有
`_team_context`（team_id / user_key / run_id / acting_member_id / task_id），
普通对话里根本看不到这些工具的 schema，不占任何 token。

工具只写 DB 表（消息/任务/队列）+ 通知 team_hub；spawn run 一律由编排器消费队列完成。
"""

from __future__ import annotations

import json
from datetime import datetime

from openvort.plugin.base import BaseTool
from openvort.utils.logging import get_logger

log = get_logger("core.team_tools")


def _ok(**kw) -> str:
    kw.setdefault("ok", True)
    return json.dumps(kw, ensure_ascii=False)


def _fail(message: str, **kw) -> str:
    kw.setdefault("ok", False)
    kw["message"] = message
    return json.dumps(kw, ensure_ascii=False)


def _context(params: dict) -> dict:
    ctx = params.get("_team_context")
    return ctx if isinstance(ctx, dict) else {}


def _get_sf():
    from openvort.db.engine import get_session_factory
    return get_session_factory()


async def _load_team(db, team_id: str):
    """返回 (团队 Member 行, 解析后的 V3 dict)；失败返回 (None, 错误信息)。"""
    from openvort.contacts.models import Member
    from openvort.core.engine.team_service import is_team_row
    from openvort.core.engine.virtual_persona import parse_team_persona

    row = await db.get(Member, team_id)
    if not row or not is_team_row(row):
        return None, "团队不存在或已解散"
    team = parse_team_persona(row.virtual_system_prompt)
    if not team:
        return None, "团队数据损坏（人设 JSON 解析失败）"
    return (row, team), ""


async def _resolve_assignee(db, team: dict, ident: str):
    """按成员 ID 或花名册内姓名解析一个被指派成员。"""
    ident = (ident or "").strip()
    if not ident:
        return None, "指派对象不能为空"
    roster_ids = {e["member_id"] for e in team["roster"]}
    if ident in roster_ids:
        return ident, ""
    names = [e for e in team["roster"] if e.get("name") == ident]
    if len(names) == 1:
        return names[0]["member_id"], ""
    if len(names) > 1:
        return None, f"花名册里有多位「{ident}」，请用成员 ID 精确指派"
    return None, f"「{ident}」不在团队花名册中，只能指派给团队成员"


class TeamBroadcastTool(BaseTool):
    name = "team_broadcast"
    description = (
        "把内容发到团队公屏（用户能看到的唯一渠道）。你的其他输出用户都看不到；"
        "需要让用户知道首答、进展、结论时必须调用本工具。内容用面向用户的口吻，"
        "简洁说清：做了什么/结论是什么/接下来谁做什么。"
    )

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "要上公屏的内容（Markdown）"},
            },
            "required": ["content"],
        }

    async def execute(self, params: dict) -> str:
        from openvort.core.engine import team_data, team_hub
        from openvort.core.engine.team_service import strip_team_prefix

        content = (params.get("content") or "").strip()
        if not content:
            return _fail("content 不能为空")
        ctx = _context(params)
        if not ctx.get("team_id"):
            return _fail("仅团队会话内可用")

        sf = _get_sf()
        async with sf() as db:
            loaded, err = await _load_team(db, ctx["team_id"])
            if err:
                return _fail(err)
            team_row, team = loaded
            acting = ctx.get("acting_member_id", "")
            session_id = await team_data.get_or_create_team_session(
                db, ctx["team_id"], ctx["user_key"], strip_team_prefix(team_row.name))
            sender_name = next(
                (e.get("name", "") for e in team["roster"] if e["member_id"] == acting), "")
            msg = await team_data.write_team_message(
                db, session_id=session_id, owner_id=ctx["user_key"],
                sender_type="assistant", sender_id=acting, content=content,
                visibility="broadcast", source="team",
                metadata={"team_id": ctx["team_id"], "sender_name": sender_name,
                          "run_id": ctx.get("run_id", "")},
            )
            await db.commit()
            await team_hub.notify_broadcast(
                ctx["team_id"], ctx["user_key"], {
                    "id": str(msg.id),
                    "role": "assistant",
                    "content": content,
                    "sender_id": acting,
                    "sender_name": sender_name,
                    "timestamp": msg.created_at.isoformat() if msg.created_at else None,
                    "session_id": session_id,
                },
                run_id=ctx.get("run_id", ""),
            )
        return _ok(message="已广播到公屏")


class TeamAssignTool(BaseTool):
    name = "team_assign"
    description = (
        "把你完成后的后续工作指派给其他团队成员（可一次指派多人，按顺序依次执行，"
        "全部完成后会回到你这里汇总）。指派时必须写清：任务标题、具体要求、"
        "已有的产出及其文件路径（如 PRD 在共享电脑上的位置）。"
        "返工任务请带 parent_task_id 指向原任务。"
    )

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "任务标题，一句话"},
                "brief": {"type": "string", "description": "任务要求：做什么、交付什么、注意什么"},
                "assignees": {
                    "type": "array", "items": {"type": "string"},
                    "description": "被指派成员列表（姓名或成员 ID），按执行顺序排列",
                },
                "artifacts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "共享电脑上的文件/目录路径"},
                            "desc": {"type": "string", "description": "这份产出是什么"},
                        },
                        "required": ["path"],
                    },
                    "description": "本阶段已有产出（可选）",
                },
                "parent_task_id": {"type": "string", "description": "返工任务指向的原任务 ID（可选）"},
            },
            "required": ["title", "brief", "assignees"],
        }

    async def execute(self, params: dict) -> str:
        from openvort.core.engine import team_data

        ctx = _context(params)
        if not ctx.get("team_id"):
            return _fail("仅团队会话内可用")
        title = (params.get("title") or "").strip()
        brief = (params.get("brief") or "").strip()
        assignee_idents = params.get("assignees") or []
        if not title or not brief:
            return _fail("title 和 brief 必填")
        if not isinstance(assignee_idents, list) or not assignee_idents:
            return _fail("assignees 至少一名成员")

        sf = _get_sf()
        async with sf() as db:
            loaded, err = await _load_team(db, ctx["team_id"])
            if err:
                return _fail(err)
            team_row, team = loaded
            assignees: list[str] = []
            for ident in assignee_idents:
                mid, err = await _resolve_assignee(db, team, str(ident))
                if err:
                    return _fail(err)
                if mid not in assignees:
                    assignees.append(mid)

            parent_id = (params.get("parent_task_id") or "").strip()
            rework_count = 0
            if parent_id:
                parent = await team_data.get_team_task(db, parent_id)
                if parent and parent.team_id == ctx["team_id"]:
                    rework_count = (parent.rework_count or 0) + 1
                    parent.rework_count = rework_count
                    if rework_count >= team["config"]["max_rework"]:
                        await db.commit()
                        return _fail(
                            f"任务「{parent.title}」已返工 {rework_count} 次，达到上限"
                            f"（{team['config']['max_rework']}）。请勿继续返工，"
                            "把分歧点整理清楚上报用户决策。"
                        )

            task = await team_data.create_team_task(
                db, team_id=ctx["team_id"], user_key=ctx["user_key"],
                title=title, brief=brief, assignees=assignees,
                artifacts=params.get("artifacts") or [],
                created_by_member_id=ctx.get("acting_member_id", ""),
                parent_task_id=parent_id,
            )
            await team_data.enqueue(
                db, ctx["team_id"], ctx["user_key"], "member_run",
                {"task_id": task.id, "member_id": assignees[0]},
            )
            await db.commit()
            names = []
            roster_names = {e["member_id"]: e.get("name", "") for e in team["roster"]}
            for mid in assignees:
                names.append(roster_names.get(mid, mid))
        return _ok(message=f"已指派「{title}」→ {'、'.join(names)}（按顺序执行）",
                   task_id=task.id, assignees=assignees)


class TeamReportTool(BaseTool):
    name = "team_report"
    description = (
        "回合交接汇报。**每次处理结束前必须调用一次**：汇报你做了什么、结论/产出"
        "（含共享电脑上的文件路径）、遇到的问题。多人任务里每人完成后各自调用。"
        "负责人验收时用 verdict 字段给出结论。"
    )

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "本次处理汇报：做了什么/结论/问题"},
                "artifacts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "desc": {"type": "string"},
                        },
                        "required": ["path"],
                    },
                    "description": "本次产出文件（可选）",
                },
                "task_status": {
                    "type": "string",
                    "enum": ["in_progress", "done", "rejected"],
                    "description": "对当前任务的状态判定（可选）：done=本任务完成；"
                                   "rejected=验收不通过需返工；in_progress=仍在进行",
                },
                "verdict": {
                    "type": "string",
                    "enum": ["pass", "reject"],
                    "description": "验收结论（负责人验收时给）",
                },
                "verdict_comment": {"type": "string", "description": "验收意见（配合 verdict）"},
            },
            "required": ["summary"],
        }

    async def execute(self, params: dict) -> str:
        from openvort.core.engine import team_data, team_hub

        ctx = _context(params)
        if not ctx.get("team_id"):
            return _fail("仅团队会话内可用")
        summary = (params.get("summary") or "").strip()
        if not summary:
            return _fail("summary 必填")
        team_hub.mark_reported(ctx.get("run_id", ""))

        sf = _get_sf()
        async with sf() as db:
            task = await team_data.get_team_task(db, ctx.get("task_id", ""))
            entry = {
                "member_id": ctx.get("acting_member_id", ""),
                "summary": summary,
                "artifacts": [
                    {"path": a.get("path", ""), "desc": a.get("desc", "")}
                    for a in (params.get("artifacts") or []) if isinstance(a, dict) and a.get("path")
                ],
                "at": datetime.utcnow().isoformat(timespec="seconds"),
            }
            if task:
                await team_data.append_task_report(db, task, entry)
                await team_data.extend_task_artifacts(db, task, entry["artifacts"])
                verdict = params.get("verdict")
                if verdict in ("pass", "reject"):
                    task.review_verdict_json = json.dumps({
                        "verdict": verdict,
                        "comment": (params.get("verdict_comment") or "").strip(),
                        "by": ctx.get("acting_member_id", ""),
                    }, ensure_ascii=False)
                status = params.get("task_status")
                if status in ("in_progress", "done", "rejected"):
                    task.status = status
                await db.flush()
            await db.commit()
        return _ok(message="汇报已记录，将交接给下一棒")


class TeamRequestReviewTool(BaseTool):
    name = "team_request_review"
    description = (
        "工作做得差不多了，请团队负责人验收当前任务。负责人会收到你的汇报与产出，"
        "给出通过/不通过的结论；不通过时负责人会重新指派返工或上报用户。"
    )

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "请验收的说明：完成情况与自评"},
            },
            "required": ["summary"],
        }

    async def execute(self, params: dict) -> str:
        from openvort.core.engine import team_data

        ctx = _context(params)
        if not ctx.get("team_id"):
            return _fail("仅团队会话内可用")
        summary = (params.get("summary") or "").strip()

        sf = _get_sf()
        async with sf() as db:
            loaded, err = await _load_team(db, ctx["team_id"])
            if err:
                return _fail(err)
            _, team = loaded
            task = await team_data.get_team_task(db, ctx.get("task_id", ""))
            if not task:
                return _fail("当前没有关联任务，无法请求验收")
            await team_data.set_task_status(db, task, "review")
            await team_data.enqueue(
                db, ctx["team_id"], ctx["user_key"], "member_run",
                {"task_id": task.id, "member_id": team["leader_id"], "review": True,
                 "requested_by": ctx.get("acting_member_id", "")},
            )
            await db.commit()
        return _ok(message="已请负责人验收，验收结果会同步到公屏/任务记录")


class TeamReadSopTool(BaseTool):
    name = "team_read_sop"
    description = (
        "查阅团队完整沟通 SOP。当你需要确认协作流程、汇报口径、异常处理方式等细节时调用；"
        "自己能直接做完时不必读。"
    )

    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params: dict) -> str:
        ctx = _context(params)
        if not ctx.get("team_id"):
            return _fail("仅团队会话内可用")
        sf = _get_sf()
        async with sf() as db:
            loaded, err = await _load_team(db, ctx["team_id"])
            if err:
                return _fail(err)
            _, team = loaded
        sop = team.get("sop") or {}
        full_text = (sop.get("full_text") or "").strip()
        if not full_text:
            return _ok(content="（本团队暂未配置 SOP 全文）")
        return _ok(content=full_text)


# 本地执行表：team_runtime 拼工具列表 & 分发执行都用它
TEAM_TOOLS: dict[str, BaseTool] = {
    t.name: t
    for t in (TeamBroadcastTool(), TeamAssignTool(), TeamReportTool(),
              TeamRequestReviewTool(), TeamReadSopTool())
}


def team_tool_schemas() -> list[dict]:
    return [t.to_claude_tool() for t in TEAM_TOOLS.values()]
