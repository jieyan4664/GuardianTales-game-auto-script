这个脚本用于我个人使用，所以很多地方没有做成通用，若你对这个脚本感兴趣，可以参考这个脚本，有问题可以联系我（2409229406@qq.com）

# 图像识别游戏脚本框架

基于 Python + OpenCV + adb 的通用底座, 面向横屏手游 (当前目标《坎特伯雷公主与骑士唤醒冠军之剑的奇幻冒险》),
运行于**雷电模拟器** (实测 设备 1920x1080 = 实际帧 1920x1080, 无需旋转)。
`core/env.py` 也内置了夜神 / 蓝叠 的 adb 路径与端口探测, 换模拟器一般不用改代码。

## 设计目标

- **分层隔离**: 采集 / 感知 / 决策 / 执行四层严格分离, 换游戏只改 `game/` 与 `assets/`, `core/` 完全复用。
- **状态机驱动**: 把游戏界面抽象成有限状态, 杜绝散装 `if 找到A: 点A` 写法。
- **数据驱动**: 模板路径、阈值、ROI、点击坐标全部落在 `config/*.yaml`, 调参不用改代码。
- **帧缓存**: 一轮决策内只截一次图, 所有识别复用同一帧。
- **Dry-run 默认开启**: 所有任务脚本默认只打印不点击, 加 `--live` 才真操作。

## 目录结构

```
.
├── main.py                     # 状态机骨架入口 (见下方"关于 main.py")
├── config/
│   ├── settings.yaml           # 设备 / 截图 / 循环 / 视觉 / 动作
│   ├── states.yaml             # 界面状态识别规则 (cropper --save 自动写入)
│   ├── coords.yaml             # 固定点击坐标表 [x, y, w, h]
│   ├── daily.yaml              # 日常一条龙的步骤编排
│   ├── city.yaml               # 主城任务 (inn / 宝箱 / 礼物 / 建筑 / 每日领取)
│   ├── pvp.yaml                # 圆形角斗场的战斗判定配置
│   ├── sweep.yaml              # 消耗焕发次数 扫荡配置
│   └── sweep_normal.yaml       # 普通扫荡 (指定次数) 配置
├── core/                       # 基础设施, 与游戏无关
│   ├── logger.py
│   ├── env.py                  # adb 路径 / 端口 自动探测
│   ├── device.py               # 截图 / 点击 / 滑动
│   ├── capture.py              # 截图源 (adb / 窗口)
│   ├── frame.py                # 帧缓存 + ROI
│   ├── coords.py               # 坐标表读取 (Coords)
│   └── vision.py               # 模板匹配 / 多尺度 / 颜色
├── game/                       # 游戏特定逻辑
│   ├── states.py               # State 枚举
│   ├── recognizer.py           # 界面识别
│   ├── actions.py              # 原子动作
│   ├── engine.py               # 状态机主循环
│   └── handlers/               # 各状态处理器
├── assets/
│   ├── templates/              # 模板图 (平铺, 按名字引用)
│   └── screenshots/            # 调试截图
├── logs/debug/                 # 识别失败时自动留存的证据帧
└── tools/                      # 任务脚本 (实际业务都在这里)
    ├── grab.py                 # 环境自检 + 截图
    ├── cropper.py              # 框选采集模板 / 坐标
    ├── probe.py                # 实时识别可视化, 调阈值
    ├── diagnose.py             # 画面诊断: 看各模板实测相似度
    ├── daily.py                # 日常一条龙 (编排以下各步)
    ├── city.py                 # 主城任务
    ├── pvp.py                  # 圆形角斗场
    ├── sweep.py                # 消耗焕发次数
    └── sweep_normal.py         # 普通扫荡 (指定次数)
```

## 快速开始

### 1. 安装依赖
```bash
pip install -r requirements.txt
```

### 2. 启动模拟器
确认模拟器已运行, 目标游戏已能正常打开。
分辨率基准是 **1920x1080**; 改了分辨率所有坐标都要重新采集。

### 3. 环境自检
```bash
python tools/grab.py
```
自动探测 adb 路径、连接设备、连续截图评估性能。**这是第一步必跑**:
- 确认能连上模拟器
- 拿到实际帧分辨率 (如果游戏是竖屏, 这里看到的会是旋转后的尺寸)
- 确认截图耗时可接受 (单帧 > 400ms 建议降低模拟器分辨率)

### 4. 采集模板 / 坐标
```bash
python tools/cropper.py --live              # 存模板图
python tools/cropper.py --live --roi-mode   # 只记 ROI 坐标, 不存图
```
从模拟器实时画面框选区域。**位置固定、文字类的按钮一律用坐标** (`--roi-mode`),
**图形会变的才用模板** (`--live`) —— 坐标不受字体渲染影响, 比模板稳。

| 采集方式 | 存到哪 | 怎么用 |
|---|---|---|
| `--live` | `assets/templates/<名字>.png` (+ 可 `--save` 写 `states.yaml`) | 代码里 `matcher.find(img, "名字")` |
| `--live --roi-mode` | 只打印 `ROI 坐标 [x,y,w,h]` | 手动填进 `config/coords.yaml` |

