# Acceptance spec — is an implementation faithful?

Objective, tool-agnostic checks for ANY implementation of this planner —
this one, a rebuild, a port. Every numeric check is hand-computable with a
calculator; every behavior check is demonstrable live in a few clicks.
An implementation that "looks right" but fails these is not faithful.

## 1. Demand model

`weekly contacts = Members x CPM / 52 x seasonality` (CPM = ANNUAL contacts
per member — a multiplier, never a divisor; 52 is fixed, never the horizon).

- **D1.** Members flat 1,550,000, CPM 1.45, seasonality all 1.0 ->
  weekly forecast **43,221** (2,247,500 / 52 = 43,221.15).
- **D2.** Seasonality reshapes but PRESERVES the annual total: setting every
  week's index to 1.2 changes nothing; a real shape moves volume between
  weeks with the year's sum unchanged (+-1 rounding).
- **D3.** A measured-membership entry for a week REPLACES the forecast spread
  for that week: 110,000 actual against a flat 100,000 forecast -> that
  week's contacts run exactly 1.100x the untouched weeks'.

## 2. Requirements

- **R1 (workload basis).** 10,000 contacts/week, AHT 360 s, margin 5%,
  paid 40 h, shrinkage 32%, occupancy 85% ->
  hours = 10,000 x 360/3600 x 1.05 = 1,050;
  divisor = 40 x 0.68 x 0.85 = 23.12; required **45.4 FTE**.
- **R2 (Erlang basis).** Occupancy acts as a CAP on utilisation, never a
  divisor (Erlang already prices service-level idle time). The agent count is
  MINIMAL: one agent fewer misses the SL target, the returned count meets it.
- **R3 (small-queue premium).** At AHT 300 s, SL 80/40: 10 calls/hour needs
  **3** agents; 50 calls/hour needs **7**. Small queues carry a real premium
  over pure workload (~1.5x); large queues converge toward it (~1.01x).

## 3. Supply walk

- **S1.** A class of 10 with 10% training and 5% coaching attrition lands
  **8.55** graduates in production on its graduation week (a one-decimal
  display may show 8.5 or 8.6 — judge the engine value or headcount effect).
- **S2.** 28%/yr attrition on 100 heads drips ~**0.538**/week; an entered
  actual REPLACES that week's drip; an entered **0 means nobody left**; a
  BLANK fully-elapsed week also means 0 (past weeks are facts, not model);
  attrition never forward-fills.
- **S3.** Step-change columns (contacts-per-member, LOA, support counts)
  carry an edit forward to later weeks; **transfers do not** (one-time
  events).

## 4. Hiring advisor

- **H1.** With a 4-week cadence gap, no two recommended class starts are
  closer than 4 weeks (including classes already planned).
- **H2.** With "one class at a time", starts space by the class's WHOLE
  pipeline (training + nesting) length.
- **H3.** A need below the minimum class size NEVER yields a sub-minimum
  class: it folds into the previous recommended class when the max allows,
  otherwise the weeks are reported as uncoverable — visibly, never silently.

## 5. Data honesty

- **DH1.** A week whose feed values are entirely missing aggregates as
  MISSING everywhere (shown as a dash/gap), never as 0.
- **DH2.** Unreadable dates in an export are counted, warned about, and
  excluded; an export with NO readable dates is rejected, not shown empty.
- **DH3.** Weeks covering fewer days than the queue's normal operating week
  (minus any configured holiday closures) are excluded from plan-vs-actual
  comparisons WITH a visible note naming them.

## 6. Durability

- **U1.** A value typed into any grid survives: navigating to another page
  and back, publishing, and a full browser reload.
- **U2.** Two sessions of the same login are never both editors — the newer
  Take-control wins and the older session visibly downgrades.
- **U3.** A corrupted/truncated edit-lock file cannot permanently block
  editing; recovery preserves the corrupt file for diagnosis.

## Scoring

Numeric checks: exact where integer, else +-0.1. Behavior checks: pass only
if demonstrated live. All 15 pass = faithful. Suggested protocol for
evaluating a rebuild: time-box it, record the prompts/tool used, and score
against this file — results are then evidence, not impressions.
