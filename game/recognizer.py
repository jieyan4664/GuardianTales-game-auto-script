"""界面识别器: 依据 states.yaml 规则判定当前界面.

识别策略:
- 严格按规则顺序匹配 (从上到下), 命中即返回
- 弹窗类规则应放在最前
- 阈值可在 rules 中逐个覆盖
"""
from __future__ import annotations

from pathlib import Path

import yaml

from core.frame import Frame
from core.logger import get_logger
from core.vision import MatchResult, Matcher
from game.states import State

log = get_logger("recognize")


class Rule:
    __slots__ = ("state", "template", "threshold", "roi")

    def __init__(self, raw: dict) -> None:
        self.state = State[raw["state"]]
        self.template = raw["template"]
        self.threshold = raw.get("threshold")
        self.roi = raw.get("roi")


class Recognizer:
    def __init__(self, matcher: Matcher, rules_path: Path) -> None:
        self.matcher = matcher
        self.rules_path = Path(rules_path)
        self.rules: list[Rule] = []
        self.load()

    def load(self) -> None:
        if not self.rules_path.is_file():
            log.warning("规则文件不存在: %s", self.rules_path)
            self.rules = []
            return
        data = yaml.safe_load(self.rules_path.read_text(encoding="utf-8")) or {}

        rules: list[Rule] = []
        skipped: list[str] = []
        for raw in data.get("rules", []):
            try:
                rule = Rule(raw)
            except Exception as exc:
                log.warning("跳过无效规则 %s (%s)", raw, exc)
                continue
            # 校验模板文件是否存在. 若不校验, 匹配到该规则时会抛
            # FileNotFoundError 直接中断主流程 —— 模板被删/改名后很容易踩到.
            if not self._template_exists(rule.template):
                skipped.append(rule.template)
                continue
            rules.append(rule)

        self.rules = rules
        if skipped:
            log.warning(
                "跳过 %d 条规则 (模板文件缺失): %s",
                len(skipped), ", ".join(skipped),
            )
        log.info("已载入 %d 条界面规则", len(self.rules))

    def _template_exists(self, name: str) -> bool:
        """检查模板文件是否存在 (只查路径, 不加载图像)."""
        base = Path(self.matcher.dir)
        if (base / name).is_file():
            return True
        if (base / f"{name}.png").is_file():
            return True
        return bool(list(base.rglob(f"{name}*")))

    def reload(self) -> None:
        """热重载规则与已缓存模板."""
        self.load()
        self.matcher.reload()

    def detect(self, frame: Frame) -> tuple[State, MatchResult | None]:
        for rule in self.rules:
            hit = self.matcher.find(
                frame.image, rule.template, rule.threshold, rule.roi
            )
            if hit:
                return rule.state, hit
        return State.UNKNOWN, None