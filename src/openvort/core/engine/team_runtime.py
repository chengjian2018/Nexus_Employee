"""
AI 团队成员 run 执行器

一次 execute_team_run = 团队流水线上的一棒：以某位成员（或负责人）的独立身份跑一个
完整 agentic loop（人设/公屏/任务卡上下文 + 全局工具 + 五个团队工具），产出与汇报
全部落到团队会话（private 为主，广播走 team_broadcast 工具）。

由 team_orchestrator 调用；超时/重试/护栏都在编排器那一层，本模块只管"跑好这一棒"。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from openvort.core.engine.team_tools import TEAM_TOOLS, team_tool_schemas
from openvort.core.engine.virtual_persona import (
    parse_team_persona, parse_virtual_persona, render_persona_sections,
)
from openvort.plugin import visibility as plugin_visibility
from openvort.utils.logging import get_logger

log = get_logger("core.team_runtime")

# 与 agent.py 保持一致的内置工具屏蔽集（团队电脑在线时这些被 node 工具替代）
from openvort.core.engine.agent import _NODE_SUPERSEDED_TOOLS  # noqa: F401

BROADCAST_DIGEST_LIMIT = 20
OWN_HISTORY_LIMIT = 10


@dataclass
class TeamRunSpec:
    """一棒的全部身份信息。kind 决定轮次上限与回合指令。"""

    run_id: str
    team_id: str
    user_key: str
    acting_member_id: str
    kind: str                 # leader_turn / member_run / summary_turn / leader_wakeup
    task_id: str = ""
    user_content: str = ""    # leader_turn：触发本轮的用户消息
    reason: str = ""          # leader_wakeup：唤醒原因
    review: bool = False      # member_run 且被指派人是 leader：验收回合
    requested_by: str = ""    # review：发起验收请求的成员
    extra: dict = field(default_factory=dict)


async def execute_team_run(*, llm, registry, spec: TeamRunSpec) -> dict:
    """跑一棒。返回 {"ok", "error", "final_text", "broadcast_count"}；异常向上抛给编排器。"""
    from openvort.contacts.models import Member
    from openvort.core.engine import team_data
    from openvort.core.engine.team_service import (
        get_team, strip_team_prefix,
    )
    from openvort.db.engine import get_session_factory

    sf = get_session_factory()
    async with sf() as db:
        team_row = await get_team(db, spec.team_id)
        team = parse_team_persona(team_row.virtual_system_prompt)
        if not team:
            raise RuntimeError(f"团队 {spec.team_id} 人设 JSON 解析失败")
        acting = await db.get(Member, spec.acting_member_id)
        if not acting:
            raise RuntimeError(f"成员 {spec.acting_member_id} 不存在")
        session_id = await team_data.get_or_create_team_session(
            db, spec.team_id, spec.user_key, strip_team_prefix(team_row.name))
        node_info = await _team_node_info(db, team_row)
        await db.commit()

    is_leader = spec.acting_member_id == team["leader_id"]
    rounds_limit = (team["config"]["max_rounds_leader"] if is_leader
                    else team["config"]["max_rounds_member"])

    # ---- 上下文三件：公屏摘要 / 自己的历史 / 本回合指令 ----
    async with sf() as db:
        broadcasts = await team_data.list_session_messages(
            db, session_id, visibility="broadcast", limit=BROADCAST_DIGEST_LIMIT)
        own_history = await team_data.list_session_messages(
            db, session_id, limit=OWN_HISTORY_LIMIT * 3)
        own_history = [m for m in own_history
                       if m.sender_id == spec.acting_member_id
                       and m.sender_type == "assistant"][-OWN_HISTORY_LIMIT:]
        task = await team_data.get_team_task(db, spec.task_id)
        roster_names = {e["member_id"]: e.get("name", "") for e in team["roster"]}

    transcript = _build_transcript(broadcasts, roster_names, spec)
    own_digest = _build_own_digest(own_history)
    instruction = _build_instruction(spec, team, task, roster_names,
                                     strip_team_prefix(team_row.name))
    system = _build_system_prompt(
        spec, team=team, acting=acting, team_name=strip_team_prefix(team_row.name),
        is_leader=is_leader, node_info=node_info, user_key=spec.user_key)

    messages: list[dict] = []
    if transcript:
        messages.append({"role": "user", "content": transcript})
        messages.append({"role": "assistant", "content": "（已了解公屏背景与我的历史，准备处理本回合任务）"})
    if own_digest:
        messages.append({"role": "user", "content": own_digest})
        messages.append({"role": "assistant", "content": "（已回顾我此前的处理记录）"})
    messages.append({"role": "user", "content": instruction})

    # 回合指令落库（system 消息，private），保证事后可回放每一棒的输入
    async with sf() as db:
        await team_data.write_team_message(
            db, session_id=session_id, owner_id=spec.user_key,
            sender_type="system", sender_id=spec.acting_member_id,
            content=f"[{spec.kind}] {instruction[:4000]}",
            visibility="private", source="team",
            metadata={"run_id": spec.run_id, "kind": spec.kind, "task_id": spec.task_id},
        )
        await db.commit()

    # ---- 工具集：全局插件工具（按宿主可见性过滤）+ 团队五工具 ----
    visible = await plugin_visibility.get_visible_plugins(registry, spec.acting_member_id)
    tools = registry.to_claude_tools(permissions=None, allowed_plugins=visible)
    blocked = {"git_code_task"}
    if node_info and node_info.get("status") in ("online", "running"):
        blocked |= set(_NODE_SUPERSEDED_TOOLS)
    tools = [t for t in tools if t["name"] not in blocked]
    tools += team_tool_schemas()

    ctx = {
        "team_id": spec.team_id,
        "user_key": spec.user_key,
        "run_id": spec.run_id,
        "acting_member_id": spec.acting_member_id,
        "task_id": spec.task_id,
    }

    final_text = ""
    any_tool_called = False
    for _ in range(rounds_limit):
        response = await llm.create(system=system, messages=messages,
                                    tools=tools if tools else None)
        messages.append({"role": "assistant", "content": _serialize(response.content)})

        if response.stop_reason != "tool_use":
            final_text = _extract_text(response)
            break

        any_tool_called = True
        tool_results = []
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            tool_input = dict(block.input)
            tool_input["_caller_id"] = spec.acting_member_id
            tool_input["_member_id"] = spec.acting_member_id
            tool_input["_target_member_id"] = spec.acting_member_id
            tool_input["_caller_member_id"] = spec.user_key
            tool_input["_user_input"] = instruction[:2000]
            tool_input["_team_context"] = ctx
            try:
                if block.name in TEAM_TOOLS:
                    result = await TEAM_TOOLS[block.name].execute(tool_input)
                else:
                    result = await registry.execute_tool(block.name, tool_input)
            except Exception as e:  # 工具异常不终止整棒，交给 LLM 自行调整
                log.warning(f"[team-run] 工具 {block.name} 执行失败: {e}")
                result = json.dumps({"ok": False, "message": f"工具执行失败: {e}"},
                                    ensure_ascii=False)
            log.info(f"[team-run:{spec.run_id[:8]}] {block.name} → {result[:200]}")
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
            })
        messages.append({"role": "user", "content": tool_results})
    else:
        final_text = final_text or "（达到轮次上限，未产出自然语言结论）"

    from openvort.core.engine import team_hub
    broadcast_count = team_hub.broadcast_count(spec.run_id)

    # 收尾文本落库（private）：这是成员自己的过程留档，不上公屏
    if final_text.strip():
        async with sf() as db:
            await team_data.write_team_message(
                db, session_id=session_id, owner_id=spec.user_key,
                sender_type="assistant", sender_id=spec.acting_member_id,
                content=final_text, visibility="private", source="team",
                metadata={"run_id": spec.run_id, "kind": spec.kind},
            )
            await db.commit()

    return {
        "ok": True,
        "error": "",
        "final_text": final_text,
        "broadcast_count": broadcast_count,
        "any_tool_called": any_tool_called,
    }


# ---- prompt 构建 ----


def _roster_lines(team: dict) -> list[str]:
    lines = []
    for e in team["roster"]:
        name = e.get("name") or e.get("member_id")
        marker = "（负责人）" if e["member_id"] == team["leader_id"] else ""
        title = f"，{e['position']}" if e.get("position") else ""
        note = f"：{e['role_note']}" if e.get("role_note") else ""
        lines.append(f"- {name}{marker}{title}{note}")
    return lines


def _sop_section(team: dict) -> str:
    sop = team.get("sop") or {}
    summary = (sop.get("summary") or "").strip()
    if not summary:
        return ""
    text = summary
    if (sop.get("full_text") or "").strip():
        text += "\n\n（完整 SOP 可随时用 team_read_sop 工具查阅；自己能直接做完时不必读）"
    return f"\n\n## 团队沟通 SOP（摘要）\n\n{text}"


def _computer_section(node_info: dict | None, user_key: str, member_name: str) -> str:
    if not node_info:
        return ("\n\n## 团队工作电脑\n\n团队尚未配置共享工作电脑。"
                "需要动手执行（写代码/跑脚本/产出文件）时，负责人可用 node_manage"
                "(action=create_and_bind) 创建并绑定到团队。")
    from openvort.core.engine.team_data import safe_dir_name
    status = node_info.get("status", "unknown")
    if status not in ("online", "running"):
        return f"\n\n## 团队工作电脑\n\n共享电脑「{node_info.get('name', '')}」当前不可用（{status}），本回合先用非电脑方式处理。"
    user_dir = safe_dir_name(user_key)
    private_dir = safe_dir_name(member_name)
    return f"""
