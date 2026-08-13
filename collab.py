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
import copy
import getpass
import json
import os
import re
import socket
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

LOCK_STALE_MIN = 5  # a lock older than this (no heartbeat) may be taken over
# Refresh the heartbeat at most this often. Comfortably inside LOCK_STALE_MIN,
# and it turns a per-rerun share WRITE into roughly one per minute.
HEARTBEAT_MIN_SEC = 45


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


def _atomic_write(path: Path, text: str, _count: bool = True):
    """Write via temp + os.replace so readers never see a half-written file.

    `_count=False` keeps the background draft writer's time out of IO_PERF —
    that tally is "share I/O the planner WAITED on this run", and the whole
    point of the async writer is that nobody waits on it."""
    t0 = time.perf_counter()
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)  # atomic rename on the same volume
    # Any write to shared state invalidates every cache layer — otherwise a
    # read later in the SAME run would serve the pre-write value (take-control
    # read back a stale lock and stayed read-only: 11 checks red, 2026-08-10).
    _invalidate(path)
    if _count:
        IO_PERF["s"] += time.perf_counter() - t0
        IO_PERF["n"] += 1


# ---------------------------------------------------------------- per-run memo
# Pointers and the lock are read UNCACHED by design — that is how a colleague's
# publish or takeover becomes visible without a refresh button. But within ONE
# script run the same file was being fetched three to five times (measured
# 2026-08-10: 6 pointer reads, 5 directory scans and 3 existence checks for a
# single grid edit), and its value cannot change mid-run. On a slow SMB share
# every one of those is a round trip, and round trips are what the planner
# feels — 20 of them at ~250 ms is the 5-7 second freeze they reported.
#
# So: memoize for the DURATION of a run, and drop it when the next run starts.
# Liveness across reruns is unchanged, which is the property that matters.
# Every writer below clears the memo, so a read after a write in the same run
# still sees the write.
_RUN_MEMO: dict = {}


# Share-I/O cost of the CURRENT script run, for the sidebar's timing line —
# the user cannot see round trips, so the app tells them (2026-08-13, after
# an assumption change measured at 15 seconds on the real share while the
# dev model said 2-3: stop estimating, let each deployment report itself).
IO_PERF = {"s": 0.0, "n": 0}


def new_run() -> None:
    """Drop the per-run memo and the I/O tally. Called once at the top of
    each script run."""
    _RUN_MEMO.clear()
    IO_PERF["s"] = 0.0
    IO_PERF["n"] = 0


def _memo(key, produce):
    if key not in _RUN_MEMO:
        _RUN_MEMO[key] = produce()
    return _RUN_MEMO[key]


def _read_json(path: Path):
    # No `exists()` pre-check: on a network share that is a SECOND round trip
    # for every pointer read, and the missing-file case is already handled by
    # the except. Pointers (active-, budget-, the lock) are read uncached on
    # every rerun, so this halves the fixed cost of each one.
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ------------------------------------------------- cross-run stat validation
# Round three of the share work (2026-08-13, "it took nine seconds"). The
# run-memo stopped repeat reads WITHIN a run; every rerun still paid a full
# OPEN for each pointer file and a scandir per listed directory, and on SMB an
# open+read+close is 2-3 round trips where a stat is one. The overwhelmingly
# common case is "nothing changed since the last rerun", so:
#
#   * a pointer file is revalidated with ONE os.stat — (mtime_ns, size)
#     unchanged serves the cached parse; moving triggers a real read;
#   * a listed DIRECTORY is revalidated by its own mtime — creating, deleting
#     or atomically replacing any entry bumps it, so one stat answers "did
#     anything change?" and the scandir is only paid when it did.
#
# Liveness is preserved because every shared-state change here is a file
# create, unlink or atomic replace — all of which move the stats being
# checked. Windows' SMB attribute cache can serve a stale stat for a few
# seconds (FileInfoCacheLifetime, default 10s); acceptable for signals acted
# on at human timescale, and the lock ACQUIRE path never trusts a stat — the
# O_EXCL create stays the arbiter. Values are deep-copied on the way out:
# they now cross rerun boundaries, and heartbeat() MUTATES the dict it read.
_STAT_CACHE: dict[str, tuple[int, int, object]] = {}
_DIR_CACHE: dict[str, tuple[int, tuple]] = {}

