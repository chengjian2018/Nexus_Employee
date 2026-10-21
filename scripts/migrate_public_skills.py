"""
Migrate skill_kb skill corpus into OpenVort runtime skills (scope=public).

Two-phase design — the OpenVort DB is only reachable inside the docker
network, while the skill corpus only exists on the host:

  1. plan  (run on host, stdlib only):
     python scripts/migrate_public_skills.py plan [--kb-db PATH] [--corpus PATH] [--out plan.json]

     Reads kb.db (safety='pass' AND is_canonical=1), parses each SKILL.md
     (frontmatter name/description/requires + markdown body, same rules as
     skill/loader.py), resolves name collisions, writes a self-contained
     plan JSON.

  2. apply (run inside the openvort container):
     python scripts/migrate_public_skills.py apply plan.json [--enable]

     Upserts plan entries into the skills table: scope='public',
     created_by='skill_kb'. Idempotent:
     - same-name public skill created by an earlier run -> update
       description/content/tags/requires (enabled kept as user left it)
     - same-name builtin or user-created public skill -> skipped + reported
     - new rows default to enabled=False: every enabled public skill is
       injected into ALL members' prompts, so never bulk-enable the corpus;
       enable per-skill via the web UI or the skill_use tool instead.

Only the SKILL.md body is migrated. Auxiliary scripts/refs files stay
behind (DB skills have no file directory), so skills whose instructions
depend on bundled scripts degrade to reference text.
"""

import argparse
import json
import re
import sqlite3
import sys
import uuid
from collections import defaultdict
from pathlib import Path

DEFAULT_KB_DB = "/Users/chengjian/py_projects/skill_kb/skill_knowledge/kb/kb.db"
DEFAULT_CORPUS = "/Users/chengjian/py_projects/skill_kb/skill_knowledge"
DEFAULT_PLAN = "/tmp/openvort_public_skills_plan.json"

SOURCE_MARK = "skill_kb"  # skills.created_by marker for idempotent re-runs
NAME_MAX = 100
SKILL_TYPE_DEFAULT = "workflow"

# Mirrors openvort/skill/loader.py (kept in sync so migrated content matches
# what the loader would parse from the same file).
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_yaml_list(val: str) -> list[str]:
    val = val.strip()
    if val.startswith("[") and val.endswith("]"):
        return [s.strip().strip("\"'") for s in val[1:-1].split(",") if s.strip()]
    return [val] if val else []


def _parse_skill_md(raw: str, fallback_name: str) -> dict:
    """Parse SKILL.md -> name/description/content/requires (loader-compatible)."""
    name = fallback_name
    description = ""
    enabled = True
    content = raw
    requires: dict[str, list[str]] = {}

    match = _FRONTMATTER_RE.match(raw)
    if match:
        frontmatter = match.group(1)
        content = raw[match.end():]
        lines = frontmatter.splitlines()
        i = 0
        while i < len(lines):
            stripped = lines[i].strip()
            if stripped.startswith("name:"):
                name = stripped.split(":", 1)[1].strip().strip("\"'") or name
            elif stripped.startswith("description:"):
                description = stripped.split(":", 1)[1].strip().strip("\"'")
            elif stripped.startswith("enabled:"):
                enabled = stripped.split(":", 1)[1].strip().lower() not in ("false", "no", "0", "off")
            elif stripped.startswith("requires:"):
                i += 1
                while i < len(lines) and lines[i] and lines[i][0] in (" ", "\t"):
                    sub = lines[i].strip()
                    if ":" in sub:
                        key, val = sub.split(":", 1)
                        requires[key.strip()] = _parse_yaml_list(val)
                    i += 1
                continue
            i += 1

    return {
        "name": name,
        "description": description,
        "content": content.strip(),
        "enabled": enabled,
        "requires": requires,
    }


def _load_kb_rows(kb_db: Path) -> list[dict]:
    conn = sqlite3.connect(f"file:{kb_db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, rel_path, root, name, COALESCE(dir_name,''), "
            "COALESCE(description,''), COALESCE(summary_zh,''), n_scripts, "
            "COALESCE(total_score,0) "
            "FROM skills WHERE safety='pass' AND is_canonical=1"
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "kb_id": r[0], "rel_path": r[1], "root": r[2] or "", "name": r[3] or "",
            "dir_name": r[4] or (r[1].split("/")[0] if r[1] else ""), "description": r[5],
            "summary_zh": r[6], "n_scripts": r[7] or 0, "total_score": r[8] or 0.0,
        }
        for r in rows
    ]


