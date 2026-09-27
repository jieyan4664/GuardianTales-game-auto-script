"""引擎: 截图 → 识别 → 分发到状态处理器 → 执行动作.

主循环结构:
    1. AdbDevice.screencap → Frame
    2. Recognizer.detect(frame) → (State, MatchResult)
    3. Engine.step() 根据 State 找到 Handler
    4. Handler.handle(ctx) 执行业务逻辑
    5. sleep(interval) 节流
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from core.device import AdbDevice
from core.frame import Frame
from core.logger import get_logger, save_debug_frame
from core.vision import Matcher
from game.actions import Actions
from game.recognizer import Recognizer
from game.states import State

log = get_logger("engine")


@dataclass
class Context:
    """运行上下文 (依赖注入容器), 供各 Handler 使用.

    设计原则: 不让 Handler 直接 import 任何东西, 全部从 ctx 拿,
    便于替换实现做单测.
    """
    device: AdbDevice
    matcher: Matcher
    actions: Actions
    recognizer: Recognizer
    settings: dict
    frame: Frame | None = None
    round_index: int = 0
    state_log: list[State] = field(default_factory=list)

    def remember(self, state: State) -> None:
        self.state_log.append(state)
        if len(self.state_log) > 50:
            del self.state_log[:25]


class Engine:
    def __init__(
        self,
        ctx: Context,
        handlers: dict[State, "Handler"],
        unknown_handler=None,
    ) -> None:
        self.ctx = ctx
        self.handlers = handlers
        self.unknown_handler = unknown_handler
        self.running = False

        loop_cfg = ctx.settings.get("loop", {})
        self.interval = float(loop_cfg.get("interval", 0.35))
        self.max_unknown = int(loop_cfg.get("max_unknown_rounds", 40))
        self.log_unknown = bool(loop_cfg.get("log_unknown_frame", True))

    def step(self) -> State:
        """单步执行: 截图 → 识别 → 更新 ctx. 返回识别到的状态."""
        frame = Frame(self.ctx.device.screencap(), index=self.ctx.round_index)
        self.ctx.frame = frame
        state, hit = self.ctx.recognizer.detect(frame)
        self.ctx.remember(state)
        return state

    def run(self) -> None:
        self.running = True
        unknown_streak = 0
        log.info("引擎启动, 循环间隔 %.2fs", self.interval)

        while self.running:
            cycle_start = time.perf_counter()
            try:
                state = self.step()

                if state is State.UNKNOWN:
                    unknown_streak += 1
                    log.debug("未知界面 x%d", unknown_streak)
                    if self.log_unknown and unknown_streak == self.max_unknown:
                        path = save_debug_frame(
                            self.ctx.frame.image, "unknown"
                        )
                        log.warning(
                            "连续 %d 轮未识别, 证据帧: %s",
                            unknown_streak, path,
                        )
                    if (
                        unknown_streak >= self.max_unknown
                        and self.unknown_handler
                    ):
                        self.unknown_handler.handle(self.ctx)
                        unknown_streak = 0
                else:
                    if unknown_streak:
                        log.info("恢复识别: %s", state.name)
                    unknown_streak = 0
                    handler = self.handlers.get(state)
                    if handler:
                        handler.handle(self.ctx)
                    else:
                        log.debug("状态 %s 暂无处理器", state.name)

                self.ctx.round_index += 1

            except KeyboardInterrupt:
                log.info("收到中断信号, 退出")
                self.running = False
                break
            except Exception:
                log.exception("循环异常, 本轮跳过")
                time.sleep(1.0)

            elapsed = time.perf_counter() - cycle_start
            if elapsed < self.interval:
                time.sleep(self.interval - elapsed)

        log.info("引擎已停止")