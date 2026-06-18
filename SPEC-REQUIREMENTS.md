# Crypto Signal & Trading Bot — Requirements Specification

> **Spec-Driven Development artifact: the SPEC (the *what* and *why*).**
>
> This document defines **what the system must do and why**, expressed as user stories and
> testable acceptance criteria. It is deliberately **implementation-agnostic** — it names no
> language, library, database, or file. *How* the system is built lives in `PLAN.md`
> (the technical **plan**); the ordered work items live in `BUILD_PLAN.md` (the **tasks**).
>
> | SDD layer | File | Answers |
> |---|---|---|
> | **Spec** (this file) | `SPEC-REQUIREMENTS.md` | What & why — requirements, acceptance criteria |
> | Plan | `PLAN.md` | How — architecture, schema, tech stack |
> | Tasks | `BUILD_PLAN.md` | In what order — phased work items |
>
> **Status:** v1.0 · all clarifications RESOLVED (see §12). Ready for build.
> **Principle:** every requirement must be testable and trace to a user story. No speculative
> "might need" features.

---

## 1. Problem statement

A solo, technically-minded trader wants to participate in crypto markets systematically rather
than emotionally, but faces three problems: (1) markets run 24/7 and cannot be watched
continuously by a human; (2) manual trading is dominated by emotion — holding losers, selling
winners early; (3) commercial trading bots are opaque, expensive, often insecure, and give the
user no understanding of *why* a trade was made.

The system exists to let one person run a transparent, auditable, self-hosted trading process
that makes pre-committed decisions mechanically, can be validated without risking money, and
improves measurably from its own results — while the user retains full understanding and
control, and learns by building it.

---

## 2. Users & stakeholders

- **Primary user / operator** — the individual who builds, runs, tunes, and (optionally) trades
  with the system. Technically capable, learning-motivated, risk-aware, manages own capital.
- **Open-source contributor** — a third party who clones the public framework to learn from or
  extend it, without access to the operator's private strategy or funds.

There are **no** end-customers, no managed third-party money, and no multi-tenant users in scope
(see §5 Non-goals).

---

## 3. Goals & success metrics

| # | Goal | Measurable success criterion |
|---|------|------------------------------|
| G1 | Operate unattended | System runs for ≥ 7 consecutive days with no manual intervention and no unhandled crash |
| G2 | Never risk money unintentionally | Zero real orders are ever placed while in simulation mode (verifiable in logs) |
| G3 | Decisions are explainable | 100% of emitted signals carry a human-readable reason; operator can reconstruct any decision from stored data |
| G4 | Truthful evaluation | Simulated results include all costs (fees); backtest and forward-test results are consistent within an agreed tolerance |
| G5 | Improvement is measurable | After a defined sample of trades, the operator can compute win rate, win/loss ratio, net result, and max drawdown, and observe the effect of a tuning change |
| G6 | Edge stays private | No file in the public repository contains tuned parameters, secrets, or trade history |
| G7 | Learning | Operator understands every subsystem well enough to modify it |

**RESOLVED (G4):** backtest and forward-test are "consistent" when win rate agrees within
**±5 percentage points** and net result has the **same sign and is within ~20%**. A larger gap
indicates a bug (typically look-ahead leakage) and blocks trust in either result.

**RESOLVED (G5):** performance is judged on **≥ 100 closed trades**. 30–100 is a weak
preliminary read only; below 30 is noise. No tuning verdict is drawn below the threshold.

---

## 4. In scope

- Continuous collection of market, on-chain, and social-sentiment information for a configured
  set of crypto assets.
- Durable, auditable storage of every observation, decision, and trade.
- Generation of directional trade signals by combining the collected information.
- Pre-trade risk evaluation and position sizing.
- Order placement with protective exit orders, and management of open positions to close.
- Simulation ("paper") mode that mirrors live behavior without real funds.
- Retrospective evaluation of closed trades and operator-driven tuning.
- Operating as free/low-cost software on hardware the operator controls.
- Being published as open source with the operator's strategy kept private.

## 5. Non-goals (explicitly out of scope)

