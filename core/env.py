"""环境探测: 自动定位 adb.exe / MuMu 安装目录 / ADB 端口.

为什么自研一个探测层?
- 系统 PATH 里的 adb 版本若与 MuMu 内置不一致, `adb devices` 会无法列出模拟器
- 默认端口 16384 在端口被占用时会按官方规则偏移 (+1, +32)
- MuMu 安装路径在 Program Files / D:\\Sofware 等多个位置
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from .logger import get_logger

log = get_logger("env")

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# 模拟器安装目录内 adb.exe 的相对路径 (按优先级)
# - 雷电 (LDPlayer): adb.exe 就在安装根目录, 如 D:\leidian\LDPlayer9\adb.exe
# - MuMu: 在 nx_main/ 等子目录
_ADB_RELATIVE = [
    r"adb.exe",                  # 雷电
    r"shell\adb.exe",            # MuMu
    r"nx_main\adb.exe",
    r"nx_device\shell\adb.exe",
    r"nx_device\12.0\shell\adb.exe",
    r"vmonitor\bin\adb_server.exe",
    r"shell\adb_server.exe",
]

# ADB 端口候选
# - 雷电 (LDPlayer): 基准 5555, 每多开一个实例 +2 → 5555, 5557, 5559, ...
# - MuMu: 基准 16384, +32 递增
def _candidate_ports() -> list[int]:
    ports = [5555 + i * 2 for i in range(10)]     # 雷电
    ports += [16384 + i * 32 for i in range(8)]   # MuMu 12
    ports += [16384 + i for i in range(1, 5)]
    ports += [7555, 62001, 62025]                 # MuMu 旧版 / 夜神 / 蓝叠
    return ports


# 用户常把软件装在「软件总目录」下 (名字各式各样). 这些前缀/全名命中即视为安装父目录.
_SOFTWARE_COLLECTION = {
    "program files", "program files (x86)",
    "software", "sofware", "softwares", "soft", "softwares",
    "apps", "tools",
}


def _mumu_dirs() -> list[Path]:
    """枚举可能的 MuMu 安装目录.

    兼容 Windows 上 Path 大小写敏感 (与 PowerShell 不同) 以及非标准安装位置.
    """
    found: list[Path] = []
    seen: set[str] = set()

    def _add(p: Path) -> None:
        key = str(p).lower()
        if key not in seen and p.is_dir():
            seen.add(key)
            found.append(p)

    # 候选父目录
    parents: list[Path] = []

    # Phase 1: 环境变量中已知的
    for key in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        v = os.environ.get(key)
        if v:
            parents.append(Path(v))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        parents.append(Path(local))

    # Phase 2: 扫描每个盘符根目录
    #   - 直接是 Netease → 当作父目录
    #   - 名字像 Soft*/Program*/Apps → 当作软件总目录, 深入一层找 Netease
    for drive in "CDEFGHIJK":
        root = Path(f"{drive}:/")
        if not root.exists():
            continue
        try:
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                lname = child.name.lower()
                if lname in ("netease", "leidian"):
                    # Netease → MuMu 的父目录; leidian → 雷电的父目录
                    parents.append(child)
                elif lname.startswith("ldplayer"):
                    # 雷电也常直接装在盘符根, 如 D:\LDPlayer9
                    _add(child)
                elif lname in _SOFTWARE_COLLECTION:
                    # 软件总目录, 深入一层找 Netease / leidian / LDPlayer*
                    try:
                        for sub in child.iterdir():
                            if sub.is_dir():
                                slname = sub.name.lower()
                                if slname in ("netease", "leidian"):
                                    parents.append(sub)
                                elif slname.startswith("ldplayer"):
                                    _add(sub)
                    except OSError:
                        pass
        except OSError:
            continue

    # Phase 3: 在每个父目录下找 MuMu* 子目录; 若是 Netease, 再深入一层
    for parent in parents:
        if not parent.is_dir():
            continue
        try:
            for child in parent.iterdir():
                if not child.is_dir():
                    continue
                lname = child.name.lower()
                if lname.startswith("mumu") or lname.startswith("ldplayer"):
                    _add(child)
                elif lname in ("netease", "leidian"):
                    try:
                        for sub in child.iterdir():
                            if sub.is_dir() and (
                                sub.name.lower().startswith("mumu")
                                or sub.name.lower().startswith("ldplayer")
                            ):
                                _add(sub)
                    except OSError:
                        pass
        except OSError:
            continue
    return found


def find_ldconsole() -> str | None:
    """定位雷电的命令行工具 ldconsole.exe (在安装目录内).

    为什么要用它, 而不是直接拉起/结束进程:
      雷电官方提供了 ldconsole.exe 做实例管理 (list2 列实例 / launch 启动 /
      quit 退出 / runapp 拉起应用), 属于"干净启动 / 干净退出".
      直接去拉 Ld9BoxHeadless.exe 或结束进程都是越权操作: 容易起错实例、
      留下脏状态, 而且拿不到实例与端口的对应关系.
    """
    for d in _mumu_dirs():
        for name in ("ldconsole.exe", "dnconsole.exe"):
            cand = d / name
            if cand.is_file():
                return str(cand)
    return None


def find_adb() -> str | None:
    """定位 adb.exe: 模拟器自带 → PATH → 深度搜索.

    优先用模拟器自带的 adb: 系统 PATH 里的 adb 版本若与模拟器不匹配,
    会出现 adb devices 列不出设备的情况 (同时装多个模拟器时尤其明显).
    """
    for d in _mumu_dirs():
        for rel in _ADB_RELATIVE:
            cand = d / rel
            if cand.is_file():
                return str(cand)

    path = shutil.which("adb")
    if path:
        return path

    for d in _mumu_dirs():
        for rel in _ADB_RELATIVE:
            cand = d / rel
            if cand.is_file():
                return str(cand)

    for d in _mumu_dirs():
        try:
            for exe in d.rglob("adb.exe"):
                return str(exe)
        except OSError:
            continue
    return None


def _port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _adb_cmd(adb_path: str, args: list[str], timeout: float = 10.0) -> str:
    try:
        proc = subprocess.run(
            [adb_path, *args],
            capture_output=True,
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
        return (proc.stdout or b"").decode("utf-8", "replace")
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.debug("adb 调用失败 %s: %s", args, exc)
        return ""


def list_devices(adb_path: str) -> list[str]:
    """解析 adb devices 输出, 只取在线序列号."""
    out = _adb_cmd(adb_path, ["devices"])
    serials: list[str] = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def probe_all_instances(adb_path: str) -> dict[str, list[str]]:
    """枚举所有在线实例, 返回 {serial: [pkg1, pkg2, ...]} 第三方应用列表.

    用于判断游戏具体装在哪个实例上.
    """
    result: dict[str, list[str]] = {}
    for serial in list_devices(adb_path):
        try:
            proc = subprocess.run(
                [adb_path, "-s", serial, "shell", "pm", "list", "packages", "-3"],
                capture_output=True,
                timeout=10.0,
                creationflags=_NO_WINDOW,
            )
            pkgs = []
            for line in proc.stdout.decode("utf-8", "replace").splitlines():
                if line.startswith("package:"):
                    pkgs.append(line[len("package:"):].strip())
            result[serial] = pkgs
        except Exception:
            result[serial] = []
    return result


def detect_serial(adb_path: str, host: str = "127.0.0.1") -> str | None:
    """探测可用的模拟器序列号, 必要时主动 connect 候选端口."""
    serials = list_devices(adb_path)
    if serials:
        log.info("发现已连接设备: %s", serials)
        return serials[0]

    candidates = [p for p in _candidate_ports() if _port_open(host, p)]
    if not candidates:
        log.warning("未发现任何开放的 ADB 端口, 请确认模拟器已启动")
        return None

    log.info("尝试连接候选端口: %s", candidates)
    for port in candidates:
        _adb_cmd(adb_path, ["connect", f"{host}:{port}"], timeout=5.0)

    serials = list_devices(adb_path)
    if serials:
        return serials[0]
    return None


def detect_screen_size(adb_path: str, serial: str) -> tuple[int, int] | None:
    """读取模拟器设定的设备分辨率 (注意: 实际帧可能被旋转)."""
    out = _adb_cmd(adb_path, ["-s", serial, "shell", "wm", "size"])
    if not out:
        return None
    override = re.search(r"Override size:\s*(\d+)x(\d+)", out)
    match = override or re.search(r"Physical size:\s*(\d+)x(\d+)", out)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def environment_report(adb_path: str | None, serial: str | None) -> str:
    """输出环境自检摘要."""
    lines = [
        f"adb 路径     : {adb_path or '未找到'}",
        f"adb 序列号   : {serial or '未连接'}",
    ]
    if adb_path and serial:
        size = detect_screen_size(adb_path, serial)
        lines.append(f"设备分辨率   : {size[0]}x{size[1]}" if size else "设备分辨率   : 未知")
    return "\n".join(lines)