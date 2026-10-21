"""
vortteam 插件 — 团队负责人在对话中管理团队成员

工具全局注册，执行时校验宿主：当前对话的 AI 员工（_target_member_id）
必须是某团队负责人，否则拒绝。移交规则基准模板在 assets/ 下（不放
prompts/，避免被注册进全局 system prompt），由 team_service 在建团时
快照进团队行的人设 JSON。
"""

from pathlib import Path

from openvort.plugin.base import BasePlugin, BaseTool
from openvort.utils.logging import get_logger

log = get_logger("plugins.vortteam")


class VortTeamPlugin(BasePlugin):
    """AI 团队员工管理插件"""

    name = "vortteam"
    display_name = "AI 团队员工管理"
    description = "团队负责人在对话中管理团队成员：新建 AI 员工并入队、拉入现有员工、移除冗余成员"
    version = "0.1.0"
    # 仅团队负责人（有 allow 绑定的 AI 员工宿主）可见；绑定由 team_service 建团/换负责人时自动维护
    visibility = "members"

    def get_tools(self) -> list[BaseTool]:
        from openvort.db.engine import get_session_factory
        from openvort.plugins.vortteam.tools.team_tools import (
            TeamCreateEmployeeTool, TeamManageMemberTool,
        )

        sf_getter = get_session_factory
        return [
            TeamCreateEmployeeTool(sf_getter),
            TeamManageMemberTool(sf_getter),
        ]

    def get_prompts(self) -> list[str]:
        prompts_dir = Path(__file__).parent / "prompts"
        prompts = []
        for f in sorted(prompts_dir.glob("*.md")):
            try:
                prompts.append(f.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning(f"读取 prompt 文件 {f.name} 失败: {e}")
        return prompts

    def validate_credentials(self) -> bool:
        return True
