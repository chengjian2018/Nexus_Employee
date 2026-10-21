"""
AI 员工 V2 三件套（identity / persona / bible）

V2 记录把三件套以 JSON 存在原有的 `Post.default_persona` 与
`Member.virtual_system_prompt` 字段里，不改表结构：

    {"version": 2, "identity": "...", "persona": "...", "bible": "..."}

值为 Markdown，三个字段的语义沿用 skill_kb：
- identity：职责与边界（核心定位 / 核心职责 / 职责边界 / 降级策略）
- persona：工作风格（工作风格 / 沟通方式 / 处理不确定性 / 反模式）
- bible：工作手册（通用工作流程 / 需要用户确认的动作 / 交付标准）

AI 团队（V3）沿用同一字段，扩展为：

    {"version": 3, "identity": "...", "persona": "...", "bible": "...",
     "leader_id": "...", "roster": [{"member_id": "...", "name": "...",
     "position": "...", "role_note": "...", "joined_at": "..."}],
     "handoff_rules": "..."}

roster 是团队花名册的唯一权威；条目里的 name/position 是渲染用的展示快照，
每次花名册写入时刷新（成员改名不自动回填）。handoff_rules 为团队协作与
任务移交规则基准，建团时从全局模板快照。

解析失败、版本不符或缺键，一律视为老的纯文本人设，由调用方原样拼接。
"""

import json

PERSONA_VERSION = 2
PERSONA_KEYS = ("identity", "persona", "bible")

TEAM_PERSONA_VERSION = 3


def parse_virtual_persona(text: str | None) -> dict[str, str] | None:
    """解析 V2 三件套；非 V2 JSON（老纯文本、坏 JSON、缺键）返回 None。"""
    if not text:
        return None
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    try:
        data = json.loads(stripped)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("version") != PERSONA_VERSION:
        return None
    parts: dict[str, str] = {}
    for key in PERSONA_KEYS:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
        parts[key] = value.strip()
    return parts


def is_virtual_persona_v2(text: str | None) -> bool:
    return parse_virtual_persona(text) is not None


def build_virtual_persona(identity: str, persona: str, bible: str) -> str:
    """三件套 → 存库用的 V2 JSON 字符串。"""
    return json.dumps(
        {
            "version": PERSONA_VERSION,
            "identity": identity.strip(),
            "persona": persona.strip(),
            "bible": bible.strip(),
        },
        ensure_ascii=False,
    )


def render_persona_sections(parts: dict[str, str]) -> str:
    """三件套 → system prompt 里的三节 Markdown（IDENTITY → PERSONA → BIBLE）。"""
    return (
        f"## 角色身份（IDENTITY）\n\n{parts['identity']}\n\n"
        f"## 工作风格（PERSONA）\n\n{parts['persona']}\n\n"
        f"## 工作手册（BIBLE）\n\n{parts['bible']}"
    )


# ---- V3：AI 团队 ----

# 团队护栏默认值（团队 JSON config 键可逐项覆盖；缺键/坏值一律回退到这里）
TEAM_CONFIG_DEFAULTS = {
    "max_runs": 15,             # 单条用户消息触发的成员 run 总数上限
    "max_rework": 3,            # 同一任务返工次数上限
    "run_timeout": 15,          # 单次 run 超时（分钟）
    "max_rounds_member": 25,    # 成员 run 的 agentic loop 轮次上限
    "max_rounds_leader": 15,    # leader run 的 agentic loop 轮次上限
}


def normalize_team_config(raw) -> dict:
    """合并团队 JSON config 键与默认值；只接受合法正整数。"""
    config = dict(TEAM_CONFIG_DEFAULTS)
    if isinstance(raw, dict):
        for key in config:
            try:
                value = int(raw.get(key, config[key]))
            except (TypeError, ValueError):
                continue
            if value > 0:
                config[key] = value
    return config


