"""菜单面板 Handler (点击主城 menu 后展开的面板).

已采集的模板 (均为面板内的功能入口):
  - book        图鉴 / 书籍
  - attendance  签到 / 出勤
  - quest       任务
  - party       队伍 / 编队
  - enhance     强化
  - inventory   背包 / 仓库
  - sns         社交 / 社区
  - friend      好友

当前只做「识别 + 提供动作方法」, 不自动点击.
"""
from __future__ import annotations

from core.logger import get_logger
from game.handlers.base import Handler
from game.states import State

log = get_logger("h.menu_panel")

# 面板内的功能入口模板名
ENTRIES = (
    "book",
    "attendance",
    "quest",
    "party",
    "enhance",
    "inventory",
    "sns",
    "friend",
)


class MenuPanelHandler(Handler):
    state = State.MENU

    def handle(self, ctx) -> None:
        log.info("菜单面板已展开 (帧 #%d) —— 等待具体功能指令", ctx.round_index)
        # TODO: 需要哪个功能就调用 open_entry(ctx, "xxx")

    # ---- 可用动作 ----
    def open_entry(self, ctx, name: str) -> bool:
        """点击面板内的某个功能入口."""
        if name not in ENTRIES:
            log.warning("未知入口: %s (已采集: %s)", name, ", ".join(ENTRIES))
            return False
        ok = self.tap_template(ctx, name)
        log.info("打开 [%s]: %s", name, "已点击" if ok else f"未找到 {name}")
        return ok
