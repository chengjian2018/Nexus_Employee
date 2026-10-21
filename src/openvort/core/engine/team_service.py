"""
AI 团队服务 — 团队即员工（members 表一行）

团队行与普通 AI 员工同存于 members 表，靠两点区分：
1. name 以固定前缀 `[团队] ` 开头（受控入口强制写入）；
2. virtual_system_prompt 是 V3 团队 JSON（version:3，含三件套 + 花名册 + leader + 移交规则）。

花名册（roster）的唯一权威就存在团队行的 V3 JSON 里；roster 变更时由本服务
重写整段 JSON（三件套不动，只重渲染花名册），管理页 API 与 vortteam 插件工具
共用这里的全部校验（min-2、leader 保护、去重、禁止团队嵌套）。
"""

from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from openvort.contacts.models import Member
from openvort.core.engine.virtual_persona import (
    build_team_persona, normalize_team_config, parse_team_persona,
)
from openvort.plugin import visibility as plugin_visibility

# 团队名固定前缀：列表过滤（V1/V2/聊天/建团候选）与肉眼识别共用这一个判据
TEAM_NAME_PREFIX = "[团队] "
MIN_TEAM_SIZE = 2

# 移交规则全局模板放在 vortteam 插件包的 assets 下（不放 prompts/，
# 避免 get_prompts() 把它注册进全局 system prompt）
_HANDOFF_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2] / "plugins" / "vortteam" / "assets" / "team_handoff.md"
)


class TeamError(ValueError):
    """团队操作的业务校验错误，消息可直接展示给用户/模型。"""


def normalize_team_name(name: str) -> str:
    """确保团队名带且仅带一个前缀；去掉用户输入里的重复前缀。"""
    core = (name or "").strip()
    while core.startswith(TEAM_NAME_PREFIX):
        core = core[len(TEAM_NAME_PREFIX):].strip()
    if not core:
        raise TeamError("团队名称不能为空")
    return TEAM_NAME_PREFIX + core


def strip_team_prefix(name: str) -> str:
    if name and name.startswith(TEAM_NAME_PREFIX):
        return name[len(TEAM_NAME_PREFIX):]
    return name or ""


def is_team_row(m: Member | None) -> bool:
    return bool(m and m.is_virtual and (m.name or "").startswith(TEAM_NAME_PREFIX))


def load_handoff_template() -> str:
    try:
        return _HANDOFF_TEMPLATE_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


# ---- 查询 ----


async def list_team_rows(db) -> list[Member]:
    return list((await db.execute(
        select(Member).where(
            Member.is_virtual == True,  # noqa: E712
            Member.name.like(f"{TEAM_NAME_PREFIX}%"),
        ).order_by(Member.created_at)
    )).scalars())


async def get_team(db, team_id: str) -> Member:
    m = await db.get(Member, team_id)
    if not m or not is_team_row(m):
        raise TeamError("团队不存在")
    return m


