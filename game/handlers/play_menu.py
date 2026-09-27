"""玩法选择页 Handler (点击 play 后, 带左右翻页箭头的页面).

已采集的模板:
  - play_back        左上角返回箭头 → 回主城
  - play_page_prev   最左中箭头 → 上一页
  - play_page_next   最右中箭头 → 下一页

当前 Handler 只做「识别 + 提供动作方法」, 不自动点击 ——
等明确要刷哪个玩法后, 再在这里编排「翻页找玩法 → 进入」的流程.
"""
from __future__ import annotations

from core.logger import get_logger
from game.handlers.base import Handler
from game.states import State

log = get_logger("h.play_menu")


class PlayMenuHandler(Handler):
    state = State.PLAY_MENU

    def handle(self, ctx) -> None:
        log.info("玩法选择页 (帧 #%d) —— 等待具体玩法指令", ctx.round_index)
        # TODO: 接入玩法后, 这里编排动作, 例如:
        #   1. 用 page_next / page_prev 翻到目标玩法所在页
        #   2. 点击该玩法入口 (需再采集对应入口模板)
        #   3. 进入后交给玩法 Handler 处理

    # ---- 可用动作 (供后续玩法流程调用) ----
    def go_back(self, ctx) -> bool:
        """点左上角返回箭头, 回到主城."""
        ok = self.tap_template(ctx, "play_back")
        log.info("返回主城: %s", "已点击" if ok else "未找到 play_back")
        return ok

    def page_prev(self, ctx) -> bool:
        ok = self.tap_template(ctx, "play_page_prev")
        log.info("上一页: %s", "已点击" if ok else "未找到 play_page_prev")
        return ok

    def page_next(self, ctx) -> bool:
        ok = self.tap_template(ctx, "play_page_next")
        log.info("下一页: %s", "已点击" if ok else "未找到 play_page_next")
        return ok
