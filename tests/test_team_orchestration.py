"""AI 团队编排回复路径 — 内存 SQLite + 脚本化假 LLM 的回归测试。

覆盖：团队 JSON sop/config 键（解析/默认值/重建保留）、队列优先级（用户消息插队）、
端到端流水线（leader 首答→指派→成员执行→汇报→回指派者汇总）、多人依次执行、
回合协议兜底（未汇报→唤醒负责人）、失败链（重试→唤醒→暂停广播）、
max_runs 护栏（跳过机器项 + 守卫唤醒 + lane 暂停）。
"""

import json
import unittest
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import openvort.auth.models  # noqa: F401
import openvort.contacts.models  # noqa: F401
import openvort.db.models  # noqa: F401
import openvort.db.engine as db_engine
from openvort.contacts.models import Member
from openvort.core.engine import team_data, team_hub, team_service
from openvort.core.engine.team_orchestrator import PAUSE_NOTICE, TeamOrchestrator
from openvort.core.engine.virtual_persona import (
    TEAM_CONFIG_DEFAULTS, build_team_persona, parse_team_persona,
)
from openvort.db.engine import Base
from openvort.db.models import ChatMessage, TeamTask, TeamTaskQueue


async def _make_db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _add_employee(db, name: str, position: str) -> Member:
    m = Member(name=name, position=position, is_virtual=True,
               virtual_system_prompt=json.dumps({
                   "version": 2, "identity": f"{name} 的职责", "persona": "严谨",
                   "bible": "按流程",
               }), skills="[]")
    db.add(m)
    await db.flush()
    return m


# ---- 脚本化假 LLM / 假插件注册表 ----


def text_resp(text: str):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn")


def tool_resp(*calls: tuple[str, dict], text: str = ""):
    blocks = []
    if text:
        blocks.append(SimpleNamespace(type="text", text=text))
    for i, (name, inp) in enumerate(calls):
        blocks.append(SimpleNamespace(type="tool_use", id=f"t{i}", name=name, input=inp))
    return SimpleNamespace(content=blocks, stop_reason="tool_use")


class ScriptedLLM:
    """按顺序弹出预设响应；可用 callable 生成响应或抛错。"""

    def __init__(self, script: list):
        self.script = list(script)
        self.calls: list[dict] = []

    async def create(self, *, system, messages, tools=None, **kw):
        self.calls.append({"system": system, "messages": messages, "tools": tools})
        if not self.script:
            return text_resp("（脚本耗尽）")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return step()
        return step


class FakeRegistry:
    def list_plugins(self):
        return []

    def is_plugin_disabled(self, name):
        return False

    def to_claude_tools(self, **kw):
        return []

    async def execute_tool(self, name, params):
        return json.dumps({"ok": False, "message": f"no tool {name}"})


class TeamOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine, self.sf = await _make_db()
        self._old_sf = db_engine._session_factory
        db_engine._session_factory = self.sf  # team_tools / team_runtime 用全局 factory
        async with self.sf() as db:
            self.leader = await _add_employee(db, "阿产", "产品负责人")
            self.pm = await _add_employee(db, "小产", "产品经理")
            self.fe = await _add_employee(db, "小前", "前端")
            self.team = await team_service.create_team(
                db, "大厂团队", "demo", "I", "P", "B",
                [self.leader.id, self.pm.id, self.fe.id], {}, self.leader.id,
            )
            await db.commit()
        self.user = "user-1"

    async def asyncTearDown(self):
        db_engine._session_factory = self._old_sf
        await self.engine.dispose()

    def _orch(self, script) -> TeamOrchestrator:
        self.llm = ScriptedLLM(script)
        return TeamOrchestrator(self.sf, self.llm, FakeRegistry())

    async def _run_lane(self, orch: TeamOrchestrator):
        orch._running = True
        await orch._consume_lane(self.team.id, self.user)

    @staticmethod
    def _user_texts(call: dict) -> str:
        """某次 LLM 调用的全部 user 文本（messages 是引用，后续会被追加，只看 user 字符串）。"""
        return "\n".join(
            m["content"] for m in call["messages"]
            if m.get("role") == "user" and isinstance(m.get("content"), str)
        )

    async def _queue(self) -> list[TeamTaskQueue]:
        async with self.sf() as db:
            return list((await db.execute(
                select(TeamTaskQueue).order_by(TeamTaskQueue.id))).scalars())

    async def _tasks(self) -> list[TeamTask]:
        async with self.sf() as db:
            return list((await db.execute(select(TeamTask).order_by(TeamTask.created_at))).scalars())

    async def _broadcasts(self) -> list[ChatMessage]:
        async with self.sf() as db:
            return list((await db.execute(
                select(ChatMessage).where(ChatMessage.visibility == "broadcast")
                .order_by(ChatMessage.id))).scalars())

    # ---- 团队 JSON：sop / config ----

    async def test_persona_sop_config_roundtrip_and_defaults(self):
        data = parse_team_persona(self.team.virtual_system_prompt)
        self.assertEqual(data["config"], TEAM_CONFIG_DEFAULTS)  # 缺键 → 默认
        self.assertEqual(data["sop"], {})
        raw = build_team_persona("I", "P", "B", self.leader.id, data["roster"], "",
                                 sop={"summary": "摘要", "full_text": "全文"},
                                 config={"max_runs": 3, "max_rework": "bad", "run_timeout": -1})
        parsed = parse_team_persona(raw)
        self.assertEqual(parsed["sop"]["summary"], "摘要")
        self.assertEqual(parsed["config"]["max_runs"], 3)
        self.assertEqual(parsed["config"]["max_rework"], TEAM_CONFIG_DEFAULTS["max_rework"])
        self.assertEqual(parsed["config"]["run_timeout"], TEAM_CONFIG_DEFAULTS["run_timeout"])
        # 老 JSON 不含新键照常解析
        old = json.loads(raw)
        old.pop("sop"); old.pop("config")
        self.assertIsNotNone(parse_team_persona(json.dumps(old)))

    async def test_rebuild_preserves_sop_config(self):
        async with self.sf() as db:
            team = await team_service.get_team(db, self.team.id)
            await team_service.update_team_sop_config(
                db, team, sop={"summary": "S", "full_text": "F"}, config={"max_runs": 7})
            await team_service.update_team_profile(db, team, identity="新身份")  # 重建 JSON
            await team_service.change_team_leader(db, team, self.pm.id)
            await db.commit()
            data = parse_team_persona(team.virtual_system_prompt)
        self.assertEqual(data["identity"], "新身份")
        self.assertEqual(data["sop"]["summary"], "S")
        self.assertEqual(data["config"]["max_runs"], 7)
        self.assertEqual(data["leader_id"], self.pm.id)

    # ---- 队列优先级 ----

    async def test_user_message_jumps_ahead_of_machine_items(self):
        async with self.sf() as db:
            await team_data.enqueue(db, self.team.id, self.user, "member_run", {"member_id": self.pm.id})
            await team_data.enqueue(db, self.team.id, self.user, "summary_turn", {})
            await team_data.enqueue(db, self.team.id, self.user, "user_message", {"content": "hi"})
            await db.commit()
            first = await team_data.claim_next_item(db, self.team.id, self.user)
            second = await team_data.claim_next_item(db, self.team.id, self.user)
            await db.commit()
        self.assertEqual(first.kind, "user_message")
        self.assertEqual(second.kind, "member_run")

    # ---- 端到端：首答 → 指派 → 执行 → 汇报 → 回指派者汇总 ----

    async def test_pipeline_single_assignee_round_trip(self):
        orch = self._orch([
            # leader_turn
            tool_resp(("team_broadcast", {"content": "收到，先出 PRD"}),
                      ("team_assign", {"title": "写 PRD", "brief": "收集需求产出 PRD",
                                       "assignees": ["小产"]}),
                      ("team_report", {"summary": "已分派给小产"})),
            text_resp("done leader"),
            # member_run（小产）
            tool_resp(("team_broadcast", {"content": "PRD v1 已产出"}),
                      ("team_report", {"summary": "PRD 在 public/prd.md",
                                       "artifacts": [{"path": "/workspace/users/user-1/public/prd.md", "desc": "PRD"}],
                                       "task_status": "done"})),
            text_resp("done pm"),
            # summary_turn（回到指派者 leader）
            tool_resp(("team_broadcast", {"content": "PRD 阶段完成，进入评审"}),
                      ("team_report", {"summary": "阶段收口"})),
            text_resp("done summary"),
        ])
        await orch.submit_user_message(self.team.id, self.user, "做一个登录功能", sender_name="老板")
        await self._run_lane(orch)

        queue = await self._queue()
        self.assertEqual([q.kind for q in queue], ["user_message", "member_run", "summary_turn"])
        self.assertTrue(all(q.status == "done" for q in queue), [q.status for q in queue])

        tasks = await self._tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].status, "done")
        self.assertEqual(json.loads(tasks[0].assignees_json), [self.pm.id])
        reports = json.loads(tasks[0].reports_json)
        self.assertEqual([r["member_id"] for r in reports], [self.pm.id, self.leader.id])
        self.assertEqual(json.loads(tasks[0].artifacts_json)[0]["desc"], "PRD")

        contents = [m.content for m in await self._broadcasts()]
        self.assertEqual(contents, ["做一个登录功能", "收到，先出 PRD", "PRD v1 已产出", "PRD 阶段完成，进入评审"])
        # 成员 run 的 system prompt 带了任务卡与协议
        member_call = self.llm.calls[2]
        self.assertIn("team_report", member_call["system"])
        self.assertIn("写 PRD", self._user_texts(member_call))
        self.assertTrue(any(t["name"] == "team_assign" for t in member_call["tools"]))

    async def test_multi_assignee_runs_in_order_then_summary(self):
        orch = self._orch([
            tool_resp(("team_assign", {"title": "评审 PRD", "brief": "各自评审",
                                       "assignees": ["小产", "小前"]}),
                      ("team_report", {"summary": "派出评审"})),
            text_resp("leader done"),
            tool_resp(("team_report", {"summary": "小产评审意见"})),
            text_resp("pm done"),
            tool_resp(("team_report", {"summary": "小前评审意见"})),
            text_resp("fe done"),
            tool_resp(("team_report", {"summary": "汇总完成"})),
            text_resp("summary done"),
        ])
        await orch.submit_user_message(self.team.id, self.user, "评审一下")
        await self._run_lane(orch)
        queue = await self._queue()
        kinds = [(q.kind, json.loads(q.payload_json).get("member_id")) for q in queue]
        self.assertEqual(kinds, [
            ("user_message", None),
            ("member_run", self.pm.id),
            ("member_run", self.fe.id),
            ("summary_turn", self.leader.id),
        ])
        task = (await self._tasks())[0]
        self.assertEqual(task.status, "done")
        self.assertEqual(task.current_index, 2)
        self.assertEqual([r["member_id"] for r in json.loads(task.reports_json)],
                         [self.pm.id, self.fe.id, self.leader.id])

    # ---- 回合协议：未汇报 → 唤醒负责人 ----

    async def test_member_without_report_wakes_leader(self):
        orch = self._orch([
            tool_resp(("team_assign", {"title": "写 PRD", "brief": "b", "assignees": ["小产"]}),
                      ("team_report", {"summary": "派了"})),
            text_resp("leader done"),
            text_resp("我做完了但忘了汇报"),           # 成员静默结束
            tool_resp(("team_report", {"summary": "兜底处理"})),  # leader_wakeup
            text_resp("wakeup done"),
        ])
        await orch.submit_user_message(self.team.id, self.user, "开始")
        await self._run_lane(orch)
        queue = await self._queue()
        self.assertEqual([q.kind for q in queue], ["user_message", "member_run", "leader_wakeup"])
        reason = json.loads(queue[2].payload_json)["reason"]
        self.assertIn("没有调用 team_report", reason)
        self.assertIn("小产", reason)
        self.assertIn("异常唤醒", self._user_texts(self.llm.calls[3]))

    # ---- 失败链：重试 → 唤醒 → 负责人也失败 → 暂停广播 ----

    async def test_failure_chain_pauses_lane(self):
        orch = self._orch([
            tool_resp(("team_assign", {"title": "写 PRD", "brief": "b", "assignees": ["小产"]}),
                      ("team_report", {"summary": "派了"})),
            text_resp("leader done"),
            RuntimeError("llm down 1"),   # 成员第一次失败 → 重试
            RuntimeError("llm down 2"),   # 第二次失败 → leader_wakeup
            RuntimeError("llm down 3"),   # 负责人兜底也失败 → 暂停
        ])
        await orch.submit_user_message(self.team.id, self.user, "开始")
        await self._run_lane(orch)
        queue = await self._queue()
        self.assertEqual([q.kind for q in queue], ["user_message", "member_run", "leader_wakeup"])
        self.assertEqual(queue[1].status, "failed")
        self.assertEqual(queue[1].attempts, 2)
        self.assertEqual(queue[2].status, "failed")
        self.assertIn("连续失败", json.loads(queue[2].payload_json)["reason"])
        self.assertTrue(orch._chains[(self.team.id, self.user)]["paused"])
        contents = [m.content for m in await self._broadcasts()]
        self.assertEqual(contents[-1], PAUSE_NOTICE)

    # ---- 护栏：max_runs ----

    async def test_max_runs_guard_skips_and_pauses(self):
        async with self.sf() as db:
            team = await team_service.get_team(db, self.team.id)
            await team_service.update_team_sop_config(db, team, config={"max_runs": 1})
            await db.commit()
        orch = self._orch([
            tool_resp(("team_assign", {"title": "写 PRD", "brief": "b", "assignees": ["小产"]}),
                      ("team_report", {"summary": "派了"})),
            text_resp("leader done"),                       # runs=1 → 已达上限
            tool_resp(("team_broadcast", {"content": "本轮已达上限，阶段汇报：…"}),
                      ("team_report", {"summary": "汇报并暂停"})),  # 守卫唤醒
            text_resp("guard done"),
        ])
        await orch.submit_user_message(self.team.id, self.user, "开始")
        await self._run_lane(orch)
        queue = await self._queue()
        self.assertEqual([q.kind for q in queue], ["user_message", "member_run", "leader_wakeup"])
        self.assertEqual(queue[1].status, "skipped")
        self.assertEqual(queue[2].status, "done")
        self.assertTrue(json.loads(queue[2].payload_json)["guard"])
        self.assertTrue(orch._chains[(self.team.id, self.user)]["paused"])
        # 新用户消息重置 lane（护栏不应锁死团队）
        self.llm.script.extend([tool_resp(("team_report", {"summary": "继续"})), text_resp("ok")])
        await orch.submit_user_message(self.team.id, self.user, "继续吧")
        await self._run_lane(orch)
        self.assertFalse(orch._chains[(self.team.id, self.user)]["paused"])

    # ---- 工具校验 ----

    async def test_assign_rejects_non_roster_and_rework_limit(self):
        from openvort.core.engine.team_tools import TEAM_TOOLS
        ctx = {"team_id": self.team.id, "user_key": self.user, "run_id": "r1",
               "acting_member_id": self.leader.id, "task_id": ""}
        res = json.loads(await TEAM_TOOLS["team_assign"].execute({
            "title": "t", "brief": "b", "assignees": ["路人甲"], "_team_context": ctx}))
        self.assertFalse(res["ok"])
        self.assertIn("花名册", res["message"])

        async with self.sf() as db:
            team = await team_service.get_team(db, self.team.id)
            await team_service.update_team_sop_config(db, team, config={"max_rework": 1})
            await db.commit()
        first = json.loads(await TEAM_TOOLS["team_assign"].execute({
            "title": "原任务", "brief": "b", "assignees": ["小产"], "_team_context": ctx}))
        self.assertTrue(first["ok"])
        rework = json.loads(await TEAM_TOOLS["team_assign"].execute({
            "title": "返工", "brief": "b", "assignees": ["小产"],
            "parent_task_id": first["task_id"], "_team_context": ctx}))
        self.assertFalse(rework["ok"])
        self.assertIn("返工", rework["message"])

    async def test_tools_require_team_context(self):
        from openvort.core.engine.team_tools import TEAM_TOOLS
        for name in ("team_broadcast", "team_assign", "team_report", "team_request_review", "team_read_sop"):
            res = json.loads(await TEAM_TOOLS[name].execute(
                {"content": "x", "summary": "x", "title": "x", "brief": "x", "assignees": ["a"]}))
            self.assertFalse(res["ok"], name)

    async def test_broadcast_feeds_hub_subscribers(self):
        from openvort.core.engine.team_tools import TEAM_TOOLS
        q = team_hub.subscribe_broadcasts(self.team.id, self.user)
        try:
            ctx = {"team_id": self.team.id, "user_key": self.user, "run_id": "r9",
                   "acting_member_id": self.leader.id, "task_id": ""}
            await TEAM_TOOLS["team_broadcast"].execute({"content": "首答", "_team_context": ctx})
            msg = await team_hub.wait_for_broadcast(self.team.id, self.user, timeout=1, q=q)
        finally:
            team_hub.unsubscribe_broadcasts(self.team.id, self.user, q)
        self.assertEqual(msg["content"], "首答")
        self.assertEqual(msg["sender_name"], "阿产")
        self.assertEqual(team_hub.broadcast_count("r9"), 1)


if __name__ == "__main__":
    unittest.main()
