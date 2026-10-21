"""按员工启用插件 — 可见性矩阵 / registry 过滤 / 团队绑定联动回归（内存 SQLite）。"""

import unittest

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import openvort.auth.models  # noqa: F401
import openvort.contacts.models  # noqa: F401
import openvort.db.models  # noqa: F401
from openvort.contacts.models import Member
from openvort.core.engine import team_service
from openvort.db.engine import Base
from openvort.db.models import PluginConfig, PluginMemberBinding
from openvort.plugin import visibility
from openvort.plugin.base import BasePlugin, BaseTool
from openvort.plugin.registry import PluginRegistry


class _Tool(BaseTool):
    def __init__(self, name: str):
        self.name = name
        self.description = f"tool {name}"

    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params: dict) -> str:
        return "{}"


class _Plugin(BasePlugin):
    def __init__(self, name: str, core: bool = False, vis: str = "all"):
        self.name = name
        self.display_name = name
        self.core = core
        self.visibility = vis

    def get_tools(self):
        return [_Tool(f"{self.name}.do")]


class _Registry(PluginRegistry):
    """直接控制启用集的测试替身（绕过 disable/enable 对 tools 的副作用）"""

    def __init__(self, plugins, disabled=()):
        super().__init__()
        self._test_plugins = plugins
        self._test_disabled = set(disabled)

    def list_plugins(self):
        return list(self._test_plugins)

    def is_plugin_disabled(self, name):
        return name in self._test_disabled


async def _make_db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


CORE, ALLP, MEMBP = "corep", "allp", "membersp"


def _registry(disabled=()):
    return _Registry(
        [_Plugin(CORE, core=True), _Plugin(ALLP), _Plugin(MEMBP, vis="members")],
        disabled=disabled,
    )


class VisibilityMatrixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        visibility.clear_visibility_cache()
        self.engine, self.sf = await _make_db()

    async def asyncTearDown(self):
        await self.engine.dispose()
        visibility.clear_visibility_cache()

    async def test_no_host_excludes_members_only(self):
        async with self.sf() as db:
            visible = await visibility.compute_visible_plugins(_registry(), None, db=db)
        self.assertEqual(visible, frozenset({CORE, ALLP}))

    async def test_unbound_host_same_as_no_host(self):
        async with self.sf() as db:
            visible = await visibility.compute_visible_plugins(_registry(), "h1", db=db)
        self.assertEqual(visible, frozenset({CORE, ALLP}))

    async def test_allow_binding_grants_members_plugin(self):
        async with self.sf() as db:
            await visibility.set_member_binding(db, MEMBP, "h1", visibility.MODE_ALLOW)
            visible = await visibility.compute_visible_plugins(_registry(), "h1", db=db)
            # 其他宿主不受影响
            other = await visibility.compute_visible_plugins(_registry(), "h2", db=db)
        self.assertEqual(visible, frozenset({CORE, ALLP, MEMBP}))
        self.assertEqual(other, frozenset({CORE, ALLP}))

    async def test_deny_binding_narrows_all_plugin(self):
        async with self.sf() as db:
            await visibility.set_member_binding(db, ALLP, "h1", visibility.MODE_DENY)
            visible = await visibility.compute_visible_plugins(_registry(), "h1", db=db)
        self.assertEqual(visible, frozenset({CORE}))

    async def test_core_immune_to_deny(self):
        async with self.sf() as db:
            await visibility.set_member_binding(db, CORE, "h1", visibility.MODE_DENY)
            visible = await visibility.compute_visible_plugins(_registry(), "h1", db=db)
        self.assertIn(CORE, visible)

    async def test_global_disable_is_upper_bound(self):
        async with self.sf() as db:
            await visibility.set_member_binding(db, ALLP, "h1", visibility.MODE_ALLOW)
            visible = await visibility.compute_visible_plugins(
                _registry(disabled=(ALLP,)), "h1", db=db)
        self.assertNotIn(ALLP, visible)

    async def test_config_override_beats_plugin_attr(self):
        async with self.sf() as db:
            db.add(PluginConfig(plugin_name=MEMBP, config_data='{"visibility": "all"}'))
            await db.flush()
            visible = await visibility.compute_visible_plugins(_registry(), None, db=db)
        self.assertIn(MEMBP, visible)  # members-only 插件被配置覆盖为全员可见


class RegistryFilterTests(unittest.TestCase):
    def setUp(self):
        self.registry = PluginRegistry()
        self.registry.register_tool(_Tool("p1.do"), plugin_name="p1")
        self.registry.register_tool(_Tool("p2.do"), plugin_name="p2")
        self.registry.register_tool(_Tool("loose.do"))  # 非插件工具
        self.registry.register_prompt("P1 知识", source="plugin:p1")
        self.registry.register_prompt("P2 知识", source="plugin:p2")
        self.registry.register_prompt("技能知识", source="skill:demo")

    def test_tools_filtered_by_plugin(self):
        names = {t["name"] for t in self.registry.to_claude_tools(allowed_plugins=frozenset({"p1"}))}
        self.assertEqual(names, {"p1.do", "loose.do"})  # 未归属工具不受过滤

    def test_none_means_unfiltered(self):
        names = {t["name"] for t in self.registry.to_claude_tools()}
        self.assertEqual(names, {"p1.do", "p2.do", "loose.do"})

    def test_prompt_sections_filtered_but_skill_kept(self):
        text = self.registry.get_system_prompt_extension(allowed_plugins=frozenset({"p1"}))
        self.assertIn("P1 知识", text)
        self.assertIn("技能知识", text)
        self.assertNotIn("P2 知识", text)


