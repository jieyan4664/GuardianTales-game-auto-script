"""主界面 Handler (实际使用, 非示例).

作用:
- 确认「识别 → 点击」链路可用
- dry_run=true 时只打印 tap 坐标, 不会真的点击, 可放心验证坐标准确性

后续接玩法时, 在这里按流程编排动作即可.
"""
from __future__ import annotations

from core.logger import get_logger
from game.handlers.base import Handler
from game.states import State

log = get_logger("h.main")


class MainCityHandler(Handler):
    state = State.MAIN

    def handle(self, ctx) -> None:
        log.info("主城: 已确认在主界面 (帧 #%d)", ctx.round_index)

        # 演示点击: 用已采集的 [菜单] 按钮验证坐标是否正确.
        # dry_run 下只会打印 tap(x, y), 确认坐标落在按钮上后再加 --live 真点.
        if self.tap_template(ctx, "menu"):
            log.info("已发出点击: [menu]")
        else:
            log.warning("本帧未找到 [menu] 模板")
