"""未知界面兜底恢复.

当连续 N 轮都识别不到任何状态时, 触发:
- 按返回键退出可能的弹窗 / 子界面
- 点击屏幕中央下方区域, 试图关掉遮罩
"""
from __future__ import annotations

import time

from core.device import KEY_BACK
from core.logger import get_logger

log = get_logger("h.unknown")


class UnknownHandler:
    def __init__(self, on_recover=None) -> None:
        self.on_recover = on_recover

    def handle(self, ctx) -> None:
        log.warning("触发兜底恢复流程")
        w, h = ctx.device.frame_size
        ctx.device.key(KEY_BACK)
        time.sleep(0.6)
        # 点两下屏幕中央偏下, 关掉可能的遮罩层 / 公告
        ctx.device.tap(w // 2, int(h * 0.85))
        time.sleep(0.4)
        ctx.device.tap(w // 2, int(h * 0.85))
        log.info("兜底恢复完成, 下一轮重新识别")

    def __call__(self, ctx) -> None:
        self.handle(ctx)