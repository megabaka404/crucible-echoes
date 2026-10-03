# 坩埚余响 · Crucible Echoes

一个可复现、可存档、数据驱动的炼金构筑 roguelike，提供 Windows 桌面版、文字 CLI 与 LLM Agent 接口。

你将在 4×5 实验台上构筑成分池，利用邻接、生成、移除和永久成长效果，在有限回合内完成逐渐提高的订单。三个操作界面共用同一游戏核心。

> 想让 AI 自己玩：把项目交给能运行终端的 AI，并告诉它：“请阅读 README，使用 `agent` 接口开一局，一直玩到胜利或失败。”

项目不使用第三方游戏的美术、文本、代码或品牌素材，采用 [MIT License](LICENSE) 开源。

## 快速开始

CLI、Agent 与模拟需要 Python 3.10+，无第三方依赖；桌面版另需安装下面的可选依赖。

```powershell
py -3 game.py new --seed 42 --difficulty 1
py -3 game.py start
```

新局可用 `--fun-mode` 选择一个与难度独立且互斥的娱乐模式：

```powershell
py -3 game.py new --seed 42 --difficulty 10 --fun-mode giant
py -3 game.py new --seed 42 --difficulty 10 --fun-mode rapid
py -3 game.py new --seed 42 --difficulty 10 --fun-mode blind_box
py -3 game.py new --seed 42 --difficulty 10 --fun-mode minimal
py -3 game.py new --seed 42 --difficulty 10 --fun-mode mutation
```

省略时为 `none`。每局最多选择一种娱乐模式，且与 D1-D15 难度独立：

| 模式 | 简要说明 |
|---|---|
| `none` | 标准规则。 |
| `giant` | 5×8（40格）实验台；成分复制、订单目标按 `ceil(×1.75)`、Delete Token 获取量翻倍。 |
| `rapid` | 每回合自动移除一个成分；获得两次普通成分选择；Roll Token 获取量翻倍。 |
| `blind_box` | 每次加入成分时随机化身份；第4份主线订单起目标按 `ceil(×0.85)`；Roll Token随机转为 Delete 或 Essence。 |
| `minimal` | 3×4（12格）实验台；成分按稀有度额外加值；永久成长翻倍；每完成2份成功订单获得1个 Delete Token。 |
| `mutation` | 每5次 spin 后，全池正常成分同时变异为同级其他成分；每个实例独立有1%概率升级稀有度，并保留永久成长。 |

娱乐模式不会改变标准 `none` 规则。

## Windows 桌面版

桌面版使用同一个 Python 游戏核心，`pywebview` 只负责窗口与 HTML/CSS/JavaScript 显示层。前端不自行计算规则：每次点击都由核心校验并返回一份完整 view state，因此 CLI、Agent 和桌面版会共享 RNG、存档和规则。

安装并运行开发版：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m desktop.app --save .saves/current.json
```

可以用真实窗口做一次自动 bridge smoke（完成新局、结束回合、选择候选后自动退出）：

```powershell
.\.venv\Scripts\python.exe -m desktop.app --smoke-test
```

也可以只使用项目声明的可选依赖：`py -3 -m pip install -e ".[desktop]"`。

窗口中提供卡片与邻接高亮、池内删除、候选选择、预计结算、操作变化汇总、图鉴和可关闭的首次机制提示；底部操作栏常驻。Windows 桌面版默认将存档与日志保存在 `%LOCALAPPDATA%/CrucibleEchoes/saves/`，避免 EXE 启动目录不可写；首次启动会复制可找到的旧 `.saves/current.json`，保留原文件。显式 `--save` 路径、CLI 和 Agent 存档不变。

构建不依赖 Python 的 Windows onedir 包（需要 Windows 与 PyInstaller）：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File build/build_windows.ps1
```

构建后的 EXE 也支持同样的 `--smoke-test` 参数。

产物为 `dist/CrucibleEchoes/CrucibleEchoes.exe` 及同目录资源，同时会生成可分发的 `dist/CrucibleEchoes-windows.zip`。目前项目保留 `--debug` 参数用于排查前端桥接问题；崩溃信息写入存档旁的 `crucible-echoes.log`。

