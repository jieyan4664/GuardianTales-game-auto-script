"""雷电 (LDPlayer) 模拟器启动器 —— 独立工具.

注意: 目前【没有】接入 tools/daily.py 或任何任务脚本, 先单独验证好再说.

为什么用官方 ldconsole.exe, 而不是直接拉进程:
  雷电安装目录下自带命令行工具 ldconsole.exe, 支持 list2(列实例) / launch(启动)
  / quit(退出) / runapp(拉起应用) 等操作, 属于"干净启动 / 干净退出".
  直接去拉 Ld9BoxHeadless.exe 或结束进程都是越权操作: 容易起错实例、留下脏状态,
  而且拿不到"实例 ↔ adb 端口"的对应关系.

流程:
  ① 定位安装目录里的 ldconsole.exe (core.env.find_ldconsole)
  ② list2 读实例清单 (index / 名称 / 是否已启动)
  ③ 已在运行 → 不重复启动; 未运行 → launch
  ④ 轮询等 adb 能列到该设备 (实例 N 的 adb 端口 = 5555 + N*2)
  ⑤ 等 sys.boot_completed == 1 (Android 真正就绪, 光 adb 连上还不够)
  ⑥ 可选: 拉起游戏 (ldconsole runapp → monkey → am start, 依次尝试)
  ⑦ 可选: 点进主城. 画面顺序是
       黑屏(开局) → 登录界面 → 点一下 → 黑屏(登录后) → 主城,
     所以要先【跳过开局黑屏】(否则会被误当成登录后的黑屏, 一下都不点),
     再在登录界面周期性点击 (⚠️ 别点左下角, 那里有适龄提示会跳走),
     最后在登录后的黑屏期间【停手】, 只等主城出现 (判据: 右下角 my_room)

用法:
    python tools/launcher.py                  # 启动实例 0, 等开机完成
    python tools/launcher.py --list            # 只看实例清单, 不启动
    python tools/launcher.py --game            # 开机完成后再启动游戏
    python tools/launcher.py --index 1         # 指定实例
    python tools/launcher.py --timeout 180     # 等 adb 出现的上限(秒)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.device import AdbDevice               # noqa: E402
from core.env import find_adb, find_ldconsole   # noqa: E402
from core.logger import get_logger              # noqa: E402
from core.vision import Matcher                 # noqa: E402
from game.actions import Actions                # noqa: E402

log = get_logger("launcher")

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _decode(raw: bytes) -> str:
    """解码命令输出.

    ldconsole 是 Windows 控制台程序, 输出走 GBK, 不是 UTF-8 ——
    按 UTF-8 解会得到替换字符 \\ufffd, 再打印到 GBK 控制台就直接抛
    UnicodeEncodeError. 所以先试 UTF-8, 出现替换字符就退回 GBK.
    """
    for enc in ("utf-8", "gbk"):
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if "\ufffd" not in text:
            return text
    return raw.decode("gbk", "replace")


def _run(cmd: list[str], timeout: float = 30.0) -> tuple[int, str]:
    """跑一条命令, 返回 (返回码, 合并输出). 出错也不抛异常, 便于诊断."""
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=_NO_WINDOW)
        out = _decode((r.stdout or b"") + (r.stderr or b""))
        return r.returncode, out.strip()
    except (OSError, subprocess.SubprocessError) as e:
        return -1, f"{type(e).__name__}: {e}"


def parse_list2(text: str) -> list[dict]:
    """解析 `ldconsole list2` 的输出.

    每行逗号分隔, 字段顺序:
        index, 名称, 顶层窗口句柄, 绑定窗口句柄, 绑定?, android PID, VBox PID, 宽, 高, DPI

    判断实例有没有启动, 看的是 android PID / VBox PID: 两者都是 -1 表示没启动.
    (不靠窗口句柄判断 —— 窗口关了但 VM 还在后台跑的情况是存在的)
    """
    items: list[dict] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 10:
            continue
        try:
            idx = int(parts[0])
            pid_android = int(parts[5])
            pid_vbox = int(parts[6])
        except ValueError:
            continue
        items.append({
            "index": idx,
            "name": parts[1],
            "running": pid_android > 0 or pid_vbox > 0,
            "pid_android": pid_android,
            "pid_vbox": pid_vbox,
            "w": parts[7],
            "h": parts[8],
            "dpi": parts[9],
        })
    return items


def expected_serial(index: int) -> str:
    """实例 index 对应的 adb 序列号.

    雷电实例 0 的 adb 端口是 5555, adb 把它显示为 emulator-5554 (端口 - 1);
    每多开一个实例端口 +2 → 实例 1 是 5557 / emulator-5556.
    """
    return f"emulator-{5555 + index * 2 - 1}"


def device_present(adb: str, serial: str) -> bool:
    """adb devices 里是否已经有这个设备, 且状态是 device (不是 offline)."""
    _, out = _run([adb, "devices"])
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[0] == serial and parts[1] == "device":
            return True
    return False


def wait_device(adb: str, serial: str, timeout: float,
                interval: float = 2.0) -> bool:
    """轮询等 adb 列到该设备且状态为 device.

    刚启动时会先经历 offline / unauthorized, 那时还不能操作, 所以要等状态.
    """
    end = time.time() + timeout
    while time.time() < end:
        _, out = _run([adb, "devices"])
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[0] == serial:
                if parts[1] == "device":
                    log.info("adb 已连上 [%s]", serial)
                    return True
                log.info("设备 [%s] 当前状态: %s, 继续等...", serial, parts[1])
                break
        else:
            log.info("等 adb 出现 [%s] (还剩 %.0fs)", serial, end - time.time())
        time.sleep(interval)
    log.warning("等 %.0fs 仍未连上 [%s]", timeout, serial)
    return False


def wait_boot(adb: str, serial: str, timeout: float,
              interval: float = 3.0) -> bool:
    """等 Android 真正就绪: sys.boot_completed == 1.

    为什么 adb 连上还不够: 设备刚出现时系统还在启动, 这时候发 am start
    会被系统忽略或启动到一半, 后面截屏/点击行为会很怪.
    """
    end = time.time() + timeout
    while time.time() < end:
        _, out = _run([adb, "-s", serial, "shell",
                       "getprop", "sys.boot_completed"])
        if out.strip() == "1":
            log.info("系统开机完成 (sys.boot_completed=1)")
            return True
        log.info("等待开机完成 (还剩 %.0fs)", end - time.time())
        time.sleep(interval)
    log.warning("等 %.0fs 仍未开机完成", timeout)
    return False


def build_device(adb: str, serial: str, settings: dict) -> AdbDevice:
    """构造设备对象 (只用于截图 + 点击), 参数与其它任务脚本保持一致."""
    dev_cfg = settings.get("device", {})
    action_cfg = settings.get("action", {})
    return AdbDevice(
        adb_path=adb,
        serial=serial,
        dry_run=False,
        tap_jitter=int(action_cfg.get("tap_jitter", 0)),
        min_tap_interval=float(action_cfg.get("min_tap_interval", 0.05)),
        rotate=int(dev_cfg.get("rotate", 0)),
    )


def build_matcher(settings: dict) -> Matcher:
    vision_cfg = settings.get("vision", {})
    return Matcher(
        threshold=float(vision_cfg.get("threshold", 0.85)),
        scales=vision_cfg.get("scales", [1.0]),
        method=vision_cfg.get("method", "ccoeff"),
    )


def at_main_city(device: AdbDevice, matcher: Matcher, tpl: str,
                 roi) -> bool:
    """是否已进入主城: 认右下角的 my_room 标识 (模板 + ROI 都在 city.yaml)."""
    try:
        return matcher.exists(device.screencap(), tpl, roi=roi)
    except FileNotFoundError:
        log.warning("模板 [%s] 不存在, 无法判断是否在主城", tpl)
        return False


def wait_main_city(device: AdbDevice, matcher: Matcher, tpl: str, roi,
                   timeout: float, interval: float = 2.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if at_main_city(device, matcher, tpl, roi):
            log.info("已进入主城")
            return True
        time.sleep(interval)
    return False


def _black_ratio(img, pixel_threshold: int = 30) -> float:
    """画面里"接近全黑"的像素占比.

    为什么用占比而不是整帧平均亮度: 登录后的加载黑屏是【大范围】黑,
    但往往还留着少量 UI / logo, 平均亮度会被那点亮部拉高, 容易漏判.
    直接数"够黑的像素占多少"更贴合"大范围黑屏"这个特征.
    """
    import cv2
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    return float((gray < pixel_threshold).mean())


def hold_black(device: AdbDevice, black_ratio: float, hold: float,
               interval: float = 0.6) -> bool:
    """黑屏是否真的【持续住】了: hold 秒内全程都得是黑屏才算.

    为什么需要这个: 点掉登录界面后会有一小段转场暗帧, 单凭一帧判定会把
    它当成"登录后的加载黑屏"而提前停手 (实测踩过). 真正的加载黑屏是持续
    好几秒的, 所以要求它 hold 住才算数.
    """
    end = time.time() + hold
    while time.time() < end:
        if _black_ratio(device.screencap()) < black_ratio:
            return False
        time.sleep(interval)
    return True


def enter_main_city(device: AdbDevice, matcher: Matcher, actions: Actions,
                    tpl: str, roi, tap_pt, settle: float, tap_gap: float,
                    max_taps: int, timeout: float,
                    black_ratio: float = 0.6, black_hold: float = 2.0,
                    interval: float = 2.0) -> bool:
    """把游戏推进到主城.

    实测到的画面顺序:
        黑屏 / logo动画(开局) → 登录界面 → [点一下] → 黑屏(登录后加载) → 主城

    为什么不能靠"当前是不是黑屏"来分阶段:
      开局是黑屏, 登录后也是黑屏, 两者长得一样. 踩过的坑就是一上来看到
      黑屏就当成"登录后的", 于是一次都不点, 游戏永远卡在登录界面.

    改用【有没有见过登录界面(非黑屏)】当判据:
      没见过 → 现在再黑也只是开局, 继续点 (点击还能加快 logo 动画)
      见过   → 现在又黑了 = 登录后的加载黑屏, 停手

    两个注意点:
      - ⚠️ 绝对不能点【左下角】: 那里有适龄提示, 点了会跳转到别的页面
      - 每一轮都先查主城: 已经在主城就一次都不点, 避免在城里乱点
    """
    # 先快查: 单独跑 --enter-city 时游戏可能已经在主城了, 那就不用等也不用点
    if at_main_city(device, matcher, tpl, roi):
        log.info("已经在主城, 无需点击")
        return True

    log.info("等游戏起来 %.1fs ...", settle)
    time.sleep(settle)

    # 阶段 1: 一路点到"登录后的黑屏"出现为止
    #   (开局黑屏期间【也点】, 实测能加快 logo 动画, 所以不能见黑就停)
    seen_login = False
    for i in range(1, max_taps + 1):
        if at_main_city(device, matcher, tpl, roi):
            log.info("已进入主城")
            return True

        ratio = _black_ratio(device.screencap())
        black = ratio >= black_ratio

        if black and seen_login:
            # 必须【持续住】才算登录后的加载黑屏 —— 转场的一帧暗帧不算,
            # 否则会在登录还没真正开始时就把手停了.
            if hold_black(device, black_ratio, black_hold):
                log.info("检测到【登录后的加载黑屏】(黑像素占比 %.2f, "
                         "已持续 %.1fs) → 停止点击, 等主城",
                         ratio, black_hold)
                break
            log.info("只是一闪而过的暗帧 (占比 %.2f), 不算加载黑屏 → 继续点",
                     ratio)
        if not black:
            seen_login = True

        log.info("点一下屏幕 %s (第 %d/%d 次; 避开左下角%s)",
                 tap_pt, i, max_taps,
                 "" if seen_login else " · 开局阶段, 可加快 logo 动画")
        actions.tap_point(*tap_pt, wait=tap_gap)

    # 阶段 2: 黑屏之后不再点击, 只等主城出现
    log.info("等主城出现 (最多 %.0fs, 期间不点击)...", timeout)
    if wait_main_city(device, matcher, tpl, roi, timeout, interval):
        return True

    log.warning("等 %.0fs 仍未进入主城 —— 请手动看一眼模拟器停在哪个界面",
                timeout)
    return False


def _launch_ok(rc: int, out: str) -> bool:
    """判断启动指令是否真的成功.

    am start 被系统拒绝时返回码仍可能是 0, 所以必须从输出里认这些字样 ——
    只看返回码会把失败当成成功.
    """
    if rc != 0:
        return False
    bad = ("SecurityException", "Permission Denial", "Error:",
           "Error type", "Exception occurred", "not exported",
           "does not exist", "Unknown command")
    return not any(b in out for b in bad)


def main() -> None:
    parser = argparse.ArgumentParser(description="雷电模拟器启动器 (独立工具)")
    parser.add_argument("--list", action="store_true",
                        help="只打印实例清单, 不启动")
    parser.add_argument("--index", type=int, default=0,
                        help="实例序号 (默认 0)")
    parser.add_argument("--name", default=None,
                        help="按实例名选择 (与 --index 二选一)")
    parser.add_argument("--game", action="store_true",
                        help="开机完成后用 am start 拉起游戏")
    parser.add_argument("--timeout", type=float, default=180.0,
                        help="等 adb 列出设备的上限秒数 (冷启动较慢)")
    parser.add_argument("--boot-timeout", type=float, default=120.0,
                        help="等系统开机完成的上限秒数")
    parser.add_argument("--game-wait", type=float, default=8.0,
                        help="启动游戏后等待的秒数")
    parser.add_argument("--enter-city", action="store_true",
                        help="点一下屏幕, 从登录界面进入主城 "
                             "(判据: 主城右下角的 my_room 标识)")
    parser.add_argument("--tap-x", type=int, default=None,
                        help="登录界面的点击 x (默认屏幕中心)")
    parser.add_argument("--tap-y", type=int, default=None,
                        help="登录界面的点击 y (默认屏幕中心)")
    parser.add_argument("--city-settle", type=float, default=1.0,
                        help="拉起游戏后等多少秒再开始点击 (默认 1s)")
    parser.add_argument("--city-tap-gap", type=float, default=1.0,
                        help="两次点击之间的间隔 (默认 1s). 实测: 1s 比 3s 快约 5 秒 "
                             "—— 开局 logo 动画期间点得更密, 能把它推快")
    parser.add_argument("--city-max-taps", type=int, default=10,
                        help="黑屏出现前最多点几次 (默认 10)")
    parser.add_argument("--city-timeout", type=float, default=60.0,
                        help="黑屏之后等主城出现多久 (默认 60s, 期间一次都不点)")
    parser.add_argument("--city-black-ratio", type=float, default=0.6,
                        help="黑像素占比达到多少算黑屏 (默认 0.6)")
    parser.add_argument("--city-black-hold", type=float, default=2.0,
                        help="黑屏要持续多少秒才认作登录后的加载黑屏 "
                             "(默认 2s; 用于排除转场的一帧暗帧)")
    args = parser.parse_args()

    ld = find_ldconsole()
    if not ld:
        sys.exit("[ERROR] 没找到 ldconsole.exe —— 雷电安装目录没被探测到")
    log.info("ldconsole: %s", ld)

    settings = yaml.safe_load(
        (ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")
    ) or {}

    rc, out = _run([ld, "list2"])
    if rc != 0:
        sys.exit(f"[ERROR] list2 执行失败: {out}")
    items = parse_list2(out)
    if not items:
        sys.exit(f"[ERROR] 解析不到任何实例, list2 原始输出:\n{out}")

    print("实例清单:")
    for it in items:
        state = "运行中" if it["running"] else "未启动"
        print(f"  [{it['index']}] {it['name']}  {state}  "
              f"{it['w']}x{it['h']} dpi={it['dpi']}")
    if args.list:
        return

    # 选实例
    if args.name:
        target = next((i for i in items if i["name"] == args.name), None)
        if target is None:
            sys.exit(f"[ERROR] 没有名为 [{args.name}] 的实例")
    else:
        target = next((i for i in items if i["index"] == args.index), None)
        if target is None:
            sys.exit(f"[ERROR] 没有 index={args.index} 的实例")

    idx = target["index"]
    serial = expected_serial(idx)

    adb = find_adb()
    if not adb:
        sys.exit("[ERROR] 没找到 adb.exe")

    # 双重确认"是不是已经在跑": list2 说在跑, 或 adb 里已经有了 → 都不重复启动
    # (避免开出第二个实例, 那会让 adb 出现多台设备、serial 自动探测变得不可控)
    if target["running"] or device_present(adb, serial):
        log.info("实例 [%d] 已在运行 (list2=%s, adb 已有=%s), 不重复启动",
                 idx, target["running"], device_present(adb, serial))
    else:
        log.info("启动实例 [%d] %s ...", idx, target["name"])
        rc, out = _run([ld, "launch", "--index", str(idx)], timeout=60.0)
        if rc != 0:
            sys.exit(f"[ERROR] 启动失败 (返回码 {rc}): {out}")
        log.info("已发送启动指令")

    if not wait_device(adb, serial, args.timeout):
        sys.exit(1)
    if not wait_boot(adb, serial, args.boot_timeout):
        sys.exit(1)

    print(f"\n[OK] 模拟器就绪: {serial}")

    if args.game:
        dev = settings.get("device", {})
        pkg = dev.get("game_package")
        act = dev.get("game_activity")
        if not pkg:
            log.warning("settings.yaml 里没配 device.game_package, 跳过启动游戏")
            return
        # 启动方式按优先级依次尝试:
        #   1) ldconsole runapp —— 雷电原生, 不依赖 Activity 是否 exported
        #   2) monkey          —— 通用做法, 按 LAUNCHER category 启动,
        #                        同样不需要知道 Activity 名
        #   3) am start -n     —— 兜底; 只有 exported 的 Activity 才能这样起
        #      (实测 .MainActivity 就起不来: SecurityException ... not exported)
        methods: list[tuple[str, list[str]]] = [
            ("ldconsole runapp", [ld, "runapp", "--index", str(idx),
                                  "--packagename", pkg]),
            ("monkey", [adb, "-s", serial, "shell", "monkey", "-p", pkg,
                        "-c", "android.intent.category.LAUNCHER", "1"]),
        ]
        if act:
            methods.append(("am start", [adb, "-s", serial, "shell", "am",
                                         "start", "-n", f"{pkg}/{act}"]))

        for label, cmd in methods:
            log.info("尝试 [%s] 启动 %s ...", label, pkg)
            rc, out = _run(cmd, timeout=60.0)
            print(f"  [{label}] {out}")
            if _launch_ok(rc, out):
                log.info("[%s] 启动指令成功", label)
                break
            log.warning("[%s] 未成功, 换下一种方式", label)
        else:
            log.error("几种方式都失败了 —— 请在模拟器里手动点开游戏")
            return

        time.sleep(args.game_wait)

        # 验证: 指令成功 ≠ 游戏真的起来了, 所以再确认一次前台.
        # 注意: 管道要写在【设备侧】, 让 grep 在模拟器里跑完只回传匹配行 ——
        #      如果把整个 dumpsys 拉回本地再过滤, 传输量巨大, 实测要 8~9 秒.
        _, out = _run([adb, "-s", serial, "shell",
                       "dumpsys window | grep mCurrentFocus"])
        focus = next((l.strip() for l in out.splitlines()
                      if "mCurrentFocus" in l), "")
        if pkg in focus:
            print(f"[OK] 游戏已到前台: {focus}")
        else:
            print(f"[!] 前台不是游戏: {focus or '(读不到)'}")

    # 从登录界面点进主城 (可单独用: 游戏已在登录界面时只加 --enter-city)
    if args.enter_city:
        city_cfg = yaml.safe_load(
            (ROOT / "config" / "city.yaml").read_text(encoding="utf-8")
        ) or {}
        tpl = city_cfg.get("my_room", "my_room")
        roi = city_cfg.get("home_roi")

        dev = build_device(adb, serial, settings)
        matcher = build_matcher(settings)
        actions = Actions(dev, matcher)
        actions.default_wait = float(
            settings.get("action", {}).get("default_wait", 0.6))

        if args.tap_x is not None and args.tap_y is not None:
            tap_pt = (args.tap_x, args.tap_y)
        else:
            w, h = dev.frame_size
            tap_pt = (w // 2, h // 2)

        if enter_main_city(dev, matcher, actions, tpl, roi, tap_pt,
                           settle=args.city_settle,
                           tap_gap=args.city_tap_gap,
                           max_taps=args.city_max_taps,
                           timeout=args.city_timeout,
                           black_ratio=args.city_black_ratio,
                           black_hold=args.city_black_hold):
            print(f"\n[OK] 已进入主城, 可以跑任务脚本了 (serial: {serial})")
        else:
            sys.exit(1)


if __name__ == "__main__":
    main()
