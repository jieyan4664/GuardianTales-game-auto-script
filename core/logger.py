"""日志与调试证据留存.

Windows 终端默认不解释 ANSI 转义序列, 通过 ctypes 打开 VT 模式后才能输出彩色日志.
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
DEBUG_DIR = LOG_DIR / "debug"
LOG_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

_COLORS = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[37;41m",
}
_RESET = "\033[0m"


def _ansi_supported() -> bool:
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


_USE_COLOR = _ansi_supported()


class _ColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if _USE_COLOR:
            color = _COLORS.get(record.levelname, "")
            if color:
                text = f"{color}{text}{_RESET}"
        return text


_INITIALIZED: set[str] = set()


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """按子模块名返回一个全局唯一 logger, 同时挂上彩色控制台与日志文件 handler."""
    logger = logging.getLogger(f"gamebot.{name}")
    if name in _INITIALIZED:
        return logger

    logger.setLevel(level)
    logger.propagate = False

    console_fmt = _ColorFormatter(
        "%(asctime)s [%(levelname).1s] %(name)-12s | %(message)s", "%H:%M:%S"
    )
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(console_fmt)
    logger.addHandler(console)

    file_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s | %(message)s"
    )
    file_handler = logging.FileHandler(
        LOG_DIR / f"run_{time.strftime('%Y%m%d')}.log", encoding="utf-8"
    )
    file_handler.setFormatter(file_fmt)
    logger.addHandler(file_handler)

    _INITIALIZED.add(name)
    return logger


def save_debug_frame(image: np.ndarray, tag: str = "frame") -> Path:
    """识别失败时保存证据帧, 用于事后分析模板匹配分数 / 调参."""
    path = DEBUG_DIR / f"{time.strftime('%H%M%S')}_{tag}.png"
    ok, buf = cv2.imencode(".png", image)
    if ok:
        path.write_bytes(buf.tobytes())
    return path