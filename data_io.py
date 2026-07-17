"""
Real-data import/export for the Capacity Planner.

Source-agnostic: whatever ACD / forecast / WFM system you export from, you shape
the data to these templates and load it. Nothing here depends on a specific
vendor — the ACD live parser lives separately in capacity_planner.py.

Two flows:
  1. Whole-LOB workbook  — download build_template_workbook(), fill the Demand /
     Roster / New-Hire / Assumptions sheets, upload it back with read_workbook().
  2. Single-table file   — replace just one table (demand or roster) from a CSV
     or one-sheet Excel via read_table().

Every reader returns (data, ImportReport). The report separates hard errors
(import blocked) from warnings (imported, but look here), so the UI can show the
planner exactly what happened instead of a stack trace.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
import pandas as pd


# ----------------------------------------------------------------------
# Canonical schema for each input table.
#
# Each column carries the aliases we'll accept from a real-world export
# (case-insensitive, punctuation-insensitive) so planners don't have to
# rename headers by hand. `required` columns block the import if missing;
# optional columns are filled with `default` when absent.
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Col:
    name: str                       # canonical column name used by the engine
    kind: str                       # "week" | "float" | "int"
    required: bool = True
    default: object = None
    aliases: tuple = ()             # extra headers that map to this column
    min: float | None = None
    max: float | None = None


DEMAND_COLS = [
    Col("Week", "week", aliases=("week starting", "week start", "date", "wk")),
    Col("Members", "float", aliases=("membership", "member count", "accounts"),
        min=0),
    Col("CPM", "float",
        aliases=("cpm", "calls per member", "call per member", "cpm annual",
                 "contacts per member", "contact rate"),
        min=0),
    Col("Seasonality", "float", required=False, default=1.0,
        aliases=("seasonality", "seasonal index", "season index", "index",
                 "seasonality index", "profile"), min=0),
    Col("Fcst Override", "float", required=False, default=np.nan,
        aliases=("forecast override", "override", "manual forecast",
                 "planner forecast")),
    Col("AHT (sec)", "float",
        aliases=("aht", "aht sec", "aht seconds", "avg handle time",
                 "average handle time", "handle time"), min=0),
]

ROSTER_COLS = [
    Col("Week", "week", aliases=("week starting", "week start", "date", "wk")),
    Col("LOA", "float", required=False, default=0.0,
        aliases=("leave of absence", "loa hc", "on leave"), min=0),
    Col("Transfers +/-", "float", required=False, default=0.0,
        aliases=("transfers", "transfer", "net transfers", "xfers", "moves")),
]

NH_COLS = [
    Col("Class Start Week", "week",
        aliases=("start week", "class start", "start date", "hire week")),
    Col("Class Size", "float",
        aliases=("size", "heads", "seats", "class hc", "hires"), min=0),
    Col("Training Wks", "int", required=False, default=4,
        aliases=("training weeks", "train wks", "training"), min=0),
    Col("Coaching Wks", "int", required=False, default=2,
        aliases=("coaching weeks", "coach wks", "nesting", "nesting wks"),
        min=0),
    Col("Training Attr %", "float", required=False, default=0.0,
        aliases=("training attrition", "train attr", "training attrition %"),
        min=0, max=100),
    Col("Coaching Attr %", "float", required=False, default=0.0,
        aliases=("coaching attrition", "coach attr", "coaching attrition %",
                 "nesting attr"), min=0, max=100),
]

# Assumptions are simple key/value; these are the keys the engine reads.
ASSUMPTION_KEYS = {
    "starting_hc": ("Starting production HC", 100.0),
    "annual_attrition_pct": ("Annual attrition %", 28.0),
    "shrinkage_pct": ("Shrinkage %", 32.0),
    "occupancy_pct": ("Target occupancy %", 85.0),
    "paid_hours_per_week": ("Paid hrs / FTE / week", 40.0),
    "workload_margin_pct": ("Workload margin %", 5.0),
}

SCHEMAS = {"demand": DEMAND_COLS, "roster": ROSTER_COLS, "nh": NH_COLS}
_SHEET = {"demand": "Demand", "roster": "Roster", "nh": "NewHire"}


# ----------------------------------------------------------------------
# Report object — carries structured feedback back to the UI.
# ----------------------------------------------------------------------
@dataclass
class ImportReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def merge(self, other: "ImportReport", prefix: str = ""):
        p = f"{prefix}: " if prefix else ""
        self.errors += [p + e for e in other.errors]
        self.warnings += [p + w for w in other.warnings]
        self.notes += [p + n for n in other.notes]


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _norm(s: str) -> str:
    """Normalize a header for fuzzy matching: lowercase, strip punctuation."""
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def _coerce_week(series: pd.Series) -> pd.Series:
    """Parse a Week column to ISO 'YYYY-MM-DD' date strings."""
    def one(v):
        if pd.isna(v) or (isinstance(v, str) and not v.strip()):
            return None
        if isinstance(v, (datetime, date)):
            return (v.date() if isinstance(v, datetime) else v).isoformat()
        ts = pd.to_datetime(v, errors="coerce")
        return None if pd.isna(ts) else ts.date().isoformat()
    return series.map(one)


def _match_columns(df: pd.DataFrame, cols: list[Col]) -> dict:
    """Map incoming headers -> canonical names via exact then alias match."""
    lookup = {}
    for c in cols:
        lookup[_norm(c.name)] = c.name
        for a in c.aliases:
            lookup[_norm(a)] = c.name
    mapping = {}
    for raw in df.columns:
        canon = lookup.get(_norm(raw))
        if canon and canon not in mapping.values():
            mapping[raw] = canon
    return mapping


# ----------------------------------------------------------------------
# Table reader — one table (demand / roster / nh) from a DataFrame.
# ----------------------------------------------------------------------
def coerce_table(df: pd.DataFrame, table_key: str) -> tuple[pd.DataFrame, ImportReport]:
    cols = SCHEMAS[table_key]
    rep = ImportReport()
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]

    mapping = _match_columns(df, cols)
    df = df.rename(columns=mapping)
    matched = set(mapping.values())

    unmapped = [c for c in df.columns if c not in matched]
    if unmapped:
        rep.notes.append(f"Ignored unrecognized column(s): {', '.join(map(str, unmapped))}")

    # Required columns must be present.
    for c in cols:
        if c.required and c.name not in df.columns:
            rep.errors.append(f"Missing required column '{c.name}'")
    if rep.errors:
        return pd.DataFrame(), rep

    out = pd.DataFrame()
    for c in cols:
        if c.name not in df.columns:
            out[c.name] = c.default
            rep.notes.append(f"Column '{c.name}' not provided — defaulted.")
            continue
        s = df[c.name]
        if c.kind == "week":
            out[c.name] = _coerce_week(s)
        else:
            num = pd.to_numeric(s, errors="coerce")
            bad = int(num.isna().sum() - s.isna().sum())
            if bad > 0:
                rep.warnings.append(f"'{c.name}': {bad} non-numeric value(s) set blank.")
            if c.kind == "int":
                out[c.name] = num.round().astype("Float64")
            else:
                out[c.name] = num
            if c.min is not None and (num < c.min).any():
                rep.warnings.append(f"'{c.name}': value(s) below {c.min}.")
            if c.max is not None and (num > c.max).any():
                rep.warnings.append(f"'{c.name}': value(s) above {c.max}.")

    # Drop fully blank rows.
    key = "Class Start Week" if table_key == "nh" else "Week"
    out = out[out[key].notna()].reset_index(drop=True)

    if table_key in ("demand", "roster"):
        if out.empty:
            rep.errors.append("No rows with a valid Week found.")
        dups = out["Week"][out["Week"].duplicated()].tolist()
        if dups:
            rep.warnings.append(f"Duplicate week(s): {', '.join(dups[:5])} — kept first.")
            out = out.drop_duplicates(subset="Week").reset_index(drop=True)
        out = out.sort_values("Week").reset_index(drop=True)
        # Fill numeric defaults for optional columns left blank.
        for c in cols:
            if c.kind != "week" and c.default is not None and not (
                    isinstance(c.default, float) and np.isnan(c.default)):
                out[c.name] = out[c.name].fillna(c.default)

    # Restore native dtypes the engine expects (plain float, not Float64).
    for c in cols:
        if c.kind != "week":
            out[c.name] = pd.to_numeric(out[c.name], errors="coerce").astype(float)
    return out, rep


def read_table(file, table_key: str) -> tuple[pd.DataFrame, ImportReport]:
    """Read a single-table CSV or one-sheet Excel file."""
    rep = ImportReport()
    try:
        raw = _read_any(file)
    except Exception as exc:  # noqa: BLE001 — surface parse errors to the UI
        rep.errors.append(f"Could not read file: {exc}")
        return pd.DataFrame(), rep
    df, r = coerce_table(raw, table_key)
    rep.merge(r)
    return df, rep


def _read_any(file, sheet=0) -> pd.DataFrame:
    """Read CSV or Excel from an uploaded file / path into a DataFrame."""
    name = getattr(file, "name", str(file)).lower()
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        return pd.read_excel(file, sheet_name=sheet)
    return pd.read_csv(file, encoding="utf-8-sig")


# ----------------------------------------------------------------------
# Whole-LOB workbook reader.
# ----------------------------------------------------------------------
def read_workbook(file) -> tuple[dict | None, ImportReport]:
    """Read a filled template workbook into a LOB dict.

    Returns ({demand, roster, nh, assumptions}, report) or (None, report) if
    the required sheets can't be read.
    """
    rep = ImportReport()
    try:
        xls = pd.ExcelFile(file)
    except Exception as exc:  # noqa: BLE001
        rep.errors.append(f"Could not open workbook: {exc}")
        return None, rep

    sheets = {_norm(s): s for s in xls.sheet_names}
    lob = {}

    for key in ("demand", "roster", "nh"):
        want = _norm(_SHEET[key])
        if want not in sheets:
            if key == "nh":
                lob["nh"] = _empty_nh()
                rep.notes.append("No NewHire sheet — starting with no classes.")
                continue
            rep.errors.append(f"Missing '{_SHEET[key]}' sheet.")
            continue
        raw = pd.read_excel(xls, sheet_name=sheets[want])
        df, r = coerce_table(raw, key)
        rep.merge(r, prefix=_SHEET[key])
        lob[key] = df

    if rep.errors:
        return None, rep

    # Align roster to demand weeks (engine indexes them positionally).
    lob["roster"], r = align_roster(lob["demand"], lob["roster"])
    rep.merge(r, prefix="Roster")

    # Assumptions sheet is optional.
    lob["assumptions"], r = _read_assumptions(xls, sheets)
    rep.merge(r, prefix="Assumptions")

    rep.notes.append(
        f"Loaded {len(lob['demand'])} weeks, {len(lob['nh'])} new-hire class(es).")
    return lob, rep


def _align_roster(demand: pd.DataFrame, roster: pd.DataFrame) -> tuple[pd.DataFrame, ImportReport]:
    """Reindex roster to exactly the demand weeks, in order."""
    rep = ImportReport()
    weeks = demand["Week"].tolist()
    r = roster.set_index("Week")
    missing = [w for w in weeks if w not in r.index]
    extra = [w for w in r.index if w not in weeks]
    if missing:
        rep.warnings.append(f"{len(missing)} week(s) missing from Roster — defaulted to 0.")
    if extra:
        rep.warnings.append(f"{len(extra)} Roster week(s) not in Demand — dropped.")
    out = pd.DataFrame({"Week": weeks})
    out["LOA"] = [float(r.loc[w, "LOA"]) if w in r.index else 0.0 for w in weeks]
    out["Transfers +/-"] = [
        float(r.loc[w, "Transfers +/-"]) if w in r.index else 0.0 for w in weeks]
    return out, rep


def _read_assumptions(xls, sheets) -> tuple[dict, ImportReport]:
    rep = ImportReport()
    defaults = {k: v[1] for k, v in ASSUMPTION_KEYS.items()}
    if _norm("Assumptions") not in sheets:
        rep.notes.append("No Assumptions sheet — used defaults.")
        return defaults, rep
    df = pd.read_excel(xls, sheet_name=sheets[_norm("Assumptions")], header=None)
    # Accept either key or human label in the first column, value in the second.
    label_to_key = {_norm(lbl): k for k, (lbl, _) in ASSUMPTION_KEYS.items()}
    label_to_key.update({_norm(k): k for k in ASSUMPTION_KEYS})
    found = 0
    for _, row in df.iterrows():
        if len(row) < 2:
            continue
        k = label_to_key.get(_norm(row.iloc[0]))
        val = pd.to_numeric(row.iloc[1], errors="coerce")
        if k and not pd.isna(val):
            defaults[k] = float(val)
            found += 1
    if not found:
        rep.warnings.append("Assumptions sheet had no recognizable rows — used defaults.")
    return defaults, rep


def _empty_nh() -> pd.DataFrame:
    return pd.DataFrame({c.name: pd.Series(dtype="object" if c.kind == "week" else "float")
                         for c in NH_COLS})


# ----------------------------------------------------------------------
# Template builders — hand these to the planner to fill in.
# ----------------------------------------------------------------------
def build_template_workbook(weeks: list[str], lob_name: str = "New LOB") -> bytes:
    """Build a fill-in Excel workbook with weeks pre-populated and a guide."""
    demand = pd.DataFrame({c.name: (weeks if c.kind == "week" else [None] * len(weeks))
                           for c in DEMAND_COLS})
    roster = pd.DataFrame({c.name: (weeks if c.kind == "week" else [0] * len(weeks))
                           for c in ROSTER_COLS})
    nh_example = {
        "Class Start Week": weeks[2] if len(weeks) > 2 else (weeks[0] if weeks else None),
        "Class Size": 12, "Training Wks": 4, "Coaching Wks": 2,
        "Training Attr %": 8, "Coaching Attr %": 4,
    }
    nh = pd.DataFrame([nh_example])
    assumptions = pd.DataFrame(
        [(lbl, dflt) for _, (lbl, dflt) in ASSUMPTION_KEYS.items()],
        columns=["Assumption", "Value"])

    guide = _guide_frame(lob_name)

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        guide.to_excel(xw, sheet_name="Guide", index=False)
        demand.to_excel(xw, sheet_name="Demand", index=False)
        roster.to_excel(xw, sheet_name="Roster", index=False)
        nh.to_excel(xw, sheet_name="NewHire", index=False)
        assumptions.to_excel(xw, sheet_name="Assumptions", index=False)
    return buf.getvalue()


def _guide_frame(lob_name: str) -> pd.DataFrame:
    rows = [
        ("Capacity Planner — import template", ""),
        (f"Line of business", lob_name),
        ("", ""),
        ("HOW TO USE", ""),
        ("1.", "Fill the Demand and Roster sheets. Weeks are pre-filled — keep the rows."),
        ("2.", "Add one row per new-hire class on the NewHire sheet (or delete the example)."),
        ("3.", "Adjust the Assumptions sheet values."),
        ("4.", "Upload this workbook on the Data page. Headers can be renamed to common"),
        ("", "synonyms (e.g. 'AHT' for 'AHT (sec)') — the importer maps them."),
        ("", ""),
        ("COLUMN", "MEANING"),
    ]
    for c in DEMAND_COLS:
        rows.append((f"Demand · {c.name}", _describe(c)))
    for c in ROSTER_COLS:
        rows.append((f"Roster · {c.name}", _describe(c)))
    for c in NH_COLS:
        rows.append((f"NewHire · {c.name}", _describe(c)))
    return pd.DataFrame(rows, columns=["Field", "Notes"])


def _describe(c: Col) -> str:
    req = "required" if c.required else f"optional (default {c.default})"
    hints = {
        "Week": "Monday of each week (any date format).",
        "Members": "Serviced population that generates contacts.",
        "CPM": "Calls Per Member (annual) — expected weekly calls = Members × CPM ÷ 52.",
        "Seasonality": "Weekly index, 1.0 = average week (1.15 = +15%). Reshapes the "
                       "year without changing the annual total; blank = 1.0.",
        "Fcst Override": "Leave blank to use the model; fill to force a forecast.",
        "AHT (sec)": "Average handle time in seconds.",
        "LOA": "Headcount on leave that week.",
        "Transfers +/-": "Net headcount moved in (+) or out (-).",
        "Class Start Week": "Week the class begins training.",
        "Class Size": "Heads starting the class.",
        "Training Wks": "Weeks in training before coaching.",
        "Coaching Wks": "Weeks in coaching/nesting before production.",
        "Training Attr %": "% lost during training.",
        "Coaching Attr %": "% of survivors lost during coaching.",
    }
    return f"[{req}] {hints.get(c.name, '')}".strip()


def build_table_csv(table_key: str, weeks: list[str] | None = None) -> str:
    """Single-table CSV template as text (weeks pre-filled when provided)."""
    cols = SCHEMAS[table_key]
    n = len(weeks) if weeks else 0
    data = {}
    for c in cols:
        if c.kind == "week" and weeks and c.name != "Class Start Week":
            data[c.name] = weeks
        else:
            data[c.name] = [None] * n
    return pd.DataFrame(data).to_csv(index=False)
