"""AI 员工 V2 — 岗位库 + V2 员工 + 三件套自然语言生成（技能库强化）

三件套以 V2 JSON 存在现有 Post.default_persona / Member.virtual_system_prompt
字段里（不改表结构）；本路由是 V2 的唯一写入口，现有 members / posts 路由不动。
"""

import json
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, func, select

from openvort.config.settings import get_settings
from openvort.contacts.models import Member
from openvort.core.engine.employee_generator import (
    generate_persona, match_functions, format_kb_section,
)
from openvort.core.engine.llm import LLMClient
from openvort.core.engine.virtual_persona import (
    build_virtual_persona, is_virtual_persona_v2, parse_virtual_persona,
)
from openvort.db.employee_kb_models import (
    EmployeeFunction, EmployeeFunctionSkill, EmployeePostSkill, EmployeeSkillRef,
)
from openvort.db.models import MemberSkill, Post, Skill
from openvort.web.deps import get_db_session_factory

router = APIRouter()

# V2 员工技能订阅在 MemberSkill.source 上的标记（同步时只动自己写的行）
SKILL_SOURCE = "v2"

_MAX_SUBSCRIBABLE_SKILLS = 80  # 交给 LLM 挑选的平台技能上限


# ---- Request / Response models ----

class CreatePostRequest(BaseModel):
    name: str
    description: str = ""
    key: str = ""
    identity: str
    persona: str
    bible: str


class UpdatePostRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    identity: str | None = None
    persona: str | None = None
    bible: str | None = None


class InstantiateRequest(BaseModel):
    name: str
    position: str = ""
    bio: str = ""


class CreateMemberRequest(BaseModel):
    name: str
    position: str = ""
    bio: str = ""
    post_key: str = ""
    identity: str
    persona: str
    bible: str
    skill_ids: list[str] = []


class UpdateMemberRequest(BaseModel):
    name: str | None = None
    position: str | None = None
    bio: str | None = None
    identity: str | None = None
    persona: str | None = None
    bible: str | None = None
    skill_ids: list[str] | None = None


class GenerateRequest(BaseModel):
    name: str
    description: str = ""
    regenerate_field: str | None = None  # identity / persona / bible，单份重生成
    current: dict[str, str] = {}  # 单份重生成时提供的现有三件套


# ---- helpers ----

def _post_item(post: Post) -> dict:
    parts = parse_virtual_persona(post.default_persona) or {}
    return {
        "id": post.id,
        "key": post.key,
        "name": post.name,
        "description": post.description,
        "identity": parts.get("identity", ""),
        "persona": parts.get("persona", ""),
        "bible": parts.get("bible", ""),
        "enabled": post.enabled,
        "created_at": post.created_at.isoformat() if post.created_at else None,
    }


def _member_item(m: Member, skill_ids: list[str] | None = None) -> dict:
    parts = parse_virtual_persona(m.virtual_system_prompt) or {}
    return {
        "id": m.id,
        "name": m.name,
        "position": m.position,
        "bio": m.bio,
        "post": m.post,
        "identity": parts.get("identity", ""),
        "persona": parts.get("persona", ""),
        "bible": parts.get("bible", ""),
        "skill_ids": skill_ids or [],
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


async def _post_skills_map(db, post_keys: list[str]) -> dict[str, list[dict]]:
    """V2 岗位 → 素材库技能列表（skill_kb default_skills 快照）。"""
    if not post_keys:
        return {}
    rows = (await db.execute(
        select(EmployeePostSkill.post_key, EmployeeSkillRef.name, EmployeeSkillRef.description)
        .join(EmployeeSkillRef, EmployeeSkillRef.skill_ref_id == EmployeePostSkill.skill_ref_id)
        .where(EmployeePostSkill.post_key.in_(post_keys), EmployeePostSkill.enabled == True)  # noqa: E712
        .order_by(EmployeePostSkill.post_key, EmployeePostSkill.id)
    )).all()
    result: dict[str, list[dict]] = {}
    for key, name, description in rows:
        result.setdefault(key, []).append({"name": name, "description": (description or "")[:100]})
    return result


def _require_persona_parts(identity: str, persona: str, bible: str) -> None:
    for field, value in (("identity", identity), ("persona", persona), ("bible", bible)):
        if not (value or "").strip():
            raise HTTPException(status_code=400, detail=f"{field} 不能为空")


async def _get_v2_post(db, post_id: str) -> Post:
    post = await db.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="岗位不存在")
    if not is_virtual_persona_v2(post.default_persona):
        raise HTTPException(status_code=400, detail="非 V2 岗位，请在原岗位管理中编辑")
    return post