def team_payload(m: Member) -> dict:
    team = parse_team_persona(m.virtual_system_prompt) or {}
    return {
        "id": m.id,
        "name": strip_team_prefix(m.name),
        "raw_name": m.name,
        "description": m.bio or "",
        "identity": team.get("identity", ""),
        "persona": team.get("persona", ""),
        "bible": team.get("bible", ""),
        "leader_id": team.get("leader_id", ""),
        "roster": team.get("roster", []),
        "handoff_rules": team.get("handoff_rules", ""),
        "sop": team.get("sop") or {"summary": "", "full_text": ""},
        "config": team.get("config") or {},
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


async def _fetch_regular_members(db, member_ids: list[str]) -> list[Member]:
    """按 id 批量取普通 AI 员工（必须存在、is_virtual、且不是团队行）。"""
    if not member_ids:
        return []
    rows = (await db.execute(
        select(Member).where(Member.id.in_(member_ids))
    )).scalars().all()
    by_id = {m.id: m for m in rows}
    for mid in member_ids:
        m = by_id.get(mid)
        if not m:
            raise TeamError(f"成员 {mid} 不存在")
        if not m.is_virtual:
            raise TeamError(f"「{m.name}」不是 AI 员工，不能加入团队")
        if is_team_row(m):
            raise TeamError(f"「{strip_team_prefix(m.name)}」是一个团队，团队不能嵌套")
    return [by_id[mid] for mid in member_ids]


def _validate_roster(member_ids: list[str], leader_id: str | None) -> str:
    if len(member_ids) < MIN_TEAM_SIZE:
        raise TeamError(f"团队至少需要 {MIN_TEAM_SIZE} 名成员")
    if len(set(member_ids)) != len(member_ids):
        raise TeamError("成员列表存在重复")
    if leader_id is None:
        leader_id = member_ids[0]  # 默认：第一个被拉进来的成员是负责人
    if leader_id not in member_ids:
        raise TeamError("负责人必须在成员列表中")
    return leader_id


def _roster_entries(members: list[Member], role_notes: dict[str, str],
                    joined_ats: dict[str, str] | None = None) -> list[dict]:
    now = datetime.now().isoformat(timespec="seconds")
    entries = []
    for m in members:
        entries.append({
            "member_id": m.id,
            "name": m.name,
            "position": m.position or "",
            "role_note": (role_notes or {}).get(m.id, ""),
            "joined_at": (joined_ats or {}).get(m.id) or now,
        })
    return entries


def _rebuild_prompt(team: Member, identity: str, persona: str, bible: str,
                    leader_id: str, roster: list[dict], handoff_rules: str) -> None:
    current = parse_team_persona(team.virtual_system_prompt) or {}
    team.virtual_system_prompt = build_team_persona(
        identity, persona, bible, leader_id, roster, handoff_rules,
        sop=current.get("sop") or None,
        config=current.get("config") or None,
    )


# ---- 写操作 ----


async def _sync_leader_plugin_bindings(db, old_leader_id: str | None,
                                       new_leader_id: str | None) -> None:
    """负责人变化后，同步员工管理插件的自动 allow 绑定。

    新负责人立即绑定；旧负责人若不再担任任何团队的负责人则释放（admin 手配行不动）。
    """
    if new_leader_id and new_leader_id != old_leader_id:
        await plugin_visibility.ensure_auto_allow_bindings(db, new_leader_id)
    if old_leader_id and old_leader_id != new_leader_id:
        if not await resolve_leader_teams(db, old_leader_id):
            await plugin_visibility.release_auto_allow_bindings(db, old_leader_id)


async def reconcile_leader_plugin_bindings(db) -> tuple[int, int]:
    """启动对账（幂等）：为所有现任负责人补写自动绑定，清掉已卸任者的残留 auto 行。

    覆盖"功能上线前已建团队"与"异常中断留下的半截状态"两种情况。返回 (负责人数, 清理数)。
    """
    leader_ids: set[str] = set()
    for row in await list_team_rows(db):
        team = parse_team_persona(row.virtual_system_prompt)
        if team:
            leader_ids.add(team["leader_id"])
    for mid in leader_ids:
        await plugin_visibility.ensure_auto_allow_bindings(db, mid)

    Binding = plugin_visibility.PluginMemberBinding
    stray = set((await db.execute(
        select(Binding.member_id).where(
            Binding.source == plugin_visibility.SOURCE_AUTO,
            Binding.plugin_name.in_(plugin_visibility.TEAM_MANAGED_PLUGINS),
        )
    )).scalars().all())
    removed = 0
    for mid in stray - leader_ids:
        await plugin_visibility.release_auto_allow_bindings(db, mid)
        removed += 1
    await db.commit()
    return len(leader_ids), removed


async def create_team(db, name: str, description: str,
                      identity: str, persona: str, bible: str,
                      member_ids: list[str], role_notes: dict[str, str] | None = None,
                      leader_id: str | None = None) -> Member:
    for field, value in (("identity", identity), ("persona", persona), ("bible", bible)):
        if not (value or "").strip():
            raise TeamError(f"团队三件套缺少 {field}")
    resolved_leader = _validate_roster(member_ids, leader_id)
    members = await _fetch_regular_members(db, member_ids)
    handoff = load_handoff_template()
    team = Member(
        name=normalize_team_name(name),
        position="AI 团队",
        bio=(description or "").strip(),
        is_virtual=True,
        virtual_system_prompt="",
        skills="[]",
    )
    _rebuild_prompt(
        team, identity.strip(), persona.strip(), bible.strip(),
        resolved_leader, _roster_entries(members, role_notes or {}), handoff,
    )
    db.add(team)
    await db.flush()
    await _sync_leader_plugin_bindings(db, None, resolved_leader)
    return team


async def update_team_profile(db, team: Member, name: str | None = None,
                              description: str | None = None,
                              identity: str | None = None,
                              persona: str | None = None,
                              bible: str | None = None) -> Member:
    current = parse_team_persona(team.virtual_system_prompt)
    if not current:
        raise TeamError("团队数据损坏（人设 JSON 解析失败）")
    if name is not None:
        team.name = normalize_team_name(name)
    if description is not None:
        team.bio = description.strip()
    new_identity = (identity if identity is not None else current["identity"]).strip()
    new_persona = (persona if persona is not None else current["persona"]).strip()
    new_bible = (bible if bible is not None else current["bible"]).strip()
    for field, value in (("identity", new_identity), ("persona", new_persona), ("bible", new_bible)):
        if not value:
            raise TeamError(f"{field} 不能为空")
    _rebuild_prompt(team, new_identity, new_persona, new_bible,
                    current["leader_id"], current["roster"], current["handoff_rules"])
    await db.flush()
    return team


async def update_team_sop_config(db, team: Member, sop: dict | None = None,
                                 config: dict | None = None) -> Member:
    """更新团队 JSON 的可选键 sop / config（None 表示不动，空值清除该键）。"""
    current = parse_team_persona(team.virtual_system_prompt)
    if not current:
        raise TeamError("团队数据损坏（人设 JSON 解析失败）")
    new_sop = current.get("sop") or {}
    if sop is not None:
        new_sop = {
            "summary": (sop.get("summary") or "").strip(),
            "full_text": (sop.get("full_text") or "").strip(),
        }
        if not new_sop["summary"] and not new_sop["full_text"]:
            new_sop = {}
    new_config = current.get("config") or {}
    if config is not None:
        new_config = normalize_team_config(config)
    team.virtual_system_prompt = build_team_persona(
        current["identity"], current["persona"], current["bible"],
        current["leader_id"], current["roster"], current["handoff_rules"],
        sop=new_sop or None, config=new_config or None,
    )
    await db.flush()
    return team


async def update_roster(db, team: Member, member_ids: list[str],
                        role_notes: dict[str, str] | None = None,
                        leader_id: str | None = None) -> Member:
    current = parse_team_persona(team.virtual_system_prompt)
    if not current:
        raise TeamError("团队数据损坏（人设 JSON 解析失败）")
    resolved_leader = _validate_roster(member_ids, leader_id or current["leader_id"])
    members = await _fetch_regular_members(db, member_ids)
    joined_ats = {e["member_id"]: e.get("joined_at", "") for e in current["roster"]}
    notes = dict(role_notes or {})
    _rebuild_prompt(team, current["identity"], current["persona"], current["bible"],
                    resolved_leader, _roster_entries(members, notes, joined_ats),
                    current["handoff_rules"])
    await db.flush()
    await _sync_leader_plugin_bindings(db, current["leader_id"], resolved_leader)
    return team


async def add_team_member(db, team: Member, member_id: str, role_note: str = "") -> Member:
    current = parse_team_persona(team.virtual_system_prompt)
    if not current:
        raise TeamError("团队数据损坏（人设 JSON 解析失败）")
    if any(e["member_id"] == member_id for e in current["roster"]):
        raise TeamError("该成员已在团队中")
    member_ids = [e["member_id"] for e in current["roster"]] + [member_id]
    notes = {e["member_id"]: e.get("role_note", "") for e in current["roster"]}
    notes[member_id] = role_note or ""
    return await update_roster(db, team, member_ids, notes, current["leader_id"])


async def remove_team_member(db, team: Member, member_id: str) -> Member:
    current = parse_team_persona(team.virtual_system_prompt)
    if not current:
        raise TeamError("团队数据损坏（人设 JSON 解析失败）")
    if member_id == current["leader_id"]:
        raise TeamError("团队负责人不能被移除；如需更换，请先在管理页变更负责人")
    if not any(e["member_id"] == member_id for e in current["roster"]):
        raise TeamError("该成员不在团队中")
    if len(current["roster"]) <= MIN_TEAM_SIZE:
        raise TeamError(f"团队至少需要 {MIN_TEAM_SIZE} 名成员，不能继续移除；如需解散请使用解散操作")
    member_ids = [e["member_id"] for e in current["roster"] if e["member_id"] != member_id]
    notes = {e["member_id"]: e.get("role_note", "") for e in current["roster"] if e["member_id"] != member_id}
    return await update_roster(db, team, member_ids, notes, current["leader_id"])


async def change_team_leader(db, team: Member, leader_id: str) -> Member:
    current = parse_team_persona(team.virtual_system_prompt)
    if not current:
        raise TeamError("团队数据损坏（人设 JSON 解析失败）")
    if not any(e["member_id"] == leader_id for e in current["roster"]):
        raise TeamError("新负责人必须是当前团队成员")
    _rebuild_prompt(team, current["identity"], current["persona"], current["bible"],
                    leader_id, current["roster"], current["handoff_rules"])
    await db.flush()
    await _sync_leader_plugin_bindings(db, current["leader_id"], leader_id)
    return team


async def disband_team(db, team: Member) -> None:
    """解散 = 物理删除团队行（花名册随行消失）；成员员工行全部保留。"""
    current = parse_team_persona(team.virtual_system_prompt)
    old_leader = current["leader_id"] if current else None
    await db.delete(team)
    await db.flush()
    await _sync_leader_plugin_bindings(db, old_leader, None)


# ---- 插件宿主校验 ----


async def resolve_leader_teams(db, member_id: str) -> list[Member]:
    """某人（通常是 AI 员工）担任负责人的全部团队。"""
    teams = []
    for row in await list_team_rows(db):
        team = parse_team_persona(row.virtual_system_prompt)
        if team and team["leader_id"] == member_id:
            teams.append(row)
    return teams


async def resolve_hosted_team(db, target_member_id: str, team_id: str = ""):
    """插件宿主校验：当前对话的 AI 员工（_target_member_id）必须是某团队的负责人。

    返回 (team, error_message)：成功时 error_message 为空串。
    负责多个团队且未指定 team_id 时返回错误并列出可选团队。
    """
    if not target_member_id:
        return None, "员工管理工具仅在与团队负责人（AI 员工）对话时可用"
    teams = await resolve_leader_teams(db, target_member_id)
    if team_id:
        for t in teams:
            if t.id == team_id:
                return t, ""
        return None, "你不是该团队的负责人，无法执行此操作"
    if not teams:
        return None, "你不是任何团队的负责人，无法使用员工管理工具"
    if len(teams) > 1:
        names = "、".join(strip_team_prefix(t.name) for t in teams)
        return None, f"你同时是 {len(teams)} 个团队的负责人（{names}），请指定 team_id"
    return teams[0], ""


# ---- 三件套生成（团队/新员工共用，两步 LLM + 技能库素材） ----


async def generate_persona_with_kb(llm, db, name: str, description: str,
                                   regenerate_field: str | None = None,
                                   current: dict[str, str] | None = None) -> dict:
    """复用 V2 两步生成器：职能匹配 → 素材注入 → 生成三件套（或单份重生成）。

    返回 {"identity", "persona", "bible", "matched_functions", "suggested_skills"}。
    """
    import json as _json

    from openvort.core.engine.employee_generator import (
        format_kb_section, generate_persona, match_functions,
    )
    from openvort.db.employee_kb_models import (
        EmployeeFunction, EmployeeFunctionSkill, EmployeeSkillRef,
    )
    from openvort.db.models import Skill

    max_skills = 80
    functions = [{
        "function_id": f.function_id, "name": f.name,
        "positioning": f.positioning, "domain": f.domain,
        "categories": _json.loads(f.categories_json or "[]"),
    } for f in (await db.execute(
        select(EmployeeFunction).order_by(EmployeeFunction.function_id)
    )).scalars()]
    skills = [{
        "id": s.id, "name": s.name, "description": s.description,
    } for s in (await db.execute(
        select(Skill).where(
            Skill.enabled == True, Skill.scope.in_(["builtin", "public"])  # noqa: E712
        ).order_by(Skill.sort_order).limit(max_skills)
    )).scalars()]

    matched_functions: list[dict] = []
    mapped_skills: list[dict] = []
    suggested: list[dict] = []
    if functions:
        matched = await match_functions(llm, name, description, functions, skills)
        matched_functions = [f for f in functions if f["function_id"] in matched["functions"]]
        suggested = [s for s in skills if s["id"] in matched["openvort_skills"]]
        if matched_functions:
            rows = (await db.execute(
                select(EmployeeSkillRef, EmployeeFunctionSkill.confidence)
                .join(EmployeeFunctionSkill,
                      EmployeeFunctionSkill.skill_ref_id == EmployeeSkillRef.skill_ref_id)
                .where(EmployeeFunctionSkill.function_id.in_(
                    [f["function_id"] for f in matched_functions]))
                .order_by(EmployeeFunctionSkill.confidence.desc())
            )).all()
            seen: set[str] = set()
            for ref, _conf in rows:
                if ref.name in seen:
                    continue
                seen.add(ref.name)
                mapped_skills.append({
                    "name": ref.name, "description": ref.description, "summary_zh": ref.summary_zh,
                })
                if len(mapped_skills) >= 12:
                    break

    kb_section = format_kb_section(matched_functions, mapped_skills, suggested)
    parts = await generate_persona(
        llm, name, description, kb_section,
        regenerate_field=regenerate_field, current=current,
    )
    parts.setdefault("identity", "")
    parts.setdefault("persona", "")
    parts.setdefault("bible", "")
    parts["matched_functions"] = [
        {"function_id": f["function_id"], "name": f["name"], "positioning": f["positioning"]}
        for f in matched_functions
    ]
    parts["suggested_skills"] = suggested
    return parts
