"""Windows Graphics Capture (WGC) 截图 —— 能抓到 GPU 合成的游戏画面.

为什么需要它:
    MuMu 的游戏画面走 GPU overlay 合成, 传统方式全部失效:
      - adb screencap / screenrecord : 只拿到桌面 (画面根本不进 Android framebuffer)
      - PrintWindow                  : 尺寸对但全黑 (抓不到硬件加速层)
      - mss (GDI BitBlt)             : 全黑 (老 API 抓不到 overlay)
    WGC 是 Windows 10+ 的现代捕获 API, 和 Win+Shift+S 走同一套合成路径,
    因此能拿到完整游戏画面, 且不需要改动 MuMu 的渲染模式 (不影响游戏性能).

实现要点:
    windows-capture 是回调式 API (on_frame_arrived), 而脚本需要同步取帧.
    这里用「线程里 start + queue 等一帧 + capture_control.stop()」包装成同步.
"""
from __future__ import annotations

import queue
import threading
from typing import Optional

import numpy as np

try:
    from windows_capture import WindowsCapture, Frame, InternalCaptureControl
    _WGC_AVAILABLE = True
except ImportError:
    _WGC_AVAILABLE = False
    WindowsCapture = None  # type: ignore
    Frame = None  # type: ignore
    InternalCaptureControl = None  # type: ignore


class WgcCaptureError(RuntimeError):
    pass


class WgcCapture:
    """按窗口句柄 (或标题) 用 WGC 抓取画面."""

    def __init__(self, hwnd: Optional[int] = None, window_name: Optional[str] = None,
                 monitor_index: Optional[int] = None):
        if not _WGC_AVAILABLE:
            raise WgcCaptureError(
                "缺少 windows-capture, 请执行: pip install windows-capture"
            )
        if hwnd is None and not window_name and monitor_index is None:
            raise ValueError("需要指定 hwnd / window_name / monitor_index 之一")
        self.hwnd = hwnd
        self.window_name = window_name
        # 捕获整个显示器 —— 游戏画面不在常规窗口里时 (虚拟坐标窗口) 用它,
        # 等价于 Win+Shift+S 截屏幕, 能拿到 DWM 合成后的画面
        self.monitor_index = monitor_index

    def grab(self, timeout: float = 8.0) -> Optional[np.ndarray]:
        """同步抓取一帧 BGR 图像. 超时返回 None."""
        result: queue.Queue = queue.Queue(maxsize=1)

        kwargs: dict = {"cursor_capture": False, "draw_border": False}
        if self.monitor_index is not None:
            kwargs["monitor_index"] = int(self.monitor_index)
        elif self.hwnd is not None:
            kwargs["window_hwnd"] = int(self.hwnd)
        else:
            kwargs["window_name"] = self.window_name

        try:
            capture = WindowsCapture(**kwargs)
        except Exception as exc:
            raise WgcCaptureError(f"创建 WGC 会话失败: {exc}") from exc

        @capture.event  # type: ignore
        def on_frame_arrived(frame, capture_control):
            try:
                # frame_buffer 是 BGRA, 转 BGR 后取出前 3 通道
                buf = frame.convert_to_bgr().frame_buffer
                result.put_nowait(np.ascontiguousarray(buf))
            except Exception:
                pass
            capture_control.stop()

        @capture.event  # type: ignore
        def on_closed():
            pass

        worker = threading.Thread(target=capture.start, daemon=True)
        worker.start()
        try:
            return result.get(timeout=timeout)
        except Exception:
            return None
        finally:
            # 线程是 daemon, 不阻塞退出
            pass
