"""Multi-planner collaboration on a shared network drive.

Each plan YEAR is its own active plan, with its own pointer and its own lock, so
2026 can stay the operating plan while the team builds 2027. Within a year the
team works one plan at a time (spreadsheet-style single writer). This
module owns the three shared-state primitives that live in the scenarios folder
on the share — no database, no server:

  active-<year>.json — pointer naming that year's blessed snapshot + version.
  edit-<year>.lock   — who holds edit control FOR THAT YEAR + heartbeat.
  vNNNN ....json / personal ....json — immutable snapshots (the audit trail).

Design choices:
  * Edit control is a cooperative lock. Acquire is atomic via O_EXCL create;
    a stale lock (no heartbeat within LOCK_STALE_MIN) or an explicit force lets
    another planner take over — always surfaced with a warning in the UI.
  * Publishing writes a NEW immutable snapshot and advances active.json; old
    versions are never mutated, so "restore vN" just publishes a fresh version
    carrying vN's content. That gives a full who/when/what changelog for free.
  * Sandbox what-ifs are saved as `personal` snapshots and never touch the
    pointer, so a read-only viewer can branch off without the lock.

All functions take the scenarios directory as their first argument so the app
can point them at the share path.
"""
import getpass
import json
import os
import re
import socket
import uuid
from datetime import datetime
from pathlib import Path

LOCK_STALE_MIN = 5  # a lock older than this (no heartbeat) may be taken over


# ---------------------------------------------------------------- identity
def who() -> str:
    try:
        return getpass.getuser() or "unknown"
    except Exception:
        return "unknown"


def host() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "?"


# ---------------------------------------------------------------- time utils
def _now() -> datetime:
    return datetime.now()


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse(s: str):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def age_min(iso: str) -> float:
    """Minutes since the given timestamp (huge number if unparseable)."""
    dt = _parse(iso or "")
    return 1e9 if dt is None else (_now() - dt).total_seconds() / 60.0


# ---------------------------------------------------------------- low-level io
def _dir(d) -> Path:
    p = Path(d)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _atomic_write(path: Path, text: str):
    """Write via temp + os.replace so readers never see a half-written file."""
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)  # atomic rename on the same volume


def _read_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------- plan years
# Each plan year is its own independently-published, independently-locked plan,
# so 2026 can stay the operating plan while the team works on 2027. Before this
# there was ONE pointer and ONE lock for the whole folder: publishing 2027 would
# have displaced 2026, and anyone editing either made the other read-only.
#
# LEGACY_YEAR is the single-pointer era's only plan year. Shares written by that
# code have a bare `active.json` / `edit.lock`; both are still read for that year
# so an existing share opens with its full history and needs no migration step.
LEGACY_YEAR = 2026

# Publishing LEGACY_YEAR also writes the bare `active.json`. Everyone launches
# the one copy on the share, so the only window where two code versions coexist
# is a session left OPEN across an update — that process keeps the old module
# until relaunch, and would otherwise read a pointer nobody updates any more.
# Dual-writing costs one small file per publish and closes it.
# REMOVE THIS after the release following the one that introduces it (added
# 2026-07-29). Left alone, a compatibility shim becomes permanent by forgetting,
# and then nobody can tell whether `active.json` still means anything.
DUAL_WRITE_LEGACY = True


# ---------------------------------------------------------------- active pointer
def active_path(d, year) -> Path:
    return Path(d) / f"active-{int(year)}.json"


def legacy_active_path(d) -> Path:
    return Path(d) / "active.json"


def read_active(d, year):
    j = _read_json(active_path(d, year))
    if j is None and int(year) == LEGACY_YEAR:
        j = _read_json(legacy_active_path(d))      # pre-per-year share
    return j


def write_active(d, year, meta: dict):
    dd = _dir(d)
    _atomic_write(active_path(dd, int(year)), json.dumps(meta))
    if DUAL_WRITE_LEGACY and int(year) == LEGACY_YEAR:
        _atomic_write(legacy_active_path(dd), json.dumps(meta))


# --------------------------------------------------------------- budget pointer
# The year's LOCKED-IN official plan — what was committed to before the year
# started, as opposed to `active`, which is the latest published working truth
# (manager ask 2026-08-02, relayed by the user: "we need a budget locked in vs
# the working plan ... so we can see how we have drifted compared to budget").
#
# Deliberately a SECOND pointer at the same immutable snapshots rather than a
# copy of the plan: the budget IS a published version, so locking one costs
# nothing, can never disagree with what was actually published, and keeps the
# whole audit chain (version number, author, note, the Change log's diffs).
#
# Re-baselining is ALLOWED but never silent. Mid-year re-forecasts are real,
# and a system that forbids them gets worked around with a spreadsheet — but a
# budget you can quietly move is not a budget. So `write_budget` PUSHES the
# previous designation onto `history`, which is append-only: the original
# baseline survives every re-lock and the UI can always show what it was.
def budget_path(d, year) -> Path:
    return Path(d) / f"budget-{int(year)}.json"


def read_budget(d, year):
    return _read_json(budget_path(d, year))