- Managing or trading **other people's** money; acting as an advisor or fund.
- Multi-user accounts, authentication, or a hosted SaaS offering.
- High-frequency / latency-arbitrage trading (sub-second competitive execution).
- Leveraged or derivatives trading in v1 (spot only — see §7 FR-EX).
- A graphical user interface in v1 (deferred; see §8 NFR-OBS).
- Guaranteeing or implying profit. The system is a disciplined process, not a money machine.
- Tax accounting, regulatory reporting, or portfolio accounting beyond the trade journal.

---

## 6. User stories

Priorities: **P0** must-have for a usable system · **P1** important · **P2** later · **P3** nice-to-have.

- **US-1 (P0)** — As the operator, I want the system to watch the markets continuously so that
  I never miss an opportunity while away from the screen.
- **US-2 (P0)** — As the operator, I want every observation and decision recorded so that I can
  audit exactly what the system saw and why it acted.
- **US-3 (P0)** — As the operator, I want a trade signal only when independent information
  sources agree, so that I avoid acting on noise.
- **US-4 (P0)** — As the operator, I want each potential trade checked against my risk rules and
  sized automatically, so that no single trade can do outsized damage.
- **US-5 (P0)** — As the operator, I want every position to have protective exits set the moment
  it opens, so that an unattended position can never run away.
- **US-6 (P0)** — As the operator, I want to run the entire system in simulation with no real
  money, so that I can validate it safely before risking capital.
- **US-7 (P0)** — As the operator, I want realized results to include all trading costs, so that
  my evaluation reflects reality and not a fee-free fantasy.
- **US-8 (P1)** — As the operator, I want a periodic summary of how the system is performing, so
  that I can judge whether it has an edge and tune it.
- **US-9 (P1)** — As the operator, I want to change strategy parameters without changing code,
  so that I can experiment quickly and safely.
- **US-10 (P1)** — As the operator, I want to be notified of signals and trades, so that I stay
  informed without watching the database.
- **US-11 (P0)** — As a contributor, I want to clone and run the framework without ever seeing
  the operator's secrets or strategy, so that the project is safe to open-source.
- **US-12 (P2)** — As the operator, I want the system to suggest which information source is
  helping or hurting, so that my tuning is data-driven rather than guesswork.
- **US-13 (P3)** — As the operator, I want a read-only visual dashboard once the system is
  proven, so that I can see performance at a glance.

---

## 7. Functional requirements

Each requirement is testable. EARS-style "THE SYSTEM SHALL" statements define behavior; selected
requirements include Given/When/Then acceptance scenarios. IDs are stable references.

### Data collection — FR-DC  (US-1, US-2)

- **FR-DC-1** — THE SYSTEM SHALL continuously collect market information (price, volume, and
  derived liquidity/volatility measures) for each configured asset.
- **FR-DC-2** — THE SYSTEM SHALL collect on-chain information (e.g. exchange flow direction,
  large-transfer activity) for each configured asset.
- **FR-DC-3** — THE SYSTEM SHALL collect social-sentiment information and reduce each item to a
  directional sentiment value with a source-credibility weight and a novelty indicator.
- **FR-DC-4** — Collection sources SHALL operate independently; WHEN one source becomes
  unavailable THE SYSTEM SHALL continue collecting from the others without interruption.
- **FR-DC-5** — A collection component SHALL NOT make, influence, or place any trading decision.
  It records observations only.
- **FR-DC-6** — THE SYSTEM SHALL only record **completed** market periods; it SHALL NOT use a
  still-forming period in any stored observation or decision.

> **Acceptance (FR-DC-6):** *Given* a market period that has not yet closed, *when* the collector
> runs, *then* no observation for that period is written until the period completes.

> **Acceptance (FR-DC-4):** *Given* the sentiment source is unreachable, *when* collection runs,
> *then* market and on-chain observations are still recorded and the outage is logged.

### Data persistence — FR-DP  (US-2)

- **FR-DP-1** — THE SYSTEM SHALL durably store every observation, every signal evaluation
  (including non-firing ones), and every trade with its full lifecycle.
- **FR-DP-2** — Components SHALL communicate only through stored data, not by direct invocation,
  so that any component can fail or restart without corrupting another.
- **FR-DP-3** — THE SYSTEM SHALL allow the operator to inspect all stored data after the fact to
  reconstruct any decision.
