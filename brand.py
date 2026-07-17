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
BG = "#050814"
SURFACE = "rgba(15, 23, 42, 0.72)"
BORDER = "rgba(51, 65, 85, 0.75)"
TEXT = "#f8fafc"
BODY = "#cbd5e1"
MUTED = "#94a3b8"
CYAN = "#06b6d4"
CYAN_LT = "#67e8f9"
BLUE = "#3b82f6"
VIOLET = "#8b5cf6"
MAGENTA = "#d946ef"
GREEN = "#10b981"
TEAL = "#14b8a6"
AMBER = "#f59e0b"
AMBER_LT = "#fcd34d"
PINK = "#ec4899"

# Per-item accent gradients — assigned to LOBs cyclically (hue → adjacent hue).
ACCENTS = [
    (CYAN, BLUE),
    (VIOLET, MAGENTA),
    (GREEN, TEAL),
    (AMBER, PINK),
]


def accent_for(i: int) -> tuple[str, str]:
    return ACCENTS[i % len(ACCENTS)]


# ---------------------------------------------------------------- CSS
_CSS = f"""
<style>
/* ------- atmosphere: two fixed blurred glows behind everything ------- */
.stApp::before, .stApp::after {{
  content: ""; position: fixed; border-radius: 50%;
  filter: blur(70px); pointer-events: none; z-index: 0;
}}
.stApp::before {{
  width: 620px; height: 620px; top: -180px; left: 34%;
  background: {CYAN}; opacity: .14;
}}
.stApp::after {{
  width: 560px; height: 560px; bottom: -160px; right: -120px;
  background: {VIOLET}; opacity: .16;
}}

/* ------- typography ------- */
html, body, .stApp, [class*="css"] {{
  font-family: Inter, ui-sans-serif, system-ui, -apple-system,
               BlinkMacSystemFont, "Segoe UI", sans-serif;
}}
h1 {{ letter-spacing: -0.045em; }}
h2, h3 {{ letter-spacing: -0.03em; }}

/* ------- glassy metric cards (native st.metric, app-wide) ------- */
[data-testid="stMetric"] {{
  background: {SURFACE}; border: 1px solid {BORDER};
  border-radius: 22px; padding: 14px 18px;
  backdrop-filter: blur(14px);
  box-shadow: 0 24px 80px rgba(0,0,0,0.28);
}}
[data-testid="stMetricLabel"] {{
  text-transform: uppercase; letter-spacing: .08em;
  font-size: 11px !important; color: {MUTED} !important;
}}
[data-testid="stMetricLabel"] p {{ white-space: normal; overflow: visible; }}
[data-testid="stMetricValue"] {{
  font-weight: 800; letter-spacing: -0.02em; color: {TEXT};
  font-size: clamp(20px, 2.4vw, 34px) !important;
}}

/* ------- sidebar: darker glass ------- */
[data-testid="stSidebar"] {{
  background: rgba(8, 12, 26, 0.92);
  border-right: 1px solid {BORDER};
}}

/* ------- expanders / editors pick up the surface ------- */
[data-testid="stExpander"] details {{
  background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 16px;
}}

/* ------- WFM components (emitted by brand.py helpers) ------- */
.cc-header {{
  display: flex; align-items: center; justify-content: space-between;
  margin: 0 0 18px 0; position: relative; z-index: 1;
}}
.cc-brand {{ display: flex; align-items: center; gap: 12px; }}
.cc-tile {{
  width: 42px; height: 42px; border-radius: 14px;
  background: linear-gradient(135deg, {CYAN}, {BLUE});
  display: flex; align-items: center; justify-content: center;
  color: #fff; font-weight: 800; font-size: 16px; letter-spacing: -0.02em;
}}
.cc-word {{ color: {TEXT}; font-weight: 800; font-size: 19px; line-height: 1.05; }}
.cc-sub {{
  color: {MUTED}; font-size: 10.5px; text-transform: uppercase;
  letter-spacing: .1em; margin-top: 2px;
}}
.cc-meta {{ text-align: right; color: {MUTED}; font-size: 12px; }}
.cc-meta b {{ color: {BODY}; display: block; font-size: 12.5px; }}

.cc-hero {{
  background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 28px;
  padding: 26px 30px; backdrop-filter: blur(14px);
  box-shadow: 0 24px 80px rgba(0,0,0,0.28);
  display: flex; justify-content: space-between; gap: 26px; align-items: center;
  position: relative; z-index: 1; margin-bottom: 16px;
}}
.cc-hero h1 {{
  font-size: clamp(26px, 3.6vw, 40px); line-height: .98;
  letter-spacing: -0.055em; color: {TEXT}; margin: 10px 0 10px 0;
}}
.cc-hero p {{ color: {BODY}; font-size: 14.5px; line-height: 1.6; margin: 0; max-width: 62ch; }}
.cc-box {{
  border: 1px solid rgba(6,182,212,.45); background: rgba(6,182,212,.08);
  border-radius: 22px; padding: 16px 26px; text-align: center; min-width: 170px;
}}
.cc-box .lbl {{
  color: {CYAN_LT}; font-size: 11px; text-transform: uppercase; letter-spacing: .08em;
}}
.cc-box .val {{ color: {TEXT}; font-size: 40px; font-weight: 800; letter-spacing: -0.03em; line-height: 1.1; }}
.cc-box .unit {{ color: {MUTED}; font-size: 12px; }}
.cc-box.bad {{ border-color: rgba(236,72,153,.5); background: rgba(236,72,153,.08); }}
.cc-box.bad .lbl {{ color: #f9a8d4; }}
.cc-box.good {{ border-color: rgba(16,185,129,.5); background: rgba(16,185,129,.08); }}
.cc-box.good .lbl {{ color: #6ee7b7; }}

.cc-pill {{
  display: inline-block; border-radius: 999px; padding: 3px 12px;
  font-size: 11.5px; font-weight: 600; letter-spacing: .02em;
  border: 1px solid {BORDER}; color: {BODY}; background: rgba(51,65,85,.3);
  margin-right: 6px;
}}
.cc-pill.blue  {{ color: {CYAN_LT};  border-color: rgba(6,182,212,.4);  background: rgba(6,182,212,.1); }}
.cc-pill.green {{ color: #6ee7b7; border-color: rgba(16,185,129,.4); background: rgba(16,185,129,.1); }}
.cc-pill.amber {{ color: {AMBER_LT}; border-color: rgba(245,158,11,.4); background: rgba(245,158,11,.1); }}
.cc-pill.pink  {{ color: #f9a8d4; border-color: rgba(236,72,153,.4); background: rgba(236,72,153,.1); }}

.cc-card {{
  background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 22px;
  padding: 0 0 14px 0; backdrop-filter: blur(14px); overflow: hidden;
  box-shadow: 0 24px 80px rgba(0,0,0,0.28); position: relative; z-index: 1;
  margin-bottom: 12px;
}}
.cc-card .bar {{ height: 6px; }}
.cc-card .inner {{ padding: 14px 18px 0 18px; }}
.cc-card .ttl {{ color: {TEXT}; font-weight: 700; font-size: 15.5px; }}
.cc-card .sub {{ color: {MUTED}; font-size: 12px; margin-bottom: 8px; }}
.cc-card .kv {{ color: {BODY}; font-size: 13px; line-height: 1.65; }}
.cc-card .kv b {{ color: {TEXT}; }}

.cc-stats {{
  display: flex; gap: 12px; margin: 0 0 14px 0; position: relative; z-index: 1;
}}
.cc-stat {{
  flex: 1; background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 22px;
  padding: 14px 18px 10px 18px; backdrop-filter: blur(14px);
  box-shadow: 0 24px 80px rgba(0,0,0,0.28); min-width: 0;
}}
.cc-stat .lbl {{
  color: {MUTED}; font-size: 11px; text-transform: uppercase; letter-spacing: .08em;
}}
.cc-stat .valrow {{ display: flex; align-items: baseline; gap: 8px; margin-top: 2px; }}
.cc-stat .val {{
  color: {TEXT}; font-weight: 800; letter-spacing: -0.02em;
  font-size: clamp(20px, 2.2vw, 30px);
}}
.cc-stat .delta {{
  font-size: 11.5px; font-weight: 700; border-radius: 999px; padding: 2px 8px;
  border: 1px solid {BORDER}; color: {BODY}; background: rgba(51,65,85,.3);
  white-space: nowrap;
}}
.cc-stat .delta.good {{ color: #6ee7b7; border-color: rgba(16,185,129,.4); background: rgba(16,185,129,.1); }}
.cc-stat .delta.bad  {{ color: #f9a8d4; border-color: rgba(236,72,153,.4); background: rgba(236,72,153,.1); }}
.cc-stat .spark {{ margin-top: 6px; opacity: .9; line-height: 0; }}

.cc-band {{
  background: linear-gradient(90deg, rgba(6,182,212,.10), rgba(59,130,246,.10));
  border: 1px solid rgba(6,182,212,.35); border-radius: 22px;
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
  background: linear-gradient(90deg, {CYAN}, {BLUE});
  border-radius: 2px; opacity: .8;
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
    """Chart config matching the house style — transparent glass background,
    muted axes, Inter, brand categorical range."""
    return {
        "config": {
            "background": "transparent",
            "font": "Inter, ui-sans-serif, system-ui, sans-serif",
            "view": {"stroke": "transparent"},
            "axis": {
                "labelColor": MUTED, "titleColor": MUTED,
                "gridColor": "rgba(51,65,85,0.45)", "domainColor": BORDER,
                "tickColor": BORDER, "labelFontSize": 11, "titleFontSize": 11,
            },
            "legend": {"labelColor": BODY, "titleColor": MUTED,
                       "labelFontSize": 11, "titleFontSize": 11},
            "range": {"category": [CYAN, VIOLET, GREEN, AMBER, PINK, BLUE]},
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