async def _get_v2_member(db, member_id: str) -> Member:
    m = await db.get(Member, member_id)
    if not m:
        raise HTTPException(status_code=404, detail="员工不存在")
    if not is_virtual_persona_v2(m.virtual_system_prompt):
        raise HTTPException(status_code=400, detail="非 V2 员工，请在 AI 员工页编辑")
    return m


async def _sync_member_skills(db, member_id: str, skill_ids: list[str]) -> None:
    """只同步 V2 写入的订阅行（source='v2'），不动其他来源。"""
    await db.execute(
        delete(MemberSkill).where(MemberSkill.member_id == member_id, MemberSkill.source == SKILL_SOURCE)
    )
    for skill_id in skill_ids:
        db.add(MemberSkill(member_id=member_id, skill_id=skill_id, source=SKILL_SOURCE))


# ---- V2 标记（V1 页只读保护用） ----

@router.get("/v2-ids")
async def get_v2_ids():
    """列出全部 V2 岗位 / V2 员工的 id，供 V1 页面做只读标记。"""
    factory = get_db_session_factory()
    async with factory() as db:
        posts = (await db.execute(select(Post.id, Post.default_persona))).all()
        members = (await db.execute(
            select(Member.id, Member.virtual_system_prompt).where(Member.is_virtual == True)  # noqa: E712
        )).all()
    return {
        "post_ids": [pid for pid, persona in posts if is_virtual_persona_v2(persona)],
        "member_ids": [mid for mid, vsp in members if is_virtual_persona_v2(vsp)],
    }


# ---- 素材库统计 ----

@router.get("/kb/stats")
async def get_kb_stats():
    factory = get_db_session_factory()
    async with factory() as db:
        functions = (await db.execute(select(func.count()).select_from(EmployeeFunction))).scalar() or 0
        skills = (await db.execute(select(func.count()).select_from(EmployeeSkillRef))).scalar() or 0
        mappings = (await db.execute(select(func.count()).select_from(EmployeeFunctionSkill))).scalar() or 0
    return {"functions": functions, "skill_refs": skills, "mappings": mappings}


# ---- 岗位库 ----

@router.get("/posts")
async def list_posts():
    factory = get_db_session_factory()
    async with factory() as db:
        rows = (await db.execute(select(Post).order_by(Post.created_at))).scalars().all()
        v2_rows = [p for p in rows if is_virtual_persona_v2(p.default_persona)]
        skills_map = await _post_skills_map(db, [p.key for p in v2_rows])
    items = [{**_post_item(p), "skills": skills_map.get(p.key, [])} for p in v2_rows]
    return {"posts": items, "total": len(items)}


@router.post("/posts")
async def create_post(req: CreatePostRequest):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="名称不能为空")
    _require_persona_parts(req.identity, req.persona, req.bible)

    key = (req.key or "").strip() or f"v2-{uuid.uuid4().hex[:12]}"
    factory = get_db_session_factory()
    async with factory() as db:
        exists = (await db.execute(select(Post).where(Post.key == key))).scalar_one_or_none()
        if exists:
            raise HTTPException(status_code=409, detail=f"岗位 key '{key}' 已存在")
        post = Post(
            id=uuid.uuid4().hex,
            key=key,
            name=name,
            description=req.description.strip(),
            default_persona=build_virtual_persona(req.identity, req.persona, req.bible),
            enabled=True,
        )
        db.add(post)
        await db.commit()
    return {"success": True, "id": post.id, "key": post.key}