## 团队工作电脑（共享）

全团队共用一台电脑「{node_info.get('name', '')}」，用 node_shell / node_file_read / node_file_write 操作。

### 目录约定（必须遵守）
- 正式产出（要交给下一棒/评审的）写 `/workspace/users/{user_dir}/public/`
- 个人草稿写 `/workspace/users/{user_dir}/private/{private_dir}/`（别人不会去读）
- 团队级共享资料在 `/workspace/team/`
- 引用产出时给出完整路径，下一棒和评审会直接读这些文件"""


def _protocol_section(is_leader: bool) -> str:
    leader_extra = """
### 负责人特别注意
- 每轮用户消息由你先接：先 team_broadcast 一句简短首答（表态+计划），再决定自己做还是 team_assign 分派
- 需要动手且团队还没电脑时，主动 node_manage(action=create_and_bind) 配一台
- 成员异常（未汇报/失败）会把你唤醒兜底：判断重派、自己接手，或上报用户""" if is_leader else ""
    return f"""
## 团队协作协议（必须遵守）

你在团队的串行任务流水线上：同一时刻全队只有你一个人在处理，你的交接决定下一棒。

### 工具用法
- `team_broadcast`：发公屏。**用户只看得到公屏**，你的其他输出用户都看不见
- `team_assign`：指派后续工作（可多人按顺序；写清要求与已有产出路径）
- `team_report`：**本回合结束前必须调用一次**，汇报做了什么/结论/产出路径/问题
- `team_request_review`：需要负责人验收时调用
- `team_read_sop`：需要核对协作流程细节时读

