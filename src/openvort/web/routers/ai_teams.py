"""AI 团队管理 — 团队即员工（members 表一行，name 前缀 + V3 人设 JSON）

团队的唯一写入口：建团/改三件套/改花名册/换负责人/解散，以及团队版三件套
生成（草稿不落库）。成员候选只含普通 AI 员工（排除团队行与真人）。
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from openvort.config.settings import get_settings
from openvort.contacts.models import Member
from openvort.core.engine import team_service
from openvort.core.engine.llm import LLMClient
from openvort.core.engine.team_service import TeamError
from openvort.web.deps import get_db_session_factory

router = APIRouter()


# ---- Request models ----

class RosterEntryIn(BaseModel):
    member_id: str
    role_note: str = ""


class CreateTeamRequest(BaseModel):
    name: str
    description: str = ""
    identity: str
    persona: str
    bible: str
    members: list[RosterEntryIn]
    leader_id: str = ""  # 缺省为第一个成员


class UpdateTeamRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    identity: str | None = None
    persona: str | None = None
    bible: str | None = None


class UpdateRosterRequest(BaseModel):
    members: list[RosterEntryIn]
    leader_id: str = ""


class UpdateSopConfigRequest(BaseModel):
    sop_summary: str | None = None
    sop_full_text: str | None = None
    config: dict | None = None  # 护栏参数：max_runs/max_rework/run_timeout/max_rounds_*


class AddMemberRequest(BaseModel):
    member_id: str
    role_note: str = ""


class ChangeLeaderRequest(BaseModel):
    leader_id: str


class GenerateTeamRequest(BaseModel):
    name: str
    description: str = ""
    member_posts: list[str] = []  # 成员岗位摘要，作为生成素材
    regenerate_field: str | None = None
    current: dict[str, str] = {}


def _err(e: TeamError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(e))


# ---- 团队 CRUD ----


@router.get("/teams")
async def list_teams():
    factory = get_db_session_factory()
    async with factory() as db:
        rows = await team_service.list_team_rows(db)
        return {"teams": [team_service.team_payload(m) for m in rows], "total": len(rows)}


@router.get("/teams/{team_id}")
async def get_team(team_id: str):
    factory = get_db_session_factory()
    async with factory() as db:
        team = await team_service.get_team(db, team_id)
        return team_service.team_payload(team)


@router.post("/teams")
async def create_team(req: CreateTeamRequest):
    factory = get_db_session_factory()
    async with factory() as db:
        try:
            team = await team_service.create_team(
                db, req.name, req.description,
                req.identity, req.persona, req.bible,
                [m.member_id for m in req.members],
                {m.member_id: m.role_note for m in req.members},
                req.leader_id or None,
            )
        except TeamError as e:
            raise _err(e)
        await db.commit()
        return {"success": True, "id": team.id}


@router.put("/teams/{team_id}")
async def update_team(team_id: str, req: UpdateTeamRequest):
    factory = get_db_session_factory()
    async with factory() as db:
        team = await team_service.get_team(db, team_id)
        try:
            await team_service.update_team_profile(
                db, team, name=req.name, description=req.description,
                identity=req.identity, persona=req.persona, bible=req.bible,
            )
        except TeamError as e:
            raise _err(e)
        await db.commit()
    return {"success": True}


@router.put("/teams/{team_id}/roster")
async def update_roster(team_id: str, req: UpdateRosterRequest):
    factory = get_db_session_factory()
    async with factory() as db:
        team = await team_service.get_team(db, team_id)
        try:
            await team_service.update_roster(
                db, team,
                [m.member_id for m in req.members],
                {m.member_id: m.role_note for m in req.members},
                req.leader_id or None,
            )
        except TeamError as e:
            raise _err(e)
        await db.commit()
    return {"success": True}


@router.put("/teams/{team_id}/sop-config")
async def update_sop_config(team_id: str, req: UpdateSopConfigRequest):
    """编排链路的团队级配置：沟通 SOP（摘要常驻 / 全文按需读）与护栏参数。"""
    factory = get_db_session_factory()
    async with factory() as db:
        team = await team_service.get_team(db, team_id)
        sop = None
        if req.sop_summary is not None or req.sop_full_text is not None:
            current = team_service.team_payload(team)["sop"]
            sop = {
                "summary": req.sop_summary if req.sop_summary is not None else current.get("summary", ""),
                "full_text": req.sop_full_text if req.sop_full_text is not None else current.get("full_text", ""),
            }
        try:
            await team_service.update_team_sop_config(db, team, sop=sop, config=req.config)
        except TeamError as e:
            raise _err(e)
        await db.commit()
        return team_service.team_payload(team)


@router.post("/teams/{team_id}/members")
async def add_member(team_id: str, req: AddMemberRequest):
    factory = get_db_session_factory()
    async with factory() as db:
        team = await team_service.get_team(db, team_id)
        try:
            await team_service.add_team_member(db, team, req.member_id, req.role_note)
        except TeamError as e:
            raise _err(e)
        await db.commit()
    return {"success": True}


@router.delete("/teams/{team_id}/members/{member_id}")
async def remove_member(team_id: str, member_id: str):
    factory = get_db_session_factory()
    async with factory() as db:
        team = await team_service.get_team(db, team_id)
        try:
            await team_service.remove_team_member(db, team, member_id)
        except TeamError as e:
            raise _err(e)
        await db.commit()
    return {"success": True}


@router.put("/teams/{team_id}/leader")
async def change_leader(team_id: str, req: ChangeLeaderRequest):
    factory = get_db_session_factory()
    async with factory() as db:
        team = await team_service.get_team(db, team_id)
        try:
            await team_service.change_team_leader(db, team, req.leader_id)
        except TeamError as e:
            raise _err(e)
        await db.commit()
    return {"success": True}


@router.delete("/teams/{team_id}")
async def disband_team(team_id: str):
    """解散 = 物理删除团队行；成员员工行全部保留。"""
    factory = get_db_session_factory()
    async with factory() as db:
        team = await team_service.get_team(db, team_id)
        await team_service.disband_team(db, team)
        await db.commit()
    return {"success": True}


# ---- 成员候选 / 移交规则模板 ----


@router.get("/candidates")
async def list_candidates():
    """普通 AI 员工候选（排除团队行与真人），供建团/拉人选择。"""
    factory = get_db_session_factory()
    async with factory() as db:
        rows = (await db.execute(
            select(Member).where(
                Member.is_virtual == True,  # noqa: E712
                Member.status == "active",
                ~Member.name.like(f"{team_service.TEAM_NAME_PREFIX}%"),
            ).order_by(Member.created_at)
        )).scalars().all()
        return {
            "candidates": [{
                "id": m.id, "name": m.name, "position": m.position or "",
                "bio": m.bio or "",
            } for m in rows],
            "total": len(rows),
        }


@router.get("/handoff-template")
async def get_handoff_template():
    return {"template": team_service.load_handoff_template()}


# ---- 团队版三件套生成（草稿不落库） ----


@router.post("/generate")
async def generate(req: GenerateTeamRequest):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="团队名称不能为空")

    description = (req.description or "").strip()
    if req.member_posts:
        posts = "\n".join(f"- {p.strip()}" for p in req.member_posts if p.strip())
        description = f"{description}\n\n团队构成（成员岗位）：\n{posts}".strip()

    settings = get_settings()
    llm = LLMClient(settings.llm.get_model_chain())
    factory = get_db_session_factory()
    async with factory() as db:
        try:
            result = await team_service.generate_persona_with_kb(
                llm, db, name, description,
                regenerate_field=req.regenerate_field, current=req.current,
            )
        except ValueError as e:
            raise HTTPException(status_code=502, detail=f"生成失败: {e}")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"生成失败: {e}")
    return {
        "identity": result.get("identity", ""),
        "persona": result.get("persona", ""),
        "bible": result.get("bible", ""),
        "matched_functions": result.get("matched_functions", []),
        "suggested_skills": result.get("suggested_skills", []),
    }
