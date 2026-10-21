"""AI 团队（team_service）回归测试 — 内存 SQLite。

覆盖：建团校验（min-2/去重/leader 在队/禁嵌套）、默认负责人、拉人/踢人护栏
（负责人保护、最小规模）、换负责人、改三件套保留花名册、解散保留成员、
插件宿主校验（负责人解析/多团队歧义）、插件工具端到端。
"""

import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import openvort.auth.models  # noqa: F401 — 注册全部表
import openvort.contacts.models  # noqa: F401
import openvort.db.models  # noqa: F401
from openvort.contacts.models import Member
from openvort.core.engine import team_service
from openvort.core.engine.team_service import TEAM_NAME_PREFIX, TeamError
from openvort.core.engine.virtual_persona import parse_team_persona
from openvort.db.engine import Base


async def _make_db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _add_employee(db, name: str, position: str = "工程师") -> Member:
    m = Member(name=name, position=position, is_virtual=True,
               virtual_system_prompt="", skills="[]")
    db.add(m)
    await db.flush()
    return m


async def _make_team(db, members: list[Member], name: str = "产品组", **kwargs):
    return await team_service.create_team(
        db, name, "测试团队", "I", "P", "B",
        [m.id for m in members], {}, **kwargs,
    )


class TeamServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine, self.sf = await _make_db()
        self.session = self.sf()
        async with self.session.begin():
            self.a = await _add_employee(self.session, "小A", "前端")
            self.b = await _add_employee(self.session, "小B", "后端")
            self.c = await _add_employee(self.session, "小C", "测试")

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_create_team_defaults(self):
        team = await _make_team(self.session, [self.a, self.b])
        self.assertEqual(team.name, TEAM_NAME_PREFIX + "产品组")
        self.assertTrue(team.is_virtual)
        data = parse_team_persona(team.virtual_system_prompt)
        self.assertEqual(data["leader_id"], self.a.id)  # 默认第一个成员是负责人
        self.assertEqual([e["member_id"] for e in data["roster"]], [self.a.id, self.b.id])
        self.assertEqual(data["roster"][0]["name"], "小A")  # 展示快照
        self.assertTrue(data["handoff_rules"])  # 移交规则模板已快照

    async def test_create_team_min_size(self):
        with self.assertRaises(TeamError):
            await _make_team(self.session, [self.a])

    async def test_create_team_rejects_duplicates(self):
        with self.assertRaises(TeamError):
            await _make_team(self.session, [self.a, self.a])

    async def test_create_team_leader_must_be_member(self):
        with self.assertRaises(TeamError):
            await _make_team(self.session, [self.a, self.b], leader_id=self.c.id)

    async def test_create_team_no_nesting(self):
        team = await _make_team(self.session, [self.a, self.b], name="外层")
        with self.assertRaises(TeamError):
            await _make_team(self.session, [team, self.c], name="内层")

    async def test_create_team_rejects_human_member(self):
        human = Member(name="真人", position="", is_virtual=False)
        self.session.add(human)
        await self.session.flush()
        with self.assertRaises(TeamError):
            await _make_team(self.session, [self.a, human])

    async def test_remove_member_guards(self):
        team = await _make_team(self.session, [self.a, self.b, self.c])
        await team_service.remove_team_member(self.session, team, self.c.id)
        data = parse_team_persona(team.virtual_system_prompt)
        self.assertEqual(len(data["roster"]), 2)

        with self.assertRaises(TeamError):  # 踢到 min-2 以下
            await team_service.remove_team_member(self.session, team, self.b.id)
        with self.assertRaises(TeamError):  # 负责人不可踢
            await team_service.remove_team_member(self.session, team, self.a.id)
        with self.assertRaises(TeamError):  # 不在队中
            await team_service.remove_team_member(self.session, team, self.c.id)

    async def test_add_member_snapshot_and_duplicate(self):
        team = await _make_team(self.session, [self.a, self.b])
        d = await _add_employee(self.session, "小D", "设计")
        await team_service.add_team_member(self.session, team, d.id, "负责视觉")
        data = parse_team_persona(team.virtual_system_prompt)
        entry = next(e for e in data["roster"] if e["member_id"] == d.id)
        self.assertEqual(entry["role_note"], "负责视觉")
        self.assertEqual(entry["name"], "小D")
        with self.assertRaises(TeamError):
            await team_service.add_team_member(self.session, team, d.id)

    async def test_change_leader(self):
        team = await _make_team(self.session, [self.a, self.b, self.c])
        with self.assertRaises(TeamError):  # 新负责人必须在队中
            outsider = await _add_employee(self.session, "路人")
            await team_service.change_team_leader(self.session, team, outsider.id)
        await team_service.change_team_leader(self.session, team, self.b.id)
        data = parse_team_persona(team.virtual_system_prompt)
        self.assertEqual(data["leader_id"], self.b.id)
        self.assertEqual(len(data["roster"]), 3)  # 花名册不受影响

    async def test_update_profile_preserves_roster(self):
        team = await _make_team(self.session, [self.a, self.b])
        before = parse_team_persona(team.virtual_system_prompt)
        await team_service.update_team_profile(self.session, team, identity="I2")
        after = parse_team_persona(team.virtual_system_prompt)
        self.assertEqual(after["identity"], "I2")
        self.assertEqual(after["roster"], before["roster"])
        self.assertEqual(after["leader_id"], before["leader_id"])
        self.assertEqual(after["handoff_rules"], before["handoff_rules"])

    async def test_update_roster_preserves_joined_at(self):
        team = await _make_team(self.session, [self.a, self.b])
        before = parse_team_persona(team.virtual_system_prompt)
        await team_service.update_roster(
            self.session, team, [self.a.id, self.b.id],
            {self.a.id: "新注记"})
        after = parse_team_persona(team.virtual_system_prompt)
        self.assertEqual(
            [e["joined_at"] for e in after["roster"]],
            [e["joined_at"] for e in before["roster"]])
        self.assertEqual(after["roster"][0]["role_note"], "新注记")

    async def test_disband_keeps_members(self):
        team = await _make_team(self.session, [self.a, self.b])
        team_id = team.id
        await team_service.disband_team(self.session, team)
        await self.session.flush()
        self.assertIsNone(await self.session.get(Member, team_id))
        self.assertIsNotNone(await self.session.get(Member, self.a.id))
        self.assertIsNotNone(await self.session.get(Member, self.b.id))
        self.assertEqual(await team_service.list_team_rows(self.session), [])

    async def test_resolve_hosted_team(self):
        team = await _make_team(self.session, [self.a, self.b], name="一组")
        resolved, err = await team_service.resolve_hosted_team(self.session, self.a.id)
        self.assertEqual(err, "")
        self.assertEqual(resolved.id, team.id)

        _, err = await team_service.resolve_hosted_team(self.session, self.b.id)
        self.assertIn("负责人", err)  # 普通成员不是负责人
        _, err = await team_service.resolve_hosted_team(self.session, "")
        self.assertIn("负责人", err)

        # 多团队歧义：a 同时是两个团队的负责人
        await _make_team(self.session, [self.a, self.c], name="二组")
        _, err = await team_service.resolve_hosted_team(self.session, self.a.id)
        self.assertIn("team_id", err)
        resolved, err = await team_service.resolve_hosted_team(
            self.session, self.a.id, team_id=team.id)
        self.assertEqual(err, "")
        self.assertEqual(resolved.id, team.id)

        # 指定了别人的团队
        other = (await team_service.list_team_rows(self.session))[-1]
        _, err = await team_service.resolve_hosted_team(
            self.session, self.b.id, team_id=other.id)
        self.assertIn("负责人", err)


