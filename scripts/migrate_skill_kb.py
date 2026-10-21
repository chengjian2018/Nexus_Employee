"""
Migrate skill_kb digital-employee data into OpenVort (AI 员工 V2).

Usage:
    python scripts/migrate_skill_kb.py [--dry-run] [--skip-posts]
        [--qoderwake-db PATH] [--kb-db PATH] [--taxonomy PATH]

What it does (idempotent, safe to re-run):
1. Material tables (full refresh each run):
   - taxonomy.yaml 49 functions  -> employee_functions
   - kb.db canonical skills      -> employee_skill_refs (name/description/summary only)
   - kb.db skill_function        -> employee_function_skills
2. Posts (one-time, skip + report on conflict):
   - qoderwake.db waker_template (50 rows) -> Post with default_persona
     stored as V2 JSON {"version":2,"identity","persona","bible"}.
   - skips templates whose key/name already exists in OpenVort
     (already-migrated V2 keys are skipped silently).

The target DB comes from OpenVort settings (OPENVORT_DATABASE_URL env or
.env), the same database the web app uses.
"""

import argparse
import asyncio
import json
import sqlite3
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import yaml  # noqa: E402

DEFAULT_QODERWAKE_DB = "/Users/chengjian/py_projects/skill_kb/data/qoderwake.db"
DEFAULT_KB_DB = "/Users/chengjian/py_projects/skill_kb/skill_knowledge/kb/kb.db"
DEFAULT_TAXONOMY = "/Users/chengjian/py_projects/skill_kb/taxonomy.yaml"

POST_KEY_MAX = 32
POST_DESC_MAX = 256


