"""普通扫荡 (手动指定次数) —— 不使用焕发次数.

与 tools/sweep.py (消耗焕发次数) 相互独立, 互不影响.

流程 (每个目标):
  点卡片扫荡按钮(坐标) → 等弹窗 (默认次数=1)
  → 用模板定位加号, 点 (N-1) 次把次数设成 N (上限 50)
  → 点弹窗扫荡按钮(坐标)
  → 等结算标识 sweep_result 出现 → 点确认(坐标) → 下一个目标

用法:
    python tools/sweep_normal.py --from-main             # 干跑
    python tools/sweep_normal.py --from-main --live      # 实跑
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.coords import Coords                      # noqa: E402
from core.device import AdbDevice                   # noqa: E402
from core.env import detect_serial, find_adb        # noqa: E402
from core.frame import Frame                         # noqa: E402
from core.logger import get_logger                  # noqa: E402
from core.vision import Matcher                     # noqa: E402
from game.actions import Actions                    # noqa: E402
from game.recognizer import Recognizer              # noqa: E402
from game.states import State                       # noqa: E402

# 复用 sweep.py 中已验证的导航逻辑, 避免重复实现
from tools.sweep import (                           # noqa: E402
    TAB_CARD_GROUP,
    TOP_LABEL,
    TOP_SUB_GROUP,
    navigate_to_tab,
    select_tab,
    locate_entry,
    has_free_attempt,
    wait_until_stable,
)

log = get_logger("sweep_normal")


def load_yaml(path: Path) -> dict:
    result = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return result if isinstance(result, dict) else {}


def set_sweep_times(device, matcher, actions, target_times: int,
                    reset_taps: int = 0, plus_interval: float = 0.15,
                    max_times: int = 50) -> bool:
    """把弹窗中的扫荡次数设为 target_times.

    弹窗打开时默认次数 = 1, 所以只需点加号 (N-1) 次.
    若发现次数保留了上次的值, 可设 reset_taps>0 先点减号归零.

    加号/减号用小图标模板定位; 定位一次后复用坐标点击, 避免每点一次都截图.
    """
    if target_times < 1:
        log.warning("次数 %d 无效, 至少为 1", target_times)
        return False
    if target_times > max_times:
        log.warning("次数 %d 超过上限 %d, 截断为 %d", target_times, max_times, max_times)
        target_times = max_times

    # 1. 可选: 先点减号归零
    if reset_taps > 0:
        img = device.screencap()
        hit = matcher.find(img, "sweep_minus")
        if hit:
            pt = hit.center
            log.info("减号 %s, 归零点 %d 次", pt, reset_taps)
            for _ in range(reset_taps):
                actions.tap_point(pt[0], pt[1], wait=plus_interval)
        else:
            log.warning("未找到减号模板 sweep_minus, 跳过归零")

    # 2. 点加号 (N-1) 次: 默认 1 → target_times
    need = target_times - 1
    if need <= 0:
        log.info("目标次数为 1, 与默认一致, 无需调整")
        return True

    img = device.screencap()
    hit = matcher.find(img, "sweep_plus")
    if not hit:
        log.error("未找到加号模板 sweep_plus, 无法设置次数")
        return False

    pt = hit.center
    log.info("加号 %s → 点 %d 次 (1 → %d)", pt, need, target_times)
    for _ in range(need):
        actions.tap_point(pt[0], pt[1], wait=plus_interval)
    return True


def run_sweep_target(device, matcher, actions, recognizer, coords, t,
                     vals, state) -> bool:
    """执行单个扫荡目标的完整流程 (从切标签到点确认/返回).

    抽成函数是为了让「日常一条龙」(tools/daily.py) 能复用同一套逻辑,
    两边行为完全一致, 不各写一份.

    返回 True = 已扫荡; False = 跳过 (没次数 / 没找到入口 / 缺少配置).
    state 是跨目标共享的 {"top", "tab"}, 记录当前所在的顶层/次级页,
    用来避免重复切换页面.
    """
    wait_dialog = vals["wait_dialog"]
    wait_timeout = vals["wait_timeout"]
    poll_interval = vals["poll_interval"]

    slot = t.get("slot")
    top = t.get("top", "rift")
    tab = t.get("tab", "evolution")
    group = TAB_CARD_GROUP.get(tab, "evolution_cards")
    name = t.get("name", slot)
    times_n = int(t.get("times", 1))
    sub_group = TOP_SUB_GROUP.get(top, "rift_menu")
    # 可选的入口限定检索区域 (<tab>_roi), 避免同页其它模式被误匹配
    entry_roi = coords.box(f"{tab}_roi", sub_group)

    log.info("=== 目标: %s x%d 次 ===", name, times_n)

    # 0.1 切到顶层标签 (推荐 / 裂痕 ...)
    if top != state["top"]:
        tl = coords.center(TOP_LABEL.get(top) or top, "play_menu")
        if tl:
            log.info("点玩法页标签 [%s] %s", top, tl)
            actions.tap_point(*tl)
            time.sleep(wait_dialog)
        state["top"] = top

    # 0.2 进入/切到该次级页
    #     locate_entry 的目标(thetis)只定位不点击 —— 要先判断免费次数再决定点不点;
    #     其它目标在这里直接切过去, 保证 0.3 判断时【已经在正确的页面上】.
    entry_pt = None
    if t.get("locate_entry"):
        entry_pt = locate_entry(actions, coords, tab, sub_group, entry_roi)
        if entry_pt is None:
            log.warning("跳过 [%s]: 未在限定区域找到入口 [%s]", name, tab)
            return False
    elif tab != state["tab"]:
        select_tab(actions, coords, tab, group=sub_group, roi=entry_roi)
        state["tab"] = tab

    # 0.3 免费次数判断 (此时已在目标页面)
    if t.get("check_free"):
        free = has_free_attempt(actions, coords, group, slot)
        if free is False:
            log.info("「%s」没有免费次数, 取消本次", name)
            # 不用 ✗ 这类特殊字符: Windows 控制台默认 GBK, 编码会直接崩
            print(f"  [取消] {name}: 没有免费次数")
            return False

    # 0.4 点击进入 (仅 locate_entry 的目标需要 —— 前面只定位没点)
    if entry_pt is not None:
        log.info("点击入口 [%s] %s", tab, entry_pt)
        actions.tap_point(*entry_pt)
        time.sleep(wait_dialog)
        state["tab"] = tab

    # 0.5 等"已进入"该模式的标识 (若配置)
    entered_tpl = t.get("entered_template")
    if entered_tpl:
        try:
            ok = actions.wait_for(entered_tpl, timeout=wait_timeout,
                                  interval=poll_interval)
        except FileNotFoundError:
            ok = False
            log.warning("进入标识模板 [%s] 未采集, 跳过等待", entered_tpl)
        if not ok:
            log.warning("未检测到进入标识 [%s], 仍继续", entered_tpl)

    # 1. 点卡片扫荡按钮: 坐标优先, 无坐标则用图片(可限定区域)
    pt = coords.center(f"{slot}.sweep", group)
    if pt is None:
        sweep_tpl = t.get("sweep_template")
        if not sweep_tpl:
            log.warning("跳过 [%s]: 未采集 %s.%s.sweep, 且无 sweep_template",
                        name, group, slot)
            return False
        frame = Frame(actions.device.screencap())
        try:
            hit = actions.matcher.find(frame.image, sweep_tpl,
                                       roi=t.get("sweep_roi"))
        except FileNotFoundError:
            hit = None
            log.warning("扫荡按钮模板 [%s] 未采集", sweep_tpl)
        if not hit:
            log.warning("跳过 [%s]: 未匹配到扫荡按钮 [%s]", name, sweep_tpl)
            return False
        log.info("点扫荡按钮 [%s] score=%.2f @ %s",
                 sweep_tpl, hit.score, hit.center)
        actions.device.tap(*hit.center)
    else:
        log.info("点卡片扫荡按钮 %s", pt)
        actions.tap_point(pt[0], pt[1])
    time.sleep(wait_dialog)

    # 2. 设置次数: 免费次数类副本(觉醒/thetis)由系统填好, 整段跳过
    if t.get("auto_free_times"):
        log.info("[%s] 弹窗已默认选好剩余免费次数, 跳过设次数", name)
    elif bool(t.get("use_free_times", False)):
        img = device.screencap()
        try:
            free_available = matcher.exists(img, "sweep_check_on")
        except FileNotFoundError:
            free_available = False
            log.warning("模板 sweep_check_on 缺失, 无法判断免费次数")
        if free_available:
            log.info("[%s] 检测到免费次数(已勾选), 跳过设次数", name)
        else:
            log.info("[%s] 未检测到免费次数(应已用完), 按 times=%d 设次数",
                     name, times_n)
            if not set_sweep_times(
                device, matcher, actions, times_n,
                reset_taps=vals["reset_taps"],
                plus_interval=vals["plus_interval"],
                max_times=vals["max_times"],
            ):
                log.warning("[%s] 次数设置未成功, 仍继续", name)
    else:
        if not set_sweep_times(
            device, matcher, actions, times_n,
            reset_taps=vals["reset_taps"],
            plus_interval=vals["plus_interval"],
            max_times=vals["max_times"],
        ):
            log.warning("[%s] 次数设置未成功, 仍继续 (将按当前次数扫荡)", name)

    # 3. 点弹窗的扫荡按钮 (坐标)
    sp = coords.center("sweep", "sweep_dialog")
    if sp is None:
        log.error("缺少 sweep_dialog.sweep 坐标, 中止")
        return False
    log.info("点扫荡按钮 %s", sp)
    actions.tap_point(sp[0], sp[1])

    # 4. 等扫荡完成 (结算标识出现)
    try:
        appeared = actions.wait_for(vals["result_tpl"], timeout=wait_timeout,
                                    interval=poll_interval)
    except FileNotFoundError:
        log.warning("结算模板 [%s] 不存在, 退化为等待画面稳定",
                    vals["result_tpl"])
        appeared = wait_until_stable(device, timeout=wait_timeout,
                                     interval=poll_interval)
    if appeared:
        log.info("扫荡完成 (检测到 [%s])", vals["result_tpl"])
    else:
        log.warning("等待结算超时 (%.1fs), 仍尝试点确认", wait_timeout)

    # 5. 点确认 (+ 补充点击)
    okp = coords.center("ok", "sweep_dialog")
    if okp:
        # wait=0: 避免 tap_point 内部再叠加 default_wait,
        # 间隔严格由 confirm_interval 一处控制
        log.info("点确认 %s", okp)
        actions.tap_point(okp[0], okp[1], wait=0)

        # 补充点击: 扫荡完成后还会再弹一层(奖励/二次确认), 同位置再点
        # thetis 不需要 (点完确认直接回到无遮挡页面), 用目标级 0 关掉
        extra_n = int(t.get("extra_confirm_taps", vals["extra_taps"]))
        for i in range(extra_n):
            if vals["confirm_interval"] > 0:
                time.sleep(vals["confirm_interval"])
            log.info("补充点击 %d/%d %s (间隔 %.2fs)",
                     i + 1, extra_n, okp, vals["confirm_interval"])
            actions.tap_point(okp[0], okp[1], wait=0)

        # 末次点击后给界面留出响应时间
        time.sleep(vals["post_confirm_wait"])

    # 6. thetis: 回到无遮挡页面后, 按返回键回到推荐页
    if t.get("back_after"):
        log.info("[%s] 按返回键回到上层页面", name)
        actions.back()

    log.info("[%s] 流程结束", name)
    print()
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="普通扫荡 —— 手动指定次数 (不使用焕发次数)"
    )
    parser.add_argument("--live", action="store_true", help="真正执行点击 (默认干跑)")
    parser.add_argument(
        "--from-main", action="store_true",
        help="从主城自动导航到目标次级页",
    )
    parser.add_argument("--wait-dialog", type=float, default=1.2,
                        help="点卡片扫荡后等待弹窗的秒数")
    parser.add_argument("--wait-timeout", type=float, default=30.0,
                        help="等待扫荡完成(结算标识出现)的超时秒数")
    parser.add_argument("--poll-interval", type=float, default=1.0,
                        help="等待结算标识时的轮询间隔秒数")
    args = parser.parse_args()

    settings = load_yaml(ROOT / "config" / "settings.yaml")
    cfg = load_yaml(ROOT / "config" / "sweep_normal.yaml")
    coords = Coords()

    adb_path = find_adb()
    if not adb_path:
        sys.exit("[ERROR] 未找到 adb.exe")
    serial = settings.get("device", {}).get("serial")
    if not serial or serial == "auto":
        serial = detect_serial(adb_path)
    if not serial:
        sys.exit("[ERROR] 未探测到模拟器, 请确认雷电已启动")

    action_cfg = settings.get("action", {})
    vision_cfg = settings.get("vision", {})
    device = AdbDevice(
        adb_path=adb_path,
        serial=serial,
        dry_run=not args.live,
        tap_jitter=int(action_cfg.get("tap_jitter", 0)),
        min_tap_interval=float(action_cfg.get("min_tap_interval", 0.05)),
        rotate=int(settings.get("device", {}).get("rotate", 0)),
    )
    matcher = Matcher(
        threshold=float(vision_cfg.get("threshold", 0.85)),
        scales=vision_cfg.get("scales", [1.0]),
        method=vision_cfg.get("method", "ccoeff"),
    )
    actions = Actions(device, matcher)
    actions.default_wait = float(action_cfg.get("default_wait", 0.6))
    recognizer = Recognizer(matcher, ROOT / "config" / "states.yaml")

    targets = [t for t in cfg.get("targets", []) if t.get("enabled")]
    if not targets:
        sys.exit(
            "[ERROR] sweep_normal.yaml 中没有 enabled: true 的目标.\n"
            "        请把想刷的副本 enabled 改为 true, 并设好 times"
        )

    max_times = int(cfg.get("max_times", 50))
    reset_taps = int(cfg.get("reset_taps", 0))
    plus_interval = float(cfg.get("plus_interval", 0.15))
    result_tpl = cfg.get("result_template", "sweep_result")
    extra_taps = int(cfg.get("extra_confirm_taps", 0))
    confirm_interval = float(cfg.get("confirm_interval", 1.0))
    post_confirm_wait = float(cfg.get("post_confirm_wait", 1.0))

    print("=" * 60)
    print("  普通扫荡 (手动指定次数)"
          + ("  (实跑)" if args.live else "  (干跑, 不会真点击)"))
    print("=" * 60)
    for t in targets:
        print(f"  {t['name']}  x{t.get('times', 1)} 次")
    print(f"次数上限: {max_times} | 归零点击: {reset_taps}")
    print()

    # 启动时统一识别当前界面: 在主城就先点 play 进玩法页.
    # 不再只依赖 --from-main —— 否则不传该参数时, 脚本会误以为已在卡片页,
    # 上来就点卡片坐标, 在主城启动必然点错.
    frame = Frame(device.screencap())
    state, _ = recognizer.detect(frame)
    log.info("当前状态: %s", state.name)
    if state == State.MAIN:
        log.info("在主城 → 点击 play")
        if not actions.tap(frame, "play").success:
            log.warning("未找到 play 模板, 尝试继续 (可能已在玩法页)")
        time.sleep(args.wait_dialog)
    elif state == State.UNKNOWN:
        log.warning("未能识别当前界面, 按原流程继续")

    current_top = None
    current_tab = None
    if args.from_main:
        first = targets[0]
        navigate_to_tab(
            (device, matcher, actions, recognizer), coords,
            first.get("top", "rift"), first.get("tab", "evolution"),
        )
        current_top = first.get("top", "rift")
        current_tab = first.get("tab", "evolution")
        print()

    state = {"top": current_top, "tab": current_tab}
    vals = {
        "wait_dialog": args.wait_dialog,
        "wait_timeout": args.wait_timeout,
        "poll_interval": args.poll_interval,
        "reset_taps": reset_taps,
        "plus_interval": plus_interval,
        "max_times": max_times,
        "confirm_interval": confirm_interval,
        "extra_taps": extra_taps,
        "post_confirm_wait": post_confirm_wait,
        "result_tpl": result_tpl,
    }
    for t in targets:
        run_sweep_target(device, matcher, actions, recognizer, coords,
                         t, vals, state)
    device.close()
    if not args.live:
        print("干跑结束 (未真正点击)。确认后加 --live 实跑。")
    else:
        print("实跑结束。")


if __name__ == "__main__":
    main()
