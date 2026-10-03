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
- `state.stats.last_board_topology`: optional saved `all_adjacent` / `panorama`
  topology of the last settled board. On load, the core restores that board
  with live instance UIDs, preserving holes left by removed instances and
  excluding off-board pool entries from board-based predicates. Loading does
  not redraw RNG or emit gameplay events. Legacy saves without this metadata
  safely derive periodic-item topology; an expired one-turn topology cannot
  be perfectly recovered from legacy files that never saved it;
- `stats.spawn_counters`: persisted success counters such as the summon-magic
  guarantee counter (old saves receive an empty object automatically);
- `state.stats.round_events`: persisted counts for the current round, so
  round-scoped essence triggers continue across stateless agent actions;
- `state.stats.item_storage`: persisted balances such as the piggy-bank reserve
  (old saves receive an empty object automatically);
- `state.flags.ingredient_generation_disabled` and
  `state.flags.ingredient_generation_permanently_disabled`: effective and
  permanent component-generation switches; `ingredient_generation_bonus`
  reports the permanent value bonus applied to generator ingredients;
- `state.fun_mode` identifies the mutually exclusive entertainment mode
  (`none`, `giant`, `rapid`, `blind_box`, `minimal`, or `mutation`). Giant
  uses `ceil(×1.75)` order targets, blind-box uses `ceil(×0.85)` from mainline
  order 4, minimal awards one Delete Token per two successful orders, and
  mutation transforms the whole normal ingredient pool after every fifth spin;
- `available_actions`: executable command strings for the next step; toggleable
  items expose `toggle ITEM_ID`, and bundle rewards expose one `choose N`
  action for the all-or-nothing option;
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
python game.py agent new --seed 42 --difficulty 1 --fun-mode mutation --save .saves/agent-mutation.json
python game.py agent spin --save .saves/agent.json
python game.py agent choose 2 --save .saves/agent.json
python game.py agent status --save .saves/agent.json
```

Supported agent actions are `new` (with `--fun-mode none|giant|rapid|blind_box|minimal|mutation`), `status`, `spin`, `choose N`, `skip`,
`reroll`, `remove N`, `inventory`, `use ITEM_ID`, `toggle ITEM_ID`, and `help`. Mutating actions
load the save, execute one engine action, save the resulting state, and exit.
Read-only actions also emit the same state envelope.

Active items such as the sandpaper box and easter-egg box appear as
`use ITEM_ID` in both `available_actions` and `available_action_specs`. They are
never exchanged automatically.

Optional saved bookkeeping is also visible in the full state. Pending choice
`details.draw_constraints` preserves fixed rarity and slot minimums across
rerolls; `choice_uid` and `event_counts` scope reroll triggers to the same
choice, not the entire spin. `stats.next_choice_uid` allocates identities
without RNG. `stats.choice_round_progress` and `choice_round_streak` track
completed reward phases: multiple picks in one spin are not multiple rounds,
and a skipped reward breaks the streak. Essence baselines reset at acquisition
or a stabilized repeat use. Missing fields in legacy saves start with empty
progress/default counters; no schema version migration is required.

The three `flags.choice_minimum_*` counters retain explicit zero values
after consumption, matching legacy defaults installed on load. Rerolling
keeps the current group's candidate count and constraints; it neither
reapplies candidate-count bonuses nor spends next-new-choice flags/hooks.
An exhausted item pool may return fewer candidates, including an empty
skippable reward. Empty rewards expose `skip`, not `choose` or `reroll`;
no compensation or duplicate owned item is invented.
Fallback draws also respect slot minimums and exact fixed tiers. A constrained
pool can produce a smaller or empty skippable reward even when other tiers
still have stock. Internal membership redraw trials emit no choice events;
only the final published reroll candidates do. Ordinary draw order is unchanged.
Automatic item grants likewise omit exhausted rewards without aborting a
removal/periodic action; explicit tiers do not substitute another rarity.
Other effects of the action continue unchanged. No new save fields are needed.

`stats.round_event_values` and `stats.round_removed_values` retain the current
round's event amounts and removed ingredient `(rarity, base)` pairs across
single-action reloads. Both reset when the next valid spin starts. Missing
legacy fields default to `{}` / `[]`; global removal history is not guessed
to be the current round. An old save cannot recover context that was never
written, but loads normally and records future actions correctly.

`stats.observed_content` is optional per-save discovery bookkeeping with
`ingredients`, `items`, and `essences` ID lists. Published pending queues and
owned content count as observable; rejected internal draw trials do not.
It does not emit acquisition events or draw RNG. Legacy saves seed it from
available state/history, not guessed unseen definitions.

When an action is invalid, the process returns exit code `2` but still emits a
single `[STATE]` line with `ok: false`, an error object, and the unchanged
loaded state. A missing save can only report a minimal error envelope because
there is no game state to load; the `available_actions` field then contains
`new`.

The core rejects all mutating actions after `won`/`lost`. With a pending reward,
only its listed choice/skip/reroll actions are executable; `spin`, `remove`,
`use`, and `toggle` wait until the queue is resolved. Rejected actions do not
spend RNG, Tokens, items, or overwrite the save. Read-only status/help/inventory
remain available after a run ends.

`last_log` contains the complete latest public action's messages, including
its reward/listener messages; it does not replay the previous action or truncate
the chain to three lines. This is an action log, not a full causal animation
timeline. Income-based essence conditions require an observed `stats.last_income`;
a missing legacy field is unknown, not a measured 0g turn. A real 0g settlement
still satisfies the normal parity/multiple/maximum rules.