# Round five (2026-08-13, the timing line's first field report: share I/O
# 1.35 s over 6 ops ≈ 225 ms per stat — paid on EVERY keystroke). Liveness
# signals don't need per-keystroke freshness: within this window a validated
# value is served without even the stat, so a burst of grid edits touches the
# share ZERO times. The cost, stated: a colleague's publish/takeover shows up
# to LIVENESS_MIN_SEC later (on top of SMB's own ~10 s attribute cache) —
# nothing against LOCK_STALE_MIN=5 min, and one's OWN actions are always
# immediate because every writer calls _invalidate.
LIVENESS_MIN_SEC = 5.0
_LAST_CHECK: dict[str, float] = {}
_RESOLVE_CACHE: dict[tuple, object] = {}


def _invalidate(path: Path):
    """Called wherever shared state changes: the file's own cached parse and
    its parent directory's cached listing are dropped, plus the whole run
    memo and the liveness clocks. Invalidate where the change happens, never
    on the way in."""
    _STAT_CACHE.pop(str(path), None)
    _DIR_CACHE.pop(str(Path(path).parent), None)
    _LAST_CHECK.pop(str(path), None)
    _LAST_CHECK.pop(str(Path(path).parent), None)
    _RESOLVE_CACHE.clear()
    _RUN_MEMO.clear()


def _read_json_pointer(path: Path):
    """Pointer-file reader (active-, budget-, the lock): NO share op inside
    the liveness window, one os.stat to revalidate after it, a full read only
    when the file actually moved. Missing files cache as None ((-1,-1)
    signature) so an absent budget pointer is not re-statted per keystroke."""
    key = str(path)
    now = time.monotonic()
    if key in _STAT_CACHE and now - _LAST_CHECK.get(key, -1e9) < LIVENESS_MIN_SEC:
        return copy.deepcopy(_STAT_CACHE[key][2])
    t0 = time.perf_counter()
    try:
        try:
            stt = os.stat(path)
        except OSError:
            _STAT_CACHE[key] = (-1, -1, None)
            _LAST_CHECK[key] = now
            return None
        hit = _STAT_CACHE.get(key)
        if hit and hit[0] == stt.st_mtime_ns and hit[1] == stt.st_size:
            _LAST_CHECK[key] = now
            return copy.deepcopy(hit[2])
        val = _read_json(path)
        if val is None:
            return None             # unreadable ≠ missing: never cache a fluke
        _STAT_CACHE[key] = (stt.st_mtime_ns, stt.st_size, val)
        _LAST_CHECK[key] = now
        return copy.deepcopy(val)
    finally:
        IO_PERF["s"] += time.perf_counter() - t0
        IO_PERF["n"] += 1


def _dir_entries(d) -> tuple:
    """(name, mtime_ns, size, is_file) for every entry in `d` — ONE scandir
    shared by every listing consumer (version history, drafts, the corrupt-
    lock caption), revalidated across reruns by the directory's own mtime."""
    def _load():
        key = str(d)
        now = time.monotonic()
        if key in _DIR_CACHE and now - _LAST_CHECK.get(key, -1e9) < LIVENESS_MIN_SEC:
            return _DIR_CACHE[key][1]
        t0 = time.perf_counter()
        try:
            try:
                dstt = os.stat(d)
            except OSError:
                _DIR_CACHE.pop(key, None)
                return ()
            hit = _DIR_CACHE.get(key)
            if hit and hit[0] == dstt.st_mtime_ns:
                _LAST_CHECK[key] = now
                return hit[1]
            try:
                with os.scandir(d) as it:
                    ents = []
                    for e in it:
                        try:
                            stt = e.stat()  # free on Windows: rides the listing
                            ents.append((e.name, stt.st_mtime_ns, stt.st_size,
                                         e.is_file()))
                        except OSError:
                            continue
            except OSError:
                return ()
            ents = tuple(ents)
            _DIR_CACHE[key] = (dstt.st_mtime_ns, ents)
            _LAST_CHECK[key] = now
            return ents
        finally:
            IO_PERF["s"] += time.perf_counter() - t0
            IO_PERF["n"] += 1
    return _memo(("dirents", str(d)), _load)