def write_budget(d, year, meta: dict):
    """Point `year`'s budget at a published version. Carries the previous
    designation into `history` (oldest first) so a re-baseline is additive."""
    prev = read_budget(d, year)
    hist = list(prev.get("history", [])) if prev else []
    if prev:
        hist.append({k: v for k, v in prev.items() if k != "history"})
    _atomic_write(budget_path(_dir(d), int(year)),
                  json.dumps({**meta, "plan_year": int(year), "history": hist}))


def clear_budget(d, year) -> bool:
    """Remove the designation. The SNAPSHOT is untouched — this unpicks a
    pointer, it never deletes a published version."""
    p = budget_path(_dir(d), int(year))
    if p.exists():
        p.unlink()
        return True
    return False


# ---------------------------------------------------------------- edit lock
def lock_path(d, year) -> Path:
    return Path(d) / f"edit-{int(year)}.lock"


def legacy_lock_path(d) -> Path:
    return Path(d) / "edit.lock"


def _live_lock_path(d, year) -> Path:
    """The lock file to READ AND WRITE for this year.

    Prefers the year-scoped file. Falls back to the bare `edit.lock` only while
    that file still exists and this is LEGACY_YEAR — otherwise a session left
    open on the old code would appear to hold nothing, and new code would hand
    out edit control for a plan someone is actively editing.

    Deliberately NOT dual-written, unlike the pointer: a lock is transient and
    two files would need an atomic O_EXCL race across both. Once anyone acquires
    through this code the year-scoped file exists and wins from then on; the
    stranded legacy lock ages out at LOCK_STALE_MIN and stops mattering."""
    p = lock_path(d, year)
    if not p.exists() and int(year) == LEGACY_YEAR:
        legacy = legacy_lock_path(d)
        if legacy.exists():
            return legacy
    return p


def read_lock(d, year):
    return _read_json(_live_lock_path(d, year))


def lock_is_stale(info: dict | None) -> bool:
    if not info:
        return True
    hb = info.get("heartbeat") or info.get("acquired_at") or ""
    return age_min(hb) > LOCK_STALE_MIN


def _lock_record(user: str, extra: dict | None = None) -> dict:
    # token = per-ACQUISITION ownership (audit 2026-07-14): user alone can't
    # tell two sessions of the same Windows login apart — two tabs were both
    # silently granted edit control. The session that acquired keeps the token
    # in its own state; heartbeat/release/holds require it to match.
    rec = {"user": user, "host": host(), "token": uuid.uuid4().hex,
           "acquired_at": _iso(_now()), "heartbeat": _iso(_now())}
    if extra:
        rec.update(extra)
    return rec


def owns_lock(info: dict | None, user: str, token: str | None) -> bool:
    """Session-level ownership. User must match; when the record carries a
    token (post-2026-07-14), the session's token must match too — a second
    tab or a restarted session must click Take control (which rotates the
    token, downgrading the other session on its next heartbeat). Records
    written before tokens existed fall back to user-match."""
    if not info or info.get("user") != user:
        return False
    if "token" not in info:      # legacy record — user match only
        return True
    return token is not None and info.get("token") == token


def acquire_lock(d, year, user: str, force: bool = False):
    """Try to take edit control OF THIS PLAN YEAR. Returns (ok, lock_info).

    ok=False means someone else holds a *fresh* lock (and force was not set);
    lock_info is then the current holder so the UI can offer takeover."""
    p = _live_lock_path(_dir(d), year)
    cur = read_lock(d, year)
    if cur is None and p.exists():
        # Present but UNREADABLE (crash between O_EXCL create and the JSON
        # write, share hiccup, truncation): before this branch existed, no
        # acquire — not even force — could ever succeed again (audit#2
        # 2026-07-14: force returned (False, None) forever). An unreadable
        # lock has no owner and no heartbeat, so it is stale by definition:
        # preserve it for diagnosis, then fall through to a fresh O_EXCL
        # acquire, which keeps concurrent recoverers racing safely.
        try:
            p.replace(p.with_name(
                f"{p.name}.corrupt-{_now().strftime('%Y%m%d-%H%M%S')}"))
        except OSError:
            pass
        cur = read_lock(d, year)   # a valid lock may have appeared meanwhile
    if cur is None:
        try:  # atomic create — wins the race against another new acquirer
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(_lock_record(user), f)
            return True, read_lock(d, year)
        except FileExistsError:
            cur = read_lock(d, year)  # lost the race; fall through
    if cur and cur.get("user") == user:
        # Same planner, (possibly) another session: re-acquire with a FRESH
        # token so exactly one session owns it — the other downgrades on its
        # next heartbeat instead of both silently editing.
        _atomic_write(p, json.dumps(_lock_record(user)))
        return True, read_lock(d, year)
    if cur and (force or lock_is_stale(cur)):
        _atomic_write(p, json.dumps(
            _lock_record(user, {"taken_over_from": cur.get("user")})))
        return True, read_lock(d, year)
    return False, cur