@router.put("/posts/{post_id}")
async def update_post(post_id: str, req: UpdatePostRequest):
    factory = get_db_session_factory()
    async with factory() as db:
        post = await _get_v2_post(db, post_id)
        if req.name is not None:
            if not req.name.strip():
                raise HTTPException(status_code=400, detail="名称不能为空")
            post.name = req.name.strip()
        if req.description is not None:
            post.description = req.description.strip()
        if any(getattr(req, f) is not None for f in ("identity", "persona", "bible")):
            parts = parse_virtual_persona(post.default_persona) or {"identity": "", "persona": "", "bible": ""}
            for f in ("identity", "persona", "bible"):
                value = getattr(req, f)
                if value is not None:
                    if not value.strip():
                        raise HTTPException(status_code=400, detail=f"{f} 不能为空")
                    parts[f] = value.strip()
            post.default_persona = build_virtual_persona(parts["identity"], parts["persona"], parts["bible"])
        await db.commit()
    return {"success": True}


@router.delete("/posts/{post_id}")
async def delete_post(post_id: str):
    """逻辑删除（enabled=False），与 V1 岗位删除口径一致。"""
    factory = get_db_session_factory()
    async with factory() as db:
        post = await _get_v2_post(db, post_id)
        post.enabled = False
        await db.commit()
    return {"success": True}


@router.post("/posts/{post_id}/instantiate")
async def instantiate_post(post_id: str, req: InstantiateRequest):
    """从 V2 岗位实例化一个 AI 员工（persona JSON 原样拷贝，同 create_member 链路）。"""
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="员工姓名不能为空")

    factory = get_db_session_factory()
    async with factory() as db:
        post = await _get_v2_post(db, post_id)
        member = Member(
            name=name,
            position=(req.position or "").strip() or post.name,
            bio=(req.bio or "").strip() or post.description,
            is_virtual=True,
            post=post.key,
            virtual_role=post.key,
            virtual_system_prompt=post.default_persona,
            skills=json.dumps([post.key]),
            auto_report=post.default_auto_report,
            report_frequency=post.default_report_frequency,
        )
        db.add(member)
        await db.commit()
    return {"success": True, "id": member.id}


# ---- V2 员工 ----

@router.get("/members")
async def list_members():
    factory = get_db_session_factory()
    async with factory() as db:
        rows = (await db.execute(
            select(Member).where(Member.is_virtual == True).order_by(Member.created_at)  # noqa: E712
        )).scalars().all()
        items = []
        for m in rows:
            if not is_virtual_persona_v2(m.virtual_system_prompt):
                continue
            skill_ids = (await db.execute(
                select(MemberSkill.skill_id).where(
                    MemberSkill.member_id == m.id, MemberSkill.source == SKILL_SOURCE)
            )).scalars().all()
            items.append(_member_item(m, list(skill_ids)))
    return {"members": items, "total": len(items)}


@router.get("/members/{member_id}")
async def get_member(member_id: str):
    factory = get_db_session_factory()
    async with factory() as db:
        m = await _get_v2_member(db, member_id)
        skills = (await db.execute(
            select(MemberSkill, Skill.name).join(Skill, Skill.id == MemberSkill.skill_id).where(
                MemberSkill.member_id == m.id, MemberSkill.source == SKILL_SOURCE)
        )).all()
        item = _member_item(m, [ms.skill_id for ms, _ in skills])
        item["skills"] = [{"id": ms.skill_id, "name": name} for ms, name in skills]
        if m.post:
            post_skills = await _post_skills_map(db, [m.post])
            item["post_skills"] = post_skills.get(m.post, [])
    return item


@router.post("/members")
async def create_member(req: CreateMemberRequest):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="员工姓名不能为空")
    _require_persona_parts(req.identity, req.persona, req.bible)

    factory = get_db_session_factory()
    async with factory() as db:
        post_key = (req.post_key or "").strip()
        if post_key:
            post = (await db.execute(select(Post).where(Post.key == post_key))).scalar_one_or_none()
            if not post:
                raise HTTPException(status_code=400, detail=f"岗位 '{post_key}' 不存在")
            position = (req.position or "").strip() or post.name
        else:
            position = (req.position or "").strip()

        member = Member(
            name=name,
            position=position,
            bio=(req.bio or "").strip(),
            is_virtual=True,
            post=post_key,
            virtual_role=post_key,
            virtual_system_prompt=build_virtual_persona(req.identity, req.persona, req.bible),
            skills=json.dumps([post_key]) if post_key else "[]",
        )
        db.add(member)
        await db.flush()
        await _sync_member_skills(db, member.id, req.skill_ids)
        await db.commit()
    return {"success": True, "id": member.id}