### 硬性规则
1. 结束前必须 team_report——没有产出也要汇报原因与建议，不许静默结束
2. 产出文件写共享电脑的规定路径（见上），交接时给出路径
3. 只能指派花名册内成员；返工任务带 parent_task_id
4. 不要编造其他成员的结论；他们的正式产出在任务汇报里{leader_extra}"""


def _build_system_prompt(spec: TeamRunSpec, *, team, acting, team_name: str,
                         is_leader: bool, node_info, user_key: str) -> str:
    member_name = acting.name
    persona = parse_virtual_persona(acting.virtual_system_prompt)
    parts = [f"你是「{team_name}」团队的「{member_name}」"
             + ("，团队负责人。" if is_leader else "，团队成员。")
             + "此刻你正在团队内部的任务流水线上独自处理一棒工作。"]
    if persona:
        parts.append("\n" + render_persona_sections(persona))
    parts.append("\n## 团队花名册\n\n" + "\n".join(_roster_lines(team)))
    parts.append(_sop_section(team))
    parts.append(_computer_section(node_info, user_key, member_name))
    parts.append(_protocol_section(is_leader))
    parts.append("\n## 基本准则\n- 完全以成员身份行事；产出专业、结论明确\n- 用中文")
    return "".join(p for p in parts if p)


def _build_transcript(broadcasts, roster_names: dict, spec: TeamRunSpec) -> str:
    if not broadcasts:
        return ""
    lines = ["以下是团队公屏最近的公开消息（用户与成员的广播，时间正序）："]
    for m in broadcasts:
        if m.sender_type == "user":
            who = "用户"
        else:
            who = roster_names.get(m.sender_id, m.sender_id or "?")
            who = f"{who}（公屏）"
        content = (m.content or "").strip()
        if content:
            lines.append(f"[{who}] {content[:800]}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _build_own_digest(own_messages) -> str:
    if not own_messages:
        return ""
    lines = ["你在本团队此前的处理留档（最近若干条，仅你可见）："]
    for m in own_messages:
        content = (m.content or "").strip()
        if content:
            lines.append(f"- {content[:600]}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _task_card(team, task, roster_names: dict) -> str:
    from openvort.core.engine.team_data import task_artifacts, task_reports
    if not task:
        return "（无关联任务记录）"
    lines = [f"- 标题：{task.title}", f"- 要求：{task.brief}"]
    assignees = json.loads(task.assignees_json or "[]")
    names = [roster_names.get(mid, mid) for mid in assignees]
    lines.append(f"- 执行顺序：{' → '.join(names)}")
    if task.created_by_member_id:
        lines.append(f"- 指派人：{roster_names.get(task.created_by_member_id, task.created_by_member_id)}")
    arts = task_artifacts(task)
    if arts:
        lines.append("- 已有产出：" + "；".join(
            f"{a.get('path', '')}（{a.get('desc', '')}）" for a in arts))
    if task.rework_count:
        lines.append(f"- 返工次数：{task.rework_count}")
    reports = task_reports(task)
    if reports:
        lines.append("- 各成员汇报：")
        for r in reports:
            rname = roster_names.get(r.get("member_id", ""), "?")
            lines.append(f"  * {rname}：{(r.get('summary') or '')[:500]}")
            for a in r.get("artifacts") or []:
                lines.append(f"    产出：{a.get('path', '')}（{a.get('desc', '')}）")
    verdict = {}
    try:
        verdict = json.loads(task.review_verdict_json or "{}")
    except (ValueError, TypeError):
        pass
    if verdict.get("verdict"):
        lines.append(f"- 上次验收：{verdict['verdict']}（{verdict.get('comment', '')}）")
    return "\n".join(lines)


def _build_instruction(spec: TeamRunSpec, team, task, roster_names: dict,
                       team_name: str) -> str:
    my_name = roster_names.get(spec.acting_member_id, spec.acting_member_id)
    if spec.kind == "leader_turn":
        return f"""## 用户最新消息