def _load_taxonomy_functions(taxonomy_path: Path) -> list[dict]:
    with open(taxonomy_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    functions = []
    for fn in data.get("functions") or []:
        functions.append({
            "function_id": fn.get("id", ""),
            "name": fn.get("name", ""),
            "name_en": fn.get("name_en", ""),
            "positioning": fn.get("positioning", ""),
            "domain": fn.get("domain", ""),
            "categories": [
                {"id": c.get("id", ""), "name": c.get("name", ""), "description": c.get("description", "")}
                for c in (fn.get("categories") or [])
            ],
        })
    return [f for f in functions if f["function_id"] and f["name"]]


def _load_kb_skills(kb_db: Path) -> list[dict]:
    conn = sqlite3.connect(f"file:{kb_db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, name, COALESCE(description,''), COALESCE(summary_zh,'') "
            "FROM skills WHERE safety='pass' AND is_canonical=1"
        ).fetchall()
    finally:
        conn.close()
    return [{"skill_ref_id": r[0], "name": r[1] or "", "description": r[2], "summary_zh": r[3]} for r in rows]


def _load_function_skills(kb_db: Path, valid_fn_ids: set[str], valid_skill_ids: set[int]) -> list[tuple]:
    conn = sqlite3.connect(f"file:{kb_db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT function_id, skill_id, COALESCE(confidence,0) FROM skill_function"
        ).fetchall()
    finally:
        conn.close()
    return [
        (fn_id, skill_id, conf)
        for fn_id, skill_id, conf in rows
        if fn_id in valid_fn_ids and skill_id in valid_skill_ids
    ]


def _load_waker_templates(qoderwake_db: Path) -> list[dict]:
    conn = sqlite3.connect(f"file:{qoderwake_db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, type, name, description, COALESCE(role_files,''), COALESCE(default_skills,'') "
            "FROM waker_template WHERE status='published'"
        ).fetchall()
    finally:
        conn.close()

    templates = []
    for tpl_id, tpl_type, name, description, role_files, default_skills in rows:
        try:
            files = json.loads(role_files) if role_files else {}
        except ValueError:
            files = {}
        try:
            skills = json.loads(default_skills) if default_skills else []
        except ValueError:
            skills = []
        templates.append({
            "id": tpl_id, "type": tpl_type, "name": name,
            "description": description or "",
            "role_files": files if isinstance(files, dict) else {},
            "default_skills": skills if isinstance(skills, list) else [],
        })
    return templates


def _post_key_for(tpl_id: str, taken: set[str]) -> str:
    """skill_kb 模板 id -> Post.key（varchar(32)；超长截断，冲突则加短后缀重试）。"""
    key = tpl_id.strip().lower()
    if not key:
        return ""
    while key in taken:
        suffix = uuid.uuid4().hex[:6]
        key = key[: POST_KEY_MAX - len(suffix) - 1] + "-" + suffix
    return key[:POST_KEY_MAX]


async def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate skill_kb data into OpenVort (AI 员工 V2)")
    parser.add_argument("--qoderwake-db", default=DEFAULT_QODERWAKE_DB)
    parser.add_argument("--kb-db", default=DEFAULT_KB_DB)
    parser.add_argument("--taxonomy", default=DEFAULT_TAXONOMY)
    parser.add_argument("--skip-posts", action="store_true", help="只刷新素材表，不迁移岗位")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写入")
    args = parser.parse_args()

    qoderwake_db, kb_db, taxonomy = Path(args.qoderwake_db), Path(args.kb_db), Path(args.taxonomy)
    for p in (qoderwake_db, kb_db, taxonomy):
        if not p.exists():
            print(f"[错误] 数据源不存在: {p}")
            return 1

    from sqlalchemy import delete, select

    from openvort.config.settings import get_settings
    from openvort.core.engine.virtual_persona import build_virtual_persona, is_virtual_persona_v2
    from openvort.db.employee_kb_models import (
        EmployeeFunction, EmployeeFunctionSkill, EmployeePostSkill, EmployeeSkillRef,
    )
    from openvort.db.engine import init_db
    from openvort.db.models import Post

    settings = get_settings()
    print(f"目标库: {settings.database_url}")
    await init_db(settings.database_url)

    from openvort.db.engine import get_session_factory
    sf = get_session_factory()

    functions = _load_taxonomy_functions(taxonomy)
    skills = _load_kb_skills(kb_db)
    print(f"数据源: {len(functions)} 职能, {len(skills)} 技能素材")

    # ---- 1. 素材表全量刷新 ----
    fn_ids = {f["function_id"] for f in functions}
    skill_ids = {s["skill_ref_id"] for s in skills}
    function_skills = _load_function_skills(kb_db, fn_ids, skill_ids)
    print(f"职能-技能映射: {len(function_skills)} 条")

    async with sf() as db:
        # 分阶段 flush，确保父表（职能/技能素材）先于映射表落库
        await db.execute(delete(EmployeePostSkill))
        await db.execute(delete(EmployeeFunctionSkill))
        await db.execute(delete(EmployeeSkillRef))
        await db.execute(delete(EmployeeFunction))
        await db.flush()
        for f in functions:
            db.add(EmployeeFunction(
                function_id=f["function_id"], name=f["name"], name_en=f["name_en"],
                positioning=f["positioning"], domain=f["domain"],
                categories_json=json.dumps(f["categories"], ensure_ascii=False),
            ))
        await db.flush()
        for s in skills:
            db.add(EmployeeSkillRef(
                skill_ref_id=s["skill_ref_id"], name=s["name"],
                description=s["description"], summary_zh=s["summary_zh"],
            ))
        await db.flush()
        for fn_id, skill_id, conf in function_skills:
            db.add(EmployeeFunctionSkill(function_id=fn_id, skill_ref_id=skill_id, confidence=conf))
        if args.dry_run:
            await db.rollback()
            print("[dry-run] 素材表刷新已回滚")
        else:
            await db.commit()
            print(f"素材表已刷新: {len(functions)} 职能 / {len(skills)} 技能 / {len(function_skills)} 映射")

    if args.skip_posts:
        return 0

    # ---- 2. 岗位迁移（幂等：已迁 V2 跳过，冲突 skip + 报告） ----
    templates = _load_waker_templates(qoderwake_db)
    print(f"待迁移岗位模板: {len(templates)}")

    migrated, skipped = [], []
    async with sf() as db:
        existing = {
            key: persona for key, persona in (
                await db.execute(select(Post.key, Post.default_persona))
            ).all()
        }
        existing_names = {
            name for (name,) in (await db.execute(select(Post.name))).all()
        }

        for tpl in templates:
            files = tpl["role_files"]
            if not all((files.get(k) or "").strip() for k in ("identity_md", "persona_md", "bible_md")):
                skipped.append((tpl["name"], "role_files 缺少三源之一"))
                continue

            if tpl["id"] in existing:
                if is_virtual_persona_v2(existing[tpl["id"]]):
                    continue  # 已迁移过，幂等跳过
                skipped.append((tpl["name"], f"key '{tpl['id']}' 已存在（非 V2）"))
                continue

            if tpl["name"] in existing_names:
                skipped.append((tpl["name"], "同名岗位已存在"))
                continue

            key = _post_key_for(tpl["id"], set(existing.keys()))
            if not key:
                skipped.append((tpl["name"], "无法生成有效 key"))
                continue

            persona_json = build_virtual_persona(
                files["identity_md"], files["persona_md"], files["bible_md"],
            )
            if not args.dry_run:
                db.add(Post(
                    id=uuid.uuid4().hex,
                    key=key,
                    name=tpl["name"],
                    description=tpl["description"][:POST_DESC_MAX],
                    default_persona=persona_json,
                    enabled=True,
                ))
            existing[key] = persona_json
            existing_names.add(tpl["name"])
            migrated.append((tpl["name"], key))

        if args.dry_run:
            await db.rollback()
            print(f"[dry-run] 将迁移 {len(migrated)} 个岗位（已回滚）")
        else:
            await db.commit()
            print(f"已迁移 {len(migrated)} 个岗位")

    if migrated:
        print("\n迁移明细:")
        for name, key in migrated:
            print(f"  + {name}  ({key})")
    if skipped:
        print("\n跳过明细:")
        for name, reason in skipped:
            print(f"  - {name}: {reason}")

    # ---- 3. 岗位-技能映射（全量刷新；按岗位名对齐已存在的 V2 岗位，兼容 key 截断与重跑） ----
    async with sf() as db:
        v2_post_keys = {
            name: key for key, name, persona in (
                await db.execute(select(Post.key, Post.name, Post.default_persona))
            ).all()
            if is_virtual_persona_v2(persona)
        }
        valid_skill_ids = {s["skill_ref_id"] for s in skills}
        await db.execute(delete(EmployeePostSkill))
        mapping_count = 0
        for tpl in templates:
            key = v2_post_keys.get(tpl["name"])
            if not key:
                continue
            seen: set[int] = set()
            for item in tpl["default_skills"]:
                if not isinstance(item, dict):
                    continue
                ref = item.get("ref")
                if not isinstance(ref, int) or ref not in valid_skill_ids or ref in seen:
                    continue
                seen.add(ref)
                db.add(EmployeePostSkill(
                    post_key=key, skill_ref_id=ref,
                    enabled=bool(item.get("enabled", True)),
                ))
                mapping_count += 1
        if args.dry_run:
            await db.rollback()
            print(f"[dry-run] 岗位-技能映射 {mapping_count} 条（已回滚）")
        else:
            await db.commit()
            print(f"岗位-技能映射已刷新: {mapping_count} 条（覆盖 {sum(1 for t in templates if t['name'] in v2_post_keys)} 个 V2 岗位）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
