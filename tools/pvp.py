"""进入 PVP 圆形角斗场.

流程:
  主城 → 点 play → 玩法页
       → 点 pvp 标签 (958,178)
       → 校验: 用 pvp 模板确认真的到了 PVP 页 (没到则重试一次)
       → 点圆形角斗场卡片 (241,473) 进入

用法:
    python tools/pvp.py --from-main             # 干跑
    python tools/pvp.py --from-main --live      # 实跑
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.coords import Coords                   # noqa: E402
from core.device import AdbDevice                # noqa: E402
from core.env import detect_serial, find_adb     # noqa: E402
from core.frame import Frame                     # noqa: E402
from core.logger import get_logger               # noqa: E402
from core.vision import Color, Matcher           # noqa: E402
from game.actions import Actions                 # noqa: E402
from game.recognizer import Recognizer           # noqa: E402
from game.states import State                    # noqa: E402

# 复用 sweep.py 的"等画面稳定"判断 (战斗中有动画, 静止即结束)
from tools.sweep import wait_until_stable        # noqa: E402

log = get_logger("pvp")

# PVP 页面识别模板 (config/states.yaml 里 PVP 状态所用)
PVP_TEMPLATE = "pvp"


def load_yaml(path: Path) -> dict:
    result = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return result if isinstance(result, dict) else {}


def ticket_left(device, matcher) -> bool | None:
    """读入场券: True=还有票, False=已用完(0/5), None=读不到."""
    try:
        img = device.screencap()
        empty = matcher.exists(img, "ticket_empty")
    except FileNotFoundError:
        log.warning("模板 ticket_empty 不存在, 跳过读票")
        return None
    return not empty


def _available_templates(matcher, img, names) -> list:
    """返回真实存在的模板名 (模板文件缺失的会被跳过)."""
    usable = []
    for name in names or []:
        try:
            matcher.exists(img, name)     # 只为探测模板文件是否存在
            usable.append(name)
        except FileNotFoundError:
            continue
    return usable


def wait_battle_done(device, matcher, hud_templates, timeout: float,
                     interval: float, start_timeout: float = 20.0,
                     hold: float = 3.0, result_templates=None,
                     result_color: dict | None = None,
                     hud_roi=None) -> bool:
    """等战斗结束. 多种策略, 按顺序优先采用第一个可用的.

    策略 0 (最优): 结算按钮的【黄色背景占比】达到阈值 = 打完
      为什么用颜色而不是模板: 确认按钮是"文字+黄色背景"的长方形, 而纯色均匀块
      拿去做模板匹配是失效的(整块无差异, CCOEFF 归一化会退化, 可能到处都匹配).
      改用颜色占比则完全不看文字, 且被段位弹窗遮挡一半也无所谓 ——
      剩下那半仍有约 50% 黄像素, 把占比阈值设低即可.

    策略 A: 等【结算画面的元素出现】= 打完
      为什么改成这个: 实测发现战斗 HUD 元素(统计按钮 dps/heal/hp)在结算画面上
      【依然存在】(结算画面本身就会显示 dps/heal/hp 数据), 所以"等它消失"
      永远等不到. 而结算画面会停留较长时间(实测几十秒), 等它"出现"没有时序竞争.
      采集对象: 结算画面的"确认"按钮 / 固定出现的奖励元素.

    策略 B (兜底): 战斗 HUD【出现 → 连续消失】
      仅当没采集结算模板时使用. 要求目标元素是真正"战斗专属"的
      (战斗结束就消失), 否则会退化成超时.
      两段式的必要性: 只等"消失"不行 —— 点完弹窗战斗后画面还没切进战斗,
      HUD 本来就不在, 会立刻误判成已结束.

    "任意一个"的含义: 倍速按钮 x1/x2、结算与段位弹窗各自形态不同,
      同类都采全, 命中任意一个即算命中.

    两种模板都没采集时, 退化为等待画面稳定 (不精确, 仅兜底).
    """
    img0 = device.screencap()

    # ---- 策略 0: 结算按钮黄色背景占比 ----
    if result_color and result_color.get("roi"):
        roi = result_color["roi"]
        rgb = tuple(result_color.get("rgb") or (255, 255, 0))
        tol = int(result_color.get("tol", 40))
        need = float(result_color.get("ratio", 0.25))
        log.info("用结算按钮颜色判定: roi=%s rgb=%s tol=%.0f 占比阈值=%.2f",
                 roi, rgb, tol, need)
        end = time.time() + timeout
        while time.time() < end:
            img = device.screencap()
            got = Color.ratio_in_roi(img, roi, rgb, tol)
            if got >= need:
                log.info("检测到结算按钮黄色 (占比 %.2f >= %.2f) → 战斗结束",
                         got, need)
                return True
            time.sleep(interval)
        log.warning("等待结算按钮颜色超时 (%.1fs)", timeout)
        return False

    # ---- 策略 A: 等结算画面元素出现 ----
    result_names = _available_templates(matcher, img0, result_templates)
    if result_names:
        log.info("用结算画面模板判定 (等出现): %s", result_names)
        end = time.time() + timeout
        while time.time() < end:
            img = device.screencap()
            for name in result_names:
                if matcher.exists(img, name):
                    log.info("检测到结算画面 [%s] → 战斗结束", name)
                    return True
            time.sleep(interval)
        log.warning("等待结算画面超时 (%.1fs)", timeout)
        return False

    # ---- 策略 B: 战斗 HUD 出现 → 连续消失 ----
    usable = _available_templates(matcher, img0, hud_templates)
    if not usable:
        log.warning("HUD/结算模板均未采集, 退化为等待画面稳定")
        return wait_until_stable(device, timeout=timeout, interval=interval)

    log.info("用 %d 个战斗 HUD 模板判断: %s", len(usable), usable)

    def any_hit(img=None):
        """返回 (模板名, 得分, 中心点) 或 None.

        只在 battle_hud_roi 限定的区域里找: 统计按钮位置固定,
        不限制的话会全屏乱匹配(结算画面 / 战斗动作区都能凑出高分).
        实测结算画面与战斗画面的统计按钮不在同一位置, 所以限定区域后
        "匹配到" 就等价于 "在战斗画面", 不需要再校验颜色.
        """
        if img is None:
            img = device.screencap()
        for name in usable:
            hit = matcher.find(img, name, roi=hud_roi)
            if hit:
                return name, hit.score, hit.center
        return None

    # 阶段 1: 等 HUD 出现 = 确认真的进入战斗画面
    end = time.time() + start_timeout
    while time.time() < end:
        res = any_hit()
        if res:
            log.info("检测到战斗 HUD [%s] (score=%.2f @ %s) "
                     "→ 确认进入战斗画面", res[0], res[1], res[2])
            break
        time.sleep(interval)
    else:
        log.warning(
            "%.1fs 内未检测到战斗 HUD, 可能没打起来, 退化为等待画面稳定",
            start_timeout,
        )
        return wait_until_stable(device, timeout=timeout, interval=interval)

    # 阶段 2: HUD 必须【连续缺席 hold 秒】才算打完
    #         (中途看到一次就重新计时, 避免动画/特效造成的瞬时遮挡误判)
    end = time.time() + timeout
    absent_since = None
    started2 = time.time()
    last_log = 0.0
    while time.time() < end:
        img = device.screencap()
        hit = any_hit(img)
        if hit is None:
            if absent_since is None:
                absent_since = time.time()
            elif time.time() - absent_since >= hold:
                log.info("战斗 HUD 已连续缺席 %.1fs → 战斗结束", hold)
                return True
        else:
            absent_since = None

        # 进度日志: 便于判断是"战斗真的长"/"元素仍在"还是"误匹配"
        elapsed2 = time.time() - started2
        if elapsed2 - last_log >= 15.0:
            if hit:
                log.info("HUD 仍在: [%s] score=%.2f @ %s "
                         "(已等 %.0fs / 上限 %.0fs)",
                         hit[0], hit[1], hit[2], elapsed2, timeout)
            else:
                log.info("HUD 已消失 (已等 %.0fs / 上限 %.0fs)", elapsed2, timeout)
            last_log = elapsed2
        time.sleep(interval)

    log.warning("等待战斗 HUD 消失超时 (%.1fs)", timeout)
    return False


def _is_black(img, threshold: float = 12.0) -> bool:
    """黑屏判定: 整帧平均亮度极低."""
    return float(img.mean()) < threshold


def dismiss_to_list(actions, device, matcher, close_roi, marker: str,
                    max_rounds: int = 6, wait: float = 1.0,
                    black_threshold: float = 12.0) -> bool:
    """战后收尾: 一路关到回到对手列表.

    战后画面顺序 (实测):
        战斗进行中 → 段位升降/结算画面 → 黑屏 → 被挑战弹窗(若有) → 对手列表
    对应的处理手段:
      - 段位升降 / 结算画面: 它们的"确认"位置不同且可能叠两层, 但都能用【返回键】关掉;
      - 黑屏: 过场画面, 什么都不按, 纯等它过去 (此时按返回键无意义还可能误触);
      - 被挑战弹窗: 会遮挡 defense, 用【点空白区】关 (与进角斗场时那个弹窗同一处空白).
    收敛条件: marker(colosseum_defense, 角斗场页特有按钮) 重新可见 = 已回到列表.
    """
    for i in range(max_rounds):
        img = device.screencap()

        # 黑屏: 不操作, 等它过去
        if black_threshold > 0 and _is_black(img, black_threshold):
            log.info("检测到黑屏, 不按键, 等待其结束")
            end = time.time() + 10.0
            while time.time() < end:
                time.sleep(wait)
                if not _is_black(device.screencap(), black_threshold):
                    break
            continue

        try:
            on_list = matcher.exists(img, marker)
        except FileNotFoundError:
            log.warning("模板 %s 缺失, 无法判断是否回到列表", marker)
            return False
        if on_list:
            log.info("已回到对手列表 (检测到 %s)", marker)
            return True

        log.info("仍未回到列表, 按返回键 (%d/%d)", i + 1, max_rounds)
        actions.back(wait)
        try:
            if matcher.exists(device.screencap(), marker):
                continue
        except FileNotFoundError:
            pass

        if close_roi:
            cx = close_roi[0] + close_roi[2] // 2
            cy = close_roi[1] + close_roi[3] // 2
            log.info("点空白区 (%d, %d) 关闭残留弹窗", cx, cy)
            actions.tap_point(cx, cy, wait=wait)
        else:
            log.warning("pvp_popup.close_area 未采集, 无法点空白区")

    log.warning("收尾 %d 轮后仍未回到列表", max_rounds)
    return False


def settle_after_battle(actions, device, matcher, close_roi, marker: str,
                        tab_templates, black_threshold: float = 12.0,
                        back_gap: float = 0.5, blank_gap: float = 0.2,
                        timeout: float = 30.0, refresh_roi=None,
                        refresh_template: str = "colosseum_refresh") -> bool:
    """战后收尾: 按实测画面顺序逐层处理, 不做盲试.

    实测顺序:
      段位升降 / 结算层 (最多叠 2 层)
        → 黑屏 (>2s)
        → 角斗场画面 (defense 可能被"被挑战弹窗"遮挡)
        → 小奖励弹窗 (必定出现, 不遮挡 defense)

    各层手段:
      1) 段位/结算层: 只用【返回键】. 两层叠着时, 第一次按下后 0.5s 内补第二次
         (黑屏 >2s, 来得及). 成功进入黑屏即认为这两层已关掉.
         —— 这段绝不能点空白区: 此时根本没有需要点空白区才能关的弹窗.
      2) 黑屏: 不操作, 等它过去.
      3) 回到角斗场画面: ranking / rewards / battleRecord 任一可见即算
         (这三个 tab 即使被挑战弹窗出现时也仍然可见).
      4) defense 被遮挡 → 说明有"被挑战弹窗":
           点空白区关掉它, 然后【0.2s 内】同位置再点一次关掉随即出现的小奖励弹窗.
         defense 没被遮挡 → 没有被挑战弹窗:
           点空白区一次, 关掉必定出现的小奖励弹窗.
      5) 干净的角斗场画面 = 一次战斗循环完成.
    """
    def is_black() -> bool:
        if black_threshold <= 0:
            return False
        return _is_black(device.screencap(), black_threshold)

    def blank_tap() -> None:
        if not close_roi:
            log.warning("pvp_popup.close_area 未采集, 无法点空白区")
            return
        cx = close_roi[0] + close_roi[2] // 2
        cy = close_roi[1] + close_roi[3] // 2
        log.info("点空白区 (%d, %d)", cx, cy)
        actions.tap_point(cx, cy, wait=0)      # wait=0: 间隔由调用方精确控制

    def on_colosseum() -> str | None:
        img = device.screencap()
        for t in tab_templates or []:
            try:
                if matcher.exists(img, t):
                    return t
            except FileNotFoundError:
                continue
        return None

    # 1) 段位/结算层: 返回键最多 2 次, 间隔 0.5s
    entered_black = False
    for i in range(2):
        log.info("按返回键关段位/结算层 (%d/2)", i + 1)
        actions.back(0)
        time.sleep(back_gap)
        if is_black():
            log.info("已进入黑屏 → 段位/结算层已关闭")
            entered_black = True
            break
    if not entered_black:
        end = time.time() + 3.0
        while time.time() < end:
            if is_black():
                entered_black = True
                break
            time.sleep(0.3)

    # 2) 等黑屏过去
    if entered_black:
        end = time.time() + 15.0
        while time.time() < end:
            if not is_black():
                log.info("黑屏结束")
                break
            time.sleep(0.5)

    # 3) 等回到角斗场画面
    end = time.time() + timeout
    while time.time() < end:
        t = on_colosseum()
        if t:
            log.info("已回到角斗场画面 (检测到 %s)", t)
            break
        time.sleep(0.5)
    else:
        log.warning("等待回到角斗场画面超时 (%.1fs)", timeout)
        return False

    # 4) defense 是否被遮挡 → 决定点几次空白区
    try:
        occluded = not matcher.exists(device.screencap(), marker)
    except FileNotFoundError:
        log.warning("模板 %s 缺失, 无法判断是否被遮挡", marker)
        occluded = False

    if occluded:
        log.info("defense 被遮挡 → 点空白区关被挑战弹窗, "
                 "%.1fs 内再点一次关小奖励弹窗", blank_gap)
        blank_tap()
        time.sleep(blank_gap)
        blank_tap()
    else:
        log.info("defense 未遮挡 → 点空白区关小奖励弹窗")
        blank_tap()

    # 5) 刷新对手列表: 挑战【失败】时第 3 个敌人不会自动刷新, 需手动点刷新按钮.
    #    该按钮不是每次都有 —— 有就点, 没有就不管.
    if refresh_roi:
        try:
            hit = matcher.find(device.screencap(), refresh_template,
                               roi=refresh_roi)
        except FileNotFoundError:
            log.warning("刷新按钮模板 [%s] 未采集, 跳过刷新", refresh_template)
            hit = None
        if hit:
            log.info("检测到刷新按钮 [%s] (score=%.2f @ %s) → 点击刷新对手",
                     refresh_template, hit.score, hit.center)
            actions.tap_point(*hit.center)
            time.sleep(0.5)
        else:
            log.info("未检测到刷新按钮, 无需刷新")

    time.sleep(0.5)
    log.info("战后收尾完成, 一次战斗循环结束")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="进入 PVP 圆形角斗场")
    parser.add_argument("--live", action="store_true", help="真正点击 (默认干跑)")
    parser.add_argument(
        "--from-main", action="store_true",
        help="从主城开始自动导航 (不加则假设已在玩法页/pvp 页)",
    )
    parser.add_argument("--wait-page", type=float, default=1.5,
                        help="点标签后等待页面切换的秒数")
    parser.add_argument("--repeat", type=int, default=0,
                        help="重复挑战次数 (0 = 只进角斗场不打, 默认)")
    parser.add_argument("--opponent", type=int, default=3,
                        help="挑战第几个对手卡片 (1/2/3, 默认第 3 个)")
    parser.add_argument("--wait-dialog", type=float, default=1.2,
                        help="点卡片战斗按钮后等待弹窗出现的秒数")
    parser.add_argument("--battle-timeout", type=float, default=120.0,
                        help="等待战斗 HUD 消失(打完)的超时秒数; "
                             "要给足余量, 超时后会去按返回键, 战斗未结束时会误伤")
    parser.add_argument("--start-timeout", type=float, default=20.0,
                        help="等待战斗 HUD 出现(确认开打)的超时秒数")
    parser.add_argument("--absent-hold", type=float, default=3.0,
                        help="战斗 HUD 需连续缺席多少秒才算打完")
    parser.add_argument("--poll-interval", type=float, default=1.0,
                        help="等待战斗结束时的轮询间隔秒数")
    parser.add_argument("--wait-dismiss", type=float, default=1.0,
                        help="收尾时每次按返回键/点空白区后的等待秒数")
    parser.add_argument("--max-dismiss", type=int, default=6,
                        help="收尾最多循环几轮 (防止死循环)")
    parser.add_argument("--black-threshold", type=float, default=12.0,
                        help="整帧平均亮度低于此值视为黑屏(黑屏时不按键); 0=关闭该检测")
    parser.add_argument("--hud-template", nargs="+", default=None,
                        help="战斗 HUD 模板名(可多个), 覆盖 config/pvp.yaml")
    parser.add_argument("--result-template", nargs="+", default=None,
                        help="结算画面模板名(可多个), 覆盖 config/pvp.yaml; "
                             "配了它就优先用「等结算出现」判定")
    args = parser.parse_args()

    settings = load_yaml(ROOT / "config" / "settings.yaml")
    coords = Coords()
    pvp_cfg = load_yaml(ROOT / "config" / "pvp.yaml")

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
    print("  进入 PVP 圆形角斗场"
          + ("  (实跑)" if args.live else "  (干跑, 不会真点击)"))
    print("=" * 60)

    # ---- 1~4. 进入圆形角斗场 ----
    # 若当前已在角斗场内 (手动打开的 / 上次运行停在这里), 必须跳过导航 ——
    # 否则会去点"pvp 标签"这类此刻根本不存在的元素, 导致误判甚至误点.
    frame = Frame(device.screencap())
    state, _ = recognizer.detect(frame)
    log.info("当前状态: %s", state.name)

    if state == State.COLOSSEUM:
        log.info("已在圆形角斗场内, 跳过导航步骤")
    else:
        # 1. 主城 → 玩法页
        if args.from_main:
            if state == State.MAIN:
                log.info("在主城 → 点击 play")
                if not actions.tap(frame, "play").success:
                    log.warning("未找到 play 模板, 尝试继续")
                time.sleep(args.wait_page)
            elif state == State.UNKNOWN:
                log.warning("未能识别当前界面, 仍尝试按主城流程继续")

        # 2. 点 pvp 标签
        pvp_pt = coords.center("pvp", "play_menu")
        if pvp_pt is None:
            sys.exit("[ERROR] 缺少 play_menu.pvp 坐标")
        log.info("点 pvp 标签 %s", pvp_pt)
        actions.tap_point(*pvp_pt)
        time.sleep(args.wait_page)

        # 3. 校验是否真的到了 PVP 页
        try:
            on_pvp = actions.wait_for(PVP_TEMPLATE, timeout=6.0, interval=0.8)
        except FileNotFoundError:
            log.warning("pvp 模板不存在, 跳过页面校验")
            on_pvp = True

        if not on_pvp:
            log.warning("未识别到 PVP 页, 重试点一次 pvp 标签")
            actions.tap_point(*pvp_pt)
            time.sleep(args.wait_page)
            try:
                on_pvp = actions.wait_for(PVP_TEMPLATE, timeout=6.0, interval=0.8)
            except FileNotFoundError:
                on_pvp = True
            if not on_pvp:
                log.error("仍未能进入 PVP 页, 中止 (避免点错坐标)")
                device.close()
                sys.exit(1)

        log.info("已进入 PVP 页")

        # 4. 点圆形角斗场卡片进入
        col_pt = coords.center("colosseum.enter", "pvp_cards")
        if col_pt is None:
            sys.exit("[ERROR] 缺少 pvp_cards.colosseum.enter 坐标")
        log.info("点击圆形角斗场卡片 %s", col_pt)
        actions.tap_point(col_pt[0], col_pt[1])
        time.sleep(args.wait_page)
        log.info("已进入圆形角斗场")

    # ---- 5. 处理"战斗信息"弹窗: 它会遮挡底层的"防御"按钮 ----
    # 机制: 进角斗场后, 若此前被其它玩家挑战过, 会先弹出战斗信息页盖住底层.
    # 判断: 用 colosseum_defense 模板看"防御"按钮是否可见; 不可见 = 被遮挡.
    close_roi = coords.box("close_area", "pvp_popup")
    try:
        defense_visible = matcher.exists(
            device.screencap(), "colosseum_defense")
    except FileNotFoundError:
        defense_visible = True
        log.warning("模板 colosseum_defense 缺失, 跳过弹窗判断")

    if not defense_visible:
        if close_roi is None:
            log.warning("防御按钮被遮挡, 但 pvp_popup.close_area 未采集, 无法关闭")
        else:
            cx = close_roi[0] + close_roi[2] // 2
            cy = close_roi[1] + close_roi[3] // 2
            log.info("防御按钮被遮挡 → 点空白区关闭弹窗 (%d, %d)", cx, cy)
            actions.tap_point(cx, cy)
            time.sleep(args.wait_page)

    # ---- 6. 读取入场券 (简化版: 只判断有票 / 没票) ----
    # ticket_empty = 没票时显示的 "0/5"; 匹配到它 = 没票, 否则当作还有票.
    left = ticket_left(device, matcher)
    print()
    if left is False:
        print("入场券: 已用完 (0/5) —— 无法继续战斗")
    elif left is True:
        print("入场券: 还有剩余 —— 可以战斗")
    else:
        print("入场券: 未能读取")

    # ---- 7. 重复挑战: 点第 N 张卡战斗 → 弹窗里再点战斗 → 打完收尾 → 循环 ----
    if args.repeat > 0:
        card_pt = coords.center(f"card{args.opponent}", "colosseum_cards")
        if card_pt is None:
            log.error("缺少 colosseum_cards.card%d 坐标", args.opponent)
            device.close()
            sys.exit(1)

        # 判定模板清单: 命令行优先, 其次 config/pvp.yaml
        #   result = "结算画面出现" (优先策略)
        #   hud    = "战斗 HUD 出现→消失" (兜底策略)
        hud_templates = (args.hud_template
                         or pvp_cfg.get("battle_hud_templates")
                         or [])
        result_templates = (args.result_template
                            or pvp_cfg.get("battle_result_templates")
                            or [])
        result_color = pvp_cfg.get("battle_result_color")
        hud_roi = pvp_cfg.get("battle_hud_roi")
        log.info("战斗 HUD 判定模板: %s (搜索区域 %s)", hud_templates, hud_roi)
        tab_templates = pvp_cfg.get("colosseum_tab_templates") or []
        back_gap = float(pvp_cfg.get("settle_back_gap", 0.5))
        blank_gap = float(pvp_cfg.get("settle_blank_gap", 0.2))
        refresh_roi = pvp_cfg.get("colosseum_refresh_roi")
        log.info("结算画面判定模板: %s", result_templates)
        log.info("结算按钮颜色判定: %s", result_color or "未配置")

        for r in range(1, args.repeat + 1):
            if ticket_left(device, matcher) is False:
                log.info("入场券已用完, 停止挑战")
                break

            log.info("=== 第 %d/%d 场: 挑战第 %d 个卡片 ===",
                     r, args.repeat, args.opponent)

            # (a) 确保没有弹窗遮挡 (被挑战弹窗会盖住卡片)
            dismiss_to_list(
                actions, device, matcher, close_roi, "colosseum_defense",
                max_rounds=args.max_dismiss, wait=args.wait_dismiss,
                black_threshold=args.black_threshold,
            )

            # (b) 点第 N 张卡的"战斗"按钮 (坐标)
            log.info("点第 %d 张卡战斗按钮 %s", args.opponent, card_pt)
            actions.tap_point(card_pt[0], card_pt[1])
            time.sleep(args.wait_dialog)

            # (c) 点弹窗里的"战斗" (图片方式)
            frame = Frame(device.screencap())
            hit = actions.tap(frame, "colosseum_popup_battle")
            if not hit.success:
                log.warning("未匹配到弹窗战斗按钮 colosseum_popup_battle, 中止")
                break

            # (d) 等战斗结束
            if not wait_battle_done(
                device, matcher, hud_templates,
                args.battle_timeout, args.poll_interval,
                args.start_timeout, args.absent_hold,
                result_templates, result_color, hud_roi,
            ):
                log.warning("第 %d 场: 未能确认战斗结束, 仍尝试收尾", r)

            # (e) 战后收尾: 按实测顺序逐层处理 (段位/结算 → 黑屏 → 弹窗)
            settle_after_battle(
                actions, device, matcher, close_roi, "colosseum_defense",
                tab_templates,
                black_threshold=args.black_threshold,
                back_gap=back_gap, blank_gap=blank_gap,
                refresh_roi=refresh_roi,
            )
            log.info("第 %d 场结束", r)
            print()

    device.close()
    if not args.live:
        print("干跑结束 (未真正点击)。确认坐标无误后加 --live 实跑。")
    else:
        print("实跑结束：已进入圆形角斗场。")


if __name__ == "__main__":
    main()
