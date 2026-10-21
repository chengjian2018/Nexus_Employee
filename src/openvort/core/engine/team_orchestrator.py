"""
AI 团队编排器 — 响应队列 worker

(team_id, user_key) 是一条 lane：lane 内严格串行（同一时刻只跑一棒），不同 lane 并行。
照抄 AgentTaskRunner 的形态：DB 表记录状态、常驻 asyncio 任务消费、重启时孤儿 running
标 failed、pending 续跑。

一条用户消息的生命周期（见设计共识）：
  user_message → leader_turn → [member_run …] → summary_turn / review → …
护栏：
  - 单条用户消息触发的 run 总数 ≤ config.max_runs，超限只唤醒 leader 做阶段汇报并暂停 lane
  - 成员 run 失败重试 1 次 → 再失败唤醒 leader → leader 也失败则公屏广播暂停
  - 成员 run 结束没 team_report → 唤醒 leader 兜底
"""

from __future__ import annotations

import asyncio
import json

from openvort.core.engine import team_data, team_hub
from openvort.core.engine.team_runtime import TeamRunSpec, execute_team_run, new_run_id
from openvort.core.engine.team_service import get_team, strip_team_prefix
from openvort.core.engine.virtual_persona import parse_team_persona
from openvort.utils.logging import get_logger

log = get_logger("core.team_orchestrator")

POLL_INTERVAL_SECONDS = 1.5
PAUSE_NOTICE = (
    "⚠️ 团队流水线已暂停：负责人兜底处理也失败了，为避免空转已停止自动接力。"
    "请发一条新消息告诉团队接下来怎么做。"
)
GUARD_NOTICE_REASON = (
    "流水线超限暂停：本轮用户消息已触发 {runs} 次成员执行，达到上限 {limit}。"
    "请做阶段性汇报并等待用户指示，不要再指派。"
)


