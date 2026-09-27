"""游戏脚本入口.

用法:
    python main.py                  # 干跑 (默认 dry_run=true)
    python main.py --live           # 实跑, 真正执行点击
    python main.py --adb D:/path/adb.exe --serial 127.0.0.1:16384
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.capture import create_screenshot_source                        # noqa: E402
from core.device import AdbDevice                                       # noqa: E402
from core.env import detect_serial, environment_report, find_adb         # noqa: E402
from core.logger import get_logger                                      # noqa: E402
from core.vision import Matcher                                         # noqa: E402
from game.actions import Actions                                        # noqa: E402
from game.engine import Context, Engine                                 # noqa: E402
from game.handlers.main import MainCityHandler                          # noqa: E402
from game.handlers.menu_panel import MenuPanelHandler                   # noqa: E402
from game.handlers.play_menu import PlayMenuHandler                     # noqa: E402
from game.handlers.unknown import UnknownHandler                        # noqa: E402
from game.states import State                                           # noqa: E402
from game.recognizer import Recognizer                                  # noqa: E402

log = get_logger("main")


def load_settings(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def build_device(settings: dict, args) -> AdbDevice:
    cfg = settings.get("device", {})

    adb_path = args.adb or cfg.get("adb_path")
    if not adb_path or adb_path == "auto":
        adb_path = find_adb()
    if not adb_path:
        sys.exit("[ERROR] 未找到 adb.exe, 请用 --adb 指定路径")

    serial = args.serial or cfg.get("serial")
    if not serial or serial == "auto":
        serial = detect_serial(adb_path)
    if not serial:
        sys.exit("[ERROR] 未探测到模拟器, 请确认 MuMu 已启动并开启 ADB 调试")

    action_cfg = settings.get("action", {})
    device = AdbDevice(
        adb_path=adb_path,
        serial=serial,
        dry_run=bool(action_cfg.get("dry_run", True)) and not args.live,
        tap_jitter=int(action_cfg.get("tap_jitter", 0)),
        min_tap_interval=float(action_cfg.get("min_tap_interval", 0.05)),
        rotate=int(cfg.get("rotate", 0)),
        screenshot_source=create_screenshot_source(settings),
    )
    if device.rotate:
        log.info("截图后旋转 %d°, 逻辑坐标将按旋转后尺寸", device.rotate)
    log.info("设备已就绪\n%s", environment_report(adb_path, serial))
    return device


def main() -> None:
    parser = argparse.ArgumentParser(description="图像识别游戏脚本 (通用底座)")
    parser.add_argument(
        "-c", "--config",
        default=str(ROOT / "config" / "settings.yaml"),
    )
    parser.add_argument("--adb", default=None, help="手动指定 adb.exe 路径")
    parser.add_argument(
        "--serial", default=None,
        help="手动指定设备序列号, 如 127.0.0.1:16384",
    )
    parser.add_argument(
        "--live", action="store_true",
        help="★ 真正执行点击 (不加此参数则只打印不点击)",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="只跑一轮就退出, 用于 dry-run 联调",
    )
    args = parser.parse_args()

    settings = load_settings(Path(args.config))
    vision_cfg = settings.get("vision", {})

    matcher = Matcher(
        threshold=float(vision_cfg.get("threshold", 0.85)),
        scales=vision_cfg.get("scales", [1.0]),
        method=vision_cfg.get("method", "ccoeff"),
    )
    recognizer = Recognizer(matcher, ROOT / "config" / "states.yaml")
    device = build_device(settings, args)

    actions = Actions(device, matcher)
    ctx = Context(
        device=device,
        matcher=matcher,
        actions=actions,
        recognizer=recognizer,
        settings=settings,
    )

    handlers: dict = {
        State.MAIN: MainCityHandler(),
        State.MENU: MenuPanelHandler(),
        State.PLAY_MENU: PlayMenuHandler(),
    }
    # 接入更多玩法时在这里注册: handlers[State.XXX] = XxxHandler()

    engine = Engine(ctx, handlers, UnknownHandler())
    if args.once:
        # 单步模式: 跑一次识别 + 打印将执行的动作, 然后退出
        state = engine.step()
        if ctx.frame is not None:
            log.info("当前帧: %dx%d", ctx.frame.width, ctx.frame.height)
        if state in handlers:
            log.info("将交给 Handler: %s", state.name)
            handlers[state].handle(ctx)
        else:
            log.info("识别到状态: %s (暂无处理器)", state.name)
        device.close()
        return

    try:
        engine.run()
    finally:
        device.close()


if __name__ == "__main__":
    main()