# Agent interface

The `agent` command is a stateless, one-action-per-process interface for an
LLM or other automation. The human-oriented `start` command is unchanged.

Every invocation prints exactly one line with this shape:

```text
[STATE] {"protocol":"crucible-echoes-agent/v1", ...}
```

The JSON envelope contains:

- `state`: the complete persisted `GameState`, including the RNG state used to
  resume the deterministic stream;
- `ingredients`, `items_detail`, `essences_detail`: owned definitions and
  instance metadata;
- `pending_choices`: the complete reward queue, including offer definitions;
- tagged reward queues expose `tag_filter`, so an agent can see that roster
  choices are restricted before selecting or rerolling;
- `last_board` and `last_log`: the most recent observable result. Each
  `last_board` row includes `present`; a `false` row was on the sampled board
  but was removed during that spin and no longer participates in board-based
  triggers;
- `stats.spawn_counters`: persisted success counters such as the summon-magic
  guarantee counter (old saves receive an empty object automatically);
- `state.stats.round_events`: persisted counts for the current round, so
  round-scoped essence triggers continue across stateless agent actions;
- `state.stats.item_storage`: persisted balances such as the piggy-bank reserve
  (old saves receive an empty object automatically);
- `available_actions`: executable command strings for the next step;
- `available_action_specs`: the same actions in structured form;
- `endless_mode`, `endless_order`, `endless_target`, `peace_mode`,
  `peace_order`, and `order_detail`: the current post-mainline mode and
  remaining rounds. After the final mainline reward, a `run_end` pending choice
  exposes `choose 1` (end), `choose 2` (enter infinite mode), and `choose 3`
  (enter peace mode);
- `ok`, `action`, and `error`: the result of the just-completed operation.

## One-step usage

Use one persistent save path for the whole run:

```text
python game.py agent new --seed 42 --difficulty 1 --save .saves/agent.json
python game.py agent spin --save .saves/agent.json
python game.py agent choose 2 --save .saves/agent.json
python game.py agent status --save .saves/agent.json
```

Supported agent actions are `new`, `status`, `spin`, `choose N`, `skip`,
`reroll`, `remove N`, `inventory`, `use ITEM_ID`, and `help`. Mutating actions
load the save, execute one engine action, save the resulting state, and exit.
Read-only actions also emit the same state envelope.

Active items such as the sandpaper box and easter-egg box appear as
`use ITEM_ID` in both `available_actions` and `available_action_specs`. They are
never exchanged automatically.

When an action is invalid, the process returns exit code `2` but still emits a
single `[STATE]` line with `ok: false`, an error object, and the unchanged
loaded state. A missing save can only report a minimal error envelope because
there is no game state to load; the `available_actions` field then contains
`new`.
