# 桌面版架构

桌面版是现有 `GameEngine` 的新操作层，不包含第二套游戏规则。

```text
HTML/CSS/JS  →  pywebview API bridge  →  GameEngine  →  JSON save
                  ↑                         ↓
              complete view state      existing CLI/Agent payload
```

## 组件

- `desktop/app.py`：创建可调整大小的 pywebview 窗口，配置日志和存档路径。
- `desktop/bridge.py`：暴露 `new_game`、`continue_game`、`action`、`preview`、`save`、`catalog`、`get_state`。动作完成后自动写 JSON 存档。
- `desktop/view_model.py`：把核心 Agent payload 转换为 UI 所需的完整快照，补充卡片详情、邻接索引、图鉴和窗口布局信息。
- `frontend/index.html`、`frontend/css/style.css`、`frontend/js/app.js`：菜单、实验台、详情栏、待选择候选、日志、图鉴和设置。

## 状态与合法性

前端只渲染 `crucible-echoes-desktop/v1` 快照。Roll、Delete、选择、跳过、物品使用、禁令开关和结束回合全部发送回 Python；Python 核心负责合法性、数值、随机数和事件顺序。前端阻止重复提交中的动作，Python bridge 使用可重入锁顺序处理调用，避免同时写入同一存档。

桌面动作在独立的核心运行状态副本上结算，保留盘面实例引用和本轮拓扑；原只读Catalog共享，不重建抽取池。原子写入存档成功后才提交状态。权限不足/磁盘满等写入失败时，操作未生效，原状态、Token、RNG和正式存档保留，可安全重试。菜单中无当前对局可正常退出；有对局时“退出”按钮仅在保存成功后关闭窗口。此行为属于桌面操作事务，不改变核心规则或CLI/Agent接口。

新游戏、继续游戏和重要操作也会先完整生成候选界面数据，再提交或保存。缺失定义、界面转换异常等失败不会替换当前对局、切换存档路径或覆盖目标文件；可返回原局或菜单处理。此检查不等于完整存档schema校验，损坏存档不会被自动擦除或强行迁移。

共用保存接口为每次写入创建同目录的独立临时文件，写入/同步后原子替换正式存档；进程内只串行化短暂的替换阶段。Windows临时权限拒绝最多尝试5次、间隔10ms；持续失败仍明确报错，只清理该次自己创建的临时文件，不碰其他写入者或旧`.tmp`。JSON格式与路径不变，不使用游戏RNG。此措施防止临时文件互踩，但不提供跨进程状态合并；多窗口同时使用同一存档仍为最后成功保存者覆盖，应避免。

卡片会区分两种价值：首次转动前，以及未在本轮出现的池内成分，显示核心计算的「稳定价值」，包含基础、永久成长、全局修正和普通物品加成，但不包含随机、位置、邻接和 aura 效果；已转动的盘面显示「本轮结算」价值。稀有度与原始基础金币始终单独显示，避免把结算结果误认成基础数值。完整盘面尺寸由娱乐模式和工程图纸决定，不随池里当前有几张成分改变。

已结算盘面的邻接高亮复用核心保存/恢复的实际拓扑，包括全邻接和全景镜等本轮效果；即使相应的一回合状态已消退，也不会把本轮高亮错误还原为普通邻接。已经离场的目标不再高亮。

详情栏可以查看当前物品和未消耗精粹；可使用或开关的物品显示对应操作。Delete 既能选择本轮盘面上的成分，也能选择下方池内未出场的合法成分。

物品详情直接展示已保存的储蓄、周期事件进度及已有触发记录；精粹显示已触发次数，不把稳定器等有限保留机制误称为永久效果。未埋点的物品不会伪造零次触发。选择中的物品/精粹状态会随新快照更新，消耗后清除旧详情。

图鉴使用每局存档的可选 `stats.observed_content` 保留已见成分、物品和精粹ID，包括公开待选队列中的未选候选；使用后消失的物品不会从图鉴遗忘。内部未发布的抽取试案不算已见，未见定义不自动解锁；展示文本仍读取当前Catalog，不缓存旧文案。这是每份存档的历史，不是跨所有对局的全局解锁。旧存档可从当前库存/队列和已有成分/精粹历史继续积累，无法恢复原本没有记录的历史未选候选或已消耗物品。

选中成分离开本轮盘面但仍在池中时，详情从最新池内状态更新，不缓存过时成长/价值；彻底离场且不再有盘面记录时清空选择。盘面卡片支持Tab聚焦、Enter/空格选择，池内卡片也有清晰的键盘焦点与选中高亮。

底部操作栏常驻视口，避免池较大、奖励展开或低窗口高度时“结束回合”落到屏幕外。原生smoke会实际检查新局、待奖励和选择结束后的按钮边界；DOM stub不替代此布局检查。

首次机制提示依据当前可见定义和状态识别精粹、永久成长、倒计时离场、邻接和Token，逐个确认，不弹模态框。确认记录保存在本机浏览器存储，可在设置中关闭；关闭不把尚未展示的机制当成已经确认。浏览器存储不可用时仍可正常玩，在本窗口内记住设置/确认状态。邻接提示明确位置高亮不等于一定受到效果。

成功操作另返回临时 `action_changes`，按UID比较操作前后成分、身份、永久加值、金币、Token、物品及精粹消耗。前端显示可折叠汇总并快速高亮对应卡片，关闭动画仍保留文字；尊重系统减少动态效果设置。此汇总不是连锁时间线，不猜来源或删除原因，未包含同动作内出生后又消失的中间实例。它不写入存档、不加入CLI/Agent协议；只读状态及失败操作不重放旧汇总。