CLI / Agent 默认存档为 `.saves/current.json`，桌面版默认使用上述用户数据目录；均可用 `--save path.json` 指定其他路径。

常用人类命令：

```text
status                 查看状态
spin                   结算一回合
choose N               选择第 N 个候选
skip                   跳过选择
reroll                 使用 Roll Token
remove N               使用 Delete Token 移除第 N 格
inventory              查看物品、装备和精粹
use ITEM_ID            使用可主动使用的物品
toggle ITEM_ID         开关可切换道具（如禁令）
help                   查看帮助
```

## AI Agent 接口

Agent 每次进程只执行一个动作，通过 JSON 存档持久化状态，并在操作后输出一行完整的 `[STATE]` JSON。下一步只应从 `available_actions` 中选择。

```powershell
py -3 game.py agent new --seed 42 --difficulty 1 --save .saves/agent.json
py -3 game.py agent new --seed 42 --difficulty 1 --fun-mode minimal --save .saves/agent-minimal.json
py -3 game.py agent spin --save .saves/agent.json
py -3 game.py agent status --save .saves/agent.json
```

支持的动作包括：`new`、`status`、`spin`、`choose N`、`skip`、`reroll`、`remove N`、`inventory`、`use ITEM_ID`、`toggle ITEM_ID` 和 `help`。`agent new` 的 `--fun-mode` 接受 `none`、`giant`、`rapid`、`blind_box`、`minimal`、`mutation`；状态中的 `fun_mode` 会持续保存。持有「禁令」时用 `toggle ban` 开关成分自身生成。

协议细节见 [`docs/AGENT_INTERFACE.md`](docs/AGENT_INTERFACE.md)。



## 内容规模

- **成分：156 个**：1 级 50 个、2 级 57 个、3 级 38 个、4 级 10 个，另有特殊成分“废渣”。成分会放入实验台并参与邻接、生成、移除和价值结算。
- **物品：123 个**：1 级 50 个、2 级 36 个、3 级 24 个、4 级 13 个。物品提供持续收益、周期触发、构筑联动或主动操作效果；例如“禁令”可用 `toggle ban` 切换成分自身生成，“颜料盒”提供整组接受/放弃选择。
- **精粹：112 个**：通过 Essence Token 激活，通常提供一次性的强化或补救效果；调色板精粹和禁令精粹会建立对应的永久全局状态。
- **难度：15 级**：D1-D6保持原有规则；D7-D11每33回合加入废渣，D11最终订单+75g；D12让废渣价值恒为0g、多留2回合且周期改为27回合；D13第12单+23g并将废渣周期改为20回合；D14最终订单再+75g；D15精粹Token奖励降为1个，并从第4单起每次成功结算后最多扣5g。D10及以上仍需完成额外的最终订单。

基础实验台为 4×5 共 20 格，工程图纸可永久扩建 1 格。完整规则和具体效果见 [`docs/SPEC.md`](docs/SPEC.md)。

## 特别注意
不要拿过多成分！也不要填不满20个就开始删牌！注意生成类的选取。最好控制在20-30个是比较优秀的策略。

## 项目结构

```text
game.py                         CLI 入口
src/crucible_echoes/cli.py     人类 CLI、Agent 接口和模拟命令
src/crucible_echoes/engine.py  游戏状态、结算和事件系统
src/crucible_echoes/model.py   JSON 状态模型与序列化
src/crucible_echoes/rng.py     可保存、可复现的随机数流
src/crucible_echoes/simulation.py  批量模拟、策略和报告
src/crucible_echoes/data/      成分、物品、精粹和规则数据
desktop/                        pywebview 应用、桥接和桌面 view model
frontend/                      HTML/CSS/JavaScript 桌面界面
build/                         PyInstaller spec 与 Windows 构建脚本
tests/                          自动测试
docs/SPEC.md                    完整规则规格
docs/AGENT_INTERFACE.md         Agent 协议
```