# ---------------------------------------------------- background draft writer
# The auto-draft is the one share WRITE on the hot path: it ran inside the
# rerun, so every change waited on the share (plus whatever antivirus adds on
# writes). Now the rerun hands the payload to a single daemon worker and
# finishes; the worker writes on its own time.
#
#   * LATEST WINS, one slot per path: three quick edits while the share is
#     slow write once, with the newest payload — the queue cannot back up.
#   * Writes stay ATOMIC (temp + replace via _atomic_write), so a crash
#     mid-write still leaves the previous complete draft, never a torn one.
#   * The trade, stated honestly: work die-between-edit-and-flush widens from
#     "since your last keystroke" to "since the worker last caught up" — a
#     few seconds on a bad share, for a net whose job is surviving a closed
#     tab. A KILLED PROCESS may drop the last pending write.
#   * Readers call async_flush() first (there is one draft reader); deleters
#     call async_cancel() before unlink, or a pending write would resurrect
#     the draft the planner just discarded.
_ASYNC_CV = threading.Condition()
_ASYNC_PENDING: dict[str, str] = {}
_ASYNC_BUSY: str | None = None
_ASYNC_LAST: dict[str, tuple[float, str | None]] = {}   # path -> (secs, error)
_ASYNC_THREAD: threading.Thread | None = None


def _async_worker():
    global _ASYNC_BUSY
    while True:
        with _ASYNC_CV:
            while not _ASYNC_PENDING:
                _ASYNC_CV.wait()
            path, text = next(iter(_ASYNC_PENDING.items()))
            del _ASYNC_PENDING[path]
            _ASYNC_BUSY = path
        t0 = time.perf_counter()
        err = None
        try:
            _atomic_write(Path(path), text, _count=False)
        except OSError as e:
            err = str(e)
        with _ASYNC_CV:
            _ASYNC_LAST[path] = (time.perf_counter() - t0, err)
            _ASYNC_BUSY = None
            _ASYNC_CV.notify_all()


def async_write(path: Path, text: str) -> None:
    """Queue an atomic write and return immediately. Latest wins per path."""
    global _ASYNC_THREAD
    with _ASYNC_CV:
        if _ASYNC_THREAD is None or not _ASYNC_THREAD.is_alive():
            _ASYNC_THREAD = threading.Thread(
                target=_async_worker, name="cp-draft-writer", daemon=True)
            _ASYNC_THREAD.start()
        _ASYNC_PENDING[str(path)] = text
        _ASYNC_CV.notify_all()


def async_cancel(path: Path) -> None:
    """Drop any pending write for this path (call BEFORE deleting the file)."""
    with _ASYNC_CV:
        _ASYNC_PENDING.pop(str(path), None)


def async_flush(timeout: float = 5.0) -> bool:
    """Wait until nothing is pending or in flight. True if it drained."""
    deadline = time.monotonic() + timeout
    with _ASYNC_CV:
        while _ASYNC_PENDING or _ASYNC_BUSY is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            _ASYNC_CV.wait(left)
    return True


def async_last_write(path: Path) -> tuple[float, str | None] | None:
    """(seconds, error) of the last completed background write, for the
    timing line. None while nothing has completed yet."""
    with _ASYNC_CV:
        if str(path) in _ASYNC_PENDING or _ASYNC_BUSY == str(path):
            return None                      # still in flight — say "writing…"
        return _ASYNC_LAST.get(str(path))


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