class TeamOrchestrator:
    def __init__(self, session_factory, llm, registry):
        self._sf = session_factory
        self._llm = llm
        self._registry = registry
        self._lane_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._chains: dict[tuple[str, str], dict] = {}
        # lane 当前正在跑的一棒（供前端刷新/重连后拉取，补推送只发一次的缺口）
        self._lane_status: dict[tuple[str, str], dict] = {}
        self._poll_task: asyncio.Task | None = None
        self._running = False
        self._wake = asyncio.Event()

    def get_lane_status(self, team_id: str, user_key: str) -> dict:
        info = self._lane_status.get((team_id, user_key))
        if not info:
            return {"running": False}
        return {"running": True, **info}

    # ---- 生命周期 ----

    async def start(self) -> None:
        async with self._sf() as db:
            recovered = await team_data.recover_on_startup(db)
            await db.commit()
        if recovered:
            log.info(f"团队队列：重启回收 {recovered} 条孤儿 running 项")
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        log.info("TeamOrchestrator 已启动")

    async def stop(self) -> None:
        self._running = False
        self._wake.set()
        for t in [self._poll_task, *self._lane_tasks.values()]:
            if t and not t.done():
                t.cancel()
        self._lane_tasks.clear()

    def is_lane_busy(self, team_id: str, user_key: str) -> bool:
        t = self._lane_tasks.get((team_id, user_key))
        return bool(t and not t.done())

    # ---- 入口：用户消息 ----

    async def submit_user_message(self, team_id: str, user_key: str, content: str, *,
                                  source: str = "chat", sender_name: str = "",
                                  metadata: dict | None = None) -> str:
        """写公屏 + 入队，返回团队会话 session_id。调用方可提前 subscribe 等首答。"""
        content = (content or "").strip()
        if not content:
            raise ValueError("消息内容为空")
        async with self._sf() as db:
            team_row = await get_team(db, team_id)
            session_id = await team_data.get_or_create_team_session(
                db, team_id, user_key, strip_team_prefix(team_row.name))
            await team_data.write_team_message(
                db, session_id=session_id, owner_id=user_key,
                sender_type="user", sender_id=user_key, content=content,
                visibility="broadcast", source=source,
                metadata={"sender_name": sender_name, **(metadata or {})},
            )
            await team_data.enqueue(db, team_id, user_key, "user_message",
                                    {"content": content, "sender_name": sender_name})
            await db.commit()
        self._wake.set()
        return session_id

    # ---- worker ----

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                async with self._sf() as db:
                    lanes = await team_data.pending_lanes(db)
                for lane in lanes:
                    task = self._lane_tasks.get(lane)
                    if task and not task.done():
                        continue
                    self._lane_tasks[lane] = asyncio.create_task(self._consume_lane(*lane))
            except Exception as e:
                log.error(f"团队队列轮询异常: {e}")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def _consume_lane(self, team_id: str, user_key: str) -> None:
        lane = (team_id, user_key)
        chain = self._chains.setdefault(lane, {"runs": 0, "guard_hit": False, "paused": False})
        try:
            while self._running:
                async with self._sf() as db:
                    item = await team_data.claim_next_item(db, team_id, user_key)
                    await db.commit()
                if not item:
                    break
                try:
                    await self._process_item(item, chain)
                except Exception as e:
                    log.error(f"团队队列项 {item.id} 处理异常（已标 failed）: {e}", exc_info=True)
                    async with self._sf() as db:
                        row = await db.get(team_data.TeamTaskQueue, item.id)
                        if row:
                            await team_data.finish_item(db, row, "failed", str(e))
                            await db.commit()
        except asyncio.CancelledError:
            pass
        finally:
            self._lane_tasks.pop(lane, None)

    async def _process_item(self, item, chain: dict) -> None:
        payload = _loads(item.payload_json)
        team_id, user_key = item.team_id, item.user_key
        async with self._sf() as db:
            team_row = await get_team(db, team_id)
            team = parse_team_persona(team_row.virtual_system_prompt)
            if not team:
                raise RuntimeError("团队人设 JSON 解析失败")
            team_name = strip_team_prefix(team_row.name)
        cfg = team["config"]
        roster_names = {e["member_id"]: e.get("name", "") for e in team["roster"]}

        # 机器项在 lane 暂停期间一律跳过（直到下一条用户消息重置）
        if item.kind != "user_message" and chain.get("paused") and not payload.get("guard"):
            await self._finish(item.id, "skipped", "lane paused")
            return

        spec = self._build_spec(item, payload, team, chain)
        if spec is None:
            await self._finish(item.id, "skipped", "unknown kind")
            return

        if item.kind == "user_message":
            chain.update(runs=0, guard_hit=False, paused=False)
            await self._ensure_user_workspace(team_row, user_key, team)
        elif not payload.get("guard") and chain["runs"] >= cfg["max_runs"]:
            await self._finish(item.id, "skipped", "guard: max_runs")
            if not chain["guard_hit"]:
                chain["guard_hit"] = True
                async with self._sf() as db:
                    await team_data.enqueue(db, team_id, user_key, "leader_wakeup", {
                        "reason": GUARD_NOTICE_REASON.format(runs=chain["runs"], limit=cfg["max_runs"]),
                        "guard": True,
                    })
                    await db.commit()
            return

        if spec.kind == "member_run" and spec.task_id and not spec.review:
            async with self._sf() as db:
                task = await team_data.get_team_task(db, spec.task_id)
                if task and task.status == "pending":
                    task.status = "in_progress"
                await db.commit()

        async with self._sf() as db:
            row = await db.get(team_data.TeamTaskQueue, item.id)
            if row:
                await team_data.set_item_run_id(db, row, spec.run_id)
                await db.commit()

        acting_name = roster_names.get(spec.acting_member_id, spec.acting_member_id)
        task_title = await self._task_title(spec.task_id)
        self._lane_status[(team_id, user_key)] = {
            "member_id": spec.acting_member_id, "member_name": acting_name,
            "task_title": task_title, "kind": spec.kind, "run_id": spec.run_id,
        }
        await team_hub.push_team_status(
            team_id, user_key, status="running", run_id=spec.run_id, kind=spec.kind,
            member_id=spec.acting_member_id, member_name=acting_name,
            task_title=task_title, team_name=team_name,
        )
        log.info(f"[team:{team_name}] lane={user_key} 开跑 {spec.kind} by {acting_name} run={spec.run_id[:8]}")

        try:
            result = await asyncio.wait_for(
                execute_team_run(llm=self._llm, registry=self._registry, spec=spec),
                timeout=cfg["run_timeout"] * 60,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._handle_failure(item, spec, chain, e, team, acting_name)
            self._lane_status.pop((team_id, user_key), None)
            await team_hub.push_team_status(team_id, user_key, status="idle", run_id=spec.run_id)
            return

        chain["runs"] += 1
        await self._finish(item.id, "done")
        await self._after_run(item, spec, chain, team, roster_names, result)
        if payload.get("guard"):
            chain["paused"] = True
            async with self._sf() as db:
                await team_data.skip_pending_machine_items(db, team_id, user_key)
                await db.commit()
        team_hub.forget_run(spec.run_id)
        self._lane_status.pop((team_id, user_key), None)
        await team_hub.push_team_status(team_id, user_key, status="idle", run_id=spec.run_id)
        log.info(f"[team:{team_name}] lane={user_key} 完成 {spec.kind} by {acting_name} "
                 f"broadcasts={result.get('broadcast_count', 0)} runs={chain['runs']}")

    def _build_spec(self, item, payload: dict, team: dict, chain: dict) -> TeamRunSpec | None:
        base = dict(run_id=new_run_id(), team_id=item.team_id, user_key=item.user_key)
        if item.kind == "user_message":
            return TeamRunSpec(kind="leader_turn", acting_member_id=team["leader_id"],
                               user_content=payload.get("content", ""), **base)
        if item.kind == "member_run":
            member_id = payload.get("member_id") or team["leader_id"]
            return TeamRunSpec(kind="member_run", acting_member_id=member_id,
                               task_id=payload.get("task_id", ""),
                               review=bool(payload.get("review")),
                               requested_by=payload.get("requested_by", ""), **base)
        if item.kind == "summary_turn":
            return TeamRunSpec(kind="summary_turn",
                               acting_member_id=payload.get("member_id") or team["leader_id"],
                               task_id=payload.get("task_id", ""), **base)
        if item.kind == "leader_wakeup":
            return TeamRunSpec(kind="leader_wakeup", acting_member_id=team["leader_id"],
                               reason=payload.get("reason", ""),
                               task_id=payload.get("task_id", ""), **base)
        return None

    # ---- 失败链 ----

    async def _handle_failure(self, item, spec: TeamRunSpec, chain: dict, exc: Exception,
                              team: dict, acting_name: str) -> None:
        err = str(exc) or exc.__class__.__name__
        if isinstance(exc, asyncio.TimeoutError):
            err = f"执行超时（{team['config']['run_timeout']} 分钟）"
        log.warning(f"团队 run 失败: kind={spec.kind} by {acting_name} err={err}")
        async with self._sf() as db:
            row = await db.get(team_data.TeamTaskQueue, item.id)
            if not row:
                return
            row.attempts = (row.attempts or 0) + 1
            if item.kind != "leader_wakeup" and row.attempts <= 1:
                await team_data.requeue_item(db, row, err)
                await db.commit()
                return
            await team_data.finish_item(db, row, "failed", err)
            if item.kind == "leader_wakeup":
                chain["paused"] = True
                await team_data.skip_pending_machine_items(db, item.team_id, item.user_key)
                session_id = await team_data.get_or_create_team_session(
                    db, item.team_id, item.user_key)
                msg = await team_data.write_team_message(
                    db, session_id=session_id, owner_id=item.user_key,
                    sender_type="system", sender_id="", content=PAUSE_NOTICE,
                    visibility="broadcast", source="team",
                )
                await db.commit()
                await team_hub.notify_broadcast(item.team_id, item.user_key, {
                    "id": str(msg.id), "role": "system", "content": PAUSE_NOTICE,
                    "sender_id": "", "sender_name": "系统",
                    "timestamp": msg.created_at.isoformat() if msg.created_at else None,
                    "session_id": session_id,
                })
                return
            task_title = ""
            if spec.task_id:
                task = await team_data.get_team_task(db, spec.task_id)
                task_title = task.title if task else ""
            await team_data.enqueue(db, item.team_id, item.user_key, "leader_wakeup", {
                "reason": f"成员「{acting_name}」执行「{task_title or spec.kind}」连续失败：{err}",
                "task_id": spec.task_id,
            })
            await db.commit()

    # ---- run 后处理：回合协议 + 任务推进 ----

    async def _after_run(self, item, spec: TeamRunSpec, chain: dict, team: dict,
                         roster_names: dict, result: dict) -> None:
        if spec.kind != "member_run" or not spec.task_id:
            return
        acting_name = roster_names.get(spec.acting_member_id, spec.acting_member_id)
        async with self._sf() as db:
            task = await team_data.get_team_task(db, spec.task_id)
            if not task:
                return
            if not team_hub.is_reported(spec.run_id):
                await team_data.enqueue(db, item.team_id, item.user_key, "leader_wakeup", {
                    "reason": f"成员「{acting_name}」处理任务「{task.title}」结束时没有调用 team_report 交接，"
                              "产出去向不明，请你兜底：核实、重派或自己接手。",
                    "task_id": task.id,
                })
                await db.commit()
                return
            if spec.review:
                await db.commit()
                return
            if task.status == "review":
                # 已被 team_request_review 转入验收，等负责人验收回合
                await db.commit()
                return
            assignees = team_data.task_assignees(task)
            next_idx = (task.current_index or 0) + 1
            task.current_index = next_idx
            if next_idx < len(assignees):
                task.status = "in_progress"
                await team_data.enqueue(db, item.team_id, item.user_key, "member_run",
                                        {"task_id": task.id, "member_id": assignees[next_idx]})
                await db.commit()
                return
            # 全部被指派者跑完：收尾状态 + 决定是否回到指派人汇总
            if task.status in ("pending", "in_progress"):
                task.status = "done"
            has_followup = await team_data.has_pending_after(
                db, item.team_id, item.user_key, item.id)
            creator = task.created_by_member_id
            if creator and creator != spec.acting_member_id and (len(assignees) > 1 or not has_followup):
                await team_data.enqueue(db, item.team_id, item.user_key, "summary_turn",
                                        {"task_id": task.id, "member_id": creator})
            await db.commit()

    # ---- 共享电脑目录骨架（P5）----

    async def _ensure_user_workspace(self, team_row, user_key: str, team: dict) -> None:
        node_id = getattr(team_row, "remote_node_id", "") or ""
        if not node_id:
            return
        try:
            from openvort.db.models import RemoteNode
            from openvort.core.execution.docker_executor import DockerExecutor
            async with self._sf() as db:
                node = await db.get(RemoteNode, node_id)
            if not node or node.status not in ("online", "running"):
                return
            cid = (_loads(node.config)).get("container_id", "")
            if not cid:
                return
            user_dir = team_data.safe_dir_name(user_key)
            dirs = [f"/workspace/users/{user_dir}/public", "/workspace/team"]
            for e in team["roster"]:
                dirs.append(f"/workspace/users/{user_dir}/private/{team_data.safe_dir_name(e.get('name') or e['member_id'])}")
            cmd = "mkdir -p " + " ".join(f"'{d}'" for d in dirs)
            await DockerExecutor._exec_streaming(cid, cmd, timeout=10)
        except Exception as e:
            log.debug(f"初始化团队电脑用户目录失败（忽略）: {e}")

    # ---- 小工具 ----

    async def _finish(self, item_id: int, status: str, error: str = "") -> None:
        async with self._sf() as db:
            row = await db.get(team_data.TeamTaskQueue, item_id)
            if row:
                await team_data.finish_item(db, row, status, error)
                await db.commit()

    async def _task_title(self, task_id: str) -> str:
        if not task_id:
            return ""
        async with self._sf() as db:
            task = await team_data.get_team_task(db, task_id)
            return task.title if task else ""


def _loads(text: str | None) -> dict:
    try:
        data = json.loads(text or "{}")
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


_orchestrator: TeamOrchestrator | None = None


def get_team_orchestrator() -> TeamOrchestrator | None:
    return _orchestrator


def init_team_orchestrator(session_factory, llm, registry) -> TeamOrchestrator:
    global _orchestrator
    _orchestrator = TeamOrchestrator(session_factory, llm, registry)
    return _orchestrator
