"""示例处理器: 主界面 Handler.

说明:
- 演示 Handler 的写法 (覆盖在抽象基类上)
- 演示「先处理弹窗覆盖层 → 再做主流程」的通用模式
- 实际玩法根据你提供的截图与流程再具体实现
"""
from __future__ import annotations

from core.logger import get_logger
from game.handlers.base import Handler
from game.states import State

log = get_logger("h.main")


class MainHandler(Handler):
    """主界面处理器示例. 正式接入玩法前可保留作为参考, 也可以删除."""

    state = State.MAIN

    # 覆盖层模板名: 弹窗 / 公告 / 新手引导 / 体力不足等
    OVERLAY_TEMPLATES = (
        "ui/popup_confirm.png",
        "ui/popup_notice_close.png",
        "ui/stamina_empty_ok.png",
        "ui/network_error_retry.png",
    )

    def handle(self, ctx) -> None:
        frame = ctx.frame

        # 第一优先: 关弹窗 / 公告
        for popup in self.OVERLAY_TEMPLATES:
            if self.tap_template(ctx, popup):
                log.info("关闭覆盖层: %s", popup)
                return

        # 第二优先: 体力条 / 资源条识别 (示例)
        stamina = ctx.matcher.find(
            frame.image,
            "ui/stamina_bar.png",
            roi=[100, 60, 400, 60],
        )
        if stamina:
            log.debug("体力条识别: %s", stamina)
            # TODO: 读取体力数值 → 判断是否足够 → 选择继续或停止

        # 第三优先: 主流程动作 (待接入玩法)
        log.debug("主界面待处理, 帧 #%d", ctx.round_index)