`preview` 使用深拷贝的核心状态执行一次安全模拟，不会写回 RNG 或存档。它展示一个模拟结果，不承诺所有随机结果；执行新操作后旧预览会隐藏。

## 存档与日志

新局种子输入按十进制整数文本传给Python，不先转成JavaScript浮点数，保留大于2^53的种子精度。桌面快照额外提供 `seed_text` 精确文本，原 `seed` 字段仍保留；直接调用bridge时大整数也应使用文本。小数和布尔值输入会被拒绝，原局及存档不变。seed9001原生smoke结束后还通过实际新局表单验证uint64最大种子的精确传输。

Windows 桌面版默认使用可写的用户目录：

```text
%LOCALAPPDATA%/CrucibleEchoes/saves/current.json
%LOCALAPPDATA%/CrucibleEchoes/saves/crucible-echoes.log
```

默认路径不依赖启动时的工作目录。若未设置 `LOCALAPPDATA`，使用用户目录下的 `.local/share/CrucibleEchoes/saves/`。

首次使用新默认路径时，如果新存档不存在，会检查旧工作目录、源码目录以及打包后 EXE 目录中的 `.saves/current.json`。找到合法旧存档后复制到新目录；原文件保留，新目录中已有存档不会被覆盖。旧存档损坏或迁移失败会显示错误，仍允许从菜单新建游戏。

可以用 `--save <路径>` 明确指定存档位置；相对路径在启动时解析为绝对路径，日志放在该存档的同级目录。这个参数不会触发默认存档迁移。新建或继续失败时，已有局面不会被替换。

自动读取、继续、新建、回合操作及手动保存均在完整界面状态构建成功后才提交或写入；定义缺失或界面无法显示的候选存档不会替换当前局。错误视图也无法显示时返回可操作菜单，保留原运行状态/文件，可重试或另建游戏。此保护不是完整存档schema验证，不会删除损坏文件或擅自改写未知成分。

## 开发与打包

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m desktop.app
.\.venv\Scripts\python.exe -m desktop.app --smoke-test
.\.venv\Scripts\python.exe run_tests.py
powershell -ExecutionPolicy Bypass -File build/build_windows.ps1
```

PyInstaller 使用 `build/CrucibleEchoes.spec` 生成 onedir 目录，前端资源随完整目录一起分发；PyInstaller 6 默认通常放在 `dist/CrucibleEchoes/_internal/frontend/`，程序通过 bundle 资源根目录定位，无需依赖启动目录。EXE 的文件/产品版本取自 `pyproject.toml`。设置环境变量 `CRUCIBLE_ICON` 为现有 `.ico` 路径可指定图标；不提供图标时仍能正常构建。

发布时保留完整目录，不只复制EXE。包内包含项目MIT许可、README和环境中可取得的依赖许可文本；第三方运行环境（如WebView2）仍按各自安装条件使用。本项目没有对Windows程序做代码签名，首次运行可能出现系统安全提示。

打包脚本默认使用项目 `.venv` 中的 Python，未找到时尝试系统 `python`。可以显式传入其他已经安装依赖的解释器，路径必须为绝对路径：

```powershell
powershell -ExecutionPolicy Bypass -File build/build_windows.ps1 -PythonPath "C:\path\to\python.exe"
```

脚本会检查 Python、依赖安装及 PyInstaller 的退出码；失败时停止，不把旧构建误打包为新产物。`-Clean` 会先验证精确的 `build/CrucibleEchoes`、`dist/CrucibleEchoes` 子目录再清理，不清理仓库根目录。

## 验证方式

`python -m desktop.app --smoke-test` 会打开临时测试窗口，使用临时存档和 seed `9001`、`none` 模式依次验证新建、结束回合和选牌，同时检查成分卡渲染、选中/邻居高亮、奖励候选以及操作按钮的可用状态。成功后关闭窗口，并在运行目录写入 `desktop-smoke-result.json`，包含本次运行 ID、实际 renderer、seed 与模式；这个文件不属于游戏存档。新测试开始时会先标记结果为 pending，避免旧成功文件误导；30秒没有收到回调时关闭测试窗口并报告失败。

`tests/test_desktop.py` 覆盖核心与桌面动作一致性、RNG 不变、预览隔离、存档迁移、并发保存、稳定价值、各盘面尺寸及和平模式进度。前端回归使用 Node 执行实际 `app.js` 配合轻量 DOM stub；它能够检查菜单保存竞态、重复点击和高亮等逻辑，但不能替代真实 WebView 窗口验证。系统未提供 `node` 时，可以指定：

```powershell
$env:CRUCIBLE_NODE_EXECUTABLE = "C:\path\to\node.exe"
python -m unittest tests.test_desktop -v
```

测试结果与 EXE smoke 结果必须分别报告；旧 EXE 通过启动检查，不代表当前源码的 GUI 修改已经打包生效。
# 桌面输入边界

种子、难度、选择序号和删除序号只接受整数或带可选正负号的十进制整数文本。
不将小数截断为序号，也不把布尔值当成1/0；非法输入返回错误并保留当前游戏与存档。
大整数种子通过文本传递，不经JavaScript浮点数转化。有效范围和操作合法性仍由原游戏核心校验。
