"""
Importers for the three real feeds the WFM team already exports:

  * Skill_Mapping.csv  — the join table: Skill_ID / Queue_Name -> Line of Business
  * wfm.csv         — WFM interval export (the forecast is already done here:
                         Forecasted_CV, Forecasted_AHT, Required_FTE per queue)
  * split.csv          — ACD hsplit interval export (actual staffed time + AUX)

Nothing here forecasts. WFM and ACD become the *reality check* against the
planner's built-in model:
  - WFM's interval Required_FTE  -> weekly ON-ROLL FTE benchmark (seated-hours
    method: sum(Required_FTE)*interval_hrs, grossed up by shrinkage over paid hrs).
  - ACD staffed time + AUX          -> measured actual staffed FTE and in-office
    shrinkage, to calibrate the model's assumptions instead of guessing.

Grain note: WFM/ACD rows are 15-min intervals. We roll up to weeks (Mon-anchored)
and always report how many *days* each week actually covers — a partial week (the
sanitized sample is a single day) must never be read as a full week.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

INTERVAL_HOURS = 0.25  # 15-minute intervals

# Fallback gross-up assumptions when the caller doesn't pass per-LOB ones.
DEFAULT_SHRINKAGE_PCT = 32.0
DEFAULT_PAID_HRS_WEEK = 40.0
DEFAULT_OCCUPANCY_PCT = 85.0
DEFAULT_MARGIN_PCT = 0.0


# ----------------------------------------------------------------------
# Report (mirrors data_io.ImportReport so the UI treats both the same)
# ----------------------------------------------------------------------
@dataclass
class ImportReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# ----------------------------------------------------------------------
# Skill mapping
# ----------------------------------------------------------------------
@dataclass
class Mapping:
    split_to_lob: dict          # int Skill_ID -> LOB
    queue_to_lob: dict          # Queue_Name    -> LOB
    lobs: list                  # ordered unique LOBs

    def lob_for_split(self, code) -> str | None:
        try:
            return self.split_to_lob.get(int(code))
        except (TypeError, ValueError):
            return None

    def lob_for_queue(self, name) -> str | None:
        return self.queue_to_lob.get(str(name).strip())


def load_mapping(file, field_map: dict | None = None
                 ) -> tuple[Mapping | None, ImportReport]:
    """Read the skill→LOB join table. Like the feeds, its columns resolve via
    saved map → aliases → canonical names, so another system's export of the
    same table works once mapped."""
    rep = ImportReport()
    try:
        m = pd.read_csv(file, encoding="utf-8-sig")
    except Exception as exc:  # noqa: BLE001
        rep.errors.append(f"Could not read mapping: {exc}")
        return None, rep

    m, resolved, missing, auto = resolve_columns(m, "mapping", field_map)
    if missing:
        rep.errors.append(
            "Mapping file missing required field(s): "
            + ", ".join(f"{field_label(m)} ({m})" for m in missing)
            + ". Map them to this file's columns in 🧩 Column mapping (🔌 Real Data).")
        return None, rep
    if auto:
        rep.notes.append("Auto-matched by alias: "
                         + ", ".join(f"{field_label(c)} ← {resolved[c]}"
                                     for c in auto))

    split_to_lob, queue_to_lob = {}, {}
    for _, r in m.iterrows():
        lob = str(r["Line_of_Business"]).strip()
        if not lob or lob.lower() == "nan":
            continue
        if pd.notna(r["Skill_ID"]):
            try:
                split_to_lob[int(r["Skill_ID"])] = lob
            except (TypeError, ValueError):
                pass
        if pd.notna(r["Queue_Name"]) and str(r["Queue_Name"]).strip():
            queue_to_lob[str(r["Queue_Name"]).strip()] = lob

    lobs = sorted(set(split_to_lob.values()) | set(queue_to_lob.values()))
    if not lobs:
        rep.errors.append("Mapping produced no lines of business.")
        return None, rep
    rep.notes.append(
        f"Mapped {len(split_to_lob)} skill(s) and {len(queue_to_lob)} queue(s) "
        f"to {len(lobs)} LOB(s).")
    return Mapping(split_to_lob, queue_to_lob, lobs), rep


# ----------------------------------------------------------------------
# Shared helpers
# ----------------------------------------------------------------------
def _read_concat(files) -> pd.DataFrame:
    frames = [pd.read_csv(f, encoding="utf-8-sig") for f in files]
    return pd.concat(frames, ignore_index=True)


def _week_start(d: pd.Series) -> pd.Series:
    """Monday of the week for a date-like series, as ISO 'YYYY-MM-DD'."""
    dt = pd.to_datetime(d, errors="coerce")
    monday = dt - pd.to_timedelta(dt.dt.weekday, unit="D")
    return monday.dt.date.astype("string")


def _seated_hrs_to_fte(seated_hrs, shrink_pct, paid_hrs_week) -> float:
    """On-roll FTE needed = seated-hours grossed up for shrinkage / paid hours."""
    denom = paid_hrs_week * (1 - shrink_pct / 100)
    return seated_hrs / denom if denom > 0 else np.nan


# ----------------------------------------------------------------------
# Vendor-agnostic column mapping
#
# The app works in CANONICAL field names (the ones the engine and rollups
# use). Any vendor's export — WFM today, NICE IEX tomorrow, a hand-built
# CSV — is admitted by mapping ITS headers onto these. Resolution order:
#   1. an explicit saved map (planner picked the columns in the UI), then
#   2. alias auto-detection (case/punctuation-insensitive), then
#   3. the canonical name itself.
# Required fields still block the import when unresolved — loudly, naming
# exactly what is missing. Unknown extra columns are always ignored.
# ----------------------------------------------------------------------
FIELDS = {
    "mapping": [   # the join table itself (any WFM/ACD vendor's export of it)
        ("Skill_ID", True, ("skill id", "skill", "split", "split id", "queue id",
                            "acd skill", "skill number")),
        ("Line_of_Business", True, ("line of business", "lob", "business line",
                                    "department", "team", "service")),
        ("Queue_Name", True, ("queue name", "queue", "ctgroup", "contact group",
                              "management unit", "mu")),
    ],
    "wfm": [   # forecast + actuals feed (WFM / IEX / …)
        ("Date", True, ("date", "datetime", "interval start", "start time",
                        "timestamp", "date/time", "period")),
        ("Queue", True, ("queue", "queue name", "ctgroup", "contact group",
                         "skill", "service", "queue_id", "mu", "management unit")),
        ("Required_FTE", True, ("required fte", "req fte", "required staff",
                                "requirement", "required agents", "req agents")),
        ("Forecasted_CV", True, ("forecast cv", "forecast volume", "fcst volume",
                                 "forecast contacts", "forecast calls",
                                 "forecasted contacts", "fcst cv")),
        ("Forecasted_AHT", True, ("forecast aht", "fcst aht", "forecasted aht",
                                  "forecast handle time")),
        ("Actual_CV", True, ("actual cv", "actual volume", "actual contacts",
                             "actual calls", "offered", "contacts offered",
                             "calls offered", "volume")),
        ("Actual_AHT", True, ("actual aht", "aht", "handle time",
                              "average handle time")),
        ("Actual_ASA", False, ("actual asa", "asa", "average speed of answer",
                               "avg speed answer")),
        ("Actual_Abandonment", False, ("actual abandonment", "abandon rate",
                                       "abandonment", "abandon %", "aband rate")),
        ("Actual_Occupancy", False, ("actual occupancy", "occupancy", "occ %",
                                     "occupancy %")),
        ("Actual_PCA", False, ("actual pca", "actual sl", "actual service level",
                               "service level", "sl %", "sl actual", "pca")),
        ("Forecasted_PCA", False, ("forecast pca", "forecast sl",
                                   "forecasted service level", "fcst sl")),
        ("Required_PCA", False, ("required pca", "sl target", "service level target",
                                 "sl goal", "target sl")),
    ],
    "acd": [   # staffed-time + AUX feed (ACD hsplit / IEX-side ACD / …)
        ("row_date", True, ("row date", "date", "day")),
        ("split", True, ("split", "skill", "skill id", "skill_id", "queue id",
                         "split/skill", "group")),
        ("i_stafftime", True, ("staffed time", "staff time", "staffed seconds",
                               "stafftime", "logged in time", "staffed_sec")),
        ("i_auxtime", True, ("aux time", "auxtime", "total aux", "aux seconds",
                             "aux_sec", "unavailable time")),
        ("starttime", False, ("start time", "interval", "time", "interval start")),
        ("i_availtime", False, ("available time", "avail time", "idle time")),
        ("i_acdtime", False, ("acd time", "talk time", "handle time")),
        ("i_acwtime", False, ("acw time", "after call work", "wrap time")),
        ("i_othertime", False, ("other time",)),
    ],
}


# Human labels for the canonical fields — what planners see in the 🧩 column
# mapper and in import warnings (the canonical name rides along in parentheses
# so warnings stay greppable). One home: UI and reports both read from here.
FIELD_LABELS = {
    "Skill_ID": "Split / skill ID",
    "Line_of_Business": "Line of business",
    "Queue_Name": "Queue name",
    "Date": "Date / interval start",
    "Queue": "Queue name",
    "Required_FTE": "Required staff per interval",
    "Forecasted_CV": "Forecast contacts",
    "Forecasted_AHT": "Forecast handle time (AHT, sec)",
    "Actual_CV": "Actual contacts",
    "Actual_AHT": "Actual handle time (AHT, sec)",
    "Actual_ASA": "Actual speed of answer (ASA, sec)",
    "Actual_Abandonment": "Actual abandon rate",
    "Actual_Occupancy": "Actual occupancy %",
    "Actual_PCA": "Actual service level %",
    "Forecasted_PCA": "Forecast service level %",
    "Required_PCA": "Service level target %",
    "row_date": "Date",
    "split": "Split / skill number",
    "i_stafftime": "Staffed time (sec)",
    "i_auxtime": "AUX time (sec)",
    "starttime": "Interval start time",
    "i_availtime": "Available (idle) time (sec)",
    "i_acdtime": "Talk time (sec)",
    "i_acwtime": "After-call work time (sec)",
    "i_othertime": "Other time (sec)",
}


def field_label(name: str) -> str:
    """Plain-English label for a canonical field name (falls back to itself)."""
    return FIELD_LABELS.get(name, name)


def _key(s) -> str:
    """Header comparison key: case- and punctuation-insensitive."""
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def peek_headers(files) -> list[str]:
    """Column headers of the export(s) — readable even when the import fails,
    so the planner can map columns by hand in the UI."""
    files = files if isinstance(files, (list, tuple)) else [files]
    try:
        return list(pd.read_csv(files[0], encoding="utf-8-sig", nrows=0).columns)
    except Exception:  # noqa: BLE001
        return []


def resolve_columns(df: pd.DataFrame, feed: str, saved_map: dict | None = None
                    ) -> tuple[pd.DataFrame, dict, list[str], list[str]]:
    """Rename a vendor export's columns to canonical names.

    Returns (renamed_df, resolved {canonical: source_column},
             missing_required, auto_detected_canonicals). Never guesses past
    the alias table; anything unresolved is reported, not invented."""
    saved = {k: v for k, v in (saved_map or {}).items() if v}
    by_key = {_key(c): c for c in df.columns}
    resolved, missing, auto = {}, [], []
    for canon, required, aliases in FIELDS[feed]:
        src_col = None
        if canon in saved and saved[canon] in df.columns:
            src_col = saved[canon]                       # planner's explicit pick
        elif canon in df.columns:
            src_col = canon                              # already canonical
        else:
            for cand in (canon,) + tuple(aliases):       # alias auto-detect
                hit = by_key.get(_key(cand))
                if hit is not None:
                    src_col, _ = hit, auto.append(canon)
                    break
        if src_col is not None:
            resolved[canon] = src_col
        elif required:
            missing.append(canon)
    renamed = df.rename(columns={v: k for k, v in resolved.items() if v != k})
    return renamed, resolved, missing, auto


def load_wfm(files, mapping: Mapping, field_map: dict | None = None
                ) -> tuple[pd.DataFrame, ImportReport]:
    """Read WFM interval export(s) (WFM, IEX, …) and tag each row with its
    LOB. `field_map` = saved {canonical: source column} from the UI mapper."""
    rep = ImportReport()
    files = files if isinstance(files, (list, tuple)) else [files]
    try:
        df = _read_concat(files)
    except Exception as exc:  # noqa: BLE001
        rep.errors.append(f"Could not read WFM export: {exc}")
        return pd.DataFrame(), rep

    df, resolved, missing, auto = resolve_columns(df, "wfm", field_map)
    if missing:
        rep.errors.append(
            "WFM export missing required field(s): "
            + ", ".join(f"{field_label(m)} ({m})" for m in missing)
            + ". Map them to this file's columns in 🧩 Column mapping "
              "(🔌 Real Data page) — any vendor's export works once mapped.")
        return pd.DataFrame(), rep
    if auto:
        rep.notes.append("Auto-matched by alias: "
                         + ", ".join(f"{field_label(c)} ← {resolved[c]}"
                                     for c in auto))

    df = df.copy()
    df["LOB"] = df["Queue"].map(mapping.lob_for_queue)
    unmapped = sorted(set(df.loc[df["LOB"].isna(), "Queue"].dropna().unique()))
    if unmapped:
        rep.warnings.append(
            f"{len(unmapped)} WFM queue(s) not in mapping — excluded: "
            + ", ".join(unmapped[:6]) + ("…" if len(unmapped) > 6 else ""))
    df = df[df["LOB"].notna()].copy()
    if df.empty:
        rep.errors.append("No WFM rows matched the mapping.")
        return pd.DataFrame(), rep

    opt = ["Actual_ASA", "Actual_Abandonment", "Actual_Occupancy",
           "Actual_PCA", "Forecasted_PCA", "Required_PCA"]
    _raw_cols = {}
    for c in (["Required_FTE", "Forecasted_CV", "Forecasted_AHT",
               "Actual_CV", "Actual_AHT"] + [o for o in opt if o in df.columns]):
        _raw_cols[c] = df[c].copy()          # pre-coercion, for blank-vs-malformed
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["Week"] = _week_start(df["Date"])
    df["_date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date
    # Post-coercion honesty (audit 2026-07-14): presence checks can't catch a
    # date-format change or text pollution — those coerce to NaT/NaN, NaT-dated
    # rows silently vanished in the weekly groupby, and all-NaN weeks summed
    # to a plausible-looking 0. Count, say so loudly, drop unreadable dates
    # EXPLICITLY, and reject an import with no readable dates at all.
    bad_dates = int(df["_date"].isna().sum())
    if bad_dates:
        rep.warnings.append(
            f"{bad_dates:,} WFM row(s) have unreadable dates — EXCLUDED from "
            "weekly rollups (a date-format change in the export is the usual "
            "cause; fix the export or the 🧩 column mapping).")
        df = df[df["_date"].notna()].copy()
        if df.empty:
            rep.errors.append("Every WFM row had an unreadable date — import "
                              "rejected rather than shown as empty weeks.")
            return pd.DataFrame(), rep
    # Two distinct hazards (audit#2 2026-07-14), two calibrations:
    # MALFORMED non-blank values ("oops", "#N/A") — a healthy export has ZERO,
    # so ANY count is corruption and warns, even one (a single silent -1 was
    # the audit's repro). Legitimate BLANKS are normal interval data (~17%
    # blank Actual_CV on the clean sample — not-yet-elapsed intervals), so
    # blank share warns only above 25% (a format-change tell).
    for c in ["Required_FTE", "Forecasted_CV", "Forecasted_AHT",
              "Actual_CV", "Actual_AHT"]:
        raw = _raw_cols[c]
        blank = raw.isna() | (raw.astype(str).str.strip() == "")
        malformed = int((~blank & df[c].isna()).sum())
        if malformed:
            rep.warnings.append(
                f"WFM '{field_label(c)}' ({c}): {malformed:,} MALFORMED non-blank "
                "value(s) read as blank — a healthy export has none; the "
                "affected intervals no longer count toward totals. Check the "
                "export format.")
        n_blank = int(blank.sum())
        if n_blank and n_blank / len(df) > 0.25:
            rep.warnings.append(
                f"WFM '{field_label(c)}' ({c}): {n_blank:,} of {len(df):,} value(s) "
                "blank — unusually high; all-blank weeks stay missing in the "
                "rollup, never 0. If this export used to load cleanly, its "
                "format changed.")
    rep.notes.append(f"Loaded {len(df):,} WFM interval rows across "
                     f"{df['_date'].nunique()} day(s).")
    return df, rep


def wfm_weekly(df: pd.DataFrame, assumptions: dict | None = None
                  ) -> pd.DataFrame:
    """Roll interval WFM rows up to weekly per-LOB on-roll FTE benchmark.

    `assumptions` is an optional {lob: {shrinkage_pct, paid_hours_per_week}} map;
    LOBs absent from it fall back to module defaults.
    """
    assumptions = assumptions or {}
    rows = []
    for (lob, week), g in df.groupby(["LOB", "Week"]):
        a = assumptions.get(lob, {})
        shrink = float(a.get("shrinkage_pct", DEFAULT_SHRINKAGE_PCT))
        paid = float(a.get("paid_hours_per_week", DEFAULT_PAID_HRS_WEEK))
        occ = float(a.get("occupancy_pct", DEFAULT_OCCUPANCY_PCT))
        margin = float(a.get("workload_margin_pct", DEFAULT_MARGIN_PCT))
        # min_count=1: a week whose column failed coercion entirely must stay
        # MISSING — the pandas default would sum all-NaN to a plausible 0
        # (audit 2026-07-14; same rule as the app-side _bench_series).
        seated_hrs = g["Required_FTE"].sum(min_count=1) * INTERVAL_HOURS
        fcst_cv = g["Forecasted_CV"].sum(min_count=1)
        act_cv = g["Actual_CV"].sum(min_count=1)
        # Contact-weighted handle times & service metrics (blank intervals ignored).
        f_aht = _wavg(g["Forecasted_AHT"], g["Forecasted_CV"])
        a_aht = _wavg(g["Actual_AHT"], g["Actual_CV"])
        a_asa = _wavg(g["Actual_ASA"], g["Actual_CV"]) if "Actual_ASA" in g else np.nan
        a_ab = _wavg(g["Actual_Abandonment"], g["Actual_CV"]) if "Actual_Abandonment" in g else np.nan
        a_occ = _wavg(g["Actual_Occupancy"], g["Actual_CV"]) if "Actual_Occupancy" in g else np.nan
        a_occ = a_occ if (pd.notna(a_occ) and a_occ > 0) else np.nan  # 0% = not reported
        # PCA is WFM's service level (% answered within threshold, i.e.
        # acceptable ÷ offered — verified against ACD acceptable/callsoffered),
        # so the weekly figure is the contact-weighted interval average.
        f_sl = _wavg(g["Forecasted_PCA"], g["Forecasted_CV"]) if "Forecasted_PCA" in g else np.nan
        a_sl = _wavg(g["Actual_PCA"], g["Actual_CV"]) if "Actual_PCA" in g else np.nan
        # Required_PCA is the queue's SL target (75/80); constant per queue, so
        # the contact-weighted blend only matters when an LOB mixes targets.
        t_sl = _wavg(g["Required_PCA"], g["Actual_CV"]) if "Required_PCA" in g else np.nan
        # Workload-based requirement from WFM's forecast CV × AHT and OUR
        # assumptions — same formula as compute_plan. Alternative to WFM's
        # interval Required_FTE, whose per-interval minimum staffing inflates
        # the weekly sum for small queues with long open hours.
        prod_hrs = paid * (1 - shrink / 100) * (occ / 100)
        wl_fte = (fcst_cv * f_aht / 3600 * (1 + margin / 100) / prod_hrs
                  if pd.notna(f_aht) and fcst_cv > 0 and prod_hrs > 0 else np.nan)
        rows.append({
            "LOB": lob, "Week": week,
            "Days Covered": g["_date"].nunique(),
            "WFM Required FTE": round(_seated_hrs_to_fte(seated_hrs, shrink, paid), 1),
            "Workload Req FTE": round(wl_fte, 1) if pd.notna(wl_fte) else np.nan,
            "Seated Hrs (req)": round(seated_hrs, 1),
            "Forecast Contacts": float(fcst_cv) if pd.notna(fcst_cv) else np.nan,
            "Actual Contacts": float(act_cv) if pd.notna(act_cv) else np.nan,
            "Forecast AHT (sec)": round(f_aht, 0) if pd.notna(f_aht) else np.nan,
            "Actual AHT (sec)": round(a_aht, 0) if pd.notna(a_aht) else np.nan,
            "Forecast SL %": round(f_sl, 1) if pd.notna(f_sl) else np.nan,
            "Actual SL %": round(a_sl, 1) if pd.notna(a_sl) else np.nan,
            "SL Target %": round(t_sl, 1) if pd.notna(t_sl) else np.nan,
            "Actual ASA (sec)": round(a_asa, 0) if pd.notna(a_asa) else np.nan,
            "Abandon (actual)": round(a_ab, 2) if pd.notna(a_ab) else np.nan,
            "Occupancy % (actual)": round(a_occ, 1) if pd.notna(a_occ) else np.nan,
        })
    out = pd.DataFrame(rows)
    return out.sort_values(["LOB", "Week"]).reset_index(drop=True) if not out.empty else out


def _wavg(values: pd.Series, weights: pd.Series) -> float:
    v = pd.to_numeric(values, errors="coerce")
    w = pd.to_numeric(weights, errors="coerce").fillna(0)
    mask = v.notna() & (w > 0)
    return float((v[mask] * w[mask]).sum() / w[mask].sum()) if mask.any() else np.nan


# ----------------------------------------------------------------------
# ACD split  ->  actual staffed FTE + measured in-office shrinkage
# ----------------------------------------------------------------------
def load_split(files, mapping: Mapping, field_map: dict | None = None
               ) -> tuple[pd.DataFrame, ImportReport]:
    """Read ACD interval export(s) (ACD hsplit, …). `field_map` = saved
    {canonical: source column} from the UI mapper."""
    rep = ImportReport()
    files = files if isinstance(files, (list, tuple)) else [files]
    try:
        df = _read_concat(files)
    except Exception as exc:  # noqa: BLE001
        rep.errors.append(f"Could not read ACD export: {exc}")
        return pd.DataFrame(), rep

    df, resolved, missing, auto = resolve_columns(df, "acd", field_map)
    if missing:
        rep.errors.append(
            "ACD export missing required field(s): "
            + ", ".join(f"{field_label(m)} ({m})" for m in missing)
            + ". Map them to this file's columns in 🧩 Column mapping "
              "(🔌 Real Data page).")
        return pd.DataFrame(), rep
    if auto:
        rep.notes.append("Auto-matched by alias: "
                         + ", ".join(f"{field_label(c)} ← {resolved[c]}"
                                     for c in auto))

    df = df.copy()
    df["LOB"] = df["split"].map(mapping.lob_for_split)
    unmapped = sorted(set(df.loc[df["LOB"].isna(), "split"].dropna().unique()))
    if unmapped:
        rep.warnings.append(
            f"{len(unmapped)} ACD split code(s) not in mapping — excluded: "
            + ", ".join(str(int(c)) for c in unmapped))
    df = df[df["LOB"].notna()].copy()
    if df.empty:
        rep.errors.append("No ACD split rows matched the mapping.")
        return pd.DataFrame(), rep

    for c in ["i_stafftime", "i_auxtime"]:
        raw = df[c]
        coerced = pd.to_numeric(raw, errors="coerce")
        blank = raw.isna() | (raw.astype(str).str.strip() == "")
        malformed = int((~blank & coerced.isna()).sum())
        if malformed:
            # Corruption stays UNKNOWN (NaN), never a fake observed 0 — zero
            # staffed time is a real possible measurement (audit#2 2026-07-14).
            rep.warnings.append(
                f"ACD '{field_label(c)}' ({c}): {malformed:,} MALFORMED non-blank value(s) kept as "
                "missing — affected weeks show missing staffing, not an "
                "understated figure. Check the export format.")
        # legitimate blanks = 0 (an interval with no AUX/staffed seconds);
        # malformed non-blanks stay NaN (unknown), valid values pass through
        df[c] = coerced.mask(blank & coerced.isna(), 0.0)
    df["Week"] = _week_start(df["row_date"])
    df["_date"] = pd.to_datetime(df["row_date"], errors="coerce").dt.date
    bad_dates = int(df["_date"].isna().sum())
    if bad_dates:
        rep.warnings.append(
            f"{bad_dates:,} ACD row(s) have unreadable dates — EXCLUDED from "
            "weekly rollups (date-format change in the export is the usual cause).")
        df = df[df["_date"].notna()].copy()
        if df.empty:
            rep.errors.append("Every ACD row had an unreadable date — import "
                              "rejected rather than shown as empty weeks.")
            return pd.DataFrame(), rep
    rep.notes.append(f"Loaded {len(df):,} ACD interval rows across "
                     f"{df['_date'].nunique()} day(s).")
    return df, rep


def split_weekly(df: pd.DataFrame, assumptions: dict | None = None
                 ) -> pd.DataFrame:
    """Weekly per-LOB actual staffed FTE + measured in-office shrinkage %."""
    assumptions = assumptions or {}
    rows = []
    for (lob, week), g in df.groupby(["LOB", "Week"]):
        a = assumptions.get(lob, {})
        paid = float(a.get("paid_hours_per_week", DEFAULT_PAID_HRS_WEEK))
        staff_hrs = g["i_stafftime"].sum(min_count=1) / 3600   # missing ≠ 0
        aux_hrs = g["i_auxtime"].sum(min_count=1) / 3600
        days = g["_date"].nunique()
        # Staffed hours are already net of AUX-out-of-office by ACD definition;
        # in-office shrink here = AUX time as a share of staffed time.
        shrink_pct = (aux_hrs / staff_hrs * 100) if staff_hrs else np.nan
        rows.append({
            "LOB": lob, "Week": week, "Days Covered": days,
            "Actual Staffed FTE": round(staff_hrs / paid, 1) if paid else np.nan,
            "Staffed Hrs": round(staff_hrs, 1),
            "In-Office Shrink %": round(shrink_pct, 1) if pd.notna(shrink_pct) else np.nan,
        })
    out = pd.DataFrame(rows)
    return out.sort_values(["LOB", "Week"]).reset_index(drop=True) if not out.empty else out
