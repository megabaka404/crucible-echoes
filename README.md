# 坩埚余响 · Crucible Echoes

一个可复现、可存档、数据驱动的纯文字炼金构筑 roguelike。

你将在 4×5 实验台上扩充成分池，利用邻接、生成、移除和永久成长效果，在有限回合内完成逐渐提高的订单。游戏同时支持人类 CLI 和面向 LLM 的单步 Agent 接口。

> 想让 AI 自己玩：把项目交给能运行终端的 AI，并告诉它：“请阅读 README，使用 `agent` 接口开一局，一直玩到胜利或失败。”

项目不使用第三方游戏的美术、文本、代码或品牌素材，采用 [MIT License](LICENSE) 开源。

## 快速开始

需要 Python 3.10+，无第三方依赖。

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

默认存档为 `.saves/current.json`，可用 `--save path.json` 指定其他路径。

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

- **成分：146 个**：1 级 45 个、2 级 56 个、3 级 34 个、4 级 10 个，另有特殊成分“废渣”。成分会放入实验台并参与邻接、生成、移除和价值结算。
- **物品：119 个**：1 级 50 个、2 级 33 个、3 级 24 个、4 级 12 个。物品提供持续收益、周期触发、构筑联动或主动操作效果；例如“禁令”可用 `toggle ban` 切换成分自身生成，“颜料盒”提供整组接受/放弃选择。
- **精粹：108 个**：通过 Essence Token 激活，通常提供一次性的强化或补救效果；调色板精粹和禁令精粹会建立对应的永久全局状态。
- **难度：15 级**：D1-D10 保持原有规则；D11 最终订单+75g，D12 让废渣价值恒为0g且多留2回合，D13 第12单+23g并使全局稀有度权重×0.95，D14 最终订单再+75g，D15 从第4单起每次成功结算后最多扣7g。D10及以上仍需完成额外的最终订单。

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
tests/                          自动测试
docs/SPEC.md                    完整规则规格
docs/AGENT_INTERFACE.md         Agent 协议
```

游戏内容采用数据驱动设计，新增内容通常只需扩展 `src/crucible_echoes/data/` 并补充测试。

## 测试

```powershell
py -3 run_tests.py
```

测试覆盖 RNG 可复现、稀有度、邻接、生成/移除、永久成长、订单、Token、精粹、难度、Agent 状态接口和批量模拟。

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