# A bare `active.json` is READ as 2026's pointer (above) but no longer WRITTEN:
# the dual-write shim that kept it fresh for sessions left open across the
# 2026-07-29 update was removed 2026-08-13, several releases later, per its own
# removal note. A stale `active.json` left on a share is harmless — the
# year-scoped pointer exists there and wins — and deliberately not deleted:
# publish should never remove files it did not write.


# ---------------------------------------------------------------- active pointer
def active_path(d, year) -> Path:
    return Path(d) / f"active-{int(year)}.json"


def legacy_active_path(d) -> Path:
    return Path(d) / "active.json"


def read_active(d, year):
    def _load():
        j = _read_json_pointer(active_path(d, year))
        if j is None and int(year) == LEGACY_YEAR:
            j = _read_json_pointer(legacy_active_path(d))   # pre-per-year share
        return j
    return _memo(("active", str(d), int(year)), _load)


def write_active(d, year, meta: dict):
    _atomic_write(active_path(_dir(d), int(year)), json.dumps(meta))


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
    return _memo(("budget", str(d), int(year)),
                 lambda: _read_json_pointer(budget_path(d, year)))


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
        _invalidate(p)
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

    The lock was never dual-written (unlike the pointer, briefly, until
    2026-08-13): a lock is transient and two files would need an atomic O_EXCL
    race across both. Once anyone acquires through this code the year-scoped
    file exists and wins from then on; the stranded legacy lock ages out at
    LOCK_STALE_MIN and stops mattering."""
    def _resolve():
        p = lock_path(d, year)
        if not p.exists() and int(year) == LEGACY_YEAR:
            legacy = legacy_lock_path(d)
            if legacy.exists():
                return legacy
        return p
    # The exists() probes above are share round trips too — throttled on the
    # same liveness window. _invalidate clears this cache wholesale, so one's
    # own acquire/release re-resolves immediately.
    def _cached():
        key = (str(d), int(year))
        now = time.monotonic()
        hit = _RESOLVE_CACHE.get(key)
        if hit and now - hit[0] < LIVENESS_MIN_SEC:
            return hit[1]
        t0 = time.perf_counter()
        p = _resolve()
        IO_PERF["s"] += time.perf_counter() - t0
        IO_PERF["n"] += 1
        _RESOLVE_CACHE[key] = (now, p)
        return p
    return _memo(("lockpath", str(d), int(year)), _cached)


def read_lock(d, year):
    return _memo(("lock", str(d), int(year)),
                 lambda: _read_json_pointer(_live_lock_path(d, year)))


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
        _invalidate(p)             # the rename changed what _live_lock_path resolves to
        cur = read_lock(d, year)   # a valid lock may have appeared meanwhile
    if cur is None:
        try:  # atomic create — wins the race against another new acquirer
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(_lock_record(user), f)
            # This path writes DIRECTLY (the O_EXCL create is what makes the
            # race safe), so it bypasses _atomic_write's invalidation — clear
            # by hand or the read below serves the memoized "no lock" and the
            # caller gets no token, i.e. stays read-only after taking control.
            _invalidate(p)
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
        # Ownership is re-checked EVERY rerun — that is how a takeover is
        # detected — but the WRITE is throttled. A lock goes stale at
        # LOCK_STALE_MIN (5 min), so refreshing more often than
        # HEARTBEAT_MIN_SEC bought nothing and cost a share write on every
        # rerun, i.e. every keystroke in a grid (measured 2026-08-10).
        if age_min(cur.get("heartbeat") or cur.get("acquired_at") or "") * 60 \
                < HEARTBEAT_MIN_SEC:
            return True
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
        p = _live_lock_path(d, year)
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        _invalidate(p)
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


# Listing snapshots must NOT read their plan payloads (perf, 2026-08-03).
#
# Measured on a 30-version folder: the app pulled ~100 files and 3.2 MB off the
# share on EVERY RERUN — and in Streamlit a rerun is every keystroke in a grid,
# every widget touch, every page change. The Budget page cost 189 reads / 6.5 MB.
# All of it to render "v3 · name · author · date" in a list, because
# `_all_snapshots` parsed each version's ENTIRE plan just to reach its header.
# On a slow network share that is the difference between usable and not
# (user: "the network share is super slow ... cumbersome to navigate even
# through just pages").
#
# So listings carry METADATA ONLY, cached per file. A published `vNNNN` is
# immutable, but the cache is keyed on (path, mtime, size) anyway so a pointer
# file — which does change — can share the same path, and a hand-edited folder
# cannot serve a stale header. Callers that genuinely need the plan (Sandbox,
# Restore, scenario compare) call `load_snapshot(d, meta["_file"])`, which is
# the one place the payload is paid for.
#
# Negative results are cached too: `active-*.json` and `budget-*.json` match the
# glob, carry no "lobs", and were being re-read and discarded every rerun.
_SNAP_META_KEYS = ("version", "name", "author", "published_at", "saved_at",
                   "parent_version", "note", "kind", "plan_year", "n_weeks",
                   "members_start", "members_end")
_SNAP_CACHE: dict[tuple, dict | None] = {}


def _snapshot_meta(p: Path, sig=None) -> dict | None:
    """One snapshot's listing header, WITHOUT its plan payload. None when the
    file is not a snapshot (a pointer, a stray json) or cannot be read.

    `sig` is the caller's already-obtained (mtime_ns, size) — `_all_snapshots`
    gets it free from the shared directory scan, and re-statting here would
    put back exactly the per-file round trip that scan exists to avoid."""
    if sig is None:
        try:
            stt = p.stat()
            sig = (stt.st_mtime_ns, stt.st_size)
        except OSError:
            return None
    key = (str(p), sig[0], sig[1])
    if key in _SNAP_CACHE:
        return _SNAP_CACHE[key]
    j = _read_json(p)
    meta = None
    if isinstance(j, dict) and "lobs" in j:
        meta = {k: j[k] for k in _SNAP_META_KEYS if k in j}
        meta["_file"] = p.name
    if len(_SNAP_CACHE) > 4000:        # a share that large is already a problem
        _SNAP_CACHE.clear()            # — bound the dict rather than grow forever
    _SNAP_CACHE[key] = meta
    return meta


def list_names(d) -> list[str]:
    """Entry names in a shared directory, off the ONE shared `_dir_entries`
    scan (dir-mtime validated, memoized per run).

    `Path.glob` is the trap here: CPython 3.13 rewrote pathlib to walk with
    scandir, while 3.11 — the oldest version this app supports — stats every
    entry. Identical code measured 9 network round trips per edit on 3.14 and
    15 on 3.11, entirely from globs. Benchmark on the OLDEST supported
    interpreter, not the newest. Callers filter the returned names in Python,
    which costs nothing.
    """
    return [name for name, _m, _s, _f in _dir_entries(d)]


def _all_snapshots(d) -> list[dict]:
    return _memo(("snaps", str(d)), lambda: _all_snapshots_uncached(d))


def _all_snapshots_uncached(d) -> list[dict]:
    """Every snapshot's METADATA, newest-first ordering left to callers.

    Deliberately no `lobs` — see the note above. Anything needing the plan
    itself calls `load_snapshot` with the entry's `_file`.

    Enumerated with `os.scandir`, and that is the whole point on a network
    share: a DirEntry carries the size and mtime the cache key needs, taken
    from the directory listing ITSELF. `glob()` + `p.stat()` costs one round
    trip PER FILE; scandir costs one for the directory. Measured on the real
    deployment (user 2026-08-03: "every change takes like 15-20 seconds"): ONE
    roster edit was 274 filesystem round trips against a 40-version folder,
    258 of them stats. At the ~50-70 ms round trip a slow SMB share gives you,
    that is the 15-20 seconds — the reads were already cached; it was the
    METADATA calls that had not been.
    """
    out = []
    for name, mtime_ns, size, is_file in sorted(_dir_entries(d)):
        if name == "active.json" or not name.endswith(".json") or not is_file:
            continue
        meta = _snapshot_meta(Path(d) / name, (mtime_ns, size))
        if meta is not None:
            out.append(meta)
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
