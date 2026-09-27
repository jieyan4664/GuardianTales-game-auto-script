"""游戏界面状态枚举. 在此扩充具体玩法状态."""
from __future__ import annotations

from enum import Enum, auto


class State(Enum):
    UNKNOWN = auto()
    LOADING = auto()       # 加载 / 转场中
    MAIN = auto()          # 主界面 (世界地图 / 大本营)
    MENU = auto()          # 菜单面板 (点击主城菜单后展开的面板)
    PLAY_MENU = auto()     # 玩法选择页 (点击 play 后, 带左右翻页箭头的页面)
    PVP = auto()           # PVP 列表页 (点击 pvp 标签后, 5 张卡片横向排列)
    COLOSSEUM = auto()     # 圆形角斗场内部 (点角斗场卡片进入后, 右侧 3 张敌方卡片)
    RIFT_MENU = auto()     # 裂痕页 (选中"裂痕"标签后的 3 个次级选项 + 6 张副本卡片)
    # 按次级细分: 靠各卡片上"不重复的图像"识别, 可精确到当前停在哪个次级
    RIFT_EVOLUTION = auto()  # 裂痕 → 进化石副本卡片页
    RIFT_MYTH = auto()       # 裂痕 → 开花石副本卡片页
    RIFT_RESOURCE = auto()   # 裂痕 → 资源副本卡片页
    SWEEP_DIALOG = auto()  # 扫荡弹窗 (加减次数/滑条/焕发勾选/取消/扫荡)
    SWEEP_RESULT = auto()  # 扫荡完成 (确认按钮)
    RESULT = auto()                # 战斗结算
    POPUP = auto()         # 通用弹窗
    # 按后续玩法扩展:
    # STAGE_SELECT = auto()    # 关卡选择
    # BATTLE = auto()          # 战斗中
    # TEAM_SETUP = auto()      # 编队
    # SHOP = auto()            # 商店
    # GACHA = auto()           # 抽卡
    # ...


# 状态优先级: 弹窗类必须高于其它界面, 因为弹窗会覆盖在游戏画面之上
ORDERED_PRIORITY = [
    State.POPUP,
    State.LOADING,
    State.RESULT,
    State.MENU,
    State.SWEEP_RESULT,
    State.SWEEP_DIALOG,
    State.RIFT_RESOURCE,
    State.RIFT_MYTH,
    State.RIFT_EVOLUTION,
    State.RIFT_MENU,
    State.COLOSSEUM,
    State.PVP,
    State.PLAY_MENU,
    State.MAIN,
    State.UNKNOWN,
]