class TeamToolsTests(unittest.IsolatedAsyncioTestCase):
    """插件工具端到端（宿主校验 + 增删成员），LLM 生成路径不在单测范围。"""

    async def test_manage_member_tool(self):
        import json

        from openvort.plugins.vortteam.tools.team_tools import TeamManageMemberTool

        engine, sf = await _make_db()
        tool = TeamManageMemberTool(lambda: sf)
        async with sf() as db:
            a = await _add_employee(db, "组长", "前端")
            b = await _add_employee(db, "小B", "后端")
            c = await _add_employee(db, "小C", "测试")
            await _make_team(db, [a, b])
            await db.commit()

        async with sf() as db:
            db.add_all([a, b, c])
            await db.flush()
            # 非负责人宿主：拒绝
            result = json.loads(await tool.execute({
                "action": "add", "member": "小C",
                "_target_member_id": c.id,
            }))
            self.assertFalse(result["ok"])
            # 负责人宿主：拉人成功
            result = json.loads(await tool.execute({
                "action": "add", "member": "小C", "role_note": "回归测试",
                "_target_member_id": a.id,
            }))
            self.assertTrue(result["ok"], result)
            # 移出普通成员成功
            result = json.loads(await tool.execute({
                "action": "remove", "member": b.id,
                "_target_member_id": a.id,
            }))
            self.assertTrue(result["ok"], result)
            # 负责人不可移除
            result = json.loads(await tool.execute({
                "action": "remove", "member": "组长",
                "_target_member_id": a.id,
            }))
            self.assertFalse(result["ok"])

        await engine.dispose()


if __name__ == "__main__":
    unittest.main()
