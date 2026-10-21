"""
V2 三件套生成器 — LLM 两步法（技能库强化）

第一步：把全部职能（taxonomy 快照，约 49 个）+ OpenVort 自有技能列表交给 LLM，
        挑出与岗位匹配的职能和建议订阅的 OpenVort 技能；
第二步：把匹配职能的定位/类目 + 挂载的外部技能素材格式化注入生成 prompt，
        生成 IDENTITY / PERSONA / BIBLE 三段 Markdown（===IDENTITY=== 等定界行切分）。

不做本地模板回退：LLM 主模型链自带 failover，仍失败则抛异常由调用方报错。
"""

import json
import re

from openvort.core.engine.llm import LLMClient
from openvort.utils.logging import get_logger

log = get_logger("core.employee_generator")

FIELDS = ("identity", "persona", "bible")
FIELD_LABELS = {
    "identity": "IDENTITY（职责与边界）",
    "persona": "PERSONA（工作风格）",
    "bible": "BIBLE（工作手册）",
}

_STEP1_SYSTEM = "你是岗位职能匹配助手。只输出 JSON，不输出任何其他文字。"

_STEP1_PROMPT = """下面是一个 AI 数字员工岗位，请从职能库里挑出与它匹配的职能，并从可订阅技能里挑出适合该岗位订阅的技能。

岗位名称：{name}
职责描述：{desc}

职能库（function_id: 名称｜定位｜领域）：
{functions}

可订阅技能（id: 名称：简介）：
{skills}

要求：
- 职能最多挑 5 个，只挑真正相关的；没有合适的就返回空数组
- 可订阅技能最多挑 8 个；没有合适的就返回空数组
- 只输出 JSON：{{"functions": ["F01", ...], "openvort_skills": ["<技能id>", ...]}}"""

_STEP2_SYSTEM = "你是一名 AI 数字员工的角色定义专家。只按要求的格式输出，不要输出任何解释。"

_STEP2_PROMPT_HEAD = """你要为一名 AI 数字员工生成三份角色源文件（Markdown）。

角色名称：{name}
职责描述：{desc}
"""

_STEP2_PROMPT_TAIL = """
要求：内容与职责描述强相关、具体可执行；参考素材与岗位不符时不要硬套；
不要输出任何解释，只输出以下三段，每段以定界行开始与结束
（===IDENTITY=== / ===PERSONA=== / ===BIBLE===，定界行独占一行、原样输出，不要用代码块包裹）；
每段内部直接以规定的章节标题开始，不要另加总标题。
IDENTITY 为职责与边界（核心定位/核心职责 3~6 条/职责边界/降级策略）；
PERSONA 为工作风格（工作风格/沟通方式/处理不确定性/反模式）；
BIBLE 为工作手册（通用工作流程 4~6 步/需要用户确认的动作/交付标准）。"""

_STEP2_REGENERATE_TAIL = """
请基于上述信息，只重新生成「{field_label}」这一份文件，输出为一段 Markdown。
不要输出任何解释、不要输出定界行；直接以章节标题开始。
{field_spec}"""

_FIELD_SPECS = {
    "identity": "IDENTITY 为职责与边界（核心定位/核心职责 3~6 条/职责边界/降级策略）。",
    "persona": "PERSONA 为工作风格（工作风格/沟通方式/处理不确定性/反模式）。",
    "bible": "BIBLE 为工作手册（通用工作流程 4~6 步/需要用户确认的动作/交付标准）。",
}

# 定界行：===IDENTITY===（容忍空白 === IDENTITY ===）或标题行 # IDENTITY（可带尾注）
_MARKER_RE = re.compile(
    r"^\s*(?:={2,}\s*(IDENTITY|PERSONA|BIBLE)\s*={2,}"
    r"|#{1,3}\s+(IDENTITY|PERSONA|BIBLE)\b.*)$", re.M)


def _extract_text(result) -> str:
    text = ""
    if hasattr(result, "content"):
        for block in result.content:
            if hasattr(block, "text"):
                text += block.text
    return text.strip()