- **FR-DP-4** — Stored data SHALL be sufficient to replay historical conditions through the
  decision logic (enabling backtesting) without a live data feed.

### Signal generation — FR-SG  (US-3)

- **FR-SG-1** — THE SYSTEM SHALL combine the three information sources into a single composite
  conviction measure and a single direction per asset. In v1 (long-or-flat) the actionable
  direction is **long or no-action**; a bearish reading never produces a short (FR-EX-4).
- **FR-SG-2** — THE SYSTEM SHALL emit a trade signal only WHEN the composite measure meets or
  exceeds a configured threshold **AND** all active sources agree on direction.
- **FR-SG-3** — WHEN a signal is emitted THE SYSTEM SHALL store a human-readable explanation of
  why it fired (the contributing factors).
- **FR-SG-4** — THE SYSTEM SHALL record evaluations that do **not** fire, so that non-action is
  auditable too.
- **FR-SG-5** — The relative influence of each source SHALL be operator-configurable.

> **Acceptance (FR-SG-2):** *Given* a composite measure above threshold but sources disagreeing
> on direction, *when* the engine evaluates, *then* no signal is emitted and the disagreement is
> recorded as the reason.

### Risk management — FR-RM  (US-4)

- **FR-RM-1** — Before any trade, THE SYSTEM SHALL verify current exposure and drawdown are
  within configured limits; WHEN a limit is breached THE SYSTEM SHALL block the trade and record
  the reason.
- **FR-RM-2** — THE SYSTEM SHALL block a trade WHEN market volatility exceeds a configured ceiling.
- **FR-RM-3** — THE SYSTEM SHALL size each position from the operator's measured edge and
  capital, never as a fixed arbitrary amount, scaling larger positions to higher-conviction edges.
- **FR-RM-4** — THE SYSTEM SHALL reject a signal WHEN its expected gain does not exceed the
  round-trip trading cost by a configured margin (a fee-aware edge filter).
- **FR-RM-5** — Blocking a trade is a valid, logged outcome — not an error.

### Trade execution — FR-EX  (US-5, US-7)

- **FR-EX-1** — WHEN opening a position THE SYSTEM SHALL place the protective stop-loss and
  take-profit orders atomically with the entry; it SHALL NOT leave an open position unprotected
  at any point.
- **FR-EX-2** — THE SYSTEM SHALL prefer order types that incur the lower trading fee where doing
  so does not defeat the trade's purpose.
- **FR-EX-3** — THE SYSTEM SHALL deduct all applicable trading fees from every fill, in both
  simulation and live operation, and SHALL store realized results net of fees.
- **FR-EX-4** — THE SYSTEM SHALL support **spot, long-or-flat positions only** in v1. It holds
  either the asset (long) or stablecoin (flat) and SHALL NOT open short positions. A bearish
  consensus SHALL result in no entry, and in closing any open long — never in opening a short.
  Shorting is deferred to a future futures-based version. **RESOLVED.**

> **Acceptance (FR-EX-1):** *Given* an approved trade, *when* it is opened, *then* a stop-loss and
> a take-profit exist for it before the next system cycle; there is no observable window in which
> the position exists without both.

### Position monitoring & exit — FR-MX  (US-5)

- **FR-MX-1** — At entry THE SYSTEM SHALL define a complete set of exit conditions: a stop-loss, a
  take-profit, an optional trailing stop, a thesis-invalidation (signal) exit, and a maximum
  holding time.
- **FR-MX-2** — THE SYSTEM SHALL close a position WHEN the **first** of its exit conditions is met.
- **FR-MX-3** — The stop-loss and take-profit SHALL remain effective even WHEN the system's own
  logic is not running.
- **FR-MX-4** — WHEN the conditions that justified entry no longer hold THE SYSTEM SHALL close the
  position regardless of current profit or loss.
- **FR-MX-5** — WHEN a position's maximum holding time elapses THE SYSTEM SHALL close it.
- **FR-MX-6** — WHEN a position closes THE SYSTEM SHALL record the closing price, which exit fired,
  the realized result net of fees, and whether it was a win.

