# Team Setup — running the Capacity Planner from the share

The app is **not hosted**. Each planner runs it on their own PC by double-clicking
`Launch Capacity Planner.bat` on the share. The shared team state (`scenarios/`
versions, edit locks, remembered data paths) lives on the share, so everyone sees
the same published plan; the Python environment lives on each PC (fast), built
automatically by the launcher.

**Each plan year is its own plan** — its own published versions and its own edit
lock — so someone can build next year while this year stays the operating plan.
Pick the year with **Plan year** at the top of the sidebar.

## One-time, per person

1. **Install Python — 3.11.4 or newer** (3.11, 3.12, 3.13 and 3.14 all work) —
   Software Center if IT offers it, else python.org. Whatever your IT has
   approved is fine; **3.11.4 is verified**, so there is no need to chase a
   newer version through an approval process.
   On the installer's first screen, **tick "Add python.exe to PATH"**, then use
   the default install (per-user — needs **no admin rights**, and it includes
   the `py` launcher our start script relies on). The PATH tick isn't required
   to run the app, but it makes troubleshooting commands work in a terminal
   instead of hitting "'python' is not recognized". Nothing else to configure —
   don't create environments or pip-install anything by hand.
2. **Double-click `Launch Capacity Planner.bat`** on the share. The first run
   builds a private environment under `%LOCALAPPDATA%\CapacityPlanner` and takes
   a few minutes; it opens the app in the browser when ready.

That's it. Every later launch is a double-click and a few seconds. A desktop
shortcut to the .bat is handy (right-click → Send to → Desktop).

## How package installs work (two modes, automatic)

- **With internet/proxy access to PyPI** (the firewall ticket): the launcher
  pip-installs straight from `requirements.txt`.
  **This is the mode our machines use** — they reach PyPI, so nothing else is
  needed and there is no `wheels\` folder on the share.
- **Without internet** — only if that ever changes: put a `wheels\` folder next
  to the app and the launcher installs from it instead. It is **not** part of
  the download; you build it yourself, from any machine with internet:

      python3 -m pip download -r requirements.txt -d wheels --platform win_amd64 --python-version 311 --only-binary=:all:

  Change `311` to match the Python the team installed, and re-run it whenever
  `requirements.txt` changes. If it can't find a wheel, the pinned version has
  no build for that Python — adjust the pin, never install a Python IT hasn't
  approved.

  ⚠️ **A `wheels\` folder that exists but is missing your Python's wheels breaks
  the launch even on a machine with internet**: the offline branch runs pip with
  `--no-index`, so it cannot reach PyPI to fill the gap. Either keep the folder
  complete for the version everyone runs, or don't have one. Standardize the
  team on ONE Python version — a mixed team means the folder must carry every
  version in use.

## How it actually runs (for the questions IT will ask)

- **Nothing is hosted.** Each person's launcher starts a web server **on their
  own PC**, and their browser talks to that. Streamlit brings the server with it
  (Starlette + uvicorn, pulled in automatically by `pip install streamlit`) —
  there is nothing to install, configure, or administer.
- **It listens on `127.0.0.1` only** (set in `.streamlit/config.toml`). That is
  the loopback address: reachable from that machine and nowhere else. No other
  PC can connect to it, and it needs no firewall exception. Streamlit's own
  default is every interface, which is why this is pinned.
- **Planners never talk to each other over the network.** Collaboration happens
  entirely through files in `scenarios/` on the share — the published versions,
  the per-year edit lock, the drafts. There is no server-to-server anything.
- **A `__pycache__` folder appearing on the share is normal and safe.** Python
  caches compiled bytecode next to the modules it imports. Several people doing
  that at once is fine: CPython writes each file to a uniquely-named temporary
  and then renames it atomically, so a reader never sees a half-written file and
  concurrent writers produce identical content. If the share is read-only for
  some people, Python silently skips caching rather than failing. Leave it
  alone — deleting it or setting `PYTHONDONTWRITEBYTECODE` just makes every
  launch recompile those files over the network.

## Updates

- **App updates**: just edit the `.py` files on the share — everyone gets them
  on their next launch. **Ask people to close and relaunch after an update**, and
  wait for that before relying on the new version.

  This matters more than it sounds. There is ONE copy of the app on the share, so
  an update reaches everyone at once — except a session someone left OPEN. That
  session does not simply keep running the old version, which would at least be
  consistent. Streamlit re-reads the MAIN script from disk on every interaction,
  while the modules beside it (`collab.py`, `sources.py`, `brand.py`) are loaded
  once when the app starts and stay in memory until relaunch. So an open session
  ends up running the NEW main script against the OLD supporting modules — a
  combination that has never existed anywhere and has never been tested. Changes
  routinely span both at once.

  That is the only way two versions of the app can ever be reading the same
  `scenarios/` folder, and it is why the update instruction is about
  correctness, not tidiness. Per-PC copies of the app folder have the same
  effect permanently: four planners, four different apps, four private plans.

  **The tell**: if the console says *"An update to the [server] config option
  section was detected... please restart"*, the update landed while your app was
  running. Nothing is broken and the message itself is harmless — the config
  file is usually unchanged and only its timestamp moved — but it means you are
  in the mixed state above. Close and relaunch before trusting anything.

  **To close it properly**: shut the browser tab AND the black console window
  (or press Ctrl+C in it). Closing only the tab leaves the app running.
- **Package updates**: change `requirements.txt` (and refresh `wheels\` if used).
  The launcher detects the change and re-syncs each person's environment
  automatically on next launch.

## Troubleshooting

- **"Python is not installed"** — do step 1; the `py` launcher comes with the
  python.org installer by default.
- **First launch is slow** — normal (one-time package install + antivirus
  scanning new files). Later launches are seconds.
- **Port already in use** — someone (or a stuck process) is already running it
  on this PC. Close the other window, or launch with another port:
  `...python.exe -m streamlit run capacity_planner.py --server.port 8502`.
- **"An application control policy has blocked this file" (streamlit.exe)** —
  corporate AppLocker/WDAC blocking the unsigned `streamlit.exe` shim that pip
  creates. You never need that file: **always start the app with the launcher
  .bat**, which runs Streamlit as a Python module inside the signed (allowed)
  python.exe. Never type `streamlit run ...` in a terminal at work; the manual
  equivalent, if you ever need it, is:
  `%LOCALAPPDATA%\CapacityPlanner\venv\Scripts\python.exe -m streamlit run capacity_planner.py`
- **Corporate proxy blocks pip** — either use the `wheels\` offline mode, or
  set the proxy once: `pip config set global.proxy http://proxy:port`.
- **"You're read-only" in the app** — someone else holds the edit lock
  (sidebar shows who). That's the collaboration model, not a bug: take over,
  wait, or use Sandbox.
