"""坐标管理: 读取 config/coords.yaml, 按名字取点击中心点.

为什么单独放一个配置文件:
- 纯文字选项(推荐/裂痕/pvp/...)用模板匹配极易失手(笔画细、抗锯齿敏感),
  改用固定坐标点击; 坐标写在这里, 换分辨率或微调位置时只改这一个文件.
- 与模板(assets/templates)分离: 模板用于"识别状态", 坐标用于"点击文字选项".

坐标格式 [x, y, w, h], 取中心点 (x + w//2, y + h//2).
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COORDS_PATH = ROOT / "config" / "coords.yaml"


class Coords:
    def __init__(self, path: Path | str = COORDS_PATH) -> None:
        self.path = Path(path)
        self.data: dict = {}
        self.load()

    def load(self) -> None:
        if self.path.is_file():
            self.data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        else:
            self.data = {}

    def reload(self) -> None:
        self.load()

    def box(self, name: str, group: str | None = None) -> list[int] | None:
        """取 [x, y, w, h].

        支持嵌套路径:
            box("rift", "play_menu")             → 两层 (组.名)
            box("void.sweep", "evolution_cards") → 多层 (组.属性.功能)
            box("void.sweep")                    → 跨组查找
        """
        parts = name.split(".")

        def _dig(node) -> list[int] | None:
            for part in parts:
                if not isinstance(node, dict) or part not in node:
                    return None
                node = node[part]
            return list(node) if isinstance(node, list) else None

        if group is not None:
            return _dig(self.data.get(group))

        # group 为空: 跨组逐个尝试
        for g in self.data.values():
            if isinstance(g, dict):
                got = _dig(g)
                if got is not None:
                    return got
        return _dig(self.data)

    def center(self, name: str, group: str | None = None) -> tuple[int, int] | None:
        """取中心点 (cx, cy), 找不到返回 None."""
        b = self.box(name, group)
        if not b or len(b) != 4:
            return None
        x, y, w, h = (int(v) for v in b)
        if w <= 0 or h <= 0:
            return None
        return (x + w // 2, y + h // 2)

    def tap(self, actions, name: str, group: str | None = None,
            wait: float | None = None) -> bool:
        """按坐标名点击. 需要传入 Actions (用于 tap_point 与等待)."""
        pt = self.center(name, group)
        if pt is None:
            return False
        actions.tap_point(pt[0], pt[1], wait=wait)
        return True