> **Acceptance (FR-MX-2):** *Given* an open position whose price simultaneously approaches both
> stop and target, *when* one is reached first, *then* the position closes on that exit and the
> others are cancelled — no double-close, no orphaned order.

**RESOLVED (FR-MX-5):** the maximum holding time is a **hard cap that is always active** — no
indefinite holds in v1. Default **48 hours** (for a 1-hour timeframe). Genuine long-term holding
is a separate manual activity, not a system behavior.

### Operating modes & safety — FR-SF  (US-6) — *highest-criticality*

- **FR-SF-1** — THE SYSTEM SHALL provide a simulation mode and a live mode controlled by a single
  explicit setting; simulation SHALL be the default.
- **FR-SF-2** — WHILE in simulation mode THE SYSTEM SHALL NOT place, modify, or cancel any real
  order under any circumstances.
- **FR-SF-3** — Simulation mode SHALL exercise the identical decision, risk, and exit logic as
  live mode, differing only in that fills are simulated.
- **FR-SF-4** — THE SYSTEM SHALL make the active mode unambiguous in its logs and notifications.

> **Acceptance (FR-SF-2):** *Given* simulation mode is active, *when* any signal results in a
> trade, *then* inspection of all outbound activity shows no real order was sent; only a simulated
> fill is recorded. **This is a release-blocking test.**

### Learning & evaluation — FR-LE  (US-8, US-12)

- **FR-LE-1** — THE SYSTEM SHALL compute, over closed trades, at minimum: win rate, average win
  versus average loss, net result after fees, and maximum drawdown.
- **FR-LE-2** — THE SYSTEM SHALL produce a periodic performance summary for the operator.
- **FR-LE-3** — THE SYSTEM SHALL attribute outcomes to information sources so the operator can see
  which source is helping or hurting on losing trades.
- **FR-LE-4** — THE SYSTEM SHALL judge *system* performance on a sample of trades, never declare a
  single trade as evidence of edge, and SHALL make this distinction clear in summaries.
- **FR-LE-5** — Tuning SHALL be operator-driven in v1; the system informs, the operator decides.

### Configuration — FR-CF  (US-9)

- **FR-CF-1** — THE SYSTEM SHALL externalize all tunable behavior (source weights, thresholds,
  risk limits, fees, exit parameters, asset universe, mode) into operator-editable settings,
  requiring no code change to adjust.
- **FR-CF-2** — THE SYSTEM SHALL ship a safe-default configuration template suitable for
  simulation out of the box.

### Notifications — FR-NT  (US-10)

- **FR-NT-1** — THE SYSTEM SHALL be able to notify the operator of emitted signals, opened and
  closed trades, and the periodic summary, via a channel the operator already uses.
- **FR-NT-2** — Notifications SHALL be optional and configurable.

### Open source & privacy — FR-OS  (US-11) — *highest-criticality*

- **FR-OS-1** — THE SYSTEM SHALL keep all secrets, tuned parameters, trained models, and trade
  history out of version control.
- **FR-OS-2** — No file intended for the public repository SHALL contain a value that constitutes
  the operator's trading edge or any credential.
- **FR-OS-3** — A contributor SHALL be able to clone and run the framework in simulation using
  only the public files plus their own credentials.

> **Acceptance (FR-OS-2):** *Given* the repository as published, *when* its tracked files are
> inspected, *then* no secret, no tuned parameter set, and no trade history is present. **This is a
> release-blocking test, verified before the first publish.**

---

## 8. Non-functional requirements

- **NFR-COST** — The system SHALL be runnable at negligible recurring cost on hardware the
  operator controls, using free data sources where feasible.
- **NFR-REL (reliability)** — A failure in any single component SHALL NOT crash the others; the
  system SHALL recover or degrade gracefully (ties to FR-DC-4, FR-DP-2).
- **NFR-AUD (auditability)** — Every action SHALL be reconstructable after the fact from stored
  data (ties to FR-DP-3).
- **NFR-SEC (security)** — Credentials used for trading SHALL be scoped to the minimum permission
  required and SHALL never grant withdrawal of funds.
- **NFR-TEST** — The system SHALL be testable, including a check that decisions never depend on
  data unavailable at decision time (no look-ahead). Safety requirements FR-SF-2 and FR-OS-2 have
  release-blocking tests.