@router.put("/members/{member_id}")
async def update_member(member_id: str, req: UpdateMemberRequest):
    factory = get_db_session_factory()
    async with factory() as db:
        m = await _get_v2_member(db, member_id)
        if req.name is not None:
            if not req.name.strip():
                raise HTTPException(status_code=400, detail="姓名不能为空")
            m.name = req.name.strip()
        if req.position is not None:
            m.position = req.position.strip()
        if req.bio is not None:
            m.bio = req.bio.strip()
        if any(getattr(req, f) is not None for f in ("identity", "persona", "bible")):
            parts = parse_virtual_persona(m.virtual_system_prompt) or {"identity": "", "persona": "", "bible": ""}
            for f in ("identity", "persona", "bible"):
                value = getattr(req, f)
                if value is not None:
                    if not value.strip():
                        raise HTTPException(status_code=400, detail=f"{f} 不能为空")
                    parts[f] = value.strip()
            m.virtual_system_prompt = build_virtual_persona(parts["identity"], parts["persona"], parts["bible"])
        if req.skill_ids is not None:
            await _sync_member_skills(db, m.id, req.skill_ids)
        await db.commit()
    return {"success": True}


# ---- 三件套生成（草稿不落库） ----

@router.post("/generate")
async def generate(req: GenerateRequest):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="名称不能为空")

    settings = get_settings()
    llm = LLMClient(settings.llm.get_model_chain())

    factory = get_db_session_factory()
    async with factory() as db:
        functions = [{
            "function_id": f.function_id, "name": f.name,
            "positioning": f.positioning, "domain": f.domain,
            "categories": json.loads(f.categories_json or "[]"),
        } for f in (await db.execute(select(EmployeeFunction).order_by(EmployeeFunction.function_id))).scalars()]
        skills = [{
            "id": s.id, "name": s.name, "description": s.description,
        } for s in (await db.execute(
            select(Skill).where(Skill.enabled == True, Skill.scope.in_(["builtin", "public"]))  # noqa: E712
            .order_by(Skill.sort_order).limit(_MAX_SUBSCRIBABLE_SKILLS)
        )).scalars()]

        matched_functions, mapped_skills = [], []
        if functions:
            try:
                matched = await match_functions(llm, name, req.description, functions, skills)
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"职能匹配失败: {e}")
            matched_functions = [f for f in functions if f["function_id"] in matched["functions"]]
            suggested = [s for s in skills if s["id"] in matched["openvort_skills"]]

            if matched_functions:
                rows = (await db.execute(
                    select(EmployeeSkillRef, EmployeeFunctionSkill.confidence)
                    .join(EmployeeFunctionSkill,
                          EmployeeFunctionSkill.skill_ref_id == EmployeeSkillRef.skill_ref_id)
                    .where(EmployeeFunctionSkill.function_id.in_([f["function_id"] for f in matched_functions]))
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
        else:
            suggested = []

    kb_section = format_kb_section(matched_functions, mapped_skills, suggested)
    try:
        parts = await generate_persona(
            llm, name, req.description, kb_section,
            regenerate_field=req.regenerate_field, current=req.current,
        )
    except ValueError as e:
        raise HTTPException(status_code=502, detail=f"生成失败: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"生成失败: {e}")

    return {
        "identity": parts.get("identity", ""),
        "persona": parts.get("persona", ""),
        "bible": parts.get("bible", ""),
        "matched_functions": [
            {"function_id": f["function_id"], "name": f["name"], "positioning": f["positioning"]}
            for f in matched_functions
        ],
        "suggested_skills": suggested,
    }
