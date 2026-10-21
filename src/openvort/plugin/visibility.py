"""
按员工启用插件 — 可见性计算与绑定读写

补上插件可见性的第四维"宿主成员"（前三维：全局开关 / 用户权限 / 渠道）。
一次 agent 运行若以某 AI 员工身份进行（web 员工私聊、IM 员工 bot），该员工就是
宿主 H；可见插件集按下面规则算，工具 schema 与插件 prompt 段同源过滤：

    对每个全局 enabled 的插件 P（vis = config_data.visibility 覆盖 ?? 插件类默认）：
      core=True            → 恒可见（系统依赖，不可收窄）
      vis == "members"     → 仅当存在 (P, H, allow) 绑定；无宿主一律不可见
      vis == "all"         → 默认可见，除非存在 (P, H, deny) 绑定

全局 enabled=false 是上界：任何绑定都放行不了已下线插件。
没有任何绑定数据的员工 = 现状行为（全局启用集减去 members-only 插件）。
"""

import json
import time

from sqlalchemy import delete, select

from openvort.db.models import PluginConfig, PluginMemberBinding
from openvort.utils.logging import get_logger

log = get_logger("plugin.visibility")

VISIBILITY_ALL = "all"
VISIBILITY_MEMBERS = "members"
MODE_ALLOW = "allow"
MODE_DENY = "deny"
SOURCE_ADMIN = "admin"
SOURCE_AUTO = "auto"

# 由团队功能自动为负责人维护 allow 绑定的插件（见 team_service）
TEAM_MANAGED_PLUGINS: tuple[str, ...] = ("vortteam",)

_CACHE_TTL_SECONDS = 30.0
_cache: dict[str, tuple[float, frozenset[str]]] = {}


def resolve_visibility(plugin, config_data: dict | None = None) -> str:
    """生效可见性：plugin_configs.config_data.visibility 覆盖 > 插件类属性默认 > all。"""
    if isinstance(config_data, dict):
        override = config_data.get("visibility")
        if override in (VISIBILITY_ALL, VISIBILITY_MEMBERS):
            return override
    value = getattr(plugin, "visibility", VISIBILITY_ALL) or VISIBILITY_ALL
    return value if value in (VISIBILITY_ALL, VISIBILITY_MEMBERS) else VISIBILITY_ALL


async def load_config_overrides(db) -> dict[str, dict]:
    rows = (await db.execute(select(PluginConfig.plugin_name, PluginConfig.config_data))).all()
    result: dict[str, dict] = {}
    for name, data in rows:
        try:
            parsed = json.loads(data or "{}")
        except (ValueError, TypeError):
            parsed = {}
        result[name] = parsed if isinstance(parsed, dict) else {}
    return result


async def load_member_binding_modes(db, member_id: str) -> dict[str, str]:
    rows = (await db.execute(
        select(PluginMemberBinding.plugin_name, PluginMemberBinding.mode)
        .where(PluginMemberBinding.member_id == member_id)
    )).all()
    return {name: mode for name, mode in rows}


async def compute_visible_plugins(registry, member_id: str | None, db=None) -> frozenset[str]:
    """不走缓存的生效集计算；db 为空时自行开会话。"""
    if db is None:
        from openvort.db.engine import get_session_factory
        sf = get_session_factory()
        if sf is None:
            # 数据库未就绪（极早期启动）：退化为现状行为
            return frozenset(
                p.name for p in registry.list_plugins()
                if not registry.is_plugin_disabled(p.name)
                and (p.core or resolve_visibility(p) == VISIBILITY_ALL)
            )
        async with sf() as session:
            return await compute_visible_plugins(registry, member_id, db=session)

    overrides = await load_config_overrides(db)
    modes = await load_member_binding_modes(db, member_id) if member_id else {}

    visible: set[str] = set()
    for plugin in registry.list_plugins():
        name = plugin.name
        if registry.is_plugin_disabled(name):
            continue
        if plugin.core:
            visible.add(name)
            continue
        vis = resolve_visibility(plugin, overrides.get(name))
        if vis == VISIBILITY_MEMBERS:
            if member_id and modes.get(name) == MODE_ALLOW:
                visible.add(name)
        else:
            if not (member_id and modes.get(name) == MODE_DENY):
                visible.add(name)
    return frozenset(visible)


async def get_visible_plugins(registry, member_id: str | None, db=None) -> frozenset[str]:
    """带 TTL 缓存的生效集；管理操作后调 clear_visibility_cache 立即失效。"""
    key = member_id or ""
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]
    result = await compute_visible_plugins(registry, member_id, db=db)
    _cache[key] = (now, result)
    return result


def clear_visibility_cache(member_id: str | None = None) -> None:
    """member_id 为 None 时清空全部（插件启停、可见性配置变更等全局事件）。"""
    if member_id is None:
        _cache.clear()
    else:
        _cache.pop(member_id, None)
        _cache.pop("", None)


# ---- 绑定读写 ----


async def get_member_bindings(db, member_id: str) -> list[dict]:
    rows = (await db.execute(
        select(PluginMemberBinding).where(PluginMemberBinding.member_id == member_id)
        .order_by(PluginMemberBinding.plugin_name)
    )).scalars().all()
    return [{
        "plugin_name": r.plugin_name, "mode": r.mode, "source": r.source,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    } for r in rows]


async def set_member_binding(db, plugin_name: str, member_id: str,
                             mode: str | None, source: str = SOURCE_ADMIN) -> None:
    """mode 为 None 表示删除绑定行（回到默认）。调用方负责 commit。"""
    if mode is not None and mode not in (MODE_ALLOW, MODE_DENY):
        raise ValueError(f"非法的绑定模式: {mode}")
    existing = (await db.execute(
        select(PluginMemberBinding).where(
            PluginMemberBinding.plugin_name == plugin_name,
            PluginMemberBinding.member_id == member_id,
        )
    )).scalar_one_or_none()
    if mode is None:
        if existing:
            await db.delete(existing)
    elif existing:
        existing.mode = mode
        existing.source = source
    else:
        db.add(PluginMemberBinding(
            plugin_name=plugin_name, member_id=member_id, mode=mode, source=source,
        ))
    await db.flush()
    clear_visibility_cache(member_id)


async def ensure_auto_allow_bindings(db, member_id: str,
                                     plugin_names: tuple[str, ...] = TEAM_MANAGED_PLUGINS) -> None:
    """为宿主写入 source=auto 的 allow 行（已有 admin 行不覆盖其来源）。"""
    for name in plugin_names:
        existing = (await db.execute(
            select(PluginMemberBinding).where(
                PluginMemberBinding.plugin_name == name,
                PluginMemberBinding.member_id == member_id,
            )
        )).scalar_one_or_none()
        if existing:
            if existing.mode != MODE_ALLOW:
                existing.mode = MODE_ALLOW
                existing.source = SOURCE_AUTO
            continue
        db.add(PluginMemberBinding(
            plugin_name=name, member_id=member_id, mode=MODE_ALLOW, source=SOURCE_AUTO,
        ))
    await db.flush()
    clear_visibility_cache(member_id)


async def release_auto_allow_bindings(db, member_id: str,
                                      plugin_names: tuple[str, ...] = TEAM_MANAGED_PLUGINS) -> None:
    """只删 source=auto 的 allow 行；管理员手配的 admin 行保留。"""
    await db.execute(
        delete(PluginMemberBinding).where(
            PluginMemberBinding.member_id == member_id,
            PluginMemberBinding.plugin_name.in_(plugin_names),
            PluginMemberBinding.source == SOURCE_AUTO,
        )
    )
    await db.flush()
    clear_visibility_cache(member_id)
