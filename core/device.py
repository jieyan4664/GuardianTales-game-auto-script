"""设备层: 封装 adb 的截图 / 点击 / 滑动 / 按键 / 应用控制.

四个必须避开的坑:
1. PowerShell/cmd 下重定向 adb 输出会破坏 PNG 二进制 —— 必须用 subprocess 捕获原始 bytes
2. adb shell 不能传二进制 —— 截图必须用 exec-out
3. input tap 每次启进程, 延迟 150~500ms —— 多点必须合并到一次 shell 会话
4. Windows 下必须加 CREATE_NO_WINDOW —— 否则每次点击都闪黑色控制台
"""
from __future__ import annotations

import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Sequence

import cv2
import numpy as np

from .logger import get_logger

log = get_logger("device")

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

KEY_BACK = 4
KEY_HOME = 3
KEY_ENTER = 66
KEY_POWER = 26


@dataclass
class CaptureStat:
    frames: int = 0
    total_ms: float = 0.0
    last_ms: float = 0.0

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.frames if self.frames else 0.0


class AdbDevice:
    """通过 adb 控制 Android 设备 (MuMu 模拟器)."""

    def __init__(
        self,
        adb_path: str,
        serial: str,
        dry_run: bool = False,
        tap_jitter: int = 0,
        min_tap_interval: float = 0.05,
        timeout: float = 20.0,
        rotate: int = 0,
        screenshot_source=None,
    ) -> None:
        self.adb_path = adb_path
        self.serial = serial
        self.dry_run = dry_run
        self.tap_jitter = tap_jitter
        self.min_tap_interval = min_tap_interval
        self.timeout = timeout
        # 0 = 不旋转; 90/180/270 = 顺时针旋转
        self.rotate = int(rotate) % 360
        # 可选: 外部注入的截图函数 () -> np.ndarray | None
        # MuMu 把游戏画面渲染到 Windows 窗口, adb 只能截到桌面,
        # 因此主流程需要注入 Windows 窗口截图 (见 core/window.py)
        self.screenshot_source = screenshot_source
        self._rotate_map = {
            0: None,
            90: cv2.ROTATE_90_CLOCKWISE,
            180: cv2.ROTATE_180,
            270: cv2.ROTATE_90_COUNTERCLOCKWISE,
        }

        self.stat = CaptureStat()
        self._frame_size: tuple[int, int] | None = None
        # 旋转前的原始尺寸 (供诊断使用)
        self._raw_size: tuple[int, int] | None = None
        self._last_action_ts = 0.0
        self._exec_out_ok = True

    # ---------------- 底层 ----------------
    def _run(self, args: Sequence[str], binary: bool = False, timeout: float | None = None):
        cmd = [self.adb_path, "-s", self.serial, *args]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout or self.timeout,
            creationflags=_NO_WINDOW,
        )
        if binary:
            return proc.stdout or b""
        out = (proc.stdout or b"").decode("utf-8", "replace")
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        if proc.returncode != 0 and err:
            log.debug("adb 非零退出 [%s]: %s", " ".join(args), err)
        return out

    def shell(self, *args: str, binary: bool = False, timeout: float | None = None):
        return self._run(["shell", *args], binary=binary, timeout=timeout)

    def check_alive(self) -> bool:
        try:
            return "ok" in self.shell("echo", "ok", timeout=8.0)
        except Exception:
            return False

    # ---------------- 截图 ----------------
    def screencap(self) -> np.ndarray:
        """截取一帧, 返回 BGR ndarray (已按 rotate 配置旋转).

        若注入了 screenshot_source (如 Windows 窗口截图), 优先使用它 ——
        MuMu 下 adb 只能截到桌面, 游戏画面必须从 Windows 窗口抓.
        """
        start = time.perf_counter()

        # ---- 优先: 注入的截图源 ----
        if self.screenshot_source is not None:
            try:
                image = self.screenshot_source()
            except Exception as exc:
                log.warning("注入的截图源失败, 回退 adb: %s", exc)
                image = None
            if image is not None:
                if self._raw_size is None:
                    rh, rw = image.shape[:2]
                    self._raw_size = (rw, rh)
                if self.rotate and self.rotate in self._rotate_map:
                    image = cv2.rotate(image, self._rotate_map[self.rotate])
                cost = (time.perf_counter() - start) * 1000.0
                self.stat.frames += 1
                self.stat.total_ms += cost
                self.stat.last_ms = cost
                if self._frame_size is None:
                    h, w = image.shape[:2]
                    self._frame_size = (w, h)
                    log.info("帧分辨率: %dx%d (来源=注入)", w, h)
                return image

        # ---- 回退: adb 截图 ----
        image = None

        if self._exec_out_ok:
            data = self._run(
                ["exec-out", "screencap", "-p"], binary=True, timeout=25.0
            )
            if data:
                # 部分 adb 版本会在 PNG 前输出 4 字节长度前缀或 CRLF, 不再严检 magic,
                # 直接交给 imdecode 试错; 失败时再降级.
                image = cv2.imdecode(
                    np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR
                )
                if image is None and len(data) > 1024:
                    log.warning(
                        "exec-out 解码失败 (长度 %d), 降级为落盘模式",
                        len(data),
                    )
                    self._exec_out_ok = False
            else:
                log.warning("exec-out 返回空数据, 降级为落盘模式")
                self._exec_out_ok = False

        if image is None:
            image = self._screencap_via_file()
        if image is None:
            raise RuntimeError(
                f"截图失败 (设备 {self.serial}). 常见原因:\n"
                f"  1) 模拟器未启动 / adb 断连 —— 运行 adb devices 确认\n"
                f"  2) 序列号变了 (模拟器重启后常见, 如 5554→5556) ——\n"
                f"     把 settings.yaml 的 device.serial 设为 auto 自动探测\n"
                f"  3) 落盘模式依赖 /sdcard 可写, 若不可写也会失败"
            )

        # 记录旋转前的原始尺寸
        if self._raw_size is None:
            rh, rw = image.shape[:2]
            self._raw_size = (rw, rh)

        # 自动旋转: 让"逻辑坐标"始终是游戏的纵向布局
        if self.rotate and self.rotate in self._rotate_map:
            image = cv2.rotate(image, self._rotate_map[self.rotate])

        cost = (time.perf_counter() - start) * 1000.0
        self.stat.frames += 1
        self.stat.total_ms += cost
        self.stat.last_ms = cost

        if self._frame_size is None:
            h, w = image.shape[:2]
            self._frame_size = (w, h)
            log.info(
                "实际帧分辨率: %dx%d (rotate=%d, 单帧耗时 %.0fms)",
                w, h, self.rotate, cost,
            )
        return image

    def _screencap_via_file(self, remote: str = "/sdcard/_bot_shot.png"):
        """降级方案: 先落盘再 cat 回来 (比 pull 少一次进程开销)."""
        self.shell("screencap", "-p", remote, timeout=30.0)
        data = self._run(["exec-out", "cat", remote], binary=True, timeout=30.0)
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)

    @property
    def frame_size(self) -> tuple[int, int]:
        """实际帧尺寸 (旋转后的真实值)."""
        if self._frame_size is None:
            self.screencap()
        return self._frame_size or (0, 0)

    # ---------------- 多 display / SurfaceView 绕过 ----------------
    def list_displays(self) -> list[int]:
        """列出设备上所有 display 的 id.

        Unity 游戏 (SurfaceView) 有时渲染在非 0 号 display 上,
        而 screencap 默认只截 display 0 —— 这会导致截到桌面而非游戏.
        """
        out = self.shell("dumpsys", "display", timeout=15.0)
        ids: set[int] = set()
        for line in out.splitlines():
            m = re.search(r"Display\s+(\d+)\s*[:\]]", line)
            if m:
                ids.add(int(m.group(1)))
        if not ids:
            out2 = self.shell("cmd", "display", "list", timeout=10.0)
            for line in out2.splitlines():
                m = re.search(r"Display\s+(\d+)", line)
                if m:
                    ids.add(int(m.group(1)))
        return sorted(ids) if ids else [0]

    def screencap_help(self) -> str:
        """读取 screencap 帮助信息 (确认是否支持 -d display 参数)."""
        return self.shell("screencap", "-h", timeout=10.0)

    def screencap_display(self, display_id: int = 0) -> np.ndarray | None:
        """截取指定 display 的画面 (用于绕过 SurfaceView 捕获问题)."""
        data = self._run(
            ["exec-out", "screencap", "-d", str(display_id), "-p"],
            binary=True, timeout=25.0,
        )
        if data:
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                return img
        # 降级: 落盘 + cat
        remote = f"/sdcard/_bot_shot_d{display_id}.png"
        self.shell("screencap", "-d", str(display_id), "-p", remote, timeout=30.0)
        data = self._run(["exec-out", "cat", remote], binary=True, timeout=30.0)
        if data:
            return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        return None

    def screencap_via_record(self, duration: int = 1, tmp_dir: str = ".") -> np.ndarray | None:
        """通过 screenrecord 录屏再取首帧 —— 能捕获 SurfaceView / Unity 游戏画面.

        screencap 只抓 UI layer, 遇到 SurfaceView (Unity/视频/地图) 会截到桌面或黑屏;
        screenrecord 抓的是最终合成输出, 包含 SurfaceView 内容.

        注意: 延迟较高 (录制+pull+解码 约 2~4s/帧), 只适合低频探测或一次性采模板.
        """
        import tempfile
        import os

        remote = "/sdcard/_bot_record.mp4"
        # 先清掉旧文件
        self.shell("rm", "-f", remote, timeout=10.0)
        # 录制 (screenrecord 最长 180s, 这里只录 1~3s)
        self.shell(
            "screenrecord", "--time-limit", str(duration), remote,
            timeout=duration * 2 + 20.0,
        )
        # 拉到本地临时文件
        fd, local = tempfile.mkstemp(suffix=".mp4", dir=tmp_dir)
        os.close(fd)
        try:
            proc = subprocess.run(
                [self.adb_path, "-s", self.serial, "pull", remote, local],
                capture_output=True, timeout=60.0,
                creationflags=_NO_WINDOW,
            )
            if proc.returncode != 0:
                log.warning("录屏 pull 失败")
                return None
            cap = cv2.VideoCapture(local)
            ok, frame = cap.read()
            cap.release()
            if not ok or frame is None:
                return None
            return frame
        finally:
            try:
                os.unlink(local)
            except OSError:
                pass

    # ---------------- 输入 ----------------
    def _throttle(self) -> None:
        delta = time.perf_counter() - self._last_action_ts
        if delta < self.min_tap_interval:
            time.sleep(self.min_tap_interval - delta)
        self._last_action_ts = time.perf_counter()

    def _jitter(self, x: int, y: int) -> tuple[int, int]:
        if self.tap_jitter <= 0:
            return int(x), int(y)
        return (
            int(x) + random.randint(-self.tap_jitter, self.tap_jitter),
            int(y) + random.randint(-self.tap_jitter, self.tap_jitter),
        )

    def tap(self, x: int, y: int) -> None:
        x, y = self._jitter(x, y)
        if self.dry_run:
            log.info("[DRY-RUN] tap(%d, %d)", x, y)
            return
        self._throttle()
        self.shell("input", "tap", str(x), str(y))

    def tap_seq(self, points: Sequence[tuple[int, int]], interval: float = 0.25) -> None:
        """性能优化: 多个点击合并进一次 shell 会话.

        单点点击 300ms → 10 点连续点击从 3s 降到 ~0.4s.
        """
        if not points:
            return
        pts = [self._jitter(x, y) for x, y in points]
        if self.dry_run:
            log.info("[DRY-RUN] tap_seq(%s)", pts)
            return
        script = "; ".join(
            f"input tap {x} {y}; sleep {interval:.2f}" for x, y in pts
        )
        self.shell(script, timeout=10.0 + interval * len(pts))

    def swipe(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 300,
    ) -> None:
        if self.dry_run:
            log.info(
                "[DRY-RUN] swipe(%d,%d → %d,%d, %dms)",
                x1, y1, x2, y2, duration_ms,
            )
            return
        self._throttle()
        self.shell(
            "input", "swipe",
            str(int(x1)), str(int(y1)),
            str(int(x2)), str(int(y2)),
            str(int(duration_ms)),
        )

    def long_press(self, x: int, y: int, duration_ms: int = 1500) -> None:
        """起点终点相同 + 时长 = 长按."""
        self.swipe(x, y, x, y, duration_ms)

    def key(self, code: int) -> None:
        if self.dry_run:
            log.info("[DRY-RUN] keyevent(%d)", code)
            return
        self._throttle()
        self.shell("input", "keyevent", str(code))

    # ---------------- 应用 ----------------
    def resolve_launch_activity(self, package: str) -> str | None:
        """查找包的 LAUNCHER Activity 完整组件名 (pkg/.ActName 或 pkg/ActName).

        优先于 monkey 启动方式 —— monkey 对 Unity / 加固游戏不稳定.
        """
        out = self.shell(
            "cmd", "package", "resolve-activity", "--brief",
            "-c", "android.intent.category.LAUNCHER", package,
            timeout=10.0,
        )
        for line in out.splitlines():
            line = line.strip()
            if "/" in line and not line.startswith("#"):
                return line
        return None

    def launch_app(self, package: str, activity: str | None = None) -> str:
        """启动应用 (返回 am start 的原始输出, 便于诊断失败原因).

        - activity 不为空: 直接 am start -n pkg/activity (推荐, 适合 Unity / 加固游戏)
        - activity 为空: 先用 resolve_launch_activity 查主 Activity, 再 am start
        - 都失败: 退到 monkey 兜底
        """
        if self.dry_run:
            log.info("[DRY-RUN] launch(%s, %s)", package, activity)
            return ""

        if not activity:
            activity = self.resolve_launch_activity(package)

        if activity:
            # 支持 ".MainActivity" 简写: 自动补成 "pkg/.MainActivity"
            if "/" not in activity:
                activity = f"{package}/{activity}"
            log.info("am start -n %s", activity)
            out = self.shell("am", "start", "-n", activity, timeout=15.0)
            return out

        log.warning("未找到主 Activity, 退到 monkey 兜底")
        return self.shell(
            "monkey", "-p", package,
            "-c", "android.intent.category.LAUNCHER", "1",
        )

    def force_stop(self, package: str) -> None:
        self.shell("am", "force-stop", package)

    def list_installed(self, keyword: str = "", third_party_only: bool = False) -> list[str]:
        """列出已安装的应用包名.

        - keyword 非空时按子串过滤 (大小写不敏感)
        - third_party_only=True 时用 `pm list packages -3` 只看第三方应用
        """
        args = ["pm", "list", "packages"]
        if third_party_only:
            args.append("-3")
        out = self.shell(*args, timeout=15.0)
        pkgs: list[str] = []
        needle = keyword.lower()
        for line in out.splitlines():
            if not line.startswith("package:"):
                continue
            pkg = line[len("package:"):].strip()
            if not needle or needle in pkg.lower():
                pkgs.append(pkg)
        return pkgs

    def current_package(self) -> str:
        """获取当前前台包名. 优先用 ResumedActivity (Android 7+), 兜底 mCurrentFocus."""
        # ResumedActivity 格式: "ResumedActivity: ActivityRecord{... pkg/act}"
        out = self.shell("dumpsys", "activity", "activities")
        match = re.search(r"ResumedActivity:\s*.*?(\S+)/[\w.]+", out)
        if match:
            return match.group(1)
        # 兜底: mCurrentFocus=Window{... pkg/act}
        out = self.shell("dumpsys", "window", "windows")
        match = re.search(r"mCurrentFocus=.*?([\w.]+)/", out)
        return match.group(1) if match else ""

    def current_focus_raw(self) -> dict[str, str]:
        """诊断用: 返回 dumpsys 关键字段的原始值, 便于排查焦点问题."""
        info: dict[str, str] = {}
        out = self.shell("dumpsys", "activity", "activities")
        for line in out.splitlines():
            line = line.strip()
            if line.startswith(("ResumedActivity:", "mResumedActivity:")):
                info["resumed"] = line
            elif line.startswith("mFocusedApp:"):
                info["focused_app"] = line
        out2 = self.shell("dumpsys", "window", "windows")
        for line in out2.splitlines():
            line = line.strip()
            if line.startswith("mCurrentFocus="):
                info["current_focus"] = line
            elif line.startswith("mFocusedWindow="):
                info["focused_window"] = line
        return info

    def running_activities(self, package: str) -> list[str]:
        """列出指定包当前在 Activity 栈里的 Activity (后台运行也算).

        用于定位游戏主城界面的真实 Activity —— 游戏进程的当前界面往往和
        LAUNCHER 入口 Activity 不同 (入口可能是权限页/闪屏页).

        只保留 ActivityRecord / Hist 行, 过滤掉 baseDir / cmp= / service 等噪音.
        """
        out = self.shell("dumpsys", "activity", "activities")
        found: list[str] = []
        seen: set[str] = set()
        for line in out.splitlines():
            if package not in line:
                continue
            # 只要 ActivityRecord / Hist 行, 排除属性行和服务行
            if "ActivityRecord" not in line and "Hist" not in line:
                continue
            if any(bad in line for bad in ("baseDir", "dataDir", "cmp=", "mActivityComponent")):
                continue
            m = re.search(r"(\S+)/([\w.$]+)", line)
            if m:
                comp = f"{m.group(1)}/{m.group(2)}"
                pkg_part = comp.split("/", 1)[0]
                if pkg_part == package and comp not in seen:
                    seen.add(comp)
                    found.append(comp)
        return found

    def is_running(self, package: str) -> bool:
        """判断包的进程是否存在 (无论前台还是后台)."""
        out = self.shell("ps", "-A")
        return package in out

    def close(self) -> None:
        log.info(
            "截图统计: %d 帧, 平均 %.0fms/帧",
            self.stat.frames, self.stat.avg_ms,
        )