def parse_team_persona(text: str | None) -> dict | None:
    """解析 V3 团队人设；非 V3 JSON（含 V2、老纯文本、坏 JSON）返回 None。

    返回键：identity / persona / bible（str）、leader_id（str）、
    roster（list[dict]，member_id 必有）、handoff_rules（str，可为空）、
    sop（dict，可为空：{"summary": str, "full_text": str}）、
    config（dict，经 normalize_team_config 合并默认值）。
    """
    if not text:
        return None
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    try:
        data = json.loads(stripped)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("version") != TEAM_PERSONA_VERSION:
        return None
    parts: dict = {}
    for key in PERSONA_KEYS:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
        parts[key] = value.strip()
    leader_id = data.get("leader_id")
    if not isinstance(leader_id, str) or not leader_id:
        return None
    roster = data.get("roster")
    if not isinstance(roster, list) or not roster:
        return None
    entries = []
    for item in roster:
        if not isinstance(item, dict) or not item.get("member_id"):
            return None
        entries.append({
            "member_id": item["member_id"],
            "name": item.get("name", ""),
            "position": item.get("position", ""),
            "role_note": item.get("role_note", ""),
            "joined_at": item.get("joined_at", ""),
        })
    parts["leader_id"] = leader_id
    parts["roster"] = entries
    handoff = data.get("handoff_rules")
    parts["handoff_rules"] = handoff if isinstance(handoff, str) else ""
    sop = data.get("sop")
    parts["sop"] = sop if isinstance(sop, dict) else {}
    parts["config"] = normalize_team_config(data.get("config"))
    return parts


def is_team_persona(text: str | None) -> bool:
    return parse_team_persona(text) is not None


def build_team_persona(identity: str, persona: str, bible: str,
                       leader_id: str, roster: list[dict],
                       handoff_rules: str,
                       sop: dict | None = None,
                       config: dict | None = None) -> str:
    """三件套 + 花名册 → 存库用的 V3 JSON 字符串。

    sop/config 为可选键（编排链路使用）；传 None 时不写入，老团队行保持原样。
    """
    entries = []
    for item in roster:
        entry = {"member_id": item["member_id"]}
        # name/position 是展示快照，仅展示层使用，权威数据只有 member_id/role_note/joined_at
        for key in ("name", "position", "role_note", "joined_at"):
            if item.get(key):
                entry[key] = item[key]
        entries.append(entry)
    data = {
        "version": TEAM_PERSONA_VERSION,
        "identity": identity.strip(),
        "persona": persona.strip(),
        "bible": bible.strip(),
        "leader_id": leader_id,
        "roster": entries,
        "handoff_rules": (handoff_rules or "").strip(),
    }
    if isinstance(sop, dict) and (sop.get("summary") or sop.get("full_text")):
        data["sop"] = {
            "summary": (sop.get("summary") or "").strip(),
            "full_text": (sop.get("full_text") or "").strip(),
        }
    if isinstance(config, dict) and config:
        data["config"] = normalize_team_config(config)
    return json.dumps(data, ensure_ascii=False)


def render_team_persona_sections(team: dict) -> str:
    """V3 团队人设 → prompt 段：三件套 + 花名册 + 移交规则（纯文本，无需查库）。"""
    parts = [render_persona_sections(team)]
    roster_lines = []
    for item in team["roster"]:
        name = item.get("name") or item.get("member_id")
        title = item.get("position") or ""
        marker = "（负责人）" if item["member_id"] == team["leader_id"] else ""
        note = f"：{item['role_note']}" if item.get("role_note") else ""
        roster_lines.append(f"- {name}{marker}{'，' + title if title else ''}{note}")
    parts.append("## 团队花名册（按此分工与移交任务）\n\n" + "\n".join(roster_lines))
    if team.get("handoff_rules"):
        parts.append(f"## 团队协作与任务移交规则\n\n{team['handoff_rules']}")
    return "\n\n".join(parts)


def build_persona_prompt_section(text: str) -> str:
    """system prompt 的 `# AI 员工人设` 段：V2/V3 渲染为结构化小节，老纯文本原样带入。"""
    team = parse_team_persona(text)
    if team:
        body = render_team_persona_sections(team)
        return f"\n\n# AI 员工人设\n\n{body}"
    parts = parse_virtual_persona(text)
    body = render_persona_sections(parts) if parts else text
    return f"\n\n# AI 员工人设\n\n{body}"
