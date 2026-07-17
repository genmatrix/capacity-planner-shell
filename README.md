# Capacity Planner — app shell

A complete, self-contained **contact-center workforce capacity planner**
(Streamlit): demand forecasting from drivers (members × contacts-per-member ×
seasonality), Erlang-C or workload staffing requirements, a supply walk with
attrition/new-hire classes/ramp, a hiring advisor with real training-calendar
constraints, budget rollups, scenario compare, and file-based multi-planner
collaboration (single-writer lock, immutable published versions) — no server,
no database.

**This shell ships with no data of any kind.** No sample exports, no mapping,
no configuration, no plan history. It has never contained real operational
data — the planning model is driven entirely by what you enter.

## Run it

1. Install Python 3.12–3.14 (per-user install is fine).
2. Double-click `Launch Capacity Planner.bat` (Windows — builds its own
   environment), or: `pip install -r requirements.txt` then
   `streamlit run capacity_planner.py`.

## First boot

With no skill mapping present, the app **self-demos**: three synthetic sample
lines of business with generated numbers, so every page shows itself.

To plan real queues: put a `Skill_Mapping.csv` (columns: `Skill_ID`,
`Line_of_Business`, `Queue_Name`) next to `capacity_planner.py` and relaunch —
you get blank scaffolds named for your lines. Point the 🔌 Real Data page at
your WFM/ACD export locations to load actuals; any vendor's export works via
the built-in column mapper. Shared team state (published plan versions, the
edit lock, drafts) is created in a `scenarios/` folder on first use — put the
app folder on a network share and the whole team plans against one version
history.

## Is an implementation faithful?

`ACCEPTANCE.md` defines this app's behavior as 15 objective checks — every
number hand-computable, every behavior demonstrable in a few clicks. Any
rebuild or port can be scored against it.

## Docs

`TEAM_SETUP.md` covers per-PC setup, offline installs, and troubleshooting.
The in-app **📖 Guide** page carries task-by-task recipes and a live weekly
checklist.
