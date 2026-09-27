"""消耗焕发次数 —— 按 config/sweep.yaml 自动扫荡副本.

流程 (每个目标):
  点卡片扫荡按钮(坐标) → 等弹窗
  → 判断是否已勾选(绿勾模板 sweep_check_on)
      未勾选 → 点复选框坐标勾上
  → 点弹窗的扫荡按钮(坐标)
  → 等结果 → 点确认(坐标) → 回列表, 处理下一个目标

用法:
    python tools/sweep.py             # 干跑, 只打印不点击 (默认, 安全)
    python tools/sweep.py --live      # 实跑
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.coords import Coords                     # noqa: E402
from core.device import AdbDevice                  # noqa: E402
from core.env import detect_serial, find_adb       # noqa: E402
from core.frame import Frame                       # noqa: E402
from core.logger import get_logger                 # noqa: E402
from core.vision import Matcher                    # noqa: E402
from game.actions import Actions                   # noqa: E402
from game.recognizer import Recognizer             # noqa: E402
from game.states import State                      # noqa: E402

log = get_logger("sweep")


# 次级选项(tab) → 卡片坐标组的映射
#   裂痕下有 3 个次级: 进化石(evolution) / 开花石(myth) / 资源觉醒(resource)
#   每个次级下方的卡片坐标存放在 coords.yaml 对应的组里
TAB_CARD_GROUP = {
    "evolution": "evolution_cards",
    "myth": "myth_cards",
    "resource": "resource_cards",
    "thetis": "thetis_cards",       # 忒提斯英雄传 (推荐页)
}

# 玩法页顶层标签 → 它在 play_menu 中的坐标名
TOP_LABEL = {
    "rift": "rift",
    "recommend": "recommend",
}
# 点完顶层标签后, 次级选项 / 玩法入口所在的坐标组
TOP_SUB_GROUP = {
    "rift": "rift_menu",
    "recommend": "recommend_menu",
}


def navigate_to_tab(ctx, coords: Coords, top: str = "rift",
                    tab: str = "evolution", wait: float = 1.5) -> None:
    """从主城导航到「顶层标签 → 次级/入口」的页面.

    步骤: 主城 → 点 play → 点顶层标签(推荐/裂痕) → 点该入口
    """
    device, matcher, actions, recognizer = ctx

    frame = Frame(device.screencap())
    state, _ = recognizer.detect(frame)
    log.info("当前状态: %s", state.name)

    if state == State.MAIN:
        log.info("在主城 → 点击 play")
        if not actions.tap(frame, "play").success:
            log.warning("未找到 play 模板, 尝试继续 (可能已在玩法页)")
        time.sleep(wait)
    elif state == State.UNKNOWN:
        log.warning("未能识别当前界面, 仍尝试按主城流程继续")

    # 点顶层标签 (推荐 / 裂痕 / ...)
    label = TOP_LABEL.get(top, top)
    tl = coords.center(label, "play_menu")
    if tl:
        log.info("点玩法页标签 [%s] %s", top, tl)
        actions.tap_point(*tl)
        time.sleep(wait)

    # 点该标签下的次级选项 / 具体玩法入口
    select_tab(actions, coords, tab,
               group=TOP_SUB_GROUP.get(top, "rift_menu"), wait=wait)
    log.info("导航完成, 应已停在 [%s/%s] 页面", top, tab)


def _find_entry(actions, coords: Coords, tab: str, group: str,
                roi=None):
    """找入口标识, 返回 MatchResult 或 None (不点击)."""
    try:
        frame = Frame(actions.device.screencap())
        return actions.matcher.find(frame.image, tab, roi=roi)
    except FileNotFoundError:
        log.warning("模板 [%s] 不存在, 请先采集", tab)
        return None


def locate_entry(actions, coords: Coords, tab: str, group: str = "rift_menu",
                 roi=None):
    """在限定区域里定位入口, 返回中心点 (只定位, 不点击).

    为什么要和"点击"分开: thetis 要先看免费次数(n/3)再决定点不点,
    所以得先拿到位置, 判断完再点.
    """
    pt = coords.center(tab, group)
    if pt is not None:
        return pt
    hit = _find_entry(actions, coords, tab, group, roi)
    if hit:
        log.info("限定区域内找到入口 [%s] score=%.2f @ %s",
                 tab, hit.score, hit.center)
        return hit.center
    log.warning("限定区域内未找到入口 [%s] (区域 %s)", tab, roi or "全屏")
    return None


def select_tab(actions, coords: Coords, tab: str, group: str = "rift_menu",
               wait: float = 1.0, roi=None) -> bool:
    """点某个次级选项 / 具体玩法入口.

    - 有坐标 → 直接点坐标 (稳, 位置固定的文字按钮);
    - 无坐标 → 模板匹配: 用同名模板在 roi(可限定) 内定位后点击.
      限定区域很重要: 推荐页有多种模式, 全屏匹配容易张冠李戴.
    """
    pt = coords.center(tab, group)
    if pt is not None:
        log.info("点次级选项 [%s] %s (组=%s)", tab, pt, group)
        actions.tap_point(*pt)
        time.sleep(wait)
        return True

    hit = _find_entry(actions, coords, tab, group, roi)
    if hit:
        log.info("点入口 [%s] (模板匹配 score=%.2f @ %s)",
                 tab, hit.score, hit.center)
        actions.device.tap(*hit.center)
        time.sleep(wait)
        return True

    log.warning("未找到入口 [%s] (组=%s, 区域=%s)", tab, group, roi or "全屏")
    return False


def has_free_attempt(actions, coords: Coords, card_group: str, slot: str,
                     threshold: float = 0.85) -> bool | None:
    """判断是否还有免费次数 (n/3 这类指示).

    两种判定方式, 二选一配在 coords.yaml 的 <card_group>.<slot>:

      free_left (推荐): 匹配到 = 【还有】免费次数;
                        没匹配到 = 已用完 / 无法确认 → 判为没有.
                        为什么推荐: 安全. 万一套模板没匹配上, 结果是"不扫",
                        而不会在没次数时误扫、把挑战券(真货币)扣掉.

      free_empty      : 匹配到 = 【没有】免费次数; 没匹配到 → 当作还有.
                        风险: 若该模板没匹配上, 会误判成还有次数而继续扫荡.

      free_roi        : 两种方式共用的限定检索区域.

    返回 True=还有免费次数, False=没有, None=未配置(保守当作有).
    """
    slot_cfg = (coords.data.get(card_group) or {}).get(slot) or {}
    roi = coords.box(f"{slot}.free_roi", card_group)
    frame = Frame(actions.device.screencap())

    # 方式一 (推荐): 正向确认"还有次数"
    left_tpl = slot_cfg.get("free_left")
    if left_tpl:
        try:
            hit = actions.matcher.find(frame.image, left_tpl, roi=roi,
                                       threshold=threshold)
        except FileNotFoundError:
            log.warning("模板 [%s] 不存在, 无法判断剩余次数", left_tpl)
            return None
        if hit:
            log.info("[%s.%s] 命中剩余次数模板 %s (score=%.2f) → 还有免费次数",
                     card_group, slot, left_tpl, hit.score)
            return True
        log.info("[%s.%s] 未命中剩余次数模板 %s → 判为没有免费次数(不冒险扫)",
                 card_group, slot, left_tpl)
        return False

    # 方式二: 反向确认"已用完"
    empty_tpl = slot_cfg.get("free_empty")
    if empty_tpl:
        try:
            hit = actions.matcher.find(frame.image, empty_tpl, roi=roi,
                                       threshold=threshold)
        except FileNotFoundError:
            log.warning("模板 [%s] 不存在, 无法判断剩余次数", empty_tpl)
            return None
        if hit:
            log.info("[%s.%s] 命中无免费模板 %s (score=%.2f) → 没有免费次数",
                     card_group, slot, empty_tpl, hit.score)
            return False
        log.info("[%s.%s] 未命中无免费模板 → 还有免费次数", card_group, slot)
        return True

    log.warning("[%s.%s] 未配置 free_left / free_empty, 默认当作有免费次数",
                card_group, slot)
    return None


def wait_until_stable(device, timeout: float = 30.0, interval: float = 1.0,
                      diff_threshold: float = 2.0,
                      stable_rounds: int = 2) -> bool:
    """等待画面稳定 —— 连续若干次采样差异都极小, 认为动画/进度已结束.

    为什么用它判断扫荡完成:
      实测「确认按钮在扫荡期间就一直存在且不变化」, 所以不能靠"等按钮出现";
      而扫荡进行中通常有 loading 动画(画面持续变化), 完成后静止.
      判断画面稳定是通用做法, 不需要额外采集模板.

    返回 True = 画面已稳定(认为完成); False = 超时.
    """
    import cv2

    end = time.time() + timeout
    prev = None
    stable = 0
    while time.time() < end:
        img = device.screencap()
        if prev is not None:
            diff = float(cv2.absdiff(prev, img).mean())
            if diff < diff_threshold:
                stable += 1
                if stable >= stable_rounds:
                    log.info("画面已稳定 (连续 %d 次差异 < %.1f)", stable, diff_threshold)
                    return True
            else:
                stable = 0
        prev = img
        time.sleep(interval)
    log.warning("等待画面稳定超时 (%.1fs)", timeout)
    return False


def load_yaml(path: Path) -> dict:
    result = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return result if isinstance(result, dict) else {}


def main() -> None:
    parser = argparse.ArgumentParser(description="消耗焕发次数 —— 自动扫荡副本")
    parser.add_argument(
        "--live", action="store_true",
        help="真正执行点击 (不加此参数则只打印不点击)",
    )
    parser.add_argument(
        "--wait-dialog", type=float, default=1.2,
        help="点卡片扫荡后等待弹窗出现的秒数",
    )
    parser.add_argument(
        "--wait-result", type=float, default=2.5,
        help="无结果页模板时的固定等待秒数 (兜底用)",
    )
    parser.add_argument(
        "--wait-timeout", type=float, default=30.0,
        help="等待扫荡完成(结果页出现)的超时秒数",
    )
    parser.add_argument(
        "--poll-interval", type=float, default=1.0,
        help="等待结果页时的轮询间隔秒数",
    )
    parser.add_argument(
        "--from-main", action="store_true",
        help="从主城自动导航到裂痕-进化石卡片页 (不用手动进页面)",
    )
    args = parser.parse_args()

    settings = load_yaml(ROOT / "config" / "settings.yaml")
    sweep_cfg = load_yaml(ROOT / "config" / "sweep.yaml")
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

    # 先解析目标副本 (下面的自动导航需要用到第一个目标的 tab)
    targets = [t for t in sweep_cfg.get("targets", []) if t.get("enabled")]
    if not targets:
        sys.exit(
            "[ERROR] sweep.yaml 中没有 enabled: true 的目标.\n"
            "        请把想刷的副本 enabled 改为 true"
        )

    # 记录当前所在的次级页, 避免连续刷同一个次级时重复点 tab
    current_tab = None
    if args.from_main:
        first_tab = targets[0].get("tab", "evolution")
        navigate_to_tab((device, matcher, actions, recognizer), coords, first_tab)
        current_tab = first_tab
        print()

    max_daily = int(sweep_cfg.get("max_daily", 10))
    auto_check = bool(sweep_cfg.get("auto_check_remaining", True))
    extra_taps = int(sweep_cfg.get("extra_confirm_taps", 1))
    confirm_interval = float(sweep_cfg.get("confirm_interval", 0.5))

    print("=" * 60)
    print("  消耗焕发次数" + ("  (实跑)" if args.live else "  (干跑, 不会真点击)"))
    print("=" * 60)
    print(f"目标副本  : {', '.join(t['name'] for t in targets)}")
    print(f"次数上限  : {max_daily} 次 (所有副本共用)")
    print(f"勾选策略  : {'自动勾选剩余次数' if auto_check else '不自动勾选'}")
    print()

    for t in targets:
        slot = t.get("slot")
        tab = t.get("tab", "evolution")
        group = TAB_CARD_GROUP.get(tab, "evolution_cards")
        name = t.get("name", slot)

        # 切换次级页 (仅当与当前次级不同时)
        if tab != current_tab:
            select_tab(actions, coords, tab)
            current_tab = tab

        pt = coords.center(f"{slot}.sweep", group)
        if pt is None:
            log.warning(
                "跳过 [%s]: 未采集坐标 %s.%s.sweep (先用 --roi-mode 采集)",
                name, group, slot,
            )
            continue

        log.info("=== 目标: %s ===", name)
        # 1. 点卡片上的扫荡按钮
        log.info("点卡片扫荡按钮 %s", pt)
        actions.tap_point(pt[0], pt[1])
        time.sleep(args.wait_dialog)

        # 2. 判断是否已勾选 (绿勾模板)
        img = device.screencap()
        checked = False
        try:
            checked = matcher.exists(img, "sweep_check_on")
        except FileNotFoundError:
            log.warning("模板 sweep_check_on 不存在, 跳过勾选判断")

        if checked:
            log.info("已勾选 (检测到绿勾) → 直接扫荡")
        elif auto_check:
            cb = coords.center("checkbox", "sweep_dialog")
            if cb:
                log.info("未勾选 → 点复选框 %s", cb)
                actions.tap_point(cb[0], cb[1])
                time.sleep(0.6)
            else:
                log.warning("未勾选但需要勾选, 且缺少 checkbox 坐标")

        # 3. 点弹窗的扫荡按钮
        sp = coords.center("sweep", "sweep_dialog")
        if sp is None:
            log.error("缺少 sweep_dialog.sweep 坐标, 中止")
            return
        log.info("点扫荡按钮 %s", sp)
        actions.tap_point(sp[0], sp[1])

        # 4. 等扫荡完成 —— 确认按钮期间一直存在且不变(不能作为依据);
        #    画面稳定也不可靠(结算页仍有动画). 因此等「结算标识图」出现.
        result_tpl = sweep_cfg.get("result_template", "sweep_result")
        appeared = False
        try:
            appeared = actions.wait_for(
                result_tpl,
                timeout=args.wait_timeout,
                interval=args.poll_interval,
            )
        except FileNotFoundError:
            log.warning(
                "结算标识模板 [%s] 不存在, 退化为等待画面稳定", result_tpl
            )
            appeared = wait_until_stable(
                device,
                timeout=args.wait_timeout,
                interval=args.poll_interval,
            )

        if appeared:
            log.info("扫荡完成 (检测到结算标识 [%s])", result_tpl)
        else:
            log.warning(
                "等待扫荡完成超时 (%.1fs), 仍尝试点确认", args.wait_timeout
            )

        # 5. 点确认, 结束本次扫荡
        ok = coords.center("ok", "sweep_dialog")
        if ok:
            # wait=0: 间隔严格由 confirm_interval 控制, 不叠加 default_wait
            log.info("点确认 %s", ok)
            actions.tap_point(ok[0], ok[1], wait=0)

            # 6. 补充点击: 结算后还会再弹一层, 同位置再点 (不是双击)
            for i in range(extra_taps):
                if confirm_interval > 0:
                    time.sleep(confirm_interval)
                log.info(
                    "补充点击 %d/%d %s (间隔 %.2fs)",
                    i + 1, extra_taps, ok, confirm_interval,
                )
                actions.tap_point(ok[0], ok[1], wait=0)

            # 末次点击后给界面留出响应时间
            time.sleep(1.0)
        else:
            log.warning("缺少 sweep_dialog.ok 坐标, 跳过确认")

        log.info("[%s] 流程结束", name)
        print()

    device.close()
    if not args.live:
        print("干跑结束 (未真正点击)。确认上面坐标无误后, 加 --live 实跑。")
    else:
        print("实跑结束。")


if __name__ == "__main__":
    main()
