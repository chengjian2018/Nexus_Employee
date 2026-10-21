"""vortteam 插件工具 — 团队员工管理（新建入队 / 拉入 / 移除）

所有工具共用宿主校验：_target_member_id（当前对话的 AI 员工）必须是某团队
负责人；负责人同时管理多个团队时需传 team_id。校验与业务规则全部复用
core.engine.team_service，与管理页 API 同一套口径。
"""

import json

from openvort.plugin.base import BaseTool
from openvort.utils.logging import get_logger

log = get_logger("plugins.vortteam.tools")


def _fail(message: str) -> str:
    return json.dumps({"ok": False, "message": message}, ensure_ascii=False)


async def _resolve_member(db, ident: str):
    """按成员 ID 或姓名精确匹配一个普通 AI 员工（排除团队行）。"""
    from sqlalchemy import select

    from openvort.contacts.models import Member
    from openvort.core.engine.team_service import TEAM_NAME_PREFIX

    ident = (ident or "").strip()
    if not ident:
        return None, "需要提供 member（成员姓名或成员 ID）"
    m = await db.get(Member, ident)
    if m and m.is_virtual and not (m.name or "").startswith(TEAM_NAME_PREFIX):
        return m, ""
    rows = (await db.execute(
        select(Member).where(Member.name == ident, Member.is_virtual == True)  # noqa: E712
    )).scalars().all()
    rows = [r for r in rows if not (r.name or "").startswith(TEAM_NAME_PREFIX)]
    if not rows:
        return None, f"未找到 AI 员工「{ident}」"
    if len(rows) > 1:
        return None, f"存在多个名为「{ident}」的 AI 员工，请改用成员 ID 精确指定"
    return rows[0], ""


class TeamCreateEmployeeTool(BaseTool):
    name = "team_create_employee"
    description = (
        "为当前团队新建一名 AI 数字员工并自动拉入团队。当团队负责人判断现有成员"
        "都不适合承接某项工作时使用。会通过两步生成自动产出完整三件套人设"
        "（identity/persona/bible），耗时约 1~2 分钟，请先告知用户稍候。仅团队负责人可用。"
    )

    def __init__(self, get_session_factory):
        self._get_sf = get_session_factory

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "新员工姓名，如「数据分析师小数」"},
                "description": {
                    "type": "string",
                    "description": "职责描述：要做什么、服务谁、边界在哪；越具体生成质量越高",
                },
                "role_note": {"type": "string", "description": "该成员在团队中的分工注记（可选）"},
                "team_id": {"type": "string", "description": "团队 ID（负责人同时管理多个团队时必填）"},
            },
            "required": ["name", "description"],
        }

    async def execute(self, params: dict) -> str:
        name = (params.get("name") or "").strip()
        description = (params.get("description") or "").strip()
        if not name or not description:
            return _fail("需要提供 name（姓名）和 description（职责描述）")

        from openvort.config.settings import get_settings
        from openvort.contacts.models import Member
        from openvort.core.engine import team_service
        from openvort.core.engine.llm import LLMClient
        from openvort.core.engine.virtual_persona import build_virtual_persona
        from openvort.db.models import MemberSkill

        sf = self._get_sf()
        async with sf() as db:
            team, err = await team_service.resolve_hosted_team(
                db, params.get("_target_member_id", ""), params.get("team_id", ""))
            if err:
                return _fail(err)
            team_name = team_service.strip_team_prefix(team.name)

            settings = get_settings()
            llm = LLMClient(settings.llm.get_model_chain())
            try:
                result = await team_service.generate_persona_with_kb(llm, db, name, description)
            except Exception as e:
                log.error(f"vortteam 新建员工生成失败: {e}")
                return _fail(f"三件套生成失败: {e}")

            # 建员工 + 订阅建议技能 + 入队为同一事务：入队校验失败则全部回滚
            member = Member(
                name=name,
                position=name,
                bio=description,
                is_virtual=True,
                virtual_system_prompt=build_virtual_persona(
                    result["identity"], result["persona"], result["bible"]),
                skills="[]",
            )
            db.add(member)
            await db.flush()
            for skill in result.get("suggested_skills", []):
                db.add(MemberSkill(member_id=member.id, skill_id=skill["id"], source="v2"))
            try:
                await team_service.add_team_member(db, team, member.id, params.get("role_note", ""))
            except team_service.TeamError as e:
                return _fail(f"创建失败（入队校验未通过，已整体回滚）: {e}")
            await db.commit()

        return json.dumps({
            "ok": True,
            "message": f"已创建 AI 员工「{name}」（三件套已生成）并加入团队「{team_name}」",
            "member_id": member.id,
            "team_id": team.id,
        }, ensure_ascii=False)


class TeamManageMemberTool(BaseTool):
    name = "team_manage_member"
    description = (
        "管理当前团队的成员：add 把现有 AI 员工拉入团队；remove 把成员移出团队。"
        "移除只解除团队关系、不删除该员工；团队最少保留 2 人；负责人不可被移除。"
        "用户提出「团队冗余/精简人手」时用 remove，缺人时优先用 add 复用现有员工。"
        "仅团队负责人可用。"
    )

    def __init__(self, get_session_factory):
        self._get_sf = get_session_factory

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "remove"], "description": "拉入或移出"},
                "member": {"type": "string", "description": "目标 AI 员工的姓名或成员 ID"},
                "role_note": {"type": "string", "description": "分工注记（add 时可选）"},
                "team_id": {"type": "string", "description": "团队 ID（负责人同时管理多个团队时必填）"},
            },
            "required": ["action", "member"],
        }

    async def execute(self, params: dict) -> str:
        action = params.get("action")
        if action not in ("add", "remove"):
            return _fail("action 只支持 add / remove")

        from openvort.core.engine import team_service

        sf = self._get_sf()
        async with sf() as db:
            team, err = await team_service.resolve_hosted_team(
                db, params.get("_target_member_id", ""), params.get("team_id", ""))
            if err:
                return _fail(err)
            team_name = team_service.strip_team_prefix(team.name)

            member, err = await _resolve_member(db, params.get("member", ""))
            if err:
                return _fail(err)

            try:
                if action == "add":
                    await team_service.add_team_member(
                        db, team, member.id, params.get("role_note", ""))
                    message = f"已把「{member.name}」拉入团队「{team_name}」"
                else:
                    await team_service.remove_team_member(db, team, member.id)
                    message = f"已把「{member.name}」移出团队「{team_name}」（该员工本身保留）"
            except team_service.TeamError as e:
                return _fail(str(e))
            await db.commit()

        return json.dumps({"ok": True, "message": message, "team_id": team.id}, ensure_ascii=False)
