"""
团队编排事件枢纽（进程内）

三件事，都是纯内存状态，不碰数据库：
1. 公屏广播订阅：IM handler / Web SSE 想等"负责人首答"时在这里挂队列；
   team_broadcast 工具每写一条公屏消息就推一次，同时推 WS `team_message`。
2. 运行状态推送：编排器每次启停一个 run 推 WS `team_status`，前端渲染"XX 正在处理"状态条。
3. 回合协议标记：team_report 工具把 run_id 记进来，编排器在 run 结束后据此判断"有没有交接棒"。

WS 推送目标是 user_key；非成员 key（如 `wecom:xxx`）不会有 WS 连接，send_to 自然空转。
"""

from __future__ import annotations

import asyncio
import time

from openvort.utils.logging import get_logger

log = get_logger("core.team_hub")

_BROADCAST_QUEUE_MAX = 50
_REPORT_TTL_SECONDS = 6 * 3600

_broadcast_subscribers: dict[tuple[str, str], list[asyncio.Queue]] = {}
_reported_runs: dict[str, float] = {}
_run_broadcast_counts: dict[str, int] = {}


def _lane(team_id: str, user_key: str) -> tuple[str, str]:
    return (team_id, user_key)


# ---- 广播订阅 ----


def subscribe_broadcasts(team_id: str, user_key: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=_BROADCAST_QUEUE_MAX)
    _broadcast_subscribers.setdefault(_lane(team_id, user_key), []).append(q)
    return q


def unsubscribe_broadcasts(team_id: str, user_key: str, q: asyncio.Queue) -> None:
    subs = _broadcast_subscribers.get(_lane(team_id, user_key))
    if not subs:
        return
    try:
        subs.remove(q)
    except ValueError:
        pass
    if not subs:
        _broadcast_subscribers.pop(_lane(team_id, user_key), None)


async def notify_broadcast(team_id: str, user_key: str, message: dict, run_id: str = "") -> None:
    """一条公屏消息落库后调用：喂给所有订阅者 + 推 WS。"""
    if run_id:
        _run_broadcast_counts[run_id] = _run_broadcast_counts.get(run_id, 0) + 1
    for q in list(_broadcast_subscribers.get(_lane(team_id, user_key), [])):
        try:
            q.put_nowait(message)
        except asyncio.QueueFull:
            pass
    await _ws_send(user_key, {
        "type": "team_message",
        "team_id": team_id,
        "user_key": user_key,
        "message": message,
    })


async def wait_for_broadcast(team_id: str, user_key: str, timeout: float,
                             q: asyncio.Queue | None = None) -> dict | None:
    """等 lane 上的下一条公屏消息（Web SSE / IM 首答用）；超时返回 None。

    调用方若已提前 subscribe（避免漏掉提交与等待之间的消息），把队列传进来。
    """
    own = q is None
    if own:
        q = subscribe_broadcasts(team_id, user_key)
    try:
        return await asyncio.wait_for(q.get(), timeout=timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        if own:
            unsubscribe_broadcasts(team_id, user_key, q)


# ---- 运行状态 ----


async def push_team_status(team_id: str, user_key: str, **payload) -> None:
    await _ws_send(user_key, {
        "type": "team_status",
        "team_id": team_id,
        "user_key": user_key,
        **payload,
    })


async def _ws_send(user_key: str, data: dict) -> None:
    try:
        from openvort.web.ws import manager as ws_mgr
        await ws_mgr.send_to(user_key, data)
    except Exception as e:
        log.debug(f"WS 推送失败（忽略）: {e}")


# ---- 回合协议标记 ----


def mark_reported(run_id: str) -> None:
    if run_id:
        _reported_runs[run_id] = time.monotonic()
        _gc_reports()


def is_reported(run_id: str) -> bool:
    return run_id in _reported_runs


def broadcast_count(run_id: str) -> int:
    return _run_broadcast_counts.get(run_id, 0)


def forget_run(run_id: str) -> None:
    _reported_runs.pop(run_id, None)
    _run_broadcast_counts.pop(run_id, None)


def _gc_reports() -> None:
    if len(_reported_runs) < 500:
        return
    cutoff = time.monotonic() - _REPORT_TTL_SECONDS
    for rid in [r for r, ts in _reported_runs.items() if ts < cutoff]:
        _reported_runs.pop(rid, None)
        _run_broadcast_counts.pop(rid, None)
