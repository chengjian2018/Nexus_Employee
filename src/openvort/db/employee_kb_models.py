"""
AI 员工 V2 生成素材库 — skill_kb 静态快照

由 scripts/migrate_skill_kb.py 一次性导入（可重复执行，快照表全量刷新）：
- EmployeeFunction：taxonomy 49 职能（生成时全量交给 LLM 挑选匹配）
- EmployeeSkillRef：外部技能参考素材（仅名称/描述/摘要）
- EmployeeFunctionSkill：职能 ↔ 外部技能映射

仅服务于 V2 三件套的"技能库强化"生成，与运行时技能体系
（skills / member_skills / role_skills）无关。
"""

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from openvort.db.engine import Base


class EmployeeFunction(Base):
    """职能库 — 生成时供 LLM 匹配的岗位职能 taxonomy 快照"""

    __tablename__ = "employee_functions"

    function_id: Mapped[str] = mapped_column(String(16), primary_key=True)  # F01
    name: Mapped[str] = mapped_column(String(64))
    name_en: Mapped[str] = mapped_column(String(128), default="")
    positioning: Mapped[str] = mapped_column(Text, default="")  # 定位句
    domain: Mapped[str] = mapped_column(String(64), default="", index=True)
    categories_json: Mapped[str] = mapped_column(Text, default="[]")  # [{"id","name","description"}]


class EmployeeSkillRef(Base):
    """外部技能参考素材 — skill_kb 技能库快照（仅名称/描述/摘要）"""

    __tablename__ = "employee_skill_refs"

    skill_ref_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)  # 保留 skill_kb 原 id，便于映射
    name: Mapped[str] = mapped_column(String(200), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    summary_zh: Mapped[str] = mapped_column(Text, default="")


class EmployeeFunctionSkill(Base):
    """职能 ↔ 外部技能映射"""

    __tablename__ = "employee_function_skills"
    __table_args__ = (
        UniqueConstraint("function_id", "skill_ref_id", name="uq_employee_function_skill"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    function_id: Mapped[str] = mapped_column(String(16), ForeignKey("employee_functions.function_id"), index=True)
    skill_ref_id: Mapped[int] = mapped_column(Integer, ForeignKey("employee_skill_refs.skill_ref_id"), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)


class EmployeePostSkill(Base):
    """V2 岗位 ↔ 外部技能映射 — skill_kb waker_template.default_skills 快照"""

    __tablename__ = "employee_post_skills"
    __table_args__ = (
        UniqueConstraint("post_key", "skill_ref_id", name="uq_employee_post_skill"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_key: Mapped[str] = mapped_column(String(64), index=True)  # 对应 Post.key（V2 岗位）
    skill_ref_id: Mapped[int] = mapped_column(Integer, ForeignKey("employee_skill_refs.skill_ref_id"), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
