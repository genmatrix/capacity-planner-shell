"""WFM house brand for the Capacity Planner app.

Ports the visual identity from Recommendation_Format_Style_Guide.md into
Streamlit: deep-navy canvas, glassy panels, cyan→blue through-line, pills,
stat cards, and a branded wordmark header. Also provides the matching Altair
chart theme so health graphs read as part of the same system.

Everything is inline CSS / HTML (no external assets, no internet dependency)
— same rule as the brief format.
"""
import json
import os
from pathlib import Path

import altair as alt
import streamlit as st

# ---------------------------------------------------------------- identity
# The wordmark is a LOCAL setting, not part of the code.
#
# Why it works this way: the work copy has to carry the real organisation's
# name, and nothing carrying that name may ever reach the public shell. So the
# name lives in a file that is NOT in the distribution — not in publish
# ALLOWLIST, not in git — and the code ships only these generic defaults. Three
# consequences worth knowing:
#
#   * Re-extracting a shell ZIP over the app folder does NOT clobber it. A ZIP
#     adds and overwrites what it contains; branding.json is not in it, so a
#     work-side update keeps the organisation's branding without re-entering.
#   * It sits beside the app, so on the share every planner sees the same
#     wordmark — same pattern as data_paths.json and holidays.json.
#   * DEFAULT_MARK and DEFAULT_WORD must stay STRING LITERALS. The publish
#     pipeline rewrites them by exact text match ("WFM" -> "WFM"), and the
#     banned-term scan rejects the untransformed value — so computing them, or
#     moving them into the JSON file as the source of truth, would silently
#     take the scrubbing off the public build.
IDENTITY_FILE = Path(__file__).resolve().parent / "branding.json"
DEFAULT_MARK = "WFM"
DEFAULT_WORD = "WFM"
DEFAULT_SUB = "Workforce Management"


