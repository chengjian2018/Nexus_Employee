"""
数据库模型

v0.1 基础模型：消息记录、调度任务。
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from openvort.db.engine import Base


def _uuid() -> str:
    return uuid.uuid4().hex


# SQLite 只对 INTEGER PRIMARY KEY 自增；PostgreSQL 保持 BIGINT（内存 SQLite 测试用）
_BigIntPK = BigInteger().with_variant(Integer, "sqlite")


class MessageLog(Base):
    """消息日志"""

    __tablename__ = "message_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    user_name: Mapped[str] = mapped_column(String(64), default="")
    direction: Mapped[str] = mapped_column(String(8))  # "in" / "out"
    msg_type: Mapped[str] = mapped_column(String(16), default="text")
    content: Mapped[str] = mapped_column(Text, default="")
    response: Mapped[str] = mapped_column(Text, default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ChannelConfig(Base):
    """通道配置持久化"""

    __tablename__ = "channel_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    config_data: Mapped[str] = mapped_column(Text, default="{}")  # JSON
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class PluginConfig(Base):
    """插件配置持久化"""

    __tablename__ = "plugin_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plugin_name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    config_data: Mapped[str] = mapped_column(Text, default="{}")  # JSON
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class PluginMemberBinding(Base):
    """插件 ↔ AI 员工宿主绑定（按员工启用插件的"门禁"）

    与 MemberPluginSetting（个人凭证）分离：这里只回答"以某员工身份对话时，
    某插件是否可见"。mode="allow" 给 visibility=members 的插件授权；
    mode="deny" 从默认可见集里剔除。source="auto" 的行由团队等功能自动维护。
    """

    __tablename__ = "plugin_member_bindings"
    __table_args__ = (
        UniqueConstraint("plugin_name", "member_id", name="uq_plugin_member_binding"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plugin_name: Mapped[str] = mapped_column(String(64), index=True)
    member_id: Mapped[str] = mapped_column(String(32), index=True)
    mode: Mapped[str] = mapped_column(String(8), default="allow")  # allow / deny
    source: Mapped[str] = mapped_column(String(8), default="admin")  # admin / auto
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ChatSession(Base):
    """聊天会话持久化（完整 Claude messages 列表）"""

    __tablename__ = "chat_sessions"
    __table_args__ = (
        UniqueConstraint("channel", "user_id", "session_id", name="uq_chat_session_channel_user_session"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True, default="default")
    title: Mapped[str] = mapped_column(String(200), default="新对话")
    messages: Mapped[str] = mapped_column(Text, default="[]")  # JSON
    archived_messages: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)  # JSON — preserved after context reset
    context_reset_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    target_type: Mapped[str] = mapped_column(String(16), default="ai", index=True)  # "ai" | "member"
    target_id: Mapped[str] = mapped_column(String(32), default="")  # target member id when target_type="member"
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    unread_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class SystemConfig(Base):
    """系统配置（key-value 存储，Web 面板写入，优先级高于 .env）"""

    __tablename__ = "system_configs"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ScheduleJob(Base):
    """调度任务持久化"""

    __tablename__ = "schedule_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")

    # 归属: owner_id = task owner (AI employee when delegated, user for personal reminders)
    owner_id: Mapped[str] = mapped_column(String(32), ForeignKey("members.id"), index=True)
    # creator_id = who delegated/created this task (always the real human user)
    creator_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    scope: Mapped[str] = mapped_column(String(16), default="personal")  # personal | team

    # 调度
    schedule_type: Mapped[str] = mapped_column(String(16), default="cron")  # cron | interval | once
    schedule: Mapped[str] = mapped_column(String(64))  # cron 表达式 / 秒数 / ISO 时间
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")

    # 执行动作
    action_type: Mapped[str] = mapped_column(String(32), default="agent_chat")  # agent_chat
    action_config: Mapped[str] = mapped_column(Text, default="{}")  # JSON: {"prompt": "..."}

    # DEPRECATED: use owner_id instead; kept for backward compat during migration
    target_member_id: Mapped[str] = mapped_column(String(32), default="", nullable=True)

    visible: Mapped[bool] = mapped_column(Boolean, default=True)

    # 状态
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str] = mapped_column(String(16), default="pending")  # success | failed | pending
    last_result: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class Skill(Base):
    """技能知识条目 — 内置/公共/个人三级体系"""

    __tablename__ = "skills"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(100), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[str] = mapped_column(Text, default="")  # markdown
    scope: Mapped[str] = mapped_column(String(16), index=True)  # builtin / public / personal
    skill_type: Mapped[str] = mapped_column(String(16), default="workflow")  # role / workflow / knowledge / template / guideline (legacy, kept for compat)
    tags: Mapped[str] = mapped_column(Text, default="")  # JSON array of tag strings, e.g. '["工作流程","规范准则"]'
    owner_id: Mapped[str] = mapped_column(String(32), default="", index=True)  # personal → member.id
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str] = mapped_column(String(32), default="")
    requires_json: Mapped[str] = mapped_column(Text, default="")  # JSON: {"bins":["pandoc"],"env":["API_KEY"],"packages":["docx"],"plugins":["name"]}
    marketplace_slug: Mapped[str] = mapped_column(String(128), default="", index=True)
    marketplace_author: Mapped[str] = mapped_column(String(128), default="")
    marketplace_version: Mapped[str] = mapped_column(String(32), default="")
    marketplace_display_name: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class MemberSkill(Base):
    """成员订阅的公共 Skill"""

    __tablename__ = "member_skills"
    __table_args__ = (
        UniqueConstraint("member_id", "skill_id", name="uq_member_skill"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    member_id: Mapped[str] = mapped_column(String(32), ForeignKey("members.id"), index=True)
    skill_id: Mapped[str] = mapped_column(String(32), ForeignKey("skills.id"), index=True)
    source: Mapped[str] = mapped_column(String(32), default="personal")  # role:developer / personal / public
    custom_content: Mapped[str] = mapped_column(Text, default="")  # 自定义内容，覆盖 Skill.content
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PostSkill(Base):
    """岗位-技能映射表"""

    __tablename__ = "role_skills"  # 保持向后兼容，暂不重命名表

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    role: Mapped[str] = mapped_column(String(32), index=True)  # 岗位标识：developer/pm/qa/designer/assistant (数据库用 role)
    post: Mapped[str] = mapped_column(String(32), index=True, nullable=True)  # 冗余字段，保持兼容
    skill_id: Mapped[str] = mapped_column(String(32), ForeignKey("skills.id"), index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)  # 越小越优先


class Post(Base):
    """AI 员工岗位元数据表"""

    __tablename__ = "virtual_roles"  # 保持向后兼容，暂不重命名表

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # developer/pm/qa/designer/assistant
    name: Mapped[str] = mapped_column(String(64))  # 开发工程师
    description: Mapped[str] = mapped_column(String(256), default="")  # 一句话描述
    icon: Mapped[str] = mapped_column(String(32), default="")  # 前端图标 key，如 Code/ClipboardList
    default_persona: Mapped[str] = mapped_column(Text, default="")  # 默认人设
    default_auto_report: Mapped[bool] = mapped_column(Boolean, default=False)
    default_report_frequency: Mapped[str] = mapped_column(String(16), default="daily")  # daily/weekly
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


# 保留旧名称的别名，保持向后兼容
VirtualRole = Post
RoleSkill = PostSkill


class WorkAssignment(Base):
    """工作安排 - 记录 AI 员工被安排的工作任务"""

    __tablename__ = "work_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 任务基本信息
    title: Mapped[str] = mapped_column(String(256))  # 任务标题/一句话描述
    summary: Mapped[str] = mapped_column(Text, default="")  # 任务详细描述/AI 的理解
    plan: Mapped[str] = mapped_column(Text, default="")  # AI 员工"打算怎么做"的简短步骤

    # 归属与执行
    requested_by_member_id: Mapped[str] = mapped_column(String(32), ForeignKey("members.id"), index=True)  # 谁要求的
    assignee_member_id: Mapped[str] = mapped_column(String(32), ForeignKey("members.id"), index=True)  # 谁来执行（AI 员工）

    # 来源追踪
    source_type: Mapped[str] = mapped_column(String(16), default="manual")  # chat / code_task / schedule / manual
    source_id: Mapped[str] = mapped_column(String(64), default="")  # 来源ID（如 chat session id, code_task id 等）
    source_detail: Mapped[str] = mapped_column(Text, default="")  # 来源详情摘要

    # 关联的计划任务（可选，一个工作安排可以关联多个定时任务）
    related_schedule_id: Mapped[int] = mapped_column(Integer, ForeignKey("schedule_jobs.id"), nullable=True)

    # 状态
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending / in_progress / completed / ongoing
    priority: Mapped[str] = mapped_column(String(16), default="normal")  # low / normal / high / urgent

    # 时间戳
    due_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 截止日期
    last_action_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 最近一次行动/汇报时间
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class RemoteNode(Base):
    """远程工作节点 — AI 员工可绑定的远程执行环境（支持多种节点类型）"""

    __tablename__ = "remote_nodes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(64), index=True)
    node_type: Mapped[str] = mapped_column(String(32), default="docker")
    description: Mapped[str] = mapped_column(Text, default="")
    gateway_url: Mapped[str] = mapped_column(String(512))
    gateway_token: Mapped[str] = mapped_column(Text, default="")  # Fernet encrypted
    config: Mapped[str] = mapped_column(Text, default="{}")  # type-specific config JSON
    status: Mapped[str] = mapped_column(String(16), default="unknown")  # online / offline / unknown
    machine_info: Mapped[str] = mapped_column(Text, default="{}")  # JSON: {"os": "macOS", "hostname": "dev-a"}
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<RemoteNode {self.id[:8]} {self.name} ({self.node_type})>"


class GroupChat(Base):
    """群聊记录 — 持久化群聊元数据及项目关联"""

    __tablename__ = "group_chats"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    chat_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)  # platform chat ID
    platform: Mapped[str] = mapped_column(String(16), index=True)  # wecom / dingtalk / feishu
    name: Mapped[str] = mapped_column(String(200), default="")
    project_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # VortFlow project ID (no FK)
    bound_by: Mapped[str | None] = mapped_column(String(32), nullable=True)  # member_id who bound the project
    bound_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

class VoiceProvider(Base):
    """语音及向量服务厂商配置 - 支持 ASR（语音识别）、TTS（语音合成）、Embedding（向量化）"""

    __tablename__ = "voice_providers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(64))  # 厂商名称（如 "阿里云-TTS"）
    platform: Mapped[str] = mapped_column(String(32), index=True)  # aliyun/tencent/azure/openai/dashscope 等
    service_type: Mapped[str] = mapped_column(String(16), default="tts")  # tts | asr | embedding
    api_key: Mapped[str] = mapped_column(Text, default="")  # API 密钥（Fernet 加密存储）
    config: Mapped[str] = mapped_column(Text, default="{}")  # 厂商特定配置 JSON
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    owner_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("members.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ChatMessage(Base):
    """Chat message — persistent record of every user/assistant message for UI display, unread tracking, and search."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(_BigIntPK, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    owner_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    sender_type: Mapped[str] = mapped_column(String(16), nullable=False)  # user / assistant / system
    sender_id: Mapped[str] = mapped_column(String(32), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")  # tool calls, attachments, etc.
    source: Mapped[str] = mapped_column(String(16), default="chat")  # chat / schedule / webhook / proactive
    # broadcast：上公屏（团队会话里成员主动广播 / 用户消息）；private：仅本人与调试视图可见。
    # 普通会话全部为 broadcast，与旧数据语义一致。
    visibility: Mapped[str] = mapped_column(String(16), default="broadcast")
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Notification(Base):
    """Notification record — tracks delayed IM delivery and read status."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    recipient_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # schedule / ai_message / webhook / system
    source_id: Mapped[str] = mapped_column(String(64), default="")
    session_id: Mapped[str] = mapped_column(String(64), default="")
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending / sent / read / cancelled / expired
    data_json: Mapped[str] = mapped_column(Text, default="{}")
    im_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    im_channel: Mapped[str] = mapped_column(String(32), default="")
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AgentTask(Base):
    """Agent task — tracks a running/completed agent execution for async task management."""

    __tablename__ = "agent_tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    executor_id: Mapped[str] = mapped_column(String(32), default="")
    source: Mapped[str] = mapped_column(String(16), default="chat")  # chat / schedule / webhook
    source_id: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending / running / completed / failed / cancelled
    description: Mapped[str] = mapped_column(Text, default="")
    progress: Mapped[str] = mapped_column(Text, default="")
    result_summary: Mapped[str] = mapped_column(Text, default="")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ChannelBot(Base):
    """AI 员工的独立 IM Bot 凭证 — 每个 AI 员工可绑定一个或多个通道的 Bot"""

    __tablename__ = "channel_bots"
    __table_args__ = (
        UniqueConstraint("channel_type", "member_id", name="uq_channel_bot_channel_member"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    channel_type: Mapped[str] = mapped_column(String(32), index=True)  # wecom / dingtalk / feishu
    member_id: Mapped[str] = mapped_column(String(32), ForeignKey("members.id"), index=True)
    credentials: Mapped[str] = mapped_column(Text, default="{}")  # JSON, Fernet-encrypted secrets
    status: Mapped[str] = mapped_column(String(16), default="active")  # active / inactive
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_test_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ImInbox(Base):
    """IM inbound message idempotency table — cross-instance dedup"""

    __tablename__ = "im_inbox"
    __table_args__ = (
        UniqueConstraint("channel", "msg_id", name="uq_im_inbox_channel_msg_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    msg_id: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="claimed")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ImCursor(Base):
    """IM consume cursor — shared poll offset across instances"""

    __tablename__ = "im_cursors"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class MemberPluginSetting(Base):
    """Per-member plugin settings — stores personal config (e.g. API keys) per plugin per user"""

    __tablename__ = "member_plugin_settings"
    __table_args__ = (
        UniqueConstraint("member_id", "plugin_name", name="uq_member_plugin_setting"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    member_id: Mapped[str] = mapped_column(String(32), ForeignKey("members.id"), index=True)
    plugin_name: Mapped[str] = mapped_column(String(64), index=True)
    settings_data: Mapped[str] = mapped_column(Text, default="{}")  # JSON, secret fields Fernet-encrypted
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class TeamTask(Base):
    """AI 团队任务 — 一次指派（可含多名被指派者，按顺序依次执行）

    团队/用户维度（team_id + user_key）隔离：同一团队为不同用户跑的流水线互不可见。
    assignees_json 为有序成员 id 数组，current_index 指向正在执行的那一位；
    artifacts_json 只存共享工作电脑上的路径+描述（单一真相源在电脑上）。
    """

    __tablename__ = "team_tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    team_id: Mapped[str] = mapped_column(String(32), index=True)  # 团队行 member id（无 FK，团队解散后保留历史）
    user_key: Mapped[str] = mapped_column(String(64), index=True)  # 发起用户 key（member_id 或 平台:原始id）
    title: Mapped[str] = mapped_column(String(200), default="")
    brief: Mapped[str] = mapped_column(Text, default="")
    artifacts_json: Mapped[str] = mapped_column(Text, default="[]")  # [{"path": "...", "desc": "..."}]
    assignees_json: Mapped[str] = mapped_column(Text, default="[]")  # ["member_id", ...] 按顺序执行
    current_index: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # pending / in_progress / review / done / rejected / cancelled
    rework_count: Mapped[int] = mapped_column(Integer, default=0)
    reports_json: Mapped[str] = mapped_column(Text, default="[]")  # 各被指派者的 team_report 记录
    review_verdict_json: Mapped[str] = mapped_column(Text, default="{}")  # {"verdict","comment","by","at"}
    created_by_member_id: Mapped[str] = mapped_column(String(32), default="")
    parent_task_id: Mapped[str] = mapped_column(String(32), default="")  # 返工/验收任务指回源任务
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class TeamTaskQueue(Base):
    """AI 团队响应队列 — (team_id, user_key) 一条 lane，lane 内严格串行

    kind：user_message（用户新消息，优先于一切指派）/ member_run（成员执行任务）/
    summary_turn（多人任务全部跑完后回到指派者汇总）/ leader_wakeup（异常兜底唤醒负责人）。
    status：pending / running / done / failed / skipped。重启时 running 标 failed，pending 续跑。
    """

    __tablename__ = "team_task_queue"

    id: Mapped[int] = mapped_column(_BigIntPK, primary_key=True, autoincrement=True)
    team_id: Mapped[str] = mapped_column(String(32), index=True)
    user_key: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    run_id: Mapped[str] = mapped_column(String(32), default="")  # 最近一次执行的 run id
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