### 5. 调阈值
```bash
python tools/probe.py           # 实时看各模板相似度: Q 退出 / S 存帧 / R 热重载 / +/- 调阈值
python tools/diagnose.py -s MAIN   # 一次性打印某状态下所有模板的实测分
```

### 6. 干跑 → 实跑
```bash
python tools/daily.py           # 干跑 (只打印计划动作)
python tools/daily.py --live    # 实跑
```

## 常用脚本

所有任务脚本**默认干跑**, 加 `--live` 才真正点击。

| 脚本 | 用途 | 常用命令 |
|---|---|---|
| `tools/grab.py` | 环境自检: adb / 设备 / 分辨率 / 截图耗时 | `python tools/grab.py` |
| `tools/cropper.py` | 框选采集模板图或 ROI 坐标 | `--live` / `--live --roi-mode` |
| `tools/probe.py` | 实时显示所有模板的最高相似度 | `python tools/probe.py` |
| `tools/diagnose.py` | 诊断画面与模板的匹配度 (排查"识别不出来") | `python tools/diagnose.py -s MAIN` |
| `tools/daily.py` | **日常一条龙**: 按 `daily.yaml` 顺序跑完全部日常 | `python tools/daily.py --live` |
| `tools/city.py` | 主城任务: inn + 宝箱 + 礼物 + 拖动找建筑 + 每日领取 | `--gifts-only` / `--buildings-only` / `--daily-task-only` |
| `tools/pvp.py` | 圆形角斗场: 进入并重复挑战指定对手 | `--repeat 10 --opponent 3 --live` |
| `tools/sweep.py` | 消耗**焕发次数**扫副本 | `python tools/sweep.py --from-main --live` |
| `tools/sweep_normal.py` | **普通扫荡**, 次数由你指定 (不涉及焕发) | `python tools/sweep_normal.py --from-main --live` |

三个"只做某一块"的调试开关 (便于单独验证, 不用跑整条链):
`city.py --gifts-only` / `city.py --buildings-only` / `city.py --daily-task-only`。

## 日常一条龙 (`tools/daily.py`)

顺序完全由 `config/daily.yaml` 的 `steps` 决定 (可按 `kind` 增删 / 调序), 当前是:

| # | 步骤 | kind | 说明 |
|---|---|---|---|
| 1 | 主城任务 | `city` | inn + 宝箱 + 礼物(friend/公会/商店) + 拖动找建筑。**必须在主城做, 排最前** |
| 2 | 忒提斯英雄传 | `thetis` | 查 n/3 免费次数, 有则扫, 无则跳过 |
| 3 | 消耗焕发次数 | `radiant` | 要刷哪些副本在 `daily.yaml` 的 `targets` 里单独列 (10 次共用) |
| 4 | 额外扫荡副本 | `extra_sweep` | **可选, 默认关**。焕发用完后按指定次数再扫一遍副本 |
| 5 | 觉醒副本 | `awakening` | 查免费次数 (正向确认), 有则扫, 无则跳过 |
| 6 | 圆形角斗场 | `colosseum` | 交给 `tools/pvp.py` 子进程, 消耗所有挑战次数 (没票自动停) |
| 7 | 每日任务一键领取 | `daily_task` | **收尾, 必须放最后** —— 奖励取决于前面任务是否完成 |

几条**顺序约束**(挪动前务必看清):
- 第 1 步和第 7 步都必须**在主城**做, 所以代码把它们排在"点 play 进玩法页"之前处理。
- 第 7 步必须**最后**做: 别的任务没做完, 每日任务的奖励就领不全。
- `daily.py` 启动时会先看当前在哪个界面; 若不在主城 (上次跑完留在角斗场等), 会先一路返回主城再进玩法页。

## 配置文件一览

| 文件 | 管什么 | 被谁读 |
|---|---|---|
| `settings.yaml` | 设备(serial/rotate) / 截图源 / 循环 / 视觉(threshold/scales/method) / 动作 | 全部脚本 |
| `states.yaml` | 界面状态识别规则 (`state/template/threshold/roi`) | `game/recognizer.py`, `probe.py`, `diagnose.py` |
| `coords.yaml` | 固定点击坐标表 `[x, y, w, h]` | `core/coords.py` → 各任务脚本 |
| `daily.yaml` | 日常步骤编排 (`steps[].kind` + `targets`) | `daily.py` |
| `city.yaml` | 主城任务 (首页切换 / inn / 宝箱 / 礼物 / 建筑 / 每日领取) | `city.py`, `daily.py` |
| `pvp.yaml` | 角斗场的战斗判定 (HUD 模板清单 / 颜色策略) | `pvp.py`, `diagnose.py` |
| `sweep.yaml` | 消耗焕发次数: 每日 10 次上限 + 要刷的副本 | `sweep.py` |
| `sweep_normal.yaml` | 普通扫荡: 目标 + 各自次数 | `sweep_normal.py`, `daily.py` |

