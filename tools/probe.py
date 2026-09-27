"""实时识别可视化: 循环截图, 标注所有模板的当前位置与最高相似度.

调阈值必备工具:
- 实时看到哪个模板匹配分最高、是否过线
- 按 +/- 调阈值, 按 R 热重载, 按 S 保存当前帧
- 退出 Q

输出窗口按比例缩放到 DISPLAY_MAX 以内, 2K 屏幕也能看.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import cv2
import numpy as np
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.device import AdbDevice                                 # noqa: E402
from core.env import detect_serial, find_adb                      # noqa: E402
from core.logger import get_logger                                # noqa: E402
from core.vision import Matcher                                   # noqa: E402

log = get_logger("probe")
STATES_YAML = ROOT / "config" / "states.yaml"
SHOT_DIR = ROOT / "assets" / "screenshots"
SHOT_DIR.mkdir(parents=True, exist_ok=True)
DISPLAY_MAX = 1280


def load_rules() -> list[dict]:
    if not STATES_YAML.exists():
        return []
    data = yaml.safe_load(STATES_YAML.read_text(encoding="utf-8")) or {}
    return data.get("rules", [])


def draw(frame: np.ndarray, results, threshold: float) -> np.ndarray:
    """在画面上画识别结果. 颜色: 绿=过阈值, 黄=接近阈值, 红=低于阈值."""
    out = frame.copy()
    cv2.putText(
        out,
        f"Threshold: {threshold:.2f}  |  Q:quit  S:save  R:reload  +/-:thresh",
        (10, 30),
        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2,
    )
    for r in results:
        x, y, ww, hh = r.box
        if r.score >= threshold:
            color = (0, 255, 0)
        elif r.score >= threshold - 0.1:
            color = (0, 200, 255)
        else:
            color = (0, 80, 255)
        cv2.rectangle(out, (x, y), (x + ww, y + hh), color, 2)
        label = f"{r.name} {r.score:.3f}"
        cv2.putText(
            out, label, (x, max(15, y - 6)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2,
        )
    return out


def fit_display(image: np.ndarray) -> np.ndarray:
    h, w = image.shape[:2]
    if w <= DISPLAY_MAX:
        return image
    scale = DISPLAY_MAX / w
    return cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)


def main() -> None:
    parser = argparse.ArgumentParser(description="实时识别探针")
    parser.add_argument("--threshold", type=float, default=0.85)
    parser.add_argument("--interval", type=float, default=0.15)
    args = parser.parse_args()

    adb_path = find_adb()
    if not adb_path:
        sys.exit("[ERROR] 未找到 adb.exe")
    serial = detect_serial(adb_path)
    if not serial:
        sys.exit("[ERROR] 未探测到模拟器")

    device = AdbDevice(adb_path, serial, dry_run=True)
    matcher = Matcher(threshold=args.threshold)

    threshold = args.threshold
    window = "probe | Q:quit  S:save  R:reload  +/-:threshold"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)

    print(f"[probe] 启动 | threshold={threshold:.2f}")
    print("        Q 退出 | S 保存当前帧 | R 热重载 | +/- 调阈值")

    while True:
        image = device.screencap()
        rules = load_rules()

        results = []
        for rule in rules:
            try:
                hit = matcher.find(
                    image,
                    rule["template"],
                    threshold=-1.0,
                    roi=rule.get("roi"),
                )
                if hit and hit.score >= 0.3:
                    results.append(hit)
            except Exception as exc:
                log.debug("匹配 %s 失败: %s", rule.get("template"), exc)

        vis = fit_display(draw(image, results, threshold))
        cv2.imshow(window, vis)

        # 等待按键, 同时节流
        key = cv2.waitKey(int(args.interval * 1000)) & 0xFF
        if key == ord("q") or key == 27:  # Q / ESC
            break
        elif key == ord("s"):
            ts = time.strftime("%Y%m%d_%H%M%S")
            out = SHOT_DIR / f"probe_{ts}.png"
            cv2.imwrite(str(out), image)
            print(f"[✓] 已保存 {out}")
        elif key == ord("r"):
            matcher.reload()
            print("[✓] 模板已热重载")
        elif key in (ord("+"), ord("=")):
            threshold = min(1.0, threshold + 0.02)
            print(f"[threshold] {threshold:.2f}")
        elif key in (ord("-"), ord("_")):
            threshold = max(0.0, threshold - 0.02)
            print(f"[threshold] {threshold:.2f}")

    cv2.destroyAllWindows()
    device.close()


if __name__ == "__main__":
    main()