def cmd_plan(args) -> int:
    kb_db, corpus, out = Path(args.kb_db), Path(args.corpus), Path(args.out)
    if not kb_db.exists() or not corpus.is_dir():
        print(f"[错误] 数据源不存在: {kb_db} / {corpus}")
        return 1

    root_subdir = {"en": "skills", "zh": "zh-skills"}
    rows = _load_kb_rows(kb_db)
    print(f"kb.db 合格 skill: {len(rows)}")

    entries, missing, empty_content = [], [], 0
    for row in rows:
        sub = root_subdir.get(row["root"])
        if not sub:
            missing.append((row["name"], f"未知 root '{row['root']}'"))
            continue
        skill_file = corpus / sub / row["rel_path"]
        if not skill_file.is_file():
            missing.append((row["name"], f"文件缺失 {sub}/{row['rel_path']}"))
            continue
        try:
            raw = skill_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            missing.append((row["name"], f"读取失败: {e}"))
            continue

        parsed = _parse_skill_md(raw, row["name"] or row["dir_name"])
        if not parsed["content"]:
            empty_content += 1
            continue
        description = parsed["description"] or row["description"] or row["summary_zh"]
        entries.append({
            "kb_id": row["kb_id"],
            "dir_name": row["dir_name"],
            "root": row["root"],
            "name": parsed["name"].replace("\x00", "")[:NAME_MAX],
            "description": description.replace("\x00", ""),
            "content": parsed["content"].replace("\x00", ""),
            "requires": parsed["requires"],
            "enabled_fm": parsed["enabled"],
            "n_scripts": row["n_scripts"],
            "total_score": row["total_score"],
        })

    # 名称消歧：frontmatter name 可能跨 en/zh 重复（如 ppt-generator）。
    # 组内 total_score 高者保留原名，其余追加 dir_name 后缀（dir_name 唯一）。
    by_lower: dict[str, list[int]] = defaultdict(list)
    for idx, e in enumerate(entries):
        by_lower[e["name"].lower()].append(idx)
    renamed = 0
    for _, idxs in by_lower.items():
        if len(idxs) < 2:
            continue
        idxs.sort(key=lambda i: (-entries[i]["total_score"], entries[i]["kb_id"]))
        for idx in idxs[1:]:
            e = entries[idx]
            suffix = e["root"] if e["dir_name"].lower() == e["name"].lower() else e["dir_name"]
            base = e["name"][: NAME_MAX - len(suffix) - 1]
            e["name"] = f"{base}-{suffix}"[:NAME_MAX]
            renamed += 1

    plan = {
        "source": str(kb_db),
        "corpus": str(corpus),
        "count": len(entries),
        "skills": entries,
    }
    out.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    with_scripts = sum(1 for e in entries if e["n_scripts"] > 0)
    fm_disabled = sum(1 for e in entries if not e["enabled_fm"])
    print(f"解析成功 {len(entries)} 个（en {sum(1 for e in entries if e['root']=='en')} / "
          f"zh {sum(1 for e in entries if e['root']=='zh')}）")
    print(f"重名消歧改名: {renamed}；frontmatter 禁用: {fm_disabled}；空正文跳过: {empty_content}")
    print(f"带辅助脚本(scripts/) 的 skill: {with_scripts} 个 — 仅迁 SKILL.md 正文，脚本引用将失效")
    if missing:
        print(f"缺失/不可读 {len(missing)} 个:")
        for name, why in missing[:10]:
            print(f"  - {name}: {why}")
    print(f"计划已写入: {out}")
    return 0


def cmd_apply(args) -> int:
    import asyncio

    return asyncio.run(_apply_async(args))


async def _apply_async(args) -> int:
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    entries = plan["skills"]
    print(f"计划导入 {len(entries)} 个公共 skill (source={plan['source']})")

    from sqlalchemy import select

    from openvort.config.settings import get_settings
    from openvort.db.engine import init_db
    from openvort.db.models import Skill

    settings = get_settings()
    print(f"目标库: {settings.database_url}")
    await init_db(settings.database_url)

    from openvort.db.engine import get_session_factory
    sf = get_session_factory()

    inserted, updated, skipped_builtin, skipped_user = [], [], [], []
    async with sf() as db:
        result = await db.execute(
            select(Skill).where(Skill.scope.in_(["builtin", "public"]))
        )
        existing: dict[str, Skill] = {}
        for row in result.scalars().all():
            existing.setdefault(row.name.lower(), row)

        for e in entries:
            tags = json.dumps([SOURCE_MARK, e["root"]], ensure_ascii=False)
            requires_json = json.dumps(e["requires"], ensure_ascii=False) if e["requires"] else ""
            row = existing.get(e["name"].lower())

            if row is not None:
                if row.scope == "builtin":
                    skipped_builtin.append((e["name"], "与内置 skill 重名"))
                    continue
                if row.created_by != SOURCE_MARK:
                    skipped_user.append((e["name"], "同名公共 skill 已存在（非本次迁移创建）"))
                    continue
                # 幂等重跑：刷新素材字段，enabled 保留用户在 UI 的开关
                row.description = e["description"]
                row.content = e["content"]
                row.tags = tags
                row.requires_json = requires_json
                updated.append(e["name"])
                continue

            row = Skill(
                id=uuid.uuid4().hex,
                name=e["name"],
                description=e["description"],
                content=e["content"],
                scope="public",
                skill_type=SKILL_TYPE_DEFAULT,
                tags=tags,
                requires_json=requires_json,
                enabled=bool(args.enable),
                created_by=SOURCE_MARK,
            )
            db.add(row)
            existing[e["name"].lower()] = row
            inserted.append(e["name"])

        await db.commit()

    print(f"新增 {len(inserted)}（enabled={bool(args.enable)}），更新 {len(updated)}，"
          f"跳过 builtin 重名 {len(skipped_builtin)}，跳过用户同名 {len(skipped_user)}")
    for label, items in (("builtin 重名", skipped_builtin), ("用户同名", skipped_user)):
        if items:
            print(f"\n{label} 明细:")
            for name, why in items:
                print(f"  - {name}: {why}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate skill_kb corpus into OpenVort public skills")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="解析语料生成计划 JSON（宿主机，无第三方依赖）")
    p_plan.add_argument("--kb-db", default=DEFAULT_KB_DB)
    p_plan.add_argument("--corpus", default=DEFAULT_CORPUS)
    p_plan.add_argument("--out", default=DEFAULT_PLAN)
    p_plan.set_defaults(func=cmd_plan)

    p_apply = sub.add_parser("apply", help="把计划 JSON 导入 OpenVort（需 openvort 环境/容器内）")
    p_apply.add_argument("plan", help="plan 阶段输出的 JSON 路径")
    p_apply.add_argument("--enable", action="store_true",
                         help="新导入的 skill 置为 enabled（默认禁用：启用的公共 skill 会注入所有成员 prompt）")
    p_apply.set_defaults(func=cmd_apply)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
