const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync('frontend/js/app.js', 'utf8');
const storage = new Map();
const flush = () => new Promise(resolve => setImmediate(resolve));

function browser(state, blocked = false) {
  const nodes = [], ids = new Map(), hooks = {};
  class El {
    constructor() {
      this.children = []; this.dataset = {}; this.classes = new Set(); this.value = '1';
      this.style = { setProperty() {} }; nodes.push(this);
      this.classList = { add: x => this.classes.add(x), remove: x => this.classes.delete(x),
        toggle: (x, v) => (v === undefined ? !this.classes.has(x) : v) ? this.classes.add(x) : this.classes.delete(x) };
    }
    set className(value) { this.classes = new Set(value.split(/\s+/)); }
    set innerHTML(value) { this.html = value; this.children = []; }
    appendChild(value) { this.children.push(value); }
    addEventListener(type, callback) { (this.listeners ||= {})[type] = callback; }
    setAttribute(key, value) { this[key] = value; }
    scrollIntoView() {}
  }
  const match = (el, selector) => selector.split('.').filter(Boolean).every(name => el.classes.has(name));
  const document = { body: new El(), getElementById(id) { if (!ids.has(id)) ids.set(id, new El()); return ids.get(id); },
    createElement() { return new El(); }, querySelectorAll(selector) { return selector.startsWith('.') ? nodes.filter(el => selector.split(',').some(s => match(el, s.trim()))) : []; }, querySelector() { return null; },
    addEventListener(type, callback) { hooks[type] = callback; } };
  const localStorage = { getItem(key) { if (blocked) throw new Error('storage blocked'); return storage.get(key) ?? null; },
    setItem(key, value) { if (blocked) throw new Error('storage blocked'); storage.set(key, value); } };
  let apiCalls = 0; const newGameInputs = [];
  const window = { location: { search: '' }, addEventListener(type, callback) { hooks[type] = callback; },
    pywebview: { api: { async get_state() { return state; }, async difficulty_info() { return { ok: true, rules: [] }; },
      async action() { apiCalls++; return state; }, async new_game(...args) { newGameInputs.push(args); return state; } } } };
  vm.runInNewContext(source, { document, window, localStorage, setTimeout, clearTimeout, URLSearchParams, console });
  const el = id => document.getElementById(id);
  return { el, state, async render() { hooks.pywebviewready(); await flush(); }, apiCalls: () => apiCalls, newGameInputs };
}

const base = () => ({ ok: true, protocol: 'crucible-echoes-desktop/v1', screen: 'game', status: 'playing',
  fun_mode: 'none', board: [], ingredients: [], items: [], essences: [], tokens: {}, last_log: [],
  available_actions: ['spin'], available_action_specs: [], pending_choices: [] });