## 关于 `main.py` / `game/handlers/`

`main.py` + `game/engine.py` + `game/handlers/` 是**最早的状态机骨架**: 截图 → `Recognizer.detect`
→ 按 `State` 查 Handler → `handler.handle(ctx)`。目前各 handler 基本只做识别与日志演示
(仅 `MainCityHandler` 会点一次 `menu` 验证链路), 真实玩法编排都发生在 `tools/` 下的任务脚本里。

两者**共用同一套底层** (`core/*` + `game/recognizer` + `game/actions`), 只是任务脚本各自构建
`AdbDevice → Matcher → Actions` 后直接跑, 不经过 engine 的状态分发。想让 `main.py` 接管更多玩法时,
再往里加 Handler 即可。

## 添加新玩法

主流路径 (任务脚本):

1. **采素材**: `python tools/cropper.py --live` 存模板图; `--roi-mode` 记坐标。
2. **写配置**: 坐标进 `config/coords.yaml`, 行为/开关进该玩法自己的 yaml (如 `config/city.yaml`)。
3. **接流程**: 新任务在 `tools/` 下加脚本 (参考 `tools/city.py` 的结构), 或挂到 `daily.yaml` 的步骤里。

需要新界面状态时 (状态机路径):

1. `game/states.py` 定义新的 `State`。
2. `config/states.yaml` 添加识别规则 (`tools/cropper.py --save` 可自动生成)。
3. `game/handlers/` 创建处理器, 继承 `Handler` 基类。
4. `main.py` 中注册 `handlers[State.XXX] = MyHandler(...)`。

## 已避开的常见坑

| 坑 | 解决方案 |
|---|---|
| `adb exec-out screencap > x.png` 在 PowerShell 写坏 PNG | 用 `subprocess.run(capture_output=True)` 捕获原始 bytes |
| 系统 PATH 里的 adb 与模拟器自带 adb 版本冲突 | `env.py` 优先探测模拟器自带 adb |
| 模拟器分辨率与游戏方向不一致导致画面被旋转 | 实际帧尺寸自动探测, 模板按真实分辨率采集 (`rotate` 可配) |
| 截图分辨率超过屏幕, OpenCV 窗口无法操作 | `cropper.py` 自动缩放显示并还原坐标 |
| `input tap` 每次启进程, 延迟 300ms+ | `tap_seq` 合并多次点击到一次 shell 会话 |
| Windows 控制台闪窗 | `subprocess.CREATE_NO_WINDOW` |
| PowerShell ANSI 颜色无法显示 | `kernel32.SetConsoleMode` 启用 VT |
| **纯色块**(如按钮的黄色底)拿去做模板匹配会失效 | `TM_CCOEFF_NORMED` 对无纹理的均匀块退化(到处都匹配) → 改用 `Color.ratio_in_roi` 做**颜色占比**检测 |
| **同一个图标颜色不同**(如黄色版 / 白色版)会双双高分命中 | 该算法先减均值再归一化, 对亮度/对比度不敏感 → 必须额外加颜色校验才能区分 |
| **ROI 和模板一样大**, 匹配分数暴跌 | 差几像素模板就放不进检索区 → ROI 四周务必留余量 |
| 在主城上**多按返回键** → 弹出"退出游戏"确认框 | 返回逻辑一律"**先检测、已在首页就绝不按键**", 没回去才再按一次 |
| 模板里的**文字**对缩放/抗锯齿敏感, 匹配发飘 | 位置固定就用坐标; 必须用模板时把 ROI 框死在该区域, 并聚焦按钮本体、不带背景 |
| Windows 控制台默认 GBK, 日志里打 `✗ ★` 等字符会直接崩 | 日志/print 只用中文与 ASCII 符号 |

## 注意事项

- 游戏脚本可能违反游戏用户协议, **请用测试账号验证**, 账号风险自担
- 首次运行请保持干跑, 验证无误后再加 `--live`
- 修改 `config/states.yaml` 后, 可在 `tools/probe.py` 里按 `R` 热重载, 无需重启脚本
- 模拟器重启后 adb 序列号可能变化 (实测 5554 → 5556), `settings.yaml` 里 `serial: auto` 会自动探测

## 免责声明

- 本项目仅供**个人学习与技术研究**使用, 不得用于商业用途或任何盈利行为。
- `assets/templates/` 下的模板图取自游戏画面截图, **相关游戏素材的版权归原厂商所有**;
  本项目仅将其用于界面识别, 不主张任何权利, 也与原厂商无任何关联。
- 使用自动化脚本可能违反游戏的用户协议, **请务必使用测试账号**;
  因使用本项目导致的账号封禁、数据丢失或其它任何后果, 均由使用者自行承担。
- 本项目按「现状」提供, 不保证可用性、适用性或无误, 作者不承担任何直接或间接损失的责任。