{spec.user_content.strip()}

## 你要做的（团队负责人回合）

1. 先调用 team_broadcast 给用户一句简短首答：团队已接手 + 你的处理计划（一两句话即可）
2. 判断怎么干：
   - 自己能很快做完的，直接做完（可用工具/共享电脑），然后广播结论
   - 需要成员接力的，用 team_assign 指派（可一次多人按顺序执行；写清任务要求与你的已有产出）
3. 结束前调用 team_report 汇报你的处理摘要（是否指派、指派了什么、用户需要注意什么）"""

    if spec.kind == "leader_wakeup":
        return f"""## 异常唤醒（团队负责人兜底回合）

原因：{spec.reason or '未指定'}

## 你要做的

1. 根据原因处理：成员没交代就结束/执行失败/流水线超限时，由你判断——重派（team_assign）、
   自己接手做完、或把情况整理后 team_broadcast 上报用户请他决策
2. 若原因是"流水线超限暂停"：**不要再指派**，只做阶段性总结并广播，等用户下一条消息
3. 结束前调用 team_report"""

    if spec.kind == "summary_turn":
        card = _task_card(team, task, roster_names)
        return f"""## 多人任务执行完毕，请你汇总（你是指派人）

{card}

## 你要做的

1. 审看各成员汇报，必要时读共享电脑上的产出文件核实
2. 达标：team_broadcast 阶段结论（并可 team_assign 下一阶段或 team_report 收尾）
3. 不达标：team_assign 返工（带 parent_task_id），说明问题所在
4. 结束前调用 team_report"""

    # member_run（含 review 回合）
    card = _task_card(team, task, roster_names)
    if spec.review:
        requester = roster_names.get(spec.requested_by, spec.requested_by)
        return f"""## 验收请求（团队负责人验收回合）

成员「{requester}」请求你验收以下任务：
{card}

## 你要做的

1. 读取产出文件（共享电脑路径）核实，按任务要求逐项判断
2. 通过：team_report(task_status="done", verdict="pass", verdict_comment=...) 并 team_broadcast 结论
3. 不通过：team_report(task_status="rejected", verdict="reject", verdict_comment=问题清单)
   **然后必须二选一**：team_assign 返工任务（parent_task_id=本任务）；或 team_broadcast 说明分歧需用户决策
4. 不要静默结束"""
    return f"""## 你的任务（团队成员回合，你是「{my_name}」）

{card}

## 你要做的

1. 按任务要求完成工作；需要动手时用共享电脑（产出写 public 路径）
2. 需要查阅协作规范读 team_read_sop
3. 完成后如需继续分工，可 team_assign 指派下一棒；需要负责人验收用 team_request_review
4. **结束前必须 team_report**：做了什么/结论/产出路径/遗留问题"""


# ---- 小工具 ----


def _serialize(content) -> list[dict]:
    serialized = []
    for block in content:
        btype = getattr(block, "type", None)
        if btype == "text":
            serialized.append({"type": "text", "text": block.text})
        elif btype == "tool_use":
            serialized.append({"type": "tool_use", "id": block.id,
                               "name": block.name, "input": block.input})
    return serialized


def _extract_text(response) -> str:
    parts = [b.text for b in response.content if getattr(b, "type", None) == "text"]
    return "\n".join(parts).strip()


async def _team_node_info(db, team_row) -> dict | None:
    """团队共享电脑信息（挂在团队行上的 remote_node）。"""
    node_id = getattr(team_row, "remote_node_id", "") or ""
    if not node_id:
        return None
    try:
        from openvort.db.models import RemoteNode
        node = await db.get(RemoteNode, node_id)
        if not node:
            return None
        return {"name": node.name, "status": node.status, "node_type": node.node_type}
    except Exception:
        return None


def new_run_id() -> str:
    return uuid.uuid4().hex