- **NFR-PERF** — Decision latency SHALL be adequate for the chosen timeframe; the system is not
  required to compete on sub-second execution (consistent with §5 non-goals).
- **NFR-OBS (observability)** — In v1, monitoring is satisfied by notifications, inspectable
  stored data, and the periodic summary. A graphical dashboard is deferred until the system is
  proven in simulation.

---

## 9. Key domain concepts (conceptual, not a data model)

- **Observation** — a timestamped reading from one information source about one asset.
- **Signal** — a decision the engine reached at a point in time: a direction, a conviction, a
  reason, and whether it fired.
- **Trade** — the full lifecycle of one position: entry, protective exits, the exit that fired,
  and the net result.
- **Composite conviction** — the combined measure across all sources that the gate tests.
- **Edge** — the operator's measured statistical advantage, derived from the trade history; the
  private asset the open-source split protects.
- **Mode** — simulation or live; the master safety control.

*(The concrete schema and types are defined in the plan, `PLAN.md` §6 — not here.)*

---

## 10. Edge cases & error conditions

- A data source returns stale, partial, or malformed data → the system SHALL validate on intake
  and SHALL NOT act on data that fails validation.
- All three sources disagree → no signal (FR-SG-2).
- Expected gain below fee cost → no trade (FR-RM-4).
- Connectivity lost while a position is open → protective exits placed at the venue still apply
  (FR-MX-3).
- Two exits triggered near-simultaneously → exactly one close, others cancelled (FR-MX-2 accept.).
- The system restarts mid-operation → open positions and pending state are recovered from stored
  data, not lost (ties to FR-DP-1/2).
- A secret is accidentally staged for commit → prevented by FR-OS-1; if it ever reaches history,
  the credential is rotated (operational rule).

**RESOLVED:** the maximum-hold exit (FR-MX-5) **always** bounds a position; no strategy holds
indefinitely in v1. Horizon is short (hours–days), matching the short-horizon signal strategy.

---

## 11. Assumptions & dependencies

- The operator manages only their own capital and accepts all risk (ties to §5).
- Suitable free or low-cost information sources remain available; if one disappears, FR-DC-4
  limits the blast radius.
- A venue is available that accepts programmatic orders and protective exit orders.
- The operator validates extensively in simulation before any live use (a process commitment, not
  a system feature).

---

## 12. Review & acceptance checklist

Mark each only when the spec demonstrably meets it.

- [ ] Every requirement is testable (has, or can have, an objective pass/fail).
- [ ] Every requirement traces to at least one user story.
- [ ] No requirement prescribes implementation (no tech, no code, no schema).
- [ ] No speculative "might need" features are included.
- [ ] Goals have measurable success criteria (§3).
- [ ] Non-goals are explicit (§5).
- [ ] Safety requirements (FR-SF, FR-OS) have release-blocking acceptance tests.
- [x] All `[NEEDS CLARIFICATION]` markers are resolved or consciously accepted.
- [ ] Edge cases and error conditions are covered (§10).
- [ ] The plan (`PLAN.md`) and tasks (`BUILD_PLAN.md`) are consistent with this spec.

### Resolved decisions (accepted)
1. **G4** — backtest vs forward-test consistent within ±5pp win rate and ±20% net result (same sign).
2. **G5 / FR-LE-4** — verdict requires ≥ 100 closed trades (30 = weak read, <30 = noise).
3. **FR-EX-4 / FR-SG-1** — **long-or-flat, spot only.** No shorting in v1; shorting deferred to v2 (futures).
4. **FR-MX-5 / §10** — max-hold is a hard cap, default 48h; no indefinite holds.

---

## 13. Glossary

- **Maker / lower-fee order** — an order that rests and waits, incurring the lower trading fee.
- **Taker / higher-fee order** — an order that fills immediately, incurring the higher fee.
- **Round-trip cost** — combined entry + exit trading fees for one trade.
- **Drawdown** — peak-to-trough decline in account value.
- **Simulation (paper) mode** — full logic with simulated fills and no real money.
- **Thesis-invalidation exit** — closing because the reason for entering no longer holds.
- **Look-ahead** — improperly using information not available at decision time; forbidden.
