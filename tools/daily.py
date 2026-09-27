"""日常一条龙: 按 config/daily.yaml 的 steps 顺序依次完成日常.

步骤顺序由 config/daily.yaml 决定 (可增删 / 调序), 当前是:
  ① 主城任务       —— inn + 宝箱 + 礼物(friend/公会/商店) + 拖动找建筑.
                      必须在【主城】做, 所以排最前, 且不能先点 play
  ② 忒提斯英雄传   —— 查 n/3 免费次数, 有则扫, 无则跳过
  ③ 消耗焕发次数   —— 要刷哪些副本在 daily.yaml 的 targets 里单独列
  ④ 额外扫荡副本   —— 【可选, 默认关】焕发用完后按指定次数再扫一遍副本;
                      扫哪些看 sweep_normal.yaml 的 enabled, 次数看本步骤 times
  ⑤ 觉醒副本       —— 查免费次数(正向确认), 有则扫, 无则跳过
  ⑥ 圆形角斗场     —— 交给 tools/pvp.py 子进程, 消耗所有挑战次数(没票自动停)
  ⑦ 每日任务一键领取 —— 【收尾, 必须放最后】奖励取决于前面各任务是否完成,
                      没做完就领不全; 函数内部会先一路返回主城

两个"必须在主城"的步骤(① ⑥)都排在"点 play 进玩法页"之前处理;
首次进玩法页前会把起点规整到主城 —— 启动时可能还停在上次运行留下的
角斗场 / 裂痕页等内层页面, 直接点 play 会找不到按钮.
任一步没次数 / 失败 → 跳过, 继续下一步.

用法:
    python tools/daily.py            # 干跑
    python tools/daily.py --live     # 实跑
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

from core.coords import Coords                      # noqa: E402
from core.device import AdbDevice                   # noqa: E402
from core.env import detect_serial, find_adb        # noqa: E402
from core.frame import Frame                        # noqa: E402
from core.logger import get_logger                  # noqa: E402
from core.vision import Matcher                     # noqa: E402
from game.actions import Actions                    # noqa: E402
from game.recognizer import Recognizer              # noqa: E402
from game.states import State                       # noqa: E402

from tools.sweep import (                           # noqa: E402
    TAB_CARD_GROUP,
    TOP_LABEL,
    TOP_SUB_GROUP,
    select_tab,
    wait_until_stable,
)
from tools.sweep_normal import run_sweep_target     # noqa: E402
from tools.city import (                            # noqa: E402
    back_to_home,
    ensure_main_city,
    run_city_tasks,
    run_daily_task_receive,
)

log = get_logger("daily")


def load_yaml(path: Path) -> dict:
    result = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return result if isinstance(result, dict) else {}


def run_radiant_target(device, matcher, actions, coords, t, vals,
                       state) -> bool:
    """消耗焕发次数的单个目标: 还有焕发次数才扫, 没有就跳过.

    与免费次数副本的区别:
      焕发靠弹窗里的「设置为剩余焕发次数」复选框(绿勾 sweep_check_on)判断.
      次数用完后该选项会整个消失 —— 此时不扫, 也【不去点复选框坐标】
      (选项消失后点那个坐标会误触别的按钮).
    """
    wait_dialog = vals["wait_dialog"]
    wait_timeout = vals["wait_timeout"]
    poll_interval = vals["poll_interval"]

    slot = t.get("slot")
    top = t.get("top", "rift")
    tab = t.get("tab", "evolution")
    group = TAB_CARD_GROUP.get(tab, "evolution_cards")
    name = t.get("name", slot)
    sub_group = TOP_SUB_GROUP.get(top, "rift_menu")

    log.info("=== 焕发目标: %s ===", name)

    # 切顶层标签 / 次级页
    if top != state["top"]:
        tl = coords.center(TOP_LABEL.get(top) or top, "play_menu")
        if tl:
            log.info("点玩法页标签 [%s] %s", top, tl)
            actions.tap_point(*tl)
            time.sleep(wait_dialog)
        state["top"] = top
    if tab != state["tab"]:
        select_tab(actions, coords, tab, group=sub_group)
        state["tab"] = tab

    # 点卡片扫荡按钮
    pt = coords.center(f"{slot}.sweep", group)
    if pt is None:
        log.warning("跳过 [%s]: 未采集 %s.%s.sweep", name, group, slot)
        return False
    log.info("点卡片扫荡按钮 %s", pt)
    actions.tap_point(pt[0], pt[1])
    time.sleep(wait_dialog)

    # 判断还有没有焕发次数 (绿勾)
    try:
        has_radiant = matcher.exists(device.screencap(), "sweep_check_on")
    except FileNotFoundError:
        has_radiant = False
        log.warning("模板 sweep_check_on 缺失, 无法判断焕发次数")
    if not has_radiant:
        log.info("[%s] 未检测到焕发勾选(次数应已用完), 跳过", name)
        print(f"  [跳过] {name}: 没有焕发次数")
        return False
    log.info("[%s] 检测到焕发次数(已勾选) → 直接扫掉剩余次数", name)

    # 点弹窗扫荡
    sp = coords.center("sweep", "sweep_dialog")
    if sp is None:
        log.error("缺少 sweep_dialog.sweep 坐标, 跳过")
        return False
    log.info("点扫荡按钮 %s", sp)
    actions.tap_point(sp[0], sp[1])

    # 等完成
    try:
        appeared = actions.wait_for(vals["result_tpl"], timeout=wait_timeout,
                                    interval=poll_interval)
    except FileNotFoundError:
        log.warning("结算模板 [%s] 不存在, 退化为等待画面稳定",
                    vals["result_tpl"])
        appeared = wait_until_stable(device, timeout=wait_timeout,
                                     interval=poll_interval)
    log.info("扫荡完成" if appeared else "等待结算超时, 仍尝试点确认")

    # 点确认 + 补充点击
    okp = coords.center("ok", "sweep_dialog")
    if okp:
        log.info("点确认 %s", okp)
        actions.tap_point(okp[0], okp[1], wait=0)
        for i in range(int(vals["extra_taps"])):
            if vals["confirm_interval"] > 0:
                time.sleep(vals["confirm_interval"])
            log.info("补充点击 %d/%d", i + 1, int(vals["extra_taps"]))
            actions.tap_point(okp[0], okp[1], wait=0)
        time.sleep(vals["post_confirm_wait"])

    log.info("[%s] 流程结束", name)
    print()
    return True


def run_colosseum_step(step: dict, live: bool) -> None:
    """圆形角斗场: 交给 tools/pvp.py 执行 (它自带"没票自动停")."""
    cmd = [
        sys.executable, str(ROOT / "tools" / "pvp.py"),
        "--repeat", str(int(step.get("repeat", 10))),
        "--opponent", str(int(step.get("opponent", 3))),
    ]
    if live:
        cmd.append("--live")
    log.info("执行角斗场: %s", " ".join(cmd))
    print()
    subprocess.run(cmd, cwd=str(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description="日常一条龙")
    parser.add_argument("--live", action="store_true",
                        help="真正点击 (默认干跑)")
    args = parser.parse_args()

    daily_cfg = load_yaml(ROOT / "config" / "daily.yaml")
    sn_cfg = load_yaml(ROOT / "config" / "sweep_normal.yaml")
    city_cfg = load_yaml(ROOT / "config" / "city.yaml")
    settings = load_yaml(ROOT / "config" / "settings.yaml")
    coords = Coords()

    steps = [s for s in daily_cfg.get("steps", []) if s.get("enabled")]
    if not steps:
        sys.exit("[ERROR] config/daily.yaml 里没有 enabled 的步骤")

    adb_path = find_adb()
    if not adb_path:
        sys.exit("[ERROR] 未找到 adb.exe")
    serial = settings.get("device", {}).get("serial")
    if not serial or serial == "auto":
        serial = detect_serial(adb_path)
    if not serial:
        sys.exit("[ERROR] 未探测到模拟器")

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

    print("=" * 60)
    print("  日常一条龙" + ("  (实跑)" if args.live else "  (干跑)"))
    print("=" * 60)
    for s in steps:
        print(f"  {s.get('name')}  [{s.get('kind')}]")
    print()

    # 当前所在界面 (决定后面要不要先点 play)
    frame = Frame(device.screencap())
    state_detect, _ = recognizer.detect(frame)
    log.info("当前状态: %s", state_detect.name)

    # sweep_normal 的配置值, 供 thetis / awakening 复用
    vals = {
        "wait_dialog": float(sn_cfg.get("wait_dialog", 1.2)),
        "wait_timeout": float(sn_cfg.get("wait_timeout", 30.0)),
        "poll_interval": float(sn_cfg.get("poll_interval", 1.0)),
        "reset_taps": int(sn_cfg.get("reset_taps", 0)),
        "plus_interval": float(sn_cfg.get("plus_interval", 0.15)),
        "max_times": int(sn_cfg.get("max_times", 50)),
        "confirm_interval": float(sn_cfg.get("confirm_interval", 0.2)),
        "extra_taps": int(sn_cfg.get("extra_confirm_taps", 1)),
        "post_confirm_wait": float(sn_cfg.get("post_confirm_wait", 1.0)),
        "result_tpl": sn_cfg.get("result_template", "sweep_result"),
    }
    # 跨步骤共享"当前在哪个顶层/次级页", 避免重复切换
    nav_state = {"top": None, "tab": None}
    sn_targets = {t.get("name"): t for t in sn_cfg.get("targets", [])}

    entered_play = False
    for step in steps:
        kind = step.get("kind")
        print(f"\n---------- {step.get('name')} ({kind}) ----------")

        # 主城任务: 必须在【首页】做, 不能先点 play 进玩法页
        if kind == "city":
            run_city_tasks(device, matcher, actions, city_cfg, coords)
            continue

        # 收尾任务: 每日任务一键领取.
        #   必须在【首页】做, 且必须排在【全部任务之后】—— 它的奖励取决于
        #   前面各任务是否完成, 没做完就领不全.
        #   函数内部会先一路返回主城 (此处多半还停在玩法页 / 角斗场里).
        if kind == "daily_task":
            run_daily_task_receive(device, matcher, actions, coords, city_cfg)
            continue

        # 其余步骤都在玩法页 —— 首次进入时把页面切到玩法页
        if not entered_play:
            entered_play = True
            if state_detect == State.PLAY_MENU:
                log.info("启动时已在玩法页, 跳过切页")
            else:
                # 不在主城最常见的原因是: 上次运行把我们留在了角斗场 / 裂痕页
                # 等内层页面. 那种情况下直接"点 play"会找不到按钮, 后面的扫荡
                # 步骤还会在错误的页面上乱找 → 所以先把起点规整到主城.
                # (本来就在主城时 back_to_home 一次按键都不会发, 无副作用.)
                if state_detect != State.MAIN:
                    log.info("启动时不在主城 (%s) → 先返回主城",
                             state_detect.name)
                    back_to_home(device, matcher, actions, city_cfg,
                                 label="进入玩法页 前置", max_tries=8)
                    if not ensure_main_city(device, matcher, actions,
                                            city_cfg):
                        log.warning("未能确认回到主城, 仍尝试点 play")
                log.info("在主城 → 点击 play 进入玩法页")
                if not actions.tap(Frame(device.screencap()), "play").success:
                    log.warning("未找到 play 模板, 尝试继续")
                time.sleep(1.5)

        if kind in ("thetis", "awakening"):
            tname = step.get("name")
            t = sn_targets.get(tname)
            if not t:
                log.warning("在 sweep_normal.yaml 里没找到目标 [%s], 跳过",
                            tname)
                continue
            run_sweep_target(device, matcher, actions, recognizer, coords,
                             t, vals, nav_state)

        elif kind == "radiant":
            targets = step.get("targets") or []
            if not targets:
                log.warning("焕发步骤没有配置 targets, 跳过")
                continue
            for t in targets:
                run_radiant_target(device, matcher, actions, coords,
                                   t, vals, nav_state)

        elif kind == "extra_sweep":
            # 额外扫荡 (可选): 焕发用完后, 按【指定次数】再扫一遍副本.
            #   扫哪些: config/sweep_normal.yaml 里 enabled: true 的目标;
            #   次数: 本步骤的 times 统一覆盖, 写成 null 则各用自身的 times.
            #   走的是普通扫荡, 不做任何免费 / 焕发次数判断.
            named = [t for t in sn_cfg.get("targets", []) if t.get("enabled")]
            if not named:
                log.warning("sweep_normal.yaml 里没有 enabled: true 的目标, "
                            "跳过额外扫荡")
                continue
            per = step.get("times")
            log.info("额外扫荡 %d 个副本%s", len(named),
                     f", 统一 x{int(per)} 次" if per is not None
                     else ", 各用自身 times")
            for t in named:
                tt = dict(t)
                if per is not None:
                    tt["times"] = int(per)
                run_sweep_target(device, matcher, actions, recognizer,
                                 coords, tt, vals, nav_state)

        elif kind == "colosseum":
            run_colosseum_step(step, args.live)

        else:
            log.warning("未知步骤类型 [%s], 跳过", kind)

    device.close()
    print()
    if not args.live:
        print("干跑结束 (未真正点击)。确认后加 --live 实跑。")
    else:
        print("日常一条龙完成。")


if __name__ == "__main__":
    main()