def _parse_json_object(text: str) -> dict:
    """从 LLM 输出中提取第一个 JSON 对象（容忍代码围栏与前后废话）。"""
    t = re.sub(r"(?m)^```[\w-]*\s*$", "", text.strip()).strip()
    start = t.find("{")
    if start < 0:
        raise ValueError("LLM 输出中没有 JSON 对象")
    depth = 0
    for i in range(start, len(t)):
        if t[i] == "{":
            depth += 1
        elif t[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(t[start:i + 1])
    raise ValueError("LLM 输出的 JSON 不完整")


def split_sections(text: str) -> dict[str, str]:
    """从 LLM 原始输出切出三段 Markdown；缺段/空段抛 ValueError。

    容错：剥代码围栏；同名定界行第二次出现视为止界；输出截断（有起界无止界）
    时取到下一段起界或结尾；完全无定界行时按 # IDENTITY 标题切分。
    """
    t = re.sub(r"(?m)^```[\w-]*\s*$", "", text.strip()).strip()
    marks = [(m.start(), m.end(), (m.group(1) or m.group(2)).upper())
             for m in _MARKER_RE.finditer(t)]
    sections: dict[str, str] = {}
    for i, (start, end, name) in enumerate(marks):
        if name.lower() in sections:
            continue  # 止界定界行不再开新段
        nxt = marks[i + 1][0] if i + 1 < len(marks) else len(t)
        content = t[end:nxt].strip()
        if content:
            sections[name.lower()] = content
    missing = [f for f in FIELDS if f not in sections]
    if missing:
        raise ValueError(f"LLM 输出缺少 {'/'.join(m.upper() for m in missing)} 段")
    return sections


def format_kb_section(matched_functions: list[dict], mapped_skills: list[dict],
                      openvort_skills: list[dict]) -> str:
    """匹配职能 + 挂载技能 → 生成 prompt 的参考素材段。"""
    lines: list[str] = []
    if matched_functions:
        lines.append("## 相似职能（可作定位与职责素材）")
        for fn in matched_functions[:5]:
            detail = fn.get("positioning") or ""
            categories = fn.get("categories") or []
            if categories:
                cats = "、".join(c.get("name", "") for c in categories[:4])
                detail = f"{detail}；类目：{cats}" if detail else f"类目：{cats}"
            lines.append(f"- {fn.get('name', '')}：{detail}".rstrip("："))
    if mapped_skills:
        lines.append("## 可挂载技能（名称：简介）")
        for s in mapped_skills[:12]:
            d = (s.get("summary_zh") or s.get("description") or "").strip()[:80]
            lines.append(f"- {s.get('name', '')}：{d}".rstrip("："))
    if openvort_skills:
        lines.append("## 平台内可订阅技能（生成 BIBLE 时可引用其能力）")
        for s in openvort_skills[:8]:
            lines.append(f"- {s.get('name', '')}：{(s.get('description') or '')[:80]}".rstrip("："))
    if not lines:
        return ""
    lines.append("")
    lines.append("素材使用规则：参考素材与岗位不符时不要硬套；平台内可订阅技能只是能力参考，不代表该员工已具备。")
    return "\n# 参考素材（岗位知识库检索结果）\n" + "\n".join(lines) + "\n"


async def match_functions(llm: LLMClient, name: str, desc: str,
                          functions: list[dict], skills: list[dict]) -> dict:
    """第一步：LLM 挑选匹配职能 + 建议订阅的 OpenVort 技能。返回 {"functions": [...], "openvort_skills": [...]}"""
    fn_lines = "\n".join(
        f"- {f['function_id']}: {f['name']}｜{f.get('positioning', '')}｜{f.get('domain', '')}"
        for f in functions
    )
    skill_lines = "\n".join(
        f"- {s['id']}: {s['name']}：{(s.get('description') or '')[:60]}"
        for s in skills
    )
    prompt = _STEP1_PROMPT.format(
        name=name, desc=desc or "（无）",
        functions=fn_lines or "（空）", skills=skill_lines or "（空）",
    )
    result = await llm.create(
        system=_STEP1_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    data = _parse_json_object(_extract_text(result))
    if not isinstance(data, dict):
        raise ValueError("LLM 职能匹配输出不是 JSON 对象")
    fn_ids = [x for x in (data.get("functions") or []) if isinstance(x, str)]
    skill_ids = [x for x in (data.get("openvort_skills") or []) if isinstance(x, str)]
    valid_fn = {f["function_id"] for f in functions}
    valid_skill = {s["id"] for s in skills}
    return {
        "functions": [x for x in fn_ids if x in valid_fn][:5],
        "openvort_skills": [x for x in skill_ids if x in valid_skill][:8],
    }


async def generate_persona(
    llm: LLMClient, name: str, desc: str,
    kb_section: str = "",
    regenerate_field: str | None = None,
    current: dict[str, str] | None = None,
) -> dict[str, str]:
    """第二步：生成三件套（或 regenerate_field 指定的单份）。缺段抛 ValueError。"""
    if regenerate_field and regenerate_field not in FIELDS:
        raise ValueError(f"不支持的重生成字段: {regenerate_field}")

    if regenerate_field:
        others = "\n\n".join(
            f"### 现有 {FIELD_LABELS[f]}\n{(current or {}).get(f, '（无）')}"
            for f in FIELDS if f != regenerate_field
        )
        prompt = (
            _STEP2_PROMPT_HEAD.format(name=name, desc=desc or "（无）")
            + kb_section
            + (f"\n{others}\n" if others else "")
            + _STEP2_REGENERATE_TAIL.format(
                field_label=FIELD_LABELS[regenerate_field],
                field_spec=_FIELD_SPECS[regenerate_field],
            )
        )
        result = await llm.create(system=_STEP2_SYSTEM, messages=[{"role": "user", "content": prompt}])
        text = _extract_text(result)
        text = re.sub(r"(?m)^```[\w-]*\s*$", "", text).strip()
        if not text:
            raise ValueError("LLM 重生成输出为空")
        return {regenerate_field: text}

    prompt = (
        _STEP2_PROMPT_HEAD.format(name=name, desc=desc or "（无）")
        + kb_section
        + _STEP2_PROMPT_TAIL
    )
    result = await llm.create(system=_STEP2_SYSTEM, messages=[{"role": "user", "content": prompt}])
    return split_sections(_extract_text(result))
