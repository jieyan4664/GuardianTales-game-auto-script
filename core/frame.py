"""帧对象: 一轮决策内复用同一张截图, 并缓存 ROI 裁剪结果.

为什么不每识别一次就截一次图?
- 单帧截图 150~400ms, 一次循环内若识别 5 个模板就 1.5s+, 太慢
- 而且不同时刻的截图状态不一致, 会导致逻辑异常 (例如同一秒出现两个不同状态)
"""
from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np


class Frame:
    __slots__ = ("image", "index", "ts", "_rois", "_size")

    def __init__(self, image: np.ndarray, index: int = 0) -> None:
        self.image = image
        self.index = index
        self.ts = time.time()
        self._rois: dict[tuple[int, int, int, int], np.ndarray] = {}
        h, w = image.shape[:2]
        self._size = (w, h)

    @property
    def width(self) -> int:
        return self._size[0]

    @property
    def height(self) -> int:
        return self._size[1]

    @property
    def size(self) -> tuple[int, int]:
        return self._size

    def roi(self, box: tuple[int, int, int, int] | None) -> np.ndarray:
        """按 [x, y, w, h] 裁剪, 结果缓存复用."""
        if box is None:
            return self.image
        key = tuple(int(v) for v in box)
        cached = self._rois.get(key)
        if cached is None:
            x, y, w, h = key
            cached = self.image[y:y + h, x:x + w]
            self._rois[key] = cached
        return cached

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        ok, buf = cv2.imencode(".png", self.image)
        if ok:
            path.write_bytes(buf.tobytes())
        return path