class TeamBindingLinkageTests(unittest.IsolatedAsyncioTestCase):
    """建团/换负责人/解散 自动维护 vortteam 的 allow 绑定。"""

    async def asyncSetUp(self):
        visibility.clear_visibility_cache()
        self.engine, self.sf = await _make_db()

    async def asyncTearDown(self):
        await self.engine.dispose()
        visibility.clear_visibility_cache()

    async def _employee(self, db, name):
        m = Member(name=name, position="工程师", is_virtual=True,
                   virtual_system_prompt="", skills="[]")
        db.add(m)
        await db.flush()
        return m

    async def _bindings(self, db, member_id):
        rows = (await db.execute(
            select(PluginMemberBinding).where(PluginMemberBinding.member_id == member_id)
        )).scalars().all()
        return {r.plugin_name: (r.mode, r.source) for r in rows}

    async def test_lifecycle(self):
        async with self.sf() as db:
            a = await self._employee(db, "组长")
            b = await self._employee(db, "成员B")
            c = await self._employee(db, "成员C")

            team = await team_service.create_team(
                db, "一组", "", "I", "P", "B", [a.id, b.id], {})
            self.assertEqual(
                await self._bindings(db, a.id),
                {"vortteam": (visibility.MODE_ALLOW, visibility.SOURCE_AUTO)})
            self.assertEqual(await self._bindings(db, b.id), {})  # 普通成员不绑

            # 换负责人：旧释放、新绑定
            await team_service.change_team_leader(db, team, b.id)
            self.assertEqual(await self._bindings(db, a.id), {})
            self.assertEqual(
                await self._bindings(db, b.id),
                {"vortteam": (visibility.MODE_ALLOW, visibility.SOURCE_AUTO)})

            # 解散：负责人释放
            await team_service.disband_team(db, team)
            self.assertEqual(await self._bindings(db, b.id), {})

    async def test_multi_team_leader_keeps_binding_until_last(self):
        async with self.sf() as db:
            a = await self._employee(db, "双组长")
            b = await self._employee(db, "B")
            c = await self._employee(db, "C")
            t1 = await team_service.create_team(db, "一队", "", "I", "P", "B", [a.id, b.id], {})
            t2 = await team_service.create_team(db, "二队", "", "I", "P", "B", [a.id, c.id], {})

            await team_service.disband_team(db, t1)
            self.assertIn("vortteam", await self._bindings(db, a.id))  # 还领导二队，保留

            await team_service.disband_team(db, t2)
            self.assertEqual(await self._bindings(db, a.id), {})  # 全部卸任才释放

    async def test_reconcile_backfills_and_cleans(self):
        async with self.sf() as db:
            a = await self._employee(db, "存量组长")
            b = await self._employee(db, "B")
            # 模拟"联动上线前"的存量团队：建团后删掉绑定行
            team = await team_service.create_team(db, "存量队", "", "I", "P", "B", [a.id, b.id], {})
            await db.execute(PluginMemberBinding.__table__.delete())
            await db.commit()

            leaders, removed = await team_service.reconcile_leader_plugin_bindings(db)
            self.assertEqual(leaders, 1)
            self.assertIn("vortteam", await self._bindings(db, a.id))  # 补写

            # 卸任残留：手工给非负责人塞一条 auto 行，对账应清掉
            db.add(PluginMemberBinding(
                plugin_name="vortteam", member_id=b.id,
                mode="allow", source=visibility.SOURCE_AUTO))
            await db.commit()
            leaders, removed = await team_service.reconcile_leader_plugin_bindings(db)
            self.assertEqual(removed, 1)
            self.assertEqual(await self._bindings(db, b.id), {})

    async def test_admin_binding_survives_release(self):
        async with self.sf() as db:
            a = await self._employee(db, "组长")
            b = await self._employee(db, "B")
            team = await team_service.create_team(db, "一队", "", "I", "P", "B", [a.id, b.id], {})
            # 管理员手改成 admin 来源
            await visibility.set_member_binding(
                db, "vortteam", a.id, visibility.MODE_ALLOW, source=visibility.SOURCE_ADMIN)
            await team_service.disband_team(db, team)
            # release 只删 auto 行，admin 行保留
            self.assertEqual(
                await self._bindings(db, a.id),
                {"vortteam": (visibility.MODE_ALLOW, visibility.SOURCE_ADMIN)})


if __name__ == "__main__":
    unittest.main()
