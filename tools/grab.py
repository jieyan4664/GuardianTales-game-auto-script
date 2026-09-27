"""一键截图 + 环境自检.

输出:
- adb 路径 / 版本 / 设备序列号
- wm size 报告的设备分辨率
- 实际帧分辨率 (若与设备分辨率不同, 说明发生旋转)
- 单帧截图耗时 / 连续 10 帧平均 / 推算 FPS
- 当前帧保存路径

诊断性建议 (例如截图过慢时提示改成 1080x1920).

用法:
    python tools/grab.py                # 默认 rotate=0, 原样截图
    python tools/grab.py --rotate 90    # 临时旋转 90° 测试效果
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cv2                                                       # noqa: E402
import yaml                                                      # noqa: E402

from core.device import AdbDevice                                # noqa: E402
from core.env import (                                           # noqa: E402
    detect_screen_size,
    detect_serial,
    environment_report,
    find_adb,
    probe_all_instances,
)
from core.logger import get_logger                               # noqa: E402

log = get_logger("grab")
SHOT_DIR = ROOT / "assets" / "screenshots"
SHOT_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_PATH = ROOT / "config" / "settings.yaml"


def banner(text: str) -> None:
    print("=" * 60)
    print(f"  {text}")
    print("=" * 60)


def _load_rotate_from_settings() -> int:
    try:
        cfg = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8")) or {}
    except OSError:
        return 0
    return int((cfg.get("device") or {}).get("rotate", 0))


def main() -> None:
    parser = argparse.ArgumentParser(description="环境自检 + 一键截图")
    parser.add_argument(
        "--rotate", type=int, default=None,
        help="临时旋转角度 (0/90/180/270), 默认从 settings.yaml 读取",
    )
    parser.add_argument(
        "--adb", default=None, help="手动指定 adb.exe 绝对路径",
    )
    parser.add_argument(
        "--serial", default=None, help="手动指定设备序列号, 如 127.0.0.1:16384",
    )
    parser.add_argument(
        "--launch", default=None, metavar="PKG",
        help="截图前先启动指定包名 (如 com.bilibili.snake)",
    )
    parser.add_argument(
        "--activity", default=None, metavar="ACT",
        help="配合 --launch, 显式指定 Activity 组件 (如 .UnityPlayerActivity)",
    )
    parser.add_argument(
        "--resolve-activity", default=None, metavar="PKG",
        help="只查询并打印包的主 LAUNCHER Activity, 不启动不截图",
    )
    parser.add_argument(
        "--probe-all", action="store_true",
        help="枚举所有在线实例并列出每个实例上的第三方应用, 用于定位游戏在哪",
    )
    parser.add_argument(
        "--find-running", default=None, metavar="PKG",
        help="查询包是否在跑 + 当前停在哪个 Activity (后台也算)",
    )
    parser.add_argument(
        "--scan-games", action="store_true",
        help="只扫描已安装的疑似 Guardian Tales 包, 不截图",
    )
    parser.add_argument(
        "--list-third-party", action="store_true",
        help="列出设备上所有第三方应用 (排除系统包), 用于查找游戏包名",
    )
    parser.add_argument(
        "--wait", type=float, default=4.0,
        help="--launch 启动后等待秒数, 默认 4s",
    )
    parser.add_argument(
        "--quick", action="store_true",
        help="跳过 10 帧性能测试, 只截一张图 (快速验证旋转方向用)",
    )
    parser.add_argument(
        "--try-displays", action="store_true",
        help="遍历所有 display 各截一张图, 用于排查 Unity SurfaceView 截不到的问题",
    )
    parser.add_argument(
        "--record", action="store_true",
        help="用 screenrecord 录屏取首帧 (能捕获 SurfaceView/Unity 游戏画面)",
    )
    parser.add_argument(
        "--record-sec", type=int, default=1,
        help="--record 录制的秒数, 默认 1s",
    )
    parser.add_argument(
        "--win", action="store_true",
        help="用 Windows 窗口截图 (绕过 MuMu adb 截不到游戏画面的问题)",
    )
    parser.add_argument(
        "--win-list", action="store_true",
        help="列出所有可见 Windows 窗口, 用于找到 MuMu 窗口标题",
    )
    parser.add_argument(
        "--win-title", default="MuMu",
        help="--win 使用的窗口标题关键词, 默认 MuMu",
    )
    parser.add_argument(
        "--win-full", action="store_true",
        help="截取整个显示器 (排查游戏是否真的在屏幕可见区)",
    )
    parser.add_argument(
        "--win-deep", action="store_true",
        help="枚举顶层窗口 + 子窗口, 找出 MuMu 的游戏渲染窗口",
    )
    parser.add_argument(
        "--win-region", default=None, metavar="L,T,W,H",
        help="截取屏幕指定区域, 如 --win-region 100,50,1080,1920",
    )
    parser.add_argument(
        "--win-hwnd", type=int, default=None,
        help="直接绑定窗口句柄截图 (配合 --win 使用)",
    )
    parser.add_argument(
        "--win-mss", action="store_true",
        help="--win 强制使用 mss 屏幕抓取 (对比 PrintWindow 效果)",
    )
    parser.add_argument(
        "--analyze", action="store_true",
        help="分析游戏画面在窗口内的实际区域 (自动去黑边, 输出建议 content_roi)",
    )
    parser.add_argument(
        "--wgc", action="store_true",
        help="用 Windows Graphics Capture 截图 (能抓 GPU 合成的游戏画面)",
    )
    parser.add_argument(
        "--monitor", type=int, default=None,
        help="配合 --wgc: 捕获整个显示器 (1=主显示器), 游戏不在常规窗口里时用",
    )
    parser.add_argument(
        "--printwin-diag", action="store_true",
        help="逐一测试 PrintWindow 各变体 (flag/强制重绘/重试), 找出能出图的那种",
    )
    args = parser.parse_args()

    banner("环境自检 + 一键截图")

    adb_path = args.adb or find_adb()
    if not adb_path:
        sys.exit("[ERROR] 未找到 adb.exe, 请确认 MuMu 已安装或用 --adb 指定")

    serial = args.serial or detect_serial(adb_path)
    if not serial:
        sys.exit("[ERROR] 未探测到模拟器, 请确认 MuMu 已启动")

    print(environment_report(adb_path, serial))

    rotate = args.rotate if args.rotate is not None else _load_rotate_from_settings()
    device = AdbDevice(adb_path, serial, dry_run=True, rotate=rotate)
    if not device.check_alive():
        sys.exit("[ERROR] adb 连接无响应, 尝试 adb kill-server 后重试")

    # --scan-games: 仅扫描游戏包, 不进入截图流程
    if args.scan_games:
        banner("已安装的疑似 Guardian Tales 包 (含第三方应用)")
        candidates = []
        for kw in ("guardian", "gdts", "snake", "kakaogames", "bilibili"):
            candidates += device.list_installed(kw, third_party_only=True)
        # 过滤掉 TapTap / B 站主客户端这种无关的包, 只保留像游戏的
        candidates = [
            p for p in candidates
            if "kakao" in p or "gdts" in p or "snake" in p or "guardian" in p
        ]
        seen, ordered = set(), []
        for p in candidates:
            if p not in seen:
                seen.add(p)
                ordered.append(p)
        if ordered:
            for p in ordered:
                print(f"  {p}")
        else:
            print("  (未找到任何含 guardian/gt 的第三方包)")
        print("\n国际服常用包名: com.kakaogames.guardiantales")
        print("列出全部第三方应用: python tools/grab.py --list-third-party")
        return

    # --list-third-party: 列出全部第三方应用
    if args.list_third_party:
        banner("设备上所有第三方应用 (排除系统包)")
        pkgs = device.list_installed(third_party_only=True)
        if pkgs:
            for p in pkgs:
                print(f"  {p}")
            print(f"\n共 {len(pkgs)} 个第三方应用")
        else:
            print("  (设备上没有第三方应用, 游戏完全没装)")
        return

    # --resolve-activity: 仅查询主 Activity
    if args.resolve_activity:
        banner("主 LAUNCHER Activity 查询")
        act = device.resolve_launch_activity(args.resolve_activity)
        if act:
            print(f"  {args.resolve_activity}")
            print(f"  → {act}")
            print(f"\n显式启动命令示例:")
            print(f"  python tools/grab.py --launch {args.resolve_activity} --activity {act.split('/', 1)[1]}")
        else:
            print(f"  (未找到 {args.resolve_activity} 的主 Activity)")
        return

    # --find-running: 查询包的运行状态 + 当前 Activity
    if args.find_running:
        banner(f"运行状态查询: {args.find_running}")
        alive = device.is_running(args.find_running)
        print(f"进程存在  : {'是' if alive else '否'}")
        acts = device.running_activities(args.find_running)
        if acts:
            print(f"\nActivity 栈中的 Activity (后台运行也算):")
            for a in acts:
                print(f"  {a}")
            # 取最后一个作为"切到前台"目标 (栈顶通常是当前界面)
            target = acts[-1].split("/", 1)[1] if "/" in acts[-1] else acts[-1]
            print(f"\n切到前台示例:")
            print(f"  python tools/grab.py --launch {args.find_running} --activity {target}")
        else:
            print(f"\n  (Activity 栈里没有 {args.find_running}, 游戏可能完全没启动)")
        return

    # --printwin-diag: 找出 PrintWindow 到底哪种方式能出图
    if args.printwin_diag:
        banner("PrintWindow 变体诊断")
        try:
            from core.window import WindowCapture
        except ImportError as exc:
            print(f"[ERROR] {exc}")
            return
        try:
            cap = WindowCapture()
            wins = WindowCapture.list_windows(args.win_title, include_children=True)
            if not wins:
                print(f"[ERROR] 未找到标题含 '{args.win_title}' 的窗口")
                return
            exact = [w for w in wins
                     if w.title.strip().lower() == args.win_title.strip().lower()]
            info = exact[0] if exact else max(wins, key=lambda w: w.width * w.height)
            cap.bind(info.hwnd)
            print(f"目标窗口: {info}\n")

            results = cap.try_all_printwindow()
            print(f"{'方式':<22}{'拿到图':<9}{'全黑':<9}尺寸")
            print("-" * 52)
            ok_ones = []
            for name, r in results.items():
                mark = ""
                if r["ok"] and not r["black"]:
                    mark = "  ★出图了"
                    ok_ones.append(name)
                print(f"{name:<22}{str(r['ok']):<9}{str(r['black']):<9}{r['shape']}{mark}")

            if ok_ones:
                # 把第一个成功的存下来供确认
                img = None
                if ok_ones[0].startswith("flag2"):
                    img = cap._printwindow_raw(0x00000002)
                else:
                    img = cap._printwindow_raw(0x00000000)
                if img is not None:
                    ts = time.strftime("%Y%m%d_%H%M%S")
                    out_p = SHOT_DIR / f"pw_{ok_ones[0]}_{ts}.png"
                    cv2.imwrite(str(out_p), img)
                    print(f"\n已保存出图变体 '{ok_ones[0]}': {out_p}")
                    print("请打开确认是不是游戏画面")
            else:
                print("\n所有变体都拿不到有效画面")
            cap.close()
        except Exception as exc:
            print(f"[ERROR] {exc}")
        return

    # --wgc: Windows Graphics Capture 截图 (抓 GPU 合成的游戏画面)
    if args.wgc:
        banner("WGC 截图 (Windows Graphics Capture)")
        try:
            from core.window import WindowCapture, is_black_frame
            from core.wgc import WgcCapture
        except ImportError as exc:
            print(f"[ERROR] 导入失败: {exc}")
            print("请先安装: pip install windows-capture")
            return
        try:
            if args.monitor is not None:
                # 捕获整个显示器: 游戏画面不在常规窗口里时的正确方式
                print(f"模式: 捕获整个显示器 #{args.monitor}")
                img = WgcCapture(monitor_index=args.monitor).grab()
            else:
                wins = WindowCapture.list_windows(args.win_title, include_children=True)
                if not wins:
                    print(f"[ERROR] 未找到标题含 '{args.win_title}' 的窗口")
                    return
                # 精确匹配标题优先, 否则取面积最大的
                exact = [w for w in wins
                         if w.title.strip().lower() == args.win_title.strip().lower()]
                info = exact[0] if exact else max(wins, key=lambda w: w.width * w.height)
                print(f"模式: 捕获窗口 {info}")
                img = WgcCapture(hwnd=info.hwnd).grab()
            if img is None:
                print("[ERROR] WGC 抓帧超时 (窗口可能最小化/不可见, 或不支持捕获)")
                return
            hh, ww = img.shape[:2]
            ts = time.strftime("%Y%m%d_%H%M%S")
            out_w = SHOT_DIR / f"wgc_{ts}.png"
            cv2.imwrite(str(out_w), img)
            print(f"WGC 截图: {ww}x{hh}  →  {out_w}")
            if is_black_frame(img):
                print("[!] 画面是黑屏 —— WGC 也没抓到内容")
            else:
                print("[OK] 画面有内容, 请打开确认是否为游戏画面")
        except Exception as exc:
            print(f"[ERROR] {exc}")
        return

    # --win-full / --win-region / --win-deep: 窗口截图的排查工具
    if args.win_full or args.win_region or args.win_deep:
        try:
            from core.window import WindowCapture
        except ImportError as exc:
            print(f"[ERROR] 无法导入窗口模块: {exc}")
            print("请先安装: pip install pywin32 mss")
            return
        cap = WindowCapture()
        ts = time.strftime("%Y%m%d_%H%M%S")

        if args.win_deep:
            banner("顶层窗口 + 子窗口 (找 MuMu 游戏渲染窗口)")
            for kw in (args.win_title, None):
                try:
                    wins = WindowCapture.list_windows(kw, include_children=True)
                except Exception:
                    continue
                if wins:
                    print(f"\n关键词={kw or '全部'} 命中 {len(wins)} 个:")
                    print(f"{'hwnd':>10}  {'客户区':>12}  {'位置':>18}  标题")
                    for w in wins[:30]:
                        print(
                            f"{w.hwnd:>10}  {f'{w.width}x{w.height}':>12}  "
                            f"{f'({w.rect[0]},{w.rect[1]})':>18}  {w.title[:40]}"
                        )
                    break
            print("\n找客户区尺寸接近 1080x1920 (或游戏比例) 的那一行, 记下 hwnd, 然后:")
            print("  python tools/grab.py --win --win-hwnd <hwnd>")
            cap.close()
            return

        if args.win_full:
            banner("全屏截图")
            img = cap.capture_fullscreen()
            hh, ww = img.shape[:2]
            out_f = SHOT_DIR / f"fullscreen_{ts}.png"
            cv2.imwrite(str(out_f), img)
            print(f"全屏: {ww}x{hh}  →  {out_f}")

            # 自动检测游戏画面区域 (窗口 rect 不可靠时的兜底)
            try:
                from core.window import detect_content_roi, is_black_frame
                if is_black_frame(img):
                    print("\n[WARN] 全屏截图是黑屏")
                else:
                    roi = detect_content_roi(img)
                    if roi:
                        rx, ry, rw, rh = roi
                        print(f"\n检测到内容区域 [x, y, w, h] = {list(roi)}")
                        print(f"  尺寸 {rw}x{rh}, 比例 {rw/rh:.3f}")
                        print(f"\n用该区域截图验证:")
                        print(f"  python tools/grab.py --win-region {rx},{ry},{rw},{rh}")
                    else:
                        print("\n整屏都有内容, 未检测到独立区域")
            except Exception as exc:
                print(f"[i] 自动检测跳过: {exc}")

            print("\n也请打开 png 确认 MuMu 游戏画面在屏幕上的位置")
            cap.close()
            return

        if args.win_region:
            banner("区域截图")
            try:
                l, t, w, h = (int(v) for v in args.win_region.split(","))
            except ValueError:
                print("[ERROR] --win-region 格式应为 L,T,W,H, 如 100,50,1080,1920")
                cap.close()
                return
            img = cap.capture_region(l, t, w, h)
            out_r = SHOT_DIR / f"region_{ts}.png"
            cv2.imwrite(str(out_r), img)
            print(f"区域 ({l},{t}) {w}x{h}  →  {out_r}")
            cap.close()
            return

    # --win-list: 列出所有 Windows 窗口
    if args.win_list:
        try:
            from core.window import WindowCapture
        except ImportError as exc:
            print(f"[ERROR] 无法导入窗口模块: {exc}")
            print("请先安装: pip install pywin32 mss")
            return
        banner("所有可见 Windows 窗口")
        try:
            wins = WindowCapture.list_windows()
        except Exception as exc:
            print(f"[ERROR] {exc}")
            return
        if not wins:
            print("  (没有可见窗口)")
            return
        print(f"{'hwnd':>10}  {'客户区尺寸':>12}  {'位置':>14}  标题")
        for w in wins[:40]:
            print(
                f"{w.hwnd:>10}  {f'{w.width}x{w.height}':>12}  "
                f"{f'({w.rect[0]},{w.rect[1]})':>14}  {w.title[:50]}"
            )
        print("\n找标题像 MuMu/模拟器的那一行, 记下标题关键词, 然后:")
        print('  python tools/grab.py --win --win-title "<关键词>"')
        return

    # --win: Windows 窗口截图
    if args.win:
        try:
            from core.window import WindowCapture
        except ImportError as exc:
            print(f"[ERROR] 无法导入窗口模块: {exc}")
            print("请先安装: pip install pywin32 mss")
            return
        banner("Windows 窗口截图")
        try:
            cap = WindowCapture()
            if args.win_hwnd:
                info = cap.bind(args.win_hwnd)
            else:
                info = cap.find(args.win_title)
            print(f"已绑定窗口: {info}")

            from core.window import dpi_info
            _d = dpi_info()
            print(f"系统缩放: {_d.get('scale_percent', '?')}%   DPI: {_d.get('dpi', '?')}")
            if _d.get("mss_monitors"):
                print(f"mss 显示器: {_d['mss_monitors']}")

            # 默认优先 PrintWindow (不怕终端遮挡); 黑屏则自动回退 mss
            from core.window import is_black_frame
            if args.win_mss:
                method = "mss 屏幕抓取 (--win-mss 指定)"
                img = cap.capture()
            else:
                img = cap.capture_printwindow()
                if img is not None and is_black_frame(img):
                    method = "mss 屏幕抓取 (PrintWindow 返回黑屏, 已回退)"
                    img = cap.capture()
                else:
                    method = "PrintWindow"

            hh, ww = img.shape[:2]
            ts = time.strftime("%Y%m%d_%H%M%S")
            out_w = SHOT_DIR / f"win_{ts}.png"
            cv2.imwrite(str(out_w), img)
            print(f"截图方式: {method}")
            print(f"窗口截图: {ww}x{hh}  →  {out_w}")

            # --analyze: 检测游戏画面在窗口内的实际区域 (去黑边)
            if args.analyze:
                from core.window import detect_content_roi
                roi = detect_content_roi(img)
                if roi is None:
                    print("\n内容区域: 整帧都有内容 (未检测到黑边)")
                else:
                    rx, ry, rw, rh = roi
                    print(f"\n检测到内容区域 [x, y, w, h] = {list(roi)}")
                    print(f"  比例: {rw}x{rh}  ({rw/rh:.3f})")
                    if rw > rh:
                        print("  → 横向画面 (游戏可能被旋转或窗口是横屏)")
                    else:
                        print("  → 竖向画面 (符合竖屏游戏)")
                    # 保存裁剪后的游戏画面
                    crop = img[ry:ry + rh, rx:rx + rw]
                    out_c = SHOT_DIR / f"content_{ts}.png"
                    cv2.imwrite(str(out_c), crop)
                    print(f"  裁剪后游戏画面: {out_c}")
                    print(f"\n建议写入 settings.yaml:")
                    print(f"  capture:")
                    print(f"    content_roi: [{rx}, {ry}, {rw}, {rh}]")
            else:
                print("\n请打开该 png 确认是否为游戏画面")

            cap.close()
        except Exception as exc:
            print(f"[ERROR] {exc}")
        return

    # --record: 录屏取首帧 (捕获 SurfaceView)
    if args.record:
        banner(f"录屏取帧 (screenrecord, {args.record_sec}s)")
        img = device.screencap_via_record(duration=args.record_sec)
        if img is None:
            print("[ERROR] 录屏取帧失败 (可能 /sdcard 不可写 或设备不支持 screenrecord)")
            return
        hh, ww = img.shape[:2]
        ts = time.strftime("%Y%m%d_%H%M%S")
        out_r = SHOT_DIR / f"record_{ts}.png"
        cv2.imwrite(str(out_r), img)
        print(f"录屏首帧: {ww}x{hh}  →  {out_r}")
        print("\n请打开该 png 确认是否是游戏画面 (而非桌面)")
        return

    # --try-displays: 遍历所有 display 各截一张, 排查 SurfaceView 捕获问题
    if args.try_displays:
        banner("遍历所有 display 截图")
        help_txt = device.screencap_help()
        if help_txt.strip():
            print("screencap 帮助信息:")
            for line in help_txt.splitlines():
                if line.strip():
                    print(f"  {line.strip()}")
            print()
        displays = device.list_displays()
        print(f"发现 display: {displays}")
        ts = time.strftime("%Y%m%d_%H%M%S")
        for did in displays:
            img = device.screencap_display(did)
            if img is None:
                print(f"  display {did}: 截图失败")
                continue
            hh, ww = img.shape[:2]
            out_d = SHOT_DIR / f"display{did}_{ts}.png"
            cv2.imwrite(str(out_d), img)
            print(f"  display {did}: {ww}x{hh}  →  {out_d}")
        print("\n请用看图工具逐个打开上面的 png, 告诉我哪个 display 截到了游戏画面")
        print("(如果一个都没有, 说明 SurfaceView 完全无法被 screencap 捕获, 需换 minicap/录屏方案)")
        return

    # --probe-all: 枚举所有实例的第三方应用, 定位游戏在哪
    if args.probe_all:
        banner("所有实例的第三方应用列表")
        data = probe_all_instances(adb_path)
        if not data:
            print("  (无在线实例)")
        for serial, pkgs in data.items():
            print(f"\n[{serial}]  共 {len(pkgs)} 个第三方应用")
            # 优先显示疑似 Guardian Tales 的
            interesting = [p for p in pkgs if any(
                k in p.lower() for k in ("guardian", "gdts", "snake", "kakao", "bilibili")
            )]
            if interesting:
                print(f"  ★ 疑似游戏包:")
                for p in interesting:
                    print(f"    {p}")
            for p in pkgs:
                if p in interesting:
                    continue
                print(f"    {p}")
        return

    # --launch: 截图前先启动游戏 (临时关掉 dry_run 才能真正发送启动指令)
    if args.launch:
        print(f"\n启动应用: {args.launch}")
        if args.activity:
            print(f"指定 Activity: {args.activity}")
        device.dry_run = False
        ret = device.launch_app(args.launch, activity=args.activity)
        device.dry_run = True
        if ret:
            print("am start 返回:")
            for line in ret.splitlines():
                if line.strip():
                    print(f"  {line.strip()}")
        time.sleep(args.wait)
        print(f"等待 {args.wait:.1f}s 后继续…\n")

    # 真实帧分辨率: 这一步会触发降级检测, 已应用 rotate
    w, h = device.frame_size
    raw_w, raw_h = device._raw_size if device._raw_size else (w, h)
    if rotate:
        print(f"原始帧分辨率: {raw_w}x{raw_h}  →  旋转 {rotate}° 后: {w}x{h}")
    else:
        print(f"实际帧分辨率: {w}x{h}")

    # 设备 vs 实际帧对比. 注意: 是否旋转取决于游戏本身是横屏还是竖屏,
    # 这里只陈述事实, 不武断建议 rotate (横屏游戏时 1920x1080 就是正常的).
    device_size = detect_screen_size(adb_path, serial)
    if device_size:
        dw, dh = device_size
        if (dw, dh) != (w, h):
            print(
                f"ℹ 设备设置 {dw}x{dh}  vs  实际帧 {w}x{h} (宽x高)\n"
                f"   → 两者方向不同. 横屏游戏以实际帧 {w}x{h} 为准, 无需 rotate;\n"
                f"     仅当游戏是竖屏且画面躺倒时, 才在 settings.yaml 设 rotate: 90"
            )
        else:
            print(f"✓ 设备与实际帧一致: {w}x{h}")

    if args.quick:
        print("\n[快速模式] 跳过性能测试, 直接截图")
        avg = 0.0
    else:
        print("\n[性能测试] 连续截图 10 帧...")
        samples = []
        for i in range(10):
            device.screencap()
            ms = device.stat.last_ms
            samples.append(ms)
            print(f"  帧 {i+1:2d}: {ms:5.0f}ms")

        avg = sum(samples) / len(samples)
        fps = 1000.0 / avg if avg > 0 else 0.0
        print(f"\n平均耗时: {avg:.0f}ms/帧  ({fps:.1f} FPS)")

    if not args.quick:
        if avg > 800:
            print(
                f"[!] 截图极慢 (>{avg:.0f}ms/帧), 强烈建议把 MuMu 分辨率改成 1080x1920\n"
                f"    路径: MuMu 设置 → 性能 → 分辨率 → 1080x1920\n"
                f"    预期提升: ~{avg:.0f}ms/帧 → ~250ms/帧 (约 {avg/250:.1f}x)"
            )
        elif avg > 400:
            print(
                f"[!] 截图较慢 ({avg:.0f}ms/帧), 建议把 MuMu 分辨率改为 1080x1920\n"
                f"    预期提升: {avg:.0f}ms/帧 → ~250ms/帧"
            )
        elif avg > 250:
            print("[!] 截图偏慢, 可考虑 minicap 优化 (需 root)")
        else:
            print("[OK] 截图性能良好")

    # 保存当前帧 (已含 rotate) + 前台应用诊断
    image = device.screencap()
    ts = time.strftime("%Y%m%d_%H%M%S")
    suffix = f"_rot{rotate}" if rotate else ""
    out = SHOT_DIR / f"grab_{ts}{suffix}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), image)

    pkg = device.current_package() or ""
    is_game = bool(pkg) and any(
        k in pkg.lower() for k in ("guardian", "gdts", "snake", "kakao", "bilibili")
    )
    print(f"\n当前帧已保存: {out}")
    print(f"当前前台应用 : {pkg or '(未识别)'}")

    if not is_game:
        # 焦点诊断: 打印 dumpsys 关键字段
        focus = device.current_focus_raw()
        if focus:
            print("\n焦点诊断 (dumpsys):")
            for k, v in focus.items():
                print(f"  {k:>15s}: {v}")
        print(
            "\n[!] 当前前台不是 Guardian Tales, 截图可能是 MuMu 桌面/启动器.\n"
            "    解决方案:\n"
            "      1) 在 MuMu 窗口里手动点开游戏, 然后重跑本工具\n"
            "      2) 用 --launch 自动启动 (国际服: com.kakaogames.gdts):\n"
            "         python tools/grab.py --launch com.kakaogames.gdts\n"
            "      3) 不确定包名? 先扫一下:\n"
            "         python tools/grab.py --scan-games"
        )
    else:
        print(
            "\n[OK] 检测到游戏在前台, 可以进入下一步:\n"
            "      python tools/cropper.py --live  开始采集模板"
        )


if __name__ == "__main__":
    main()