def heartbeat(d, year, user: str, token: str | None = None) -> bool:
    """Refresh our lock's heartbeat. Returns False if we no longer hold it
    (someone took over, or another session of the same user re-acquired) so
    the caller can downgrade to read-only. The ownership check happens on a
    fresh read immediately before the write — a takeover landing inside that
    milliseconds-wide gap can still be clobbered (plain-filesystem locking
    has no compare-and-swap); the loser then self-detects on its next rerun
    because its own heartbeat/ownership check fails."""
    cur = read_lock(d, year)
    if owns_lock(cur, user, token):
        cur["heartbeat"] = _iso(_now())
        # write back to the file we READ — which may still be the legacy
        # edit.lock during a transition (see _live_lock_path)
        _atomic_write(_live_lock_path(d, year), json.dumps(cur))
        return True
    return False


def release_lock(d, year, user: str, token: str | None = None,
                 force: bool = False) -> bool:
    cur = read_lock(d, year)
    if cur and (force or owns_lock(cur, user, token)):
        try:
            _live_lock_path(d, year).unlink()
        except FileNotFoundError:
            pass
        return True
    return False


def holds_lock(d, year, user: str, token: str | None = None) -> bool:
    return owns_lock(read_lock(d, year), user, token)


# ---------------------------------------------------------------- snapshots
def _safe(name: str) -> str:
    """Filename-safe plan/author names. Length-clamped so a long typed name on a
    deep share path can never breach Windows' 260-char MAX_PATH — the app is
    designed to run without the admin-only long-path setting."""
    return re.sub(r"[^A-Za-z0-9 _-]", "", name or "").strip()[:60] or "scenario"


def _all_snapshots(d) -> list[dict]:
    dd = Path(d)
    if not dd.exists():
        return []
    out = []
    for p in sorted(dd.glob("*.json")):
        if p.name == "active.json":
            continue
        j = _read_json(p)
        if isinstance(j, dict) and "lobs" in j:
            j["_file"] = p.name
            out.append(j)
    return out


def next_version(d) -> int:
    """Next version number — GLOBAL across plan years, deliberately.

    It no longer reads the active pointer, because there is no longer a single
    one. Numbering across years instead of within each keeps every number
    unique and sortable, and keeps the folder legible on the share: per-year
    numbering would put two different files in there both calling themselves
    v0001. The year is carried in the payload (`plan_year`) and displayed
    beside the version, which is what actually answers "which year is this?".
    """
    vs = [j.get("version") for j in _all_snapshots(d)
          if isinstance(j.get("version"), int)]
    return (max(vs) + 1) if vs else 1


def changelog(d, year=None) -> list[dict]:
    """Published versions, newest first (for the history panel).

    `year` filters to one plan year; None returns every year. Snapshots written
    before plan years existed carry no `plan_year`, so they read as LEGACY_YEAR —
    the same default `_apply_payload` has always used when loading them."""
    snaps = [j for j in _all_snapshots(d) if isinstance(j.get("version"), int)]
    if year is not None:
        snaps = [j for j in snaps
                 if int(j.get("plan_year", LEGACY_YEAR)) == int(year)]
    return sorted(snaps, key=lambda j: j["version"], reverse=True)


def load_snapshot(d, fname: str):
    return _read_json(Path(d) / fname)


def publish(d, payload: dict, name: str, author: str,
            parent_version, note: str = "", year=None):
    """Write a new immutable version snapshot and advance THIS YEAR's pointer.

    `payload` carries the business data (n_weeks / members / lobs / plan_year).
    `year` says which pointer to advance; it defaults to the payload's own
    plan_year, which is the only value that can be right — publishing a 2027
    payload must never move 2026's pointer."""
    if year is None:
        year = payload.get("plan_year", LEGACY_YEAR)
    ver = next_version(d)
    stamp = _now().strftime("%Y%m%d-%H%M%S")
    meta = {**payload, "name": name, "author": author,
            "published_at": _iso(_now()), "saved_at": stamp,
            "version": ver, "parent_version": parent_version,
            "note": note, "kind": "active"}
    fname = f"v{ver:04d} {stamp} {_safe(name)}.json"
    _atomic_write(_dir(d) / fname, json.dumps(meta))
    write_active(d, year, {"file": fname, "version": ver, "name": name,
                           "author": author, "plan_year": int(year),
                           "published_at": meta["published_at"], "note": note})
    return meta, fname


def save_personal(d, payload: dict, name: str, author: str):
    """Save a private what-if snapshot. Does NOT touch the active pointer."""
    stamp = _now().strftime("%Y%m%d-%H%M%S")
    meta = {**payload, "name": name, "author": author,
            "published_at": _iso(_now()), "saved_at": stamp,
            "version": None, "kind": "personal"}
    fname = f"personal {stamp} {_safe(author)} {_safe(name)}.json"
    _atomic_write(_dir(d) / fname, json.dumps(meta))
    return meta, fname


def personal_snapshots(d, author: str | None = None) -> list[dict]:
    snaps = [j for j in _all_snapshots(d) if j.get("kind") == "personal"]
    if author is not None:
        snaps = [j for j in snaps if j.get("author") == author]
    return sorted(snaps, key=lambda j: j.get("saved_at", ""), reverse=True)
