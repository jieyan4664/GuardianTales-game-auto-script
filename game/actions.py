"""原子动作库: 把业务意图翻译成设备操作.

全部以「当前帧 + 模板名」驱动, 避免业务层写死坐标.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from core.device import KEY_BACK, AdbDevice
from core.frame import Frame
from core.logger import get_logger
from core.vision import Matcher

log = get_logger("actions")


@dataclass
class TapOutcome:
    success: bool
    center: tuple[int, int] | None = None
    score: float = 0.0


class Actions:
    """基于图像识别的原子动作集."""

    def __init__(self, device: AdbDevice, matcher: Matcher) -> None:
        self.device = device
        self.matcher = matcher
        self.default_wait = 0.6

    def tap(
        self,
        frame: Frame,
        template: str,
        threshold: float | None = None,
        wait: float | None = None,
        retries: int = 1,
        retry_interval: float = 0.4,
    ) -> TapOutcome:
        """在当前帧中查找模板并点击中心. 失败时可重试 (重试用新截图)."""
        for attempt in range(retries):
            img = frame.image if attempt == 0 else self.device.screencap()
            hit = self.matcher.find(img, template, threshold)
            if hit:
                self.device.tap(*hit.center)
                time.sleep(wait if wait is not None else self.default_wait)
                return TapOutcome(True, hit.center, hit.score)
            if attempt < retries - 1:
                time.sleep(retry_interval)
        return TapOutcome(False)

    def tap_point(self, x: int, y: int, wait: float | None = None) -> None:
        """直接点击坐标, 跳过识别 (适用于确认已知的位置)."""
        self.device.tap(x, y)
        time.sleep(wait if wait is not None else self.default_wait)

    def swipe(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 300,
        wait: float | None = None,
    ) -> None:
        self.device.swipe(x1, y1, x2, y2, duration_ms)
        time.sleep(wait if wait is not None else self.default_wait)

    def back(self, wait: float = 0.5) -> None:
        self.device.key(KEY_BACK)
        time.sleep(wait)

    def exists(
        self,
        frame: Frame,
        template: str,
        threshold: float | None = None,
    ) -> bool:
        return self.matcher.exists(frame.image, template, threshold)

    def wait_for(
        self,
        template: str,
        timeout: float = 10.0,
        interval: float = 0.5,
        threshold: float | None = None,
    ) -> bool:
        """轮询直到模板出现或超时."""
        end = time.time() + timeout
        while time.time() < end:
            img = self.device.screencap()
            if self.matcher.exists(img, template, threshold):
                return True
            time.sleep(interval)
        return False

    def wait_until_gone(
        self,
        template: str,
        timeout: float = 10.0,
        interval: float = 0.5,
    ) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            img = self.device.screencap()
            if not self.matcher.exists(img, template):
                return True
            time.sleep(interval)
        return False