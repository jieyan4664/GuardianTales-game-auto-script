"""主城: 登录后点 inn 建筑, 再点旁边可能出现的宝箱.

流程:
  ① 识别首页: 主城显示 my_room; 看板娘显示 heavenhold, 点它可切回主城
  ② 找 inn 建筑并点击
  ③ 在 inn 右侧找宝箱, 有就点, 没有就跳过
     —— 若点了 inn 后有界面弹出把 inn 遮挡(小概率, 因 inn 等级不同),
        则按返回键一次, 再找一次宝箱.

为什么不需要"拖动地图找建筑"那套复杂扫描:
  登录后 inn 就在屏幕上, 全屏模板匹配满分命中, 直接找即可.
  且宝箱的检索区域是【按 inn 实际位置动态计算】的, 主城被拖动过也照样有效.

用法:
    python tools/city.py            # 干跑
    python tools/city.py --live     # 实跑
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
from core.logger import get_logger                 # noqa: E402
from core.vision import Matcher                    # noqa: E402
from game.actions import Actions                   # noqa: E402
from game.recognizer import Recognizer             # noqa: E402

log = get_logger("city")


def load_yaml(path: Path) -> dict:
    result = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return result if isinstance(result, dict) else {}


def find_tpl(matcher, img, tpl, roi=None):
    """在区域里找模板, 返回 MatchResult 或 None (模板缺失只告警不抛异常)."""
    try:
        return matcher.find(img, tpl, roi=roi)
    except FileNotFoundError:
        log.warning("模板 [%s] 不存在, 请先采集", tpl)
        return None


def ensure_main_city(device, matcher, actions, cfg) -> bool:
    """确保在【主城】页面: 若在看板娘页面, 点 heavenhold 图标切回去."""
    roi = cfg.get("home_roi")
    img = device.screencap()

    if find_tpl(matcher, img, cfg.get("my_room", "my_room"), roi):
        log.info("已在主城页面 (检测到 %s)", cfg.get("my_room"))
        return True

    hit = find_tpl(matcher, img, cfg.get("heavenhold", "heavenhold"), roi)
    if not hit:
        log.warning("既没检测到主城也没检测到看板娘标识, 按原流程继续")
        return False

    log.info("在看板娘页面 → 点 heavenhold 图标切回主城 %s", hit.center)
    actions.tap_point(*hit.center)
    time.sleep(float(cfg.get("switch_wait", 1.5)))

    img = device.screencap()
    if find_tpl(matcher, img, cfg.get("my_room", "my_room"), roi):
        log.info("已切换到主城页面")
        return True
    log.warning("切换后仍未检测到主城标识, 继续尝试")
    return False


def run_city_tasks(device, matcher, actions, cfg, coords=None) -> bool:
    """主城任务: 确保在首页 → 点 inn → 点宝箱.

    返回 True = 点到了 inn; False = 没找到 inn (跳过, 不做任何误点).

    抽成函数是为了让「日常一条龙」(tools/daily.py) 能把它排在第一步 ——
    它必须在【首页】执行, 不能先点 play 进玩法页.
    """
    # ① 确保在主城
    ensure_main_city(device, matcher, actions, cfg)

    # ①-b 礼物任务 (friend / 公会签到 / 商店金币): 只依赖主城底部栏, 与 inn 无关.
    #      刻意放在 inn 之前 —— 下面"找不到 inn 就 return"会把后续整段带走,
    #      放在后面的话 inn 一旦没找到, 礼物任务就被顺带跳过了.
    if coords:
        run_gift_tasks(device, matcher, actions, coords, cfg)
        # 礼物任务的收尾允许停在【看板娘】页面(那是合法的成功状态之一),
        # 但下面的 inn 只存在于主城 → 这里补一次"切回主城", 免得 inn 找不到.
        ensure_main_city(device, matcher, actions, cfg)

        # 注: 每日任务一键领取【不在这里】.
        #     它是收尾任务 —— 奖励取决于前面各任务是否完成, 必须排在
        #     扫荡 / 角斗场等【全部任务之后】, 见 tools/daily.py 的
        #     kind: daily_task 步骤 (函数本身仍在下面).

    # ② 找 inn 并点击
    img = device.screencap()
    inn_hit = find_tpl(matcher, img, cfg.get("inn", "inn"), cfg.get("inn_roi"))
    if not inn_hit:
        log.warning("未找到 inn 建筑, 跳过主城任务")
        return False
    log.info("找到 inn: 中心 %s (score=%.2f)", inn_hit.center, inn_hit.score)
    actions.tap_point(*inn_hit.center)

    # 点 inn 后: 若主城 UI 图标(menu/play)消失 = 进了建筑界面 → 再点一下退出
    rc = cfg.get("inn_reclick") or {}
    if rc.get("enabled"):
        time.sleep(float(rc.get("wait", 1.0)))
        img = device.screencap()
        has_ui = bool(find_tpl(matcher, img, "menu")
                      or find_tpl(matcher, img, "play"))
        if not has_ui:
            log.info("主城 UI(menu/play)消失 → 疑似进入建筑界面, 再点一下退出")
            if rc.get("use_back"):
                actions.back()
            else:
                blank = (coords.center("close_area", "pvp_popup")
                         if coords else None)
                if blank:
                    log.info("点空白区 %s", blank)
                    actions.tap_point(*blank)
                else:
                    log.warning("取不到空白区坐标, 改为按返回键")
                    actions.back()
            time.sleep(1.0)

    # 宝箱不会立即出现, 停留一会儿再找, 否则容易漏掉
    time.sleep(float(cfg.get("treasure_wait", 2.0)))

    # ③ 在 inn 右侧找宝箱
    x, y, w, h = inn_hit.box
    gap = int(cfg.get("treasure_gap_left", 0))
    span = int(cfg.get("treasure_span", 400))
    dy = int(cfg.get("treasure_dy", 160))
    cy = y + h // 2
    tx = x + w + gap
    ty = max(cy - dy, 0)
    treasure_roi = [tx, ty, span, dy * 2]
    log.info("宝箱检索区域 (相对 inn 右侧): %s", treasure_roi)

    img = device.screencap()
    box_hit = find_tpl(matcher, img, cfg.get("treasure_box", "treasure_box"),
                       treasure_roi)

    if not box_hit:
        # 小概率: 点了 inn 后弹出界面把 inn 遮挡了 → 按返回键一次再找
        if not find_tpl(matcher, img, cfg.get("inn", "inn"),
                        cfg.get("inn_roi")):
            log.info("inn 被遮挡(可能有界面弹出) → 按返回键一次")
            actions.back()
            time.sleep(float(cfg.get("inn_settle", 1.2)))
            img = device.screencap()
            box_hit = find_tpl(matcher, img,
                               cfg.get("treasure_box", "treasure_box"),
                               treasure_roi)

    if not box_hit:
        log.info("未找到宝箱, 跳过 (属正常: 宝箱并非每次都有)")
    else:
        log.info("找到宝箱: 中心 %s (score=%.2f) → 点击",
                 box_hit.center, box_hit.score)
        actions.tap_point(*box_hit.center)
        # 点完宝箱会弹出奖励弹窗 → 点空白处关掉 (沿用角斗场弹窗那处空白)
        time.sleep(float(cfg.get("treasure_close_wait", 0.8)))
        blank = coords.center("close_area", "pvp_popup") if coords else None
        if blank:
            log.info("点空白区 %s 关闭奖励弹窗", blank)
            actions.tap_point(*blank)
            time.sleep(0.5)
        else:
            log.warning("未取到空白区坐标 pvp_popup.close_area, 跳过关闭弹窗")

    # ④ 建筑系列任务 (受 buildings.enabled 开关控制; 当前只做导航)
    run_buildings(device, matcher, actions, cfg)
    return True


# 拖动方向 → (dx, dy) 单位向量. 注意语义:
#   "向下拖" = 鼠标往下滑(dy>0); "右下45°" = 鼠标往右下拖, 效果是把视野往左上挪.
DIRECTIONS = {
    "down": (0, 1),
    "up": (0, -1),
    "left": (-1, 0),
    "right": (1, 0),
    "down_right": (1, 1),
    "down_left": (-1, 1),
    "up_right": (1, -1),
    "up_left": (-1, -1),
}

# 滑动围绕屏幕中心进行
_CX, _CY = 960, 540


def _frame_diff(a, b) -> float:
    """两帧的平均差异, 用来判断"画面到底动没动"(= 是否撞到地图边界)."""
    import cv2
    return float(cv2.absdiff(a, b).mean())


def drag_scan(device, matcher, actions, targets, direction, scan,
              found=None):
    """沿 direction 分步拖动主城, 记录沿途【首次出现】的目标建筑.

    targets 按优先级排列: 先遇到的先记录(对应"先做谁"的顺序).
    返回 dict: {模板名: MatchResult}.

    停止条件(任一):
      - targets 全部找到
      - 达到 max_steps
      - 撞到边界: 滑了之后画面没变化(帧差 < move_threshold)
    """
    found = dict(found or {})
    step = int(scan.get("step", 400))
    duration_ms = int(scan.get("duration_ms", 300))
    settle = float(scan.get("settle_wait", 0.8))
    max_steps = int(scan.get("max_steps", 12))
    roi = scan.get("roi")
    move_thr = float(scan.get("move_threshold", 1.0))
    dx, dy = DIRECTIONS.get(direction, (0, 1))

    def check(img):
        """在 roi 里找还没找到过的目标."""
        for t in targets:
            if t in found:
                continue
            hit = find_tpl(matcher, img, t, roi)
            if hit:
                log.info("发现建筑 [%s] 中心 %s (score=%.2f)", t, hit.center,
                         hit.score)
                found[t] = hit

    def all_found() -> bool:
        """【本步】的 targets 是否都已找到.

        注意不能写成 len(found) >= len(targets): found 里装着前面各步
        找到的建筑, 用总长度比对会导致新一步还没开始就被判定为"已全部找到".
        """
        return all(t in found for t in targets)

    # 先检查当前画面(没拖动前就可能已经能看到)
    check(device.screencap())
    if all_found():
        return found

    for i in range(max_steps):
        sx = _CX - dx * step // 2
        sy = _CY - dy * step // 2
        ex = _CX + dx * step // 2
        ey = _CY + dy * step // 2
        before = device.screencap()
        log.info("沿 [%s] 第 %d/%d 次拖动: (%d,%d)→(%d,%d)",
                 direction, i + 1, max_steps, sx, sy, ex, ey)
        device.swipe(sx, sy, ex, ey, duration_ms)
        time.sleep(settle)

        img = device.screencap()
        if _frame_diff(before, img) < move_thr:
            log.info("画面没动 → 已到地图边界, 停止该方向搜索")
            break

        check(img)
        if all_found():
            break

    return found


def run_buildings(device, matcher, actions, cfg) -> bool:
    """建筑系列任务: 目前只做【导航】—— 拖动主城找到建筑.

    建筑上的具体任务待实现, 找到后仅记录日志.
    返回 True = 执行了; False = 开关关闭, 整段跳过.
    """
    b = cfg.get("buildings") or {}
    if not b.get("enabled"):
        log.info("建筑任务开关关闭, 跳过")
        return False

    scan = b.get("scan") or {}
    found = {}

    # 沿主方向拖动, 沿途找 dispatch_outpost
    # (caravanshop 任务暂缓, 其专属的"45° 补搜"与提前停止逻辑已移除;
    #  恢复方式见 config/city.yaml 内 buildings 段的注释)
    p1 = b.get("path_first") or {}
    found = drag_scan(device, matcher, actions,
                      p1.get("targets") or [], p1.get("direction", "up"),
                      scan, found)

    # 做 dispatch_outpost 的任务 (若已找到)
    run_dispatch_outpost_task(device, matcher, actions, cfg, found)

    if found:
        log.info("建筑导航完成, 找到: %s", ", ".join(
            f"{k}@{v.center}" for k, v in found.items()))
    else:
        log.warning("建筑导航结束, 一个都没找到 (检查模板/拖动参数)")
    # TODO: 各建筑的具体任务待实现
    return True


def _click_roi(actions, roi, label, pick=None) -> None:
    """点一个区域. pick=left/right 决定横向落点(给"2 选 1"用), 否则取中心."""
    x, y, w, h = (int(v) for v in roi)
    if pick:
        fx = {"left": 0.25, "right": 0.75}.get(pick, 0.5)
        pt = (int(x + w * fx), int(y + h * 0.5))
    else:
        pt = (x + w // 2, y + h // 2)
    log.info("点击 %s %s", label, pt)
    actions.tap_point(*pt)


def _entered_building(device, matcher, building) -> bool:
    """是否已进入建筑界面: 建筑不再可见 或 主城 UI(menu/play)消失."""
    img = device.screencap()
    still_b = find_tpl(matcher, img, building)
    still_ui = find_tpl(matcher, img, "menu") or find_tpl(matcher, img, "play")
    log.info("进入判定: 建筑可见=%s 主城UI可见=%s", bool(still_b), bool(still_ui))
    return not still_b or not still_ui


def _wait_tpl(device, matcher, tpl, timeout: float = 4.0,
              interval: float = 0.5):
    """等模板出现 (用新截图轮询), 超时返回 None."""
    end = time.time() + timeout
    while time.time() < end:
        hit = find_tpl(matcher, device.screencap(), tpl)
        if hit:
            return hit
        time.sleep(interval)
    return None


def _reward_dialog(actions, t, tag) -> None:
    """选择 → 确认 → 再次确认 (阶段A/B 共用)."""
    wait = float(t.get("confirm_wait", 1.0))
    sr = t.get("select_roi")
    if sr:
        _click_roi(actions, sr, f"[{tag}] 选项", t.get("select_pick", "left"))
        time.sleep(wait)

    cr = t.get("confirm_roi")
    if not (cr and t.get("click_confirm")):
        if cr:
            log.info("[%s] 按要求暂不点击确认", tag)
        return
    _click_roi(actions, cr, f"[{tag}] 确认")
    time.sleep(wait)

    cr2 = t.get("confirm2_roi")
    if cr2:
        _click_roi(actions, cr2, f"[{tag}] 再次确认")
        time.sleep(wait)


def _gift_in(device, matcher, tpl, roi, label):
    """在指定区域里找礼物图标, 返回 MatchResult 或 None.

    礼物图标很小(gift.png 仅 20x22)且在别处也会出现, 所以必须限定 ROI,
    否则全屏匹配会点到无关的位置.
    """
    if roi is None:
        log.warning("缺少 %s 的坐标 (coords.gift_spots)", label)
        return None
    hit = find_tpl(matcher, device.screencap(), tpl, roi)
    if hit:
        log.info("%s 处发现礼物 %s (score=%.2f)", label, hit.center, hit.score)
    else:
        log.info("%s 处没有礼物", label)
    return hit


def _btn_hit(device, matcher, tpl, roi, label):
    """找一个【可点的按钮】, 返回 MatchResult 或 None.

    点击目标永远是按钮本身, 不是贴在它旁边的礼物图标 ——
    礼物图标可能落在按钮外侧, 点它会落空 (或误触别处).
    """
    hit = find_tpl(matcher, device.screencap(), tpl, roi)
    if hit:
        log.info("%s 按钮 %s (score=%.2f)", label, hit.center, hit.score)
    else:
        log.warning("%s 按钮 [%s] 未匹配到", label, tpl)
    return hit


def _home_state(device, matcher, cfg, wait: float = 0.0,
                interval: float = 0.5):
    """检测是否在【主城】或【看板娘】页面, 返回页面名或 None.

    wait > 0 时在这段时间内轮询: 页面切换有动画, 立刻截图会把"还在渲染"
    误判成"没回到首页", 从而多按一次返回键(在主城上可能触发退出游戏).
    """
    end = time.time() + wait
    while True:
        img = device.screencap()
        roi = cfg.get("home_roi")
        if find_tpl(matcher, img, cfg.get("my_room", "my_room"), roi):
            return "主城"
        if find_tpl(matcher, img, cfg.get("heavenhold", "heavenhold"), roi):
            return "看板娘"
        if time.time() >= end:
            return None
        time.sleep(interval)


def back_to_home(device, matcher, actions, cfg, back_wait: float = 1.0,
                 label: str = "返回主城", max_tries: int = 5) -> bool:
    """按返回键退回首页, 直到检测到【主城】或【看板娘】页面.

    为什么必须"按到回去为止", 而不是按一次就完事:
      正常情况下从各页按一次返回键就能回到主城 (friend 页返回是直接到主城,
      不会经过菜单层). 但"按一次够不够"会受时序影响:
        - 返回后页面有切换动画, 只截一帧容易把"还在渲染"误判成"没回去";
        - 领取时那次同位置的补点, 有时会把页面又推进一层, 需要多退一次.
      只要哪一次没退干净, 底部栏就不可见, 后面所有任务都会因此找不到按钮
      而统统跳过 (实跑日志里就是这么翻车的).
      所以统一成"没回到首页就再按一次", 让流程自己纠偏.
    为什么不干脆连按几次:
      已经回到首页就不再按键 —— 在主城上多按返回键会弹出"退出游戏"确认框.
      所以每轮都先检测(带轮询等待, 避开动画期), 确认没回去才再按一次.
    """
    for i in range(1, max_tries + 1):
        state = _home_state(device, matcher, cfg)
        if state:
            log.info("%s 已在[%s]页面", label, state)
            return True

        log.info("%s: 未在首页, 按返回键 (%d/%d)", label, i, max_tries)
        actions.back(back_wait)

        state = _home_state(device, matcher, cfg, wait=1.5)
        if state:
            log.info("%s 完成, 已返回[%s]页面 (共按 %d 次返回键)",
                     label, state, i)
            return True

    log.warning("%s: 按了 %d 次返回键仍未回到主城/看板娘页面",
                label, max_tries)
    return False


def _claim_tap(actions, pt, gap: float, label: str) -> None:
    """点领取按钮, 隔 gap 秒在【同一位置】再点一下, 关掉小奖励弹窗.

    间隔由配置的 receive_wait 决定.
    直接借 tap_point 自带的 wait 参数来精确控制 —— 不能写成
    time.sleep(gap) 再点: tap_point 默认还要等 0.6s, 两者叠加会让实际间隔
    比配置值多出 0.6s, 调参就失去意义了.
    """
    log.info("点「%s」%s → 隔 %.2fs 同位置再点一次关小奖励窗", label, pt, gap)
    actions.tap_point(*pt, wait=gap)
    actions.tap_point(*pt)


def run_friend_gift_task(device, matcher, actions, coords, cfg) -> bool:
    """领取 friend 礼物 (【不做】礼物判定, 直接进).

    逻辑:
      ① 点「菜单」按钮打开菜单层
      ② 点「好友」入口进好友页
      ③ 点「接收」→ 同位置再点一次, 关掉小奖励弹窗
      ④ 按返回键 → 检测是否回到【主城】或【看板娘】页面

    为什么这里取消 gift 判定: 这一路点下去不会误触 ——
      即使菜单/好友处没有礼物, 无非是进去后没东西可领、点「接收」落空,
      不会触发任何破坏性操作.
      (guild / shop 不同: 它们点按钮会直接扣掉/进入具体玩法, 所以仍保留判定)

    点击目标一律是按钮本身(menu.png / friend.png), 不是礼物图标 ——
    礼物图标可能贴在按钮外侧, 点它会落空.

    返回 True = 执行了; False = 开关关闭或按钮没找到.
    """
    t = cfg.get("friend_gift_task") or {}
    if not t.get("enabled"):
        log.info("friend 礼物任务开关关闭, 跳过")
        return False

    open_wait = float(t.get("open_wait", 1.0))
    back_wait = float(t.get("back_wait", 1.0))

    # ① 点「菜单」按钮
    menu_btn = _btn_hit(device, matcher, t.get("menu", "menu"),
                        t.get("menu_roi"), "菜单")
    if menu_btn is None:
        log.warning("未匹配到菜单按钮 [%s], 无法打开菜单层, 跳过",
                    t.get("menu", "menu"))
        return False
    log.info("点菜单按钮 %s", menu_btn.center)
    actions.tap_point(*menu_btn.center, wait=open_wait)

    # ② 点「好友」入口
    friend_btn = _btn_hit(device, matcher, t.get("friend", "friend"),
                          t.get("friend_roi"), "好友入口")
    if friend_btn is None:
        log.warning("未匹配到好友入口 [%s] → 关掉菜单层后放弃",
                    t.get("friend", "friend"))
        actions.tap_point(*menu_btn.center, wait=open_wait)
        return True
    log.info("点好友入口 %s", friend_btn.center)
    actions.tap_point(*friend_btn.center, wait=open_wait)

    # ③ 点「接收」→ 同位置再点一次
    recv = coords.center("friend", "gift_receive")
    if recv is None:
        log.warning("缺少 gift_receive.friend 坐标, 放弃领取")
    else:
        _claim_tap(actions, recv, float(t.get("receive_wait", 1.0)), "接收")

    # ④ 返回并验证
    back_to_home(device, matcher, actions, cfg, back_wait, "friend 礼物")
    return True


def run_guild_gift_task(device, matcher, actions, coords, cfg) -> bool:
    """领取公会【签到】礼物 (保留礼物判定).

    逻辑:
      ① 底部栏「公会」按钮处有礼物图标 → 才做; 没有则跳过
      ② 点「公会」按钮
      ③ 等 guild_castle 出现 = 确认已进入公会页 (没进去就不点后续按钮)
      ④ 点「签到」→ 同位置再点一次, 关掉小奖励弹窗
      ⑤ 按返回键 → 检测是否回到【主城】或【看板娘】页面

    返回 True = 执行了; False = 开关关闭或无礼物.
    """
    t = cfg.get("guild_gift_task") or {}
    if not t.get("enabled"):
        log.info("公会礼物任务开关关闭, 跳过")
        return False

    open_wait = float(t.get("open_wait", 1.0))
    back_wait = float(t.get("back_wait", 1.0))

    # ① 公会按钮处有礼物吗
    ghit = _gift_in(device, matcher, t.get("gift", "gift"),
                    coords.box("guild_gift", "gift_spots"), "公会按钮")
    if not ghit:
        log.info("公会处无礼物图标 → 跳过")
        return False

    # ② 点「公会」按钮
    guild_btn = _btn_hit(device, matcher, t.get("guild", "guild"),
                         t.get("guild_roi"), "公会")
    if guild_btn is None:
        log.warning("未匹配到公会按钮 [%s], 跳过", t.get("guild", "guild"))
        return False
    log.info("点公会按钮 %s", guild_btn.center)
    actions.tap_point(*guild_btn.center, wait=open_wait)

    # ③ 确认已进入公会页
    entered = t.get("entered", "guild_castle")
    if not _wait_tpl(device, matcher, entered,
                     timeout=float(t.get("entered_timeout", 8.0))):
        log.warning("未等到 [%s] 出现 → 可能没进公会页, 跳过领取", entered)
        return True
    log.info("已进入公会页 (检测到 %s)", entered)

    # ④ 点「签到」→ 同位置再点一次
    att = coords.center("guild_attendance", "gift_receive")
    if att is None:
        log.warning("缺少 gift_receive.guild_attendance 坐标, 跳过领取")
    else:
        _claim_tap(actions, att, float(t.get("receive_wait", 1.0)), "签到")

    # ⑤ 返回并验证
    back_to_home(device, matcher, actions, cfg, back_wait, "公会签到")
    return True


def run_shop_gift_task(device, matcher, actions, coords, cfg) -> bool:
    """领取商店【金币】礼物 (保留礼物判定).

    逻辑:
      ① 底部栏「商店」按钮处有礼物图标 → 才做; 没有则跳过
      ② 点「商店」按钮
      ③ 等 shop_resource 出现 = 确认已进入商店页
      ④ 点 shop_resource
      ⑤ 找金币(shop_resource_goldcoin) → 点它
      ⑥ 点「确认」→ 同位置再点一次, 关掉小奖励弹窗
      ⑦ 按返回键 → 检测是否回到【主城】或【看板娘】页面

    返回 True = 执行了; False = 开关关闭或无礼物.
    """
    t = cfg.get("shop_gift_task") or {}
    if not t.get("enabled"):
        log.info("商店礼物任务开关关闭, 跳过")
        return False

    open_wait = float(t.get("open_wait", 1.0))
    back_wait = float(t.get("back_wait", 1.0))

    # ① 商店按钮处有礼物吗
    shit = _gift_in(device, matcher, t.get("gift", "gift"),
                    coords.box("shop_gift", "gift_spots"), "商店按钮")
    if not shit:
        log.info("商店处无礼物图标 → 跳过")
        return False

    # ② 点「商店」按钮
    shop_btn = _btn_hit(device, matcher, t.get("shop", "shop"),
                        t.get("shop_roi"), "商店")
    if shop_btn is None:
        log.warning("未匹配到商店按钮 [%s], 跳过", t.get("shop", "shop"))
        return False
    log.info("点商店按钮 %s", shop_btn.center)
    actions.tap_point(*shop_btn.center, wait=open_wait)

    # ③ 确认已进入商店页, 顺便拿到要点的那个图标位置
    res_tpl = t.get("resource", "shop_resource")
    res = _wait_tpl(device, matcher, res_tpl,
                    timeout=float(t.get("entered_timeout", 8.0)))
    if not res:
        log.warning("未等到 [%s] 出现 → 可能没进商店页, 跳过领取", res_tpl)
        return True
    log.info("已进入商店页 (检测到 %s)", res_tpl)

    # ④ 点 shop_resource
    log.info("点 [%s] %s", res_tpl, res.center)
    actions.tap_point(*res.center, wait=open_wait)

    # ⑤ 找金币并点击
    coin_tpl = t.get("coin", "shop_resource_goldcoin")
    coin = _wait_tpl(device, matcher, coin_tpl,
                     timeout=float(t.get("coin_timeout", 4.0)))
    if not coin:
        log.warning("未找到金币 [%s], 跳过领取", coin_tpl)
        back_to_home(device, matcher, actions, cfg, back_wait, "商店礼物")
        return True
    log.info("点金币 %s (score=%.2f)", coin.center, coin.score)
    actions.tap_point(*coin.center, wait=open_wait)

    # ⑥ 点「确认」→ 同位置再点一次
    ok = coords.center("shop_confirm", "gift_receive")
    if ok is None:
        log.warning("缺少 gift_receive.shop_confirm 坐标, 跳过确认")
    else:
        _claim_tap(actions, ok, float(t.get("receive_wait", 1.0)), "确认")

    # ⑦ 返回并验证
    back_to_home(device, matcher, actions, cfg, back_wait, "商店礼物")
    return True


def run_daily_task_receive(device, matcher, actions, coords, cfg) -> bool:
    """每日任务: 一键领取全部奖励.

    逻辑:
      ① 点主城「任务」按钮
      ② 判断是否真的进了任务界面: 查不到主城元素(menu/play) 即算进入
         (与"点建筑进建筑页"用的是同一套判据)
      ③ 点「每日任务」页签
      ④ 逐轮领取: 每轮「点该位置 → 隔 tap_gap 再点一次关掉小奖励窗」
         (第一下领取, 第二下关弹窗). 轮次与各自的位置由配置的 rounds 列表给出 ——
         实测两轮点的【不是同一个按钮】: 第 1 轮右下的「全部领取」, 第 2 轮左下那个.
      ⑤ 按返回键 → 检测是否回到【主城】或【看板娘】页面

    前置: 这是【收尾任务】, 必须排在扫荡 / 角斗场等全部任务【之后】执行 ——
      它的奖励取决于前面各任务是否完成, 没做完就领不全.
      而排到最后, 前面那些步骤会把我们留在玩法页 / 角斗场等内层页面,
      本任务却要在主城底部栏点「任务」按钮 → 所以先一路返回主城.

    返回 True = 执行了; False = 开关关闭 / 没回到主城 / 没进任务界面.
    """
    t = cfg.get("daily_receive_task") or {}
    if not t.get("enabled"):
        log.info("每日任务领取开关关闭, 跳过")
        return False

    gap = float(t.get("tap_gap", 0.5))
    back_wait = float(t.get("back_wait", 1.0))

    # ⓪ 前置: 一路返回主城.
    #    前面各步可能把我们留在内层页面. 最深处是角斗场, 退回来是
    #    「角斗场 → PVP页 → 主城」= 2 次返回键 (不经过玩法页);
    #    其它步骤更浅. max_tries 给 8 只是留余量, 正常 1~2 次就到位.
    if not back_to_home(device, matcher, actions, cfg, back_wait,
                         "每日任务领取 前置", max_tries=8):
        log.warning("前置: 未能回到首页, 跳过每日任务领取")
        return False
    if not ensure_main_city(device, matcher, actions, cfg):
        log.warning("前置: 未能确认在主城, 跳过 (避免按错坐标)")
        return False

    # ① 点主城「任务」按钮
    task_pt = coords.center("task", "daily_task_page")
    if task_pt is None:
        log.warning("缺少 daily_task_page.task 坐标, 跳过")
        return False
    log.info("点主城「任务」按钮 %s", task_pt)
    actions.tap_point(*task_pt, wait=float(t.get("open_wait", 1.5)))

    # ② 主城元素(menu/play)都没了 = 成功进入任务界面
    img = device.screencap()
    still_ui = bool(find_tpl(matcher, img, "menu")
                    or find_tpl(matcher, img, "play"))
    log.info("进入判定: 主城UI(menu/play)可见=%s", still_ui)
    if still_ui:
        log.warning("主城 UI 仍在 → 可能没进任务界面, 中止 (避免误点)")
        return False

    # ③ 点「每日任务」页签
    dt = coords.center("daily_task", "daily_task_page")
    if dt is None:
        log.warning("缺少 daily_task_page.daily_task 坐标")
    else:
        log.info("点「每日任务」页签 %s", dt)
        actions.tap_point(*dt, wait=gap)

    # ④ 逐轮领取: 每轮「点该位置 → 同位置再点一次关掉小奖励窗」
    #    rounds 是【位置键名的列表】(不是次数) —— 两轮点的是不同按钮.
    keys = t.get("rounds") or ["receive_all"]
    for i, key in enumerate(keys, 1):
        pt = coords.center(key, "daily_task_page")
        if pt is None:
            log.warning("缺少 daily_task_page.%s 坐标, 跳过第 %d 轮", key, i)
            continue
        log.info("第 %d/%d 轮 → 位置键 [%s]", i, len(keys), key)
        _claim_tap(actions, pt, gap, f"领取 {i}/{len(keys)}")

    # ⑤ 返回并验证
    back_to_home(device, matcher, actions, cfg, back_wait, "每日任务领取")
    return True


def run_gift_tasks(device, matcher, actions, coords, cfg) -> None:
    """三个礼物任务依次执行: friend → 公会签到 → 商店金币.

    抽成函数是为了能单独跑 (见 main 的 --gifts-only).
    """
    run_friend_gift_task(device, matcher, actions, coords, cfg)
    run_guild_gift_task(device, matcher, actions, coords, cfg)
    run_shop_gift_task(device, matcher, actions, coords, cfg)


def run_dispatch_outpost_task(device, matcher, actions, cfg, found) -> bool:
    """dispatch_outpost 的任务.

    建筑上方【同一位置】二选一:
      box(宝箱) → 阶段A: 点建筑 → 选择 / 确认 / 再次确认
      感叹号(!) → 直接阶段B (上次登录没做该任务时, 感叹号取代了 box)

    阶段A 做完后, 感叹号会出现在原 box 的位置 → 接着阶段B:
      点建筑 → 点「派遣」→ 弹窗遮挡建筑 → 选择 / 确认 / 再次确认
    """
    t = cfg.get("dispatch_outpost_task") or {}
    if not t.get("enabled"):
        log.info("dispatch_outpost 任务开关关闭, 跳过")
        return False

    bname = t.get("building", "dispatch_outpost")
    hit = found.get(bname)
    if not hit:
        log.info("未找到 %s, 跳过其任务", bname)
        return False

    enter_wait = float(t.get("enter_wait", 1.5))

    # ① 建筑上方找 box / 感叹号
    x, y, w, h = hit.box
    above = int(t.get("box_above", 200))
    margin = int(t.get("box_margin", 80))
    roi = [max(x - margin, 0), max(y - above, 0), w + 2 * margin, above]
    log.info("在 %s 上方找 box / 感叹号: %s", bname, roi)
    img = device.screencap()
    box = find_tpl(matcher, img, t.get("box", "dispatch_outpost_box"), roi)
    excl = find_tpl(matcher, img,
                    t.get("exclamation", "dispatch_outpast_exclamation_point"),
                    roi)

    if not box and not excl:
        log.info("上方既无 box 也无感叹号 → 该建筑当前无任务, 跳过")
        return False

    if box:
        # 阶段A
        log.info("发现 box %s (score=%.2f) → 阶段A: 点建筑进入",
                 box.center, box.score)
        actions.tap_point(*hit.center)
        time.sleep(enter_wait)
        if not _entered_building(device, matcher, bname):
            log.warning("建筑与主城 UI 都还在 → 可能没进建筑界面, 阶段A 中止")
            return False
        _reward_dialog(actions, t, "阶段A")
    else:
        log.info("只见感叹号 %s (上次未做该任务) → 直接进入阶段B", excl.center)

    # 阶段B: 点建筑 → 点「派遣」→ 弹窗 → 选择/确认/再次确认
    #   阶段A 的弹窗关掉后, 主城与建筑会重新出现, 但可能要等一两帧;
    #   所以这里等一下再找, 实在找不到就退回阶段A 前记录的位置(镜头没动, 位置仍有效).
    log.info("阶段B: 点建筑 → 等「派遣」按钮")
    bhit = _wait_tpl(device, matcher, bname, timeout=4.0)
    if bhit:
        log.info("建筑已重新出现 %s (score=%.2f)", bhit.center, bhit.score)
    else:
        bhit = hit
        log.info("建筑尚未重新出现, 退回阶段A 前记录的位置 %s", hit.center)
    actions.tap_point(*bhit.center)
    time.sleep(enter_wait)

    dbtn = _wait_tpl(device, matcher,
                     t.get("dispatch_btn", "dispatch_outpast_dispatch"),
                     timeout=float(t.get("dispatch_wait", 6.0)))
    if not dbtn:
        log.warning("未等到「派遣」按钮 [%s], 阶段B 中止",
                    t.get("dispatch_btn"))
        return True
    log.info("点击「派遣」%s (score=%.2f)", dbtn.center, dbtn.score)
    actions.tap_point(*dbtn.center)
    time.sleep(enter_wait)

    _reward_dialog(actions, t, "阶段B")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="主城: 点 inn 与宝箱")
    parser.add_argument("--live", action="store_true",
                        help="真正点击 (默认干跑)")
    parser.add_argument("--buildings-only", action="store_true",
                        help="只做建筑系列任务 (跳过 inn / 宝箱, 便于单独调试)")
    parser.add_argument("--gifts-only", action="store_true",
                        help="只做 3 个礼物任务 (friend / 公会签到 / 商店金币), "
                             "便于单独调试")
    parser.add_argument("--daily-task-only", action="store_true",
                        help="只做每日任务一键领取, 便于单独调试")
    args = parser.parse_args()

    cfg = load_yaml(ROOT / "config" / "city.yaml")
    settings = load_yaml(ROOT / "config" / "settings.yaml")

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

    mode = ("  建筑系列任务 (单独调试)" if args.buildings_only
            else "  礼物任务 (单独调试)" if args.gifts_only
            else "  每日任务一键领取 (单独调试)" if args.daily_task_only
            else "  主城: 点 inn 与宝箱")
    print("=" * 60)
    print(mode + ("  (实跑)" if args.live else "  (干跑, 不会真点击)"))
    print("=" * 60)

    if args.buildings_only:
        run_buildings(device, matcher, actions, cfg)
    elif args.gifts_only:
        # 单独跑礼物任务: 先确保在主城(底部栏按钮才可见), 再依次跑三个任务
        ensure_main_city(device, matcher, actions, cfg)
        run_gift_tasks(device, matcher, actions, Coords(), cfg)
        # 收尾再确保一次回主城 —— 礼物任务允许停在看板娘页面,
        # 而后续任务都要在主城做, 所以这里统一切回来.
        ensure_main_city(device, matcher, actions, cfg)
    elif args.daily_task_only:
        # 单独跑每日任务一键领取: 同样先确保在主城, 跑完再确保回主城
        ensure_main_city(device, matcher, actions, cfg)
        run_daily_task_receive(device, matcher, actions, Coords(), cfg)
        ensure_main_city(device, matcher, actions, cfg)
    else:
        run_city_tasks(device, matcher, actions, cfg, Coords())

    device.close()
    if not args.live:
        print("干跑结束 (未真正点击)。确认后加 --live 实跑。")
    else:
        print("主城任务完成。")


if __name__ == "__main__":
    main()
