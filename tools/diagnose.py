"""画面诊断: 查看当前画面与各模板的实际匹配度.

用途: 排查"明明在某个界面, 脚本却识别不出来"的问题.
与识别流程的区别: 识别只告诉你命中/未命中, 本工具给出每个模板的
实际相似度, 便于判断是"差一点"(调阈值) 还是"完全不对"(模板/画面不符).

用法:
    python tools/diagnose.py                # 看全部
    python tools/diagnose.py -s MAIN        # 只看主城相关模板

除状态规则外, 还会额外判定"是否处于圆形角斗场的战斗画面":
    匹配到 colosseum_stat_dps / heal / hp 中任意一个即算在战斗中
    (模板清单取自 config/pvp.yaml 的 battle_hud_templates)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.frame import Frame                     # noqa: E402
from core.device import AdbDevice                # noqa: E402
from core.env import detect_serial, find_adb     # noqa: E402
from core.logger import get_logger               # noqa: E402
from core.vision import Matcher                  # noqa: E402
from game.recognizer import Recognizer           # noqa: E402

log = get_logger("diagnose")


def main() -> None:
    parser = argparse.ArgumentParser(description="诊断当前画面的识别情况")
    parser.add_argument(
        "-s", "--state", default=None,
        help="只看某个状态的模板 (如 MAIN / PLAY_MENU / SWEEP_DIALOG)",
    )
    args = parser.parse_args()

    settings = yaml.safe_load(
        (ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")
    ) or {}
    adb_path = find_adb()
    if not adb_path:
        sys.exit("[ERROR] 未找到 adb.exe")
    serial = settings.get("device", {}).get("serial")
    if not serial or serial == "auto":
        serial = detect_serial(adb_path)
    if not serial:
        sys.exit("[ERROR] 未探测到模拟器, 请确认雷电已启动")

    device = AdbDevice(
        adb_path=adb_path,
        serial=serial,
        rotate=int(settings.get("device", {}).get("rotate", 0)),
    )
    vision_cfg = settings.get("vision", {})
    matcher = Matcher(
        threshold=float(vision_cfg.get("threshold", 0.85)),
        scales=vision_cfg.get("scales", [1.0]),
        method=vision_cfg.get("method", "ccoeff"),
    )
    recognizer = Recognizer(matcher, ROOT / "config" / "states.yaml")

    img = device.screencap()
    print(f"画面尺寸  : {img.shape[1]}x{img.shape[0]}")
    print(f"默认阈值  : {matcher.threshold}")
    print(f"规则总数  : {len(recognizer.rules)}")
    print()

    state, hit = recognizer.detect(Frame(img))
    print(f"当前判定  : {state.name}")
    if hit:
        print(f"  命中模板: {hit.name}  相似度 {hit.score:.3f}  位置 {hit.center}")
    else:
        print("  未命中任何规则 (UNKNOWN)")
    print()

    print("各模板在当前画面的最高相似度:")
    print(f"  {'状态':<14}{'模板':<22}{'相似度':<9}判定")
    print("  " + "-" * 58)
    shown = 0
    for rule in recognizer.rules:
        if args.state and rule.state.name != args.state:
            continue
        shown += 1
        try:
            s = matcher.score(img, rule.template)
        except FileNotFoundError:
            print(f"  {rule.state.name:<14}{rule.template:<22}{'缺失':<9}-")
            continue
        th = rule.threshold if rule.threshold is not None else matcher.threshold
        mark = "命中" if s >= th else "未达阈值"
        print(f"  {rule.state.name:<14}{rule.template:<22}{s:<9.3f}{mark} (阈值 {th})")

    if shown == 0:
        print(f"  (状态 {args.state} 下没有规则)")
    print()

    # ---- 圆形角斗场: 是否在战斗画面 ----
    # 判定规则: 匹配到 colosseum_stat_dps / heal / hp 中任意一个,
    #           即认为当前处于【圆形角斗场的战斗画面】.
    # 模板清单取自 config/pvp.yaml 的 battle_hud_templates, 与 pvp.py 保持同源.
    pvp_cfg = yaml.safe_load(
        (ROOT / "config" / "pvp.yaml").read_text(encoding="utf-8")
    ) or {}
    hud_tpls = pvp_cfg.get("battle_hud_templates") or []
    if hud_tpls:
        hud_roi = pvp_cfg.get("battle_hud_roi")
        print("圆形角斗场战斗画面判定 (限定区域内命中任意一个即算在战斗中):")
        print(f"  搜索区域 {hud_roi or '全屏'}")
        best = None
        for name in hud_tpls:
            try:
                s = matcher.score(img, name, roi=hud_roi)
            except FileNotFoundError:
                print(f"  {name:<24}{'缺失':<9}-")
                continue
            h = (matcher.find(img, name, roi=hud_roi)
                 if s >= matcher.threshold else None)
            if not h:
                print(f"  {name:<24}{s:<9.3f}未达阈值 (阈值 {matcher.threshold})")
                continue
            print(f"  {name:<24}{s:<9.3f}命中 @ {h.center}")
            if best is None or s > best[1]:
                best = (name, s)
        if best:
            print(f"  → 判定: 在圆形角斗场【战斗画面】"
                  f" ({best[0]}, {best[1]:.3f})")
        else:
            print("  → 判定: 不在战斗画面")
            print("     (上面若有高分命中却被判偏白, 就是结算画面的白色同款图标)")
        print()

    print("判读: 相似度接近阈值(差 0.05 内) → 调低阈值即可;")
    print("      普遍很低(<0.5) → 画面与模板不符, 可能不在此界面或模板已过时。")
    print("      命中但位置可疑(不在按钮应在的位置) → 多半是误匹配, 需限定搜索区域。")

    device.close()


if __name__ == "__main__":
    main()
