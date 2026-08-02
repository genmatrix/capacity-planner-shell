import json
import os
import re
import time
from io import StringIO
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

import sources as sx
import collab
import brand

# Tab title follows the local wordmark. brand.identity() reads a plain file and
# never raises, so it is safe this early — set_page_config must be the first
# Streamlit call and cannot be retried.
pd.set_option("mode.string_storage", "python")
st.set_page_config(page_title=f"{brand.identity()['word']} Capacity Planner",
                   layout="wide")
brand.inject()

# Where the shared team plan lives. Point this at the WFM network share so all
# planners read/write the same active.json + snapshots, e.g.
#   SCENARIO_DIR = Path(r"\\your-share\WFM\CapacityPlanner\scenarios")
SCENARIO_DIR = Path("scenarios")
if (Path(__file__).parent / "demo.flag").exists():
    if "_demo_scen_dir" not in st.session_state:
        import tempfile
        st.session_state["_demo_scen_dir"] = Path(
            tempfile.mkdtemp(prefix="demo_scen_"))
    SCENARIO_DIR = st.session_state["_demo_scen_dir"]
CONSOLIDATED = "— Consolidated (all LOBs) —"

# One tick per script run — stable_editor uses it to notice a grid that
# skipped a run (page navigation destroys the widget's edit state; the base
# must then re-seed from the stored frame or the planner's edits are erased).
st.session_state["_run_seq"] = st.session_state.get("_run_seq", 0) + 1



# ----------------------------------------------------------------------
# Snapshot (de)serialization — the plan's business data as a plain dict.
# Collaboration mechanics (lock / pointer / versioning) live in collab.py.
# ----------------------------------------------------------------------
def _serialize_lobs() -> dict:
    """The current working plan as a JSON-ready payload (no collab metadata)."""
    lobs_payload = {}
    for lob, d in st.session_state.lobs.items():
        lobs_payload[lob] = {
            "demand": d["demand"].to_json(orient="split"),
            "roster": d["roster"].to_json(orient="split"),
            "nh": d["nh"].to_json(orient="split"),
            "assumptions": d["assumptions"],
        }
    _ma = st.session_state.get("members_actual")
    return {
        "n_weeks": st.session_state.n_weeks,
        "plan_year": plan_year(),
        "members_start": float(st.session_state.get("members_start", 0.0) or 0.0),
        "members_end": float(st.session_state.get("members_end", 0.0) or 0.0),
        "members_actual": ([None if v is None or (isinstance(v, float) and np.isnan(v))
                            else float(v) for v in _ma]
                           if _ma is not None else None),
        "lobs": lobs_payload,
    }


def _payload_lobs(p: dict) -> dict:
    """Parse a snapshot payload's LOB frames into the {lob: {demand, roster, nh,
    assumptions}} shape compute_plan takes, applying every legacy-column
    migration. Pure — never touches session state — so read-only consumers
    (scenario compare) share ONE migration path with plan loading."""
    lobs = {}
    for lob, d in p["lobs"].items():
        dem = pd.read_json(StringIO(d["demand"]), orient="split")
        if "Seasonality" not in dem.columns:   # plan saved before seasonality existed
            dem.insert(dem.columns.get_loc("CPM") + 1, "Seasonality", 1.0)
        if "Members (actual)" not in dem.columns:   # pre-actual-members plans
            dem.insert(dem.columns.get_loc("Members") + 1, "Members (actual)", np.nan)
        ros = pd.read_json(StringIO(d["roster"]), orient="split")
        for _col, _legacy in (("Supervisors", "supervisors"), ("Leads/Project", "leads")):
            if _col not in ros.columns:   # plan saved before support columns existed
                ros[_col] = float(d["assumptions"].get(_legacy, 0.0) or 0.0)
        if "Attrition (actual)" not in ros.columns:   # pre-actualized-attrition plans
            ros["Attrition (actual)"] = np.nan
        if "Mentors" not in ros.columns:              # pre-mentors plans
            ros["Mentors"] = 0.0
        _nh = pd.read_json(StringIO(d["nh"]), orient="split")
        if "Actual Grads" not in _nh.columns:         # pre-actualized-grads plans
            _nh["Actual Grads"] = np.nan
        # JSON round-trips rebuild indexes as Int64/object, NOT RangeIndex. On
        # a num_rows="dynamic" editor that is fatal: Streamlit shows the index
        # as a required editable column and NEVER COMMITS a typed row until it
        # is filled — the planner's new NH class stayed a client-side phantom
        # and vanished on navigation (diagnosed live 2026-07-14). Grid rows
        # are positional; the index carries nothing — normalize it at load.
        lobs[lob] = {
            "demand": dem.reset_index(drop=True),
            "roster": ros.reset_index(drop=True),
            "nh": _nh.reset_index(drop=True),
            "assumptions": d["assumptions"],
        }
    return lobs


# ---- Change log: field-level diffs between published versions ---------------
# Manager ask 2026-08-01: track every field change, who made it, and why.
#
# The first two are COMPUTED, not collected. Every published version stores the
# COMPLETE plan, so "what changed between v7 and v8" is a diff of two files —
# no planner types anything, and it works retroactively on versions published
# before this page existed. Attribution needs no per-edit tracking either: the
# single-writer lock means everything between two versions was done by whoever
# published the later one.
#
# WHY is the one part no system can derive, and it is captured PER PUBLISH
# rather than per field (user decision 2026-08-01, taking the recommendation).
# Twenty edits made for one reason produce one honest sentence; demanding
# twenty justifications produces twenty copies of "updated" — an audit trail
# that looks complete and says nothing. The publish note is REQUIRED for that
# reason; see render_publish_panel.
#
# Everything here is PURE (no st.*) so the diff can be asserted directly from a
# check script without booting the app.

ASSUMPTION_LABELS = {
    "starting_hc": "Starting headcount",
    "annual_attrition_pct": "Attrition %/yr",
    "shrinkage_pct": "Shrinkage %",
    "occupancy_pct": "Target occupancy %",
    "paid_hours_per_week": "Paid hours/week",
    "workload_margin_pct": "Workload margin %",
    "ft_pct": "Full-time %",
    "req_basis": "Requirement basis",
    "sl_target_pct": "Service level target %",
    "sl_threshold_sec": "SL threshold (sec)",
    "open_hrs_week": "Open hours/week",
    "ramp_weeks": "NH ramp weeks",
    "ramp_start_pct": "NH ramp start %",
    "transfer_ramp_weeks": "Transfer ramp weeks",
    "transfer_ramp_start_pct": "Transfer ramp start %",
    "class_gap_weeks": "Hiring cadence gap (weeks)",
    "class_min_size": "Min class size",
    "class_max_size": "Max class size",
    "one_class_at_a_time": "One class at a time",
    "members_start": "Members — start",
    "members_end": "Members — year-end",
    "supervisors": "Supervisors (legacy flat)",
    "leads": "Leads/Project (legacy flat)",
}

DIFF_COLS = ["LOB", "Area", "Field", "Week", "Was", "Now"]


def _is_blank(v) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and np.isnan(v):
        return True
    return isinstance(v, str) and not v.strip()


def _cells_equal(a, b) -> bool:
    """Blank-insensitive, float-noise-tolerant cell comparison. A JSON round
    trip can turn 5 into 5.0 and None into NaN; neither is a planner edit, and
    a change log full of those is a change log nobody reads."""
    if _is_blank(a) and _is_blank(b):
        return True
    if _is_blank(a) or _is_blank(b):
        return False
    try:
        return bool(abs(float(a) - float(b)) < 5e-7)
    except (TypeError, ValueError):
        return str(a) == str(b)


def _fmt_cell(v) -> str:
    """Display form: blank for empty, thousands-separated, no fake precision."""
    if _is_blank(v):
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(f - round(f)) < 5e-7:
        return f"{int(round(f)):,}"
    return f"{f:,.2f}"


def _diff_grid(lob: str, area: str, o: pd.DataFrame, n: pd.DataFrame) -> list[dict]:
    """Cell-level diff of two weekly grids, aligned on the Week column."""
    out = []
    keyed = ("Week" in o.columns and "Week" in n.columns
             and o["Week"].is_unique and n["Week"].is_unique)
    ow = o.set_index("Week") if keyed else o.reset_index(drop=True)
    nw = n.set_index("Week") if keyed else n.reset_index(drop=True)
    for c in nw.columns:
        if c not in ow.columns:
            out.append({"LOB": lob, "Area": area, "Field": c, "Week": "",
                        "Was": "", "Now": "(column added)"})
    for c in ow.columns:
        if c not in nw.columns:
            out.append({"LOB": lob, "Area": area, "Field": c, "Week": "",
                        "Was": "(column removed)", "Now": ""})
    shared = [c for c in nw.columns if c in ow.columns]
    for wk in nw.index:
        if wk not in ow.index:
            out.append({"LOB": lob, "Area": area, "Field": "(row)",
                        "Week": str(wk), "Was": "", "Now": "(week added)"})
            continue
        for c in shared:
            a, b = ow.at[wk, c], nw.at[wk, c]
            if not _cells_equal(a, b):
                out.append({"LOB": lob, "Area": area, "Field": c,
                            "Week": str(wk) if keyed else "",
                            "Was": _fmt_cell(a), "Now": _fmt_cell(b)})
    return out


def _diff_nh(lob: str, o: pd.DataFrame, n: pd.DataFrame) -> list[dict]:
    """New-hire classes have no stable key — they are rows a planner adds and
    deletes. Group by start week (the one thing that identifies a class to a
    human), then compare within the week and report the count difference as an
    added/removed class rather than as a wall of shifted cells."""
    out, area = [], "New-hire classes"

    def by_week(df):
        g = {}
        for _, r in df.iterrows():
            g.setdefault(str(r.get("Class Start Week", "")), []).append(r)
        return g

    og, ng = by_week(o), by_week(n)
    fields = [c for c in n.columns if c != "Class Start Week"]
    for wk in sorted(set(og) | set(ng)):
        orows, nrows = og.get(wk, []), ng.get(wk, [])
        for i in range(max(len(orows), len(nrows))):
            if i >= len(orows):
                size = _fmt_cell(nrows[i].get("Class Size"))
                out.append({"LOB": lob, "Area": area, "Field": "Class",
                            "Week": wk, "Was": "", "Now": f"added — {size} seats"})
                continue
            if i >= len(nrows):
                size = _fmt_cell(orows[i].get("Class Size"))
                out.append({"LOB": lob, "Area": area, "Field": "Class",
                            "Week": wk, "Was": f"removed — {size} seats", "Now": ""})
                continue
            for c in fields:
                a, b = orows[i].get(c), nrows[i].get(c)
                if not _cells_equal(a, b):
                    out.append({"LOB": lob, "Area": area, "Field": c, "Week": wk,
                                "Was": _fmt_cell(a), "Now": _fmt_cell(b)})
    return out


def _diff_assumptions(lob: str, o: dict, n: dict) -> list[dict]:
    out = []
    for k in sorted(set(o) | set(n)):
        a, b = o.get(k), n.get(k)
        if _cells_equal(a, b):
            continue
        out.append({"LOB": lob, "Area": "Assumptions",
                    "Field": ASSUMPTION_LABELS.get(k, k.replace("_", " ").capitalize()),
                    "Week": "", "Was": _fmt_cell(a), "Now": _fmt_cell(b)})
    return out


def diff_payloads(old: dict, new: dict) -> list[dict]:
    """Every field-level difference between two published snapshots.

    Both sides go through `_payload_lobs`, so legacy-column migrations are
    applied to BOTH before comparing — otherwise every column added since the
    older version would read as a planner edit on the day the code shipped."""
    rows: list[dict] = []
    for key, label in (("n_weeks", "Planning horizon (weeks)"),
                       ("plan_year", "Plan year"),
                       ("members_start", "Members — start"),
                       ("members_end", "Members — year-end")):
        if not _cells_equal(old.get(key), new.get(key)):
            rows.append({"LOB": "(plan)", "Area": "Plan", "Field": label,
                         "Week": "", "Was": _fmt_cell(old.get(key)),
                         "Now": _fmt_cell(new.get(key))})
    oa, na = old.get("members_actual") or [], new.get("members_actual") or []
    moved = sum(1 for i in range(max(len(oa), len(na)))
                if not _cells_equal(oa[i] if i < len(oa) else None,
                                    na[i] if i < len(na) else None))
    if moved:
        rows.append({"LOB": "(plan)", "Area": "Plan", "Field": "Members (actual)",
                     "Week": "", "Was": "", "Now": f"{moved} week(s) recorded/changed"})

    old_lobs, new_lobs = _payload_lobs(old), _payload_lobs(new)
    for lob in sorted(set(old_lobs) | set(new_lobs)):
        if lob not in old_lobs:
            rows.append({"LOB": lob, "Area": "Plan", "Field": "Line of business",
                         "Week": "", "Was": "", "Now": "added"})
            continue
        if lob not in new_lobs:
            rows.append({"LOB": lob, "Area": "Plan", "Field": "Line of business",
                         "Week": "", "Was": "removed", "Now": ""})
            continue
        o, n = old_lobs[lob], new_lobs[lob]
        rows += _diff_grid(lob, "Demand", o["demand"], n["demand"])
        rows += _diff_grid(lob, "Roster", o["roster"], n["roster"])
        rows += _diff_nh(lob, o["nh"], n["nh"])
        rows += _diff_assumptions(lob, o["assumptions"], n["assumptions"])
    return rows


def _apply_payload(p: dict):
    """Load a snapshot payload into session as the working plan."""
    st.session_state.n_weeks = p["n_weeks"]
    st.session_state.plan_year = int(p.get("plan_year", DEFAULT_PLAN_YEAR))
    _ma = p.get("members_actual")
    st.session_state["members_actual"] = (
        np.array([np.nan if v is None else float(v) for v in _ma], dtype=float)
        if _ma else None)
    st.session_state.members_start = p.get("members_start")
    st.session_state.members_end = p.get("members_end")
    st.session_state.lobs = _payload_lobs(p)
    _purge_assumption_widgets()
    if st.session_state.members_start is None or st.session_state.members_end is None:
        first = next(iter(st.session_state.lobs.values()), None)
        if first is not None:
            m = first["demand"]["Members"]
            st.session_state.members_start = float(m.iloc[0])
            st.session_state.members_end = float(m.iloc[-1])


def _plan_year() -> int:
    """The plan year this session is working in.

    Every pointer and lock is scoped to a year (collab.active_path /
    lock_path), so this is what all collab calls must be given. Defaults
    defensively: the draft helpers run before init_state has necessarily
    written plan_year."""
    return int(st.session_state.get("plan_year", DEFAULT_PLAN_YEAR))


def _draft_path(year=None) -> Path:
    y = _plan_year() if year is None else int(year)
    return SCENARIO_DIR / "drafts" / f"{st.session_state.user}-{y}.json"


def _live_draft_path() -> Path:
    """The draft file to READ. Falls back to the pre-per-year name (no year
    suffix) for the legacy year, so the first launch after the update still
    offers a resume instead of silently abandoning someone's work."""
    p = _draft_path()
    if not p.exists() and _plan_year() == DEFAULT_PLAN_YEAR:
        legacy = SCENARIO_DIR / "drafts" / f"{st.session_state.user}.json"
        if legacy.exists():
            return legacy
    return p


def _autosave_draft():
    """Trust net for spreadsheet-minded planners: the working plan auto-saves to a
    per-user draft file on every change, so a closed browser tab never loses
    work. Runs at the end of every rerun; a failed write must never take the
    app down (share offline → planner keeps working, just unprotected)."""
    if "lobs" not in st.session_state:
        return
    try:
        payload = _serialize_lobs()
        blob = json.dumps(payload, sort_keys=True)
        if st.session_state.get("_draft_blob") == blob:
            return
        _draft_path().parent.mkdir(parents=True, exist_ok=True)
        collab._atomic_write(_draft_path(), json.dumps({
            "user": st.session_state.user,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "sandbox": bool(st.session_state.get("sandbox")),
            "base_version": st.session_state.get("loaded_version"),
            "payload": payload,
        }))
        st.session_state["_draft_blob"] = blob
    except OSError:
        pass


def _startup_draft_check():
    """If a previous session left a draft that differs from the freshly loaded
    plan, offer Resume/Discard (sidebar banner). A draft matching the loaded
    plan means everything was published — silently clean it up."""
    try:
        src = _live_draft_path()
        d = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    payload = d.get("payload", {})
    keys = ("n_weeks", "plan_year", "members_start", "members_end", "lobs")
    # Compare against the ACTIVE SNAPSHOT FILE, not a re-serialized session:
    # both draft and snapshot were written from live frames, so they're
    # string-comparable; frames reloaded from JSON re-serialize with dtype
    # drift and would false-positive every boot.
    act = collab.read_active(SCENARIO_DIR, _plan_year())
    snap = collab.load_snapshot(SCENARIO_DIR, act["file"]) if act else None
    if snap is not None:
        ref = {k: snap.get(k) for k in keys if k in snap}
    else:   # nothing published yet — compare to the freshly built session
        ref = {k: v for k, v in _serialize_lobs().items() if k in keys}
    mine = {k: payload.get(k) for k in keys if k in payload}
    if json.dumps(mine, sort_keys=True, default=str) == json.dumps(ref, sort_keys=True, default=str):
        src.unlink(missing_ok=True)   # everything was published — clean up
        return
    st.session_state["_draft_pending"] = d
    st.session_state["_draft_file"] = str(src)   # discard must delete what we READ


def _purge_assumption_widgets():
    """Sidebar assumption widgets are keyed ('as_*') so their identity is
    stable across reruns — without keys, Streamlit derives widget identity
    from the default value, and a default that the widget itself updates
    means every other edit is sent under a stale identity and dropped (the
    'takes two tries' bug). The cost of stable keys: after a plan load or
    replacement the keys still hold pre-load values, so drop them and let
    the widgets re-seed from the loaded assumptions."""
    for k in list(st.session_state.keys()):
        if isinstance(k, str) and k.startswith("as_"):
            del st.session_state[k]


def _available_years() -> list[int]:
    """Plan years this session can switch between.

    Every year with a published version, plus the year the working plan is on,
    plus any year this user has an auto-saved draft for. That last one matters:
    roll into 2027, don't publish yet, switch to 2026 to answer a question —
    without it 2027 would vanish from the list and the only way back to that
    work would be the resume prompt on the next boot."""
    yrs = {int(j.get("plan_year", DEFAULT_PLAN_YEAR))
           for j in collab.changelog(SCENARIO_DIR)}
    yrs.add(_plan_year())
    try:
        user = st.session_state.user
        for p in (SCENARIO_DIR / "drafts").glob(f"{user}-*.json"):
            tail = p.stem.rsplit("-", 1)[-1]
            if tail.isdigit():
                yrs.add(int(tail))
    except OSError:
        pass
    return sorted(yrs)


def _switch_year(year: int) -> None:
    """Move this session to another plan year.

    Nothing is at risk: drafts are per-year, so the year you leave keeps its own
    auto-saved draft and offers to resume it when you come back.

    The lock token is NOT dropped. Tokens are stored per year
    (`lock_token_<year>`) precisely so this session can hold 2026 and 2027 at
    once — dropping a single shared token on every switch meant returning to a
    year you had locked left you tokenless, `owns_lock` failed, and the app
    announced that you had been "taken over by" yourself (reported 2026-07-29)."""
    st.session_state.plan_year = int(year)
    _purge_assumption_widgets()
    if not _load_active_into_session():
        # Nothing published for that year yet (a fresh rollover). Keep the
        # frames on screen — they ARE that year's working plan.
        st.session_state.loaded_version = None
    # That year may hold unpublished work: drafts are per-year, so the year you
    # left kept its own. Offer it HERE rather than only at the next boot —
    # someone who added classes, rolled over, and came back found the year
    # apparently empty with no hint the work still existed (field report
    # 2026-07-30). Same Resume/Discard banner the startup check raises.
    st.session_state.pop("_draft_pending", None)
    _startup_draft_check()
    st.rerun()


def _load_active_into_session():
    """Point the working plan at the current shared active version (if any)."""
    act = collab.read_active(SCENARIO_DIR, _plan_year())
    snap = collab.load_snapshot(SCENARIO_DIR, act["file"]) if act else None
    if snap:
        _apply_payload(snap)
        st.session_state.loaded_version = act["version"]
        return True
    return False


# ----------------------------------------------------------------------
# Dummy-data initialization
# ----------------------------------------------------------------------
DEFAULT_PLAN_YEAR = 2026  # first year the team planned in the app


def plan_year() -> int:
    return int(st.session_state.get("plan_year", DEFAULT_PLAN_YEAR))
WEEKS_PER_YEAR = 52  # annual Members×CPM ÷ 52 → weekly volume (fixed, not the horizon)


def week_starts(n: int, year: int | None = None):
    # Anchor to the Monday of the week containing Jan 1 — same rule sources.py
    # uses to bucket ACD rows, so real weeks line up with plan weeks.
    jan1 = date(year if year is not None else plan_year(), 1, 1)
    anchor = jan1 - timedelta(days=jan1.weekday())
    return [anchor + timedelta(weeks=i) for i in range(n)]


def make_lob(n_weeks: int, seed: int, members0: float, cpm: float,
             aht: float, hc: float) -> dict:
    """Create one LOB's input tables with slight variation per seed."""
    weeks = [w.isoformat() for w in week_starts(n_weeks)]
    rng = np.random.default_rng(seed)
    # Placeholder Members column; the real value is the org-wide member base
    # written by apply_global_members() before compute (only CPM differs per LOB).
    members_start = float(members0)
    members_end = float(round(members0 * 1.02))
    members = np.round(np.linspace(members_start, members_end, n_weeks), 0)

    demand = pd.DataFrame(
        {
            "Week": weeks,
            "Members": members,
            "Members (actual)": [np.nan] * n_weeks,   # blank = use the forecast
            # CPM = Calls Per Member (annual): weekly calls = Members × CPM ÷ 52.
            "CPM": np.round(rng.normal(cpm, cpm * 0.02, n_weeks), 2),
            # Seasonality index (1.0 = average week) reshapes the flat spread.
            "Seasonality": [1.0] * n_weeks,
            "Fcst Override": [np.nan] * n_weeks,
            "AHT (sec)": np.round(rng.normal(aht, 6, n_weeks), 0),
        }
    )
    roster = pd.DataFrame(
        {"Week": weeks, "LOA": [round(hc * 0.04, 1)] * n_weeks,
         "Mentors": [0.0] * n_weeks,
         "Transfers +/-": [0.0] * n_weeks,
         "Attrition (actual)": [np.nan] * n_weeks,
         "Supervisors": [float(max(1, round(hc / 15)))] * n_weeks,
         "Leads/Project": [float(max(1, round(hc / 20)))] * n_weeks}
    )
    nh = pd.DataFrame(
        {
            "Class Start Week": [weeks[2]],
            "Class Size": [max(6, round(hc * 0.12))],
            "Training Wks": [4],
            "Coaching Wks": [2],
            "Training Attr %": [8.0],
            "Coaching Attr %": [4.0],
            "Actual Grads": [np.nan],   # blank = use stage attrition
        }
    )
    assumptions = {
        "starting_hc": hc,
        "annual_attrition_pct": 28.0,
        "shrinkage_pct": 32.0,
        "occupancy_pct": 85.0,
        "paid_hours_per_week": 40.0,
        "workload_margin_pct": 5.0,
        "ft_pct": 82.0,
        "req_basis": "workload", "sl_target_pct": 80.0,
        "sl_threshold_sec": 40.0, "open_hrs_week": 60.0,
        "ramp_weeks": 0, "ramp_start_pct": 60.0,
        "transfer_ramp_weeks": 2, "transfer_ramp_start_pct": 75.0,
        "class_gap_weeks": 4, "class_min_size": 1, "class_max_size": 12,
        "one_class_at_a_time": False,
        "members_start": members_start,
        "members_end": members_end,
    }
    return {"demand": demand, "roster": roster, "nh": nh, "assumptions": assumptions}


def make_blank_lob(n_weeks: int, aht: float = 400.0) -> dict:
    """A real LOB scaffold: zeroed demand for the planner to fill with actual
    Members / contacts-per-member. AHT can be pre-seeded from measured data."""
    weeks = [w.isoformat() for w in week_starts(n_weeks)]
    demand = pd.DataFrame({
        "Week": weeks, "Members": [0.0] * n_weeks,
        "Members (actual)": [np.nan] * n_weeks,   # blank = use the forecast
        "CPM": [0.0] * n_weeks,
        "Seasonality": [1.0] * n_weeks,
        "Fcst Override": [np.nan] * n_weeks, "AHT (sec)": [float(aht)] * n_weeks,
    })
    roster = pd.DataFrame(
        {"Week": weeks, "LOA": [0.0] * n_weeks, "Mentors": [0.0] * n_weeks,
         "Transfers +/-": [0.0] * n_weeks,
         "Attrition (actual)": [np.nan] * n_weeks,
         "Supervisors": [0.0] * n_weeks, "Leads/Project": [0.0] * n_weeks})
    nh = pd.DataFrame({
        "Class Start Week": pd.Series(dtype="object"),
        "Class Size": pd.Series(dtype="float"), "Training Wks": pd.Series(dtype="float"),
        "Coaching Wks": pd.Series(dtype="float"), "Training Attr %": pd.Series(dtype="float"),
        "Coaching Attr %": pd.Series(dtype="float"),
        "Actual Grads": pd.Series(dtype="float"),   # blank = use stage attrition
    })
    assumptions = {
        "starting_hc": 0.0, "annual_attrition_pct": 28.0, "shrinkage_pct": 32.0,
        "occupancy_pct": 85.0, "paid_hours_per_week": 40.0, "workload_margin_pct": 5.0,
        "ft_pct": 82.0, "req_basis": "workload", "sl_target_pct": 80.0,
        "sl_threshold_sec": 40.0, "open_hrs_week": 60.0,
        "ramp_weeks": 0, "ramp_start_pct": 60.0,
        "transfer_ramp_weeks": 2, "transfer_ramp_start_pct": 75.0,
        "class_gap_weeks": 4, "class_min_size": 1, "class_max_size": 12,
        "one_class_at_a_time": False,
        "members_start": 0.0, "members_end": 0.0,
    }
    return {"demand": demand, "roster": roster, "nh": nh, "assumptions": assumptions}


def _mapped_blank_lobs(n_weeks: int) -> dict | None:
    """Blank scaffolds named for the LOBs in Skill_Mapping.csv (app folder),
    seeding AHT from ACD measured AHT where the export is present. Returns
    None if the mapping file is absent or unreadable (caller falls back to demo)."""
    mp_path = APP_DIR / "Skill_Mapping.csv"
    if not mp_path.exists():
        return None
    # Through the parse cache (defined later in the module; resolved at call
    # time from the boot block) — this seeding read must never re-parse an
    # export the session already paid for (2026-07-15).
    _msig = _files_sig([mp_path])
    mapping, rep = _cached_mapping(
        _msig, json.dumps(load_field_maps().get("mapping") or {}))
    if mapping is None:
        return None
    meas_aht: dict = {}
    s_path = APP_DIR / "split.csv"
    if s_path.exists():
        try:
            sdf, r, _secs, _mb = _cached_feed_raw(
                "acd", _files_sig([s_path]), _msig,
                json.dumps(load_field_maps().get("acd") or {}), mapping)
            if r.ok:
                _sw = sx.split_weekly(sdf)
                if "Actual AHT (sec)" in _sw.columns:
                    meas_aht = (_sw.groupby("LOB")["Actual AHT (sec)"]
                                .mean().dropna().round().to_dict())
        except Exception:  # noqa: BLE001 — seeding is best-effort
            meas_aht = {}
    return {lob: make_blank_lob(n_weeks, float(meas_aht.get(lob, 400.0)))
            for lob in mapping.lobs}


def init_state(n_weeks: int):
    st.session_state.plan_year = plan_year()   # keep year across horizon changes
    st.session_state.n_weeks = n_weeks
    mapped = _mapped_blank_lobs(n_weeks)
    if mapped:
        # Real mapped LOBs, blank for the planner to fill with CPM per LOB.
        st.session_state.lobs = mapped
        st.session_state.members_start = 0.0
        st.session_state.members_end = 0.0
    else:
        # No mapping file present — fall back to the demo LOBs.
        st.session_state.lobs = {
            "Customer Support": make_lob(n_weeks, 7, 380_000, 2.10, 430, 89.0),
            "Lending": make_lob(n_weeks, 11, 150_000, 0.42, 540, 19.0),
            "Digital Support": make_lob(n_weeks, 13, 210_000, 0.77, 370, 29.0),
        }
        # demo: CS hiring pipeline starts EMPTY so attrition erosion is not
        # silently backfilled — the Hiring Advisor recommends the fix live.
        st.session_state.lobs["Customer Support"]["nh"] = (
            st.session_state.lobs["Customer Support"]["nh"].iloc[0:0])
        # One org-wide member base shared by every LOB (only CPM differs).
        st.session_state.members_start = 380_000.0
        st.session_state.members_end = 387_600.0


# ----------------------------------------------------------------------
# The calculation engine (this is what the scheduled job would run)
# ----------------------------------------------------------------------
def nh_production_adds(weeks: list[str], nh: pd.DataFrame) -> np.ndarray:
    """Production adds per week from new-hire classes. `Actual Grads`, when
    filled, REPLACES the modelled survivor calculation for that class (same
    rule as roster `Attrition (actual)`): a class that started 10 and graduated
    7 lands 7, whatever the stage-attrition assumptions predicted."""
    adds = np.zeros(len(weeks))
    idx = {w: i for i, w in enumerate(weeks)}

    def _num(v, default=0.0):
        """A row the planner is still typing has blank cells. Coerce, don't
        crash: a half-entered class simply contributes nothing until it has a
        size. (Adding a class row and picking only its start week used to raise
        ValueError: cannot convert float NaN to integer — 2026-07-13.)"""
        x = pd.to_numeric(v, errors="coerce")
        return float(default) if pd.isna(x) else float(x)

    for _, r in nh.dropna(subset=["Class Start Week"]).iterrows():
        if r["Class Start Week"] not in idx:
            continue
        size = _num(r.get("Class Size"))
        if size <= 0:
            continue                      # row not filled in yet — nothing to add
        actual = pd.to_numeric(r.get("Actual Grads"), errors="coerce")
        if pd.notna(actual):
            survived = float(actual)      # actuals replace the modelled survivors
        else:
            survived = (size
                        * (1 - _num(r.get("Training Attr %")) / 100)
                        * (1 - _num(r.get("Coaching Attr %")) / 100))
        grad_wk = (idx[r["Class Start Week"]]
                   + int(_num(r.get("Training Wks")))
                   + int(_num(r.get("Coaching Wks"))))
        if 0 <= grad_wk < len(weeks):
            adds[grad_wk] += survived
    return adds


def apply_global_members() -> None:
    """Membership is org-wide: one start (actual) → year-end (forecast) spread,
    shared by every LOB. Write it into each LOB's Members column so per-LOB CPM
    is the only demand differentiator between lines of business.

    "Members (actual)" — the weekly measured membership — is org-wide too: it is
    entered in one LOB's grid and mirrored into every LOB here, so the model
    always sees ONE membership history."""
    n = st.session_state.n_weeks
    ms = float(st.session_state.get("members_start", 0.0) or 0.0)
    me = float(st.session_state.get("members_end", 0.0) or 0.0)
    members = np.round(np.linspace(ms, me, n), 0)
    actual = st.session_state.get("members_actual")
    if actual is None or len(actual) != n:
        actual = np.full(n, np.nan)
    for d in st.session_state.lobs.values():
        dem = d["demand"]
        if len(dem) == n:
            dem["Members"] = members
            dem["Members (actual)"] = actual
    st.session_state["members_actual"] = actual


def capture_members_actual(edited: pd.DataFrame) -> bool:
    """Pull the org-wide actual-membership series out of whichever LOB grid was
    edited. True when it changed (caller reruns so every LOB shows the same
    series)."""
    if "Members (actual)" not in edited.columns:
        return False
    new = pd.to_numeric(edited["Members (actual)"], errors="coerce").to_numpy(dtype=float)
    cur = st.session_state.get("members_actual")
    if cur is not None and len(cur) == len(new) and np.array_equal(
            np.nan_to_num(cur, nan=-1.0), np.nan_to_num(new, nan=-1.0)):
        return False
    st.session_state["members_actual"] = new
    return True


def forward_fill_step(prev: pd.DataFrame, edited: pd.DataFrame, col: str) -> bool:
    """Step-change columns (CPM, LOA): editing a week carries the new value
    forward to every later week, until a later week is itself edited. Compares
    the edited grid to its pre-edit state, forward-fills from each changed cell
    (mutating `edited`), and returns True if anything changed (caller reruns to
    redraw the grid)."""
    if col not in edited.columns or len(prev) != len(edited):
        return False
    oc = pd.to_numeric(prev[col], errors="coerce").to_numpy(dtype=float)
    nc = pd.to_numeric(edited[col], errors="coerce").to_numpy(dtype=float)
    changed = np.where(~np.isclose(oc, nc, equal_nan=True))[0]
    if len(changed) == 0:
        return False
    filled = nc.copy()
    for i in sorted(changed):  # later edits win the tail they overlap
        filled[i:] = nc[i]
    edited[col] = filled
    return True


def stable_editor(state_df: pd.DataFrame, *, state_key: str, **editor_kwargs
                  ) -> pd.DataFrame:
    """st.data_editor wrapper that keeps the editor's INPUT frame stable across
    reruns, so edits register on the first click and the grid keeps its scroll
    position. The naive persistence pattern (`stored = st.data_editor(stored)`)
    feeds each edit back as next run's input — the widget's data hash changes,
    Streamlit rebuilds the editor, the next click is dropped, and the grid
    snaps back to the top-left.

    Here the input only changes when the stored frame diverges from what the
    editor last returned (compared against a saved COPY, so in-place external
    mutations — forward-fill, seasonality generate, plan load, rollover — are
    all caught); then the base refreshes and the widget key rotates to force a
    deliberate redraw.

    A DEAD WIDGET is external too (fix 2026-07-14): navigating to another page
    garbage-collects the editor's client-side edit state — the diffs that
    re-apply the planner's edits over the stale base. Returning with the old
    base would ERASE the stored edits (an added NH class vanished after a trip
    to the Executive View; AHT/Transfers/Actual-cell edits reverted the same
    way — only forward-fill columns survived, because their mutation forced a
    refresh). Liveness is tracked with the module's per-run sequence counter
    (`_run_seq`, ticked once at the top of every script run): an editor that
    did NOT render on the immediately previous run was destroyed, so the base
    re-seeds from the stored frame. Same-page reruns render every run, so this
    never rotates mid-edit — the two-clicks fix is untouched. (Deliberately
    NOT keyed off the widget's own session-state entry: its presence timing is
    a Streamlit internal and differs under AppTest.)"""
    base_key = f"_ed_base_{state_key}"
    last_key = f"_ed_last_{state_key}"
    gen_key = f"_ed_gen_{state_key}"
    run_key = f"_ed_run_{state_key}"
    seq = st.session_state.get("_run_seq", 0)
    rendered_last_run = st.session_state.get(run_key) == seq - 1
    external = (base_key not in st.session_state
                or last_key not in st.session_state
                or not rendered_last_run          # dead widget: navigated away
                or not state_df.equals(st.session_state[last_key]))
    if external:
        # reset_index: a non-range index on a dynamic editor makes Streamlit
        # demand an index value per new row and silently discard typed rows
        # until one is entered (2026-07-14). Grid rows are positional — never
        # let a snapshot/concat index reach the widget.
        st.session_state[base_key] = state_df.reset_index(drop=True)
        st.session_state[gen_key] = st.session_state.get(gen_key, 0) + 1
    st.session_state[run_key] = seq
    edited = st.data_editor(st.session_state[base_key],
                            key=f"{state_key}__g{st.session_state[gen_key]}",
                            **editor_kwargs)
    st.session_state[last_key] = edited.copy()
    return edited


