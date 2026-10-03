(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const screens = { menu: $("menu-screen"), game: $("game-screen"), codex: $("codex-screen"), settings: $("settings-screen"), result: $("result-screen") };
  const app = { api: null, state: null, selectedUid: null, selectedInventory: null, selecting: false, lastLogs: [], ready: false, busy: false, settings: {}, hintsSeen: new Set(), activeHint: null };
  const modeNames = { none: "主线", giant: "巨型", rapid: "迅速", blind_box: "盲盒", minimal: "极简", mutation: "变异" };

  function esc(value) { return String(value == null ? "" : value).replace(/[&<>\"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c])); }
  function showScreen(name) { Object.entries(screens).forEach(([key, el]) => el.classList.toggle("hidden", key !== name)); }
  function toast(text, error) { const el = $("toast"); el.textContent = text; el.classList.toggle("error", !!error); el.classList.add("visible"); clearTimeout(app.toastTimer); app.toastTimer = setTimeout(() => el.classList.remove("visible"), 2600); }
  function readLocal(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }
  function setting(name, fallback) { if (Object.prototype.hasOwnProperty.call(app.settings, name)) return app.settings[name]; const v = readLocal("ce_" + name); return v == null ? fallback : v === "true"; }
  function saveSetting(name, value) { app.settings[name] = value; try { localStorage.setItem("ce_" + name, String(value)); } catch (_) { /* Keep settings for this window when browser storage is unavailable. */ } }

  function mechanismHints(state) {
    // Explain only observable mechanics. This is not a second rules engine or
    // a claim that every geometric neighbour actually receives an effect.
    const rows = state.ingredients || [], defs = rows.map((row) => row.definition || {}), hints = [];
    if ((state.essences || []).length) hints.push(["essence", "精粹通常是一次性效果：满足详情里的条件后自动触发并消耗；特殊保留效果以物品描述为准，不是永久物品。"]);
    if (rows.some((row) => Number(row.permanent_bonus || 0) !== 0)) hints.push(["permanent", "永久价值变化不会随回合结束清除；详情中会分别列出基础价值和永久成长。"]);
    if (defs.some((row) => row.remove_after)) hints.push(["countdown", "有成分会在出场计数达到条件后自行离场；留意卡片计数与完整文案，离场奖励按各自效果结算。"]);
    if (defs.some((row) => row.aura || row.periodic_adjacent_permanent_growth || row.adjacent_event_growth)) hints.push(["adjacency", "悬停或选中成分可以查看相邻位置。高亮表示位置邻接，是否受到效果还要看标签和完整文案。"]);
    const tokens = state.tokens || {};
    if (Number(tokens.roll || 0) > 0 || Number(tokens.remove || 0) > 0) hints.push(["token_ops", "Roll重调当前候选；Delete进入目标选择，可删除池中未出场的合法成分。删除前先处理待选奖励。"]);
    if (Number(tokens.essence || 0) > 0) hints.push(["essence_token", "Essence Token会在成功完成订单时转换为精粹选择，不是点击后立即使用的精粹。"]);
    return hints;
  }

  function renderHint(state) {
    const panel = $("mechanism-hint");
    if (!panel) return;
    if (!state || state.ok === false || state.status !== "playing" || !setting("hints", true)) {
      panel.classList.add("hidden"); app.activeHint = null; return;
    }
    const eligible = mechanismHints(state).filter(([key]) => !app.hintsSeen.has(key) && readLocal("ce_hint_v1_" + key) !== "true");
    const hint = eligible.find(([key]) => key === app.activeHint) || eligible[0];
    panel.classList.toggle("hidden", !hint);
    if (!hint) { app.activeHint = null; return; }
    app.activeHint = hint[0]; panel.dataset.hintKind = hint[0];
    $("mechanism-hint-text").textContent = hint[1];
  }

  function dismissHint() {
    if (app.activeHint) {
      app.hintsSeen.add(app.activeHint);
      try { localStorage.setItem("ce_hint_v1_" + app.activeHint, "true"); } catch (_) { /* Session memory still prevents repetition. */ }
    }
    app.activeHint = null; renderHint(app.state);
  }

  async function call(method, ...args) {
    if (!app.api || typeof app.api[method] !== "function") { toast("桌面桥接尚未就绪，请重新打开窗口。", true); return null; }
    const mutating = ["action", "new_game", "continue_game", "save", "close"].includes(method);
    if (mutating && app.busy) return null;
    if (mutating) { app.busy = true; document.body.classList.add("api-busy"); }
    try {
      const result = await app.api[method](...args);
      if (result && result.ok === false) { toast(result.error && result.error.message || "操作失败", true); }
      if (result && result.protocol === "crucible-echoes-desktop/v1") { render(result); }
      return result;
    } catch (err) { toast("操作失败：" + err, true); return null; }
    finally { if (mutating) { app.busy = false; document.body.classList.remove("api-busy"); } }
  }

  function render(state) {
    if (!state) return;
    app.state = state;
    $("preview-box").classList.add("hidden");
    app.selecting = false;
    $("selection-hint").classList.add("hidden");
    if (state.screen === "menu" || !state.status || state.status === "menu") { showScreen("menu"); return; }
    if (state.status === "won" || state.status === "lost") { renderResult(state); return; }
    showScreen("game");
    $("difficulty-badge").textContent = "D" + (state.difficulty || 1);
    $("mode-badge").textContent = state.peace_mode ? "和平模式" : state.endless_mode ? "无限模式" : modeNames[state.fun_mode] || state.fun_mode || "主线";
    $("order-number").textContent = state.endless_mode ? "∞" + state.endless_order : (state.peace_mode ? "∞" + state.peace_order : state.order || "—");
    $("gold-value").textContent = (state.gold || 0) + "g";
    $("current-value").textContent = (state.current_value || 0) + "g";
    $("target-value").textContent = state.peace_mode ? (state.peace_target || 1000000) + "g 和平目标" : (state.order_amount || 0) + "g";
    $("turns-value").textContent = state.peace_mode ? (state.spins_left || 0) + " / 7" : (state.spins_left || 0) + " / " + (state.order_spins || 0);
    $("progress-bar").style.width = Math.min(100, Math.max(0, (state.order_progress || 0) * 100)) + "%";
    $("pool-summary").textContent = "池 " + (state.pool_size || 0) + "/" + (state.board_capacity || 20);
    $("board-caption").textContent = (state.board_rows || 4) + "×" + (state.board_columns || 5) + (state.expanded ? " +1" : "") + " · " + (state.board_capacity || 20) + "格";
    $("board").style.setProperty("--board-columns", state.board_columns || 5);
    $("roll-token").textContent = state.tokens && state.tokens.roll || 0;
    $("delete-token").textContent = state.tokens && state.tokens.remove || 0;
    $("essence-token").textContent = state.tokens && state.tokens.essence || 0;
    const ban = $("ban-state");
    ban.classList.toggle("hidden", !state.ingredient_generation_disabled && !state.ingredient_generation_permanently_disabled);
    ban.textContent = state.ingredient_generation_permanently_disabled ? "禁令精粹：永久禁生成" : "禁令：已开启";
    renderBoard(state);
    renderPool(state);
    renderPending(state);
    renderControls(state);
    renderItems(state);
    renderLog(state);
    renderActionChanges(state);
    renderHint(state);
    if (setting("animations", true)) {
      const panel = document.querySelector(".board-panel");
      if (panel) { panel.classList.remove("flash"); void panel.offsetWidth; panel.classList.add("flash"); }
    }
    if (app.selectedUid) selectCard(app.selectedUid, false);
  }

  function renderBoard(state) {
    const board = $("board");
    board.innerHTML = "";
    (state.board || []).forEach((card) => {
      const el = document.createElement("article");
      el.className = "ingredient-card" + (card.present === false ? " removed" : "");
      el.dataset.uid = String(card.uid);
      const coord = card.coord || [0, 0];
      el.style.gridColumn = String(Number(coord[1]) + 1);
      el.style.gridRow = String(Number(coord[0]) + Number(state.board_row_offset || 1));
      el.tabIndex = 0;
      el.setAttribute("role", "button");
      el.setAttribute("aria-label", (card.name || card.id) + "，查看详情或选择操作目标");
      const tags = (card.tags || []).slice(0, 3).join(" · ");
      const counter = card.counter ? "计数 " + card.counter : "";
      el.innerHTML = "<span class=\"rarity\">" + esc("◆".repeat(Math.max(1, Number(card.rarity || 1)))) + "</span>" +
        "<div class=\"card-name\">" + esc(card.name || card.id) + "</div>" +
        "<div class=\"card-meta\">稀有度 " + esc(card.rarity || 0) + "级 · 基础 " + esc(card.base || 0) + "g</div>" +
        "<div class=\"card-value\">" + esc(card.value || 0) + "g <small>" + (card.value_kind === "settled" ? "本轮结算" : "稳定价值") + "</small></div>" +
        "<div class=\"card-tags\">" + esc(tags) + "</div>" +
        (counter ? "<div class=\"card-counter\">" + esc(counter) + "</div>" : "");
      el.addEventListener("mouseenter", () => selectCard(card.uid, true));
      el.addEventListener("focus", () => selectCard(card.uid, true));
      const activate = () => {
        if (app.selecting) { removeTarget(card); return; }
        selectCard(card.uid, false);
      };
      el.addEventListener("click", activate);
      el.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault(); activate();
        }
      });
      board.appendChild(el);
    });
  }

  function poolCardFromRow(row) {
    const definition = row.definition || {};
    return {
      uid: row.uid, id: row.id, pool_slot: row.slot,
      name: definition.name || row.id, rarity: definition.rarity || 0,
      base: definition.base || 0, value: Number(row.stable_value || 0), value_kind: "stable",
      description: definition.description || "", permanent_bonus: row.permanent_bonus || 0,
      counter: row.counter || 0, flags: row.flags || {}, tags: definition.tags || [], neighbors: [],
    };
  }

  function renderPool(state) {
    const root = $("pool-list");
    if (!root) return;
    const boardUids = new Set((state.board || []).map((row) => String(row.uid)));
    const rows = (state.ingredients || []).filter((row) => !boardUids.has(String(row.uid)));
    root.innerHTML = "";
    $("pool-inventory-heading").classList.toggle("hidden", rows.length === 0);
    root.classList.toggle("hidden", rows.length === 0);
    rows.forEach((row) => {
      const card = poolCardFromRow(row);
      const el = document.createElement("button");
      el.type = "button";
      el.className = "pool-card";
      el.dataset.uid = String(card.uid);
      el.dataset.poolSlot = String(card.pool_slot);
      el.innerHTML = "<strong>" + esc(card.name) + "</strong><span>稀有度 " + esc(card.rarity) + "级 · 基础 " + esc(card.base) + "g</span>";
      el.addEventListener("click", () => {
        if (app.selecting) { removeTarget(card); return; }
        selectCard(card.uid, false, card);
      });
      el.addEventListener("focus", () => selectCard(card.uid, true, card));
      root.appendChild(el);
    });
  }

  function selectCard(uid, hover, poolCard) {
    const state = app.state;
    if (!state) return;
    const poolRow = (state.ingredients || []).find((x) => String(x.uid) === String(uid));
    const card = poolCard || (state.board || []).find((x) => String(x.uid) === String(uid)) || (poolRow && poolCardFromRow(poolRow));
    if (!card) {
      app.selectedUid = null;
      document.querySelectorAll(".ingredient-card, .pool-card").forEach((el) => {
        el.classList.remove("selected"); el.classList.remove("neighbor");
      });
      $("detail-kind").textContent = "未选择";
      $("details").textContent = "此成分已离场。悬停或点击成分，查看完整效果与邻接关系。";
      return;
    }
    app.selectedUid = uid;
    app.selectedInventory = null;
    document.querySelectorAll(".ingredient-card, .pool-card").forEach((el) => {
      el.classList.toggle("selected", el.dataset.uid === String(uid));
      el.classList.toggle("neighbor", (card.neighbors || []).some((i) => state.board[i] && String(state.board[i].uid) === el.dataset.uid));
    });
    $("detail-kind").textContent = "成分" + (hover ? " · 邻接预览" : "");
    const flags = Object.entries(card.flags || {}).filter(([, v]) => v !== false && v !== 0 && v !== "").map(([k, v]) => esc(k) + ": " + esc(v));
    $("details").innerHTML = "<div class=\"detail-block\"><div class=\"detail-title\"><h3>" + esc(card.name || card.id) + "</h3><span class=\"detail-value\">" + esc(card.value || 0) + "g</span></div>" +
      "<p class=\"detail-description\">" + esc(card.description || "暂无描述") + "</p><div class=\"stat-list\">" +
      "<span>稀有度 <strong>" + esc(card.rarity) + "级</strong></span><span>基础 <strong>" + esc(card.base) + "g</strong></span>" +
      "<span>永久成长 <strong>" + ((card.permanent_bonus || 0) >= 0 ? "+" : "") + esc(card.permanent_bonus || 0) + "g</strong></span><span>出场计数 <strong>" + esc(card.counter || 0) + "</strong></span>" +
      "<span>价值口径 <strong>" + (card.value_kind === "settled" ? "本轮已结算" : "稳定价值（不含随机/邻接）") + "</strong></span>" +
      "</div></div>" + (flags.length ? "<div class=\"muted\">状态：" + flags.join(" · ") + "</div>" : "") +
      "<div class=\"muted\">邻接目标：" + (card.neighbors || []).map((i) => state.board[i] && esc(state.board[i].name)).filter(Boolean).join("、") + "</div>";
  }

  function removeTarget(card) {
    const spec = (app.state.available_action_specs || []).find((x) => x.action === "remove" && Number(x.index) === Number(card.pool_slot));
    if (!spec) { toast("这个成分当前不能删除", true); return; }
    app.selecting = false; $("selection-hint").classList.add("hidden");
    document.querySelectorAll(".ingredient-card, .pool-card").forEach((el) => el.classList.remove("target", "invalid"));
    call("action", "remove", { index: Number(card.pool_slot) });
  }

  function renderControls(state) {
    const actions = state.available_actions || [];
    $("roll-btn").disabled = !actions.includes("reroll");
    const hasPending = Array.isArray(state.pending_choices) && state.pending_choices.length > 0;
    $("end-turn-btn").disabled = !actions.includes("spin") || hasPending || state.status !== "playing";
    $("delete-btn").disabled = !actions.some((x) => x.indexOf("remove ") === 0);
    $("roll-btn").onclick = () => call("action", "reroll");
    $("end-turn-btn").onclick = () => call("action", "end_turn");
    $("delete-btn").onclick = () => {
      if ($( "delete-btn").disabled) return;
      app.selecting = true; $("selection-hint").classList.remove("hidden");
      const legal = new Set((state.available_action_specs || []).filter((x) => x.action === "remove").map((x) => String(x.index)));
      document.querySelectorAll(".ingredient-card, .pool-card").forEach((el) => {
        const boardCard = state.board.find((x) => String(x.uid) === el.dataset.uid);
        const slot = el.dataset.poolSlot || String((boardCard || {}).pool_slot || "");
        el.classList.toggle("target", legal.has(slot));
        el.classList.toggle("invalid", !legal.has(slot));
      });
    };
  }

  function renderPending(state) {
    const pending = state.pending_choices && state.pending_choices[0];
    const panel = $("pending-panel");
    panel.classList.toggle("hidden", !pending);
    if (!pending) return;
    $("pending-title").textContent = pending.kind === "run_end" ? "本局选择" : (!(pending.offers || []).length ? (pending.kind === "item" ? "暂无可选物品，请跳过本次奖励" : "暂无合法候选，请跳过本次奖励") : "选择成分 / 道具");
    $("pending-source").textContent = pending.source || "奖励";
    const offers = $("pending-offers"); offers.innerHTML = "";
    (pending.offers || []).forEach((offer) => {
      const def = offer.definition || {};
      const btn = document.createElement("button"); btn.className = "offer";
      const rarity = def.rarity == null ? "" : "稀有度 " + def.rarity + "级";
      const base = def.base == null ? "" : "基础 " + def.base + "g";
      const meta = [rarity, base].filter(Boolean).join(" · ");
      btn.innerHTML = "<strong>" + esc(def.name || offer.id) + "</strong>" +
        (meta ? "<small class=\"offer-meta\">" + esc(meta) + "</small>" : "") +
        "<span>" + esc(def.description || "选择此项") + "</span>";
      btn.onclick = () => call("action", "choose", { number: offer.index });
      offers.appendChild(btn);
    });
    const actions = state.available_actions || [];
    $("pending-skip").disabled = !actions.includes("skip");
    $("pending-reroll").disabled = !actions.includes("reroll");
    $("pending-skip").onclick = () => call("action", "skip");
    $("pending-reroll").onclick = () => call("action", "reroll");
  }

  function renderItems(state) {
    const root = $("item-actions"); root.innerHTML = "";
    const specs = state.available_action_specs || [];
    [["物品", state.items || []], ["精粹", state.essences || []]].forEach(([kind, rows]) => {
      if (!rows.length) return;
      const heading = document.createElement("strong"); heading.className = "inventory-heading"; heading.textContent = kind + " · " + rows.length; root.appendChild(heading);
      rows.forEach((item) => {
        const btn = document.createElement("button"); btn.className = "inventory-entry"; btn.textContent = item.name || item.id;
        btn.title = item.description || "";
        btn.onclick = () => {
          app.selectedUid = null;
          app.selectedInventory = {kind, id:item.id};
          document.querySelectorAll(".ingredient-card, .pool-card").forEach((el) => { el.classList.remove("selected"); el.classList.remove("neighbor"); });
          inventoryDetails(state, item, kind);
        };
        root.appendChild(btn);
        if (item.active || item.toggle_flag) {
          const action = item.toggle_flag ? "toggle" : "use";
          const use = document.createElement("button"); use.className = "inventory-use";
          use.textContent = item.toggle_flag ? (state.ingredient_generation_disabled ? "关闭" : "开启") : "使用";
          use.disabled = !specs.some((spec) => spec.action === action && spec.item_id === item.id);
          use.onclick = () => call("action", action, { item_id: item.id });
          root.appendChild(use);
        }
      });
    });
    if (app.selectedInventory) {
      const selected = app.selectedInventory, rows = selected.kind === "精粹" ? state.essences : state.items;
      const item = (rows || []).find((row) => row.id === selected.id);
      if (item) inventoryDetails(state, item, selected.kind);
      else {
        app.selectedInventory = null;
        $("detail-kind").textContent = "未选择";
        $("details").textContent = "不再持有此物品或精粹；已见内容仍可在图鉴查看。";
      }
    }
  }

  function inventoryDetails(state, item, kind) {
    const stats = state.stats || {}, lines = [];
    if (kind === "精粹") lines.push("已触发 " + Number((stats.essence_hits || {})[item.id] || 0) + "次；消耗或保留按当前物品规则结算。");
    else {
      const counts = stats.item_trigger_counts || {};
      if (Object.prototype.hasOwnProperty.call(counts, item.id)) lines.push("记录触发 " + counts[item.id] + "次（仅已记录事件）");
      if (item.order_savings || Object.prototype.hasOwnProperty.call(stats.item_storage || {}, item.id)) lines.push("储蓄 " + Number((stats.item_storage || {})[item.id] || 0) + "g");
      Object.entries(item.event_bonus_every || {}).forEach(([event, rule]) => {
        const every = Math.max(1, Number(rule.every || 1)), count = Number((stats.item_event_counts || {})[item.id + ":" + event] || 0);
        const label = {order_completed:"完成订单", ingredient_chosen:"完成成分选择"}[event] || event;
        lines.push(label + "进度 " + (count % every) + "/" + every);
      });
    }
    $("detail-kind").textContent = kind;
    $("details").innerHTML = "<div class=\"detail-block\"><h3>" + esc(item.name || item.id) + "</h3><p class=\"detail-description\">" + esc(item.description || "") + "</p><p class=\"muted\">" + (item.rarity == null ? "有限次触发后消耗" : "稀有度 " + esc(item.rarity) + "级") + "</p>" + lines.map((line) => "<p>" + esc(line) + "</p>").join("") + "</div>";
  }

  function renderLog(state) {
    const logs = state.last_log || [];
    if (logs.join("\n") === app.lastLogs.join("\n")) return;
    app.lastLogs = logs.slice();
    $("event-log").innerHTML = logs.slice(0, 14).map((line) => "<div class=\"log-line\">" + esc(line) + "</div>").join("");
  }

  function renderActionChanges(state) {
    const panel = $("action-changes"), changes = state.ok !== false && state.action_changes;
    if (!panel) return;
    const lines = [];
    if (changes && changes.scope === "action_boundary") {
      if (changes.gold && changes.gold.delta) lines.push("存款：" + changes.gold.before + "g → " + changes.gold.after + "g（包含订单支付等结算）");
      Object.entries(changes.tokens || {}).forEach(([token, row]) => {
        if (row.delta) lines.push((token === "remove" ? "Delete" : token === "essence" ? "Essence" : token === "roll" ? "Roll" : token) + "库存：" + row.before + " → " + row.after);
      });
      (changes.ingredients || []).forEach((row) => {
        if (row.kind === "permanent") lines.push(row.name + " 永久加值：" + row.before + "g → " + row.after + "g");
        else if (row.kind === "transformed") lines.push(row.previous_name + " → " + row.name + "（同一实例变化）");
        else if (row.kind === "added") lines.push("加入成分池：" + row.name);
        else if (row.kind === "removed") lines.push("离开成分池：" + row.name);
        if (setting("animations", true)) {
          document.querySelectorAll(".ingredient-card, .pool-card").forEach((el) => {
            if (el.dataset.uid === String(row.uid)) el.classList.add(row.kind === "removed" || row.delta < 0 ? "effect-negative" : "effect-positive");
          });
        }
      });
      (changes.items || []).forEach((row) => lines.push((row.kind === "added" ? "获得物品：" : "不再持有物品：") + row.name + (row.count > 1 ? " ×" + row.count : "")));
      (changes.consumed_essences || []).forEach((row) => lines.push("精粹已消耗：" + row.name + (row.count > 1 ? " ×" + row.count : "")));
    }
    panel.classList.toggle("hidden", lines.length === 0);
    $("action-changes-content").innerHTML = lines.map((line) => "<div>" + esc(line) + "</div>").join("");
  }

  async function preview() { const result = await call("preview"); if (!result || !result.ok) return; $("preview-box").classList.remove("hidden"); $("preview-box").innerHTML = "预计金币 <strong>" + esc(result.gold_after) + "g</strong>（" + (result.delta >= 0 ? "+" : "") + esc(result.delta) + "g）<br><span>随机效果仍以实际结算为准。</span>"; }

  function renderResult(state) {
    showScreen("result");
    const won = state.status === "won";
    $("result-symbol").textContent = won ? "✦" : "☄";
    $("result-title").textContent = won ? "实验成功" : "实验结束";
    $("result-copy").textContent = (won ? "坩埚稳定了下来。" : "实验在压力中停止。") + " 最终金币：" + (state.gold || 0) + "g。";
  }

  function renderCodex(state) {
    showScreen("codex"); const root = $("codex-content"); root.innerHTML = "";
    const names = { ingredients: "成分", items: "物品", essences: "精粹" };
    Object.entries(names).forEach(([key, title]) => {
      const group = document.createElement("section"); group.className = "codex-group"; group.innerHTML = "<h2>" + title + "</h2>";
      const rows = (state.codex && state.codex[key]) || [];
      group.innerHTML += rows.length ? rows.map((row) => "<div class=\"codex-entry\"><strong>" + esc(row.name || row.id) + "</strong><p>" + esc(row.description || "") + "</p></div>").join("") : "<p class=\"muted\">尚未遇到</p>";
      root.appendChild(group);
    });
  }

  function formatRule(value) {
    if (value == null) return "";
    if (typeof value !== "object") return String(value);
    const labels = { order_bonus: "订单加价", token_reward: "Token奖励", remove: "删除Token", roll: "Roll Token", essence: "精粹Token", initial_slag: "初始废渣", slag_interval: "废渣生成间隔（回合）", ingredient_overrides: "成分限制", slag: "废渣", force_value: "最终固定价值（g）", remove_after_bonus: "残留期限增加", post_order_gold_deduction: "成功订单后扣款", from_completed: "起始订单", amount: "金额（g）", spins: "回合数" };
    return Object.entries(value).map(([key, nested]) => {
      if (key === "order_bonus") return Object.entries(nested).map(([order, bonus]) => "第" + order + "单 +" + bonus + "g").join("；");
      if (key === "final_order") return "最终主线第" + (Number(nested.after_completed) + 1) + "单：" + nested.amount + "g / " + nested.spins + "回合";
      return (labels[key] || key) + "：" + (typeof nested === "object" ? formatRule(nested) : nested);
    }).join("；");
  }
  async function loadDifficultyInfo(value) {
    const info = await call("difficulty_info", Number(value || 1));
    if (!info || !info.ok) return;
    const rules = info.rules || [];
    $("difficulty-help").innerHTML = rules.length ? rules.map((row) => "<span class=\"rule-line\"><strong>D" + esc(row.difficulty) + "</strong> " + esc(formatRule(row.rule)) + "</span>").join("") : "标准规则，无额外难度修正。";
  }
  function populateDifficulty() { const select = $("difficulty-input"); for (let i = 1; i <= 15; i++) { const option = document.createElement("option"); option.value = i; option.textContent = "D" + i; select.appendChild(option); } select.addEventListener("change", () => loadDifficultyInfo(select.value)); loadDifficultyInfo(select.value); }

  function wire() {
    if (app.wired) return;
    app.wired = true;
    populateDifficulty();
    $("new-game-btn").onclick = () => document.querySelector(".setup-card").scrollIntoView({ behavior: "smooth" });
    $("new-game-form").onsubmit = async (event) => {
      event.preventDefault();
      const seed = String($("seed-input").value || "1").trim();
      if (!/^[+-]?\d+$/.test(seed)) { toast("种子必须是十进制整数。", true); return; }
      // Do not round a uint64 seed through JavaScript's floating-point Number.
      await call("new_game", seed, Number($("difficulty-input").value || 1), $("fun-mode-input").value, (app.state && app.state.save_path) || ".saves/current.json");
    };
    $("continue-btn").onclick = () => call("continue_game", (app.state && app.state.save_path) || ".saves/current.json");
    $("save-btn").onclick = () => call("save");
    $("back-menu-btn").onclick = async () => { const result = await call("save"); if (result && result.ok) showScreen("menu"); };
    $("exit-btn").onclick = () => call("close");
    $("preview-btn").onclick = preview;
    $("cancel-selection").onclick = () => { app.selecting = false; $("selection-hint").classList.add("hidden"); document.querySelectorAll(".ingredient-card, .pool-card").forEach((el) => el.classList.remove("target", "invalid")); };
    $("clear-log-btn").onclick = () => { $("event-log").innerHTML = ""; };
    $("codex-btn").onclick = async () => { const state = app.state || await call("get_state"); if (state) renderCodex(state); };
    $("settings-btn").onclick = () => showScreen("settings");
    $("result-menu-btn").onclick = () => showScreen("menu");
    document.querySelectorAll("[data-back]").forEach((btn) => btn.onclick = () => showScreen("menu"));
    $("hints-setting").checked = setting("hints", true); $("animations-setting").checked = setting("animations", true);
    $("dismiss-hint-btn").onclick = dismissHint;
    $("hints-setting").onchange = (e) => { saveSetting("hints", e.target.checked); renderHint(app.state); }; $("animations-setting").onchange = (e) => saveSetting("animations", e.target.checked);
  }

  function ready() {
    app.api = window.pywebview && window.pywebview.api; app.ready = true;
    const smoke = new URLSearchParams(window.location.search).get("smoke") === "1";
    if (smoke && !app.smokeStarted && app.api && typeof app.api.smoke_result === "function") {
      app.smokeStarted = true;
      runBridgeSmoke();
      return;
    }
    wire();
    loadDifficultyInfo($("difficulty-input").value);
    call("get_state").then((state) => { if (state && state.screen === "codex") renderCodex(state); });
  }
  async function runBridgeSmoke() {
    try {
      const assertControlsVisible = () => {
        const rect = $("end-turn-btn").getBoundingClientRect();
        if (rect.top < 0 || rect.left < 0 || rect.bottom > window.innerHeight || rect.right > window.innerWidth) throw new Error("end-turn control outside viewport " + JSON.stringify({ bottom: rect.bottom, height: window.innerHeight }));
      };
      const initial = await app.api.get_state();
      const created = await app.api.new_game(9001, 1, "none");
      if (!created || !created.ok || created.fun_mode !== "none" || created.seed !== 9001) throw new Error("new_game failed");
      render(created);
      if ($("board").children.length !== created.board.length || $("end-turn-btn").disabled) throw new Error("initial game did not render playable board");
      assertControlsVisible();
      selectCard(created.board[0].uid, false);
      if (!document.querySelector(".ingredient-card.selected")) throw new Error("selected card highlight failed");
      const expectedNeighbors = created.board[0].neighbors.length;
      if (document.querySelectorAll(".ingredient-card.neighbor").length !== expectedNeighbors) throw new Error("neighbor highlight failed");
      const spun = await app.api.action("end_turn");
      if (!spun || !spun.ok || !spun.pending_choices || spun.pending_choices.length < 1) throw new Error("end_turn failed");
      render(spun);
      assertControlsVisible();
      if (!spun.action_changes || spun.action_changes.scope !== "action_boundary" || $("action-changes").classList.contains("hidden")) throw new Error("action changes did not render");
      if ($("pending-offers").children.length !== spun.pending_choices[0].offers.length || !$("end-turn-btn").disabled) throw new Error("reward choice did not render");
      const chosen = await app.api.action("choose", { number: 1 });
      if (!chosen || !chosen.ok || (chosen.pending_choices && chosen.pending_choices.length !== 0)) throw new Error("choose failed");
      render(chosen);
      assertControlsVisible();
      if ($("end-turn-btn").disabled) throw new Error("end turn remained disabled after choice");
      const largeSeed = "18446744073709551615";
      $("seed-input").value = largeSeed;
      await $("new-game-form").onsubmit({ preventDefault() {} });
      if (!app.state || !app.state.ok || app.state.seed_text !== largeSeed || app.state.spin !== 0) throw new Error("large integer seed lost precision across form/bridge");
      assertControlsVisible();
      await app.api.smoke_result(JSON.stringify({ ok: true, initial_screen: initial.screen, seed: chosen.seed, fun_mode: chosen.fun_mode, spin: chosen.spin, protocol: chosen.protocol, viewport: { width: window.innerWidth, height: window.innerHeight }, end_turn_visible: true, large_seed_text: app.state.seed_text, uint64_seed_roundtrip: true }));
    } catch (error) {
      await app.api.smoke_result(JSON.stringify({ ok: false, error: String(error) }));
    }
  }
  window.addEventListener("pywebviewready", ready);
  document.addEventListener("DOMContentLoaded", () => { if (window.pywebview && window.pywebview.api) ready(); else { wire(); toast("请通过桌面版启动，以连接 Python 游戏核心。", true); } });
})();
