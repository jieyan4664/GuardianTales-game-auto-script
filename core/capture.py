"""统一截图源 —— 按配置选择 Windows 窗口截图 或 adb 截图.

背景 (MuMu 12 实测):
  - adb screencap / screenrecord / screencap -d 全部只能拿到 MuMu 桌面,
    因为游戏画面 (Unity SurfaceView) 被直接渲染到 Windows 宿主窗口,
    Android 内部的 framebuffer 里没有游戏内容.
  - 因此必须走 Windows 窗口截图 (PrintWindow), 且它不怕被终端窗口遮挡.

坐标一致性 (关键):
  实测 adb 实际帧 = 1920x1080, MuMuNxDevice 窗口客户区 = 1920x1080,
  两者一致, 所以窗口像素坐标可直接用作 adb input tap 坐标, 无需映射.
"""
from __future__ import annotations

from typing import Callable, Optional

import numpy as np

from .logger import get_logger

log = get_logger("capture")


def create_screenshot_source(settings: dict) -> Optional[Callable[[], Optional[np.ndarray]]]:
    """按 settings.capture 配置创建截图函数.

    返回:
        可调用对象 () -> ndarray | None
        None 表示配置未启用窗口截图 (调用方应回退 adb)
    """
    cfg = settings.get("capture", {}) or {}
    source = cfg.get("source", "adb")

    title = cfg.get("window_title", "MuMuNxDevice")
    roi = cfg.get("content_roi")
    # 手动指定屏幕区域 [x, y, w, h].
    # MuMu 的窗口 rect 有时是屏幕外的负坐标 (如 -31995,-31940), 完全不可靠,
    # 此时直接指定游戏画面在屏幕上的实际区域才是稳的做法.
    region = cfg.get("region")

    def _apply_roi(img):
        if img is not None and roi:
            x, y, w, h = roi
            img = img[y:y + h, x:x + w]
        return img

    # ---- WGC: 抓 GPU 合成画面 (MuMu 唯一可靠方式) ----
    if source == "wgc":
        try:
            from .window import WindowCapture
            from .wgc import WgcCapture
        except ImportError as exc:
            log.warning("WGC 模块不可用 (%s), 回退 adb", exc)
            return None

        # 捕获整个显示器时用; 为 None 则按 hwnd 捕获窗口本身
        monitor = cfg.get("monitor")
        crop_to_window = cfg.get("crop_to_window", True)

        # 定位窗口: monitor 模式下 rect 用于裁剪; hwnd 模式下用于捕获
        info = None
        try:
            # 只用静态方法查窗口, 不实例化 (避免打扰窗口 / 不需要 mss)
            wins = WindowCapture.list_windows(title, include_children=True)
            if wins:
                # 精确匹配标题优先, 避免选中外层容器窗口
                exact = [w for w in wins
                         if w.title.strip().lower() == title.strip().lower()]
                info = exact[0] if exact else max(
                    wins, key=lambda w: w.width * w.height
                )
                log.info("目标窗口: %s", info)
        except Exception as exc:
            log.warning("查询窗口失败: %s", exc)

        if info is None and monitor is None:
            log.warning("未找到窗口 '%s' 且未配 monitor, 回退 adb", title)
            return None

        try:
            if monitor is not None:
                log.info("WGC 模式: 捕获显示器 #%s", monitor)
                wgc = WgcCapture(monitor_index=monitor)
            else:
                wgc = WgcCapture(hwnd=info.hwnd)  # type: ignore[union-attr]
        except Exception as exc:
            log.warning("WGC 初始化失败 (%s), 回退 adb", exc)
            return None

        # monitor 模式: 按窗口 rect 从整屏里裁出游戏画面;
        # rect 飘到屏幕外时退到手动 region, 再不行才是整屏 (含桌面, 会干扰匹配)
        rect = None
        if monitor is not None and crop_to_window:
            if info is not None and info.rect[0] >= -500 and info.rect[1] >= -500:
                rect = info.rect
            elif region:
                x, y, w, h = region
                rect = (x, y, x + w, y + h)
                log.info("窗口 rect 失效, 改用配置的 region: %s", region)
            else:
                log.warning(
                    "窗口 rect 在屏幕外%s, 且未配 region, 将返回整屏画面 (含桌面)",
                    f" {info.rect}" if info is not None else "",
                )

        def _grab_wgc() -> Optional[np.ndarray]:
            img = wgc.grab()
            if img is not None and rect:
                x0, y0, x1, y1 = rect
                img = img[y0:y1, x0:x1]
            return _apply_roi(img)

        return _grab_wgc

    if source != "window":
        return None

    # ---- 传统窗口截图 (PrintWindow / mss) ----
    try:
        from .window import WindowCapture
    except ImportError as exc:
        log.warning("窗口截图模块不可用 (%s), 回退 adb", exc)
        return None

    try:
        cap = WindowCapture()
        info = cap.find(title)
        log.info("窗口截图已就绪: %s", info)
    except Exception as exc:
        log.warning("绑定窗口 '%s' 失败 (%s), 回退 adb", title, exc)
        return None

    def _grab() -> Optional[np.ndarray]:
        # 手动屏幕区域优先: 窗口 rect 在 MuMu 下可能是屏幕外负坐标, 不可靠
        if region:
            x, y, w, h = region
            return _apply_roi(cap.capture_region(x, y, w, h))
        # capture_auto: PrintWindow 优先 (不怕遮挡),
        # 但硬件加速窗口常返回黑屏, 会自动回退 mss 屏幕抓取
        return _apply_roi(cap.capture_auto(prefer_printwindow=True))

    return _grab