def week_of_month(iso_week: str) -> int:
    """Which week of the calendar month this Monday-anchored plan week falls in
    (1–5), by the day-of-month of its Monday. Days 1–7 → week 1, 8–14 → 2, …"""
    return (date.fromisoformat(iso_week).day - 1) // 7 + 1


def wom_seasonality(weeks: list[str], uplift: dict[int, float]) -> np.ndarray:
    """Build a weekly Seasonality index from a week-of-month profile: each week
    gets 1 + uplift%/100 for its WoM position (missing positions → 1.0). The
    engine normalizes the column afterward, so these are RELATIVE lifts (a peak
    week pulls volume from the rest; the annual total is preserved)."""
    return np.array([1.0 + uplift.get(week_of_month(w), 0.0) / 100.0 for w in weeks])


def derive_seasonality_from_history(bench: pd.DataFrame | None, lob: str,
                                    weeks: list[str], min_weeks: int = 8):
    """Derive a weekly Seasonality index from ACD actual contacts.

    Each full history week's volume is bucketed by ISO week-of-year (averaging
    across years when more than one is loaded) and divided by the overall mean,
    so 1.0 = an average week — the same convention as the hand-entered column.
    Plan weeks with no matching history stay 1.0 and the engine's normalization
    keeps the annual total fixed either way.

    Returns (index_array, message). index_array is None when there isn't enough
    trustworthy history. "Full week" is judged against THIS QUEUE'S normal
    operating pattern (the modal Days Covered), not a hard-coded 7: a Mon-Fri
    queue's complete weeks show Days Covered = 5, and demanding 7 rejected an
    entire real history as partial (found at work, 2026-07-13). Truncated
    boundary weeks (below the queue's norm) are still excluded."""
    if bench is None or bench.empty or "Actual Contacts" not in bench.columns:
        return None, ("No ACD history with volume loaded — open the Real Data "
                      "page first so actuals are available.")
    b = bench[bench["LOB"] == lob]
    full = 7
    if "Days Covered" in b.columns and not b.empty:
        dc = pd.to_numeric(b["Days Covered"], errors="coerce").dropna()
        if not dc.empty:
            full = min(int(dc.mode().max()), 7)   # the queue's normal week
        # Configured holiday closures lower the bar for their weeks — the
        # recurring holiday dip is a REAL seasonal shape worth learning.
        _exp = full - _holiday_allowance(b["Week"])
        b = b[pd.to_numeric(b["Days Covered"], errors="coerce") >= _exp]
    vol = b.groupby("Week")["Actual Contacts"].sum(min_count=1).dropna()
    vol = vol[vol > 0]
    if len(vol) < min_weeks:
        return None, (f"Only {len(vol)} full week(s) of ACD history for "
                      f"**{lob}** (full = {full} operating day(s)/week for this "
                      f"queue) — need at least {min_weeks} to derive a curve "
                      "worth trusting. Export more history and reload.")
    woy = vol.index.map(lambda w: date.fromisoformat(str(w)).isocalendar()[1])
    rel = vol.groupby(woy).mean()
    rel = rel / rel.mean()
    out = np.array([float(rel.get(date.fromisoformat(w).isocalendar()[1], 1.0))
                    for w in weeks]).round(3)
    matched = sum(1 for w in weeks
                  if date.fromisoformat(w).isocalendar()[1] in rel.index)
    return out, (f"Derived from **{len(vol)}** full history week(s) "
                 f"(full = {full} operating day(s)/week for this queue) — matched "
                 f"**{matched}/{len(weeks)}** plan weeks by week-of-year "
                 "(unmatched weeks stay 1.0).")


def erlang_c_agents(calls_per_hr: float, aht_sec: float, sl_target_pct: float,
                    threshold_sec: float, max_occ_pct: float | None = None) -> int:
    """Minimum concurrent agents to answer `sl_target_pct`% of calls within
    `threshold_sec`, via Erlang C (no abandonment). `max_occ_pct` additionally
    caps agent occupancy (a/s) — the planner's occupancy assumption acts as a
    floor on staffing, not a divisor, because Erlang already prices the idle
    time needed to hit the SL. Iterative Erlang B keeps it numerically stable
    at any traffic volume (no factorials)."""
    a = calls_per_hr * aht_sec / 3600.0          # offered load in erlangs
    if a <= 0 or aht_sec <= 0:
        return 0
    s = max(1, int(np.ceil(a)))
    while True:
        b = 1.0                                   # Erlang B, recursively
        for k in range(1, s + 1):
            b = a * b / (k + a * b)
        if s > a:
            pw = b / (1 - (a / s) * (1 - b))      # Erlang C: P(wait > 0)
            sl = 1 - pw * np.exp(-(s - a) * threshold_sec / aht_sec)
        else:
            sl = 0.0                              # unstable queue: SL 0
        if sl >= sl_target_pct / 100 and (
                max_occ_pct is None or s == 0 or a / s <= max_occ_pct / 100):
            return s
        s += 1


def compute_plan(lob_data: dict) -> pd.DataFrame:
    d = lob_data["demand"].copy()
    roster, nh, a = lob_data["roster"], lob_data["nh"], lob_data["assumptions"]
    weeks = d["Week"].tolist()
    n = len(weeks)

    # CPM = Calls Per Member per year. Annual contacts = Members × CPM, so the
    # flat weekly base is Members × CPM ÷ 52. (Blank LOB has CPM=0 → base 0.)
    # Members: the org-wide forecast spread, but a filled "Members (actual)" cell
    # REPLACES it for that week (same rule as Attrition (actual) / Actual Grads),
    # so past weeks hindcast against the membership you really had — which is what
    # makes measured CPM, and next year's CPM trend, honest.
    members = pd.to_numeric(d["Members"], errors="coerce")
    if "Members (actual)" in d.columns:
        m_act = pd.to_numeric(d["Members (actual)"], errors="coerce")
        members = m_act.where(m_act.notna(), members)
    cpm = pd.to_numeric(d["CPM"], errors="coerce")
    base = pd.Series(
        np.where(cpm > 0, members * cpm / WEEKS_PER_YEAR, 0.0), index=d.index)

    # Seasonality index reshapes the flat spread WITHOUT changing the annual
    # total: scale the indices by k so Σ(base × index) == Σ(base). A peak week
    # thus pulls volume from the rest of the year rather than inflating it.
    # (All-1.0 → k=1 → no change; missing column → treated as 1.0 for old plans.)
    if "Seasonality" in d.columns:
        seas = pd.to_numeric(d["Seasonality"], errors="coerce").fillna(1.0).clip(lower=0)
    else:
        seas = pd.Series(1.0, index=d.index)
    weighted = float((base * seas).sum())
    k = float(base.sum()) / weighted if weighted > 0 else 1.0
    model_fcst = base * seas * k
    forecast = d["Fcst Override"].fillna(model_fcst)
    workload_hrs = forecast * d["AHT (sec)"] / 3600 * (1 + a["workload_margin_pct"] / 100)

    prod_hrs_per_fte = (
        a["paid_hours_per_week"] * (1 - a["shrinkage_pct"] / 100) * (a["occupancy_pct"] / 100)
    )
    workload_req = workload_hrs / prod_hrs_per_fte

    # Erlang C basis: concurrent agents to hit the SL target during open hours,
    # converted to on-roll FTE. Occupancy is NOT re-applied as a divisor here —
    # Erlang's s already includes the idle time the SL demands; the occupancy
    # assumption only caps a/s. Margin is applied to volume, as in workload.
    open_hrs = float(a.get("open_hrs_week", 60.0))
    sl_t = float(a.get("sl_target_pct", 80.0))
    sl_sec = float(a.get("sl_threshold_sec", 40.0))
    seat_hrs_per_fte = a["paid_hours_per_week"] * (1 - a["shrinkage_pct"] / 100)
    aht_arr = pd.to_numeric(d["AHT (sec)"], errors="coerce").fillna(0).to_numpy()
    fcst_arr = pd.to_numeric(forecast, errors="coerce").fillna(0).to_numpy()
    erlang_req = np.zeros(n)
    if open_hrs > 0 and seat_hrs_per_fte > 0:
        for i in range(n):
            cph = fcst_arr[i] * (1 + a["workload_margin_pct"] / 100) / open_hrs
            agents = erlang_c_agents(cph, aht_arr[i], sl_t, sl_sec,
                                     max_occ_pct=a["occupancy_pct"])
            erlang_req[i] = agents * open_hrs / seat_hrs_per_fte

    required_fte = (pd.Series(erlang_req, index=d.index)
                    if a.get("req_basis", "workload") == "erlang" else workload_req)

    wk_attr_rate = a["annual_attrition_pct"] / 100 / 52
    adds = nh_production_adds(weeks, nh)
    # Actualized attrition: a filled "Attrition (actual)" cell REPLACES the
    # modeled rate for that week (user decision 2026-07-12) — past weeks become
    # truth, future weeks stay forecast, and the walk self-corrects. Blank/NaN
    # in a FUTURE week = use the rate. Does NOT forward-fill: departures are
    # one-time events.
    # A blank week that has fully ELAPSED means nobody left (0, user decision
    # 2026-07-14): real weeks lose 0 or 1-2 people, never the modelled 0.54
    # average, so once departures are recorded as they happen the walk is an
    # actual headcount ledger. The weekly checklist counts these assumed-zero
    # weeks (a recording lapse must be visible, never a silent guess), and
    # measured_attrition_pct deliberately ignores them — entered weeks only —
    # so Adopt can't learn a falsely low rate from missing data.
    attr_actual = (pd.to_numeric(roster["Attrition (actual)"], errors="coerce")
                   .to_numpy(dtype=float)
                   if "Attrition (actual)" in roster.columns
                   else np.full(n, np.nan))
    attr_actual = np.where(_weeks_passed(weeks) & np.isnan(attr_actual),
                           0.0, attr_actual)
    # Roster columns are planner-typed: a cell left blank is "none", not
    # "unknown". Transfers has always been coerced this way; LOA and the walk's
    # own transfer read were not, so ONE blank LOA cell produced a NaN week in
    # Staffed FTE (field report 2026-07-30). Same rule as the half-typed
    # new-hire rows: never read a grid cell raw.
    loa_arr = pd.to_numeric(roster["LOA"], errors="coerce").fillna(0).to_numpy(dtype=float)
    xfer_arr = pd.to_numeric(roster["Transfers +/-"], errors="coerce") \
                 .fillna(0).to_numpy(dtype=float)
    # Mentors: agents on roll but off the phones coaching new-hire classes.
    # Learning & Development says how many they are taking; the planner types
    # that number. Deliberately NOT derived from the NH grid — how many mentors
    # a class needs is L&D's call, varies by class, and modelling it from class
    # size would replace a measured input with a guess (backlog 2026-07-31).
    ment_arr = (pd.to_numeric(roster["Mentors"], errors="coerce").fillna(0)
                  .to_numpy(dtype=float)
                if "Mentors" in roster.columns else np.zeros(n))
    hc = np.zeros(n)
    attrition = np.zeros(n)
    prev = float(a["starting_hc"] or 0)
    for i in range(n):
        attrition[i] = (float(attr_actual[i]) if not np.isnan(attr_actual[i])
                        else prev * wk_attr_rate)
        prev = prev - attrition[i] + xfer_arr[i] + adds[i]
        hc[i] = prev

    # NH ramp: grads count as bodies (hc, attrition) but deliver partial FTE
    # for their first ramp_weeks in production — linear from ramp_start_pct to
    # 100%. Cohorts decay at the weekly attrition rate while ramping so the
    # discount tracks survivors, not the original class size. ramp_weeks=0 → off.
    ramp_w = int(a.get("ramp_weeks", 0) or 0)
    ramp_start = float(a.get("ramp_start_pct", 60.0))
    ramp_discount = np.zeros(n)

    def _ramp_cohort(j: int, size: float, weeks_to_full: int, start_pct: float):
        for k in range(min(weeks_to_full, n - j)):
            prod = (start_pct + (100 - start_pct) * k / weeks_to_full) / 100
            surviving = size * (1 - wk_attr_rate) ** k
            ramp_discount[j + k] += surviving * (1 - prod)

    if ramp_w > 0:
        for j in range(n):                       # NH cohort graduating week j
            if adds[j] > 0:
                _ramp_cohort(j, adds[j], ramp_w, ramp_start)

    # Transfer ramp (interims): agents arriving on a line they don't know
    # deliver partial FTE briefly. Only FRESH inflows ramp — an inflow that
    # reverses this LOB's own earlier outflow is a returning agent coming
    # home at full weight (the interim round-trip on the donor side).
    t_ramp_w = int(a.get("transfer_ramp_weeks", 2) or 0)
    t_start = float(a.get("transfer_ramp_start_pct", 75.0))
    if t_ramp_w > 0:
        tr_arr = xfer_arr
        out_bal = 0.0
        for j in range(n):
            if tr_arr[j] < 0:
                out_bal += -tr_arr[j]
            elif tr_arr[j] > 0:
                fresh = tr_arr[j] - min(tr_arr[j], out_bal)
                out_bal -= min(tr_arr[j], out_bal)
                if fresh > 0:
                    _ramp_cohort(j, fresh, t_ramp_w, t_start)

    # Mentors are bodies on roll (they stay in Production HC and keep attriting)
    # but deliver no phone capacity while they mentor — same shape as LOA.
    staffed = hc - loa_arr - ment_arr - ramp_discount
    net = staffed - required_fte
    capacity_calls = staffed * prod_hrs_per_fte * 3600 / d["AHT (sec)"]

    # Support staff (the legacy plan rows): flat per-LOB counts the planner enters; the
    # RATIO rows are computed (walking agents ÷ count) so drift stays visible
    # as attrition/hiring moves headcount. Informational — sups/leads take no
    # calls, so Staffed/Net are untouched; Overall HC = what the org pays for.
    def _ros_col(col, legacy_key):
        if col in roster.columns:
            return pd.to_numeric(roster[col], errors="coerce").fillna(0).to_numpy(dtype=float)
        return np.full(n, float(a.get(legacy_key, 0.0) or 0.0))  # pre-column snapshots
    sups = _ros_col("Supervisors", "supervisors")
    leads = _ros_col("Leads/Project", "leads")
    support = sups + leads
    sup_ratio = np.where(sups > 0, hc / np.where(sups > 0, sups, 1.0), 0.0)
    lead_ratio = np.where(leads > 0, hc / np.where(leads > 0, leads, 1.0), 0.0)

    # Available Hours = productive hours one FTE delivers per week (the Required-FTE
    # denominator). FT/PT split mirrors the legacy model's Full-time/Part-time rows.
    ft = a.get("ft_pct", 100.0) / 100
    return pd.DataFrame(
        {
            "Week": weeks,
            "Model Forecast": model_fcst.round(0),
            "Forecast (final)": forecast.round(0),
            "Workload (hrs)": workload_hrs.round(1),
            "Available Hrs/FTE": round(prod_hrs_per_fte, 1),
            "Required FTE": pd.Series(required_fte).round(1).to_numpy(),
            "Workload Req FTE": workload_req.round(1),
            "Erlang Req FTE": np.round(erlang_req, 1),
            "Production HC": hc.round(1),
            "Prod HC — FT": (hc * ft).round(1),
            "Prod HC — PT": (hc * (1 - ft)).round(1),
            "Supervisors": sups,
            "Supervisor Ratios": np.round(sup_ratio, 1),
            "Leads/Project": leads,
            "Leads/Project Ratios": np.round(lead_ratio, 1),
            "Support Staff": support,
            "Overall HC": (hc + support).round(1),
            "Attrition": attrition.round(2),
            "Attrition (actual)": np.round(attr_actual, 2),   # blank where modelled
            "NH Grads": adds.round(1),
            "Ramp Discount": np.round(ramp_discount, 1),
            "LOA": loa_arr,
            "Mentors": ment_arr,
            "Staffed FTE": staffed.round(1),
            "Net FTE": net.round(1),
            "Volume Capacity": capacity_calls.round(0),
        }
    )


def consolidated_plan(lobs: dict) -> pd.DataFrame:
    plans = {lob: compute_plan(d) for lob, d in lobs.items()}
    first = next(iter(plans.values()))
    out = pd.DataFrame({"Week": first["Week"]})
    for col in ["Forecast (final)", "Workload (hrs)", "Required FTE", "Staffed FTE", "Net FTE"]:
        out[col] = sum(p[col].to_numpy() for p in plans.values()).round(1)
    return out


def measured_nh_washout(nh: pd.DataFrame) -> tuple[float, float, int, float] | None:
    """(actual washout %, planned washout %, classes, hired) across classes that
    have an `Actual Grads` figure. Actual = 1 − Σgrads ÷ Σsize; planned = the
    same classes' stage-attrition prediction. Calibrates the stage assumptions
    WITHOUT pretending to know which stage lost people — grads alone can't say,
    so this is a readout, not an auto-adopt."""
    if nh is None or nh.empty or "Actual Grads" not in nh.columns:
        return None
    d = nh.dropna(subset=["Class Start Week"]).copy()
    act = pd.to_numeric(d.get("Actual Grads"), errors="coerce")
    d = d[act.notna()]
    if d.empty:
        return None
    size = pd.to_numeric(d["Class Size"], errors="coerce").fillna(0)
    grads = pd.to_numeric(d["Actual Grads"], errors="coerce").fillna(0)
    tot = float(size.sum())
    if tot <= 0:
        return None
    planned = float((size
                     * (1 - pd.to_numeric(d["Training Attr %"], errors="coerce").fillna(0) / 100)
                     * (1 - pd.to_numeric(d["Coaching Attr %"], errors="coerce").fillna(0) / 100)
                     ).sum())
    return ((1 - float(grads.sum()) / tot) * 100,
            (1 - planned / tot) * 100, int(len(d)), tot)


HOLIDAYS_FILE_NAME = "holidays.json"


def _holidays_path() -> Path:
    return APP_DIR / HOLIDAYS_FILE_NAME


