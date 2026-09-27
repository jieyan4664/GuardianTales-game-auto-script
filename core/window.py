"""Windows 端窗口截图 —— 绕过 MuMu 的 adb 截图限制.

为什么需要这个模块?
MuMu 12 把游戏画面 (Unity/SurfaceView) 直接渲染到 Windows 宿主窗口,
Android 系统内部的 framebuffer 里只有桌面, 因此:
  - adb screencap        → 桌面
  - adb screencap -d N   → 该版本不支持 -d
  - adb screenrecord     → 桌面
直接从 Windows 端截 MuMu 窗口的内容, 是唯一能拿到游戏画面的可靠方式,
而且延迟比 adb 低得多 (约 10~40ms/帧).

实现要点:
- EnumWindows 遍历顶层窗口, 按标题关键词匹配 MuMu
- GetClientRect + ClientToScreen 取客户区 (自动排除标题栏和边框)
- mss 做屏幕区域抓取 (比 PIL ImageGrab 快, 支持多显示器)
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Sequence

import numpy as np

_IS_WIN = sys.platform == "win32"

if _IS_WIN:
    try:
        import ctypes
        import win32gui
        import win32con
        import win32ui
    except ImportError:
        ctypes = None
        win32gui = None
        win32con = None
        win32ui = None
else:
    ctypes = None
    win32gui = None
    win32con = None
    win32ui = None

try:
    import mss
except ImportError:
    mss = None


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    rect: tuple[int, int, int, int]      # 客户区 (left, top, right, bottom)
    window_rect: tuple[int, int, int, int]  # 整个窗口 (含边框/标题栏)

    @property
    def width(self) -> int:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]

    def __str__(self) -> str:
        return (
            f"hwnd={self.hwnd} '{self.title[:40]}' "
            f"客户区 {self.width}x{self.height} @ ({self.rect[0]},{self.rect[1]})"
        )


class WindowCaptureError(RuntimeError):
    pass


def dpi_info() -> dict:
    """读取系统 DPI 与缩放比例, 用于诊断截图坐标错位问题."""
    out: dict = {}
    if not _IS_WIN or win32gui is None:
        return out
    set_dpi_aware()
    try:
        LOGPIXELSX = 88
        # win32gui 没有 GetDeviceCaps, 需要走 gdi32
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        hdc = user32.GetDC(0)
        try:
            dpi = gdi32.GetDeviceCaps(hdc, LOGPIXELSX)
        finally:
            user32.ReleaseDC(0, hdc)
        out["dpi"] = dpi
        out["scale_percent"] = round(dpi / 96.0 * 100)
    except Exception as exc:
        out["error"] = str(exc)
    try:
        import mss as _mss
        with _mss.mss() as sct:
            out["mss_monitors"] = [
                {"left": m["left"], "top": m["top"],
                 "width": m["width"], "height": m["height"]}
                for m in sct.monitors
            ]
    except Exception as exc:
        out["mss_error"] = str(exc)
    return out


def set_dpi_aware() -> bool:
    """声明进程 DPI 感知, 使 GetWindowRect 返回真实物理像素坐标.

    不声明的话, 在 125%/150% 缩放的屏幕上:
      - GetWindowRect / GetClientRect 返回的是"逻辑坐标"(被系统缩放过)
      - 而 mss 抓取用的是"物理像素坐标"
    两者不一致会截错区域 —— 典型症状就是截出来全黑.

    必须在任何窗口/GDI 操作之前调用 (每个进程只能生效一次).
    """
    if not _IS_WIN or ctypes is None:
        return False
    try:
        # Windows 8.1+: PER_MONITOR_DPI_AWARE (最准确)
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return True
    except Exception:
        pass
    try:
        # Windows Vista+: 系统级 DPI aware (兼容老系统)
        ctypes.windll.user32.SetProcessDPIAware()
        return True
    except Exception:
        pass
    return False


class WindowCapture:
    """截取指定 Windows 窗口的客户区画面."""

    def __init__(self) -> None:
        if not _IS_WIN:
            raise WindowCaptureError("窗口截图仅在 Windows 上可用")
        # 必须在任何 GetWindowRect 之前声明, 否则抓到的坐标是被缩放过的
        set_dpi_aware()
        if win32gui is None:
            raise WindowCaptureError(
                "缺少 pywin32, 请执行: pip install pywin32"
            )
        if mss is None:
            raise WindowCaptureError("缺少 mss, 请执行: pip install mss")
        self._sct = mss.mss()
        self._hwnd: int | None = None
        self.info: WindowInfo | None = None

    # ---------------- 窗口查找 ----------------
    @staticmethod
    def _info_of(hwnd: int) -> WindowInfo | None:
        """构造单个窗口的信息. 失败返回 None."""
        try:
            title = win32gui.GetWindowText(hwnd)
            cl, ct, cr, cb = win32gui.GetClientRect(hwnd)
            if cr - cl <= 0 or cb - ct <= 0:
                return None
            sx, sy = win32gui.ClientToScreen(hwnd, (0, 0))
            return WindowInfo(
                hwnd, title,
                (sx, sy, sx + (cr - cl), sy + (cb - ct)),
                win32gui.GetWindowRect(hwnd),
            )
        except Exception:
            return None

    @staticmethod
    def list_windows(keyword: str | None = None, include_children: bool = False) -> list[WindowInfo]:
        """枚举顶层窗口. keyword 非空时按标题子串过滤.

        include_children=True 时同时枚举每个顶层窗口的子窗口 ——
        MuMu 的游戏渲染窗口常常是主窗口的子窗口, 不在顶层窗口列表里.
        """
        if win32gui is None:
            return []
        set_dpi_aware()
        found: list[WindowInfo] = []
        seen: set[int] = set()

        def _add(hwnd: int, require_visible: bool) -> None:
            if hwnd in seen:
                return
            if require_visible and not win32gui.IsWindowVisible(hwnd):
                return
            info = WindowCapture._info_of(hwnd)
            if info is None:
                return
            if keyword and keyword.lower() not in info.title.lower():
                return
            seen.add(hwnd)
            found.append(info)

        def _cb(hwnd: int, _):
            _add(hwnd, require_visible=True)
            if include_children:
                def _child_cb(chwnd: int, __):
                    _add(chwnd, require_visible=False)
                try:
                    win32gui.EnumChildWindows(hwnd, _child_cb, None)
                except Exception:
                    pass

        win32gui.EnumWindows(_cb, None)
        return found

    @staticmethod
    def list_children(hwnd: int) -> list[WindowInfo]:
        """列出指定窗口的所有子窗口."""
        if win32gui is None:
            return []
        out: list[WindowInfo] = []

        def _cb(chwnd: int, _):
            info = WindowCapture._info_of(chwnd)
            if info is not None:
                out.append(info)

        try:
            win32gui.EnumChildWindows(hwnd, _cb, None)
        except Exception:
            pass
        return out

    def find(self, keyword: str = "MuMu", prefer_largest: bool = True,
             include_children: bool = True) -> WindowInfo:
        """按标题关键词找窗口.

        - include_children=True (默认): 同时搜索子窗口 —— MuMu 的游戏渲染窗口
          (如 'MuMuNxDevice') 是主窗口的子窗口, 只搜顶层会漏掉.
        - prefer_largest: 取客户区面积最大的 (游戏画面窗口通常最大).
        """
        wins = self.list_windows(keyword, include_children=include_children)
        if not wins and not include_children:
            wins = self.list_windows(keyword, include_children=True)
        if not wins:
            allw = self.list_windows(include_children=True)
            hint = "\n".join(f"  {w}" for w in allw[:25])
            raise WindowCaptureError(
                f"未找到标题含 '{keyword}' 的窗口.\n当前可见窗口:\n{hint}"
            )
        if prefer_largest:
            wins.sort(key=lambda w: w.width * w.height, reverse=True)
        self.info = wins[0]
        self._hwnd = wins[0].hwnd
        return wins[0]

    def bind(self, hwnd: int) -> WindowInfo:
        """直接绑定到已知窗口句柄."""
        title = win32gui.GetWindowText(hwnd)
        cl, ct, cr, cb = win32gui.GetClientRect(hwnd)
        sx, sy = win32gui.ClientToScreen(hwnd, (0, 0))
        self.info = WindowInfo(
            hwnd, title,
            (sx, sy, sx + (cr - cl), sy + (cb - ct)),
            win32gui.GetWindowRect(hwnd),
        )
        self._hwnd = hwnd
        return self.info

    # ---------------- 截图 ----------------
    def capture(self) -> np.ndarray:
        """截取窗口客户区, 返回 BGR ndarray.

        默认走 mss 屏幕区域抓取 (要求窗口可见且未被遮挡).
        """
        info = self.info
        if info is None:
            info = self.find()
        left, top, right, bottom = info.rect
        w, h = right - left, bottom - top
        if w <= 0 or h <= 0:
            raise WindowCaptureError(
                f"窗口客户区尺寸异常 {w}x{h}, 窗口可能已最小化"
            )

        monitor = {"left": left, "top": top, "width": w, "height": h}
        shot = self._sct.grab(monitor)
        # mss 返回 BGRA
        frame = np.asarray(shot, dtype=np.uint8)
        if frame.ndim == 3 and frame.shape[2] == 4:
            frame = frame[:, :, :3]
        return frame

    def _printwindow_raw(self, flag: int) -> np.ndarray | None:
        """执行一次 PrintWindow (指定 flag) 并返回图像. 不判断是否黑屏."""
        if win32gui is None or win32ui is None:
            return None
        hwnd = self._hwnd
        if hwnd is None:
            return None
        bitmap = save_dc = mfc_dc = hwnd_dc = None
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            w, h = right - left, bottom - top
            if w <= 0 or h <= 0:
                return None

            hwnd_dc = win32gui.GetWindowDC(hwnd)
            if not hwnd_dc:
                return None
            mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
            save_dc = mfc_dc.CreateCompatibleDC()
            bitmap = win32ui.CreateBitmap()
            bitmap.CreateCompatibleBitmap(mfc_dc, w, h)
            save_dc.SelectObject(bitmap)

            try:
                ok = ctypes.windll.user32.PrintWindow(
                    hwnd, save_dc.GetSafeHdc(), flag
                )
            except Exception:
                ok = 0
            if not ok:
                return None

            bits = bitmap.GetBitmapBits(True)
            img = np.frombuffer(bits, dtype=np.uint8).reshape((h, w, 4))
            # BGRA → BGR
            return np.ascontiguousarray(img[:, :, :3])
        except Exception:
            return None
        finally:
            # 释放 GDI 对象, 否则长时间运行会耗尽句柄
            for _fn in (
                lambda: bitmap is not None and win32gui.DeleteObject(bitmap.GetHandle()),
                lambda: save_dc is not None and save_dc.DeleteDC(),
                lambda: mfc_dc is not None and mfc_dc.DeleteDC(),
                lambda: hwnd_dc and win32gui.ReleaseDC(hwnd, hwnd_dc),
            ):
                try:
                    _fn()
                except Exception:
                    pass

    def force_redraw(self) -> None:
        """强制窗口重绘.

        PrintWindow 返回黑屏的一个已知诱因是窗口当前没有有效重绘内容,
        RedrawWindow(RDW_INVALIDATE|RDW_UPDATENOW) 可让窗口重新提交一帧.
        """
        if ctypes is None or self._hwnd is None:
            return
        try:
            RDW_INVALIDATE = 0x0001
            RDW_UPDATENOW = 0x0100
            RDW_ALLCHILDREN = 0x0080
            RDW_FRAME = 0x0400
            ctypes.windll.user32.RedrawWindow(
                self._hwnd, None, None,
                RDW_INVALIDATE | RDW_UPDATENOW | RDW_ALLCHILDREN | RDW_FRAME,
            )
        except Exception:
            pass

    def capture_printwindow(self) -> np.ndarray | None:
        """用 PrintWindow 截取窗口 —— 优势是不依赖窗口可见性.

        依次尝试: flag=2 (PW_RENDERFULLCONTENT) / flag=0 (普通);
        若都返回黑屏, 会先强制重绘再重试一轮. 都不行返回 None.
        """
        for flag in (0x00000002, 0x00000000):
            img = self._printwindow_raw(flag)
            if img is not None and img.size > 0 and not is_black_frame(img):
                return img
        # 全黑: 强制重绘后再试一轮
        self.force_redraw()
        for flag in (0x00000002, 0x00000000):
            img = self._printwindow_raw(flag)
            if img is not None and img.size > 0 and not is_black_frame(img):
                return img
        return None

    def try_all_printwindow(self) -> dict[str, dict]:
        """诊断用: 逐一测试 PrintWindow 变体, 找出能出图的那种.

        返回 {方式: {"ok": 是否拿到图, "black": 是否全黑, "shape": (高, 宽)}}
        """
        def _desc(img) -> dict:
            if img is None or img.size == 0:
                return {"ok": False, "black": None, "shape": None}
            return {
                "ok": True,
                "black": bool(is_black_frame(img)),
                "shape": (img.shape[0], img.shape[1]),
            }

        out: dict[str, dict] = {}
        out["flag2_plain"] = _desc(self._printwindow_raw(0x00000002))
        out["flag0_plain"] = _desc(self._printwindow_raw(0x00000000))

        self.force_redraw()
        out["flag2_after_redraw"] = _desc(self._printwindow_raw(0x00000002))
        out["flag0_after_redraw"] = _desc(self._printwindow_raw(0x00000000))

        # 连续多次: 判断是否"首次失败但后续成功"
        for i in range(3):
            out[f"flag2_retry{i + 1}"] = _desc(self._printwindow_raw(0x00000002))
        return out

    def bring_to_front(self) -> bool:
        """把窗口提到最前 (最小化的话先还原).

        mss 屏幕抓取要求窗口可见且不被遮挡 —— 终端窗口常常盖住 MuMu,
        所以抓屏前先把它置顶, 抓完用户可自行切回终端.
        """
        if win32gui is None or self._hwnd is None:
            return False
        try:
            if win32gui.IsIconic(self._hwnd):
                win32gui.ShowWindow(self._hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(self._hwnd)
            return True
        except Exception:
            return False

    def capture_auto(self, prefer_printwindow: bool = True,
                     bring_front: bool = False) -> np.ndarray | None:
        """自动选择截图方式.

        1. 先试 PrintWindow (不怕被终端遮挡)
        2. 若结果是黑屏 (硬件加速窗口常见), 回退 mss 屏幕抓取
        3. 都失败返回 None

        ⚠ bring_front 默认关闭 —— 实测对 MuMu 调用 SetForegroundWindow 后,
        它会把渲染窗口重排到虚拟负坐标 (如 -31995,-31940), 导致 GetWindowRect
        返回的 rect 失效, mss 反而截到屏幕外变全黑.
        所以默认直接用 bind/find 时拿到的 rect, 不去打扰窗口.
        """
        if prefer_printwindow:
            img = self.capture_printwindow()
            if img is not None and img.size > 0 and not is_black_frame(img):
                return img
            if img is not None:
                print("[i] PrintWindow 返回黑屏, 回退 mss 屏幕抓取 (会先把窗口置顶)")
        if bring_front:
            self.bring_to_front()

        # MuMu 有时把渲染窗口放到屏幕外虚拟坐标, mss 会截到黑屏 —— 明确提示
        info = self.info
        if info is not None and (info.rect[0] < -500 or info.rect[1] < -500):
            print(
                f"[!] 窗口 rect 在屏幕外 ({info.rect[0]},{info.rect[1]}), mss 必然截到黑屏.\n"
                f"    MuMu 把渲染窗口放到了虚拟坐标. 解决办法:\n"
                f"      1) 拖动 MuMu 窗口让它重新回到屏幕内, 或重启 MuMu\n"
                f"      2) 或在 settings.yaml 配 capture.region 手动指定游戏画面区域"
            )
        try:
            return self.capture()
        except Exception:
            return None

    def capture_with_retry(self, keyword: str = "MuMu") -> np.ndarray:
        """每次截图前重新定位窗口 (窗口移动/缩放后仍能跟上)."""
        self.find(keyword)
        return self.capture()

    def capture_fullscreen(self, monitor_index: int = 1) -> np.ndarray:
        """截取整个显示器 (monitor_index=0 为所有显示器合并, 1..n 为单个显示器).

        用于排查: 当窗口枚举找不到渲染窗口时, 先全屏截一张确认游戏是否可见.
        """
        mons = self._sct.monitors
        idx = monitor_index if 0 <= monitor_index < len(mons) else len(mons) - 1
        shot = self._sct.grab(mons[idx])
        frame = np.asarray(shot, dtype=np.uint8)
        if frame.ndim == 3 and frame.shape[2] == 4:
            frame = frame[:, :, :3]
        return frame

    def capture_region(self, left: int, top: int, width: int, height: int) -> np.ndarray:
        """截取屏幕指定区域 (左上角坐标 + 宽高)."""
        monitor = {"left": left, "top": top, "width": width, "height": height}
        shot = self._sct.grab(monitor)
        frame = np.asarray(shot, dtype=np.uint8)
        if frame.ndim == 3 and frame.shape[2] == 4:
            frame = frame[:, :, :3]
        return frame

    def close(self) -> None:
        try:
            self._sct.close()
        except Exception:
            pass


def find_mumu_candidates(keywords: Sequence[str] = ("MuMu", "坎特伯雷", "snake")) -> list[WindowInfo]:
    """按多个关键词依次尝试, 返回所有命中的候选窗口."""
    seen: dict[int, WindowInfo] = {}
    for kw in keywords:
        try:
            for w in WindowCapture.list_windows(kw):
                seen.setdefault(w.hwnd, w)
        except Exception:
            continue
    return sorted(seen.values(), key=lambda w: w.width * w.height, reverse=True)


def is_black_frame(frame: np.ndarray, mean_threshold: float = 12.0,
                   std_threshold: float = 5.0) -> bool:
    """判断画面是否为黑屏 (既很暗 且 几乎没有内容变化).

    PrintWindow 抓取硬件加速 (DirectX/OpenGL) 窗口时常返回全黑图像 ——
    尺寸是对的, 但内容全黑. 这时必须回退到 mss 屏幕抓取.

    必须用 AND: 纯色亮画面 (如加载中的灰底) std 也接近 0,
    若用 OR 会被误判成黑屏而错误回退.
    """
    if frame is None or frame.size == 0:
        return True
    gray = frame[:, :, 0] if frame.ndim == 3 else frame
    gray = gray.astype(np.float32)
    return bool(gray.mean() < mean_threshold and gray.std() < std_threshold)


def detect_content_roi(frame: np.ndarray, std_threshold: float = 3.0) -> tuple[int, int, int, int] | None:
    """检测画面中真正有内容的区域 [x, y, w, h] (去掉纯色黑边).

    竖屏游戏显示在横屏窗口时左右常有黑边, 直接对整帧做模板匹配会受黑边干扰.
    这里用逐行/逐列的像素标准差判断哪里是"有内容的".

    返回 None 表示整帧都有内容 (没有明显黑边).
    """
    if frame is None or frame.size == 0:
        return None
    gray = frame[:, :, 0] if frame.ndim == 3 else frame
    gray = gray.astype(np.float32)

    col_std = gray.std(axis=0)   # 每一列的标准差
    row_std = gray.std(axis=1)   # 每一行的标准差

    col_active = np.where(col_std > std_threshold)[0]
    row_active = np.where(row_std > std_threshold)[0]

    if col_active.size == 0 or row_active.size == 0:
        return None

    x0, x1 = int(col_active[0]), int(col_active[-1]) + 1
    y0, y1 = int(row_active[0]), int(row_active[-1]) + 1
    w, h = x1 - x0, y1 - y0

    # 如果几乎占满整帧, 认为没有黑边
    fh, fw = gray.shape[:2]
    if w >= fw * 0.98 and h >= fh * 0.98:
        return None
    return (x0, y0, w, h)
