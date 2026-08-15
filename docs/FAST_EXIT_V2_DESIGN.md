# Merge-First Fast-Exit V2 — Design Document

Version: V2 (reboot/fast-exit-v2)
Base: `e98491847f12635aa028e2bd116a27d196a8f12f` (V1)
Repository: `F4uk/Polymarket-rewards1.0`

> Reward orders exist to earn Polymarket Liquidity Rewards.  Once a reward BUY
> is filled, the resulting inventory is **not** a directional investment.  It
> is an inventory-risk event that must be converted back to collateral as
> quickly as practical while minimizing realized loss.  This document records
> the audited V1 flow, the problems it has, the V2 target flow, the areas that
> are deliberately frozen, the migration plan, and the safety invariants that
> must never regress.

---

## 1. CURRENT_FLOW (audited V1, base e9849184)

The V1 post-fill pipeline, per `engine/monitor.py`, `engine/manager.py`,
`engine/take_profit.py`, `engine/merge.py`, `engine/exit_router.py`,
`engine/liquidation.py`, `api/ctf.py`, `models/database.py`:

1. **Reward BUY placement** (`manager.place_orders`): scanner/laddering proposes
   per-token POST-ONLY reward BUY sizes; `held_side_info` + `side_pauses` block
   the held token side; per-market budget = `min(balance, max_exposure_usd) -
   held_value`.
2. **Fill detection** (`monitor.check_buy_orders` / `_handle_fill`): CLOB
   `get_trades` flattened by `select_new_buy_fills`, dedup on
   `(trade_id, order_id)`; on fill: set `side_pause` (cooldown minutes), cancel
   the order remainder, no trades-table write (positions are authoritative).
3. **Resolution guard** (`check_resolution`): Gamma `umaResolutionStatus` non-empty
   -> cancel that market's BUY orders (fail-open on Gamma failure).
4. **Merge pass** (`check_merges`, `_reconcile_planned_merges`,
   `_confirm_unresolved_merges`): for Type3 wallets with `merge_enabled`, scan
   confirmed positions grouped by condition; `ordinary_binary_plan` finds the
   same-condition YES+NO complete set (NegRisk/ambiguous -> no plan); cancel
   SELL reservations, refetch positions, create durable `merge_operations`
   row (`planned` -> relayer `submitted` -> `confirmed` with FIFO
   `consumed_lots` + `realized_pnl`); partial unique index prevents a second
   active operation per (wallet, condition).
5. **Urgent FOK+Merge** (`_urgent_merge_route`, only when V1 plan_exit already
   decided B0 market stop): Type3 only; `place_complement_fok_buy` (V2 market
   order, FOK, collateral = size × limit price); durable planned merge row
   persisted before FOK; `_pending_fok_until` grace barrier so an accepted /
   indeterminate FOK is never immediately market-sold; `_reconcile_planned_merges`
   waits for confirmed Data API inventory before submitting the relayer Merge.
6. **Low-balance** (`check_low_balance`): threshold 0=off; when triggered,
   `plan_liquidation` priority order (low-reward / small / loss-ascending),
   `_market_dump` (FAK market SELL) per position; merge-reserved assets and
   in-flight merge conditions skipped; 60s cooldown after any dump.
7. **Exit** (`check_exit` / `_exit_position` / `plan_exit`): two-stage:
   - cost ≤ best bid (profit): rest a maker SELL at best ask (A), floored to
     cost; `take_profit_mode=market` -> immediate A_market FAK.
   - cost > best bid: rest at cost (B_park) — **hard cost floor**, never below
     cost; loss ≥ θ_stop (percent of cost or fixed cents) -> B0 FAK market stop.
   - resolution: `_resolution_dump` unconditional market dump (even without cost).
   - `paired_reservations` holds complete sets out of unilateral exit.
8. **Compliance** (`check_sell_orders` / `_check_compliance`): resting BUY
   re-checks spread / reward band / price band / cliff; cancels violations.
9. **Ledgers**: `actions` (order mutations), `trades` (stop-loss fills),
   `daily_pnl` (authoritative reward + rebate + sell profit/loss/fee per Beijing
   day, rebuilt from `/activity` + `get_trades` + confirmed merge PnL),
   `merge_operations` (durable Merge ledger with FIFO + PnL).

## 2. PROBLEMS (why V2 exists)

- **No immediate FOK+Merge**: the complement+Merge route is only considered
  after `plan_exit` already decided a B0 market stop.  A residual that is
  economically better closed via complement+Merge waits until deep loss.
