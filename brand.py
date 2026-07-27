"""WFM house brand for the Capacity Planner app.

Ports the visual identity from Recommendation_Format_Style_Guide.md into
Streamlit: deep-navy canvas, glassy panels, cyan→blue through-line, pills,
stat cards, and a branded wordmark header. Also provides the matching Altair
chart theme so health graphs read as part of the same system.

Everything is inline CSS / HTML (no external assets, no internet dependency)
— same rule as the brief format.
"""
import altair as alt
import streamlit as st

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
/* ------- the masthead rule: the one place brand red appears ------- */
.stApp::before {{
  content: ""; position: fixed; top: 0; left: 0; right: 0; height: 4px;
  background: {RED}; pointer-events: none; z-index: 999;
}}

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
.cc-header {{
  display: flex; align-items: center; justify-content: space-between;
  margin: 0 0 10px 0; position: relative; z-index: 1;
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
.cc-stat .spark {{ margin-top: 6px; line-height: 0; }}

.cc-band {{
  background: {TEAL_BG};
  border: 1px solid {BORDER_SOFT}; border-left: 3px solid {TEAL};
  border-radius: 0 8px 8px 0;
  padding: 14px 20px; color: {BODY}; font-size: 13.5px; line-height: 1.6;
  position: relative; z-index: 1; margin-bottom: 14px;
}}
.cc-band b {{ color: {TEXT}; }}

.cc-foot {{
  display: flex; align-items: center; justify-content: space-between;
  color: {MUTED}; font-size: 12px; margin-top: 26px; position: relative; z-index: 1;
}}
.cc-foot .line {{
  flex: 1; height: 2px; margin: 0 18px;
  background: linear-gradient(90deg, {TEAL}, {DEMAND});
  border-radius: 2px; opacity: .5;
}}
</style>
"""


def inject():
    st.markdown(_CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------- components
def header(doc_type: str, meta: str):
    st.markdown(f"""
<div class="cc-header">
  <div class="cc-brand">
    <div class="cc-tile" style="font-size:11px">WFM</div>
    <div><div class="cc-word">WFM</div>
         <div class="cc-sub">Workforce Management</div></div>
  </div>
  <div class="cc-meta"><b>{doc_type}</b>{meta}</div>
</div>""", unsafe_allow_html=True)


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


def stat_row(tiles: list[dict]):
    """Branded stat tiles: label, value, optional delta chip (tone good/bad/'' =
    neutral), optional sparkline series. Replaces bare st.metric rows so the
    stat band reads as part of the same system as the hero and cards."""
    cells = []
    for t in tiles:
        d = t.get("delta")
        dhtml = (f'<span class="delta {t.get("delta_tone", "")}">{d}</span>'
                 if d else "")
        spark = (_spark_svg(t["spark"], t.get("spark_color", CYAN))
                 if t.get("spark") is not None else "")
        cells.append(
            f'<div class="cc-stat"><div class="lbl">{t["label"]}</div>'
            f'<div class="valrow"><span class="val">{t["value"]}</span>{dhtml}</div>'
            f'<div class="spark">{spark}</div></div>')
    st.markdown(f'<div class="cc-stats">{"".join(cells)}</div>',
                unsafe_allow_html=True)


def footer(audience: str = "Prepared for leadership review"):
    st.markdown(f"""
<div class="cc-foot">
  <span>WFM · Workforce Management</span>
  <span class="line"></span>
  <span>{audience}</span>
</div>""", unsafe_allow_html=True)


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
