"""Handler 基类: 提供常用便捷方法."""
from __future__ import annotations

import time

from core.logger import get_logger
from game.engine import Context
from game.states import State

log = get_logger("h.base")


class Handler:
    state: State = State.UNKNOWN

    def handle(self, ctx: Context) -> None:
        raise NotImplementedError

    # ---- 便捷方法 ----
    def tap_template(
        self,
        ctx: Context,
        name: str,
        threshold: float | None = None,
        wait: float | None = None,
    ) -> bool:
        """在当前帧中查找模板并点击中心."""
        hit = ctx.matcher.find(ctx.frame.image, name, threshold)
        if not hit:
            return False
        ctx.device.tap(*hit.center)
        time.sleep(
            wait if wait is not None
            else ctx.settings["action"]["default_wait"]
        )
        return True