- **Cost floor on maker SELL** (`plan_exit` B_park + `_exit_position` clamp):
  a resting sell can never go below cost, so inventory with a stale/high cost
  basis rests at an unrealistic price and is never exited quickly.
- **Maker waiting is unbounded by design**: there is no bounded "maker escape
  window"; a parked position can rest at cost indefinitely.
- **No churn protection**: after a loss exit, nothing stops the same token from
  being bought again immediately (side_pause is only `cooldown_minutes`), then
  losing again — repeated fill -> loss -> rebuy churn.
- **Opposite-side reward BUY can overfill**: residual NO 20 + reward YES 50
  creates a complete set for 20 and an accidental YES 30 directional exposure.
- **No durable exit cycle / no per-condition exit audit trail**: closure
  mechanism (MERGE / FOK+MERGE / MAKER / MARKET) is not recorded per inventory
  event; restart cannot answer "what happened to this fill".
- **Low-balance / resolution dump blindly market-sells** without first merging
  pairs and without comparing protected routes (specifically low-balance dump
  uses FAK without a worst-price bound derived from depth).
- **Market exit has no explicit worst-acceptable-price bound from depth**:
  `place_market_sell` is FAK, but there is no depth-derived limit used to bound
  slippage.
- **Merge readiness is not a new-BUY gate**: Type3 wallets can open new
  positions while automatic Merge is not runtime-ready, creating inventory that
  cannot be merged back.

## 3. TARGET_FLOW (V2)

Reward BUY
-> fill detection (`check_buy_orders`)
-> **cycle created / joined** (`InventoryExitEngine.on_reward_fill`) for
   `(wallet, condition_id)`
