# Team Setup — running the Capacity Planner from the share

The app is **not hosted**. Each planner runs it on their own PC by double-clicking
`Launch Capacity Planner.bat` on the share. The shared team state (`scenarios/`
versions, edit lock, remembered data paths) lives on the share, so everyone sees
the same published plan; the Python environment lives on each PC (fast), built
automatically by the launcher.

## One-time, per person

1. **Install Python (3.14 recommended; 3.12 or 3.13 also fine — NOT 3.11)** — Software Center if IT offers it, else python.org. (The pinned numpy no longer supports 3.11.)
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
- **Without internet** (no firewall exception needed by ANYONE): the launcher
  installs from the share's `wheels\` folder — fully offline. **This folder
  ships pre-populated** with Windows wheels for **Python 3.12–3.14**
  (~250 MB), so install any of those versions and it just works.
  **Recommended: 3.14** — it matches the development environment exactly;
  standardize the whole team on one version either way.
  To refresh it later (e.g. after requirements.txt changes), from any machine
  with internet — Windows or Mac — run per Python version in use:

      python3 -m pip download -r requirements.txt -d wheels --platform win_amd64 --python-version 312 --only-binary=:all:

## Updates

- **App updates**: just edit the `.py` files on the share — everyone gets them
  on their next launch. Ask people to close/relaunch after an update.
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