def load_holidays() -> list[str]:
    """Org-wide closure dates (ISO), team-shared next to the app — a closed
    holiday makes a week legitimately one day short of the queue's norm, and
    the partial-week logic must not treat it as a truncated export (field
    finding 2026-07-16: 'for holidays it freaks out because we are closed')."""
    if "holidays" not in st.session_state:
        try:
            st.session_state["holidays"] = sorted(set(
                json.loads(_holidays_path().read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            st.session_state["holidays"] = []
    return st.session_state["holidays"]


def _holidays_in_week(week_iso) -> int:
    """How many configured closure dates fall inside this Mon-anchored week."""
    hols = load_holidays()
    if not hols:
        return 0
    try:
        monday = date.fromisoformat(str(week_iso))
    except ValueError:
        return 0
    return sum(1 for h in hols
               if monday <= date.fromisoformat(h) <= monday + timedelta(days=6))


def _holiday_allowance(weeks: pd.Series) -> pd.Series:
    """Per-row day allowance: a week containing N closures expects N fewer
    covered days. Approximation: a closure on a day a queue doesn't operate
    (Saturday holiday vs a Mon-Fri queue) still grants the allowance — that
    errs toward INCLUDING a week, never excluding a legitimate one."""
    if not load_holidays():
        return pd.Series(0, index=weeks.index)
    return weeks.map(_holidays_in_week)


def _drop_partial_weeks(b: pd.DataFrame) -> pd.DataFrame:
    """Benchmark rows below the queue's normal week (modal Days Covered, per
    LOB — the same norm seasonality-derive and the Real Data warning use) are
    truncated exports or the in-progress week. Comparing them against a full
    plan week fabricates variance — the overlay line plummets and Var explodes
    (user 2026-07-14) — so every benchmark consumer drops them here, at the
    one choke point. The Real Data page still shows and flags them."""
    if b.empty or "Days Covered" not in b.columns:
        return b
    dc = pd.to_numeric(b["Days Covered"], errors="coerce")
    norm = dc.groupby(b["LOB"]).transform(lambda x: x.mode().max())
    norm = norm - _holiday_allowance(b["Week"])   # closures aren't truncation
    return b[dc >= norm]


def _partial_bench_weeks(lob: str | None) -> list[str]:
    """The distinct partial feed weeks _drop_partial_weeks is hiding for this
    LOB (or all LOBs) — so pages can SAY they excluded them, never silently."""
    out: set[str] = set()
    for key in ("acd_weekly",):
        bench = st.session_state.get(key)
        if bench is None or bench.empty or "Days Covered" not in bench.columns:
            continue
        b = bench if lob is None else bench[bench["LOB"] == lob]
        if b.empty:
            continue
        dc = pd.to_numeric(b["Days Covered"], errors="coerce")
        norm = dc.groupby(b["LOB"]).transform(lambda x: x.mode().max())
        norm = norm - _holiday_allowance(b["Week"])
        out.update(str(w) for w in b.loc[dc < norm, "Week"])
    return sorted(out)


def _partial_note(lob: str | None):
    """One-line honesty note wherever benchmark overlays render."""
    wks = _partial_bench_weeks(lob)
    if wks:
        st.caption(f"⚠️ **{len(wks)} partial feed week(s) excluded** from the "
                   "actuals overlays/variances (below this queue's normal "
                   "*Days Covered* — an in-progress or truncated export week "
                   "read as a full week would fake a plummet). Inspect them on "
                   "Real Data: " + ", ".join(w[5:] for w in wks[:8])
                   + ("…" if len(wks) > 8 else ""))


def _bench_series(key: str, col: str, lob: str | None, weeks) -> pd.Series | None:
    """Pull a persisted ACD benchmark column, aligned to the plan weeks.

    `lob=None` sums across all LOBs (for the consolidated view). Returns None if
    the benchmark isn't loaded or has no weeks overlapping the plan horizon.
    Partial feed weeks are excluded (see _drop_partial_weeks).
    """
    bench = st.session_state.get(key)
    if bench is None or bench.empty or col not in bench.columns:
        return None
    b = bench if lob is None else bench[bench["LOB"] == lob]
    b = _drop_partial_weeks(b)
    if b.empty:
        return None
    s = b.groupby("Week")[col].sum(min_count=1)  # all-NaN week stays NaN, not 0
    aligned = s.reindex(weeks)
    return aligned if aligned.notna().any() else None


CPM_MIN_WEEKS = 8    # same discipline as attrition/seasonality: few weeks = noise


def measured_cpm(lob: str) -> tuple[float, int] | None:
    """(annualized actual CPM, weeks used) for one LOB, from the weeks that have
    BOTH an actual membership figure and actual contacts from the WFM feed:

        CPM_actual = Σ actual contacts × 52 ÷ Σ actual members (per week)

    That is the same definition the model uses in reverse (weekly calls =
    Members × CPM ÷ 52), so it is a like-for-like check on the entered CPM.
    None when the inputs aren't there. Caller must respect CPM_MIN_WEEKS."""
    lobs = st.session_state.get("lobs") or {}
    if lob not in lobs:
        return None
    dem = lobs[lob]["demand"]
    weeks = dem["Week"].tolist()
    _sess = st.session_state.get("members_actual")
    if _sess is not None and len(_sess) == len(weeks):
        m_act = pd.Series(np.asarray(_sess, dtype=float), index=dem.index)
    elif "Members (actual)" in dem.columns:
        m_act = pd.to_numeric(dem["Members (actual)"], errors="coerce")
    else:
        return None
    contacts = _bench_series("acd_weekly", "Actual Contacts", lob, weeks)
    if contacts is None:
        return None
    c = pd.to_numeric(pd.Series(contacts.values, index=dem.index), errors="coerce")
    mask = m_act.notna() & c.notna() & (m_act > 0) & (c > 0)
    if not mask.any():
        return None
    tot_m = float(m_act[mask].sum())
    if tot_m <= 0:
        return None
    return (float(c[mask].sum()) * WEEKS_PER_YEAR / tot_m, int(mask.sum()))


CPM_TREND_MIN_WEEKS = 26   # a trend needs ~half a year; 8 weeks is noise, not drift
CPM_TREND_DAMPING = 0.98   # Holt-style φ: projected week k moves by slope × Σφ^j (j≤k).
                           # Over 52 weeks that sums to ~31.8 weeks of slope, i.e. the
                           # projection captures ~61% of the raw linear drift and flattens
                           # thereafter — a short-run slope cannot run away over a year,
                           # but a real multi-month drift still shows up. φ=0.85 (a common
                           # default) would collapse a year of drift into ~6 weeks' worth.


def cpm_weekly_actuals(lob: str) -> pd.DataFrame | None:
    """Per-week measured CPM for one LOB, DESEASONALIZED.

    measured CPM(w) = actual contacts(w) × 52 ÷ actual members(w), then divided
    by that week's seasonality index — otherwise a linear fit would mistake the
    seasonal shape ("December runs hot") for a genuine drift in calls-per-member.
    Returns columns [i, Week, cpm, cpm_deseas]; None when there is nothing to
    measure."""
    lobs = st.session_state.get("lobs") or {}
    if lob not in lobs:
        return None
    dem = lobs[lob]["demand"]
    weeks = dem["Week"].tolist()
    contacts = _bench_series("acd_weekly", "Actual Contacts", lob, weeks)
    if contacts is None:
        return None
    # Org-wide series is the source of truth: the sidebar (which fits the trend
    # for the rollover panel) renders BEFORE apply_global_members() mirrors it
    # into the demand frames, so reading session state avoids a stale-frame miss.
    _sess = st.session_state.get("members_actual")
    if _sess is not None and len(_sess) == len(weeks):
        m = np.asarray(_sess, dtype=float)
    elif "Members (actual)" in dem.columns:
        m = pd.to_numeric(dem["Members (actual)"], errors="coerce").to_numpy(dtype=float)
    else:
        return None
    c = pd.to_numeric(pd.Series(contacts.values), errors="coerce").to_numpy(dtype=float)
    seas = (pd.to_numeric(dem["Seasonality"], errors="coerce").fillna(1.0)
            .replace(0, np.nan).to_numpy(dtype=float)
            if "Seasonality" in dem.columns else np.ones(len(weeks)))
    ok = (~np.isnan(m)) & (~np.isnan(c)) & (m > 0) & (c > 0)
    if not ok.any():
        return None
    idx = np.arange(len(weeks))
    cpm = np.full(len(weeks), np.nan)
    cpm[ok] = c[ok] * WEEKS_PER_YEAR / m[ok]
    out = pd.DataFrame({"i": idx[ok], "Week": np.array(weeks)[ok],
                        "cpm": cpm[ok], "cpm_deseas": cpm[ok] / seas[ok]})
    return out if not out.empty else None


def fit_cpm_trend(lob: str) -> dict | None:
    """Least-squares linear fit on DESEASONALIZED weekly measured CPM.

    Returns {level, slope_per_week, slope_per_year, pct_per_year, weeks, r2,
    last_fitted} — a description of what HAS been happening, not a forecast.
    None when there is nothing to fit. Caller must respect
    CPM_TREND_MIN_WEEKS; a slope from a handful of weeks is noise."""
    d = cpm_weekly_actuals(lob)
    if d is None or len(d) < 3:
        return None
    x = d["i"].to_numpy(dtype=float)
    y = d["cpm_deseas"].to_numpy(dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    fitted = slope * x + intercept
    ss_res = float(((y - fitted) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    level = float(y.mean())
    return {"level": level,
            "slope_per_week": float(slope),
            "slope_per_year": float(slope) * WEEKS_PER_YEAR,
            "pct_per_year": (float(slope) * WEEKS_PER_YEAR / level * 100
                             if level > 0 else 0.0),
            "weeks": int(len(d)),
            "r2": float(r2),
            "last_fitted": float(slope * x[-1] + intercept),
            "last_i": int(x[-1])}


def project_cpm(trend: dict, n_weeks: int) -> np.ndarray:
    """Project next year's per-week CPM from a fitted trend, with **damped**
    slope (φ = CPM_TREND_DAMPING): week k gets level + slope × Σφ^j, j<k. The
    damping means a steep short-run slope flattens out instead of running away
    over 52 weeks — the projection continues the drift with decreasing
    confidence, which is the honest shape for a driver forecast.

    Starts from the trend's LAST FITTED value (where the drift has got to), not
    the raw last observation (which carries that week's noise)."""
    phi, start, slope = CPM_TREND_DAMPING, trend["last_fitted"], trend["slope_per_week"]
    out = np.empty(n_weeks, dtype=float)
    cum = 0.0
    for k in range(n_weeks):
        cum += phi ** (k + 1)
        out[k] = max(0.0, start + slope * cum)
    return np.round(out, 3)


ATTR_MIN_WEEKS = 8   # below this, annualizing a few weeks is noise, not signal


def _weeks_passed(weeks: list[str]) -> np.ndarray:
    """True for plan weeks that have fully elapsed (their Sunday is behind
    today) — the boundary where forecast becomes history. Deliberately makes
    compute_plan clock-dependent: hindcasting past weeks on facts is the
    point (Members (actual) does the same via entered data)."""
    today = date.today()
    return np.array([date.fromisoformat(w) + timedelta(days=7) <= today
                     for w in weeks], dtype=bool)


def measured_attrition_pct(lob_data: dict) -> tuple[float, int] | None:
    """(annualized %, weeks used) implied by the weeks where the planner entered
    an actual: Σ departures ÷ Σ headcount-at-risk × 52 × 100, against the same
    walking headcount the engine uses. None when nothing is entered.

    Caller must respect ATTR_MIN_WEEKS: annualizing 1-2 weeks produces absurd
    figures (4 leavers out of 99 in one week annualizes to 210%). Same
    discipline as derive-seasonality, which refuses below 8 full weeks."""
    ros = lob_data["roster"]
    if "Attrition (actual)" not in ros.columns:
        return None
    act = pd.to_numeric(ros["Attrition (actual)"], errors="coerce").to_numpy(dtype=float)
    weeks_with = ~np.isnan(act)
    if not weeks_with.any():
        return None
    hc = compute_plan(lob_data)["Production HC"].to_numpy(dtype=float)
    start_hc = np.concatenate(([float(lob_data["assumptions"]["starting_hc"])], hc[:-1]))
    at_risk = start_hc[weeks_with].sum()
    if at_risk <= 0:
        return None
    return (float(act[weeks_with].sum() / at_risk * 52 * 100), int(weeks_with.sum()))


def roll_over_plan(cpm_seed: dict | None = None) -> None:
    """Seed next year's working plan from the current one. Carry rules:
    ending production HC → starting HC; year-end members → starting members
    (planner enters the new year-end target); last CPM/AHT carry everywhere
    (step-change semantics, same as their forward-fill); the seasonality shape
    copies positionally (both years are Jan-anchored Monday weeks); the final
    LOA level stays out until edited back down; Transfers and Fcst Override
    reset; NH classes whose grads land beyond this year's horizon re-enter at
    week 1 with their remaining training/coaching weeks (stage attrition kept
    only for stages with weeks remaining — survivors of a finished stage
    aren't attrited twice). Touches only the working plan — published
    snapshots are untouched until the new year is itself published."""
    yr, n = plan_year(), st.session_state.n_weeks
    new_weeks = [w.isoformat() for w in week_starts(n, yr + 1)]
    new_lobs = {}
    for name, d in st.session_state.lobs.items():
        plan = compute_plan(d)
        dem, ros, nh = d["demand"], d["roster"], d["nh"]

        def last(col, df=dem):
            v = pd.to_numeric(df[col], errors="coerce").iloc[-1]
            return float(v) if pd.notna(v) else 0.0

        seas = pd.to_numeric(dem["Seasonality"], errors="coerce").fillna(1.0).to_numpy()
        _seed = (cpm_seed or {}).get(name)
        new_dem = pd.DataFrame({
            "Week": new_weeks,
            "Members": [last("Members")] * n,   # apply_global_members re-spreads
            "Members (actual)": [np.nan] * n,   # last year's membership is history
            # CPM: flat carry by default; the planner can seed the new year from
            # the measured trend instead (rollover expander) — evidence, not habit.
            "CPM": (list(np.resize(_seed, n)) if _seed is not None
                    else [last("CPM")] * n),
            "Seasonality": np.resize(seas, n),
            "Fcst Override": [np.nan] * n,
            "AHT (sec)": [last("AHT (sec)")] * n,
        })
        new_ros = pd.DataFrame({
            "Week": new_weeks,
            "LOA": [last("LOA", ros)] * n,
            # Classes straddling Dec 31 re-enter at week 1 (below), so the
            # mentors coaching them are still off the phones on Jan 1.
            "Mentors": [last("Mentors", ros)
                        if "Mentors" in ros.columns else 0.0] * n,
            "Transfers +/-": [0.0] * n,
            "Attrition (actual)": [np.nan] * n,   # last year's departures are history

            "Supervisors": [last("Supervisors", ros)
                            if "Supervisors" in ros.columns else 0.0] * n,
            "Leads/Project": [last("Leads/Project", ros)
                              if "Leads/Project" in ros.columns else 0.0] * n,
        })
        nh_rows = []
        idx = {w: i for i, w in enumerate(dem["Week"].tolist())}

        def _n(v, default=0.0):
            x = pd.to_numeric(v, errors="coerce")
            return float(default) if pd.isna(x) else float(x)

        for _, r in nh.dropna(subset=["Class Start Week"]).iterrows():
            if r["Class Start Week"] not in idx:
                continue
            if _n(r.get("Class Size")) <= 0:
                continue                  # half-typed row: nothing to carry
            tr, co = int(_n(r.get("Training Wks"))), int(_n(r.get("Coaching Wks")))
            done = len(idx) - idx[r["Class Start Week"]]  # weeks elapsed by Dec 31
            rem = tr + co - done
            if rem <= 0:
                continue                      # graduated inside the old year
            rem_tr = max(0, tr - done)
            nh_rows.append({
                "Class Start Week": new_weeks[0],
                "Class Size": _n(r.get("Class Size")),
                "Training Wks": float(rem_tr),
                "Coaching Wks": float(rem - rem_tr),
                "Training Attr %": (_n(r.get("Training Attr %")) if rem_tr > 0 else 0.0),
                "Coaching Attr %": _n(r.get("Coaching Attr %")),
                "Actual Grads": np.nan,      # hasn't graduated yet
            })
        a = dict(d["assumptions"])
        a["starting_hc"] = float(plan["Production HC"].iloc[-1])
        new_lobs[name] = {
            "demand": new_dem, "roster": new_ros,
            "nh": pd.DataFrame(nh_rows, columns=list(nh.columns)),
            "assumptions": a,
        }
    st.session_state.lobs = new_lobs
    st.session_state.plan_year = yr + 1
    me = float(st.session_state.get("members_end", 0.0) or 0.0)
    st.session_state.members_start = me
    st.session_state.members_end = me   # planner enters the new year-end forecast
    st.session_state.loaded_version = None   # unpublished until the team publishes it
    st.session_state["members_actual"] = None   # new year: no actuals yet
    _purge_assumption_widgets()


# ----------------------------------------------------------------------
# Hiring Advisor — greedy solvers over compute_plan
# ----------------------------------------------------------------------
def recommend_classes(lob_data: dict, template: dict
                      ) -> tuple[list[dict], list[dict]]:
    """Greedy class plan: find the first red week, back a class start up by the
    pipeline lead time (training + coaching), size it net of stage attrition
    AND ramp-start productivity (a grad's week-one contribution, not their
    eventual one), simulate, repeat until green or infeasible.

    Class starts respect the LOB's hiring cadence (`class_gap_weeks`, per-LOB —
    trainers/facilitators run monthly-ish cohorts, not a class every week; user
    2026-07-14): starts sit at least `gap` weeks from every other class on the
    calendar (including classes already in the plan), so each class is sized to
    carry EVERY red week until the next slot can land grads. A start that has
    to slide later than a red week makes that week uncoverable-at-cadence —
    reported, never quietly covered by an unsustainable extra class. gap=0
    reproduces the unconstrained per-week behavior.

    A class below the LOB's minimum size (`class_min_size`, default 1 = off)
    is never recommended — a cohort has facilitator/classroom economics (user
    2026-07-14): a sub-minimum need FOLDS into the previous recommended class
    when the max allows (same bodies, earlier landing, one fewer trainer
    event, exact seats), otherwise the stretch is DEFERRED — reported
    why="min" for OT/interims — until the accumulated need justifies a cohort.

    `one_class_at_a_time` (per-LOB, default off; user 2026-07-14 — the main
    line has ONE training team): a new class cannot start until the previous
    one has fully graduated (training + nesting), so starts space by the
    class's whole pipeline length — tracked from the template/each planned
    row's own weeks, so a longer curriculum automatically stretches the
    calendar. Class size limits come from `class_max_size` (per-LOB, default
    12) with `class_min_size` clamped under it.

    Returns (recommended class rows, uncoverable shortfalls). Uncoverable
    entries carry why="lead" (inside the pipeline lead time), why="cadence"
    (no calendar-legal start lands grads in time), or why="min" (need too
    small for a cohort); all need interims or OT."""
    work = {"demand": lob_data["demand"], "roster": lob_data["roster"],
            "nh": lob_data["nh"].copy(), "assumptions": dict(lob_data["assumptions"])}
    a = work["assumptions"]
    gap = int(a.get("class_gap_weeks", 4) or 0)
    one_at_a_time = bool(a.get("one_class_at_a_time", False))
    tr, co = int(template["training_wks"]), int(template["coaching_wks"])
    lead = tr + co
    survival = ((1 - template["training_attr"] / 100)
                * (1 - template["coaching_attr"] / 100))
    prod0 = (float(a.get("ramp_start_pct", 60.0)) / 100
             if int(a.get("ramp_weeks", 0) or 0) > 0 else 1.0)
    eff = max(survival * prod0, 1e-6)   # effective week-one FTE per seat hired
    max_size = float(a.get("class_max_size", 12) or 12)
    min_size = min(max(1, int(a.get("class_min_size", 1) or 1)), int(max_size))
    # The stretch a class must carry alone: until the next legal start's grads
    # can land — one-at-a-time pushes that to a full pipeline length away.
    eff_gap = max(gap, lead) if one_at_a_time else gap
    weeks = work["demand"]["Week"].tolist()
    idx = {w: i for i, w in enumerate(weeks)}
    # Classes already in the plan occupy the training calendar too — each with
    # its OWN pipeline length (coerced: dynamic-editor rows may be half-typed).
    starts, occupied = [], []
    for _, r0 in work["nh"].iterrows():
        w0 = str(r0.get("Class Start Week"))
        if w0 in idx:
            ln = pd.to_numeric(pd.Series([r0.get("Training Wks"),
                                          r0.get("Coaching Wks")]),
                               errors="coerce").fillna(0).sum()
            starts.append(idx[w0])
            occupied.append((idx[w0], idx[w0] + max(int(ln), 1)))
    starts.sort()

    def _legal(x: int) -> bool:
        if gap and any(abs(x - p) < gap for p in starts):
            return False
        if one_at_a_time and any(x + lead > o_s and x < o_e
                                 for o_s, o_e in occupied):
            return False
        return True

    recs, uncoverable = [], []
    scan_from = 0
    for _ in range(60):                 # backstop, not a real bound
        net = compute_plan(work)["Net FTE"].to_numpy()
        red = [i for i in range(scan_from, len(net)) if net[i] < -0.05]
        if not red:
            break
        i = red[0]
        s = i - lead                    # latest start whose grads land by week i
        if s < 0:
            uncoverable.append({"week": weeks[i], "short": round(float(-net[i]), 1),
                                "why": "lead"})
            scan_from = i + 1
            continue
        while not _legal(s):            # slide later until the calendar is legal
            s += 1
        land = s + lead
        if land >= len(net):            # no legal start lands inside the horizon
            for j in red:
                uncoverable.append({"week": weeks[j], "short": round(float(-net[j]), 1),
                                    "why": "cadence"})
            break
        for j in red:                   # red weeks this class can no longer reach
            if j < land:
                uncoverable.append({"week": weeks[j], "short": round(float(-net[j]), 1),
                                    "why": "cadence"})
        # Size for the deepest shortfall in the stretch this class carries alone
        # (until the next calendar-legal slot could land grads).
        carry_end = min(land + eff_gap, len(net)) if eff_gap else land + 1
        window_red = [j for j in range(land, carry_end) if net[j] < -0.05]
        if not window_red:
            scan_from = land            # its stretch is green — no class needed yet
            continue
        depth = max(float(-net[j]) for j in window_red)
        need = float(np.ceil(depth / eff))
        if need < min_size:
            prev = recs[-1] if recs else None
            if prev is not None and prev["Class Size"] + need <= max_size:
                # Fold the sliver into the previous cohort: same bodies land
                # earlier, one fewer trainer event, exact seats.
                prev["Class Size"] += need
                work["nh"].loc[prev["_nh_i"], "Class Size"] = prev["Class Size"]
                prev["carries_to"] = weeks[window_red[-1]]
                scan_from = land
                continue
            for j in window_red:    # defer until the need justifies a cohort
                uncoverable.append({"week": weeks[j], "short": round(float(-net[j]), 1),
                                    "why": "min"})
            scan_from = carry_end
            continue
        size = float(min(max_size, need))
        row = {"Class Start Week": weeks[s], "Class Size": size,
               "Training Wks": float(tr), "Coaching Wks": float(co),
               "Training Attr %": float(template["training_attr"]),
               "Coaching Attr %": float(template["coaching_attr"]),
               "Actual Grads": np.nan}
        work["nh"] = pd.concat([work["nh"], pd.DataFrame([row])], ignore_index=True)
        starts.append(s)
        occupied.append((s, s + max(lead, 1)))
        recs.append({**row, "_nh_i": len(work["nh"]) - 1, "lands": weeks[land],
                     "covers": weeks[window_red[0]],
                     "carries_to": weeks[window_red[-1]]})
        scan_from = land
    return recs, uncoverable


def shortfall_windows(lob_data: dict) -> list[dict]:
    """Contiguous red-week windows for one LOB: start/end, deepest shortfall,
    whole-agent cover size, and whether the window overlaps LOA weeks (the
    'someone went out — do we need an interim?' case)."""
    plan = compute_plan(lob_data)
    net = plan["Net FTE"].to_numpy()
    weeks = plan["Week"].tolist()
    loa = pd.to_numeric(lob_data["roster"]["LOA"], errors="coerce").fillna(0).to_numpy()
    a = lob_data["assumptions"]
    # An arriving interim ramps on this line — size the pull so even week-one
    # (starting-productivity) coverage fills the hole.
    t_prod0 = (float(a.get("transfer_ramp_start_pct", 75.0)) / 100
               if int(a.get("transfer_ramp_weeks", 2) or 0) > 0 else 1.0)
    out, i = [], 0
    while i < len(net):
        if net[i] < -0.05:
            j = i
            while j + 1 < len(net) and net[j + 1] < -0.05:
                j += 1
            depth = float(-net[i:j + 1].min())
            out.append({"start_i": i, "end_i": j, "start": weeks[i], "end": weeks[j],
                        "depth": round(depth, 1),
                        "agents": int(np.ceil(depth / t_prod0 - 1e-9)),
                        "loa_linked": bool((loa[i:j + 1] > 0).any())})
            i = j + 1
        else:
            i += 1
    return out


def donor_after_pull(donor_data: dict, start_i: int, end_i: int, n_agents: int
                     ) -> float:
    """Donor LOB's worst-week Net if `n_agents` are pulled for the window
    (temporary transfer out at start, back the week after end)."""
    ros = donor_data["roster"].copy()
    t = pd.to_numeric(ros["Transfers +/-"], errors="coerce").fillna(0) \
          .to_numpy(dtype=float).copy()   # to_numpy can be a read-only view (CoW)
    t[start_i] -= n_agents
    if end_i + 1 < len(t):
        t[end_i + 1] += n_agents
    ros["Transfers +/-"] = t
    sim = dict(donor_data); sim["roster"] = ros
    return float(compute_plan(sim)["Net FTE"].min())


def apply_interim(spec_lob: str, donor_lob: str, start_i: int, end_i: int,
                  n_agents: int, lobs: dict | None = None) -> None:
    """Book the temporary transfer on both LOBs' rosters (working plan only)."""
    lobs = lobs if lobs is not None else st.session_state.lobs
    for name, sign in ((spec_lob, +1), (donor_lob, -1)):
        ros = lobs[name]["roster"]
        t = pd.to_numeric(ros["Transfers +/-"], errors="coerce").fillna(0) \
              .to_numpy(dtype=float).copy()   # CoW: to_numpy may be read-only
        t[start_i] += sign * n_agents
        if end_i + 1 < len(t):
            t[end_i + 1] -= sign * n_agents
        ros["Transfers +/-"] = t


# ----------------------------------------------------------------------
# ACD shrinkage — ingest ACD hsplit interval exports
# ----------------------------------------------------------------------
AUX_COLS = [f"i_auxtime{i}" for i in range(100)]


def load_acd(sources) -> pd.DataFrame:
    frames = [pd.read_csv(s, encoding="utf-8-sig") for s in sources]
    df = pd.concat(frames, ignore_index=True)
    # Same canonical-field resolution as the Real Data page, so a mapped
    # non-standard export works on BOTH pages (one mapping, one truth).
    df, _, _, _ = sx.resolve_columns(df, "acd", load_field_maps().get("acd"))
    keep = ["row_date", "starttime", "split", "i_stafftime", "i_auxtime",
            "i_availtime", "i_acdtime", "i_acwtime", "i_othertime"]
    present = [c for c in keep if c in df.columns]
    aux_present = [c for c in AUX_COLS if c in df.columns]
    return df[present + aux_present]


UNMAPPED_LOB = "— unmapped —"


FIELD_MAPS_FILE_NAME = "field_maps.json"   # {feed: {canonical: source column}}


def _field_maps_path():
    return APP_DIR / FIELD_MAPS_FILE_NAME


def load_field_maps() -> dict:
    """Remembered column mapping per feed — how THIS vendor's headers map onto
    the app's canonical fields. Empty until a planner maps a non-standard
    export (the ACD sample resolves by name or alias with no map at all)."""
    try:
        return json.loads(_field_maps_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_field_maps(maps: dict):
    collab._atomic_write(_field_maps_path(), json.dumps(
        {f: {k: v for k, v in m.items() if v} for f, m in maps.items()}, indent=2))


ACD_MAPS_FILE_NAME = "acd_maps.json"   # lives next to the app (APP_DIR, defined below)


def _acd_maps_path():
    return APP_DIR / ACD_MAPS_FILE_NAME


def load_acd_maps() -> dict:
    """Team-remembered AUX names + split→LOB choices ({} when unconfigured)."""
    try:
        return json.loads(_acd_maps_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def persist_acd_maps():
    """Write the current mapping tables whenever they differ from what's on
    disk — renames/toggles in the grids are remembered across sessions (and
    across the team, once the app runs off the share)."""
    blob = json.dumps(
        {"aux": st.session_state.aux_map.to_dict(orient="records"),
         "splits": st.session_state.split_map.to_dict(orient="records")},
        indent=2, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    if st.session_state.get("_acd_maps_blob") != blob:
        # Share offline / read-only must never take the page down (same rule
        # as _autosave_draft; audit 2026-07-14 reproduced a page crash on a
        # read-only dir). Choices still apply this session; the blob stays
        # unset so the write retries — and the warning clears — once the
        # share is back.
        try:
            collab._atomic_write(_acd_maps_path(), blob)
            st.session_state["_acd_maps_blob"] = blob
        except OSError as exc:
            st.warning(f"Couldn't save the shared mapping config "
                       f"({exc.__class__.__name__}: {exc}). Your choices still "
                       "apply in this session and saving will retry — check "
                       "the share connection/permissions.")


def apply_acd_toggles(sdf: pd.DataFrame) -> pd.DataFrame:
    """One shrinkage truth: the Shrinkage page's remembered choices govern
    EVERY ACD-derived number, including the Real Data weekly rollup and the
    calibrate-the-model path. Drops splits unticked there, honors split→LOB
    reassignments, and subtracts AUX codes unticked 'Count as In-Office
    Shrink' from i_auxtime. No-op when nothing is configured yet (reads the
    session tables, falling back to acd_maps.json)."""
    sm = st.session_state.get("split_map")
    am = st.session_state.get("aux_map")
    if sm is None or am is None:
        saved = load_acd_maps()
        if sm is None and saved.get("splits"):
            sm = pd.DataFrame(saved["splits"])
        if am is None and saved.get("aux"):
            am = pd.DataFrame(saved["aux"])
    out = sdf
    if sm is not None and len(sm):
        s_ids = pd.to_numeric(sm["Split"], errors="coerce")
        excluded = {int(s) for s, inc in zip(s_ids, sm["Include"])
                    if pd.notna(s) and not bool(inc)}
        if excluded:
            out = out[~out["split"].isin(excluded)].copy()
        remap = {int(s): l for s, l in zip(s_ids, sm["LOB"])
                 if pd.notna(s) and isinstance(l, str) and l and l != UNMAPPED_LOB}
        if remap:
            out = out.copy()
            out["LOB"] = out["split"].map(remap).fillna(out["LOB"])
    if am is not None and len(am):
        codes = pd.to_numeric(am["Code"], errors="coerce")
        off = [int(c) for c, t in zip(codes, am["Count as In-Office Shrink"])
               if pd.notna(c) and not bool(t)]
        drop_cols = [f"i_auxtime{c}" for c in off if f"i_auxtime{c}" in out.columns]
        if drop_cols:
            out = out.copy()
            sub = sum(pd.to_numeric(out[c], errors="coerce").fillna(0)
                      for c in drop_cols)
            out["i_auxtime"] = (out["i_auxtime"] - sub).clip(lower=0)
    return out


def ensure_maps(df: pd.DataFrame, mapping=None):
    """Create / extend the split→LOB and AUX-code→category mapping tables.

    When `Skill_Mapping.csv` is loaded, each split is auto-assigned its LOB via
    Skill_ID → Line_of_Business; splits absent from the mapping (blend / overhead
    skills like 173) are marked unmapped and excluded by default. Without a
    mapping we fall back to the first LOB so the page still works manually.
    Existing rows (and the planner's edits) are preserved across reruns."""
    fallback = list(st.session_state.lobs.keys())[0]
    splits = sorted(df["split"].unique())
    if "split_map" not in st.session_state:
        saved = load_acd_maps().get("splits", [])
        if saved:
            sm0 = pd.DataFrame(saved)[["Split", "LOB", "Include"]]
            # plain int64, not nullable Int64: extension dtypes crossing the
            # pyarrow bridge in st.data_editor have segfaulted on Linux hosts
            sm0["Split"] = pd.to_numeric(sm0["Split"], errors="coerce").fillna(0).astype("int64")
            sm0["Include"] = sm0["Include"].astype(bool)
            st.session_state.split_map = sm0
        else:
            st.session_state.split_map = pd.DataFrame(columns=["Split", "LOB", "Include"])
    sm = st.session_state.split_map
    for s in splits:
        if s in sm["Split"].values:
            continue
        lob = mapping.lob_for_split(s) if mapping else None
        if lob:
            sm.loc[len(sm)] = [s, lob, True]
        elif mapping:                       # mapping present but this split unmapped
            sm.loc[len(sm)] = [s, UNMAPPED_LOB, False]
        else:                               # no mapping at all — legacy manual default
            sm.loc[len(sm)] = [s, fallback, True]
    st.session_state.split_map = sm

    aux_present = [c for c in AUX_COLS if c in df.columns]
    used = [int(c.replace("i_auxtime", "")) for c in aux_present if df[c].sum() > 0]
    if "aux_map" not in st.session_state:
        saved = load_acd_maps().get("aux", [])
        if saved:
            am0 = pd.DataFrame(saved)[["Code", "Category", "Count as In-Office Shrink"]]
            am0["Code"] = pd.to_numeric(am0["Code"], errors="coerce").fillna(0).astype("int64")
            am0["Count as In-Office Shrink"] = am0["Count as In-Office Shrink"].astype(bool)
            st.session_state.aux_map = am0
        else:
            st.session_state.aux_map = pd.DataFrame(
                columns=["Code", "Category", "Count as In-Office Shrink"])
    am = st.session_state.aux_map
    for code in sorted(used):
        if code not in am["Code"].values:
            am.loc[len(am)] = [code, f"AUX {code}", True]
    st.session_state.aux_map = am


def shrinkage_tables(df: pd.DataFrame):
    sm = st.session_state.split_map
    am = st.session_state.aux_map
    inc_splits = sm[sm["Include"]].copy()
    d = df.merge(inc_splits, left_on="split", right_on="Split", how="inner")

    aux_present = [c for c in AUX_COLS if c in d.columns]
    long = d.melt(
        id_vars=["row_date", "starttime", "LOB", "i_stafftime"],
        value_vars=aux_present, var_name="aux_col", value_name="sec")
    long["Code"] = long["aux_col"].str.replace("i_auxtime", "").astype(int)
    long = long.merge(am, on="Code", how="left")
    long = long[long["Count as In-Office Shrink"].fillna(False)]

    staff_by_lob = d.groupby("LOB")["i_stafftime"].sum()
    cat = long.groupby(["LOB", "Category"])["sec"].sum().reset_index()
    cat["% of Staffed"] = (cat["sec"] / cat["LOB"].map(staff_by_lob) * 100).round(2)
    by_lob = (long.groupby("LOB")["sec"].sum() / staff_by_lob * 100).round(2)

    intr = d.groupby("starttime")["i_stafftime"].sum()
    intr_aux = long.groupby("starttime")["sec"].sum()
    intr = intr[intr >= 3 * 900]  # skip intervals with <3 staffed FTE-intervals (overnight noise)
    intraday = (intr_aux / intr * 100).dropna().rename("In-Office Shrink %")
    # ACD starttime is a military-time integer (HHMM: 0, 15, …, 45, 100 = 1:00 AM),
    # already in the center's local clock. Convert to 12-hour AM/PM labels; the
    # series stays in numeric-starttime order (groupby sorts the ints), and the
    # chart must render with sort=None since AM/PM strings don't sort lexically.
    def _clock(t):
        h, m = int(t) // 100, int(t) % 100
        return f"{h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"
    intraday.index = [_clock(t) for t in intraday.index]

    staff_sec = d["i_stafftime"].sum(min_count=1)
    overall = (long["sec"].sum() / staff_sec * 100
               if pd.notna(staff_sec) and staff_sec > 0 else np.nan)
    return overall, by_lob, cat, intraday


def render_shrinkage_page():
    st.header("ACD In-Office Shrinkage")
    page_help("ACD Shrinkage")
    st.caption(
        "Drop ACD **hsplit** interval exports here (or point at a folder of them). "
        "AUX reason-code time ÷ staffed time = measured in-office shrinkage — "
        "no assumption required. Splits are mapped to LOBs via **Skill_Mapping.csv**."
    )

    # Sources are managed in ONE place — Real Data (remembered locations,
    # plus one-off uploads there). This page just consumes the same sources:
    # remembered location first, app-folder sample as fallback.
    saved = load_data_paths()
    mapping = None
    map_cfg = resolve_data_path("mapping", saved)
    map_src = (map_cfg[0] if map_cfg else None) or _autoload("Skill_Mapping.csv")
    if map_src is not None:
        mapping, mrep = sx.load_mapping(map_src, load_field_maps().get("mapping"))
        _show_report(mrep)
        if mapping is not None:
            st.caption(f"Mapping from remembered location: **{map_cfg[0]}**" if map_cfg
                       else "Auto-loaded **Skill_Mapping.csv** from the app folder.")
    else:
        st.warning("No Skill_Mapping.csv found — splits can't be auto-assigned to LOBs. "
                   "Set its remembered location on the Real Data page; you can "
                   "still assign splits by hand below.")

    s_cfg = resolve_data_path("acd", saved)
    if s_cfg:
        sources = list(s_cfg)
        st.caption(f"Loaded **{len(s_cfg)}** ACD file(s) from the remembered location "
                   "(manage sources on the Real Data page).")
    elif _autoload("split.csv") is not None:
        sources = [_autoload("split.csv")]
        st.caption("Auto-loaded **split.csv** from the app folder.")
    else:
        st.info("No ACD export found. Set the remembered data locations on the "
                "Real Data page (a file, folder, or pattern on the share) — this "
                "page will pick them up automatically.")
        return

    df = load_acd(sources)
    ensure_maps(df, mapping)

    # Surface splits present in the data but absent from the mapping (not dropped).
    if mapping is not None:
        unmapped = sorted(s for s in df["split"].unique()
                          if mapping.lob_for_split(s) is None)
        if unmapped:
            st.warning(
                f"⚠️ {len(unmapped)} split(s) not in Skill_Mapping.csv — excluded by "
                f"default (untick stays off, or assign an LOB below): "
                f"{', '.join(map(str, unmapped))}. Blend/overhead skills belong here; a "
                "real LOB split does not.")

    known_lobs = set(st.session_state.lobs) | set(mapping.lobs if mapping else [])
    lob_options = sorted(known_lobs) + [UNMAPPED_LOB]
    with st.expander("Split → LOB mapping", expanded=bool(mapping is None)):
        st.caption(
            "Auto-seeded from Skill_Mapping.csv. Untick **Include** for blend/duplicate "
            "skills — a blend split logs the same agents twice and double-counts staffed "
            "time. (Identical staffed-time totals across two splits is the tell.)")
        st.session_state.split_map = stable_editor(
            st.session_state.split_map, state_key="split_map_ed", hide_index=True,
            column_config={"LOB": st.column_config.SelectboxColumn(options=lob_options)})

    with st.expander("AUX reason code → category mapping", expanded=False):
        st.caption(
            "Rename codes to your org's categories (Breaks, Lunch, Coaching, IT Issues, "
            "Prep/Messaging, Outbound…). Untick codes that shouldn't count as in-office "
            "shrinkage (e.g. Outbound if it's productive work in your model).")
        st.session_state.aux_map = stable_editor(
            st.session_state.aux_map, state_key="aux_map_ed", hide_index=True)
        st.caption("Renames and toggles here (and in the split mapping above) are "
                   "**remembered** — saved to `acd_maps.json` next to the app, so "
                   "they persist across sessions and apply to the whole team on "
                   "the share.")

    persist_acd_maps()

    overall, by_lob, cat, intraday = shrinkage_tables(df)

    c1, c2, c3 = st.columns(3)
    c1.metric("In-Office Shrinkage (all included splits)",
              f"{overall:.1f}%" if pd.notna(overall) else "—")
    c2.metric("Days loaded", df["row_date"].nunique())
    c3.metric("Splits included", int(st.session_state.split_map["Include"].sum()))

    st.subheader("By LOB")
    st.dataframe(by_lob.rename("In-Office Shrink %").to_frame().T, width="stretch")

    st.subheader("By category (% of staffed time)")
    pivot = cat.pivot_table(index="Category", columns="LOB",
                            values="% of Staffed", aggfunc="sum").fillna(0)
    st.dataframe(pivot.style.format("{:.2f}"), width="stretch")
    st.bar_chart(cat.groupby("Category")["sec"].sum() / 3600)

    st.subheader("Intraday shape")
    import altair as alt
    _intra = intraday.rename_axis("Time").reset_index()
    brand.chart(
        alt.Chart(_intra).mark_line(strokeWidth=2.5, color=brand.CYAN).encode(
            x=alt.X("Time:O", sort=None,
                    axis=alt.Axis(labelAngle=-60, labelOverlap="parity", title=None)),
            y=alt.Y("In-Office Shrink %:Q", title="In-Office Shrink %"),
            tooltip=["Time", alt.Tooltip("In-Office Shrink %:Q", format=".1f")]),
        height=240)
    st.caption(
        "Note: this measures **in-office** shrinkage only — the planner's Shrinkage % "
        "assumption is total (in-office + out-of-office), so update it with OOO layered "
        "on top. Split-level data multi-counts multi-skilled agents across splits; "
        "percentages within a split are sound, but for agent-true LOB totals the ACD "
        "hagent table is the eventual upgrade."
    )


# ----------------------------------------------------------------------
# Real-data page — ACD actuals + calibration
# ----------------------------------------------------------------------
APP_DIR = Path(__file__).parent


def _autoload(name: str):
    """Return an app-folder file of this name if present (for zero-click demo)."""
    p = APP_DIR / name
    return p if p.exists() else None


# Remembered feed locations — the exports always live in the same place on the
# share, so nobody should re-attach them. Stored next to the app (on the share
# that means one config for the whole team), keys: mapping / acd.
DATA_PATHS_FILE = APP_DIR / "data_paths.json"


def load_data_paths() -> dict:
    try:
        return json.loads(DATA_PATHS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_data_paths(paths: dict):
    collab._atomic_write(DATA_PATHS_FILE,
                         json.dumps({k: v for k, v in paths.items() if v}, indent=2))


def resolve_data_path(key: str, saved: dict) -> list[Path]:
    """A remembered location -> existing CSV file(s). Accepts a single file, a
    folder (all *.csv inside), or a glob pattern (e.g. …/acd/2026-*.csv).
    [] when nothing matches (share offline, typo) — callers warn about that
    loudly instead of silently falling back."""
    spec = str(saved.get(key, "") or "").strip()
    if not spec:
        return []
    p = Path(spec).expanduser()
    if not p.is_absolute():
        # A remembered RELATIVE path means "next to the app" — resolving it
        # against the CWD made it depend on where streamlit was launched from
        # (a real bug: the same config worked or failed by accident of `cd`).
        cand = (APP_DIR / p)
        if cand.exists() or not p.exists():
            p = cand
    if p.is_dir():
        return sorted(p.glob("*.csv"))
    if p.is_file():
        return [p]
    import glob
    hits = sorted(Path(h) for h in glob.glob(str(p)) if Path(h).is_file())
    if not hits and not Path(spec).expanduser().is_absolute():
        hits = sorted(Path(h) for h in glob.glob(str(APP_DIR / spec))
                      if Path(h).is_file())
    return hits


# ----------------------------------------------------------------------
# Cached feed loading + boot auto-load
#
# The expensive step is READING + PARSING the exports (the team's real ACD
# file is ~100 MB on a notoriously slow share) — cache THAT per app process,
# keyed on path+mtime+size so any file change busts it. The weekly rollups
# stay live: they depend on per-LOB assumptions and the toggles, and they
# are cheap at weekly grain. st.cache_resource (not cache_data) so the big
# raw frames are SHARED REFERENCES, never per-rerun copies — every consumer
# must treat them as immutable (apply_acd_toggles copies before mutating).
# ----------------------------------------------------------------------
def _files_sig(files) -> tuple:
    """Hashable cache key for feed files: (path, mtime, size) each."""
    out = []
    for f in files:
        fp = Path(f)
        try:
            stt = fp.stat()
            out.append((str(fp), stt.st_mtime_ns, stt.st_size))
        except OSError:
            out.append((str(fp), 0, 0))
    return tuple(out)


@st.cache_resource(show_spinner=False)
def _cached_mapping(sig: tuple, fmap_json: str):
    """(mapping, report) — parsed once per file version per process."""
    return sx.load_mapping(Path(sig[0][0]),
                           json.loads(fmap_json) if fmap_json != "{}" else None)


@st.cache_resource(show_spinner=False)
def _cached_feed_raw(feed: str, sig: tuple, map_sig: tuple, fmap_json: str,
                     _mapping) -> tuple:
    """(raw_df, report, parse_secs, mb) for one interval feed. `_mapping` is
    keyed by `map_sig` (the object itself is unhashable). The returned frame
    is a shared reference — NEVER mutate it."""
    files = [Path(x[0]) for x in sig]
    mb = sum(x[2] for x in sig) / 1e6
    t0 = time.perf_counter()
    df, rep = sx.load_split(files, _mapping,
                            json.loads(fmap_json) if fmap_json != "{}" else None)
    return df, rep, time.perf_counter() - t0, mb


def _feed_sources_from_saved(saved: dict) -> tuple:
    """The page's source precedence WITHOUT uploads: remembered location,
    else the app-folder sample. (map_files, acd_files)."""
    m = resolve_data_path("mapping", saved) or \
        ([_autoload("Skill_Mapping.csv")] if _autoload("Skill_Mapping.csv") else [])
    c = resolve_data_path("acd", saved) or \
        ([_autoload("split.csv")] if _autoload("split.csv") else [])
    return m, c


def _rollups_from_raw(sraw):
    """Weekly rollup from the cached raw ACD frame — recomputed live because it
    depends on per-LOB assumptions and the shrinkage toggles."""
    assume = {lob: d["assumptions"] for lob, d in st.session_state.lobs.items()}
    if sraw is None or sraw.empty:
        return None
    sdf = apply_acd_toggles(sraw)
    return sx.split_weekly(sdf, assume) if not sdf.empty else None


def autoload_feeds():
    """Boot-time feed load (user 2026-07-15): the Executive View must open
    with actuals — visiting Real Data was a hidden per-session toll booth.
    Loads exactly what that page would auto-load; the parse is cached per
    process so a fresh session against unchanged 100 MB exports is instant.
    Perf lands in _feed_perf (MB, parse seconds, wall seconds) so the slow
    share's cost is measured, not guessed; import errors land in _feed_errors
    and surface on the Executive View's data-health band."""
    if "acd_weekly" in st.session_state:
        return
    st.session_state["acd_weekly"] = None
    saved = load_data_paths()
    m_files, s_files = _feed_sources_from_saved(saved)
    if not m_files or not s_files:
        return                          # nothing to load — page explains
    errors, perf = [], {}
    fmaps = load_field_maps()
    t_all = time.perf_counter()
    with st.spinner("Loading feeds from remembered locations…"):
        map_sig = _files_sig(m_files)
        mapping, rep = _cached_mapping(map_sig,
                                       json.dumps(fmaps.get("mapping") or {}))
        errors += rep.errors
        if mapping is not None:
            t0 = time.perf_counter()
            df, rep, secs, mb = _cached_feed_raw(
                "acd", _files_sig(s_files), map_sig,
                json.dumps(fmaps.get("acd") or {}), mapping)
            errors += rep.errors
            perf["ACD"] = {"mb": mb, "parse_s": secs,
                           "wall_s": time.perf_counter() - t0,
                           "files": len(s_files)}
            st.session_state["acd_weekly"] = (_rollups_from_raw(df)
                                              if rep.ok else None)
    perf["total_s"] = time.perf_counter() - t_all
    st.session_state["_feed_perf"] = perf
    st.session_state["_feed_errors"] = errors


def _feed_perf_line():
    """One-line boot-load timing for captions/bands, or None."""
    perf = st.session_state.get("_feed_perf")
    if not perf:
        return None
    bits = []
    for label in ("WFM", "ACD"):
        if label in perf:
            e = perf[label]
            cached = e["wall_s"] < max(0.2, e["parse_s"] * 0.5)
            bits.append(f"{label} {e['mb']:.0f} MB in "
                        + (f"{e['wall_s']:.1f}s (cache)" if cached
                           else f"{e['parse_s']:.1f}s"))
    if not bits:
        return None
    return (" · ".join(bits)
            + f" · boot load {perf.get('total_s', 0):.1f}s")


def _show_report(rep):
    """Render an ImportReport (from sources.py or data_io.py) to the page."""
    for e in rep.errors:
        st.error(e)
    for w in rep.warnings:
        st.warning(w)
    for n in rep.notes:
        st.caption("ℹ️ " + n)


def render_real_data_page():
    st.header("Real Data — ACD actuals")
    page_help("Real Data")
    st.caption(
        "The ACD export measures what actually happened: contacts offered and "
        "answered, handle time, service level, abandons, staffed time and "
        "shrinkage. This page maps it to your lines of business, checks it "
        "against the planner's model, and lets you replace guessed "
        "AHT/shrinkage with measured values. The WFM forecast feed was retired "
        "on 2026-07-31 — it is an OPERATIONAL plan, and setting it beside a "
        "long-range capacity plan invited comparisons that confused more than "
        "they informed."
    )

    # 0 — Remembered locations (set once, loads every session) -------------
    saved = load_data_paths()
    with st.expander("Remembered data locations", expanded=not saved):
        st.caption(
            "Point each feed at its permanent home — a **file**, a **folder** of "
            "CSVs, or a **pattern** like `/Volumes/ACD/2026-*.csv`. Saved to "
            "`data_paths.json` next to the app, so on the share the whole team gets "
            "them and nobody re-attaches files. Uploads below remain one-off overrides.")
        p_map = st.text_input("Skill mapping file", value=saved.get("mapping", ""),
                              placeholder="/Volumes/WFM/Skill_Mapping.csv",
                              key="dp_mapping")
        p_acd = st.text_input("ACD split export — file / folder / pattern",
                              value=saved.get("acd", ""), key="dp_acd")
        if st.button("Remember these locations", key="dp_save"):
            # The retired "wfm" key is deliberately NOT carried forward: a
            # saved path for a feed nothing reads is a trap for whoever next
            # wonders why editing it changes nothing.
            save_data_paths({"mapping": p_map.strip(), "acd": p_acd.strip()})
            st.success("Saved — these feeds now load automatically every session.")
            st.rerun()

    # 0c — Holiday closures (field finding 2026-07-16: closed days made
    # legitimate weeks look like truncated exports) ------------------------
    with st.expander("Holiday closures"):
        st.caption(
            "Days the center is CLOSED, org-wide. A week containing a closure "
            "legitimately covers one day fewer than the queue's norm — listing "
            "it here keeps that week counted as FULL in benchmarks, variance, "
            "and seasonality (the holiday dip is a real recurring shape) "
            "instead of excluded as a truncated export. One date per line "
            "(YYYY-MM-DD); paste a column straight from Excel. Team-shared "
            "(`holidays.json` next to the app).")
        _txt = st.text_area("Closure dates", value="\n".join(load_holidays()),
                            key="holidays_txt", height=140,
                            label_visibility="collapsed",
                            placeholder="2026-11-26\n2026-12-25")
        if st.button("Save holiday closures", key="holidays_save"):
            good, bad = [], []
            for ln in _txt.splitlines():
                t = ln.strip()
                if not t:
                    continue
                try:
                    good.append(date.fromisoformat(t).isoformat())
                except ValueError:
                    bad.append(t)
            if bad:
                st.error("Not YYYY-MM-DD date(s): " + ", ".join(bad[:5])
                         + ("…" if len(bad) > 5 else "") + " — nothing saved.")
            else:
                st.session_state["holidays"] = sorted(set(good))
                try:
                    collab._atomic_write(
                        _holidays_path(),
                        json.dumps(st.session_state["holidays"], indent=2))
                    st.success(f"Saved {len(good)} closure date(s) — applied "
                               "everywhere immediately.")
                except OSError as exc:
                    st.warning("Applied this session, but couldn't save to "
                               f"the share ({exc.__class__.__name__}) — will "
                               "retry when it's back.")

    # 0b — Column mapping (vendor-agnostic ingestion) ----------------------
    def _render_column_mapper(m_files, s_files):
        """Map any vendor's headers onto the app's canonical fields. Ships
        empty: the current ACD export resolves by name or alias with no
        mapping at all. Needed when a system's headers differ (e.g. a NICE IEX
        export). Renders BEFORE the feeds load, so a file whose headers don't
        match can still be fixed from here — being locked out of the mapper by
        the very file it maps was a real bug (2026-07-13)."""
        fmaps = load_field_maps()
        feeds = [("mapping", "Skill-mapping file (split/queue → LOB)", m_files),
                 ("acd", "ACD / actuals feed", s_files)]
        broken = []
        for feed, _, files in feeds:
            if not files:
                continue
            hdrs = sx.peek_headers(files)
            if not hdrs:
                continue
            probe = pd.DataFrame(columns=hdrs)
            _, _, missing, _ = sx.resolve_columns(probe, feed, fmaps.get(feed))
            if missing:
                broken.append((feed, missing))
        if broken:
            st.error("Unmapped required field(s): "
                     + " · ".join(f"**{f}** → "
                                  + ", ".join(sx.field_label(x) for x in m)
                                  for f, m in broken)
                     + ". Open Column mapping below and point them at this "
                       "file's columns.")
        with st.expander("Column mapping — use any system's export",
                         expanded=bool(broken)):
            st.caption(
                "The app works in canonical field names. Any export (ACD, "
                "NICE IEX, a hand-built CSV) works once its columns are mapped "
                "here — **map once, remembered for the team** (`field_maps.json`). "
                "Blank = auto-detect by name/alias, which is why the current "
                "exports need no mapping. Extra columns are always ignored.")
            new_maps = dict(fmaps)
            for feed, label, files in feeds:
                st.markdown(f"**{label}**")
                if not files:
                    st.caption("_No file loaded for this feed yet._")
                    continue
                hdrs = sx.peek_headers(files)
                probe = pd.DataFrame(columns=hdrs)
                _, resolved, missing, auto = sx.resolve_columns(
                    probe, feed, fmaps.get(feed))
                saved = dict(fmaps.get(feed) or {})
                rows = []
                label_to_canon = {}
                for canon, required, _al in sx.FIELDS[feed]:
                    cur = saved.get(canon) or resolved.get(canon) or ""
                    how = ("saved" if saved.get(canon) else
                           "auto" if canon in resolved else
                           ("MISSING" if required else "—"))
                    # Plain-English field names (user 2026-07-15) — planners
                    # shouldn't decode vendor-column jargon like Forecasted_CV.
                    label_to_canon[sx.field_label(canon)] = canon
                    rows.append({"Field": sx.field_label(canon),
                                 "Required": "yes" if required else "no",
                                 "Source column": cur, "Status": how})
                ed = st.data_editor(
                    pd.DataFrame(rows), hide_index=True, width="stretch",
                    disabled=["Field", "Required", "Status"],
                    key=f"fieldmap_{feed}",
                    column_config={"Source column": st.column_config.SelectboxColumn(
                        options=[""] + hdrs,
                        help="Pick the column in YOUR export that carries this "
                             "field. Leave blank to auto-detect.")})
                new_maps[feed] = {label_to_canon[r["Field"]]: r["Source column"]
                                  for _, r in ed.iterrows() if r["Source column"]}
            if st.button("Save column mapping", key="fieldmap_save"):
                save_field_maps(new_maps)
                st.success("Saved — this export's layout is now remembered.")
                st.rerun()

    # 1 — Skill mapping (the join table the feed depends on) ---------------
    st.subheader("1 · Skill mapping")
    map_up = st.file_uploader("Skill_Mapping.csv", type="csv", key="map_upl")
    map_cfg = resolve_data_path("mapping", saved)
    _render_column_mapper(
        [map_up] if map_up else map_cfg,
        [s for s in ([_up for _up in (st.session_state.get("s_upl") or [])]
                     or resolve_data_path("acd", saved)) if s is not None])
    if map_up is None and saved.get("mapping") and not map_cfg:
        st.warning(f"Remembered mapping location matches no file — share offline or "
                   f"path moved? `{saved['mapping']}`")
    map_src = map_up or (map_cfg[0] if map_cfg else None) or _autoload("Skill_Mapping.csv")
    if map_src is None:
        st.info("Upload Skill_Mapping.csv (columns: Skill_ID, Line_of_Business, Queue_Name).")
        return
    if map_up is None:
        st.caption(f"Loaded from remembered location: **{map_cfg[0]}**" if map_cfg
                   else "Auto-loaded **Skill_Mapping.csv** from the app folder.")
    if map_up is None:
        _msig = _files_sig([map_src])
        mapping, rep = _cached_mapping(
            _msig, json.dumps(load_field_maps().get("mapping") or {}))
    else:
        _msig = None                       # uploads bypass the cache entirely
        mapping, rep = sx.load_mapping(map_src, load_field_maps().get("mapping"))
    _show_report(rep)
    if not rep.ok:
        return
    st.write("**Lines of business:** " + ", ".join(mapping.lobs))

    # 2 — Load the interval feed ------------------------------------------
    st.subheader("2 · Load feed")
    s_up = st.file_uploader("ACD split export(s)", type="csv",
                            accept_multiple_files=True, key="s_upl")
    s_cfg = resolve_data_path("acd", saved)
    if not s_up and saved.get("acd") and not s_cfg:
        st.warning(f"Remembered ACD location matches no file: `{saved['acd']}`")
    s_src = list(s_up) if s_up else (s_cfg or ([_autoload("split.csv")]
                                               if _autoload("split.csv") else []))
    if not s_up and s_src:
        st.caption(f"Loaded **{len(s_cfg)}** ACD file(s) from remembered location."
                   if s_cfg else "Auto-loaded **split.csv**.")
    if not s_src:
        st.info("Upload an ACD export to continue.")
        return

    # Gross-up assumptions come from matching model LOBs (else module defaults).
    assume = {lob: d["assumptions"] for lob, d in st.session_state.lobs.items()}

    fmaps = load_field_maps()
    sw = None
    if s_up or _msig is None:              # uploads bypass the cache
        sdf, r = sx.load_split(s_src, mapping, fmaps.get("acd"))
    else:
        sdf, r, _secs, _mb = _cached_feed_raw(
            "acd", _files_sig(s_src), _msig,
            json.dumps(fmaps.get("acd") or {}), mapping)
        st.caption(f"{_mb:.0f} MB parsed in {_secs:.1f}s — cached until "
                   "the file changes.")
    _show_report(r)
    if r.ok:
        sdf = apply_acd_toggles(sdf)
        if sdf.empty:
            st.warning("All ACD splits are excluded by the Shrinkage page "
                       "mapping — nothing to roll up.")
        sw = sx.split_weekly(sdf, assume) if not sdf.empty else None
        st.caption("ACD numbers here honor the split include/exclude and AUX "
                   "shrink toggles from the Shrinkage page — one shrinkage "
                   "truth everywhere.")

    # Persist so the Capacity Plan chart can overlay these benchmarks.
    st.session_state["acd_weekly"] = sw

    # 3 — Combined benchmark ---------------------------------------------
    st.subheader("3 · Plan vs. actuals")
    # ONE feed now, so no merge — and with it the whole duplicate-week bug
    # class disappears structurally rather than by careful joining: one feed
    # means one coverage figure means one row per LOB+week. The old code
    # outer-joined the WFM and ACD rollups and had to reconcile two
    # independently-computed "Days Covered" (field report 2026-07-30, where a
    # 6-day and a 5-day export split one week onto two lines).
    comb = sw
    if comb is None or comb.empty:
        st.warning("No rows to show after mapping.")
        return

    _norm = comb.groupby("LOB")["Days Covered"].transform(
        lambda x: x.mode().max())   # each queue's normal operating days/week
    _norm = _norm - _holiday_allowance(comb["Week"])   # closures aren't partial
    if (comb["Days Covered"] < _norm).any():
        st.warning(
            "⚠️ Some weeks are **truncated** — fewer days than that queue normally "
            "operates (see *Days Covered*; a 5-day queue's complete week is 5). "
            "Totals for those weeks reflect only the days present.")

    # The FTE-requirement benchmark picker went with the WFM feed: both of
    # its options were built on that forecast (its interval Required_FTE, or
    # the plan's workload math applied to its forecast volume). What remains
    # is the comparison that was always the honest one — the plan's OWN
    # requirement against measured staffing.
    plan_req = {}
    for _lob, _d in st.session_state.lobs.items():
        _pf = compute_plan(_d)
        plan_req[_lob] = dict(zip(_pf["Week"], _pf["Required FTE"]))
    comb["Plan Required FTE"] = [
        round(plan_req.get(r["LOB"], {}).get(r["Week"], np.nan), 1)
        for _, r in comb.iterrows()]
    comb["Net (staffed − required)"] = (
        comb["Actual Staffed FTE"] - comb["Plan Required FTE"]).round(1)

    show_cols = ["LOB", "Week", "Days Covered", "Actual Contacts",
                 "Actual AHT (sec)", "Actual SL %", "Abandon %",
                 "Actual ASA (sec)", "Occupancy % (actual)",
                 "Plan Required FTE", "Actual Staffed FTE",
                 "In-Office Shrink %", "Net (staffed − required)"]
    show = comb[[c for c in show_cols if c in comb.columns]]
    fmt = {c: "{:,.0f}" for c in ["Actual Contacts", "Actual AHT (sec)",
                                  "Actual ASA (sec)"] if c in show.columns}
    fmt.update({c: "{:,.1f}" for c in
                ["Plan Required FTE", "Actual Staffed FTE", "In-Office Shrink %",
                 "Actual SL %", "Abandon %", "Occupancy % (actual)",
                 "Net (staffed − required)"] if c in show.columns})
    st.dataframe(show.style.format(fmt, na_rep="—"), hide_index=True, width="stretch")
    st.caption("Net = Actual Staffed FTE − the PLAN's Required FTE for that "
               "week. Weeks outside the plan horizon show a dash rather than a "
               "comparison the plan cannot make.")
    if "Plan Required FTE" in comb.columns:
        chart = comb.groupby("LOB")[["Plan Required FTE", "Actual Staffed FTE"]] \
            .sum(min_count=1)   # all-missing stays a gap, never a 0-height bar
        st.bar_chart(chart)

    # 4 — Calibrate the model --------------------------------------------
    st.subheader("4 · Calibrate the model from measured data")
    agg = {}
    if "Actual AHT (sec)" in comb:
        agg["Measured AHT (sec)"] = ("Actual AHT (sec)", "mean")
    if "In-Office Shrink %" in comb:
        agg["Measured In-Office Shrink %"] = ("In-Office Shrink %", "mean")
    meas = comb.groupby("LOB").agg(**agg).round(1) if agg else pd.DataFrame()

    missing = [l for l in mapping.lobs if l not in st.session_state.lobs]
    if missing:
        st.caption(f"These mapped LOBs aren't in the model yet: {', '.join(missing)}")
        if st.button(f"Create {len(missing)} model LOB(s) from mapping"):
            for l in missing:
                aht = 400.0
                if not meas.empty and "Measured AHT (sec)" in meas and l in meas.index \
                        and pd.notna(meas.loc[l, "Measured AHT (sec)"]):
                    aht = float(meas.loc[l, "Measured AHT (sec)"])
                st.session_state.lobs[l] = make_blank_lob(st.session_state.n_weeks, aht)
            st.success(f"Created {len(missing)} LOB(s). Enter Members & CPM on the "
                       "Capacity Plan page.")
            st.rerun()

    if not meas.empty:
        st.dataframe(meas, width="stretch")
        st.caption(
            "In-office shrink is AUX ÷ staffed time — it excludes out-of-office "
            "(PTO, absence). Layer OOO on top before trusting it as total shrinkage.")
        in_model = [l for l in meas.index if l in st.session_state.lobs]
        if in_model and st.button("Apply measured AHT & shrinkage to matching model LOBs"):
            applied = []
            for lob in in_model:
                a = st.session_state.lobs[lob]
                if "Measured AHT (sec)" in meas and pd.notna(meas.loc[lob, "Measured AHT (sec)"]):
                    a["demand"]["AHT (sec)"] = round(float(meas.loc[lob, "Measured AHT (sec)"]))
                if "Measured In-Office Shrink %" in meas and pd.notna(
                        meas.loc[lob, "Measured In-Office Shrink %"]):
                    a["assumptions"]["shrinkage_pct"] = round(
                        float(meas.loc[lob, "Measured In-Office Shrink %"]), 1)
                applied.append(lob)
            st.success(f"Applied measured AHT/shrinkage to: {', '.join(applied)}.")
            st.rerun()


# ----------------------------------------------------------------------
# Collaboration — team plan status, edit control, publish, history
# ----------------------------------------------------------------------
def _hm(iso: str) -> str:
    return iso[11:16] if iso and len(iso) >= 16 else iso


def _default_lob(names: list[str]) -> str:
    """The LOB a session should open on: the largest line by starting HC (the
    org's main queue — historically the interim donor and default view). Data-
    driven so no LOB name is hard-coded (2026-07-16, public-shell build)."""
    lobs = st.session_state.get("lobs") or {}
    best, best_hc = names[0] if names else "", -1.0
    for n in names:
        hc = float((lobs.get(n, {}).get("assumptions") or {}).get("starting_hc", 0) or 0)
        if hc > best_hc:
            best, best_hc = n, hc
    return best


def render_team_status() -> tuple[bool, str]:
    """Sidebar: show the active plan + who holds edit control, and the
    take-control / take-over / sandbox buttons. Returns (editable, mode) where
    mode is 'editor' | 'sandbox' | 'viewer'."""
    user = st.session_state.user
    st.caption(f"{user}")

    # --- which plan year am I working in? -----------------------------
    # Only a control when there is a choice; a one-year team gets a caption
    # instead of a dropdown that does nothing. Seeding `year_pick` BEFORE the
    # widget instantiates is the allowed pattern (same as `_nav_goto` staging
    # `nav_page`); passing a written-back value as `index=` is what causes the
    # two-clicks bug, so it is never passed.
    _years = _available_years()
    if len(_years) > 1:
        # plan_year changes from BOTH directions and the two must not fight:
        # the planner picks it here, but `roll_over_plan` sets it to yr+1 and
        # `_apply_payload` takes it from any snapshot or resumed draft. Guarding
        # only on "value not in the list" let the widget's stale value win, so
        # rolling into 2027 was yanked straight back to 2026 on the next run.
        # Remembering the plan_year we last rendered with separates the cases:
        # if it moved underneath us, follow it; otherwise the widget is truth.
        if st.session_state.get("_year_seen") != _plan_year():
            st.session_state["year_pick"] = _plan_year()
            st.session_state["_year_seen"] = _plan_year()
        _picked = st.selectbox("Plan year", _years, key="year_pick",
                               format_func=str,
                               help="Each year is its own plan with its own "
                                    "published versions and its own edit lock — "
                                    "editing one never locks the other.")
        if int(_picked) != _plan_year():
            _switch_year(int(_picked))
    else:
        st.caption(f"Plan year **{_plan_year()}**")

    year = _plan_year()
    # Edit control belongs to ONE year, so remembering "I was the editor" has
    # to as well — a single flag fired a spurious takeover warning the moment
    # you switched years.
    _we_key = f"was_editor_{year}"
    lock = collab.read_lock(SCENARIO_DIR, year)
    act = collab.read_active(SCENARIO_DIR, year)
    # both spellings: edit.lock.corrupt-* (pre-per-year) and edit-<year>.lock.corrupt-*
    _corrupt = sorted(Path(SCENARIO_DIR).glob("edit*.lock.corrupt-*"))
    if _corrupt:
        st.caption(f"⚠️ A corrupted lock file was set aside as "
                   f"`{_corrupt[-1].name}` (crash/share hiccup mid-write). "
                   "Editing works normally; delete the file after a look.")
    # Ownership is SESSION-level (user + acquisition token, audit 2026-07-14):
    # user alone let two tabs of the same Windows login both edit silently.
    tok = st.session_state.get(f"lock_token_{year}")
    i_edit = collab.owns_lock(lock, user, tok)

    # Holding two years at once is ALLOWED — they are different plans, so it is
    # not a write conflict — but it is not free: everyone else is read-only on
    # every year you hold. Not forbidden, made visible; the same reasoning as
    # every other warning here (an honest notice beats a silent guess, and
    # auto-releasing a year just because you glanced at another would be a
    # surprise that costs someone their edit rights mid-thought).
    _also = [y for y in _years if y != year and collab.owns_lock(
        collab.read_lock(SCENARIO_DIR, y), user,
        st.session_state.get(f"lock_token_{y}"))]
    if _also:
        st.caption("You also hold edit control of "
                   + ", ".join(str(y) for y in _also)
                   + " — switch there and release it if someone else needs "
                     "that year.")

    # We thought we were editing but the lock moved → someone took over.
    if st.session_state.get(_we_key) and not i_edit and not st.session_state.sandbox:
        st.warning(
            f"⚠️ Edit control for **{year}** was taken over by "
            f"**{lock.get('user') if lock else '—'}**. "
            "Your unsaved changes are still in this session — switch to Sandbox and "
            "save them as a personal what-if to keep them.")
        st.session_state[_we_key] = False

    st.markdown(f"**{year} plan:** "
                + (f"v{act['version']} · {act['name']}" if act else "_none published yet_"))
    if act:
        st.caption(f"by {act['author']} · {_hm(act['published_at'])}"
                   + (f" · {act['note']}" if act.get("note") else ""))

    # Drift: a newer version was published than the one we're viewing.
    lv = st.session_state.get("loaded_version")
    if act and lv is not None and act["version"] > lv and not st.session_state.sandbox and not i_edit:
        st.info(f"The {year} plan advanced to v{act['version']} (you're on v{lv}).")
        if st.button("Reload active plan", width="stretch", key="reload_active"):
            _load_active_into_session()
            st.rerun()

    if st.button("Check for updates", width="stretch", key="refresh_collab"):
        st.rerun()

    # --- Sandbox mode -------------------------------------------------
    if st.session_state.sandbox:
        st.success("**Sandbox** — private what-if. Nothing here touches the team plan.")
        if st.button("Exit sandbox → active plan", width="stretch"):
            st.session_state.sandbox = False
            if not _load_active_into_session():
                st.session_state.loaded_version = None
            st.rerun()
        return True, "sandbox"

    # --- We hold edit control ----------------------------------------
    if i_edit:
        if not collab.heartbeat(SCENARIO_DIR, year, user, tok):
            st.session_state[_we_key] = False   # lost it between read & now
            st.rerun()
        st.session_state[_we_key] = True
        st.success(f"You have **edit control of {year}** "
                   f"(since {_hm(lock.get('acquired_at',''))}).")
        if st.button("Release edit control", width="stretch"):
            collab.release_lock(SCENARIO_DIR, year, user, tok)
            st.session_state.pop(f"lock_token_{year}", None)
            st.session_state[_we_key] = False
            st.rerun()
        return True, "editor"

    # --- Someone else holds it (fresh) -------------------------------
    if lock and not collab.lock_is_stale(lock):
        if lock.get("user") == user:
            st.warning(f"Your edit lock on **{year}** belongs to **another "
                       "session/tab** (or a previous run). Take control to edit "
                       "HERE — the other session becomes read-only.")
        else:
            st.warning(f"**{lock.get('user')}** is editing **{year}** "
                       f"(since {_hm(lock.get('acquired_at',''))}, "
                       f"active {int(collab.age_min(lock.get('heartbeat','')))}m ago). "
                       "You're read-only.")
    elif lock:
        st.caption(f"Stale lock on {year} from {lock.get('user')} — free to take.")
    else:
        st.caption(f"The {year} plan is unlocked.")

    c1, c2 = st.columns(2)
    take_label = "Take over" if (lock and not collab.lock_is_stale(lock)) else "Take control"
    if c1.button(take_label, width="stretch"):
        ok, info = collab.acquire_lock(SCENARIO_DIR, year, user, force=True)
        st.session_state[f"lock_token_{year}"] = (info or {}).get("token")
        # Re-seed from the shared truth ONLY when it actually moved under us
        # (a colleague published while we were viewing). Reloading the version
        # we are already on is not the no-op it looks like: _apply_payload
        # overwrites every frame, so it silently discarded work held in the
        # session — the other half of the vanishing-new-hires report
        # (2026-07-31). Same-version case: keep what we have.
        _act = collab.read_active(SCENARIO_DIR, year)
        if _act and _act.get("version") != st.session_state.get("loaded_version"):
            _load_active_into_session()
        st.session_state[_we_key] = True
        st.rerun()
    if c2.button("Sandbox", width="stretch"):
        st.session_state.sandbox = True
        st.rerun()
    return False, "viewer"


def render_publish_panel(mode: str):
    """Sidebar: publish (editor), save personal (sandbox), version history."""
    st.divider()
    user = st.session_state.user
    if mode == "editor":
        st.subheader("Publish")
        default_name = (collab.read_active(SCENARIO_DIR, _plan_year())
                        or {}).get("name", "Current Outlook")
        name = st.text_input("Plan name", value=default_name, key="pub_name")
        # REQUIRED (manager ask 2026-08-01). The Change log computes WHAT
        # changed from the snapshots themselves; this line is the only place
        # WHY can come from, and an optional field would be blank on exactly
        # the versions anyone later needs explained.
        note = st.text_input(
            "What changed? (note) *", key="pub_note",
            help="Required — one line on WHY, e.g. 'adopted measured attrition "
                 "after the August reconciliation'. The Change log page lists "
                 "every field that moved, so this only has to carry the reason.")
        parent = st.session_state.get("loaded_version")
        c1, c2 = st.columns(2)
        for label, col, release in [("Publish", c1, False), ("Publish & release", c2, True)]:
            if col.button(label, width="stretch", key=f"pub_{label}"):
                if not (note or "").strip():
                    st.error("Add a one-line note on what changed — it is the "
                             "'why' column of the Change log, and nothing else "
                             "can supply it.")
                elif not collab.holds_lock(SCENARIO_DIR, _plan_year(), user,
                                           st.session_state.get(f"lock_token_{_plan_year()}")):
                    st.error("You no longer hold edit control — can't publish.")
                else:
                    meta, _ = collab.publish(SCENARIO_DIR, _serialize_lobs(),
                                             name, user, parent, note)
                    st.session_state.loaded_version = meta["version"]
                    if release:
                        collab.release_lock(
                            SCENARIO_DIR, _plan_year(), user,
                            st.session_state.get(f"lock_token_{_plan_year()}"))
                        st.session_state.pop(f"lock_token_{_plan_year()}", None)
                        st.session_state[f"was_editor_{_plan_year()}"] = False
                    st.success(f"Published v{meta['version']}.")
                    st.rerun()
    elif mode == "sandbox":
        st.subheader("Sandbox")
        name = st.text_input("What-if name", value="my what-if", key="sb_name")
        if st.button("Save as personal what-if", width="stretch"):
            _, f = collab.save_personal(SCENARIO_DIR, _serialize_lobs(), name, user)
            st.success(f"Saved → {f}")

    _yr = _plan_year()
    with st.expander(f"Version history — {_yr}"):
        # Scoped to the selected year on purpose. Restore republishes, and
        # publish() takes its year from the payload — so restoring a 2027 entry
        # while you are looking at 2026 would advance the 2027 pointer. Correct,
        # but nobody would predict it from a list that mixed the years together.
        # Sandbox any year's version from its own year; the button below still
        # opens it privately without moving anything.
        log = collab.changelog(SCENARIO_DIR, _yr)
        if not log:
            st.caption(f"No {_yr} versions published yet.")
        for j in log[:15]:
            # Caption on its OWN row, buttons beneath. Three columns in the
            # sidebar left each button about a fifth of the rail, too narrow for
            # the word "Sandbox" — Streamlit then broke the label one character
            # per line, so the button rendered as a vertical column of letters.
            # The year is already in the expander title; drop it from every row.
            st.caption(f"**v{j['version']}** · {j.get('name','')}"
                       f" · {j.get('author','')} · {_hm(j.get('published_at',''))}"
                       + (f" — {j['note']}" if j.get("note") else ""))
            cols = st.columns(2)
            # Keys come from the FILENAME, not the version number. `next_version`
            # is global and monotonic so numbers are unique in practice, but only
            # by convention — two files claiming one number (a hand-copied
            # snapshot, a folder merged by hand) made Streamlit raise on the
            # duplicate key and took down the WHOLE SIDEBAR, not just this panel.
            # A filename is unique by construction. Found 2026-08-02 by a check
            # that hand-wrote a snapshot and hit it accidentally.
            _k = j["_file"]
            # View/edit any version privately — e.g. revisit 2026 while the
            # team's active plan is 2027 — without touching the shared pointer.
            if cols[0].button("Sandbox", key=f"sb_open_{_k}",
                              width="stretch",
                              help="Open this version privately. Nothing shared "
                                   "changes; publish deliberately if you want it live."):
                st.session_state.sandbox = True
                _apply_payload(j)
                st.session_state.loaded_version = None
                st.rerun()
            if mode == "editor" and cols[1].button("Restore", width="stretch",
                                                   key=f"restore_{_k}"):
                payload = {k: j[k] for k in ("n_weeks", "members_start", "members_end", "lobs")}
                payload["plan_year"] = j.get("plan_year", DEFAULT_PLAN_YEAR)
                meta, _ = collab.publish(SCENARIO_DIR, payload, j.get("name", "restored"),
                                         user, st.session_state.get("loaded_version"),
                                         note=f"restored from v{j['version']}")
                _apply_payload(meta)
                st.session_state.loaded_version = meta["version"]
                st.rerun()

    with st.expander("How the team plan works"):
        st.markdown(
            "- **Each plan year is its own plan** — its own published versions "
            "and its own edit lock. What you see is the latest published version "
            "of the year picked at the top of this rail.\n"
            "- So someone can build **next year** while this year stays the "
            "operating plan. Editing one year never locks the other, and "
            "publishing one never touches the other's versions.\n"
            "- Edit control is **best-effort single-writer**, per year: conflicts "
            "are detected and warned (takeovers, second tabs), not made physically "
            "impossible — publish deliberately and heed the warnings.\n"
            "- To change a year: **Take control** (so two people can't edit the "
            "same year at once), make your edits, then **Publish** — that saves a "
            "new version everyone sees.\n"
            "- You *can* hold two years at once; the rail tells you when you do. "
            "Release a year you're done with, or everyone else stays read-only "
            "on it.\n"
            "- **Sandbox** is your private copy. Experiment freely — nothing is "
            "shared unless you publish it.\n"
            "- **Version history** keeps every published version forever. Open any "
            "old one in Sandbox to look at it, or Restore it to make it the team "
            "plan again. Nothing can be lost or overwritten.\n"
            "- Your edits **auto-save as a personal draft** — if you close the "
            "browser before publishing, you'll be offered them back next time.")

    with st.expander("Branding"):
        _ident = brand.identity()
        st.caption("The wordmark in the masthead. Everyone launching from this "
                   "folder sees it.")
        _m = st.text_input("Mark (1–4 characters)", _ident["mark"],
                           max_chars=4, key="brand_mark")
        _w = st.text_input("Wordmark", _ident["word"], max_chars=40, key="brand_word")
        _s = st.text_input("Strapline", _ident["sub"], max_chars=60, key="brand_sub")
        _bc1, _bc2 = st.columns(2)
        if _bc1.button("Save branding", key="brand_save"):
            brand.save_identity(_m, _w, _s)
            st.rerun()
        if _bc2.button("Reset to default", key="brand_reset"):
            brand.reset_identity()
            # Same reason _purge_assumption_widgets() exists: these inputs are
            # keyed, so their session values survive the rerun and the boxes
            # would still show the old wordmark after the file is gone. Drop
            # the keys and let them re-seed from brand.identity().
            for _k in ("brand_mark", "brand_word", "brand_sub"):
                st.session_state.pop(_k, None)
            st.rerun()

    mine = collab.personal_snapshots(SCENARIO_DIR, user)
    if mine:
        with st.expander("My what-ifs"):
            labels = {f"{j.get('name')} · {_hm(j.get('published_at',''))}": j for j in mine}
            pick = st.selectbox("Load one (opens in sandbox)", list(labels), key="load_whatif")
            if st.button("Load what-if"):
                st.session_state.sandbox = True
                _apply_payload(labels[pick])
                st.rerun()


# ----------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------
if "user" not in st.session_state:
    st.session_state.user = collab.who()
    st.session_state.sandbox = False
    # was_editor is per-year (was_editor_<year>) — nothing to seed, the reads
    # go through .get() and a missing key is correctly "I was not editing".
if "lobs" not in st.session_state:
    if not _load_active_into_session():   # adopt the shared active plan if one exists
        init_state(52)                    # else start from the mapped-LOB default
        st.session_state.loaded_version = None
    _startup_draft_check()
# Feeds load at boot (cached parse) so the Executive View opens with actuals —
# visiting Real Data must never be a per-session prerequisite (2026-07-15).
autoload_feeds()

ALL_PAGES = ["Executive View", "Capacity Plan", "Hiring Advisor",
             "Budget", "Real Data", "ACD Shrinkage", "Change log", "Guide"]

# ---- Decommissioned pages: hidden from the product, kept in the code --------
# Phased rollout (user decision 2026-07-29): show the plan first and land the
# solver as a LATER delivery, rather than presenting everything at once.
#
# HIDE, never delete. `render_advisor_page`, `recommend_classes`,
# `shortfall_windows` and `donor_after_pull` are untouched, so reviving the page
# is emptying this set — nothing to rebuild, no migration.
#
# Hiding a page is not just dropping it from the nav. Anything that POINTS at it
# has to go too, or the app grows exactly the dead surfaces this app is careful
# not to have: Guide recipes whose "Go to" button targets a page that no longer
# exists, a checklist telling you to open it, and the four sidebar cadence
# controls that only `recommend_classes` ever reads. Each is gated below on this
# same set — grep HIDDEN_PAGES to find every one.
#
# CP_SHOW_ALL_PAGES=1 unhides everything. `hiring_cadence_check.py` drives the
# real page through the nav radio, and a hidden feature whose proof was deleted
# is a feature that rots quietly until the day you try to bring it back.
HIDDEN_PAGES = (frozenset() if os.environ.get("CP_SHOW_ALL_PAGES")
                else frozenset({"Hiring Advisor"}))

PAGES = [p for p in ALL_PAGES if p not in HIDDEN_PAGES]

# Site masthead — wordmark, then nav, then content: traditional site order, and
# it now renders on EVERY page (it used to live inside the Executive View, so
# six of seven pages had no header at all).
#
# It READS `nav_page` and never writes it. On a rerun the key already holds the
# newly clicked page before the radio instantiates, so this is the current page,
# not last run's. Writing it here would raise StreamlitAPIException.
#
# Only facts that are settled THIS EARLY belong in the meta line. LOB count and
# horizon are set by sidebar widgets hundreds of lines further down, so they
# would render one run stale — they stay on the Executive View instead.
brand.header(st.session_state.get("nav_page", PAGES[0]),
             f"Plan year {plan_year()} · {datetime.now():%b %d, %Y}")

# Navigation lives at the TOP OF THE MAIN BODY, styled as a site nav bar. It is
# still a radio, deliberately: the widget TYPE is what keeps `nav_page` stable
# and what the AppTest checks reach for (`at.radio`, key "nav_page"). Tabs would
# render every page's body on every rerun — the hiring solver included.
#
# Two rules this block must keep obeying:
#   * The radio owns `nav_page` as its KEY, so its identity is stable and a
#     click registers first time. Feeding a write-back value in as `index` on
#     an unkeyed radio churns the identity → the two-click bug (twice bitten).
#   * Deep links can't write a widget's key AFTER it renders, so they stage the
#     target in `_nav_goto`, applied here BEFORE instantiation.
#
# The label strings are load-bearing: PAGE_HELP, RECIPE_PAGE and every AppTest
# check match on these exact strings. Renaming a page means updating all three.
if "_nav_goto" in st.session_state:
    st.session_state["nav_page"] = st.session_state.pop("_nav_goto")
with st.container(key="ccnav"):
    page = st.radio("Page", PAGES, key="nav_page", label_visibility="collapsed",
                    horizontal=True)

with st.sidebar:
    st.title("Capacity Planner")
    st.divider()

    if st.session_state.get("_draft_pending"):
        _d = st.session_state["_draft_pending"]
        st.warning("You have **unsaved changes** from "
                   f"{str(_d.get('saved_at', '?'))[:16].replace('T', ' ')} that were "
                   "never published.")
        _c1, _c2 = st.columns(2)
        if _c1.button("Resume them", width="stretch", key="draft_resume"):
            _apply_payload(_d["payload"])
            st.session_state.sandbox = bool(_d.get("sandbox"))
            st.session_state.loaded_version = _d.get("base_version")
            st.session_state["_draft_pending"] = None
            st.rerun()
        if _c2.button("Discard", width="stretch", key="draft_discard"):
            # Delete the file the check actually READ — which may be the
            # pre-per-year name, in which case deleting _draft_path() would
            # leave the legacy draft to be offered again on every boot.
            Path(st.session_state.get("_draft_file")
                 or _draft_path()).unlink(missing_ok=True)
            st.session_state["_draft_pending"] = None
            st.rerun()
        st.divider()

    # Team plan status + edit control. `editable` gates every input below.
    editable, mode = render_team_status()
    st.session_state.editable = editable
    RO = not editable
    if RO:
        st.caption("Read-only. Take control (or open a Sandbox) to edit.")
    st.divider()

    lob_names = list(st.session_state.lobs.keys())
    options = lob_names + [CONSOLIDATED]
    # Customer Support is the team's main LOB — land there by default.
    default_view = _default_lob(lob_names) if lob_names else options[0]
    # Keyed: the default index is DERIVED (largest LOB by starting HC), and an
    # unkeyed selectbox re-derives its identity from that index — editing any
    # LOB's HC would churn the widget and snap the view (the "two clicks"
    # family; introduced+caught 2026-07-16 when the default went data-driven).
    view = st.selectbox("Line of Business", options,
                        index=options.index(default_view), key="lob_view")

    with st.expander("Add / remove LOB"):
        new_lob = st.text_input("New LOB name", disabled=RO)
        c1, c2 = st.columns(2)
        if c1.button("Add", disabled=RO) and new_lob and new_lob not in st.session_state.lobs:
            st.session_state.lobs[new_lob] = make_blank_lob(st.session_state.n_weeks)
            st.rerun()
        if c2.button("Remove current", disabled=RO) and view in st.session_state.lobs \
                and len(lob_names) > 1:
            del st.session_state.lobs[view]
            st.rerun()

    # Keyed ("as_" prefix → purged with the other assumption widgets on plan
    # load/rollover): its default is the value init_state() writes back, which
    # without a stable key churns identity → the two-click bug.
    n_weeks = st.slider("Planning horizon (weeks)", 12, 52, st.session_state.n_weeks,
                        key="as_horizon", disabled=RO)
    if n_weeks != st.session_state.n_weeks and not RO:
        init_state(n_weeks)
        st.rerun()

    _yr = plan_year()
    st.caption(f"Plan year: **{_yr}** (Monday-anchored weeks from Jan 1)")
    with st.expander(f"Roll into {_yr + 1}"):
        st.caption(
            f"Seeds a fresh **{_yr + 1}** working plan from what's on screen: ending "
            "production HC → starting HC, year-end members → starting members, last "
            "CPM/AHT carry forward, the seasonality shape copies, anyone on LOA or "
            "mentoring at year-end stays off the phones, and new-hire classes still "
            "in the pipeline graduate "
            f"into the new year. Published {_yr} versions stay in the changelog and "
            "remain loadable — nothing is shared until you publish the new year.")
        # CPM for the new year: flat carry, or seeded from the measured trend.
        _trends, _rows = {}, []
        for _l in st.session_state.lobs:
            _t = fit_cpm_trend(_l)
            if _t is None:
                continue
            _trends[_l] = _t
            _flat = float(pd.to_numeric(
                st.session_state.lobs[_l]["demand"]["CPM"], errors="coerce").iloc[-1] or 0)
            _proj = project_cpm(_t, st.session_state.n_weeks)
            _rows.append({
                "LOB": _l, "Weeks": _t["weeks"],
                "Trend %/yr": round(_t["pct_per_year"], 1),
                "Fit R²": round(_t["r2"], 2),
                "Flat CPM": round(_flat, 2),
                "Trend CPM (avg)": round(float(np.mean(_proj)), 2),
                "Δ volume %": (round((float(np.mean(_proj)) / _flat - 1) * 100, 1)
                               if _flat > 0 else 0.0),
            })
        _usable = {l: t for l, t in _trends.items()
                   if t["weeks"] >= CPM_TREND_MIN_WEEKS}
        _use_trend = False
        if _rows:
            st.caption("**CPM for the new year** — measured trend vs the flat carry:")
            st.dataframe(pd.DataFrame(_rows), hide_index=True, width="stretch")
            if _usable:
                _use_trend = st.checkbox(
                    f"Seed CPM from the measured trend ({len(_usable)} LOB(s) with "
                    f"≥{CPM_TREND_MIN_WEEKS} weeks) instead of carrying it flat",
                    key="roll_cpm_trend", disabled=RO,
                    help="Projects each LOB's deseasonalized CPM trend across the new "
                         "year, damped so a short-run slope can't run away. LOBs "
                         "without enough history still carry flat. Every week stays "
                         "editable afterwards.")
            else:
                st.caption(f"⚠️ No LOB has {CPM_TREND_MIN_WEEKS}+ weeks of measured CPM "
                           "yet — CPM will carry flat. (Enter **Members (actual)** and "
                           "load actual contacts to build the history.)")
        else:
            st.caption("No measured CPM yet — CPM will carry flat. Enter **Members "
                       "(actual)** on the demand grid and load the WFM feed to build "
                       "the history the trend needs.")

        # Rollover REPLACES the working plan, and since plan years became
        # separate plans, switching back to <year> loads its PUBLISHED version —
        # so anything edited but not published is reachable only through the
        # per-year draft at the next boot. Publishing first makes the year you
        # are leaving recoverable in one click, forever. (Field report
        # 2026-07-30: "rolling over made my 2026 new hires disappear" — they
        # were intact in the published version, but nothing said where.)
        _unpub = False
        try:
            _act_now = collab.read_active(SCENARIO_DIR, _yr)
            _snap_now = (collab.load_snapshot(SCENARIO_DIR, _act_now["file"])
                         if _act_now else None)
            if _snap_now is None:
                _unpub = True
            else:
                _keys = ("n_weeks", "plan_year", "members_start", "members_end", "lobs")
                _mine = {k: v for k, v in _serialize_lobs().items() if k in _keys}
                _ref = {k: _snap_now.get(k) for k in _keys if k in _snap_now}
                _unpub = (json.dumps(_mine, sort_keys=True, default=str)
                          != json.dumps(_ref, sort_keys=True, default=str))
        except Exception:      # never let a check block the panel
            _unpub = False
        if _unpub:
            st.warning(
                f"⚠️ This {_yr} plan has changes that are **not published**. "
                f"Rolling over replaces your working plan with {_yr + 1}, and "
                f"switching back to {_yr} then loads its last *published* "
                "version — these edits would not be in it. **Publish "
                f"{_yr} first**, then roll over.")

        sure = st.checkbox(f"Replace my working plan with a seeded {_yr + 1} plan",
                           key="roll_confirm", disabled=RO)
        if st.button(f"Roll into {_yr + 1}", disabled=RO or not sure, key="roll_btn"):
            _seeds = ({l: project_cpm(t, st.session_state.n_weeks)
                       for l, t in _usable.items()} if _use_trend else None)
            roll_over_plan(_seeds)
            st.success(
                f"{_yr + 1} plan seeded"
                + (f" — CPM projected from the measured trend for "
                   f"{len(_seeds)} LOB(s)." if _seeds else " — CPM carried flat.")
                + " Set the new year-end member forecast, review, then publish.")
            st.rerun()

    st.subheader("Membership (all LOBs)")
    st.caption("One org-wide member base — spreads linearly from start (actual) to "
               "year-end (forecast). Each LOB applies its own CPM to it.")
    st.session_state.members_start = st.number_input(
        "Members — start (actual)", 0.0, 50_000_000.0,
        float(st.session_state.get("members_start", 0.0) or 0.0), 1000.0, disabled=RO,
        key="as_members_start")
    st.session_state.members_end = st.number_input(
        "Members — year-end (forecast)", 0.0, 50_000_000.0,
        float(st.session_state.get("members_end", 0.0) or 0.0), 1000.0, disabled=RO,
        key="as_members_end")

    if view != CONSOLIDATED:
        a = st.session_state.lobs[view]["assumptions"]
        # Assumptions live in a popover so the rail stays short. The body
        # still executes every rerun (verified), so widget identity and the
        # as_*_{view} keys behave exactly as they did inline — which is what
        # keeps _purge_assumption_widgets() and the AppTest checks working.
        with st.popover(f"Assumptions — {view}", width="stretch"):
            a["starting_hc"] = st.number_input("Starting production HC", 0.0, 2000.0, float(a["starting_hc"]), 1.0, disabled=RO, key=f"as_hc_{view}")
            a["annual_attrition_pct"] = st.number_input(
                "Annual attrition %", 0.0, 100.0, float(a["annual_attrition_pct"]), 0.5,
                disabled=RO, key=f"as_attr_{view}",
                help="Planned tenured-agent attrition, applied as a smooth weekly drip "
                     "(rate ÷ 52 of current headcount). Weeks with a filled "
                     "**Attrition (actual)** cell in the roster grid use that number "
                     "instead. The readout below annualizes what you actually entered "
                     "— use it to check this assumption against reality.")
            _meas = measured_attrition_pct(st.session_state.lobs[view])
            if _meas is None:
                st.caption("No actual attrition entered yet (roster grid → "
                           "*Attrition (actual)*).")
            else:
                _pct, _wks = _meas
                _enough = _wks >= ATTR_MIN_WEEKS
                _delta = _pct - float(a["annual_attrition_pct"])
                st.caption(f"Actual, annualized from **{_wks}** week(s): "
                           f"**{_pct:.1f}%** · {_delta:+.1f} pts vs assumption"
                           + ("" if _enough else
                              f" — ⚠️ too few weeks to trust "
                              f"(≥{ATTR_MIN_WEEKS} recommended; a single bad week "
                              f"annualizes absurdly)"))
                if st.button("Adopt measured attrition", disabled=RO or not _enough,
                             key=f"adopt_attr_{view}",
                             help=f"Set the assumption to the annualized rate from your "
                                  f"entered weeks. Enabled at {ATTR_MIN_WEEKS}+ weeks of "
                                  f"actuals."):
                    a["annual_attrition_pct"] = round(float(_pct), 1)
                    st.session_state.pop(f"as_attr_{view}", None)   # let the widget re-seed
                    st.rerun()
            a["shrinkage_pct"] = st.number_input(
                "Shrinkage %", 0.0, 90.0, float(a["shrinkage_pct"]), 0.5, disabled=RO,
                key=f"as_shrink_{view}",
                help="Share of paid time NOT available on the phones — total: "
                     "out-of-office (PTO, sick, LOA) **plus** in-office (breaks, "
                     "meetings, coaching, training). Raises Required FTE: at 32%, one "
                     "FTE delivers 40 × 0.68 = 27.2 seated hours. The Shrinkage page "
                     "measures the **in-office** half from real AUX data — layer OOO on "
                     "top of that before setting this.")
            basis = st.selectbox(
                "Required FTE basis",
                ["Workload (volume × AHT ÷ occupancy)", "Erlang C (service-level staffing)"],
                index=1 if a.get("req_basis", "workload") == "erlang" else 0, disabled=RO,
                key=f"as_basis_{view}",
                help="Workload = FTE to handle the volume at target occupancy. Erlang C = "
                     "FTE to hit the SL target within the threshold — adds the queueing "
                     "buffer thin queues need; converges to workload at scale. Occupancy "
                     "acts as a cap on Erlang staffing, not a divisor.")
            a["req_basis"] = "erlang" if basis.startswith("Erlang") else "workload"
            if a["req_basis"] == "erlang":
                a["sl_target_pct"] = st.number_input(
                    "SL target %", 50.0, 100.0, float(a.get("sl_target_pct", 80.0)), 1.0,
                    disabled=RO, key=f"as_slt_{view}")
                a["sl_threshold_sec"] = st.number_input(
                    "SL threshold (sec)", 5.0, 600.0, float(a.get("sl_threshold_sec", 40.0)),
                    5.0, disabled=RO, key=f"as_sls_{view}")
                a["open_hrs_week"] = st.number_input(
                    "Open hours / week", 1.0, 168.0, float(a.get("open_hrs_week", 60.0)),
                    1.0, disabled=RO, key=f"as_open_{view}",
                    help="Hours the queue is open — spreads weekly volume into an hourly "
                         "arrival rate and converts concurrent agents back to weekly FTE.")
            a["occupancy_pct"] = st.number_input(
                "Target occupancy %", 50.0, 100.0, float(a["occupancy_pct"]), 0.5,
                disabled=RO, key=f"as_occ_{view}",
                help="How busy a seated agent should be — the share of seated time "
                     "actually handling contacts. The rest is the idle time a queue "
                     "needs to answer promptly. On the **workload** basis it divides "
                     "(85% → 15% more FTE than pure handling time). On the **Erlang** "
                     "basis it is only a CAP — Erlang already prices the idle time the "
                     "service level demands. Thin queues genuinely cannot run hot: "
                     "~70% at target is normal for a small skill.")
            a["paid_hours_per_week"] = st.number_input(
                "Paid hrs / FTE / week", 20.0, 60.0, float(a["paid_hours_per_week"]),
                0.5, disabled=RO, key=f"as_paid_{view}",
                help="Contracted hours one full-time equivalent is paid for (40 = "
                     "standard). The denominator that turns required *hours* into "
                     "required *people*. Lower it only if this LOB's staff genuinely "
                     "work shorter weeks — it is not a place to model part-timers "
                     "(use Full-time %) or absence (use Shrinkage %).")
            a["workload_margin_pct"] = st.number_input(
                "Workload margin %", 0.0, 30.0, float(a["workload_margin_pct"]), 0.5,
                disabled=RO, key=f"as_margin_{view}",
                help="**A deliberate cushion on demand.** Inflates the forecast volume "
                     "before staffing is computed: at 5%, a 10,000-contact week is "
                     "staffed as 10,500 — so Required FTE rises ~5%.\n\n"
                     "**Use it for the work the model can't see:**\n"
                     "• forecast error in the bad direction (you'd rather carry a small "
                     "buffer than be short half the time)\n"
                     "• work that isn't in the contact count — callbacks, outbound "
                     "follow-ups, escalations\n"
                     "• intra-week spikes a weekly average hides (a brutal Monday eats "
                     "more capacity than the weekly mean implies)\n\n"
                     "**Set it to 0 when:** you are reproducing another model's numbers for a "
                     "like-for-like parity check, or "
                     "when the LOB is on the **Erlang** basis and already beating its "
                     "SL target — Erlang buys its own buffer, so margin on top is "
                     "double-insurance.\n\n"
                     "⚠️ It stacks with Shrinkage and Occupancy multiplicatively. At "
                     "32% shrink / 85% occ / 5% margin, one hour of contact work costs "
                     "~1.82 paid hours. Don't also pad AHT or the forecast by hand — "
                     "that's the same fear, priced three times.")
            a["ft_pct"] = st.number_input("Full-time %", 0.0, 100.0, float(a.get("ft_pct", 100.0)), 1.0, disabled=RO, key=f"as_ft_{view}")
            a["ramp_weeks"] = st.number_input(
                "NH ramp — weeks to full productivity", 0, 26,
                int(a.get("ramp_weeks", 0) or 0), 1, disabled=RO, key=f"as_rampw_{view}",
                help="0 = off (grads land at 100%). Grads still count as headcount; "
                     "their Staffed-FTE contribution climbs linearly from the starting "
                     "productivity to 100% over this many production weeks. The gap "
                     "shows as the plan's 'Ramp Discount' row.")
            if int(a["ramp_weeks"]) > 0:
                a["ramp_start_pct"] = st.number_input(
                    "NH ramp — starting productivity %", 10.0, 100.0,
                    float(a.get("ramp_start_pct", 60.0)), 5.0, disabled=RO,
                    key=f"as_ramps_{view}")
            a["transfer_ramp_weeks"] = st.number_input(
                "Transfer ramp — weeks (interims in)", 0, 12,
                int(a.get("transfer_ramp_weeks", 2) or 0), 1, disabled=RO,
                key=f"as_trampw_{view}",
                help="Agents transferring IN to this LOB (e.g. an MS interim on a "
                     "specialty line) ramp briefly. Returning agents (an inflow that "
                     "reverses this LOB's earlier outflow) come home at full weight. "
                     "0 = off.")
            if int(a["transfer_ramp_weeks"]) > 0:
                a["transfer_ramp_start_pct"] = st.number_input(
                    "Transfer ramp — starting productivity %", 10.0, 100.0,
                    float(a.get("transfer_ramp_start_pct", 75.0)), 5.0, disabled=RO,
                    key=f"as_tramps_{view}")
            # The four cadence controls exist ONLY for recommend_classes — nothing
            # in compute_plan reads them. With the Hiring Advisor hidden they would
            # be four knobs that visibly do nothing, which is the specific kind of
            # dead surface this app is careful not to ship. Stored values are left
            # untouched, so unhiding restores each planner's settings.
            if "Hiring Advisor" not in HIDDEN_PAGES:
                a["class_gap_weeks"] = st.number_input(
                    "Hiring cadence — min weeks between class starts", 0, 13,
                    int(a.get("class_gap_weeks", 4) or 0), 1, disabled=RO,
                    key=f"as_cgap_{view}",
                    help="Trainer/facilitator reality: classes run in monthly-ish cohorts, "
                         "not one every week. The Hiring Advisor spaces recommended class "
                         "starts at least this far apart (per LOB — external-hire pipelines "
                         "like Customer Support vs short internal ramp-ups differ) and sizes "
                         "each class to carry every red week until the next slot can land "
                         "grads. 0 = unconstrained.")
                a["one_class_at_a_time"] = st.checkbox(
                    "Only one class in the pipeline at a time",
                    value=bool(a.get("one_class_at_a_time", False)), disabled=RO,
                    key=f"as_1caat_{view}",
                    help="Single training team: a new class cannot start until the "
                         "previous one has fully graduated (training + nesting). Spaces "
                         "recommended starts by the class's whole pipeline length — a "
                         "longer curriculum automatically stretches the calendar. "
                         "Combines with the min-weeks gap above (the stricter wins).")
                a["class_min_size"] = st.number_input(
                    "Hiring cadence — min class size (seats)", 1, 50,
                    int(a.get("class_min_size", 1) or 1), 1, disabled=RO,
                    key=f"as_cmin_{view}",
                    help="Smallest cohort worth a facilitator and a classroom. Below this, "
                         "the Hiring Advisor folds the need into the previous recommended "
                         "class when the max allows; otherwise it reports the stretch for "
                         "OT/interims and recommends a class only once the accumulated "
                         "need justifies one. 1 = off. Per LOB — cross-training 1–2 agents "
                         "pulled onto a specialty line is a normal 'class' there.\n\n"
                         "⚠️ Set too high relative to *max class size*, this starves the "
                         "plan: needs stay below the bar, nothing can fold, and the "
                         "advisor reports a long run of below-minimum weeks instead of "
                         "classes. That list is the signal to lower this, raise the max, "
                         "or accept OT — never a hidden problem.")
                a["class_max_size"] = st.number_input(
                    "Hiring cadence — max class size (seats)", 1, 100,
                    int(a.get("class_max_size", 12) or 12), 1, disabled=RO,
                    key=f"as_cmax_{view}",
                    help="Largest cohort one class can absorb (classroom seats / "
                         "facilitator span — e.g. 20 on the external-hire line). The "
                         "Hiring Advisor caps every recommended class here; a deeper "
                         "need spills to the next calendar slot or is reported for "
                         "OT/interims.")

    render_publish_panel(mode)


# Count rows print as whole numbers with thousands separators; everything else
# is FTE and keeps a decimal. "43,269" is a contact volume you can read at a
# glance; "43,269.0" is the same number pretending to a precision the forecast
# does not have.
_PLAN_COUNT_ROWS = frozenset({
    "Model Forecast", "Forecast (final)",
    "Actual Offered", "Actual Variance", "Volume Capacity",
})
# Hairlines that group the frame into demand / requirement / supply.
_PLAN_RULES = frozenset({"Workload (hrs)", "Production HC", "Staffed FTE"})
# CPM lives in its decimals — ~1.45 for the main line, and a drift worth acting
# on is hundredths. At the table's default 1dp every plausible value prints
# "1.4" or "1.5", which is not a readout.
_PLAN_PRECISION = {"CPM (plan)": 2, "CPM (actual)": 2}


def render_plan_grid(plan: pd.DataFrame, note: str):
    grid = plan.set_index("Week").T
    grid.columns = [c[5:] for c in grid.columns]

    brand.data_table(grid, int_rows=_PLAN_COUNT_ROWS, precision=_PLAN_PRECISION,
                     shade_rows={"Net FTE"}, rule_before=_PLAN_RULES)
    st.caption(note)

    def shade_net(row):
        if row.name != "Net FTE":
            return [""] * len(row)
        # Pale tint + saturated ink: readable on the light Member Hall theme.
        # (Values come from brand so a palette change lands here too.)
        return [f"background-color:{brand.SHORT_BG};color:{brand.SHORT};font-weight:600"
                if v < 0 else
                f"background-color:{brand.COVERED_BG};color:{brand.COVERED};font-weight:600"
                for v in row]

    # The compact table above is the reading surface; this is the same frame as
    # a real grid, for sorting, resizing and copying out to Excel. Kept rather
    # than replaced — losing copy-to-clipboard would be a downgrade dressed as
    # a restyle, and the diagnostic checks read the plan through here.
    with st.expander("Open as a sortable grid — sort, resize, copy to Excel"):
        st.dataframe(grid.style.apply(shade_net, axis=1).format("{:,.1f}", na_rep="—"),
                     width="stretch")


def plan_chart_with_benchmarks(plan: pd.DataFrame, lob: str | None):
    """Model Required/Staffed FTE, plus the measured actual-staffed overlay for
    any weeks that overlap the loaded ACD data.

    The WFM feed's own Required FTE overlay was removed with that feed
    (2026-07-31). It was per-15-min minimum staffing, which inflates small
    queues roughly 2x (SMB 3.8 vs 1.9 on the sample) — the team already
    distrusted it, and an operational requirement sitting on a long-range
    capacity chart is exactly the confusion the removal was asked for."""
    weeks = plan["Week"].tolist()
    chart = plan.set_index("Week")[["Required FTE", "Staffed FTE"]].copy()
    overlaid = []
    a = _bench_series("acd_weekly", "Actual Staffed FTE", lob, weeks)
    if a is not None:
        chart["Actual Staffed (ACD)"] = a.values
        overlaid.append("Actual Staffed (ACD)")
    st.line_chart(chart)
    if overlaid:
        st.caption("Overlay from your real data: " + ", ".join(overlaid)
                   + " (only weeks that overlap the plan horizon).")
        _partial_note(lob)
    elif st.session_state.get("acd_weekly") is not None:
        st.caption("ℹ️ ACD data is loaded, but none of its weeks fall inside "
                   "the current plan horizon — export the plan's weeks to overlay them.")
    else:
        st.caption("Tip: load the ACD export on the Real Data page to overlay "
                   "measured actual FTE here.")


def _model_members(lob: str | None, weeks) -> np.ndarray | None:
    """The membership series the MODEL used, week by week: the org-wide
    forecast spread with `Members (actual)` REPLACING it wherever a real figure
    was entered — exactly the rule compute_plan applies.

    Using the model's own series (rather than requiring actual membership, as
    the aggregate measured_cpm() does) is what makes a per-week CPM readable
    today: no team has entered actual membership yet, and a column that is
    blank in every row teaches nobody anything. Membership is org-wide, so any
    LOB's frame answers for all of them — that is what apply_global_members
    guarantees — which is also why the consolidated view can use the first."""
    lobs = st.session_state.get("lobs") or {}
    d = lobs.get(lob) if lob else next(iter(lobs.values()), None)
    if d is None:
        return None
    dem = d["demand"]
    if len(dem) != len(weeks) or "Members" not in dem.columns:
        return None
    m = pd.to_numeric(dem["Members"], errors="coerce").to_numpy(dtype=float)
    if "Members (actual)" in dem.columns:
        ma = pd.to_numeric(dem["Members (actual)"], errors="coerce").to_numpy(dtype=float)
        m = np.where(np.isnan(ma), m, ma)
    return m


def _planned_cpm(lob: str | None, weeks) -> np.ndarray | None:
    """The CPM sitting in the demand grid, for the row above. Consolidated view
    has no single CPM — each LOB applies its own to the shared member base —
    so it returns None rather than an average that means nothing."""
    lobs = st.session_state.get("lobs") or {}
    if lob is None or lob not in lobs:
        return None
    dem = lobs[lob]["demand"]
    if len(dem) != len(weeks) or "CPM" not in dem.columns:
        return None
    return pd.to_numeric(dem["CPM"], errors="coerce").to_numpy(dtype=float)


def plan_with_demand_benchmarks(plan: pd.DataFrame, lob: str | None) -> pd.DataFrame:
    """Append Actual Offered rows (+ variance vs the plan forecast) for weeks
    that overlap the loaded ACD data. Rows are omitted entirely when no weeks
    overlap, so the grid never shows a wall of blanks.

    The WFM feed's own forecast rows were removed with that feed
    (2026-07-31): a second forecast beside the plan's answered a question
    nobody was asking, and this grid now compares the plan against MEASURED
    contacts only.

    Variance convention: actual − plan forecast (positive = more contacts
    arrived than the planner forecast, i.e. we under-forecast)."""
    weeks = plan["Week"].tolist()
    df = plan.copy()
    fcst = plan["Forecast (final)"].to_numpy()

    a = _bench_series("acd_weekly", "Actual Contacts", lob, weeks)
    if a is not None:
        av = a.to_numpy()
        df["Actual Offered"] = av
        df["Actual Variance"] = (av - fcst).round(0)

        # Measured CPM per week, alongside the CPM that was planned. Derived,
        # never entered: actual contacts × 52 ÷ the membership the model used
        # that week. Same definition as measured_cpm(), just not aggregated —
        # so it is a like-for-like check on the assumption sitting in the
        # demand grid rather than a second, differently-defined number.
        mem = _model_members(lob, weeks)
        if mem is not None:
            with np.errstate(divide="ignore", invalid="ignore"):
                cpm_act = np.where(mem > 0, av * WEEKS_PER_YEAR / mem, np.nan)
            df["CPM (actual)"] = np.round(cpm_act, 2)
            planned = _planned_cpm(lob, weeks)
            if planned is not None:
                df["CPM (plan)"] = np.round(planned, 2)

    order = ["Week", "Model Forecast", "Forecast (final)",
             "Actual Offered", "Actual Variance",
             "CPM (plan)", "CPM (actual)",
             "Workload (hrs)", "Available Hrs/FTE", "Required FTE",
             "Workload Req FTE", "Erlang Req FTE",
             "Production HC", "Prod HC — FT", "Prod HC — PT",
             "Supervisors", "Supervisor Ratios", "Leads/Project", "Leads/Project Ratios", "Support Staff", "Overall HC",
             "Attrition", "Attrition (actual)", "NH Grads", "Ramp Discount",
             "LOA", "Mentors", "Staffed FTE", "Net FTE",
             "Volume Capacity"]
    return df[[c for c in order if c in df.columns]]


def build_reconciliation(plan: pd.DataFrame, lob_data: dict, lob: str) -> pd.DataFrame | None:
    """Plan vs. measured-actual reconciliation for one LOB, weeks-as-columns.

    Each reconcilable metric contributes a Plan / Actual / Variance triple
    (Variance = Actual − Plan); service metrics with no plan target show actual
    only. Weeks with no overlapping actuals are dropped, so the table appears
    only where real data exists."""
    weeks = plan["Week"].tolist()
    a = lob_data["assumptions"]
    pf = plan.set_index("Week")
    dem = lob_data["demand"].set_index("Week")
    rows: dict[str, pd.Series] = {}

    def const(v):
        return pd.Series([v] * len(weeks), index=weeks, dtype="float")

    def triple(name, plan_s, act_s):
        rows[f"{name} — Plan"] = plan_s.reindex(weeks)
        rows[f"{name} — Actual"] = act_s
        rows[f"{name} — Var"] = act_s - plan_s.reindex(weeks)

    # Every actual now comes from the ACD feed (2026-07-31). It measures all
    # of these directly, and the plan reconciles against MEASURED reality —
    # never against another system's forecast.
    off = _bench_series("acd_weekly", "Actual Contacts", lob, weeks)
    if off is not None:
        triple("Contacts", pf["Forecast (final)"], off)
    aht = _bench_series("acd_weekly", "Actual AHT (sec)", lob, weeks)
    if aht is not None:
        triple("AHT (sec)", dem["AHT (sec)"], aht)
    # SL% reconciles against the LOB's own target assumption. It used to
    # reconcile against the WFM forecast's SL, which made this row a
    # forecast-vs-forecast comparison the plan had no stake in; the target is
    # policy the team actually sets and is answerable for.
    sla = _bench_series("acd_weekly", "Actual SL %", lob, weeks)
    if sla is not None:
        triple("SL %", const(a.get("sl_target_pct")), sla)
    stf = _bench_series("acd_weekly", "Actual Staffed FTE", lob, weeks)
    if stf is not None:
        triple("FTE (req vs staffed)", pf["Required FTE"], stf)
    shr = _bench_series("acd_weekly", "In-Office Shrink %", lob, weeks)
    if shr is not None:
        triple("Shrink %", const(a.get("shrinkage_pct")), shr)
    occ = _bench_series("acd_weekly", "Occupancy % (actual)", lob, weeks)
    if occ is not None:
        triple("Occupancy %", const(a.get("occupancy_pct")), occ)
    asa = _bench_series("acd_weekly", "Actual ASA (sec)", lob, weeks)
    if asa is not None:
        rows["ASA (sec) — Actual"] = asa
    # Abandon is a RATE now, and correct for the first time. The WFM feed's
    # Actual_Abandonment is a per-interval COUNT (0-16 on the sample) which
    # wfm_weekly ran a volume-weighted AVERAGE over — so the old
    # "Abandon — Actual" was neither a rate nor a total. ARC read 0.70 where
    # the truth is 19 abandons on 126 offered = 15.1%.
    ab = _bench_series("acd_weekly", "Abandon %", lob, weeks)
    if ab is not None:
        rows["Abandon % — Actual"] = ab

    if not rows:
        return None
    wide = pd.DataFrame(rows, index=weeks)
    actual_cols = [c for c in wide.columns if c.endswith("Actual")]
    wide = wide.dropna(how="all", subset=actual_cols)  # keep only weeks with actuals
    return wide.T if not wide.empty else None


def render_reconciliation(df: pd.DataFrame):
    grid = df.copy()
    grid.columns = [str(c)[5:] for c in grid.columns]  # strip the year, show MM-DD

    # Variance rows get coloured ink but NO tint: a Plan/Actual/Var triple means
    # every third row would be shaded, which reads as stripes rather than as
    # meaning. Net FTE keeps the tint precisely because it is the one row where
    # the colour IS the verdict.
    var_rows = {r for r in grid.index if str(r).endswith("Var")}
    rules = {r for r in grid.index if str(r).endswith("— Plan")}
    brand.data_table(grid, tone_rows=var_rows, rule_before=rules)

    def shade_var(row):
        if not str(row.name).endswith("Var"):
            return [""] * len(row)
        return [f"color:{brand.SHORT}" if (pd.notna(v) and v < 0)
                else (f"color:{brand.COVERED}" if pd.notna(v) else "") for v in row]

    with st.expander("Open as a sortable grid — sort, resize, copy to Excel"):
        st.dataframe(grid.style.apply(shade_var, axis=1).format("{:,.1f}", na_rep="—"),
                     width="stretch")


def _reference_metrics(scope: str | None = None) -> dict | None:
    """Headline metrics of the last PUBLISHED version — the baseline for the
    Executive View's Δ chips ("what changed since we last published").

    `scope` is a LOB name to restrict to, or None for the consolidated figure.
    It is part of the CACHE KEY, not just a filter: the Executive View's scope
    selector must not be able to chart one LOB's today against every LOB's
    last publish, and a cache keyed on the file alone would do exactly that
    the moment the selector moved. Recomputed only when the pointer or the
    scope moves; None when nothing is published yet."""
    act = collab.read_active(SCENARIO_DIR, _plan_year())
    if not act:
        return None
    cache = st.session_state.get("_ref_metrics")
    if cache and cache.get("file") == act.get("file") \
            and cache.get("scope") == scope:
        return cache
    snap = collab.load_snapshot(SCENARIO_DIR, act["file"])
    if not snap:
        return None
    try:
        # Through the SHARED migration path — this used to hand-roll its own
        # partial copy (Seasonality only), so every column added since
        # (Members (actual), Attrition (actual), Mentors) was missing here
        # while present everywhere else. `_payload_lobs` is the one path.
        lobs = {l: d for l, d in _payload_lobs(snap).items()
                if scope is None or l == scope}
        if not lobs:            # the scoped LOB did not exist at publish time
            return None
        plans = [compute_plan(v) for v in lobs.values()]
        req = sum(pl["Required FTE"].to_numpy() for pl in plans)
        stf = sum(pl["Staffed FTE"].to_numpy() for pl in plans)
        net = stf - req
        # Forecast volume and the headcount walk get Δ chips too. The MEASURED
        # metrics (actual volume, actual AHT, actual attrition) deliberately do
        # not: a published plan has no actuals to be a baseline for, so a Δ
        # there would be comparing a measurement to a forecast and calling it
        # a change.
        fcst = sum(pl["Forecast (final)"].to_numpy() for pl in plans)
        hc = sum(pl["Production HC"].to_numpy() for pl in plans)
        cache = {"file": act["file"], "version": act["version"], "scope": scope,
                 "avg_req": float(req.mean()), "avg_stf": float(stf.mean()),
                 "coverage": float((net >= 0).mean() * 100),
                 "weeks_short": int((net < 0).sum()),
                 "fcst_total": float(fcst.sum()), "hc": [float(x) for x in hc]}
    except Exception:   # a malformed old snapshot must never break the page
        cache = None
    st.session_state["_ref_metrics"] = cache
    return cache


def _org_actual_aht(lobs: list[str], weeks: list[str]) -> tuple[float, int] | None:
    """(org-wide actual AHT in seconds, weeks covered), CONTACT-WEIGHTED.

    Weighted, not averaged. A plain mean over LOBs would let Wires — a few
    hundred contacts a week — pull the org number as hard as Customer Support at
    forty thousand, and the result would not be the AHT of any real call.
    `_bench_series` sums its column, which is right for contacts and nonsense
    for a rate, so the per-LOB series are combined here instead of asking it
    for a consolidated one. Partial feed weeks are already excluded inside it.
    """
    num = den = 0.0
    covered: set = set()
    for lob in lobs:
        a = _bench_series("acd_weekly", "Actual AHT (sec)", lob, weeks)
        c = _bench_series("acd_weekly", "Actual Contacts", lob, weeks)
        if a is None or c is None:
            continue
        a = pd.to_numeric(a, errors="coerce")
        c = pd.to_numeric(c, errors="coerce")
        m = a.notna() & c.notna() & (c > 0)
        if not m.any():
            continue
        num += float((a[m] * c[m]).sum())
        den += float(c[m].sum())
        covered |= set(a.index[m])
    return (num / den, len(covered)) if den > 0 else None


def _org_measured_attrition(plans: dict) -> dict | None:
    """Annualized attrition across every LOB, from the weeks a planner actually
    entered an Attrition (actual) figure.

    Returns the INPUTS as well as the answer — pct, weeks, departures,
    avg_at_risk, blank_elapsed — so the tile can show its own arithmetic
    instead of asking anyone to take 42.6% on faith.

    Aggregated from the numerator and denominator — Σ departures ÷ Σ
    headcount-at-risk × 52 — NOT by averaging the per-LOB percentages, which
    would weigh a 12-person specialty line the same as the main group.
    Deliberately counts ENTERED weeks only, the same discipline as
    measured_attrition_pct: a week nobody recorded is missing data, and letting
    it read as zero departures would teach the org a falsely low rate.
    Caller must respect ATTR_MIN_WEEKS before presenting it as fact."""
    dep = risk = 0.0
    weeks_recorded = blanks = 0
    for name, d in st.session_state.lobs.items():
        ros = d["roster"]
        if "Attrition (actual)" not in ros.columns or name not in plans:
            continue
        act = pd.to_numeric(ros["Attrition (actual)"], errors="coerce").to_numpy(dtype=float)
        m = ~np.isnan(act)
        if not m.any():
            continue
        hc = plans[name]["Production HC"].to_numpy(dtype=float)
        start = np.concatenate(([float(d["assumptions"]["starting_hc"])], hc[:-1]))
        dep += float(act[m].sum())
        risk += float(start[m].sum())
        weeks_recorded = max(weeks_recorded, int(m.sum()))
        # Elapsed weeks with NOTHING entered are the inflation risk: they are
        # excluded from the denominator, so recording only the weeks where
        # someone LEFT divides by the bad weeks alone and annualizes from
        # them. The walk already reads a blank elapsed week as zero
        # departures; the measurement deliberately does not, to stop Adopt
        # learning a falsely LOW rate from missing data. That asymmetry is
        # correct but it has a mirror image, so it has to be visible.
        blanks += int((_weeks_passed(d["demand"]["Week"].tolist()) & ~m).sum())
    if risk <= 0:
        return None
    return {"pct": float(dep / risk * 52 * 100), "weeks": weeks_recorded,
            "departures": dep, "avg_at_risk": risk / max(weeks_recorded, 1),
            "blank_elapsed": blanks}


def _compact(v: float) -> str:
    """2,175,004 → "2.18M". Contact volumes run to seven digits and five tiles
    across is not seven digits of room; the exact figure goes in the sub line."""
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:,.2f}M"
    if abs(v) >= 10_000:
        return f"{v / 1_000:,.1f}k"
    return f"{v:,.0f}"


def _month_axis() -> "alt.Axis":
    """Ordinal week axis labeled by month: one 'Jan/Feb/…' label on the first
    week of each month, blank otherwise — replaces 26 rotated MM-DD labels."""
    import altair as alt
    return alt.Axis(
        labelExpr="date(toDate(datum.value)) <= 7"
                  " ? timeFormat(toDate(datum.value), '%b') : ''",
        labelAngle=0, title=None, labelOverlap=False, tickSize=2, labelPadding=4)


def _today_rule(weeks: list[str]):
    """Dashed amber 'today' marker at the current week, when it's in-horizon."""
    import altair as alt
    tw = (date.today() - timedelta(days=date.today().weekday())).isoformat()
    if tw not in weeks:
        return None
    df = pd.DataFrame({"Week": [tw]})
    rule = alt.Chart(df).mark_rule(
        stroke=brand.AMBER, strokeDash=[4, 3], strokeWidth=1.5, opacity=.8
    ).encode(x=alt.X("Week:O", sort=None))
    txt = alt.Chart(df).mark_text(
        text="today", align="left", dx=4, color=brand.AMBER_LT,
        fontSize=10, baseline="top"
    ).encode(x=alt.X("Week:O", sort=None), y=alt.value(4))
    return rule + txt


def render_executive_view():
    """The verdict-first landing page: capacity health in layers — headline
    verdict → stat row → who/when heatmap → demand-vs-supply walk → per-LOB
    health cards → supply engine → data-health band. WFM house style."""
    import altair as alt

    # ---- scope: all lines, or one -------------------------------------
    # Everything below derives from `plans`, so filtering HERE scopes the whole
    # page — verdict, tiles, heatmap, walk, cards, supply engine and the
    # service table all follow (user ask 2026-08-01). Default stays "all", so
    # the page a planner already knows is unchanged until they pick.
    #
    # The two Δ-chip and actuals surfaces do NOT flow from `plans` and had to
    # be told the scope explicitly: `_reference_metrics(scope)` (else one LOB's
    # today would be charted against every LOB's last publish) and the ACD
    # service table, which groups the feed rollup rather than the plan.
    _ALL = "All lines of business"
    _opts = [_ALL] + list(st.session_state.lobs)
    # A LOB renamed or removed since this session last picked one would leave a
    # value the widget has no option for. Safe to purge because `ev_scope` is
    # written from ONE direction only (this widget) — unlike `plan_year`, whose
    # two writers are why it needs the `_year_seen` dance instead.
    if st.session_state.get("ev_scope") not in _opts:
        st.session_state.pop("ev_scope", None)
    _pick = st.selectbox(
        "Show", _opts, key="ev_scope",
        help="Narrow the whole page to one line of business — every number, "
             "chart and card below follows, including the change vs the last "
             "published version.")
    scope = None if _pick == _ALL else _pick

    plans = {l: compute_plan(d) for l, d in st.session_state.lobs.items()
             if scope is None or l == scope}
    if not plans:                      # scoped LOB vanished mid-rerun
        st.warning(f"{_pick} is no longer in the plan.")
        return
    lobs = list(plans)
    first = next(iter(plans.values()))
    weeks = first["Week"].tolist()
    wk_lbl = [w[5:] for w in weeks]                      # MM-DD for axes

    cons_req = sum(p["Required FTE"].to_numpy() for p in plans.values())
    cons_stf = sum(p["Staffed FTE"].to_numpy() for p in plans.values())
    cons_net = cons_stf - cons_req
    worst_i = int(np.argmin(cons_net))
    worst_net = float(cons_net[worst_i])
    weeks_under = int((cons_net < 0).sum())
    coverage = float((cons_net >= 0).mean() * 100)

    act = collab.read_active(SCENARIO_DIR, _plan_year())
    lock = collab.read_lock(SCENARIO_DIR, _plan_year())

    # ---- hero verdict -------------------------------------------------
    # The brand header is now global (above the nav bar). What stays here is
    # the scope detail, which only reads correctly this far down the script —
    # both values come from sidebar widgets.
    st.caption((f"{lobs[0]} only" if scope else f"{len(lobs)} LOBs")
               + f" · {st.session_state.n_weeks}-week horizon")
    pills = [(f"Plan v{act['version']} · {act['name']}" if act
              else "Unpublished working plan", "blue"),
             (f"{lock['user']} editing" if lock and not collab.lock_is_stale(lock)
              else "Plan unlocked", "amber" if lock and not collab.lock_is_stale(lock)
              else "green")]
    # A consolidated surplus can hide an LOB shortfall — FTE is not fungible
    # across skills, so the verdict checks every LOB, not just the sum.
    per_lob = {l: (float(p["Net FTE"].min()), int(p["Net FTE"].to_numpy().argmin()))
               for l, p in plans.items() if float(p["Required FTE"].mean()) > 0}
    short_lobs = [l for l, (m, _) in per_lob.items() if m < 0]
    if short_lobs:
        worst_lob = min(short_lobs, key=lambda l: per_lob[l][0])
        wl_net, wl_i = per_lob[worst_lob]
        pills.append((f"Short: {', '.join(short_lobs)}", "pink"))
        box = ("Worst LOB Net", f"{wl_net:+.1f}",
               f"FTE · {worst_lob} · wk {wk_lbl[wl_i]}", "bad")
        verdict = (f"Coverage breaks in {weeks_under} week(s) overall — deepest in "
                   f"{worst_lob}" if weeks_under
                   else f"Overall staffing covers, but {len(short_lobs)} of "
                        f"{len(per_lob)} LOBs run short")
    else:
        box = ("Worst-Week Net", f"{worst_net:+.1f}",
               f"FTE · wk of {wk_lbl[worst_i]}", "good" if worst_net >= 0 else "bad")
        verdict = ("Coverage holds across the horizon" if weeks_under == 0
                   else f"Coverage breaks in {weeks_under} week(s) — deepest "
                        f"{worst_net:+.1f} FTE (week of {wk_lbl[worst_i]})")
    # The verdict used to be a hero block. It is now a pill, so the page still
    # opens with the answer — a consolidated surplus can hide an LOB shortfall,
    # and that check is the reason this page exists — while the cards below
    # carry the operational numbers.
    _vtone = "pink" if (short_lobs or weeks_under) else "green"
    pills.append((verdict, _vtone))
    pills.append((f"{box[0]} {box[1]} · {box[2]}", _vtone))
    # Drift from the locked budget — the landing page should be able to answer
    # "how far are we from what we committed to" without a trip to Budget. Only
    # when a baseline exists and only at CONSOLIDATED scope: the headline
    # aggregates the whole plan, so showing it beside one LOB's numbers would
    # invite reading it as that LOB's drift.
    if scope is None:
        _bd = budget_drift_headline()
        if _bd:
            _req = _bd["req"]
            pills.append(
                (f"vs budget v{_bd['version']}: Required {_req:+.1f} FTE · "
                 f"Contacts {_bd['contacts']:+,.0f}",
                 "amber" if abs(_req) >= 1.0 else "green"))
    brand.pill_row(pills)

    # ---- headline cards: what we forecast, what actually happened -----
    ref_ = _reference_metrics(scope)
    cons_fcst = sum(p["Forecast (final)"].to_numpy() for p in plans.values())
    cons_hc = sum(p["Production HC"].to_numpy() for p in plans.values())

    # "Now" is the week we are in: the first that has not fully elapsed.
    _passed = int(_weeks_passed(weeks).sum())
    now_i = min(_passed, len(weeks) - 1)

    fcst_total = float(cons_fcst.sum())
    _fd = (fcst_total - ref_["fcst_total"]) if (ref_ and ref_.get("fcst_total")) else None
    hc_now = float(cons_hc[now_i])
    _ref_hc = (ref_.get("hc") if ref_ else None)
    _hd = (hc_now - _ref_hc[now_i]) if (_ref_hc and len(_ref_hc) == len(weeks)) else None

    act_vol = _bench_series("acd_weekly", "Actual Contacts", None, weeks)
    if act_vol is not None:
        _av = pd.to_numeric(act_vol, errors="coerce").dropna()
        act_vol = (float(_av.sum()), int(len(_av))) if len(_av) else None
    aht = _org_actual_aht(lobs, weeks)
    attr = _org_measured_attrition(plans)

    _no_feed = "no WFM actuals loaded"
    tiles = [
        {"label": "Forecast Volume", "value": _compact(fcst_total),
         "sub": f"{fcst_total:,.0f} contacts · {len(weeks)} wks",
         "delta": (f"{_fd / 1000:+,.0f}k" if _fd is not None and abs(_fd) >= 500
                   else ("· 0" if _fd is not None else None)),
         "delta_tone": "",
         "spark": list(cons_fcst), "spark_color": brand.DEMAND},
        {"label": "Actual Volume",
         "value": _compact(act_vol[0]) if act_vol else "—",
         "sub": (f"{act_vol[0]:,.0f} contacts · {act_vol[1]} wk(s) measured"
                 if act_vol else _no_feed),
         "sub_warn": act_vol is None},
        {"label": "Actual AHT", "value": f"{aht[0]:,.0f}s" if aht else "—",
         "sub": (f"contact-weighted · {aht[1]} wk(s)" if aht else _no_feed),
         "sub_warn": aht is None},
        {"label": "Production HC", "value": f"{hc_now:,.1f}",
         "sub": f"week of {wk_lbl[now_i]} · all LOBs",
         "delta": (f"{_hd:+.1f}" if _hd is not None and abs(_hd) >= 0.05
                   else ("· 0" if _hd is not None else None)),
         "delta_tone": ("good" if (_hd or 0) > 0 else "bad") if _hd else "",
         "spark": list(cons_hc), "spark_color": brand.SUPPLY},
        # Label says ANNUALIZED, and the sub shows the arithmetic. "42.6%" on
        # 53 departures reads like a mistake until you can see it is
        # 53 ÷ (216 × 30 wks) × 52 — the year-to-date rate scaled to a full
        # year, not the share of the workforce that has left (field question
        # 2026-07-30). A figure a planner cannot check is a figure they cannot
        # trust, and this one drives hiring.
        {"label": "Attrition (annualized)",
         "value": f"{attr['pct']:.1f}%" if attr else "—",
         "sub": ((f"{attr['departures']:,.0f} left over {attr['weeks']} wk(s), "
                  f"avg {attr['avg_at_risk']:,.0f} at risk"
                  + ("" if attr["weeks"] >= ATTR_MIN_WEEKS
                     else f" — under {ATTR_MIN_WEEKS}, treat as noise")
                  + (f" · ⚠ {attr['blank_elapsed']} elapsed wk(s) blank, "
                     "which inflates this"
                     if attr["blank_elapsed"] else ""))
                 if attr else "no departures recorded yet"),
         "sub_warn": (attr is None or attr["weeks"] < ATTR_MIN_WEEKS
                      or bool(attr["blank_elapsed"]))},
    ]
    brand.stat_row(tiles)

    # ---- stat row: branded tiles, sparklines, Δ vs last published ------
    ref = _reference_metrics(scope)

    def _delta(cur, prev, better, fmt="{:+.1f}"):
        if prev is None:
            return None, ""
        d = cur - prev
        if abs(d) < 0.05:
            return "· 0", ""
        if better is None:                      # direction is neutral (demand)
            return fmt.format(d), ""
        return fmt.format(d), ("good" if (d > 0) == (better == "up") else "bad")

    d1 = _delta(float(cons_req.mean()), ref and ref["avg_req"], None)
    d2 = _delta(float(cons_stf.mean()), ref and ref["avg_stf"], "up")
    d3 = _delta(coverage, ref and ref["coverage"], "up", "{:+.0f}pt")
    d4 = _delta(float(weeks_under), ref and ref["weeks_short"], "down", "{:+.0f}")
    # These are numpy sums across LOBs, and numpy's mean does NOT skip NaN — so
    # ONE missing week in ONE line blanks an org-wide tile while that line's own
    # page still reads fine (pandas .mean() skips). The blank-cell sources are
    # coerced at the walk now; this keeps a future one from printing the literal
    # string "nan" at a planner, without hiding it either: the tile says how
    # many weeks are missing.
    def _stat(arr, fmt="{:.1f}"):
        a = np.asarray(arr, dtype=float)
        bad = int(np.isnan(a).sum())
        if bad == len(a):
            return "—", "no weeks with data", True
        if bad:
            return (fmt.format(np.nanmean(a)),
                    f"{bad} week(s) missing — excluded", True)
        return fmt.format(a.mean()), "", False

    _sv_req, _sub_req, _w_req = _stat(cons_req)
    _sv_stf, _sub_stf, _w_stf = _stat(cons_stf)
    brand.stat_row([
        {"label": "Avg Required", "value": _sv_req,
         "sub": _sub_req, "sub_warn": _w_req,
         "delta": d1[0], "delta_tone": d1[1],
         "spark": list(cons_req), "spark_color": brand.VIOLET},
        {"label": "Avg Staffed", "value": _sv_stf,
         "sub": _sub_stf, "sub_warn": _w_stf,
         "delta": d2[0], "delta_tone": d2[1],
         "spark": list(cons_stf), "spark_color": brand.CYAN},
        {"label": "Coverage", "value": f"{coverage:.0f}%",
         "delta": d3[0], "delta_tone": d3[1],
         "spark": list(cons_net), "spark_color": brand.GREEN},
        {"label": "Weeks Short", "value": f"{weeks_under}",
         "delta": d4[0], "delta_tone": d4[1],
         "spark": list(-cons_net), "spark_color": brand.PINK},
    ])
    if ref:
        st.caption(f"Δ chips compare the working plan to published "
                   f"**v{ref['version']}**. Sparklines show the {len(weeks)}-week shape.")

    # ---- heatmap: who is short, and when ------------------------------
    with brand.section("heat", "Net FTE heat — who & when"):
        hm_long = pd.concat([
            pd.DataFrame({"Week": weeks, "LOB": l, "Net FTE": p["Net FTE"]})
            for l, p in plans.items()])
        brand.chart(
            alt.Chart(hm_long).mark_rect(cornerRadius=2).encode(
                x=alt.X("Week:O", sort=None, axis=_month_axis()),
                y=alt.Y("LOB:N", sort=lobs, title=None),
                color=alt.Color("Net FTE:Q",
                                scale=alt.Scale(domainMid=0,
                                                range=[brand.SHORT, brand.NEUTRAL, brand.COVERED]),
                                legend=alt.Legend(title="Net FTE")),
                tooltip=["LOB", "Week", alt.Tooltip("Net FTE:Q", format="+.1f")]),
            height=32 * len(lobs) + 60)

    # ---- demand vs supply walk ----------------------------------------
    with brand.section("walk", "Demand vs. supply — consolidated"):
        ln = pd.DataFrame({"Week": weeks, "Required": cons_req, "Staffed": cons_stf}) \
            .melt("Week", var_name="Series", value_name="FTE")
        ln_chart = alt.Chart(ln).mark_line(strokeWidth=2.5).encode(
            x=alt.X("Week:O", sort=None, axis=_month_axis()),
            y=alt.Y("FTE:Q", title="FTE"),
            color=alt.Color("Series:N",
                            scale=alt.Scale(domain=["Required", "Staffed"],
                                            range=[brand.DEMAND, brand.SUPPLY]),
                            legend=alt.Legend(title=None, orient="top")),
            tooltip=["Week", "Series", alt.Tooltip("FTE:Q", format=".1f")])
        _tr = _today_rule(weeks)
        brand.chart(ln_chart + _tr if _tr is not None else ln_chart, height=240)
        nf = pd.DataFrame({"Week": weeks, "Net": cons_net})
        nf_chart = alt.Chart(nf).mark_bar(opacity=.85, cornerRadius=2).encode(
            x=alt.X("Week:O", sort=None, axis=_month_axis()),
            y=alt.Y("Net:Q", title="Net FTE"),
            color=alt.condition(alt.datum.Net < 0,
                                alt.value(brand.PINK), alt.value(brand.GREEN)),
            tooltip=["Week", alt.Tooltip("Net:Q", format="+.1f")])
        brand.chart(nf_chart + _tr if _tr is not None else nf_chart, height=140)

    # ---- per-LOB health cards -----------------------------------------
    # A label, not a card: lob_card() draws its own, and a card of cards reads
    # as a mistake. Micro-label so it sits at the same level as the section
    # titles around it.
    st.markdown('<div class="cc-sec-ttl" style="margin:2px 0 8px">LOB health</div>',
                unsafe_allow_html=True)
    cols = st.columns(3)
    for i, (l, p) in enumerate(plans.items()):
        req, net = p["Required FTE"], p["Net FTE"]
        avg_req = float(req.mean())
        min_net = float(net.min())
        wi = int(net.to_numpy().argmin())
        cov = float((net >= 0).mean() * 100)
        attr = float(p["Attrition"].sum())
        grads = float(p["NH Grads"].sum())
        if avg_req <= 0:
            pill, tone = "No demand entered", ""
        elif min_net < 0:
            pill, tone = f"Short — {int((net < 0).sum())} wk(s)", "pink"
        elif min_net < 0.05 * avg_req:
            pill, tone = "Tight — thin buffer", "amber"
        else:
            pill, tone = "Covered", "green"
        body = (f"Worst week <b>{min_net:+.1f}</b> FTE (wk {wk_lbl[wi]}) · "
                f"coverage <b>{cov:.0f}%</b><br>"
                f"Avg required <b>{avg_req:.1f}</b> vs staffed "
                f"<b>{float(p['Staffed FTE'].mean()):.1f}</b><br>"
                f"Attrition <b>−{attr:.1f}</b> vs NH grads <b>+{grads:.1f}</b> "
                f"{'⚠️ pipeline behind' if grads < attr and avg_req > 0 else ''}")
        with cols[i % 3]:
            brand.lob_card(l, f"{st.session_state.n_weeks}-week outlook",
                           pill, tone, body, brand.accent_for(i))

    # ---- supply engine: is hiring keeping up with attrition? ----------
    with brand.section("supply", "Supply engine — attrition out vs. new-hire grads in"):
        sup = pd.DataFrame({
            "Week": weeks,
            "NH Grads": sum(p["NH Grads"].to_numpy() for p in plans.values()),
            "Attrition": -sum(p["Attrition"].to_numpy() for p in plans.values()),
        }).melt("Week", var_name="Flow", value_name="FTE")
        sup_chart = alt.Chart(sup).mark_bar(opacity=.9, cornerRadius=2).encode(
            x=alt.X("Week:O", sort=None, axis=_month_axis()),
            y=alt.Y("FTE:Q", title="FTE / week"),
            color=alt.Color("Flow:N",
                            scale=alt.Scale(domain=["NH Grads", "Attrition"],
                                            range=[brand.COVERED, brand.SHORT]),
                            legend=alt.Legend(title=None, orient="top")),
            tooltip=["Week", "Flow", alt.Tooltip("FTE:Q", format="+.2f")])
        brand.chart(sup_chart + _tr if _tr is not None else sup_chart, height=180)

    # ---- service actuals vs TARGET (ACD) -------------------------------
    # Was "actuals vs WFM forecast" until 2026-07-31. Judging service
    # against another system's FORECAST was always the weaker comparison —
    # the forecast is not a commitment and nobody is answerable for missing
    # it. The queue's SL TARGET is policy the team sets, so a miss means
    # something. Same for AHT: the plan's own assumption is what a variance
    # should be read against, since that is the number the plan was built on.
    sw = st.session_state.get("acd_weekly")
    if sw is not None and not sw.empty and "Actual SL %" in sw.columns:
        with brand.section("svc", "Service level & AHT — actuals vs. plan"):
            svc_rows = []
            # Grouped off the FEED rollup, not `plans`, so the page scope has
            # to be applied by hand here — otherwise picking one LOB still
            # showed every queue's service actuals.
            for l, g in sw.groupby("LOB"):
                if scope is not None and l != scope:
                    continue
                def wavg(col, wcol):
                    if col not in g:
                        return np.nan
                    v, w = g[col], g[wcol].fillna(0)
                    m = v.notna() & (w > 0)
                    return float((v[m] * w[m]).sum() / w[m].sum()) if m.any() else np.nan
                _a = (st.session_state.lobs.get(l) or {}).get("assumptions") or {}
                _dem = (st.session_state.lobs.get(l) or {}).get("demand")
                _plan_aht = (float(pd.to_numeric(_dem["AHT (sec)"], errors="coerce").mean())
                             if _dem is not None and "AHT (sec)" in _dem else np.nan)
                svc_rows.append({
                    "LOB": l,
                    # min_count=1 + float: an all-missing week must SHOW missing
                    # (int(sum()) re-zeroed it — audit#2 2026-07-14; int(NaN) would crash)
                    "Contacts (act)": float(g["Actual Contacts"].sum(min_count=1))
                                      if "Actual Contacts" in g else np.nan,
                    "SL% Target": float(_a.get("sl_target_pct")) if _a.get("sl_target_pct") else np.nan,
                    "SL% Actual": wavg("Actual SL %", "Actual Contacts"),
                    "Abandon %": wavg("Abandon %", "Actual Contacts"),
                    "AHT Plan (sec)": _plan_aht,
                    "AHT Actual (sec)": wavg("Actual AHT (sec)", "Actual Contacts"),
                })
            svc = pd.DataFrame(svc_rows).set_index("LOB")
            svc["SL Δ"] = svc["SL% Actual"] - svc["SL% Target"]
            svc["AHT Δ"] = svc["AHT Actual (sec)"] - svc["AHT Plan (sec)"]
            svc = svc[["Contacts (act)", "SL% Target", "SL% Actual", "SL Δ",
                       "Abandon %", "AHT Plan (sec)", "AHT Actual (sec)", "AHT Δ"]]

            def shade_delta(col):
                if col.name not in ("SL Δ", "AHT Δ"):
                    return [""] * len(col)
                bad_if_pos = col.name == "AHT Δ"   # heavier-than-planned AHT is bad
                return ["" if pd.isna(v)
                        else f"color:{brand.PINK}" if (v > 0) == bad_if_pos
                        else f"color:{brand.GREEN}" for v in col]

            st.dataframe(svc.style.apply(shade_delta, axis=0)
                         .format("{:,.1f}", na_rep="—")
                         .format("{:,.0f}", subset=["Contacts (act)"], na_rep="—"),
                         width="stretch")
            if "Days Covered" in sw:   # full = the queue's MODAL norm, never 7
                _n = (sw.groupby("LOB")["Days Covered"].transform(lambda x: x.mode().max())
                      - _holiday_allowance(sw["Week"]))
                partial = int((sw["Days Covered"] < _n).sum())
            else:
                partial = 0
            cap = (f"Contact-weighted across {sw['Week'].nunique()} loaded ACD week(s). "
                   "SL% = contacts answered within threshold ÷ offered; "
                   "AHT includes hold time. AHT Plan is the LOB's own assumption.")
            if partial:
                cap += (f" ⚠️ {partial} LOB-week(s) cover fewer days than their "
                        "queue's normal week — treat these figures as directional.")
            st.caption(cap)

    # ---- data health band ---------------------------------------------
    sw = st.session_state.get("acd_weekly")
    if sw is not None and not sw.empty and scope is not None and "LOB" in sw.columns:
        sw = sw[sw["LOB"] == scope]     # coverage for the line being looked at
    bits = []
    if sw is not None and not sw.empty:
        if "Days Covered" in sw:   # full = the queue's MODAL norm, never 7
            _n = (sw.groupby("LOB")["Days Covered"].transform(lambda x: x.mode().max())
                  - _holiday_allowance(sw["Week"]))
            full = int((sw["Days Covered"] >= _n).sum())
        else:
            full = 0
        bits.append(f"ACD actuals: <b>{sw['Week'].nunique()} wk</b> loaded"
                    + (f" (<b>{full}</b> full)" if full < sw["Week"].nunique()
                       else " (all full)"))
    else:
        bits.append("ACD actuals: <b>not loaded</b> — plan is unreconciled")
    _perf_line = _feed_perf_line()
    if _perf_line:
        bits.append(_perf_line)
    if st.session_state.get("_feed_errors"):
        bits.append(f"⚠️ feed auto-load hit <b>{len(st.session_state['_feed_errors'])}"
                    "</b> error(s) — open Real Data for details")
    ms = float(st.session_state.get("members_start", 0) or 0)
    me = float(st.session_state.get("members_end", 0) or 0)
    bits.append(f"Member base <b>{ms:,.0f} → {me:,.0f}</b>")
    with st.expander("Help with this page"):
        by_title = dict(GUIDE_SECTIONS)
        for t in PAGE_HELP["Executive View"]:
            if t in by_title:
                st.markdown(f"**{t}**")
                st.markdown(by_title[t])
        st.markdown("**Where the plan stands right now**")
        weekly_checklist()
        st.caption("Full task list on the Guide page.")

    brand.band("<b>Data health</b> — " + " · ".join(bits)
               + ". A plan reconciled against nothing is a guess — load actuals "
                 "on the Real Data page.")




# Demo builds replace None with a guided-tour section via the publish pipeline.
DEMO_TOUR_MD = """
This demo ships with a half-year of synthetic data telling one story: **Customer Support is slowly losing the staffing race.** Follow it:

1. **Executive View** — the verdict flags the shortfall; watch the heatmap turn red for Customer Support through Q2, with the dashed *today* line marking where actuals end.
2. **Real Data** — the actuals agree: service level slides from ~88% to the 50s in exactly the weeks occupancy pins near 90% and abandons climb. Try the *requirement benchmark* toggle while you're there.
3. **Capacity Plan** — open Customer Support: the reconciliation table shows plan vs actual per week. Press *Derive from actuals* to build a seasonality curve from 26 weeks of history.
4. **Hiring Advisor** — the payoff: it recommends the exact classes (lead-time and ramp aware) that would have prevented the June hole.

*Everything here is synthetic and private to your session — edit, publish, sandbox freely; it resets when you leave.*
"""

GUIDE_SECTIONS = [
    ("Every week — the plan review", """
1. Open **Real Data**. The ACD feed loads from the remembered
   locations automatically — check the notes at the top for warnings.
2. Look at **Days Covered** in the table. A week under 7 days is partial —
   don't treat its totals as a full week.
3. Go to **Capacity Plan** and open each line of business. The
   **reconciliation table** at the bottom shows Plan / Actual / Variance for
   contacts, AHT, SL%, staffing and shrinkage — red variances are where
   reality disagreed with the plan.
4. Adjust what the actuals justify: edit **CPM** where volume trended (it
   carries forward to later weeks on its own), use **Fcst Override** for
   one-off known events, update **AHT** if it has genuinely moved.
5. Check **Executive View** — the verdict line and heatmap tell you if the
   changes created or closed any shortfall.
6. **Publish** (sidebar) with a one-line note about what changed. That saves
   a new version everyone sees; nothing is ever overwritten.

*If something looks wrong:* variances that make no sense usually mean a
partial week (step 2) or an unmapped split — check the Real Data warnings.
"""),
    ("Someone goes on LOA", """
1. **Capacity Plan** → pick the LOB → **roster grid** (below the demand grid).
2. On the week the leave **starts**, set **LOA** to the number of people out.
   It carries forward automatically — no retyping every week.
3. On the **return week**, set it back down. Done.
4. Check the plan tab: **Net FTE** shows what the absence costs. If it turns
   a small line red, see the interim recipe below.
"""),
    ("Mentors pulled for a new-hire class", """
Training a class costs the floor twice: the class isn't taking calls yet, and
the agents mentoring it aren't either. The first half is already in the model
(classes only add capacity when they graduate). The second half is this row.

1. Ask **Learning & Development** how many agents they are taking, and for
   which weeks. That number is theirs to give — the app does not guess it from
   class size, because how many mentors a class needs varies by class.
2. **Capacity Plan** → pick the LOB → **roster grid** → **Mentors**. On the
   week they come off the phones, enter the count. It carries forward, exactly
   like LOA.
3. On the week they go back, set it to **0**. A mentor number left running is
   the one way this row can quietly understate you all year.
4. The plan grid gets a **Mentors** row; those FTE come out of **Staffed FTE**
   but stay in **Production HC** — they are still employed, still attriting.
"""),
    ("Covering a specialty shortfall with an interim", """
When a specialty line runs short (an LOA, a leaver), we backfill by borrowing
from Customer Support. The app prices this honestly:

1. Open **Hiring Advisor**.
2. The *Interim coverage* section lists each shortfall window with a
   recommendation — how many agents, for which weeks, **and what the pull
   does to Customer Support**. If MS goes short, the class plan below is the
   other half of the answer.
3. **Apply** books the move on both rosters: out at the start, back after the
   window. Arriving agents ramp briefly on the new line (that's priced in).
4. For a **permanent** backfill, enter it by hand instead: +1 on the specialty
   line's Transfers, −1 on MS, same week.
"""),
    ("Planning hiring classes", """
1. Open **Hiring Advisor**. The class plan section reads the current plan
   and answers: *when must classes start, and how big, to keep every week
   green* — accounting for training + coaching time, class attrition, and the
   fact that fresh grads aren't at full speed on day one.
2. Check the class template numbers above the table (training weeks, attrition
   %) — they seed from your last class.
3. Class starts respect the LOB's **hiring cadence** (sidebar: *min weeks
   between class starts*, *min/max class size*, and *only one class in the
   pipeline at a time* for a single training team). Set them per LOB — an
   external-hire pipeline runs one 10–20 seat cohort through training +
   nesting at a time; a specialty line cross-training 1–2 agents pulled from
   the main group can turn tiny "classes" around faster. Each class is sized
   to carry every red week until the next slot can land grads; a need too
   small for a cohort folds into the previous class or waits until justified.
4. **Apply** adds the classes to the New-Hire tab; review them there, then
   publish.
5. Red weeks flagged *uncoverable* need interims or overtime, not hiring —
   they're either inside the training lead time (no class can reach them) or
   between cadence slots (no sustainable class calendar lands grads in time).
"""),
    ("Answering \"who changed this, and why?\"", """
**Change log** page. Nothing has to be recorded for this to work — every
published version stores the whole plan, so the app works out what moved.

- **What changed**: a field-by-field diff of each version against the one
  before it — line of business, area, field, week, old value, new value.
- **Who and when**: stamped on the version at publish. Because only one
  person can hold edit control at a time, everything between two versions
  is theirs.
- **Why**: the note written at publish. That is the only part the app cannot
  work out, which is why the note is required.
- **Download as CSV** hands the whole thing to anyone who needs it outside
  the app.
- It is complete for versions published *before* the page existed — the
  diff is computed from the snapshots, not collected as you go.

*Writing a useful note:* say why, not what. "Adopted measured attrition after
the August reconciliation" beats "updated attrition" — the diff already shows
the number moved from 6 to 33.
"""),
    ("Trying a what-if safely", """
- **Sandbox** (sidebar) is your private copy. Change anything — nothing the
  team sees is touched until *you* publish. Save it as a personal what-if to
  keep it.
- To look at (or tinker with) an **older version**, open Version history
  (it lists the year you have selected) and press **Sandbox** on that version —
  it opens privately, the team plan stays put.
- **Restore** (editors only) makes an old version the team plan again — as a
  *new* version, so history stays complete.
"""),
    ("Reading the Executive View", """
- **The verdict** (big box) is the worst week's Net FTE — checked per LOB,
  because a surplus in one line can't cover a shortage in another.
- **Stat tiles**: the little curve is the year's shape; the **Δ chip** is the
  change vs the last published version — "what did our edits do."
- **Heatmap**: who is short, and when. Red = short, green = covered.
- The dashed **today** line marks where actuals end and plan begins.
- **Service table**: SL% and AHT, actuals vs forecast per LOB — actuals are
  colored against each line's SL target.
- **Ramp Discount** (plan grid): headcount that exists on paper but isn't at
  full productivity yet (new grads, arriving transfers).
"""),
    ("Data health warnings", """
- **Partial weeks** (Days Covered < 7): totals reflect only the days present.
  Fine for within-week comparisons; not a real weekly number.
- **Unmapped splits/queues**: surfaced with a warning, excluded by default —
  never silently dropped. Blend/overhead skills belong excluded; a real LOB
  split needs a row in the mapping file.
- **Dead remembered paths** (share offline / folder moved): loud warning, no
  silent fallback. Fix the path on the Real Data page.
- AUX codes you untick on the Shrinkage page stop counting as shrinkage
  **everywhere** — one shrinkage truth on both pages.
"""),
    ("Once a year — rolling into the new year", """
1. Sidebar → **Roll into <next year>** (needs edit control).
2. It seeds the new year from the current plan: ending headcount becomes
   starting headcount, year-end members become the new starting members, CPM
   and AHT carry, the seasonality shape copies, people on LOA or mentoring
   stay off the phones, and classes still in training graduate into the new
   year.
3. Enter the new year-end member forecast, review, then publish.
4. **This year keeps running.** Each year is its own plan with its own versions
   and its own edit lock, so publishing next year does not disturb this one —
   and someone else can carry on editing this year while you build next.
   Switch between them with **Plan year** at the top of the sidebar.
5. Nothing published is ever lost. Every prior year's versions stay in that
   year's history — open one in Sandbox any time.
"""),
    ("Working on next year while this year runs", """
Two years can be live at once, each with its own published versions and its own
edit lock.

1. **Plan year** (top of the sidebar) picks the year you are working in. It only
   appears once more than one year exists — roll over to create the next one.
2. Take control of the year you want to change. That leaves the other year
   untouched: a colleague can be editing it at the same time.
3. Your unpublished work is kept **per year**, so switching away and back offers
   your draft for that year — you cannot lose one year's edits by looking at
   another.
4. You can hold both years at once and the sidebar will say so. Release a year
   when you are done with it, otherwise everyone else is read-only on it.
"""),
    ("Building next year's budget", """
The budget answers two questions for leadership: **how many contacts** next year,
and **how many people** to serve them. It is derived from the plan — never typed
separately — so it cannot drift from what the team is actually planning.

1. Make sure the drivers are right, because everything else follows from them:
   **Members** (sidebar — this comes from Finance/org planning, you don't invent
   it) and **CPM** per LOB (Capacity Plan). Enter **Members (actual)** weekly
   as you go: it turns CPM from a guess into a measurement.
2. Roll into the new year (sidebar ). If a LOB has 26+ weeks of measured CPM,
   the panel shows its **trend** — tick the box to seed next year from the
   measured drift instead of carrying last year's number flat. You can always
   overrule any week afterwards.
3. Set the new **year-end member forecast** in the sidebar.
4. Open **Budget** → pick Monthly or Quarterly → read the totals, the peak
   period, and the per-LOB table. Download the CSV for the deck.
5. For a range (base / conservative / optimistic membership growth), use
   **Scenario compare** at the bottom of Budget: the builder saves
   member-growth variants of the working plan as personal what-ifs, and the
   compare table shows them side by side with Δs against your chosen base.
   Nothing shared moves — what-ifs are private to you.

*What to tell leadership:* "X contacts in <year> — that's <members> members from
Finance, times CPM of <cpm>, which is our measured rate <continuing its trend /
held flat>. It needs an average of N FTE, peaking at M in <period>."
"""),
    ("Pasting from Excel", """
Grids accept multi-cell paste (Ctrl/Cmd-V) — the fast way to load a year of
CPM or weekly Members (actual).

1. Paste **one column at a time**: click the first target cell, then paste
   the range. Paste is positional — it will not reorder columns for you.
2. Paste **raw numbers**: strip thousands separators, %, and $ first. A value
   that doesn't fit its column is dropped silently (the console may log a
   scary-looking ValueError — the app is fine; just re-check that cell).
3. Step-change columns (CPM, LOA, Mentors, Supervisors, Leads) carry the LAST
   pasted value forward through later weeks.
4. New-Hire **Class Start Week** must be the ISO Monday exactly
   (2026-01-05) — format Excel date cells as text before copying.
"""),
    ("Five things worth knowing", """
1. **Each plan year is its own shared plan**; what you see is the latest
   published version of the year picked at the top of the sidebar. Next year
   can be built while this year keeps running.
2. **Publish = save & share.** Take control first so two people can't edit the
   same year at once; read-only just means someone else has that year.
3. **Nothing can be lost.** Every published version is permanent; drafts
   auto-save your unsaved edits and offer them back next session.
4. **Step-change columns carry forward** on edit: CPM, LOA, Mentors,
   Supervisors, Leads. Transfers don't — they're one-time events.
5. **Supervisors/Leads are informational** — they show span-of-control drift
   but never change Staffed or Net FTE.
"""),
]


# Which Guide recipes belong on which page. The recipes themselves live ONCE
# in GUIDE_SECTIONS — page help surfaces them in place, it never copies them.
PAGE_HELP = {
    "Executive View": ["Reading the Executive View", "Five things worth knowing"],
    "Capacity Plan": ["Every week — the plan review", "Someone goes on LOA",
                         "Mentors pulled for a new-hire class",
                         "Trying a what-if safely", "Pasting from Excel"],
    "Hiring Advisor": ["Covering a specialty shortfall with an interim",
                          "Planning hiring classes"],
    # Only recipes for the task you do ON this page — pairing Budget with the
    # Executive View's reading guide was an authoring slip, not a design.
    "Budget": ["Building next year's budget", "Once a year — rolling into the new year",
               "Working on next year while this year runs"],
    "Real Data": ["Every week — the plan review", "Data health warnings"],
    "ACD Shrinkage": ["Data health warnings"],
    "Change log": ["Answering \"who changed this, and why?\""],
}


# Recipe → the page where you actually do it (deep-link targets).
RECIPE_PAGE = {
    "Building next year's budget": "Budget",
    "Every week — the plan review": "Real Data",
    "Someone goes on LOA": "Capacity Plan",
    "Mentors pulled for a new-hire class": "Capacity Plan",
    "Answering \"who changed this, and why?\"": "Change log",
    "Covering a specialty shortfall with an interim": "Hiring Advisor",
    "Planning hiring classes": "Hiring Advisor",
    "Reading the Executive View": "Executive View",
    "Data health warnings": "Real Data",
    "Pasting from Excel": "Capacity Plan",
}


def visible_recipes():
    """GUIDE_SECTIONS minus recipes whose page is hidden (see HIDDEN_PAGES).

    A recipe for a decommissioned page is worse than no recipe: it walks the
    planner through steps on a page they cannot open, and its deep-link button
    would stage a `nav_page` value the radio has no option for."""
    return [(t, md) for t, md in GUIDE_SECTIONS
            if RECIPE_PAGE.get(t) not in HIDDEN_PAGES]


def _goto(page: str, key: str):
    """Deep link — jump to the page the recipe is about. Stages the target; the
    top-of-script block applies it before the nav radio instantiates (writing a
    widget's own key after it renders raises StreamlitAPIException). This runs
    during the dispatch, i.e. AFTER that block — hence the st.rerun(), which is
    what gets the staged value applied on the next pass."""
    if st.button(f"→ Go to {page}", key=key):
        st.session_state["_nav_goto"] = page
        st.rerun()


def page_help(page: str):
    """Help for THIS page — renders the relevant Guide recipes where the
    planner already is. Single source: GUIDE_SECTIONS."""
    titles = PAGE_HELP.get(page, [])
    if not titles:
        return
    by_title = dict(visible_recipes())
    with st.expander("Help with this page"):
        for t in titles:
            if t in by_title:
                st.markdown(f"**{t}**")
                st.markdown(by_title[t])
        st.caption("Full task list on the Guide page.")


def weekly_checklist():
    """Self-checking weekly review: every item is COMPUTED from live state, so
    it cannot go stale. Not documentation — a status panel."""
    sw = st.session_state.get("acd_weekly")
    act = collab.read_active(SCENARIO_DIR, _plan_year())
    lock = collab.read_lock(SCENARIO_DIR, _plan_year())
    items = []

    # 1 — actuals loaded? One feed carries all of it now (2026-07-31), so
    # this is one item rather than two.
    if sw is not None and not sw.empty:
        wks = sw["Week"].nunique()
        if "Days Covered" in sw.columns:
            _n = (sw.groupby("LOB")["Days Covered"].transform(lambda x: x.mode().max())
                  - _holiday_allowance(sw["Week"]))
            partial = int(sw[sw["Days Covered"] < _n]["Week"].nunique())
        else:
            partial = 0
        items.append(("✅", f"Actuals loaded — **{wks}** week(s)"
                      + (f", ⚠️ **{partial}** partial (don't read as full weeks)"
                         if partial else ", all full weeks")))
        if "Actual Contacts" not in sw.columns:
            items.append(("⚠️", "The ACD export has **no volume columns** — measured "
                                "CPM, seasonality-derive and reconciliation stay off "
                                "until it includes them (Real Data → Column mapping)"))
    else:
        items.append(("⬜", "**Load actuals** — Real Data page "
                            "(remembered locations load them automatically)"))

    # 2 — does the plan cover the horizon?
    plans = {l: compute_plan(d) for l, d in st.session_state.lobs.items()}
    short = [l for l, p in plans.items()
             if float(p["Required FTE"].mean()) > 0 and float(p["Net FTE"].min()) < 0]
    if not any(float(p["Required FTE"].mean()) > 0 for p in plans.values()):
        items.append(("⬜", "**No demand entered yet** — set Members (sidebar) and "
                            "CPM per LOB on Capacity Plan"))
    elif short:
        # The fix used to be "see Hiring Advisor". With that page hidden the
        # honest instruction is the one the planner can actually follow.
        fix = ("— see Hiring Advisor for the fix"
               if "Hiring Advisor" not in HIDDEN_PAGES
               else "— add hiring classes or transfers on Capacity Plan")
        items.append(("⚠️", f"**{len(short)} LOB(s) run short**: {', '.join(short)} "
                            f"{fix}"))
    else:
        items.append(("✅", "Every LOB covered across the horizon"))

    # 2b — past weeks hindcast on facts: blank attrition in an elapsed week is
    # "nobody left" (0). A recording lapse must be VISIBLE, never a silent
    # guess — this is the counterweight that makes blank-past=0 honest.
    assumed = []
    for l, d in st.session_state.lobs.items():
        if float(d["assumptions"].get("starting_hc", 0) or 0) <= 0:
            continue
        ros = d["roster"]
        if "Attrition (actual)" not in ros.columns:
            continue
        blank = pd.to_numeric(ros["Attrition (actual)"],
                              errors="coerce").isna().to_numpy()
        n_ass = int((_weeks_passed(ros["Week"].tolist()) & blank).sum())
        if n_ass:
            assumed.append(f"{l} ({n_ass})")
    if assumed:
        items.append(("ℹ️", "**Past weeks with no recorded attrition — treated as "
                            "0 leavers**: " + ", ".join(assumed)
                            + ". Confirm nobody left, or record the departures "
                            "(roster grid → *Attrition (actual)*)"))

    # 3 — edit control / publishing state
    user = st.session_state.user
    if st.session_state.get("sandbox"):
        items.append(("", "You're in **Sandbox** — nothing you change is shared"))
    elif lock and lock.get("user") == user:
        items.append(("", "You hold **edit control** — publish when you're done"))
    elif lock and not collab.lock_is_stale(lock):
        items.append(("", f"**{lock.get('user')}** is editing — you're read-only"))
    else:
        items.append(("⬜", "**Take control** (sidebar) to edit the team plan"))

    # 4 — unpublished work?
    if act:
        lv = st.session_state.get("loaded_version")
        try:
            snap = collab.load_snapshot(SCENARIO_DIR, act["file"]) or {}
            keys = ("n_weeks", "plan_year", "members_start", "members_end", "lobs")
            cur = {k: v for k, v in _serialize_lobs().items() if k in keys}
            ref = {k: snap.get(k) for k in keys if k in snap}
            dirty = json.dumps(cur, sort_keys=True, default=str) != \
                json.dumps(ref, sort_keys=True, default=str)
        except Exception:  # noqa: BLE001
            dirty = False
        if dirty:
            items.append(("⚠️", f"**Unpublished changes** vs published v{act['version']}"
                                " — publish (sidebar) to share them"))
        else:
            items.append(("✅", f"Working plan matches published "
                                f"**v{act['version']}** ({act.get('name','')})"))
    else:
        items.append(("⬜", "**Nothing published yet** — publish a first version "
                            "so the team has a shared plan"))

    st.markdown("\n".join(f"- {icon} {txt}" for icon, txt in items))
    st.caption("Computed from the live plan and feeds — it updates itself.")


def period_key(iso_week: str, grain: str) -> str:
    """Which month/quarter a plan week belongs to. Convention: a week belongs to
    the period containing its MONDAY (plan weeks are Monday-anchored), so a week
    straddling a month boundary counts once, in the month it started. Stated
    plainly on the page — leadership will ask."""
    d = date.fromisoformat(iso_week)
    if grain == "Quarterly":
        return f"{d.year} Q{(d.month - 1) // 3 + 1}"
    return f"{d.year}-{d.month:02d} {d.strftime('%b')}"


def budget_table(grain: str, lobs: dict | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(long table, period order) — the budget artifact.

    Per LOB per period: contacts are SUMMED (a volume budget), FTE figures are
    AVERAGED across the period's weeks (headcount is a level, not a flow), and
    peak required FTE is the MAX (the week the period has to survive).
    `lobs` defaults to the working plan; scenario compare passes parsed
    snapshot payloads instead."""
    rows, order = [], []
    for lob, d in (st.session_state.lobs if lobs is None else lobs).items():
        plan = compute_plan(d)
        weeks = plan["Week"].tolist()
        keys = [period_key(w, grain) for w in weeks]
        for k in keys:
            if k not in order:
                order.append(k)
        g = plan.assign(_p=keys).groupby("_p", sort=False)
        for per, sub in g:
            rows.append({
                "LOB": lob, "Period": per, "Weeks": int(len(sub)),
                "Contacts": float(sub["Forecast (final)"].sum()),
                "Avg Required FTE": float(sub["Required FTE"].mean()),
                "Peak Required FTE": float(sub["Required FTE"].max()),
                "Avg Staffed FTE": float(sub["Staffed FTE"].mean()),
                "Worst Net FTE": float(sub["Net FTE"].min()),
                "Avg Overall HC": float(sub["Overall HC"].mean())
                if "Overall HC" in sub.columns else np.nan,
            })
    long = pd.DataFrame(rows)
    return long, order


# ---- budget baseline: the locked-in plan, and how far we have drifted -------
# `active` is the latest published truth; `budget` is what was committed to
# before the year started. Both point at immutable snapshots, so "drift" is a
# recomputation of two payloads through ONE engine — never a stored number that
# could disagree with the plan it claims to describe.
BUDGET_DRIFT_COLS = ["Contacts", "Avg Required FTE", "Peak Required FTE",
                     "Avg Staffed FTE", "Worst Net FTE", "Avg Overall HC"]


def budget_baseline(year: int | None = None) -> dict | None:
    """The locked budget pointer for `year`, or None if nothing is locked."""
    return collab.read_budget(SCENARIO_DIR, plan_year() if year is None else year)


@st.cache_data(show_spinner=False, max_entries=32)
def _budget_snapshot(scen_dir: str, fname: str):
    """Published snapshots are immutable, so the filename is a complete key —
    same reasoning as the Change log's caches."""
    return collab.load_snapshot(scen_dir, fname)


def budget_drift(grain: str, lobs: dict | None = None
                 ) -> tuple[pd.DataFrame, dict] | None:
    """(per-LOB-per-period drift frame, meta) for working plan vs locked budget.

    Δ = working − budget, so a POSITIVE Required FTE drift means the year is
    asking for more people than were budgeted. Returns None when no budget is
    locked, when its snapshot is unreadable, or when the two cannot be lined up
    (different plan year or horizon) — a misaligned comparison is refused, not
    approximated, the same rule scenario compare already follows.

    `lobs` defaults to the working plan, mirroring `budget_table`; passing it
    explicitly is how read-only consumers (and headless checks, which run
    OUTSIDE a script run where `st.session_state` is not the app's) compare
    frames they already hold."""
    ptr = budget_baseline()
    if not ptr:
        return None
    snap = _budget_snapshot(str(SCENARIO_DIR), ptr.get("file", ""))
    if not snap:
        return None
    b_lobs, b_meta = _scenario_frames(snap)
    w_lobs, w_meta = _scenario_frames(None)
    if lobs is not None:
        w_lobs = lobs
        w_meta = {**w_meta, "n_weeks": b_meta["n_weeks"]
                  if all(len(d["demand"]) == b_meta["n_weeks"] for d in lobs.values())
                  else w_meta["n_weeks"]}
    if (b_meta["plan_year"] != w_meta["plan_year"]
            or b_meta["n_weeks"] != w_meta["n_weeks"]):
        return None, {"misaligned": True, "ptr": ptr,
                      "budget": b_meta, "working": w_meta}
    b_long, b_order = budget_table(grain, lobs=b_lobs)
    w_long, _ = budget_table(grain, lobs=w_lobs)
    if b_long.empty or w_long.empty:
        return None
    keys = ["LOB", "Period"]
    m = w_long.merge(b_long, on=keys, how="outer", suffixes=(" (plan)", " (budget)"))
    for c in BUDGET_DRIFT_COLS:
        m[f"{c} Δ"] = m[f"{c} (plan)"] - m[f"{c} (budget)"]
    return m, {"misaligned": False, "ptr": ptr, "order": b_order,
               "budget": b_meta, "working": w_meta}


def budget_drift_headline(grain: str = "Monthly", lobs: dict | None = None
                          ) -> dict | None:
    """One-line drift for the Executive View: totals across the year, so the
    landing page can say how far we are from budget without opening Budget."""
    got = budget_drift(grain, lobs=lobs)
    if not got or got[0] is None:
        return None
    m, meta = got
    order = meta["order"]
    per = m.groupby("Period", sort=False)
    return {
        "version": meta["ptr"].get("version"),
        "name": meta["ptr"].get("name", ""),
        "contacts": float(m["Contacts Δ"].sum()),
        "contacts_pct": (float(m["Contacts Δ"].sum())
                         / float(m["Contacts (budget)"].sum()) * 100
                         if float(m["Contacts (budget)"].sum()) else np.nan),
        # FTE is a LEVEL, so the year figure is the mean of period sums — the
        # same aggregation the Budget page's own stat row uses. Summing an
        # average across twelve months would report twelve times the drift.
        "req": float(per["Avg Required FTE (plan)"].sum().reindex(order).mean()
                     - per["Avg Required FTE (budget)"].sum().reindex(order).mean()),
        "stf": float(per["Avg Staffed FTE (plan)"].sum().reindex(order).mean()
                     - per["Avg Staffed FTE (budget)"].sum().reindex(order).mean()),
    }


# Weekly DRIVERS live in the grids, not the assumptions dict, but a planner
# calls them assumptions all the same — "we planned AHT 400" is a demand-grid
# column. They are summarised as the mean across the horizon, with a count of
# how many weeks actually moved, because a single number cannot say whether a
# change was one week or the whole year.
BUDGET_DRIVER_COLS = [("demand", "CPM"), ("demand", "AHT (sec)"),
                      ("demand", "Seasonality"), ("demand", "Members"),
                      ("roster", "LOA"), ("roster", "Mentors"),
                      ("roster", "Supervisors"), ("roster", "Leads/Project")]


def _num(v):
    try:
        f = float(v)
        return None if np.isnan(f) else f
    except (TypeError, ValueError):
        return None


def budget_assumption_drift(lobs: dict | None = None
                            ) -> tuple[pd.DataFrame, dict] | None:
    """What we PLANNED WITH, budget vs now — the assumptions behind the numbers.

    The output table (contacts, FTE) says the year moved; this says WHY. Both
    read the same locked snapshot: a published version already carries every
    assumption and every weekly driver, so locking a budget captured all of
    this from the start — only the view was missing (user 2026-08-02: "the
    budget will need all the plan assumptions like AHT and shrinkage … my
    manager means locking assumptions of what we planned for").

    Columns: LOB, Assumption, Budget, Now, Δ, Weeks changed. Non-numeric
    assumptions (requirement basis, one-class-at-a-time) carry a blank Δ rather
    than a fabricated 0."""
    ptr = budget_baseline()
    if not ptr:
        return None
    snap = _budget_snapshot(str(SCENARIO_DIR), ptr.get("file", ""))
    if not snap:
        return None
    b_lobs = _payload_lobs(snap)
    w_lobs = st.session_state.lobs if lobs is None else lobs
    rows = []

    def add(lob, name, was, now, weeks=""):
        a, b = _num(was), _num(now)
        rows.append({
            "LOB": lob, "Assumption": name,
            "Budget": _fmt_cell(was), "Now": _fmt_cell(now),
            "Δ": "" if (a is None or b is None) else round(b - a, 3),
            "Weeks changed": weeks,
            "_changed": not _cells_equal(was, now)})

    # Plan-level drivers first — they are org-wide, so repeating them per LOB
    # would read as six independent decisions.
    for key, label in (("members_start", "Members — start"),
                       ("members_end", "Members — year-end"),
                       ("n_weeks", "Planning horizon (weeks)")):
        add("(plan)", label, snap.get(key), _serialize_plan_scalar(key))

    for lob in sorted(set(b_lobs) | set(w_lobs)):
        if lob not in b_lobs:
            add(lob, "Line of business", "", "added since the budget")
            continue
        if lob not in w_lobs:
            add(lob, "Line of business", "in the budget", "removed")
            continue
        ba, wa = b_lobs[lob]["assumptions"], w_lobs[lob]["assumptions"]
        for k in ASSUMPTION_LABELS:
            if k in ("members_start", "members_end"):
                continue                       # plan-level, already above
            if k not in ba and k not in wa:
                continue
            add(lob, ASSUMPTION_LABELS[k], ba.get(k), wa.get(k))
        for frame, col in BUDGET_DRIVER_COLS:
            bf, wf = b_lobs[lob][frame], w_lobs[lob][frame]
            if col not in bf.columns or col not in wf.columns:
                continue
            bs = pd.to_numeric(bf[col], errors="coerce")
            ws = pd.to_numeric(wf[col], errors="coerce")
            moved = ("" if len(bs) != len(ws)
                     else int((~np.isclose(bs.to_numpy(dtype=float),
                                           ws.to_numpy(dtype=float),
                                           equal_nan=True)).sum()))
            add(lob, f"{col} (weekly avg)", bs.mean(), ws.mean(), moved)

    frame = pd.DataFrame(rows)
    return frame, {"ptr": ptr}


def _serialize_plan_scalar(key: str):
    """Plan-level scalars as the payload serializer would write them, without
    paying to serialize every LOB frame just to read three numbers."""
    if key == "n_weeks":
        return int(st.session_state.n_weeks)
    return st.session_state.get(key)


def _member_variant_payload(me_new: float) -> dict:
    """The working plan with ONLY the year-end member target changed. Members
    re-spread with the SAME linear rule as apply_global_members (org-wide,
    settled); Members (actual) is untouched — actuals are facts, not scenario
    inputs. Round-trips through the payload serializer so the variant mutates
    copies, never the live working frames."""
    p = _serialize_lobs()
    n = int(p["n_weeks"])
    ms = float(p["members_start"] or 0.0)
    members = np.round(np.linspace(ms, float(me_new), n), 0)
    lobs = _payload_lobs(p)
    for d in lobs.values():
        if len(d["demand"]) == n:
            d["demand"]["Members"] = members
    p["members_end"] = float(me_new)
    p["lobs"] = {lob: {"demand": d["demand"].to_json(orient="split"),
                       "roster": d["roster"].to_json(orient="split"),
                       "nh": d["nh"].to_json(orient="split"),
                       "assumptions": d["assumptions"]}
                 for lob, d in lobs.items()}
    return p


def _scenario_candidates() -> dict:
    """Label → payload for everything comparable: the working plan (None), my
    personal what-ifs, published versions. Snapshot payloads come back whole
    from collab (lobs included), so no extra file reads."""
    cands = {"▶ Working plan": None}

    def add(label, j):
        base, i = label, 2
        while label in cands:          # same name saved twice in one minute
            label, i = f"{base} ({i})", i + 1
        cands[label] = j

    for j in collab.personal_snapshots(SCENARIO_DIR, st.session_state.user):
        add(f"{j.get('name', 'what-if')} · {_hm(j.get('published_at', ''))}", j)
    # The locked budget is just a published version, but it is the one people
    # come here to compare against — mark it so it is findable in a long list.
    _b = collab.read_budget(SCENARIO_DIR, plan_year()) or {}
    _bver = _b.get("version")
    for j in collab.changelog(SCENARIO_DIR):
        _star = "★ Budget · " if j["version"] == _bver else ""
        add(f"{_star}v{j['version']} {j.get('name', '')} · "
            f"{j.get('plan_year', DEFAULT_PLAN_YEAR)}", j)
    return cands


def _scenario_frames(payload) -> tuple[dict, dict]:
    """(lobs dict for budget_table, scenario meta). None = the working plan."""
    if payload is None:
        return st.session_state.lobs, {
            "plan_year": plan_year(),
            "n_weeks": int(st.session_state.n_weeks),
            "members_start": float(st.session_state.get("members_start", 0.0) or 0.0),
            "members_end": float(st.session_state.get("members_end", 0.0) or 0.0)}
    lobs = _payload_lobs(payload)
    ms, me = payload.get("members_start"), payload.get("members_end")
    if ms is None or me is None:       # pre-members-driver snapshots
        first = next(iter(lobs.values()), None)
        m = first["demand"]["Members"] if first is not None else None
        ms = float(m.iloc[0]) if m is not None else 0.0
        me = float(m.iloc[-1]) if m is not None else 0.0
    return lobs, {"plan_year": int(payload.get("plan_year", DEFAULT_PLAN_YEAR)),
                  "n_weeks": int(payload["n_weeks"]),
                  "members_start": float(ms or 0.0), "members_end": float(me or 0.0)}


def _budget_headline(long: pd.DataFrame, order: list) -> dict:
    """One scenario's headline figures, aggregated exactly like the page's
    stat row (mean/max of per-period sums; worst Net = worst LOB-period)."""
    peak = long.groupby("Period", sort=False)["Peak Required FTE"].sum().reindex(order)
    avg = long.groupby("Period", sort=False)["Avg Required FTE"].sum().reindex(order)
    return {"Contacts": float(long["Contacts"].sum()),
            "Avg Required FTE": float(avg.mean()),
            "Peak Required FTE": float(peak.max()),
            "Worst Net FTE": float(long["Worst Net FTE"].min())}


def render_scenario_compare(grain: str, metric: str):
    """Scenario compare — budget backlog item 1c. Recomputes each picked
    scenario through the same engine and rollup as the page above; the 
    builder saves member-growth variants as personal what-ifs (never touches
    the shared pointer, so it works in any mode)."""
    import altair as alt
    st.divider()
    st.subheader("Scenario compare")
    st.caption("Base / conservative / optimistic — or any saved what-if or published "
               "version — side by side, recomputed through the same engine as the "
               "table above. Δ columns compare against the chosen base scenario. "
               "Uses the Grain and Metric pickers at the top of the page.")
    msg = st.session_state.pop("_cmp_saved_msg", None)
    if msg:
        st.success(msg)

    # ---- builder: member-growth variants without three sandbox round-trips
    ms = float(st.session_state.get("members_start", 0.0) or 0.0)
    me = float(st.session_state.get("members_end", 0.0) or 0.0)
    with st.expander("Create member-growth variants from the working plan"):
        if ms <= 0 or not st.session_state.get("lobs"):
            st.info("Set **Members** in the sidebar first — variants re-spread the "
                    "org-wide member base toward a different year-end target.")
        else:
            st.caption(f"Working plan: **{ms:,.0f} → {me:,.0f}** members. Each variant "
                       "keeps everything else (CPM, seasonality, roster, classes) and "
                       "changes only the year-end member target. Saved as **personal "
                       "what-ifs** — nothing shared moves.")
            _seed = me if me > 0 else ms
            c1, c2 = st.columns(2)
            n1 = c1.text_input("Variant 1 name", "conservative", key="as_cmp_nm1")
            m1 = c1.number_input("Variant 1 — year-end members", 0.0, 50_000_000.0,
                                 float(round(_seed * 0.98)), 1000.0, key="as_cmp_me1")
            n2 = c2.text_input("Variant 2 name", "optimistic", key="as_cmp_nm2")
            m2 = c2.number_input("Variant 2 — year-end members", 0.0, 50_000_000.0,
                                 float(round(_seed * 1.02)), 1000.0, key="as_cmp_me2")
            if st.button("Save as personal what-ifs", key="cmp_save"):
                saved = []
                for nm, mv in ((n1, m1), (n2, m2)):
                    if nm.strip() and mv > 0:
                        collab.save_personal(SCENARIO_DIR, _member_variant_payload(mv),
                                             nm.strip(), st.session_state.user)
                        saved.append(f"{nm.strip()} ({mv:,.0f})")
                if saved:
                    st.session_state["_cmp_saved_msg"] = ("Saved: " + " · ".join(saved)
                                                          + " — pick them below.")
                st.rerun()

    # ---- scenario picker. Sanitize stored selections BEFORE the widgets
    # instantiate (a deleted what-if in a stored selection would raise).
    cands = _scenario_candidates()
    # Keyed widget: seed/sanitize session state BEFORE it instantiates and
    # pass NO default — mixing both draws a Streamlit warning about ambiguous
    # initialization (audit 2026-07-14).
    if "cmp_picks" not in st.session_state:
        st.session_state["cmp_picks"] = list(cands)[:1]
    else:
        st.session_state["cmp_picks"] = [x for x in st.session_state["cmp_picks"]
                                         if x in cands]
    picks = st.multiselect("Scenarios (2–4)", list(cands),
                           key="cmp_picks", max_selections=4)
    if len(picks) < 2:
        st.info("Pick at least two scenarios — the builder above can create "
                "member-growth variants from the working plan.")
        return
    if "cmp_base" in st.session_state and st.session_state["cmp_base"] not in picks:
        del st.session_state["cmp_base"]
    base_label = st.selectbox("Base scenario (Δ reference)", picks, key="cmp_base")

    # ---- recompute every scenario; refuse silent misalignment
    base_lobs, base_meta = _scenario_frames(cands[base_label])
    results, skipped = {}, []
    for lbl in picks:
        lobs, meta = ((base_lobs, base_meta) if lbl == base_label
                      else _scenario_frames(cands[lbl]))
        if (meta["plan_year"], meta["n_weeks"]) != (base_meta["plan_year"],
                                                    base_meta["n_weeks"]):
            skipped.append(f"{lbl} ({meta['plan_year']}, {meta['n_weeks']} wks)")
            continue
        long, order = budget_table(grain, lobs=lobs)
        results[lbl] = (long, order, meta)
    if skipped:
        st.warning("Skipped — different plan year or horizon than the base, so "
                   "periods wouldn't align: " + "; ".join(skipped))
    if len(results) < 2:
        return

    # ---- headline compare, Δ vs base
    base_long, base_order, _ = results[base_label]
    bh = _budget_headline(base_long, base_order)
    rows = []
    for lbl, (long, order, meta) in results.items():
        h = _budget_headline(long, order)
        growth = ((meta["members_end"] / meta["members_start"] - 1) * 100
                  if meta["members_start"] > 0 else np.nan)
        rows.append({
            "Scenario": lbl,
            "Members (year-end)": meta["members_end"],
            "Member growth %": growth,
            "Contacts": h["Contacts"],
            "Δ Contacts %": ((h["Contacts"] / bh["Contacts"] - 1) * 100
                             if bh["Contacts"] else np.nan),
            "Avg Required FTE": h["Avg Required FTE"],
            "Δ Avg FTE": h["Avg Required FTE"] - bh["Avg Required FTE"],
            "Peak Required FTE": h["Peak Required FTE"],
            "Δ Peak FTE": h["Peak Required FTE"] - bh["Peak Required FTE"],
            "Worst Net FTE": h["Worst Net FTE"],
        })
    cmp_df = pd.DataFrame(rows).set_index("Scenario")
    st.dataframe(cmp_df.style.format({
        "Members (year-end)": "{:,.0f}", "Member growth %": "{:+.1f}%",
        "Contacts": "{:,.0f}", "Δ Contacts %": "{:+.1f}%",
        "Avg Required FTE": "{:,.1f}", "Δ Avg FTE": "{:+.1f}",
        "Peak Required FTE": "{:,.1f}", "Δ Peak FTE": "{:+.1f}",
        "Worst Net FTE": "{:,.1f}"}, na_rep="—"), width="stretch")

    # ---- period × scenario for the page's chosen metric
    per = pd.DataFrame({lbl: long.groupby("Period", sort=False)[metric].sum()
                        .reindex(base_order)
                        for lbl, (long, order, meta) in results.items()})
    fmt = "{:,.0f}" if metric in ("Contacts", "Avg Overall HC") else "{:,.1f}"
    st.dataframe(per.style.format(fmt, na_rep="—"), width="stretch")
    if metric == "Worst Net FTE":
        st.caption("⚠️ Summed across LOBs — a consolidated figure hides which line "
                   "is short. Use the per-LOB table above for that.")

    cdf = (per.rename_axis("Period").reset_index()
           .melt("Period", var_name="Scenario", value_name=metric))
    palette = brand.CATEGORICAL
    brand.chart(
        alt.Chart(cdf).mark_bar(cornerRadius=2, opacity=.9).encode(
            x=alt.X("Period:O", sort=None, axis=alt.Axis(labelAngle=-45, title=None)),
            xOffset=alt.XOffset("Scenario:N", sort=list(results)),
            y=alt.Y(f"{metric}:Q", title=metric),
            color=alt.Color("Scenario:N", sort=list(results),
                            scale=alt.Scale(range=palette[:len(results)]),
                            legend=alt.Legend(orient="bottom", title=None)),
            tooltip=["Period", "Scenario",
                     alt.Tooltip(f"{metric}:Q", format=",.1f")]),
        height=260)

    st.download_button(
        f"Download the scenario compare ({grain.lower()}, all metrics) as CSV",
        pd.concat([long.assign(Scenario=lbl)
                   for lbl, (long, order, meta) in results.items()]).to_csv(index=False),
        file_name=f"budget_scenarios_{plan_year()}_{grain.lower()}.csv",
        mime="text/csv", key="cmp_csv")


def render_budget_page():
    import altair as alt
    yr = plan_year()
    st.header(f"Budget — {yr} volume & FTE")
    st.caption(
        f"The plan as leadership consumes it: **how many contacts** we expect in "
        f"{yr} and **how many people** it takes to serve them, by month or quarter. "
        "Contacts are summed across the period; FTE is averaged (headcount is a "
        "level, not a flow) with the peak week shown alongside. A week belongs to "
        "the period containing its Monday.")
    page_help("Budget")

    c1, c2 = st.columns([1, 2])
    grain = c1.radio("Grain", ["Monthly", "Quarterly"], horizontal=True,
                     key="budget_grain")
    metric = c2.selectbox(
        "Metric", ["Contacts", "Avg Required FTE", "Peak Required FTE",
                   "Avg Staffed FTE", "Worst Net FTE", "Avg Overall HC"],
        key="budget_metric")

    long, order = budget_table(grain)
    if long.empty:
        st.info("No LOBs in the plan yet.")
        return
    if float(long["Contacts"].sum()) <= 0:
        st.warning("No demand in the plan yet — set **Members** (sidebar) and **CPM** "
                   "per LOB on Capacity Plan, then come back. The budget is derived "
                   "from those drivers; it is never entered separately.")

    # ---- the drivers, stated up front: every number below traces to these ----
    ms = float(st.session_state.get("members_start", 0.0) or 0.0)
    me = float(st.session_state.get("members_end", 0.0) or 0.0)
    cpm_bits = []
    for lob, d in st.session_state.lobs.items():
        c = pd.to_numeric(d["demand"]["CPM"], errors="coerce")
        if float(c.mean() or 0) <= 0:
            continue
        trend = ("→" if abs(float(c.iloc[-1]) - float(c.iloc[0])) < 0.005
                 else ("↓" if float(c.iloc[-1]) < float(c.iloc[0]) else "↑"))
        cpm_bits.append(f"{lob} <b>{float(c.mean()):.2f}</b>{trend}")
    growth = f"({(me / ms - 1) * 100:+.1f}%)" if ms > 0 else "(no member base entered)"
    brand.band(
        f"<b>Drivers</b> — Members <b>{ms:,.0f} → {me:,.0f}</b> {growth} · "
        "CPM (avg, direction): "
        + " · ".join(cpm_bits or ["<i>none entered</i>"])
        + f" · contacts = Members × CPM ÷ {WEEKS_PER_YEAR} × seasonality")

    # ---- headline totals ----------------------------------------------------
    tot_contacts = float(long["Contacts"].sum())
    peak = long.groupby("Period", sort=False)["Peak Required FTE"].sum()
    avg_req = (long.groupby("Period", sort=False)["Avg Required FTE"].sum()
               .reindex(order))
    worst_p = peak.reindex(order).idxmax() if len(peak) else "—"
    brand.stat_row([
        {"label": f"{yr} contacts", "value": f"{tot_contacts:,.0f}",
         "spark": long.groupby("Period", sort=False)["Contacts"].sum()
                      .reindex(order).tolist(), "spark_color": brand.CYAN},
        {"label": "Avg required FTE", "value": f"{avg_req.mean():.1f}",
         "spark": avg_req.tolist(), "spark_color": brand.VIOLET},
        {"label": "Peak required FTE", "value": f"{peak.max():.1f}",
         "spark": peak.reindex(order).tolist(), "spark_color": brand.AMBER},
        {"label": "Peak period", "value": str(worst_p).split(" ")[-1]},
    ])

    # ---- LOB × period pivot for the chosen metric ---------------------------
    piv = long.pivot_table(index="LOB", columns="Period", values=metric,
                           aggfunc="sum").reindex(columns=order)
    total = piv.sum(axis=0).to_frame().T
    total.index = ["— TOTAL —"]
    grid = pd.concat([piv, total])
    fmt = "{:,.0f}" if metric in ("Contacts", "Avg Overall HC") else "{:,.1f}"
    st.dataframe(grid.style.format(fmt, na_rep="—"), width="stretch")
    if metric == "Worst Net FTE":
        st.caption("⚠️ The TOTAL row sums each LOB's worst week — a consolidated "
                   "figure hides which line is short. Read the per-LOB rows.")

    # ---- one chart, one axis ------------------------------------------------
    chart_df = long.groupby("Period", sort=False)[metric].sum().reindex(order) \
        .rename_axis("Period").reset_index()
    brand.chart(
        alt.Chart(chart_df).mark_bar(cornerRadius=3, opacity=.9).encode(
            x=alt.X("Period:O", sort=None, axis=alt.Axis(labelAngle=-45, title=None)),
            y=alt.Y(f"{metric}:Q", title=metric),
            color=alt.value(brand.CYAN if metric == "Contacts" else brand.VIOLET),
            tooltip=["Period", alt.Tooltip(f"{metric}:Q", format=",.1f")]),
        height=240)

    # ---- the export ---------------------------------------------------------
    st.download_button(
        f"Download the {yr} budget ({grain.lower()}, all metrics) as CSV",
        long.assign(**{"Plan year": yr, "Grain": grain}).to_csv(index=False),
        file_name=f"budget_{yr}_{grain.lower()}.csv", mime="text/csv")
    st.caption("Every figure is derived from the published plan's drivers — nothing "
               "here is entered separately, so the budget cannot drift from the plan.")
    render_budget_baseline(grain, yr)
    render_scenario_compare(grain, metric)


def render_budget_baseline(grain: str, yr: int):
    """Lock a published version as the year's official budget, and show how far
    the working plan has drifted from it (manager ask 2026-08-02)."""
    ptr = budget_baseline(yr)
    RO = not st.session_state.get("editable", False)

    with brand.section(
            "budget_lock", f"Budget baseline — {yr}",
            "The plan as it was committed to, held still, so the year can be "
            "measured against it."):
        if ptr:
            st.markdown(
                f"**Locked: v{ptr.get('version')} · {ptr.get('name', '')}** — "
                f"locked by {ptr.get('locked_by', '?')} "
                f"{_hm(ptr.get('locked_at', ''))}"
                + (f" · _{ptr['note']}_" if ptr.get("note") else ""))
            if ptr.get("history"):
                with st.expander(f"Re-baselined {len(ptr['history'])} time(s)"):
                    # Append-only: the ORIGINAL baseline is always the first
                    # row, so a re-lock can never quietly erase the number the
                    # year was actually committed to.
                    for h in ptr["history"]:
                        st.caption(f"was v{h.get('version')} · {h.get('name', '')} "
                                   f"— {h.get('locked_by', '?')} "
                                   f"{_hm(h.get('locked_at', ''))}"
                                   + (f" · {h['note']}" if h.get("note") else ""))
        else:
            st.info(f"No {yr} budget locked yet. Pick the published version that "
                    "was approved and lock it — nothing else changes, and the "
                    "version stays editable as the working plan.")

        # Only versions of THIS plan year: a 2027 snapshot cannot be 2026's
        # budget, and letting one be picked would produce a drift table whose
        # periods do not line up.
        vers = collab.changelog(SCENARIO_DIR, yr)
        if not vers:
            st.caption(f"Nothing published for {yr} yet — publish the approved "
                       "plan first, then lock it here.")
        elif RO:
            st.caption("Take control (sidebar) to lock or change the baseline.")
        else:
            opts = {f"v{j['version']} · {j.get('name', '')} · "
                    f"{_hm(j.get('published_at', ''))}": j for j in vers}
            c1, c2 = st.columns([2, 1])
            pick = c1.selectbox("Version to lock as the budget", list(opts),
                                key="bl_pick")
            note = c2.text_input("Note (optional)", key="bl_note",
                                 placeholder="approved by Finance")
            b1, b2 = st.columns(2)
            _label = "Re-baseline the budget" if ptr else "Lock as the budget"
            _sure = True
            if ptr:
                # Re-baselining resets what "drift" means, which is the whole
                # point of having a baseline — so it takes a deliberate tick,
                # never a single stray click.
                _sure = st.checkbox(
                    f"Yes — replace v{ptr.get('version')} as the {yr} baseline",
                    key="bl_confirm")
            if b1.button(_label, width="stretch", disabled=not _sure,
                         key="bl_lock"):
                j = opts[pick]
                collab.write_budget(SCENARIO_DIR, yr, {
                    "file": j["_file"], "version": j["version"],
                    "name": j.get("name", ""), "locked_by": st.session_state.user,
                    "locked_at": collab._iso(collab._now()), "note": note})
                st.success(f"v{j['version']} is now the {yr} budget baseline.")
                st.rerun()
            if ptr and b2.button("Clear the baseline", width="stretch",
                                 disabled=not _sure, key="bl_clear"):
                collab.clear_budget(SCENARIO_DIR, yr)
                st.success("Baseline cleared — the published version is untouched.")
                st.rerun()

    if not ptr:
        return
    got = budget_drift(grain)
    if got is None:
        st.warning("The locked budget snapshot could not be read.")
        return
    frame, meta = got
    if meta.get("misaligned"):
        st.warning(
            f"The {yr} budget was built on a "
            f"{meta['budget']['n_weeks']}-week horizon and the working plan is "
            f"{meta['working']['n_weeks']} — the periods do not line up, so a "
            "drift table would be misleading. Comparison refused.")
        return

    with brand.section("budget_drift", f"Plan vs budget — {yr} drift",
                       "Δ = working plan − budget. Positive Required FTE means "
                       "the year is asking for more people than were budgeted."):
        head = budget_drift_headline(grain)
        if head:
            _c = head["contacts"]
            _pct = ("" if np.isnan(head["contacts_pct"])
                    else f" ({head['contacts_pct']:+.1f}%)")
            brand.stat_row([
                {"label": "Contacts vs budget", "value": f"{_c:+,.0f}",
                 "sub": f"for the year{_pct}"},
                {"label": "Avg Required FTE", "value": f"{head['req']:+.1f}",
                 "sub": "more people needed" if head["req"] > 0
                        else "fewer people needed"},
                {"label": "Avg Staffed FTE", "value": f"{head['stf']:+.1f}",
                 "sub": "vs the budgeted supply"},
            ])
        show_lob = st.checkbox("Break out by line of business", key="bl_bylob")
        cols = ["Period"] + (["LOB"] if show_lob else [])
        if show_lob:
            grid = frame[cols + [f"{c} Δ" for c in BUDGET_DRIFT_COLS]]
        else:
            agg = {f"{c} Δ": "sum" for c in BUDGET_DRIFT_COLS}
            grid = frame.groupby("Period", sort=False).agg(agg).reindex(
                meta["order"]).reset_index()
        st.dataframe(grid.round(1), width="stretch", hide_index=True)
        st.download_button(
            f"Download the {yr} budget drift ({grain.lower()}) as CSV",
            frame.round(2).to_csv(index=False),
            file_name=f"budget_drift_{yr}_{grain.lower()}.csv", mime="text/csv")
        st.caption(
            f"Budget = v{meta['ptr'].get('version')}, recomputed through the same "
            "engine as the working plan — so a difference here is a real change "
            "in drivers or supply, never two systems disagreeing about arithmetic.")

    # The table above says the year moved; this one says WHY it moved.
    got_a = budget_assumption_drift()
    if got_a is None:
        return
    aframe, _ = got_a
    n_moved = int(aframe["_changed"].sum())
    with brand.section(
            "budget_assum", f"Assumptions vs budget — {yr}",
            "What we planned WITH, then and now: shrinkage, occupancy, AHT, "
            "attrition, ramp, and the weekly drivers. Locked with the version, "
            "so this is what was actually committed to."):
        if n_moved:
            st.markdown(f"**{n_moved} assumption(s) have moved since the budget "
                        "was locked.**")
        else:
            st.success("Every planning assumption still matches the budget.")
        only = st.checkbox("Show only what changed", value=True, key="bl_changed")
        show = aframe[aframe["_changed"]] if only else aframe
        if show.empty:
            st.caption("Nothing to show — tick off the filter to see every "
                       "assumption the budget locked in.")
        else:
            st.dataframe(show.drop(columns=["_changed"]), width="stretch",
                         hide_index=True)
        st.download_button(
            f"Download the {yr} assumption drift as CSV",
            aframe.drop(columns=["_changed"]).to_csv(index=False),
            file_name=f"budget_assumptions_{yr}.csv", mime="text/csv")
        st.caption(
            "**Weeks changed** counts how many of the horizon's weeks a weekly "
            "driver actually moved in — an average alone cannot tell a one-week "
            "correction from a re-plan of the whole year.")


def render_guide_page():
    st.header("Guide")
    st.caption("Task-by-task recipes, in the order they come up. The grids and "
               "sidebars also carry hover help (the small ? icons) for "
               "field-level detail.")
    if DEMO_TOUR_MD:
        with st.expander("3-minute demo tour — start here", expanded=True):
            st.markdown(DEMO_TOUR_MD)

    st.subheader("This week, right now")
    weekly_checklist()
    st.divider()

    st.subheader("Task recipes")
    for i, (title, md) in enumerate(visible_recipes()):
        with st.expander(title):
            st.markdown(md)
            target = RECIPE_PAGE.get(title)
            if target and target != st.session_state.get("nav_page"):
                _goto(target, key=f"goto_{i}")


def render_advisor_page(ro: bool):
    st.header("Hiring Advisor")
    page_help("Hiring Advisor")
    st.caption(
        "Answers two questions from the current working plan. **Specialty queues:** "
        "cover a shortfall (often an LOA) with an **interim pulled from the donor "
        "LOB** — shown with what the pull does to the donor, because interims "
        "aren't free. **Donor LOB:** when must classes start, and how big, to keep "
        "every week green — accounting for training + coaching lead time, stage "
        "attrition, and the ramp (a class is sized so even week-one grads cover "
        "the hole). Apply buttons only touch the working plan.")
    lobs = st.session_state.lobs
    names = list(lobs)
    donor = st.selectbox(
        "Donor / hiring LOB", names,
        index=names.index(_default_lob(names)) if names else 0,
        key="advisor_donor",
        help="Interims transfer out of here; new-hire classes are planned here.")

    # ---- interims for the specialty queues -----------------------------
    st.subheader("Interim coverage — specialty queues")
    any_short = False
    for name in names:
        if name == donor:
            continue
        for w, win in enumerate(shortfall_windows(lobs[name])):
            any_short = True
            tag = " · overlaps LOA" if win["loa_linked"] else ""
            donor_worst = donor_after_pull(lobs[donor], win["start_i"],
                                           win["end_i"], win["agents"])
            ok = donor_worst >= 0
            st.markdown(
                f"**{name}** — short **{win['depth']}** FTE, {win['start'][5:]} → "
                f"{win['end'][5:]}{tag}. Interim: **{win['agents']} agent(s)** from "
                f"{donor} for the window → {donor} worst-week Net becomes "
                f"**{donor_worst:+.1f}** "
                + ("(pull is affordable at the current plan)." if ok else
                   f"(**{donor} goes short** — pair the pull with the class plan below)."))
            if st.button(f"Apply interim: {win['agents']} → {name} "
                         f"({win['start'][5:]}–{win['end'][5:]})",
                         key=f"int_{name}_{w}", disabled=ro):
                apply_interim(name, donor, win["start_i"], win["end_i"], win["agents"])
                st.rerun()
    if not any_short:
        st.caption("No specialty shortfalls — nothing to cover.")

    # ---- class plan for the donor LOB -----------------------------------
    st.subheader(f"New-hire class plan — {donor}")
    st.caption("Recommendations reflect the plan as it stands — apply interims "
               "first and these update to cover the backfill too.")
    nh = lobs[donor]["nh"]
    def _tmpl_default(col, fallback):
        v = pd.to_numeric(nh[col], errors="coerce").dropna() if col in nh else []
        return float(v.iloc[-1]) if len(v) else fallback
    c1, c2, c3, c4 = st.columns(4)
    template = {
        "training_wks": c1.number_input("Training wks", 1, 20,
                                        int(_tmpl_default("Training Wks", 6))),
        "coaching_wks": c2.number_input("Coaching wks", 0, 20,
                                        int(_tmpl_default("Coaching Wks", 2))),
        "training_attr": c3.number_input("Training attr %", 0.0, 50.0,
                                         _tmpl_default("Training Attr %", 10.0), 1.0),
        "coaching_attr": c4.number_input("Coaching attr %", 0.0, 50.0,
                                         _tmpl_default("Coaching Attr %", 5.0), 1.0),
    }
    da = lobs[donor]["assumptions"]
    gap = int(da.get("class_gap_weeks", 4) or 0)
    min_sz = int(da.get("class_min_size", 1) or 1)
    max_sz = int(da.get("class_max_size", 12) or 12)
    one_att = bool(da.get("one_class_at_a_time", False))
    lead = int(template["training_wks"]) + int(template["coaching_wks"])
    eff_sp = max(gap, lead) if one_att else gap
    # Hoisted out of the f-string deliberately: a replacement field whose
    # expression spans lines is PEP 701, i.e. Python 3.12+. Work approves 3.11.4
    # only, and this is a PARSE error — the whole app failed to boot on 3.11 over
    # one line on a page that is currently hidden. Keep replacement fields to a
    # single line; `python3.11 -m py_compile` is the only reliable check.
    cadence = f"≥ {gap} weeks between class starts" if gap else "unconstrained"
    st.caption(f"Hiring cadence: **{cadence}**"
               + (f" · **one class at a time** (pipeline {lead} wks, so starts "
                  f"effectively ≥ {eff_sp} apart)" if one_att else "")
               + f" · **class size {min_sz}–{max_sz} seats**"
               + " — set per LOB in the sidebar (external-hire pipelines and short "
               "internal ramp-ups run on different calendars). Each class is sized "
               "to carry every red week until the next slot can land grads; "
               "sub-minimum needs fold into the previous class or wait for a "
               "justifiable cohort.")
    recs, uncoverable = recommend_classes(lobs[donor], template)
    lead_u = [u for u in uncoverable if u.get("why") == "lead"]
    cad_u = [u for u in uncoverable if u.get("why") == "cadence"]
    min_u = [u for u in uncoverable if u.get("why") == "min"]
    if lead_u:
        st.warning("Shortfalls **inside the pipeline lead time** — no class can "
                   "reach them; cover with interims/OT: "
                   + ", ".join(f"{u['week'][5:]} ({u['short']} FTE)"
                               for u in lead_u))
    if cad_u:
        st.warning(f"Shortfalls **between cadence slots** — at ≥ {eff_sp} weeks "
                   "between class starts"
                   + (" (one class at a time)" if one_att else "")
                   + ", no class can land grads in time; cover with interims/OT "
                   "(or shorten the cadence in the sidebar): "
                   + ", ".join(f"{u['week'][5:]} ({u['short']} FTE)"
                               for u in cad_u))
    if min_u:
        st.warning(f"Shortfalls **below the minimum class size** ({min_sz} seats) "
                   "— not enough need for a cohort yet (slivers fold into an "
                   "earlier class when the max allows); cover with interims/OT, "
                   "or lower the minimum in the sidebar: "
                   + ", ".join(f"{u['week'][5:]} ({u['short']} FTE)"
                               for u in min_u))
    if recs:
        st.dataframe(pd.DataFrame([{
            "Start": r["Class Start Week"], "Size": r["Class Size"],
            "Grads land": r.get("lands", r["covers"]),
            "First red covered": r["covers"][5:],
            "Carries through": r.get("carries_to", r["covers"])[5:],
        } for r in recs]), hide_index=True, width="stretch")
        total = sum(r["Class Size"] for r in recs)
        st.markdown(f"**{len(recs)} class(es), {total:.0f} seats** "
                    f"(lead time {lead} wks + ramp priced in).")
        if st.button(f"Apply class plan to {donor}", disabled=ro, key="apply_classes"):
            st.session_state.lobs[donor]["nh"] = pd.concat(
                [nh, pd.DataFrame([{k: v for k, v in r.items()
                                    if k not in ("covers", "lands", "carries_to",
                                                 "_nh_i")}
                                   for r in recs])], ignore_index=True)
            st.success(f"{len(recs)} class(es) added to {donor} — review on the "
                       "Capacity Plan page, then publish.")
            st.rerun()
    else:
        st.caption(f"{donor} stays green all horizon — no classes needed.")


CHANGELOG_MAX_VERSIONS = 25   # newest N; anything dropped is NAMED, never silent


# Both caches key on FILENAME ALONE, with no mtime and no invalidation, and
# that is correct rather than sloppy: a published `vNNNN` snapshot is IMMUTABLE
# by design — restoring one republishes it under a NEW name rather than
# rewriting it. So the diff between two versions is a pure function of two
# filenames and can be cached for the life of the process.
#
# Measured before adding these (25 versions, 6 LOBs, 52 weeks): 1.9 s to build
# the page, on EVERY rerun — every filter click, every widget touch — because
# 24 pairs x 2 sides x 6 LOBs x 3 frames is 864 read_json calls. The row count
# was irrelevant (449 rows); parsing is the whole cost. On the network share
# the file reads dominate instead, which is why the raw JSON is cached
# separately from the diff: consecutive pairs share a file, so each snapshot
# would otherwise be READ twice per build.
@st.cache_data(show_spinner=False, max_entries=64)
def _cached_snapshot(scen_dir: str, fname: str):
    return collab.load_snapshot(scen_dir, fname)


@st.cache_data(show_spinner=False, max_entries=256)
def _cached_version_diff(scen_dir: str, older_file: str, newer_file: str) -> list[dict]:
    a = _cached_snapshot(scen_dir, older_file)
    b = _cached_snapshot(scen_dir, newer_file)
    if a is None or b is None:
        return []
    return diff_payloads(a, b)


def _changelog_rows(log: list[dict], limit: int) -> tuple[pd.DataFrame, int]:
    """Full history: every consecutive pair of published versions, newest first.

    Returns the flat audit frame plus the number of versions actually walked,
    so the caller can say what it covered rather than implying "everything"."""
    walk = log[:limit]                       # changelog() is already newest-first
    rows = []
    for newer, older in zip(walk, walk[1:]):
        try:
            diffs = _cached_version_diff(str(SCENARIO_DIR), older["_file"],
                                         newer["_file"])
        except Exception as e:               # one unreadable old snapshot must
            diffs = [{"LOB": "(plan)", "Area": "Plan",   # never blank the page
                      "Field": "could not compare", "Week": "",
                      "Was": f"v{older['version']}", "Now": str(e)[:80]}]
        head = {"Version": f"v{newer['version']}",
                "When": _hm(newer.get("published_at", "")),
                "Who": newer.get("author", ""),
                "Why": newer.get("note", "") or "(no note)"}
        if not diffs:
            rows.append({**head, "LOB": "", "Area": "", "Field": "(no field changes)",
                         "Week": "", "Was": "", "Now": ""})
        for d in diffs:
            rows.append({**head, **d})
    cols = ["Version", "When", "Who", "Why"] + DIFF_COLS
    return pd.DataFrame(rows, columns=cols), len(walk)


def render_changelog_page():
    """Who changed what, when, and why — computed from the published versions.

    Nothing here is collected from planners: the snapshots already contain the
    complete plan, and the single-writer lock already attributes every edit
    between two versions to whoever published the later one."""
    _yr = _plan_year()
    log = collab.changelog(SCENARIO_DIR, _yr)

    with brand.section("cl_head", f"Change log — {_yr}",
                       "Every published version, and exactly which fields moved "
                       "between them. Computed from the snapshots, so it is "
                       "complete for versions published before this page existed."):
        if not log:
            st.info(f"No {_yr} versions published yet — the log starts at the "
                    "first publish.")
            return
        if len(log) < 2:
            st.info(f"Only one {_yr} version so far (v{log[0]['version']} by "
                    f"{log[0].get('author', '')}). A comparison needs two — the "
                    "log fills in from the next publish.")
            return
        st.caption("**Who** and **when** are stamped on each version. **What** "
                   "is a field-by-field diff of consecutive versions. **Why** is "
                   "the note the planner wrote at publish — the one part no "
                   "system can work out for itself.")

    # First visit of a session pays the parse (~2 s for 25 versions x 6 LOBs);
    # after that the pair cache makes it instant, and a NEW publish invalidates
    # exactly ONE pair — the other 24 stay warm.
    with st.spinner("Building the change log…"):
        frame, walked = _changelog_rows(log, CHANGELOG_MAX_VERSIONS)
    if len(log) > walked:
        st.caption(f"Showing the newest {walked} of {len(log)} published "
                   f"{_yr} versions. Older ones are still on disk and still "
                   "loadable from Version history.")

    with brand.section("cl_filter", "Filter"):
        c1, c2 = st.columns(2)
        lobs = sorted(x for x in frame["LOB"].unique() if x)
        areas = sorted(x for x in frame["Area"].unique() if x)
        pick_lob = c1.multiselect("Line of business", lobs, key="cl_lob")
        pick_area = c2.multiselect("Area", areas, key="cl_area")
    shown = frame
    if pick_lob:
        shown = shown[shown["LOB"].isin(pick_lob)]
    if pick_area:
        shown = shown[shown["Area"].isin(pick_area)]

    with brand.section("cl_table", "Every change",
                       f"{len(shown):,} row(s) across {walked} version(s)."):
        st.dataframe(shown, width="stretch", hide_index=True)
        st.download_button(
            "Download as CSV", shown.to_csv(index=False).encode("utf-8"),
            file_name=f"change-log-{_yr}.csv", mime="text/csv",
            help="The whole table as filtered, for anyone who needs it outside "
                 "the app.")

    with brand.section("cl_compare", "Compare any two versions",
                       "For a specific question — what moved between the "
                       "version we budgeted from and today's."):
        opts = {f"v{j['version']} · {j.get('name', '')} · {j.get('author', '')}": j
                for j in log}
        keys = list(opts)
        c1, c2 = st.columns(2)
        a = c1.selectbox("From", keys, index=min(1, len(keys) - 1), key="cl_from")
        b = c2.selectbox("To", keys, index=0, key="cl_to")
        ja, jb = opts[a], opts[b]
        if ja["version"] == jb["version"]:
            st.caption("Pick two different versions.")
        else:
            lo, hi = sorted((ja, jb), key=lambda j: j["version"])
            # Through the same cache — an arbitrary pair is as immutable as a
            # consecutive one, and picking the same two twice is the norm here.
            pair = _cached_version_diff(str(SCENARIO_DIR), lo["_file"], hi["_file"])
            if not pair and _cached_snapshot(str(SCENARIO_DIR), lo["_file"]) is None:
                st.warning("One of those snapshots could not be read.")
            else:
                d = pd.DataFrame(pair, columns=DIFF_COLS)
                st.caption(f"v{lo['version']} → v{hi['version']} · "
                           f"{len(d):,} field change(s)")
                if d.empty:
                    st.caption("No field-level differences between those two.")
                else:
                    st.dataframe(d, width="stretch", hide_index=True)


# Push the org-wide member spread into every LOB (sidebar edits, loaded
# scenarios, or horizon changes) before anything computes.
apply_global_members()

if page == "Executive View":
    render_executive_view()
elif page == "Hiring Advisor":
    render_advisor_page(RO)
elif page == "Budget":
    render_budget_page()
elif page == "Guide":
    render_guide_page()
elif page == "Real Data":
    render_real_data_page()
elif page == "ACD Shrinkage":
    render_shrinkage_page()
elif page == "Change log":
    render_changelog_page()
elif view == CONSOLIDATED:
    st.header("Consolidated — all LOBs")
    plan = consolidated_plan(st.session_state.lobs)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Avg Required FTE", f"{plan['Required FTE'].mean():.1f}")
    c2.metric("Avg Staffed FTE", f"{plan['Staffed FTE'].mean():.1f}")
    c3.metric("Worst-Week Net FTE", f"{plan['Net FTE'].min():+.1f}")
    c4.metric("Weeks Understaffed", int((plan["Net FTE"] < 0).sum()))
    plan_chart_with_benchmarks(plan, None)

    st.subheader("Net FTE by LOB")
    by_lob = pd.DataFrame({"Week": plan["Week"]})
    for lob, d in st.session_state.lobs.items():
        by_lob[lob] = compute_plan(d)["Net FTE"].to_numpy()
    st.line_chart(by_lob.set_index("Week"))
    render_plan_grid(plan, "Consolidated totals. Select an LOB in the sidebar to edit its inputs.")
else:
    lob = st.session_state.lobs[view]
    page_help("Capacity Plan")
    if RO:
        # The grids below are LOCKED while read-only. They used to accept typing
        # that Take control then destroyed: acquiring the lock reloads the
        # published snapshot over the session, so a class entered while viewing
        # vanished the moment you took control (field report 2026-07-31 — the
        # symptom read as "rollover ate my new hires", but no rollover was
        # needed). The sidebar has gated every input on `RO` since day one; the
        # three grids were the hole in that rule.
        st.info("**Read-only** — take control (sidebar) to edit this plan, or "
                "open a Sandbox to try changes privately. The grids below are "
                "locked so nothing you type can be lost.")
    tab_plan, tab_inputs, tab_nh = st.tabs(["Plan", "Weekly Inputs", "New-Hire Classes"])

    with tab_inputs:
        with brand.section("demand", "Demand — weekly drivers"):
            st.caption(
                f"Editing **{view}**. Edit cells directly — the plan recalculates live. "
                "**Members** is the shared org-wide base (set in the sidebar, same for every "
                "LOB). Expected weekly calls = **Members × CPM ÷ 52** (CPM = annual "
                "**Calls Per Member**), so this LOB's **CPM** sizes its demand. "
                "Editing a week's **CPM carries "
                "forward** to later weeks until you edit a later week. "
                "**Seasonality** is a weekly index (1.0 = average, 1.15 = +15%) that "
                "reshapes the year but keeps the annual total fixed — peaks pull volume "
                "from the quiet weeks. "
                "**Fcst Override** replaces the model forecast when filled; blank = model."
            )
            prev_demand = lob["demand"]
            edited_demand = stable_editor(
                prev_demand, state_key=f"demand_{view}", width="stretch", num_rows="fixed",
                disabled=True if RO else ["Week", "Members"], hide_index=True,
                column_config={
                    "Seasonality": st.column_config.NumberColumn(
                        format="%.2f", min_value=0.0,
                        help="1.0 = average week. Reshapes the year; annual total is preserved."),
                    "Members": st.column_config.NumberColumn(
                        format="localized",
                        help="Org-wide FORECAST membership (sidebar start → year-end). "
                             "Read-only here — the same series for every LOB."),
                    "Members (actual)": st.column_config.NumberColumn(
                        format="localized", min_value=0.0,
                        help="Measured membership that week. Blank = use the forecast. "
                             "Org-wide: enter it once and every LOB sees it. Filled "
                             "weeks drive the model AND the measured-CPM readout."),
                    "Fcst Override": st.column_config.NumberColumn(format="localized"),
                    "AHT (sec)": st.column_config.NumberColumn(format="localized"),
                })
            cpm_filled = forward_fill_step(prev_demand, edited_demand, "CPM")
            members_changed = capture_members_actual(edited_demand)
            lob["demand"] = edited_demand
            if cpm_filled or members_changed:
                st.rerun()  # redraw: carried-forward CPM, and actuals mirrored org-wide

            # Measured CPM — the entered CPM checked against reality.
            _mc = measured_cpm(view)
            if _mc is not None:
                _cpm_act, _cpm_wks = _mc
                _cpm_now = float(pd.to_numeric(edited_demand["CPM"],
                                               errors="coerce").iloc[-1] or 0)
                _enough = _cpm_wks >= CPM_MIN_WEEKS
                st.caption(
                    f"**Measured CPM** (actual contacts × 52 ÷ actual members, "
                    f"{_cpm_wks} week(s)): **{_cpm_act:.2f}** vs entered "
                    f"**{_cpm_now:.2f}** ({_cpm_act - _cpm_now:+.2f})"
                    + ("" if _enough else
                       f" — ⚠️ fewer than {CPM_MIN_WEEKS} weeks; treat as directional"))
                if st.button("Adopt measured CPM (all weeks)", disabled=RO or not _enough,
                             key=f"adopt_cpm_{view}",
                             help=f"Set every week's CPM to the measured rate. Enabled at "
                                  f"{CPM_MIN_WEEKS}+ weeks with both actual members and "
                                  f"actual contacts."):
                    lob["demand"]["CPM"] = round(float(_cpm_act), 2)
                    st.rerun()
            elif st.session_state.get("acd_weekly") is None:
                st.caption("Measured CPM needs actual contacts — load the ACD feed on "
                           "the Real Data page (and enter **Members (actual)**).")
            else:
                st.caption("Measured CPM needs **Members (actual)** weeks that overlap "
                           "the loaded actual contacts.")

            # Live readout so the planner sees the reshape is total-preserving.
            _seas = pd.to_numeric(edited_demand["Seasonality"], errors="coerce").fillna(1.0)
            if float(_seas.min()) != 1.0 or float(_seas.max()) != 1.0:
                _p = compute_plan(lob)["Model Forecast"]
                st.caption(
                    f"Seasonality active — peak week **{int(_p.max()):,}**, "
                    f"trough **{int(_p.min()):,}**, annual model total "
                    f"**{int(_p.sum()):,}** (unchanged by the index; only its shape moves).")

            with st.expander("Week-of-month seasonality profile"):
                st.caption(
                    "First week of the month runs hot? Set a relative uplift per "
                    "week-of-month position — this **generates the Seasonality column** "
                    "for you. Annual total is preserved (the index is normalized), so "
                    "these are relative lifts. Hand-tweak individual holiday weeks in the "
                    "grid afterward — regenerating overwrites the whole column.")
                wcols = st.columns(5)
                uplift = {}
                for i, c in enumerate(wcols, start=1):
                    uplift[i] = c.number_input(
                        f"WoM {i} %", -50.0, 100.0, 0.0, 1.0,
                        key=f"wom_{view}_{i}", disabled=RO)
                all_lobs = st.checkbox("Apply to all LOBs", value=False,
                                       key=f"wom_all_{view}", disabled=RO)
                if st.button("Generate Seasonality column", disabled=RO,
                             key=f"wom_apply_{view}"):
                    targets = st.session_state.lobs.values() if all_lobs else [lob]
                    for d in targets:
                        d["demand"]["Seasonality"] = wom_seasonality(
                            d["demand"]["Week"].tolist(), uplift)
                    st.success("Seasonality column generated"
                               + (" for all LOBs." if all_lobs else f" for {view}."))
                    st.rerun()

                st.divider()
                st.caption(
                    "**Or derive it from history:** each week's ACD actual volume ÷ "
                    "the average week = its index, averaged by week-of-year across the "
                    "loaded history. Self-maintaining once you export full-year actuals — "
                    "partial weeks are ignored.")
                if st.button("Derive from ACD actuals", disabled=RO,
                             key=f"seas_hist_{view}"):
                    idx, msg = derive_seasonality_from_history(
                        st.session_state.get("acd_weekly"), view,
                        lob["demand"]["Week"].tolist())
                    if idx is None:
                        st.warning(msg)
                    else:
                        lob["demand"]["Seasonality"] = idx
                        st.success(msg)
                        st.rerun()

        with brand.section("roster", "Roster — LOA, mentors, transfers, attrition, support"):
            st.caption(
                "Roster adjustments (weekly): LOA headcount, **Mentors** — agents "
                "Learning & Development is taking off the phones to coach new-hire "
                "classes (enter the number they tell you; they stay in Production HC "
                "but deliver no capacity, exactly like LOA) — net transfers in/out, "
                "**Attrition (actual)** — people who actually left that week (blank = "
                "use the modelled rate; a filled cell **replaces** it, so past weeks "
                "become truth and the walk self-corrects) — and support staff. "
                "**LOA, Mentors, Supervisors and Leads/Project carry forward** "
                "from the week you edit until you edit a later week (a sup added in Q3 "
                "is entered once — and set Mentors back to 0 the week they return to "
                "the phones). **Transfers** are one-time events and do not carry "
                "forward. Support staff are informational — the plan computes their "
                "ratio rows against walking agent headcount; they never affect Net FTE.")
            prev_roster = lob["roster"]
            edited_roster = stable_editor(
                prev_roster, state_key=f"roster_{view}", width="stretch", num_rows="fixed",
                disabled=True if RO else ["Week"], hide_index=True)
            filled = [forward_fill_step(prev_roster, edited_roster, c)
                      for c in ("LOA", "Mentors", "Supervisors", "Leads/Project")]
            lob["roster"] = edited_roster
            if any(filled):
                st.rerun()  # redraw so the carried-forward cells show

    with tab_nh:
        with brand.section("nh", "New-hire classes"):
            st.caption(
                "Classes walk Training → Coaching Lab → Production with stage attrition. "
                "**Actual Grads** records what really came out of a class — a filled cell "
                "**replaces** the modelled survivor calculation (started 10, graduated 7 → "
                "7 land in production, whatever the attrition % predicted). Leave blank for "
                "classes that haven't graduated yet. To add a class, type into the empty "
                "bottom row; to remove one, select the row (left edge) and press "
                "Delete — or the trash icon.")
            lob["nh"] = stable_editor(
                lob["nh"], state_key=f"nh_{view}", width="stretch",
                # num_rows too, not just `disabled`: a dynamic editor keeps its
                # add-row affordance when disabled, so a viewer could still type
                # a class into a grid that cannot keep it.
                num_rows="fixed" if RO else "dynamic",
                disabled=RO,
                hide_index=True,
                column_config={
                    "Class Start Week": st.column_config.SelectboxColumn(
                        options=lob["demand"]["Week"].tolist()),
                    "Class Size": st.column_config.NumberColumn(
                        min_value=0.0, format="%.0f",
                        help="How many people start the class. A row with no size is "
                             "ignored until you fill it in."),
                    "Training Wks": st.column_config.NumberColumn(
                        min_value=0.0, format="%.0f", help="Weeks in training."),
                    "Coaching Wks": st.column_config.NumberColumn(
                        min_value=0.0, format="%.0f", help="Weeks in the coaching lab."),
                    "Training Attr %": st.column_config.NumberColumn(
                        min_value=0.0, max_value=100.0, format="%.1f",
                        help="Share of the class lost during training."),
                    "Coaching Attr %": st.column_config.NumberColumn(
                        min_value=0.0, max_value=100.0, format="%.1f",
                        help="Share of the survivors lost during coaching."),
                    "Actual Grads": st.column_config.NumberColumn(
                        min_value=0.0, format="%.1f",
                        help="People who actually reached production from this class. "
                             "Blank = use the Training/Coaching attrition %."),
                })
            _wash = measured_nh_washout(lob["nh"])
            if _wash is not None:
                _act, _plan, _cls, _size = _wash
                st.caption(
                    f"Measured washout across **{_cls}** graduated class(es) "
                    f"({_size:.0f} hired): **{_act:.1f}%** vs **{_plan:.1f}%** planned "
                    f"({_act - _plan:+.1f} pts). Adjust Training/Coaching Attr % on future "
                    "classes if this keeps diverging — the app can't tell which stage lost "
                    "them, only the total.")

    plan = compute_plan(lob)
    with tab_plan:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Avg Required FTE", f"{plan['Required FTE'].mean():.1f}")
        c2.metric("Avg Staffed FTE", f"{plan['Staffed FTE'].mean():.1f}")
        c3.metric("Worst-Week Net FTE", f"{plan['Net FTE'].min():+.1f}")
        c4.metric("Weeks Understaffed", int((plan["Net FTE"] < 0).sum()))
        plan_chart_with_benchmarks(plan, view)
        with brand.section("plan", "Plan — weeks across, metrics down"):
            render_plan_grid(
                plan_with_demand_benchmarks(plan, view),
                "Net FTE shaded red where understaffed. Actual Offered "
                "rows (+ variance vs plan) appear when the loaded ACD weeks overlap the "
                "plan horizon. **CPM (actual)** is derived, never entered: actual contacts "
                "× 52 ÷ that week's membership (the actual figure where you have entered "
                "one, the forecast spread otherwise) — compare it against **CPM (plan)** "
                "directly above. It only reads true against **full-week** exports: a week "
                "holding a single day of contacts divides a day's calls into a year, so it "
                "lands near a sixth of the real figure. "
                "In production this table is written to the shared database "
                "by the scheduled engine.")

        with brand.section("recon", "Actuals & variance"):
            recon = build_reconciliation(plan, lob, view)
            if recon is not None:
                render_reconciliation(recon)
                st.caption(
                    "Plan vs. measured actuals, for weeks that overlap loaded ACD "
                    "data. **Variance = Actual − Plan** (red = actual below plan). Shrink % "
                    "compares plan *total* shrinkage to measured *in-office* only — layer OOO "
                    "on before treating them as like-for-like.")
                _partial_note(view)
            else:
                st.caption(
                    "Load the ACD export on the Real Data page to reconcile plan vs. actuals "
                    "here — one Plan / Actual / Variance row per metric.")

_autosave_draft()