-> resolution guard (unchanged, cancels new BUYs)
-> **Merge pass** (`check_merges`, unchanged) — existing complete sets always
   Merge first (Rule #2)
-> **Inventory Exit Engine** (`check_inventory_exit`) for one-sided residual:
   1. immediate protected route comparison (no waiting):
      `DirectRecovery(q)` (depth-weighted) vs `ComplementMergeRecovery(q)`
      (worst-case signed FOK limit price); FOK+Merge executes immediately when
      its advantage ≥ `merge_advantage_min_usd` (0.01 default).
   2. otherwise bounded **maker escape window** (`maker_exit_wait_sec`, default
      30, min 0): held-side maker SELL may rest (no cost floor, never crossing);
      opposite reward POST-ONLY BUY may rest, capped to unpaired residual.
   3. timeout / emergency (loss ≥ 紧急退出阈值) / low-balance / resolution:
      recompute both routes and execute the best **protected** immediate route
      (FOK+Merge, or FAK market SELL with worst acceptable price from depth);
      neither executable -> BLOCKED + retry, no invented execution.
-> cycle closes when managed inventory returns to zero; every leg is durable;
   exit method is auditable as MERGE / FOK+MERGE / MAKER / MARKET / MIXED.

Placement authority: scanner/laddering proposes; the Inventory Exit Engine is
the final authority:

- same held token: Reward BUY blocked while the cycle owns residual (Rule #1);
- opposite token: effective Reward BUY qty ≤ unpaired residual − already-open
  opposite BUY qty;
- Type3 + `fast_exit_enabled` + `merge_enabled` + `require_merge_ready_for_new_buys`
  + Merge runtime not READY -> new Reward BUYs paused (existing inventory,
  scanning, reconciliation continue).

## 4. UNCHANGED_AREAS (frozen, V2 does not redesign)

- market scanner / discovery / pagination / category whitelist
- reward eligibility, reward range, gap-wide / gap-mid / cliff logic
- size tiers, reward amount calculation, candidate ranking
- max concurrent markets, Post-Only Reward BUY behavior
- multi-wallet architecture, proxies, wallet encryption
- Type3 identity validation, Builder/Relayer encrypted credentials
- Deposit Wallet validation, Type3MergeClient transaction construction
- Merge adapter / calldata, Merge condition lock, Merge durable ledger,
  FIFO accounting primitives
- CLOB auth, geoblock / `trading_enabled` protections
- resolution guard (BUY cancellation), cooldown, side_pause

No new: AI/ML, automatic market blacklist, scanner strategy, reward formula,
cross-market arbitrage, NegRisk conversion, Split arbitrage, prediction logic.

## 5. MIGRATION_PLAN

1. `models/database.py`: add `inventory_exit_cycles` and `inventory_exit_legs`
   (CREATE TABLE IF NOT EXISTS + partial unique index on active non-CLOSED
   cycles per wallet+condition, NOCASE) — idempotent on empty DB, V1 DB, and
   repeated runs.  No existing table/column is altered; wallet secrets,
   templates, merge ledger and daily PnL ledger are untouched.
2. `config.py`: new template defaults `fast_exit_enabled=True`,
   `maker_exit_wait_sec=30`, `require_merge_ready_for_new_buys=True`;
   `merge_advantage_min_usd` keeps its key with clarified V2 meaning.
   Backend keys for stop loss are preserved (`stop_loss_mode`,
   `stop_loss_percent`, `theta_stop_cents`) with the new product semantics
   "紧急退出阈值"; no incompatible DB migration.
3. `engine/inventory_exit.py`: new module owning post-fill inventory.
   `engine/merge.py` stays a pairing/planning primitive; `engine/exit_router.py`
   stays pure economics; `api/ctf.py` stays funds-execution infrastructure.
4. `engine/monitor.py` is reduced toward orchestration: V2 logic lives in the
   engine; legacy `check_exit` / `check_low_balance` remain for
   `fast_exit_enabled=False` compatibility.
5. `engine/manager.py`: placement safety (Rule #1 block, opposite cap, Merge
   readiness gate) applied at order execution; scanner proposals unchanged.
6. Web: config parity (11-point rule), wallet status, monitor exit view,
   history exit cycles, dashboard true net result, per-market economics.
7. Tests: economics, inventory flows, churn, races, relayer gate,
   low-balance/resolution, DB/restart, UI config parity.
8. Docs: README / 使用说明 / RELEASE_NOTES / 系统逻辑与参数说明 / help.
9. Legacy exit code stays behind `fast_exit_enabled=False`; it is not deleted
   in this task.

## 6. SAFETY_INVARIANTS

- One active non-CLOSED cycle per `(wallet, condition_id)` — enforced by a
  SQLite partial unique index + BEGIN IMMEDIATE guard; canonical
  `condition_key()` (lowercased) everywhere.
- Same held token Reward BUY is blocked while the cycle owns residual; cycle
  close does not bypass the existing cooldown.
- Opposite Reward BUY is capped: open + new ≤ unpaired residual.
- Existing complete sets always Merge first; never unilaterally sold; NegRisk /
  ambiguous / cross-condition never merged.
- DirectRecovery uses executable depth (never `best_bid * qty`); direct market
  exit carries a worst acceptable price (FAK marketable limit at the last
  consumed bid level).
- ComplementMergeRecovery uses the actual worst-case signed FOK limit, never
  an optimistic best ask; insufficient complement depth -> no FOK.
- FOK + existing order race: lock -> CLOSING -> cancel opposite BUY -> cancel
  SELL reservations -> confirm cancellations by refetch -> refetch positions ->
  exact missing qty -> refetch book -> recompute economics -> submit exact FOK
  -> persist/reconcile.  No stale quantity; no FOK without confirmed cancels.
- FOK accepted ≠ inventory confirmed ≠ Merge submitted ≠ Merge confirmed.
  Transport uncertainty -> BLOCK / wait, never "assume failure, therefore sell".
- Merge runtime readiness gate uses actual runtime-capability state with a
  short cache; never only yesterday's `last_test_ok`.
- Emergency / low-balance / resolution never blind market-sell: pairs Merge
  first, residual uses the best protected immediate route.
- Fail-closed: positions/orders/FOK/Merge outcome unknown, cancellation not
  reconciled, Deposit Wallet mismatch, Relayer auth failure -> BLOCKED + retry.
- Multi-wallet isolation: every cycle row, lock, and decision is
  wallet-scoped; tests include two wallets in one condition.
- No live funds mutations during implementation/testing; no live BUY/SELL/FOK,
  no live Merge/Split/Redeem/wallet deploy; all funds-path tests use mocks.

## 7. SERVER ROLLOUT REQUIREMENT (fix pack 3)

Before the first V2 canary on a server:

- **open orders = 0** — Reward BUY orders that predate `bot_buy_orders` have no
  provenance and must not be claimed as bot orders.  They are displayed as
  `LEGACY/UNPROVEN OPEN BUY` and their fills are treated as UNMANAGED; they are
  never auto-cancelled, but the operator should clear them before the canary.
- **legacy unmanaged positions = 0** — inventory without matching
  `bot_buy_orders` + authoritative BUY fills is never auto-managed (no SELL /
  FOK / Merge); it is shown as UNMANAGED.  The operator must either close these
  positions or deliberately accept them as UNMANAGED before enabling V2.

After the canary, every Reward BUY order_id is persisted to `bot_buy_orders`
at placement; a persistence failure cancels the just-placed order and stops
the opening path (fail-closed).  Automatic Merge only pairs bot-owned YES/NO
quantities (managed inventory), never manual inventory on the same funder.
