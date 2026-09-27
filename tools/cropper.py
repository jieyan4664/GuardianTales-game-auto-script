"""模板采集器: 在画面上框选区域 → 自动切成模板图 → 自动写入规则.

用法:
    python tools/cropper.py                       # 取最新截图
    python tools/cropper.py -f xxx.png            # 指定截图文件
    python tools/cropper.py --live                # 从模拟器实时画面采
    python tools/cropper.py --save                # 自动写入 states.yaml
    python tools/cropper.py --state RESULT        # 指定归属状态

⚠ 关键点: 显示窗口的坐标必须除以缩放比, 才能得到原图坐标系.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEMPLATE_DIR = ROOT / "assets" / "templates"
STATES_YAML = ROOT / "config" / "states.yaml"

# 显示窗口最长边. 2560x1400 的画面超过多数显示器, 必须缩放到可操作尺寸
DISPLAY_MAX = 1280


def load_image(args) -> np.ndarray:
    if args.live:
        # 优先窗口截图: MuMu 下 adb screencap 只能拿到桌面
        settings: dict = {}
        try:
            settings = yaml.safe_load(
                (ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")
            ) or {}
        except OSError:
            pass

        try:
            from core.capture import create_screenshot_source
            source = create_screenshot_source(settings)
            if source is not None:
                img = source()
                if img is not None:
                    print("截图来源: Windows 窗口 (PrintWindow)")
                    return img
                print("[WARN] 窗口截图失败, 回退 adb")
        except Exception as exc:
            print(f"[WARN] 窗口截图不可用 ({exc}), 回退 adb")

        from core.device import AdbDevice
        from core.env import detect_serial, find_adb

        adb_path = find_adb()
        serial = detect_serial(adb_path)
        if not serial:
            sys.exit("[ERROR] 未探测到模拟器")
        print("截图来源: adb screencap (可能只截到桌面)")
        return AdbDevice(adb_path, serial).screencap()

    if args.file:
        path = Path(args.file)
    else:
        shots = sorted((ROOT / "assets" / "screenshots").glob("*.png"))
        if not shots:
            sys.exit("[ERROR] assets/screenshots 下没有截图, 请先跑 tools/grab.py")
        path = shots[-1]
        print(f"使用最新截图: {path}")

    image = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        sys.exit(f"[ERROR] 图片读取失败: {path}")
    return image


def pick_roi(image: np.ndarray) -> tuple[int, int, int, int] | None:
    """在缩放后的画面上框选, 返回原图坐标系下的 [x, y, w, h]."""
    h, w = image.shape[:2]
    scale = min(1.0, DISPLAY_MAX / max(w, h))
    if scale < 1.0:
        shown = cv2.resize(
            image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA
        )
        print(
            f"显示缩放: {w}x{h} → {shown.shape[1]}x{shown.shape[0]} "
            f"(scale={scale:.3f})"
        )
    else:
        shown = image

    window = "cropper | 框选后按 Enter 确认, 按 C 取消"
    cv2.namedWindow(window, cv2.WINDOW_AUTOSIZE)
    # 截图时 MuMu 窗口被置顶过, 这里把 cropper 也置顶, 否则会被 MuMu 盖住
    cv2.setWindowProperty(window, cv2.WND_PROP_TOPMOST, 1)
    # selectROI 返回的是「显示图」坐标, 必须除以 scale 还原成原图坐标
    x, y, w2, h2 = cv2.selectROI(window, shown, showCrosshair=True, fromCenter=False)
    cv2.destroyWindow(window)

    if w2 == 0 or h2 == 0:
        return None

    inv = 1.0 / scale
    return (
        int(round(x * inv)),
        int(round(y * inv)),
        int(round(w2 * inv)),
        int(round(h2 * inv)),
    )


def save_template(image: np.ndarray, name: str, box) -> Path:
    x, y, w, h = box
    patch = image[y:y + h, x:x + w]
    out = TEMPLATE_DIR / f"{name}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".png", patch)
    if not ok:
        sys.exit("[ERROR] 模板编码失败")
    out.write_bytes(buf.tobytes())
    return out


def append_rule(name: str, state: str, threshold: float, roi, save: bool) -> None:
    if not STATES_YAML.exists():
        STATES_YAML.write_text("rules: []\n", encoding="utf-8")

    data = yaml.safe_load(STATES_YAML.read_text(encoding="utf-8")) or {}
    rules = data.setdefault("rules", [])
    rule = {
        "state": state,
        "template": f"{name}.png",
        "threshold": threshold,
        "roi": list(roi) if roi else None,
        "_source": "cropper",
    }
    # 插到最前 (弹窗类优先匹配)
    rules.insert(0, rule)
    if save:
        STATES_YAML.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        print(f"[✓] 已写入规则 → {STATES_YAML}")
    else:
        print("[预览] 规则 (加 --save 才写入):")
        print(yaml.safe_dump(rule, allow_unicode=True, sort_keys=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="模板采集器")
    parser.add_argument("-f", "--file", default=None, help="指定截图路径")
    parser.add_argument("--live", action="store_true", help="从模拟器实时截图")
    parser.add_argument("--name", default=None, help="模板名 (不填则交互输入)")
    parser.add_argument("--state", default="MAIN", help="归属的界面状态名")
    parser.add_argument("--threshold", type=float, default=0.85)
    parser.add_argument("--save", action="store_true", help="自动写入 states.yaml")
    parser.add_argument(
        "--roi-mode", action="store_true",
        help="只记录 ROI 坐标, 不保存模板图 (用于记录大区域搜索框)",
    )
    args = parser.parse_args()

    image = load_image(args)
    print(f"原图尺寸: {image.shape[1]}x{image.shape[0]}")

    while True:
        box = pick_roi(image)
        if box is None:
            print("已取消, 退出")
            break

        name = args.name or input("\n模板名 (回车结束): ").strip()
        if not name:
            break

        if not args.roi_mode:
            path = save_template(image, name, box)
            print(f"[✓] 模板已保存: {path}")

        print(f"    ROI 坐标 [x, y, w, h] = {list(box)}")
        if args.roi_mode:
            # --roi-mode 只记录坐标, 没有模板图; 若写进规则会导致运行时找不到模板而崩溃
            print("    (--roi-mode: 未保存模板图, 故不写入规则; 请手动记录上面的坐标)")
        else:
            append_rule(name, args.state, args.threshold, box, args.save)

        # 在原图上画框, 方便连续采多张
        x, y, w, h = box
        cv2.rectangle(image, (x, y), (x + w, y + h), (0, 255, 0), 2)
        cv2.putText(
            image, name, (x, max(15, y - 6)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2,
        )


if __name__ == "__main__":
    main()