(async () => {
  const state = base(), b = browser(state);
  await b.render(); assert(b.el('mechanism-hint').classes.has('hidden'));
  state.essences = [{ id: 'synthetic_essence', name: '<unsafe>' }];
  const before = JSON.stringify(state);
  await b.render();
  assert.strictEqual(b.el('mechanism-hint').dataset.hintKind, 'essence');
  assert(b.el('mechanism-hint-text').textContent.includes('一次性'));
  assert.strictEqual(JSON.stringify(state), before);
  assert.strictEqual(b.apiCalls(), 0); // Hints are not game operations.
  b.el('dismiss-hint-btn').onclick();
  await b.render(); assert(b.el('mechanism-hint').classes.has('hidden'));
  const reloaded = browser(state); await reloaded.render();
  assert(reloaded.el('mechanism-hint').classes.has('hidden')); // Cross-window persistence.

  state.ingredients = [{ uid: 1, id: 'synthetic', permanent_bonus: 3,
    definition: { name: '<unsafe>', remove_after: 2, aura: { tag: 'anything' } } }];
  b.el('hints-setting').onchange({ target: { checked: false } });
  await b.render(); assert(b.el('mechanism-hint').classes.has('hidden'));
  assert.strictEqual(storage.get('ce_hint_v1_permanent'), undefined); // Disabled is not dismissed.
  b.el('hints-setting').onchange({ target: { checked: true } });
  assert.strictEqual(b.el('mechanism-hint').dataset.hintKind, 'permanent');
  b.el('dismiss-hint-btn').onclick(); assert.strictEqual(b.el('mechanism-hint').dataset.hintKind, 'countdown');
  b.el('dismiss-hint-btn').onclick(); assert.strictEqual(b.el('mechanism-hint').dataset.hintKind, 'adjacency');
  assert(b.el('mechanism-hint-text').textContent.includes('还要看标签'));
  b.el('dismiss-hint-btn').onclick(); assert(b.el('mechanism-hint').classes.has('hidden'));

  state.action_changes = { scope: 'action_boundary', gold: { before: 10, after: 8, delta: -2 },
    tokens: { roll: { before: 2, after: 1, delta: -1 } },
    ingredients: [{ kind: 'permanent', uid: 1, name: '<unsafe>', before: 0, after: 3, delta: 3 }],
    consumed_essences: [{ name: '<essence>', count: 1 }], items: [{ kind: 'removed', name: '<item>', count: 1 }] };
  const beforeChanges = JSON.stringify(state);
  await b.render();
  assert(!b.el('action-changes').classes.has('hidden'));
  assert(b.el('action-changes-content').html.includes('&lt;unsafe&gt;'));
  assert(b.el('action-changes-content').html.includes('包含订单支付'));
  assert(b.el('action-changes-content').html.includes('不再持有物品'));
  assert(b.el('pool-list').children[0].classes.has('effect-positive'));
  assert.strictEqual(JSON.stringify(state), beforeChanges);
  b.el('animations-setting').onchange({ target: { checked: false } });
  await b.render(); assert(!b.el('pool-list').children[0].classes.has('effect-positive'));
  assert(!b.el('action-changes').classes.has('hidden')); // Text stays available without animation.
  state.ok = false;
  await b.render(); assert(b.el('action-changes').classes.has('hidden'));
  state.ok = true; delete state.action_changes;
  await b.render(); assert(b.el('action-changes').classes.has('hidden')); // No stale replay on status.
  state.tokens = { remove: 1, essence: 1 };
  await b.render(); assert.strictEqual(b.el('mechanism-hint').dataset.hintKind, 'token_ops');
  b.el('dismiss-hint-btn').onclick(); assert.strictEqual(b.el('mechanism-hint').dataset.hintKind, 'essence_token');
  b.el('dismiss-hint-btn').onclick(); assert(b.el('mechanism-hint').classes.has('hidden'));

  const unavailable = base(); unavailable.essences = [{ id: 'anything' }];
  const c = browser(unavailable, true); await c.render();
  assert(!c.el('mechanism-hint').classes.has('hidden'));
  c.el('dismiss-hint-btn').onclick(); await c.render(); assert(c.el('mechanism-hint').classes.has('hidden'));
  unavailable.tokens = { roll: 1 };
  c.el('hints-setting').onchange({ target: { checked: false } }); await c.render();
  assert(c.el('mechanism-hint').classes.has('hidden'));
  c.el('hints-setting').onchange({ target: { checked: true } });
  assert.strictEqual(c.el('mechanism-hint').dataset.hintKind, 'token_ops');
  assert.strictEqual(c.apiCalls(), 0);

  const inventory = base(); inventory.items = [{id:'synthetic_store', name:'<store>', rarity:1, order_savings:{deposit_on_complete:5}, event_bonus_every:{order_completed:{every:3}}}];
  inventory.essences = [{id:'synthetic_essence', name:'<essence>'}];
  inventory.stats = {item_storage:{synthetic_store:15}, item_trigger_counts:{synthetic_store:2},
    item_event_counts:{'synthetic_store:order_completed':4}, essence_hits:{synthetic_essence:1}};
  const i = browser(inventory); await i.render();
  const beforeInventory = JSON.stringify(inventory);
  i.el('item-actions').children[1].onclick();
  assert(i.el('details').html.includes('储蓄 15g'));
  assert(i.el('details').html.includes('记录触发 2次'));
  assert(i.el('details').html.includes('完成订单进度 1/3'));
  assert(i.el('details').html.includes('&lt;store&gt;'));
  inventory.stats.item_storage.synthetic_store = 20;
  await i.render(); assert(i.el('details').html.includes('储蓄 20g'));
  i.el('item-actions').children[3].onclick();
  assert(i.el('details').html.includes('已触发 1次'));
  inventory.essences = [];
  await i.render(); assert.strictEqual(i.el('detail-kind').textContent, '未选择');
  assert(i.el('details').textContent.includes('不再持有'));
  assert.strictEqual(i.apiCalls(), 0);
  // The only differences are the explicit test fixture updates above.
  const originalInventory = JSON.parse(beforeInventory);
  originalInventory.stats.item_storage.synthetic_store = 20; originalInventory.essences = [];
  assert.strictEqual(JSON.stringify(inventory), JSON.stringify(originalInventory));
  inventory.pending_choices = [{kind:'item', offers:[], can_skip:true}];
  inventory.available_actions = ['skip'];
  await i.render();
  assert(i.el('pending-title').textContent.includes('暂无可选物品'));
  assert.strictEqual(i.el('pending-offers').children.length, 0);
  assert.strictEqual(i.el('pending-skip').disabled, false);
  assert.strictEqual(i.el('pending-reroll').disabled, true);
  inventory.pending_choices = [{kind:'ingredient', offers:[], can_skip:true}];
  await i.render();
  assert(i.el('pending-title').textContent.includes('暂无合法候选'));
  assert.strictEqual(i.el('pending-skip').disabled, false);
  assert.strictEqual(i.el('pending-reroll').disabled, true);
  const seedBrowser = browser(base()); await seedBrowser.render();
  seedBrowser.el('seed-input').value = '18446744073709551615';
  await seedBrowser.el('new-game-form').onsubmit({preventDefault(){}});
  assert.strictEqual(seedBrowser.newGameInputs[0][0], '18446744073709551615');
  seedBrowser.el('seed-input').value = '1.5';
  await seedBrowser.el('new-game-form').onsubmit({preventDefault(){}});
  assert.strictEqual(seedBrowser.newGameInputs.length, 1);
  console.log('frontend hints passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