游戏内容采用数据驱动设计，新增内容通常只需扩展 `src/crucible_echoes/data/` 并补充测试。

## 测试

```powershell
py -3 run_tests.py
```

测试覆盖 RNG 可复现、稀有度、邻接、生成/移除、永久成长、订单、Token、精粹、难度、Agent 状态接口、桌面桥接和批量模拟。

## 平衡分析（开发用）

```powershell
py -3 game.py simulate --games 300 --seed 20260923 --difficulty 7 --strategy heuristic-v2-content --summary-only
```

`heuristic-v2-content` 是新增的内容识别实验策略；旧 `heuristic-v2` 等策略仍可用于对照，默认策略没有改变。模拟胜率不能直接代表玩家胜率或卡牌真实强度。验证过程、改动边界与测试结果见 [优化记录](docs/BALANCE_OPTIMIZATION_20260921.md)。

可选后继 `heuristic-v2-content-v2` 会结合真实联动和实例永久成长判断抓取/删除，修正部分魔药收益估值；旧策略继续保留。分块对照支持中断续跑：

```powershell
$env:PYTHONPATH = "src"
py -3 -m crucible_echoes.strategy_benchmark --games 500 --seed 20261002 --difficulties 7 10 15 --output reports/policy_pair.json
# 同一命令增加 --resume，复用已完成的分块。
```

工具保存源码/卡表快照，拒绝混合不同版本的结果。报告包含配对胜负、精确检验、池大小与每单条件死亡率；样本不足不判定优劣。成分/装备尚未完整采集触发次数，报告明确标注“未统计”，另有真实移除次数供自毁/消耗内容复核。

`heuristic-v2-content-v3` 是独立的操作覆盖实验：会按构筑决定使用材料包、开关禁令、接受整组奖励，以及在到期订单前兑现可删除成分的储蓄。默认和旧策略不变；决策不消耗游戏RNG。可用同seed对照：

另有可选 `heuristic-v2-content-v4`，补充每回合Token、事件奖励与订单储蓄估值。它依据历史触发率，可能不适用于刚改变体系的构筑；仍是实验策略，不替换默认，也不代表卡牌的真实强弱。

报告另外核对净池变化：初始池 + 动作边界新增 − 移除 = 最终池；同实例身份转换不算扩池。动作内生成又消失不计入此口径，复制桶并非完整UID级来源追踪，旧报告缺字段表示未统计。

```powershell
py -3 -m crucible_echoes.strategy_benchmark --games 100 --seed 20261004 --difficulties 7 10 15 --baseline heuristic-v2-content-v2 --candidate heuristic-v2-content-v3 --output reports/action_pair.json
```

当前修复和对照边界见 [持续优化记录](docs/OPTIMIZATION_20261003.md)。

诊断某种主动操作是否合理，可做策略操作消融（按效果字段，不修改卡牌能力）：

```powershell
py -3 -m tools.active_policy_ablation --field order_book --games 100 --seed 20261007 --difficulties 7 10 15 --output reports/order_book_ablation.json
```

实验工具拒绝覆盖旧报告；结果仍需检查样本量及随机路径分叉，不能当作卡牌改值依据。

## 贡献

欢迎提交新的成分、物品、精粹、测试和模拟分析。请尽量保持数据驱动、可复现，并为新机制补充回归测试。

## 致谢

本项目在 OpenAI 的 ChatGPT 与 OpenAI Codex 的协助下开发完成。

## 无限模式

结算最终主线订单后，你可以选择：

end_run：结束本局，状态变为 won
enter_endless：进入无限模式，继续挑战从 1000g 开始的 10回合订单
enter_peace：进入和平模式，继续进行 7回合 / 0g 的订单，直到存款达到 1,000,000g后胜利

无限模式中，每个新订单的目标金额按照：

ceil(上一份订单目标 × 1.5)

计算，也就是向上取整。

关于状态字段、存档兼容性以及 Agent 命令的详细说明，请参阅 [`docs/ENDLESS_MODE.md`](docs/ENDLESS_MODE.md) 

