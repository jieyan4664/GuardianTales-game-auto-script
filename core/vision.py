"""视觉层: 模板匹配 (含多尺度)、ROI 搜索、颜色统计、匹配结果封装.

核心注意事项:
- 模板图用 cv2.imdecode (np.fromfile) 而非 cv2.imread: 后者在 Windows 下无法处理非 ASCII 路径
- 带 alpha 通道的模板: 用 TM_CCORR_NORMED + mask (TM_CCOEFF_NORMED 不支持 mask)
- ROI 坐标还原: 子图匹配结果要加上 ROI 偏移, 否则点击位置全错
- 多尺度: 模板按缩放因子预缓存, 避免每帧重复 resize
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import cv2
import numpy as np

from .logger import get_logger

log = get_logger("vision")

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "assets" / "templates"

_METHODS = {
    "ccoeff": cv2.TM_CCOEFF_NORMED,
    "ccorr": cv2.TM_CCORR_NORMED,
    "sqdiff": cv2.TM_SQDIFF_NORMED,
}


@dataclass
class MatchResult:
    name: str
    score: float
    x: int
    y: int
    w: int
    h: int
    scale: float = 1.0
    cost_ms: float = 0.0

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2

    @property
    def box(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.w, self.h

    def __bool__(self) -> bool:
        return self.score > 0

    def __str__(self) -> str:
        cx, cy = self.center
        return f"{self.name} score={self.score:.3f} center=({cx},{cy}) scale={self.scale:.2f}"


class Template:
    """模板图: 预加载 + 多尺度缓存 + 支持透明通道掩码."""

    def __init__(self, path: Path, name: str | None = None) -> None:
        raw = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if raw is None:
            raise FileNotFoundError(f"模板图读取失败: {path}")

        if raw.ndim == 3 and raw.shape[2] == 4:
            self.alpha: np.ndarray | None = raw[:, :, 3]
            self.image = raw[:, :, :3]
        else:
            self.alpha = None
            self.image = raw if raw.ndim == 3 else cv2.cvtColor(raw, cv2.COLOR_GRAY2BGR)

        self.name = name or path.stem
        self.path = path
        self._scaled: dict[float, np.ndarray] = {}

    def at(self, scale: float) -> np.ndarray:
        if scale not in self._scaled:
            if abs(scale - 1.0) < 1e-3:
                self._scaled[scale] = self.image
            else:
                self._scaled[scale] = cv2.resize(
                    self.image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA
                )
        return self._scaled[scale]

    @property
    def size(self) -> tuple[int, int]:
        return self.image.shape[1], self.image.shape[0]


def _match_once(
    image: np.ndarray,
    tpl: np.ndarray,
    method: int,
    mask: np.ndarray | None = None,
) -> tuple[float, tuple[int, int]]:
    """执行一次 matchTemplate, 返回 (相似度, 左上角坐标)."""
    if image.shape[0] < tpl.shape[0] or image.shape[1] < tpl.shape[1]:
        return -1.0, (-1, -1)

    if mask is not None:
        # ⚠ OpenCV 限制: 带 mask 时只支持 TM_SQDIFF 和 TM_CCORR_NORMED
        method = cv2.TM_CCORR_NORMED

    result = cv2.matchTemplate(image, tpl, method, mask=mask)

    if method == cv2.TM_SQDIFF_NORMED:
        min_val, _, min_loc, _ = cv2.minMaxLoc(result)
        return 1.0 - float(min_val), min_loc

    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    return float(max_val), max_loc


def crop_roi(
    image: np.ndarray, roi: Sequence[int] | None
) -> tuple[np.ndarray, int, int]:
    """按 [x, y, w, h] 裁剪, 返回 (子图, x 偏移, y 偏移).

    ⚠ 偏移量必须加回去 —— 这一步漏了会导致点击位置全部错位.
    """
    if roi is None:
        return image, 0, 0
    x, y, w, h = (int(v) for v in roi)
    return image[y:y + h, x:x + w], x, y


class Matcher:
    """模板匹配器: 支持多尺度、阈值过滤、ROI 限定."""

    def __init__(
        self,
        templates_dir: Path = TEMPLATE_DIR,
        threshold: float = 0.85,
        scales: Iterable[float] = (1.0,),
        method: str = "ccoeff",
        roi: Sequence[int] | None = None,
    ) -> None:
        self.dir = Path(templates_dir)
        self.threshold = threshold
        self.scales = tuple(scales)
        self.method_name = method
        self.default_roi = roi
        self._registry: dict[str, Template] = {}
        self.last_cost_ms = 0.0

    # ---- 模板加载 ----
    def get(self, name: str) -> Template:
        if name in self._registry:
            return self._registry[name]
        path = self.dir / name
        if not path.is_file():
            path = self.dir / f"{name}.png"
        if not path.is_file():
            hits = list(self.dir.rglob(f"{name}*"))
            if not hits:
                raise FileNotFoundError(
                    f"找不到模板: {name} (搜索目录 {self.dir})"
                )
            path = hits[0]
        self._registry[name] = Template(path, name=name)
        return self._registry[name]

    def reload(self) -> None:
        """调试时热重载模板, 不用重启脚本."""
        self._registry.clear()

    # ---- 匹配 ----
    def find(
        self,
        image: np.ndarray,
        name: str,
        threshold: float | None = None,
        roi: Sequence[int] | None = None,
        scales: Iterable[float] | None = None,
        method: str | None = None,
    ) -> MatchResult | None:
        start = time.perf_counter()
        thr = self.threshold if threshold is None else threshold
        method_id = _METHODS.get(method or self.method_name, cv2.TM_CCOEFF_NORMED)
        tpl = self.get(name)
        sub, off_x, off_y = crop_roi(
            image, self.default_roi if roi is None else roi
        )

        best: MatchResult | None = None
        for scale in (scales or self.scales):
            tpl_img = tpl.at(scale)
            score, loc = _match_once(sub, tpl_img, method_id, tpl.alpha)
            h, w = tpl_img.shape[:2]
            candidate = MatchResult(
                name, score, off_x + loc[0], off_y + loc[1], w, h, scale
            )
            if best is None or candidate.score > best.score:
                best = candidate
            if score >= thr:
                best.cost_ms = (time.perf_counter() - start) * 1000.0
                self.last_cost_ms = best.cost_ms
                return best

        self.last_cost_ms = (time.perf_counter() - start) * 1000.0
        return None

    def exists(self, image, name, threshold: float | None = None,
               **kwargs) -> bool:
        """模板是否出现在画面中.

        threshold 显式成位置参数: 否则调用方按位置传 threshold 时,
        只会落进 **kwargs 而报 "takes N positional arguments but M were given".
        """
        return self.find(image, name, threshold=threshold, **kwargs) is not None

    def score(
        self, image: np.ndarray, name: str, **kwargs
    ) -> float:
        """返回当前画面下模板的最高相似度 (无论是否达阈值) —— 调阈值时就靠它."""
        start = time.perf_counter()
        method_id = _METHODS.get(
            kwargs.pop("method", self.method_name), cv2.TM_CCOEFF_NORMED
        )
        roi = kwargs.pop("roi", None)
        scales = kwargs.pop("scales", None)
        tpl = self.get(name)
        sub, _, _ = crop_roi(image, self.default_roi if roi is None else roi)
        peak = -1.0
        for scale in (scales or self.scales):
            s, _ = _match_once(sub, tpl.at(scale), method_id, tpl.alpha)
            peak = max(peak, s)
        self.last_cost_ms = (time.perf_counter() - start) * 1000.0
        return peak

    def find_best(
        self, image: np.ndarray, names: Sequence[str], **kwargs
    ) -> MatchResult | None:
        """在多个模板中取置信度最高的一个, 用于界面分类."""
        best: MatchResult | None = None
        for n in names:
            hit = self.find(image, n, **kwargs)
            if hit and (best is None or hit.score > best.score):
                best = hit
        return best

    def find_all_of(
        self,
        image: np.ndarray,
        name: str,
        threshold: float | None = None,
        roi: Sequence[int] | None = None,
        max_results: int = 20,
    ) -> list[MatchResult]:
        """找出所有匹配位置 (非极大值抑制).

        适用场景: 列表内多个相同按钮 (例如 "领取" 按钮出现在多个奖励条目中).
        """
        thr = self.threshold if threshold is None else threshold
        tpl = self.get(name)
        sub, off_x, off_y = crop_roi(image, roi)
        result = cv2.matchTemplate(sub, tpl.image, cv2.TM_CCOEFF_NORMED)

        hits: list[MatchResult] = []
        h, w = tpl.image.shape[:2]
        work = result.copy()
        for _ in range(max_results):
            _, max_val, _, max_loc = cv2.minMaxLoc(work)
            if max_val < thr:
                break
            x, y = max_loc
            hits.append(
                MatchResult(name, float(max_val), off_x + x, off_y + y, w, h)
            )
            x0, y0 = max(0, x - w // 2), max(0, y - h // 2)
            work[y0:y + h // 2 + 1, x0:x + w // 2 + 1] = -1.0
        return hits


class Color:
    """颜色工具: 判断像素颜色 / 统计 ROI 内颜色占比.

    适用场景: 体力条是否满、血条剩余量、按钮是否高亮 (亮/灰).
    """

    @staticmethod
    def rgb_at(image: np.ndarray, x: int, y: int) -> tuple[int, int, int]:
        b, g, r = image[int(y), int(x)][:3]
        return int(r), int(g), int(b)

    @staticmethod
    def near(
        image: np.ndarray,
        x: int,
        y: int,
        rgb: Sequence[int],
        tol: int = 25,
    ) -> bool:
        r, g, b = Color.rgb_at(image, x, y)
        return (
            abs(r - rgb[0]) <= tol
            and abs(g - rgb[1]) <= tol
            and abs(b - rgb[2]) <= tol
        )

    @staticmethod
    def ratio_in_roi(
        image: np.ndarray,
        roi: Sequence[int],
        rgb: Sequence[int],
        tol: int = 30,
    ) -> float:
        """ROI 内接近目标色的像素占比, 范围 0.0~1.0."""
        x, y, w, h = (int(v) for v in roi)
        patch = image[y:y + h, x:x + w]
        if patch.size == 0:
            return 0.0
        target = np.array([rgb[2], rgb[1], rgb[0]], dtype=np.int16)
        diff = np.abs(patch.astype(np.int16) - target)
        mask = np.all(diff <= tol, axis=2)
        return float(mask.mean())

    @staticmethod
    def bar_fill_ratio(
        image: np.ndarray,
        roi: Sequence[int],
        rgb: Sequence[int],
        tol: int = 40,
    ) -> float:
        """进度条填充比例: 沿水平方向找到颜色连续区段的终点."""
        x, y, w, h = (int(v) for v in roi)
        patch = image[y:y + h, x:x + w]
        if patch.size == 0:
            return 0.0
        target = np.array([rgb[2], rgb[1], rgb[0]], dtype=np.int16)
        diff = np.abs(patch.astype(np.int16) - target)
        col_mask = np.all(diff <= tol, axis=2).mean(axis=0) > 0.5
        filled = 0
        for flag in col_mask:
            if flag:
                filled += 1
            else:
                break
        return filled / w if w else 0.0