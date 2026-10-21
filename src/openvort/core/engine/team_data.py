"""
AI 团队编排数据访问层

团队会话与消息、任务表、响应队列的全部 DB 读写。纯函数式：每个调用方自带
session（编排器/工具/路由各自开会话），本模块不持有长连接。

团队会话的约定：
- channel="web" + target_type="team" + target_id=团队行 id，user_id=发起用户 key。
  Web 与 IM 双端共用同一行（IM 入站消息也写进这个会话），公屏天然按用户隔离。
- 展示历史不走 ChatSession.messages（那是 LLM 上下文 JSON），走 ChatMessage 表，
  普通视图只取 visibility="broadcast"。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime

from sqlalchemy import select, update

from openvort.contacts.models import Member
from openvort.db.models import ChatMessage, ChatSession, TeamTask, TeamTaskQueue
from openvort.utils.logging import get_logger

log = get_logger("core.team_data")

QUEUE_KINDS = ("user_message", "member_run", "summary_turn", "leader_wakeup")
TASK_STATUSES = ("pending", "in_progress", "review", "done", "rejected", "cancelled")


# ---- 团队会话 ----


async def get_or_create_team_session(db, team_id: str, user_key: str,
                                     team_name: str = "") -> str:
    """获取/创建某用户与某团队的会话，返回 session_id。"""
    stmt = select(ChatSession).where(
        ChatSession.channel == "web",
        ChatSession.user_id == user_key,
        ChatSession.target_type == "team",
        ChatSession.target_id == team_id,
        ChatSession.hidden == False,  # noqa: E712
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row:
        return row.session_id
    session_id = str(uuid.uuid4())[:8]
    db.add(ChatSession(
        channel="web",
        user_id=user_key,
        session_id=session_id,
        title=team_name or "团队会话",
        messages="[]",
        target_type="team",
        target_id=team_id,
    ))
    await db.flush()
    return session_id


async def find_team_session(db, team_id: str, user_key: str) -> ChatSession | None:
    stmt = select(ChatSession).where(
        ChatSession.channel == "web",
        ChatSession.user_id == user_key,
        ChatSession.target_type == "team",
        ChatSession.target_id == team_id,
    ).order_by(ChatSession.id.desc())
    return (await db.execute(stmt)).scalars().first()


async def write_team_message(
    db, *, session_id: str, owner_id: str, sender_type: str, sender_id: str,
    content: str, visibility: str = "broadcast", source: str = "chat",
    metadata: dict | None = None,
) -> ChatMessage:
    """写一条团队会话消息并触达会话 updated_at / 未读数。"""
    msg = ChatMessage(
        session_id=session_id,
        owner_id=owner_id,
        sender_type=sender_type,
        sender_id=sender_id,
        content=content or "",
        visibility=visibility if visibility in ("broadcast", "private") else "broadcast",
        source=source,
        is_read=sender_type == "user",
        metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
    )
    db.add(msg)
    # 碰一下会话行：列表按 updated_at 排序、未读计数依赖它（无字段改动不会触发 onupdate）
    stmt = select(ChatSession).where(
        ChatSession.session_id == session_id,
        ChatSession.user_id == owner_id,
    )
    row = (await db.execute(stmt)).scalars().first()
    if row:
        row.updated_at = datetime.utcnow()
        if sender_type != "user":
            row.unread_count = (row.unread_count or 0) + 1
    await db.flush()
    return msg


async def list_session_messages(
    db, session_id: str, *, visibility: str | None = None,
    limit: int = 100, offset: int = 0,
) -> list[ChatMessage]:
    stmt = select(ChatMessage).where(ChatMessage.session_id == session_id)
    if visibility:
        stmt = stmt.where(ChatMessage.visibility == visibility)
    stmt = stmt.order_by(ChatMessage.id.desc()).limit(limit).offset(offset)
    rows = list((await db.execute(stmt)).scalars())
    rows.reverse()
    return rows


def safe_dir_name(key: str) -> str:
    """user_key → 共享电脑上的安全目录名。"""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", key or "").strip("._") or "user"
    return cleaned[:48]


# ---- 任务表 ----


async def create_team_task(db, *, team_id: str, user_key: str, title: str, brief: str,
                           assignees: list[str], artifacts: list[dict] | None = None,
                           created_by_member_id: str = "", parent_task_id: str = "",
                           status: str = "pending") -> TeamTask:
    task = TeamTask(
        team_id=team_id,
        user_key=user_key,
        title=(title or "")[:200],
        brief=brief or "",
        artifacts_json=json.dumps(artifacts or [], ensure_ascii=False),
        assignees_json=json.dumps(assignees, ensure_ascii=False),
        current_index=0,
        status=status if status in TASK_STATUSES else "pending",
        created_by_member_id=created_by_member_id,
        parent_task_id=parent_task_id,
    )
    db.add(task)
    await db.flush()
    return task


async def get_team_task(db, task_id: str) -> TeamTask | None:
    if not task_id:
        return None
    return await db.get(TeamTask, task_id)


def task_reports(task: TeamTask) -> list[dict]:
    try:
        data = json.loads(task.reports_json or "[]")
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def task_artifacts(task: TeamTask) -> list[dict]:
    try:
        data = json.loads(task.artifacts_json or "[]")
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def task_assignees(task: TeamTask) -> list[str]:
    try:
        data = json.loads(task.assignees_json or "[]")
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


async def append_task_report(db, task: TeamTask, entry: dict) -> None:
    reports = task_reports(task)
    reports.append(entry)
    task.reports_json = json.dumps(reports, ensure_ascii=False)
    await db.flush()


async def extend_task_artifacts(db, task: TeamTask, artifacts: list[dict]) -> None:
    if not artifacts:
        return
    merged = task_artifacts(task)
    for item in artifacts:
        if isinstance(item, dict) and item.get("path"):
            merged.append({"path": item["path"], "desc": item.get("desc", "")})
    task.artifacts_json = json.dumps(merged, ensure_ascii=False)
    await db.flush()


async def set_task_status(db, task: TeamTask, status: str,
                          verdict: dict | None = None) -> None:
    if status not in TASK_STATUSES:
        return
    task.status = status
    if verdict:
        task.review_verdict_json = json.dumps(verdict, ensure_ascii=False)
    await db.flush()


# ---- 响应队列 ----


async def enqueue(db, team_id: str, user_key: str, kind: str,
                  payload: dict | None = None) -> TeamTaskQueue:
    assert kind in QUEUE_KINDS, f"未知队列 kind: {kind}"
    item = TeamTaskQueue(
        team_id=team_id,
        user_key=user_key,
        kind=kind,
        payload_json=json.dumps(payload or {}, ensure_ascii=False),
        status="pending",
    )
    db.add(item)
    await db.flush()
    return item


async def claim_next_item(db, team_id: str, user_key: str) -> TeamTaskQueue | None:
    """取走 lane 的下一条待处理项：用户消息优先于机器指派，同级 FIFO。"""
    lane = (TeamTaskQueue.team_id == team_id) & (TeamTaskQueue.user_key == user_key)
    pending = (TeamTaskQueue.status == "pending")
    stmt = (
        select(TeamTaskQueue)
        .where(lane & pending)
        .order_by((TeamTaskQueue.kind == "user_message").desc(), TeamTaskQueue.id.asc())
        .limit(1)
    )
    item = (await db.execute(stmt)).scalar_one_or_none()
    if not item:
        return None
    item.status = "running"
    item.started_at = datetime.utcnow()
    await db.flush()
    return item


async def finish_item(db, item: TeamTaskQueue, status: str, error: str = "") -> None:
    item.status = status if status in ("done", "failed", "skipped") else "done"
    item.error = (error or "")[:2000]
    item.finished_at = datetime.utcnow()
    await db.flush()


async def skip_pending_machine_items(db, team_id: str, user_key: str) -> int:
    """护栏触发后清空 lane 里剩余的机器项（用户消息保留）。"""
    stmt = (
        update(TeamTaskQueue)
        .where(
            TeamTaskQueue.team_id == team_id,
            TeamTaskQueue.user_key == user_key,
            TeamTaskQueue.status == "pending",
            TeamTaskQueue.kind != "user_message",
        )
        .values(status="skipped", error="guard: 暂停等用户", finished_at=datetime.utcnow())
    )
    result = await db.execute(stmt)
    await db.flush()
    return result.rowcount or 0


async def recover_on_startup(db) -> int:
    """进程重启：孤儿 running 标 failed（pending 保留续跑）。"""
    stmt = (
        update(TeamTaskQueue)
        .where(TeamTaskQueue.status == "running")
        .values(status="failed", error="Service restarted", finished_at=datetime.utcnow())
    )
    result = await db.execute(stmt)
    await db.flush()
    return result.rowcount or 0


async def pending_lanes(db) -> list[tuple[str, str]]:
    """有待处理项的 lane 列表。"""
    stmt = select(TeamTaskQueue.team_id, TeamTaskQueue.user_key).where(
        TeamTaskQueue.status == "pending"
    ).distinct()
    return [(r[0], r[1]) for r in (await db.execute(stmt)).all()]


async def has_pending_after(db, team_id: str, user_key: str, after_id: int) -> bool:
    """lane 里是否有比 after_id 更新的待处理项（判断成员跑完后有没有自己接力指派）。"""
    stmt = select(TeamTaskQueue.id).where(
        TeamTaskQueue.team_id == team_id,
        TeamTaskQueue.user_key == user_key,
        TeamTaskQueue.status == "pending",
        TeamTaskQueue.id > after_id,
    ).limit(1)
    return (await db.execute(stmt)).scalar_one_or_none() is not None


async def set_item_run_id(db, item: TeamTaskQueue, run_id: str) -> None:
    item.run_id = run_id
    await db.flush()


async def requeue_item(db, item: TeamTaskQueue, error: str) -> None:
    """失败重试：回到 pending，attempts 已由调用方累加。"""
    item.status = "pending"
    item.error = (error or "")[:2000]
    item.started_at = None
    await db.flush()


# ---- 杂项 ----


async def member_display_name(db, member_id: str) -> str:
    m = await db.get(Member, member_id)
    return m.name if m else (member_id or "")[:8]