def identity() -> dict:
    """{mark, word, sub} — the local override if there is one, else the
    shipped defaults. Never raises: a missing, unreadable or half-written file
    just means defaults, because a branding file is not worth a broken app."""
    data = {}
    try:
        data = json.loads(IDENTITY_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        pass

    def pick(key, default, limit):
        v = data.get(key)
        v = v.strip() if isinstance(v, str) else ""
        return (v or default)[:limit]

    return {"mark": pick("mark", DEFAULT_MARK, 4),
            "word": pick("word", DEFAULT_WORD, 40),
            "sub": pick("sub", DEFAULT_SUB, 60)}


def save_identity(mark: str, word: str, sub: str) -> None:
    """Write the override beside the app, atomically (temp + replace) so a
    planner never reads a half-written file off the share."""
    payload = {"mark": (mark or "").strip()[:4],
               "word": (word or "").strip()[:40],
               "sub": (sub or "").strip()[:60]}
    tmp = IDENTITY_FILE.with_suffix(f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, IDENTITY_FILE)


def reset_identity() -> None:
    """Back to the shipped defaults — the state the public shell must be in."""
    try:
        IDENTITY_FILE.unlink()
    except OSError:
        pass

# ---------------------------------------------------------------- palette
# "Member Hall" — the light palette, taken from the org's live public-site
# stylesheet rather than from a screenshot, so each value keeps the job it does
# there: RED is the masthead and nothing else, TEAL is every link and action,
# deep navy is body copy, warm grays are the ground.
#
# (Deliberately no vendor/company name in this file — it ships to the PUBLIC
# shell repo, whose publish gate rejects identifying terms. See the note in
# scripts/publish_github.py.)
#
# Two values look like typos and are not:
#   * SUPPLY is #0089a0, not the site's own #007c89. The site value sits just
#     under the chroma floor and reads gray as a chart fill; TEAL keeps the true
#     site value for UI chrome, where it is text and borders, not a data mark.
#   * COVERED/TIGHT/SHORT are RESERVED status colors. Never reuse one as a
#     series color — CATEGORICAL below deliberately avoids their hues.
BG = "#f4f5f5"            # page ground
SURFACE = "#ffffff"       # cards — solid; glass belongs to a dark ground
BORDER = "#d5d7d9"
BORDER_SOFT = "#e6e8e9"
TEXT = "#192838"          # headings / primary ink
BODY = "#4d5f69"          # secondary ink
MUTED = "#5f6d76"         # captions, axis labels. Darker than the source
                          # site's caption gray, which lands at 3.4:1 on this
                          # ground — too low for 11px labels. This clears 4.5:1
                          # on both the white card and the page gray.

RED = "#d12a2e"           # masthead ONLY — never a data color
TEAL = "#007c89"          # links, actions, the through-line
TEAL_DK = "#00636e"

# data marks
DEMAND = "#3b4fc8"        # Required FTE
SUPPLY = "#0089a0"        # Staffed FTE

# reserved status colors
COVERED = "#0f7f55"
TIGHT = "#b5730f"
SHORT = "#ab111a"
NEUTRAL = "#e9ebec"       # diverging-scale midpoint

# tints — pill and chip backgrounds on white
COVERED_BG = "#e8f1ed"
TIGHT_BG = "#f8f1e2"
SHORT_BG = "#f9e9ea"
TEAL_BG = "#e4f0f2"

# Series hues that are NOT status colors — named so the categorical range and
# the LOB accents share one definition instead of repeating hex literals.
RUST = "#b44f00"
INDIGO = "#4158bd"
PLUM = "#a72e5a"

# Scenario compare shows at most 4 series. Validated against the white card
# surface: worst adjacent CVD ΔE 16.6, normal-vision ΔE 23.2, all ≥ 3:1.
# Six well-separated hues are not achievable while also reserving red, amber
# and green — which is why this is four, not six.
CATEGORICAL = [SUPPLY, RUST, INDIGO, PLUM]

# Legacy names, kept as aliases so the existing call sites in
# capacity_planner.py keep working unchanged. Prefer the semantic names above
# in new code; these can be retired in a follow-up pass.
CYAN = SUPPLY
VIOLET = DEMAND
GREEN = COVERED
AMBER = TIGHT
AMBER_LT = "#8f5a0c"      # darker than AMBER: this one lands on white as text
PINK = SHORT

# Per-item accent gradients — assigned to LOBs cyclically.
ACCENTS = [
    (TEAL, DEMAND),
    (DEMAND, INDIGO),
    (COVERED, SUPPLY),
    (TIGHT, RUST),
]


def accent_for(i: int) -> tuple[str, str]:
    return ACCENTS[i % len(ACCENTS)]


# ---------------------------------------------------------------- CSS
_CSS = f"""
<style>
/* Content gutter. The masthead band bleeds to the edges of the content column
   by cancelling this with a negative margin, so the two must stay in step —
   that is why it is a custom property rather than two hard-coded numbers.

   --cc-topclear is the space reserved above the masthead. Streamlit's toolbar
   is `position: fixed`, so it reserves NO space of its own: any padding-top
   smaller than the toolbar puts content underneath it, which is what clipped
   the masthead's top half on 2026-07-28. With the toolbar collapsed to zero
   height (below) nothing needs clearing and this is 0 — but it stays a named
   property because the moment the toolbar is restored, this is the number that
   has to come back with it. */
:root {{ --cc-gutter: 2.2rem; --cc-topclear: 0rem; }}

/* ------- typography ------- */
html, body, .stApp, [class*="css"] {{
  font-family: "Avenir Next", Avenir, Inter, ui-sans-serif, system-ui,
               -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}}
h1 {{ letter-spacing: -0.035em; color: {TEXT}; }}
h2, h3 {{ letter-spacing: -0.022em; color: {TEXT}; }}
.stApp {{ background: {BG}; }}

/* ------- metric cards (native st.metric, app-wide) ------- */
[data-testid="stMetric"] {{
  background: {SURFACE}; border: 1px solid {BORDER};
  border-radius: 10px; padding: 14px 18px;
  box-shadow: 0 1px 2px rgba(25,40,56,.05);
}}
[data-testid="stMetricLabel"] {{
  text-transform: uppercase; letter-spacing: .08em;
  font-size: 11px !important; color: {MUTED} !important;
}}
[data-testid="stMetricLabel"] p {{ white-space: normal; overflow: visible; }}
[data-testid="stMetricValue"] {{
  font-weight: 700; letter-spacing: -0.02em; color: {TEXT};
  font-size: clamp(20px, 2.4vw, 34px) !important;
  font-variant-numeric: tabular-nums;
}}

/* ------- sidebar: white, separated by a rule not a shadow ------- */
[data-testid="stSidebar"] {{
  background: {SURFACE};
  border-right: 1px solid {BORDER};
}}

/* ------- expanders / editors pick up the surface ------- */
[data-testid="stExpander"] details {{
  background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px;
}}

/* ------- tabular figures wherever numbers line up ------- */
[data-testid="stDataFrame"], [data-testid="stTable"] {{
  font-variant-numeric: tabular-nums;
}}

/* ------- top navigation bar -------
   Styles the horizontal nav radio (st.container(key="ccnav")) to read as site
   nav. Degrades gracefully: if a selector stops matching after a Streamlit
   upgrade you get a plain horizontal radio in a white bar, never a broken
   layout. The widget stays a radio on purpose — see the note in
   capacity_planner.py where it is built. */
.st-key-ccnav {{
  background: {SURFACE}; border: 1px solid {BORDER};
  border-radius: 10px; padding: 4px 8px; margin-bottom: 18px;
  box-shadow: 0 1px 2px rgba(25,40,56,.05);
}}
.st-key-ccnav [role="radiogroup"] {{ gap: 2px; flex-wrap: wrap; }}
.st-key-ccnav [role="radiogroup"] label > div:first-child {{ display: none; }}
.st-key-ccnav [role="radiogroup"] label {{
  padding: 7px 14px; margin: 0; border-radius: 7px 7px 0 0;
  border-bottom: 2px solid transparent; transition: background .12s ease;
}}
.st-key-ccnav [role="radiogroup"] label p {{
  font-size: 13.5px; font-weight: 600; color: {BODY}; margin: 0;
}}
.st-key-ccnav [role="radiogroup"] label:hover {{ background: {BG}; }}
.st-key-ccnav [role="radiogroup"] label:has(input:checked) {{
  background: {TEAL_BG}; border-bottom-color: {TEAL};
}}
.st-key-ccnav [role="radiogroup"] label:has(input:checked) p {{
  color: {TEAL_DK};
}}
.st-key-ccnav [role="radiogroup"] label:focus-within {{
  outline: 2px solid {TEAL}; outline-offset: -2px;
}}

/* ------- WFM components (emitted by brand.py helpers) ------- */
/* The masthead is a BAND, not a floating wordmark: white ground, the brand
   rule along its top edge, a hairline along the bottom, bleeding to the edges
   of the content column. The red used to be a fixed hairline pinned to the
   viewport, which left it stranded above the Streamlit toolbar with the
   wordmark floating far below it. Carrying it on the band puts the rule back
   where it belongs and still spends brand red exactly once — it is chrome, and
   a red cell in this app always means understaffed. */
.cc-header {{
  display: flex; align-items: center; justify-content: space-between;
  background: {SURFACE};
  border-top: 4px solid {RED};
  border-bottom: 1px solid {BORDER};
  margin: 0 calc(-1 * var(--cc-gutter)) 16px;
  /* The extra LEFT padding is the lane the re-homed sidebar chevron sits in
     (see the toolbar block above). Left, not right, because the control that
     collapses the sidebar lives inside stSidebarHeader — on the left — so an
     expand button anywhere else makes the toggle jump across the screen
     between clicks. Reserved unconditionally: the chevron only exists while
     the sidebar is collapsed, and a band whose wordmark shifts sideways when
     you collapse the rail is worse than 34px of white space. */
  padding: 12px var(--cc-gutter) 11px calc(var(--cc-gutter) + 34px);
  position: relative; z-index: 1;
}}
.cc-brand {{ display: flex; align-items: center; gap: 12px; }}
.cc-tile {{
  width: 42px; height: 42px; border-radius: 8px;
  background: {RED};
  display: flex; align-items: center; justify-content: center;
  color: #fff; font-weight: 800; font-size: 16px; letter-spacing: -0.02em;
}}
.cc-word {{ color: {TEXT}; font-weight: 700; font-size: 19px; line-height: 1.05; }}
.cc-sub {{
  color: {MUTED}; font-size: 10.5px; text-transform: uppercase;
  letter-spacing: .1em; margin-top: 2px;
}}
.cc-meta {{ text-align: right; color: {MUTED}; font-size: 12px; }}
.cc-meta b {{ color: {BODY}; display: block; font-size: 12.5px; }}

.cc-hero {{
  background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 10px;
  padding: 26px 30px;
  box-shadow: 0 1px 2px rgba(25,40,56,.05), 0 6px 20px rgba(25,40,56,.05);
  display: flex; justify-content: space-between; gap: 26px; align-items: center;
  position: relative; z-index: 1; margin-bottom: 16px;
}}
.cc-hero h1 {{
  font-size: clamp(26px, 3.6vw, 40px); line-height: 1.0;
  letter-spacing: -0.04em; color: {TEXT}; margin: 10px 0 10px 0;
}}
.cc-hero p {{ color: {BODY}; font-size: 14.5px; line-height: 1.6; margin: 0; max-width: 62ch; }}
.cc-box {{
  border: 1px solid {TEAL}; background: {TEAL_BG};
  border-radius: 10px; padding: 16px 26px; text-align: center; min-width: 170px;
}}
.cc-box .lbl {{
  color: {TEAL_DK}; font-size: 11px; text-transform: uppercase; letter-spacing: .08em;
  font-weight: 700;
}}
.cc-box .val {{
  color: {TEXT}; font-size: 40px; font-weight: 700; letter-spacing: -0.03em;
  line-height: 1.1; font-variant-numeric: tabular-nums;
}}
.cc-box .unit {{ color: {MUTED}; font-size: 12px; }}
.cc-box.bad  {{ border-color: {SHORT};   background: {SHORT_BG}; }}
.cc-box.bad .lbl  {{ color: {SHORT}; }}
.cc-box.bad .val  {{ color: {SHORT}; }}
.cc-box.good {{ border-color: {COVERED}; background: {COVERED_BG}; }}
.cc-box.good .lbl {{ color: {COVERED}; }}

.cc-pill {{
  display: inline-block; border-radius: 999px; padding: 3px 12px;
  font-size: 11.5px; font-weight: 600; letter-spacing: .02em;
  border: 1px solid {BORDER}; color: {BODY}; background: {BG};
  margin-right: 6px;
}}
.cc-pill.blue  {{ color: {TEAL_DK}; border-color: {TEAL};    background: {TEAL_BG}; }}
.cc-pill.green {{ color: {COVERED}; border-color: {COVERED}; background: {COVERED_BG}; }}
.cc-pill.amber {{ color: {AMBER_LT}; border-color: {TIGHT};  background: {TIGHT_BG}; }}
.cc-pill.pink  {{ color: {SHORT};   border-color: {SHORT};   background: {SHORT_BG}; }}

.cc-card {{
  background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 10px;
  padding: 0 0 14px 0; overflow: hidden;
  box-shadow: 0 1px 2px rgba(25,40,56,.05); position: relative; z-index: 1;
  margin-bottom: 12px;
}}
.cc-card .bar {{ height: 4px; }}
.cc-card .inner {{ padding: 14px 18px 0 18px; }}
.cc-card .ttl {{ color: {TEXT}; font-weight: 700; font-size: 15.5px; }}
.cc-card .sub {{ color: {MUTED}; font-size: 12px; margin-bottom: 8px; }}
.cc-card .kv {{ color: {BODY}; font-size: 13px; line-height: 1.65; }}
.cc-card .kv b {{ color: {TEXT}; font-variant-numeric: tabular-nums; }}

.cc-stats {{
  display: flex; gap: 12px; margin: 0 0 14px 0; position: relative; z-index: 1;
}}
.cc-stat {{
  flex: 1; background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 10px;
  padding: 14px 18px 10px 18px;
  box-shadow: 0 1px 2px rgba(25,40,56,.05); min-width: 0;
}}
.cc-stat .lbl {{
  color: {MUTED}; font-size: 11px; text-transform: uppercase; letter-spacing: .08em;
}}
.cc-stat .valrow {{ display: flex; align-items: baseline; gap: 8px; margin-top: 2px; }}
.cc-stat .val {{
  color: {TEXT}; font-weight: 700; letter-spacing: -0.02em;
  font-size: clamp(20px, 2.2vw, 30px); font-variant-numeric: tabular-nums;
}}
.cc-stat .delta {{
  font-size: 11.5px; font-weight: 700; border-radius: 999px; padding: 2px 8px;
  border: 1px solid {BORDER}; color: {BODY}; background: {BG};
  white-space: nowrap; font-variant-numeric: tabular-nums;
}}
.cc-stat .delta.good {{ color: {COVERED}; border-color: {COVERED}; background: {COVERED_BG}; }}
.cc-stat .delta.bad  {{ color: {SHORT};   border-color: {SHORT};   background: {SHORT_BG}; }}
.cc-stat .sub {{
  color: {MUTED}; font-size: 11px; line-height: 1.35; margin-top: 3px;
  font-variant-numeric: tabular-nums;
}}
.cc-stat .sub.warn {{ color: {AMBER_LT}; font-weight: 600; }}
.cc-stat .spark {{ margin-top: 6px; line-height: 0; }}
/* Five tiles across is tight; let the value shrink rather than overflow. */
.cc-stats.five .cc-stat .val {{ font-size: clamp(18px, 1.8vw, 26px); }}

.cc-band {{
  background: {TEAL_BG};
  border: 1px solid {BORDER_SOFT}; border-left: 3px solid {TEAL};
  border-radius: 0 8px 8px 0;
  padding: 14px 20px; color: {BODY}; font-size: 13.5px; line-height: 1.6;
  position: relative; z-index: 1; margin-bottom: 14px;
}}
.cc-band b {{ color: {TEXT}; }}

/* ------- density -------
   Streamlit's defaults are laid out for demos; a planner works down a rail of
   ~20 controls and across 52 weeks, so vertical space is the scarce resource.
   These pull label and gap sizes to the proportions the R build used. All of
   it is cosmetic: if a selector stops matching after a Streamlit upgrade the
   app is merely roomier again, never broken. */
.block-container, [data-testid="stMainBlockContainer"] {{
  padding-top: var(--cc-topclear) !important;
  padding-left: var(--cc-gutter); padding-right: var(--cc-gutter);
  padding-bottom: 3rem;
}}
/* ------- the Streamlit toolbar -------
   Collapsed to nothing so the masthead is the first thing on the page.

   The usual recipe for this is `[data-testid="stHeader"] {{ display: none }}`.
   DO NOT do that here. The chevron that reopens a COLLAPSED SIDEBAR lives
   inside it:

       stHeader > stToolbar > [ stExpandSidebarButton, stStatusWidget,
                                stAppDeployButton, stMainMenu ]

   Hiding the parent takes the chevron with it, and a planner who collapses the
   rail then has no way back short of reloading the page. So the header is
   zero-height and click-through, its disposable contents are hidden
   individually, and the chevron is re-homed into the masthead's right edge —
   where .cc-header reserves a lane for it. It only renders while the sidebar
   is collapsed; the lane is empty the rest of the time.

   If a Streamlit upgrade renames these test ids the failure is visible and
   safe: the toolbar reappears at full height, overlapping the masthead until
   --cc-topclear is set back to ~4.5rem.

   It is re-homed LEFT rather than right because its counterpart — the control
   that collapses the sidebar — is inside stSidebarHeader, on the left. Putting
   expand on the right made the toggle jump from one side of the screen to the
   other between clicks. Both now sit at the left edge, next to the rail they
   act on. `position: fixed` is measured from the viewport, which lines up with
   the content column only because the app is layout="wide"; a centered layout
   would need this anchored differently. */
[data-testid="stHeader"] {{
  background: transparent; height: 0; min-height: 0; pointer-events: none;
}}
[data-testid="stToolbar"] {{ padding: 0; pointer-events: none; }}
[data-testid="stStatusWidget"],
[data-testid="stAppDeployButton"],
[data-testid="stMainMenu"] {{ display: none; }}
[data-testid="stExpandSidebarButton"] {{
  pointer-events: auto; position: fixed; z-index: 1000;
  top: 12px; left: calc(var(--cc-gutter) - 8px); right: auto;
}}
[data-testid="stSidebarUserContent"] {{ padding-top: 1.2rem; }}
[data-testid="stVerticalBlock"] {{ gap: 0.7rem; }}
[data-testid="stSidebar"] [data-testid="stVerticalBlock"] {{ gap: 0.45rem; }}
[data-testid="stWidgetLabel"] p {{
  font-size: 12px !important; font-weight: 600 !important; color: {BODY} !important;
  margin-bottom: 2px !important;
}}
[data-testid="stCaptionContainer"] p {{ font-size: 12px; color: {MUTED}; }}
[data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3 {{ font-size: 15px; }}

/* ------- actions are TEAL -------
   The house rule is "teal is every link and action", but nothing here styled a
   BUTTON, so every action affordance rendered in Streamlit's native gray while
   the rest of the page carried the palette. Panels that are mostly buttons and
   captions (the week-of-month seasonality profile) therefore read as colorless
   next to a carded, teal-linked page. config.toml's primaryColor only reaches
   Streamlit's PRIMARY kind, and the app deliberately uses none — all 20 buttons
   are secondary.

   Selector is the PREFIX form: the testid is composed as
   `stBaseButton-${{kind}}`, so this covers secondary, primary and form-submit
   without naming each kind or breaking when a kind is added.

   Outlined, not filled: 20 teal blocks would shout, and a filled button beside
   the red masthead rule starts competing for "the important thing here". Filled
   is reserved for the primary kind, if a genuinely primary action ever wants it.
   Disabled must stay obviously dead — read-only mode depends on that reading. */
[data-testid^="stBaseButton-"] {{
  border: 1px solid {TEAL}; border-radius: 8px; color: {TEAL};
  background: {SURFACE}; font-weight: 600;
}}
[data-testid^="stBaseButton-"] p {{ color: inherit !important; font-weight: 600; }}
[data-testid^="stBaseButton-"]:hover:not(:disabled) {{
  background: {TEAL_BG}; border-color: {TEAL}; color: {TEAL};
}}
[data-testid^="stBaseButton-"]:focus-visible {{
  outline: 3px solid {TEAL_BG}; outline-offset: 1px;
}}
[data-testid^="stBaseButton-"]:disabled,
[data-testid^="stBaseButton-"]:disabled p {{
  border-color: {BORDER}; color: {MUTED}; background: {SURFACE};
}}
[data-testid="stBaseButton-primary"] {{ background: {TEAL}; color: {SURFACE}; }}
[data-testid="stBaseButton-primary"]:hover:not(:disabled) {{
  background: #00646e; border-color: #00646e; color: {SURFACE};
}}

/* ------- section cards -------
   st.container(key="ccsec_...") becomes a bordered card, so a grid and the
   note that explains it read as one object instead of loose page furniture.
   Same .st-key-* hook the nav bar uses. */
[class*="st-key-ccsec_"] {{
  background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 10px;
  padding: 14px 18px 8px; margin-bottom: 12px;
  box-shadow: 0 1px 2px rgba(25,40,56,.05);
}}
.cc-sec-ttl {{
  color: {MUTED}; font-size: 10.5px; font-weight: 700;
  text-transform: uppercase; letter-spacing: .12em; margin-bottom: 2px;
}}
.cc-sec-note {{ color: {BODY}; font-size: 12.5px; line-height: 1.55; margin-bottom: 10px; }}

/* ------- the compact plan table -------
   Weeks across, metrics down, at a density you can actually scan a year in.
   The Week column and the header row both stick, because a number 40 columns
   from its label is not information. Scrolls inside its own box so the page
   body never scrolls sideways. */
.cc-tablewrap {{
  overflow: auto; max-height: 560px;
  border: 1px solid {BORDER}; border-radius: 10px; background: {SURFACE};
}}
table.cc-table {{
  border-collapse: separate; border-spacing: 0; width: max-content; min-width: 100%;
  font-size: 12px; font-variant-numeric: tabular-nums;
}}
table.cc-table th {{
  position: sticky; top: 0; z-index: 2; background: {SURFACE};
  text-align: right; font-size: 9.5px; letter-spacing: .08em; text-transform: uppercase;
  color: {MUTED}; font-weight: 700; padding: 9px 10px 7px;
  border-bottom: 1px solid {BORDER}; white-space: nowrap;
}}
table.cc-table th:first-child, table.cc-table td:first-child {{
  position: sticky; left: 0; text-align: left; white-space: nowrap;
  background: {SURFACE}; font-weight: 600; color: {TEXT};
  box-shadow: 1px 0 0 {BORDER_SOFT};
}}
table.cc-table th:first-child {{ z-index: 3; }}
table.cc-table td {{
  padding: 4px 10px; text-align: right; white-space: nowrap; color: {BODY};
  border-bottom: 1px solid {BORDER_SOFT};
}}
table.cc-table tr:last-child td {{ border-bottom: none; }}
table.cc-table tr:hover td {{ background: {BG}; }}
table.cc-table td.neg {{ color: {SHORT}; background: {SHORT_BG}; font-weight: 700; }}
table.cc-table td.pos {{ color: {COVERED}; background: {COVERED_BG}; }}
table.cc-table td.tone-neg {{ color: {SHORT}; font-weight: 600; }}
table.cc-table td.tone-pos {{ color: {COVERED}; font-weight: 600; }}
table.cc-table tr.rule td {{ border-top: 1px solid {BORDER}; }}
</style>
"""


def inject():
    st.markdown(_CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------- components
def header(doc_type: str, meta: str):
    ident = identity()
    # The tile is a fixed square, so the mark has to shrink to fit rather than
    # overflow it — "CC" and a four-letter mark cannot share one font size.
    tile_px = {1: 18, 2: 16, 3: 13}.get(len(ident["mark"]), 11)
    st.markdown(f"""
<div class="cc-header">
  <div class="cc-brand">
    <div class="cc-tile" style="font-size:{tile_px}px">{_esc(ident["mark"])}</div>
    <div><div class="cc-word">{_esc(ident["word"])}</div>
         <div class="cc-sub">{_esc(ident["sub"])}</div></div>
  </div>
  <div class="cc-meta"><b>{doc_type}</b>{meta}</div>
</div>""", unsafe_allow_html=True)


def section(key: str, title: str = "", note: str = ""):
    """A bordered card you put widgets inside:

        with brand.section("plan", "Plan", "Weeks across, metrics down."):
            ...

    The key drives the `.st-key-ccsec_*` CSS hook, so it must be unique on the
    page and stable across reruns — it is a widget key in every sense that
    matters to Streamlit."""
    box = st.container(key=f"ccsec_{key}")
    if title or note:
        with box:
            st.markdown(
                (f'<div class="cc-sec-ttl">{_esc(title)}</div>' if title else "")
                + (f'<div class="cc-sec-note">{note}</div>' if note else ""),
                unsafe_allow_html=True)
    return box


def _esc(v) -> str:
    return (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _blank(v) -> bool:
    """True for None and NaN, without importing pandas into the brand layer."""
    try:
        return v is None or v != v
    except Exception:                                    # pragma: no cover
        return False


def data_table(grid, *, int_rows=(), precision=None, shade_rows=(), tone_rows=(),
               rule_before=(), na_rep: str = "—"):
    """Render a metrics-down / periods-across frame as the compact house table.

    `grid` is a DataFrame whose INDEX is the row labels and whose columns are
    already the labels to print. Rows are classified, not cells:

      int_rows    printed with thousands separators and no decimals (counts —
                  "43,269" beats "43,269.0" for contacts and volume capacity)
      precision   {row: decimals} overriding the default 1dp, for rows whose
                  meaning lives in the decimals — CPM at 1dp is "1.5" for
                  every plausible value, which is not a readout
      shade_rows  tinted background + saturated ink by sign (Net FTE: a red
                  cell in this app always means short)
      tone_rows   ink only, no tint, by sign (variance rows, where a tint on
                  every second row would fight the table)
      rule_before a hairline above the row, for grouping

    Deliberately not st.dataframe: this is the reading surface, and the grid
    affordances (sort, resize, copy) are kept alongside it rather than
    replaced — see render_plan_grid.
    """
    head = "".join(f"<th>{_esc(c)}</th>" for c in grid.columns)
    rows = []
    for name, row in grid.iterrows():
        tr = ' class="rule"' if name in rule_before else ""
        cells = []
        for v in row:
            if _blank(v):
                cells.append(f"<td>{na_rep}</td>")
                continue
            try:
                num = float(v)
            except (TypeError, ValueError):
                cells.append(f"<td>{_esc(v)}</td>")
                continue
            dp = (precision or {}).get(name)
            if dp is None:
                dp = 0 if name in int_rows else 1
            txt = f"{num:,.{dp}f}"
            cls = ""
            if name in shade_rows:
                cls = ' class="neg"' if num < 0 else ' class="pos"'
            elif name in tone_rows:
                cls = ' class="tone-neg"' if num < 0 else ' class="tone-pos"'
            cells.append(f"<td{cls}>{txt}</td>")
        rows.append(f"<tr{tr}><td>{_esc(name)}</td>{''.join(cells)}</tr>")
    st.markdown(
        f'<div class="cc-tablewrap"><table class="cc-table">'
        f"<thead><tr><th></th>{head}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>",
        unsafe_allow_html=True)


def hero(pills: list[tuple[str, str]], title: str, framing: str,
         box_label: str, box_value: str, box_unit: str, box_tone: str = ""):
    pill_html = "".join(f'<span class="cc-pill {tone}">{txt}</span>'
                        for txt, tone in pills)
    st.markdown(f"""
<div class="cc-hero">
  <div>
    <div>{pill_html}</div>
    <h1>{title}</h1>
    <p>{framing}</p>
  </div>
  <div class="cc-box {box_tone}">
    <div class="lbl">{box_label}</div>
    <div class="val">{box_value}</div>
    <div class="unit">{box_unit}</div>
  </div>
</div>""", unsafe_allow_html=True)


def band(html: str):
    st.markdown(f'<div class="cc-band">{html}</div>', unsafe_allow_html=True)


def lob_card(title: str, subtitle: str, pill_text: str, pill_tone: str,
             body_html: str, accent: tuple[str, str]):
    st.markdown(f"""
<div class="cc-card">
  <div class="bar" style="background:linear-gradient(90deg,{accent[0]},{accent[1]})"></div>
  <div class="inner">
    <div class="ttl">{title}</div>
    <div class="sub">{subtitle}</div>
    <span class="cc-pill {pill_tone}">{pill_text}</span>
    <div class="kv" style="margin-top:8px">{body_html}</div>
  </div>
</div>""", unsafe_allow_html=True)


def _spark_svg(points, color: str = CYAN, w: int = 130, h: int = 34) -> str:
    """Inline SVG sparkline — the year's shape behind a stat number."""
    vals = [float(v) for v in points]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    step = w / (len(vals) - 1)
    pts = [(i * step, (h - 4) - (v - lo) / rng * (h - 8)) for i, v in enumerate(vals)]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    cx, cy = pts[-1]
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
            f'preserveAspectRatio="none">'
            f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="2" '
            f'stroke-linejoin="round" stroke-linecap="round" opacity="0.9"/>'
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3" fill="{color}"/></svg>')


def pill_row(pills: list[tuple[str, str]]):
    """A bare row of status pills — the hero's pill strip, usable without the
    hero. Same (text, tone) pairs: "", blue, green, amber, pink."""
    st.markdown(
        '<div style="margin-bottom:10px">'
        + "".join(f'<span class="cc-pill {tone}">{_esc(txt)}</span>'
                  for txt, tone in pills)
        + "</div>", unsafe_allow_html=True)


def stat_row(tiles: list[dict]):
    """Branded stat tiles: label, value, optional delta chip (tone good/bad/'' =
    neutral), optional `sub` context line, optional sparkline series. Replaces
    bare st.metric rows so the stat band reads as part of the same system as
    the cards.

    `sub` exists because a measured metric has to be able to say how much data
    is behind it. A tile reading "—" with no explanation is indistinguishable
    from a broken one, and a tile reading "26.4%" off two recorded weeks is
    worse than one that says so."""
    cells = []
    for t in tiles:
        d = t.get("delta")
        dhtml = (f'<span class="delta {t.get("delta_tone", "")}">{d}</span>'
                 if d else "")
        spark = (_spark_svg(t["spark"], t.get("spark_color", CYAN))
                 if t.get("spark") is not None else "")
        sub = t.get("sub")
        subhtml = (f'<div class="sub{" warn" if t.get("sub_warn") else ""}">'
                   f'{_esc(sub)}</div>') if sub else ""
        cells.append(
            f'<div class="cc-stat"><div class="lbl">{t["label"]}</div>'
            f'<div class="valrow"><span class="val">{t["value"]}</span>{dhtml}</div>'
            f'{subhtml}<div class="spark">{spark}</div></div>')
    wide = " five" if len(tiles) >= 5 else ""
    st.markdown(f'<div class="cc-stats{wide}">{"".join(cells)}</div>',
                unsafe_allow_html=True)


# `footer()` lived here until 2026-07-28. It signed each page off with
# "Prepared for leadership review" — correct for the printed recommendation
# brief this house style was ported from, wrong for a tool a planner works in
# all day. Removed with its .cc-foot CSS rather than left dead.


# ---------------------------------------------------------------- altair theme
def alt_theme() -> dict:
    """Chart config matching the house style — transparent background so the
    white card shows through, recessive axes, brand categorical range."""
    return {
        "config": {
            "background": "transparent",
            "font": "Avenir Next, Avenir, Inter, ui-sans-serif, system-ui, sans-serif",
            "view": {"stroke": "transparent"},
            "axis": {
                "labelColor": MUTED, "titleColor": MUTED,
                "gridColor": BORDER_SOFT, "domainColor": BORDER,
                "tickColor": BORDER, "labelFontSize": 11, "titleFontSize": 11,
            },
            "legend": {"labelColor": BODY, "titleColor": MUTED,
                       "labelFontSize": 11, "titleFontSize": 11},
            "range": {"category": CATEGORICAL},
        }
    }


def _register_theme():
    cfg = alt_theme()
    try:                                  # altair >= 5.5 / 6.x
        @alt.theme.register("wfm_planner", enable=True)
        def _wfm_planner():
            return cfg
    except AttributeError:                # older altair
        alt.themes.register("wfm_planner", lambda: cfg)
        alt.themes.enable("wfm_planner")


_register_theme()


def chart(c: alt.Chart, **kwargs):
    """Render an Altair chart with the house theme (theme=None stops Streamlit
    from overriding it with its own)."""
    if kwargs:
        c = c.properties(**kwargs)
    st.altair_chart(c, width="stretch", theme=None)
