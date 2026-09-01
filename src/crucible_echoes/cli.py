from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from .catalog import Catalog
from .engine import FUN_MODES, GameEngine, GameError
from .save import load_game, save_game
from .simulation import run_batch, run_difficulty_sweep, strategy_from_name, write_report, write_sweep_report

DEFAULT_SAVE = Path(".saves/current.json")


def _safe_stdout(text: str, end: str = "") -> None:
    """Print report text even when a Windows console uses a narrow code page."""
    payload = text + end
    try:
        sys.stdout.write(payload)
    except UnicodeEncodeError:
        buffer = getattr(sys.stdout, "buffer", None)
        if buffer is not None:
            encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
            buffer.write(payload.encode(encoding, errors="replace"))
        else:
            sys.stdout.write(payload.encode("ascii", errors="replace").decode("ascii"))

COMMAND_HELP = """可用命令：
  new --seed N --difficulty 1..15 [--fun-mode MODE]   新开一局
  start                              进入交互模式（自动读取存档）
  status                             查看订单、金币、最近盘面和待选奖励
  spin                               旋转并结算
  choose N                           选择当前第N个候选
  skip                               跳过当前选择
  reroll                             消耗1个Roll Token重调候选
  remove N                           消耗1个删除Token移除库存第N个成分
  inventory                          查看成分、道具、精粹和Token
  use ITEM_ID                        使用主动道具
  toggle ITEM_ID                     开关可切换道具（如禁令）
  simulate --games N                 批量模拟并生成平衡报告（可选--strategy heuristic-v1/v2/v3/v3.1）
  help                               显示本帮助
  quit                               退出交互模式

娱乐模式：none（默认）、giant（40格/目标×1.75/成分与删除Token翻倍）、
rapid（每回合自动删1个成分/两次普通成分奖励/Roll Token翻倍）、
blind_box（成分身份随机化/第4单起主线目标×0.85/Roll转Delete或Essence）、
minimal（12格/按稀有度加值/永久成长翻倍/每完成2单+1删除Token）、
mutation（每5次spin后全池正常成分同时变异，1%升级稀有度）。
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crucible-echoes", description="坩埚余响：纯文字炼金构筑 roguelike")
    sub = parser.add_subparsers(dest="command")

    new = sub.add_parser("new", help="新开一局")
    new.add_argument("--seed", type=int, default=1)
    new.add_argument("--difficulty", type=int, default=1)
    new.add_argument("--fun-mode", choices=FUN_MODES, default="none")
    new.add_argument("--save", default=str(DEFAULT_SAVE))

    start = sub.add_parser("start", help="进入交互模式")
    start.add_argument("--seed", type=int, default=1)
    start.add_argument("--difficulty", type=int, default=1)
    start.add_argument("--fun-mode", choices=FUN_MODES, default="none")
    start.add_argument("--save", default=str(DEFAULT_SAVE))

    for name in ("status", "spin", "skip", "reroll", "inventory", "help"):
        child = sub.add_parser(name)
        child.add_argument("--save", default=str(DEFAULT_SAVE))
    choose = sub.add_parser("choose")
    choose.add_argument("number", type=int)
    choose.add_argument("--save", default=str(DEFAULT_SAVE))
    remove = sub.add_parser("remove")
    remove.add_argument("number", type=int)
    remove.add_argument("--save", default=str(DEFAULT_SAVE))
    use = sub.add_parser("use")
    use.add_argument("item_id")
    use.add_argument("--save", default=str(DEFAULT_SAVE))
    toggle = sub.add_parser("toggle")
    toggle.add_argument("item_id")
    toggle.add_argument("--save", default=str(DEFAULT_SAVE))

    simulate = sub.add_parser("simulate", help="批量模拟并生成平衡报告")
    simulate.add_argument("--games", type=int, default=1000, help="模拟局数，默认1000")
    simulate.add_argument("--seed", type=int, default=1, help="批量基础seed")
    simulate.add_argument("--difficulty", type=int, default=1)
    simulate.add_argument("--fun-mode", choices=FUN_MODES, default="none")
    simulate.add_argument("--strategy", choices=("heuristic-v1", "heuristic-v2", "heuristic-v3", "heuristic-v3.1"), default="heuristic-v1")
    simulate.add_argument("--max-actions", type=int, default=5000, help="单局动作上限")
    simulate.add_argument("--report", default="reports/balance_report.md", help="Markdown报告路径")
    simulate.add_argument("--json-report", default="reports/balance_report.json", help="JSON明细报告路径")
    simulate.add_argument("--summary-only", action="store_true", help="只保留汇总和内容统计，适合大规模扫描")

    sweep = sub.add_parser("simulate-sweep", help="批量模拟难度1-15")
    sweep.add_argument("--seed", type=int, default=1, help="固定base seed")
    sweep.add_argument("--games-low", type=int, default=1000, help="难度1-5每档局数")
    sweep.add_argument("--games-high", type=int, default=500, help="难度6-15每档局数")
    sweep.add_argument("--strategy", choices=("heuristic-v1", "heuristic-v2", "heuristic-v3", "heuristic-v3.1"), default="heuristic-v1")
    sweep.add_argument("--fun-mode", choices=FUN_MODES, default="none")
    sweep.add_argument("--max-actions", type=int, default=5000, help="单局动作上限")
    sweep.add_argument("--report", default="reports/balance_sweep.md", help="汇总Markdown报告")
    sweep.add_argument("--json-report", default="reports/balance_sweep.json", help="汇总JSON报告")
    sweep.add_argument("--detail-directory", default="reports/balance_sweep", help="各难度明细目录")
    sweep.add_argument("--summary-only", action="store_true", help="只保留汇总和内容统计，适合大规模扫描")

    # Keep the machine interface separate from the human-oriented commands.
    # Every ``agent`` invocation performs at most one action and emits one
    # [STATE] JSON line.
    agent = sub.add_parser("agent", help="执行一个无状态 AI agent 动作")
    agent_sub = agent.add_subparsers(dest="agent_action", required=True)
    agent_new = agent_sub.add_parser("new", help="创建新存档")
    agent_new.add_argument("--seed", type=int, default=1)
    agent_new.add_argument("--difficulty", type=int, default=1)
    agent_new.add_argument("--fun-mode", choices=FUN_MODES, default="none")
    agent_new.add_argument("--save", default=str(DEFAULT_SAVE))
    for name in ("status", "spin", "skip", "reroll", "inventory", "help"):
        child = agent_sub.add_parser(name)
        child.add_argument("--save", default=str(DEFAULT_SAVE))
    agent_choose = agent_sub.add_parser("choose")
    agent_choose.add_argument("number")
    agent_choose.add_argument("--save", default=str(DEFAULT_SAVE))
    agent_remove = agent_sub.add_parser("remove")
    agent_remove.add_argument("number")
    agent_remove.add_argument("--save", default=str(DEFAULT_SAVE))
    agent_use = agent_sub.add_parser("use")
    agent_use.add_argument("item_id")
    agent_use.add_argument("--save", default=str(DEFAULT_SAVE))
    agent_toggle = agent_sub.add_parser("toggle")
    agent_toggle.add_argument("item_id")
    agent_toggle.add_argument("--save", default=str(DEFAULT_SAVE))
    return parser


def load_engine(path: str | Path) -> GameEngine:
    source = Path(path)
    if not source.exists():
        raise GameError(f"找不到存档：{source}；请先运行 new")
    return GameEngine().bind(load_game(source))


def render(engine: GameEngine, *, inventory: bool = False) -> str:
    state = engine.s
    payload = engine.status_payload()
    lines: list[str] = []
    amount = payload["order_amount"]
    if payload.get("endless_mode"):
        lines.append(f"无限模式：第{payload['endless_order']}份无限订单 / 目标{payload['endless_target']}g / 剩余{payload['spins_left']}回合")
    if payload.get("peace_mode"):
        lines.append(f"和平模式：第{payload['peace_order'] + 1}份和平订单 / 目标0g / 剩余{payload['spins_left']}回合 / 目标存款{payload['peace_target']}g")
    lines.append(f"状态：{payload['status']}  金币：{payload['gold']}g  难度：{payload['difficulty']}")
    lines.append(f"娱乐模式：{payload.get('fun_mode', 'none')}")
    lines.append(f"订单：第{payload['order']}份 / {amount}g  剩余旋转：{payload['spins_left']}")
    lines.append(f"实验池：{payload['pool_size']}个成分  盘面容量：{payload['board_capacity']}格  seed：{payload['seed']}")
    lines.append(f"Token：Roll {state.tokens.get('roll',0)} / 删除 {state.tokens.get('remove',0)} / 精粹 {state.tokens.get('essence',0)}")
    if "ban" in state.items:
        mode = "开启" if payload.get("ingredient_generation_disabled") else "关闭"
        suffix = "（永久禁用已生效）" if payload.get("ingredient_generation_permanently_disabled") else ""
        lines.append(f"禁令：{mode}{suffix}（toggle ban 切换）")
    if state.last_board:
        board = "  ".join(
            f"{row['slot']}:{row['name']}{'' if row.get('present', True) else '[gone]'}({row['value']:+d}g)"
            for row in state.last_board
        )
        lines.append("最近盘面：" + board)
    if state.last_log:
        lines.append("最近记录：")
        lines.extend("  " + row for row in state.last_log)
    if state.pending:
        choice = state.pending[0]
        collection = (
            engine.catalog.ingredients if choice.kind == "ingredient"
            else engine.catalog.items if choice.kind == "item"
            else engine.catalog.essences if choice.kind == "essence"
            else {}
        )
        lines.append(f"待选 {choice.kind}（来源：{choice.source}）：")
        for index, def_id in enumerate(choice.offers, 1):
            if choice.kind in {"run_end", "bundle"}:
                row = (
                    engine._definition_view(choice.kind, def_id)
                    if choice.kind == "run_end"
                    else choice.details.get("options", {}).get(def_id, {})
                )
            else:
                row = collection[def_id]
            rarity = f"{row.get('rarity')}级 " if row.get("rarity") else ""
            lines.append(f"  {index}. {row['name']} [{def_id}] — {rarity}{row.get('description','')}")
        lines.append("  可用：choose N" + (" / skip" if choice.can_skip else ""))
    if inventory:
        lines.append("成分库存：")
        for index, inst in enumerate(state.ingredients, 1):
            row = engine.catalog.ingredients[inst.def_id]
            lines.append(f"  {index}. {row['name']} [{inst.def_id}] {row.get('rarity',0)}级 基础{row.get('base',0):+d} 永久{inst.permanent_bonus:+d} 年龄{inst.age}")
        lines.append("道具：")
        lines.extend(f"  - {engine.catalog.items[x]['name']} [{x}]：{engine.catalog.items[x]['description']}" for x in state.items)
        if not state.items: lines.append("  （无）")
        lines.append("精粹：")
        lines.extend(f"  - {engine.catalog.essences[x]['name']} [{x}]：{engine.catalog.essences[x]['description']}" for x in state.essences)
        if not state.essences: lines.append("  （无）")
    lines.append("[STATE] " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines)


def execute(engine: GameEngine, command: str, args: list[str]) -> str:
    if command == "status": return render(engine)
    if command == "inventory": return render(engine, inventory=True)
    if command == "help": return COMMAND_HELP
    if command == "spin": engine.spin()
    elif command == "choose": engine.choose(int(args[0]))
    elif command == "skip": engine.skip()
    elif command == "reroll": engine.reroll()
    elif command == "remove": engine.remove(int(args[0]))
    elif command == "use": engine.use_item(args[0])
    elif command == "toggle": engine.toggle_item(args[0])
    else: raise GameError(f"未知命令：{command}")
    return render(engine, inventory=command == "remove")


def apply_action(engine: GameEngine, command: str, args: list[str]) -> None:
    """Apply exactly one mutating action without producing presentation text."""
    if command == "spin":
        engine.spin()
    elif command == "choose":
        engine.choose(int(args[0]))
    elif command == "skip":
        engine.skip()
    elif command == "reroll":
        engine.reroll()
    elif command == "remove":
        engine.remove(int(args[0]))
    elif command == "use":
        engine.use_item(args[0])
    elif command == "toggle":
        engine.toggle_item(args[0])
    else:
        raise GameError(f"未知命令：{command}")


def print_agent_state(payload: dict[str, object]) -> None:
    """Emit one and only one machine-readable line for an agent action."""
    # ASCII escaping keeps the protocol valid through legacy Windows code pages;
    # JSON parsers restore the original Unicode strings automatically.
    print("[STATE] " + json.dumps(payload, ensure_ascii=True, separators=(",", ":")))


def run_agent(ns: argparse.Namespace) -> int:
    """Execute one agent action in one process, persisting the JSON save."""
    action = ns.agent_action
    save_path = Path(getattr(ns, "save", DEFAULT_SAVE))
    engine: GameEngine | None = None
    try:
        if action == "new":
            engine = GameEngine()
            engine.new_game(ns.seed, ns.difficulty, ns.fun_mode)
            save_game(engine.s, save_path)
            print_agent_state(engine.agent_payload(action))
            return 0

        if not save_path.exists():
            raise GameError(f"找不到存档：{save_path}；请先运行 agent new")
        engine = load_engine(save_path)

        if action == "help":
            payload = engine.agent_payload(action)
            payload["agent_help"] = {
                "command": "python game.py agent ACTION --save PATH",
            "actions": ["new", "status", "spin", "choose N", "skip", "reroll", "remove N", "inventory", "use ITEM_ID", "toggle ITEM_ID"],
                "fun_modes": list(FUN_MODES),
                "contract": "每次进程只执行一个动作；成功或失败都输出一行 [STATE] JSON。",
            }
            print_agent_state(payload)
            return 0

        if action not in {"status", "inventory"}:
            args: list[str] = []
            if action in {"choose", "remove"}:
                args = [str(ns.number)]
            elif action in {"use", "toggle"}:
                args = [ns.item_id]
            apply_action(engine, action, args)
            save_game(engine.s, save_path)
        print_agent_state(engine.agent_payload(action))
        return 0
    except (GameError, ValueError, IndexError, OSError) as exc:
        if engine is not None:
            print_agent_state(
                engine.agent_payload(
                    action,
                    ok=False,
                    error={"type": type(exc).__name__, "message": str(exc)},
                )
            )
        else:
            print_agent_state(
                {
                    "protocol": "crucible-echoes-agent/v1",
                    "ok": False,
                    "action": action,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                    "state": None,
                    "available_actions": ["new"],
                    "available_action_specs": [{"action": "new"}],
                }
            )
        return 2


def run_simulation_command(ns: argparse.Namespace) -> int:
    if not 1 <= ns.difficulty <= 15:
        raise GameError("难度必须在1到15之间")
    if ns.games < 1:
        raise GameError("模拟局数必须至少为1")
    if ns.max_actions < 1:
        raise GameError("单局动作上限必须至少为1")
    report = run_batch(
        games=ns.games,
        seed=ns.seed,
        difficulty=ns.difficulty,
        strategy=strategy_from_name(ns.strategy),
        max_actions=ns.max_actions,
        retain_details=not ns.summary_only,
        fun_mode=ns.fun_mode,
    )
    write_report(report, ns.report, ns.json_report)
    _safe_stdout(report.to_markdown())
    _safe_stdout(f"\nMarkdown报告：{ns.report}\nJSON明细：{ns.json_report}\n")
    return 0


def run_simulation_sweep_command(ns: argparse.Namespace) -> int:
    if ns.games_low < 1 or ns.games_high < 1:
        raise GameError("每个难度的模拟局数必须至少为1")
    if ns.max_actions < 1:
        raise GameError("单局动作上限必须至少为1")
    games_by_difficulty = {
        difficulty: ns.games_low if difficulty <= 5 else ns.games_high
        for difficulty in range(1, 16)
    }
    report = run_difficulty_sweep(
        games_by_difficulty=games_by_difficulty,
        seed=ns.seed,
        strategy=strategy_from_name(ns.strategy),
        max_actions=ns.max_actions,
        retain_details=not ns.summary_only,
        fun_mode=ns.fun_mode,
    )
    write_sweep_report(report, ns.report, ns.json_report, ns.detail_directory)
    _safe_stdout(report.to_markdown())
    _safe_stdout(f"\n汇总Markdown报告：{ns.report}\n汇总JSON报告：{ns.json_report}\n明细目录：{ns.detail_directory}\n")
    return 0


def interactive(save_path: Path, seed: int, difficulty: int, fun_mode: str = "none") -> int:
    if save_path.exists():
        engine = load_engine(save_path)
        print("已读取存档。")
    else:
        engine = GameEngine(); engine.new_game(seed, difficulty, fun_mode); save_game(engine.s, save_path)
        print("已创建新实验。")
    print(render(engine)); print(COMMAND_HELP)
    while True:
        try:
            raw = input("炼金> ").strip()
        except EOFError:
            print()
            break
        if not raw: continue
        parts = shlex.split(raw)
        if parts[0] in {"quit", "exit", "退出"}: break
        try:
            text = execute(engine, parts[0], parts[1:])
            save_game(engine.s, save_path)
            print(text)
        except (GameError, ValueError, IndexError) as exc:
            print(f"错误：{exc}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)
    command = ns.command or "start"
    save_path = Path(getattr(ns, "save", DEFAULT_SAVE))
    try:
        if command == "agent":
            return run_agent(ns)
        if command == "simulate":
            return run_simulation_command(ns)
        if command == "simulate-sweep":
            return run_simulation_sweep_command(ns)
        if command == "new":
            engine = GameEngine(); engine.new_game(ns.seed, ns.difficulty, ns.fun_mode); save_game(engine.s, save_path)
            print(render(engine)); return 0
        if command == "start": return interactive(save_path, getattr(ns, "seed", 1), getattr(ns, "difficulty", 1), getattr(ns, "fun_mode", "none"))
        if command == "help": print(COMMAND_HELP); return 0
        engine = load_engine(save_path)
        args: list[str] = []
        if command in {"choose", "remove"}: args = [str(ns.number)]
        elif command in {"use", "toggle"}: args = [ns.item_id]
        print(execute(engine, command, args))
        save_game(engine.s, save_path)
        return 0
    except GameError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
