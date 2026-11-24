"""Shared look and helpers: card-themed dark look (card-back blue, border yellow, ball red), header, stat tiles, chips, cached thumbnails."""
import base64
import html
import io
import os

import streamlit as st

from core import ebay, vision

CSS = """<style>
@import url('https://fonts.googleapis.com/css2?family=Fredoka:wght@500;600;700&family=Nunito:wght@400;600;700;800&display=swap');
/* Card-world palette: card-back blue, card-border yellow, ball red, energy colours for states */
:root { --accent:#FFCB05; --accent-2:#FFDE55; --red:#E3350D; --bg:#0D1B4C; --panel:#14266A; --panel-2:#1A2F7C;
        --border:#2B4194; --ink:#F3F6FF; --muted:#A9B6E3; --good:#5FD068; --warn:#FFB224; --bad:#FF6B57; --info:#5DB8FF; }
html, body, .stApp, button, input, textarea, select { font-family:'Nunito', system-ui, sans-serif; }
[data-testid="stIconMaterial"], .material-symbols-rounded, [class*="material-symbols"] { font-family:'Material Symbols Rounded' !important; }
.stApp { background: radial-gradient(1200px 600px at 85% -10%, #1B3290 0%, transparent 60%), var(--bg); }
.block-container { padding-top: 4.2rem; max-width: 1400px; }
[data-testid="stHeader"] { background: transparent; }
[data-testid="stSidebar"] { background: #0A1540; border-right: 3px solid var(--accent); }
[data-testid="stSidebarNav"] a { border-radius: 999px; }
[data-testid="stSidebarNav"] a:hover { background: rgba(255,203,5,.08); }
[data-testid="stSidebarNav"] a[aria-current="page"] { background: var(--accent); }
[data-testid="stSidebarNav"] a[aria-current="page"] span { color: #0D1B4C !important; font-weight: 800; }
[data-testid="stNavSectionHeader"] { font-family:'Fredoka', sans-serif; color: var(--accent-2) !important; }
h1, h2, h3, h4 { font-family:'Fredoka', 'Nunito', sans-serif; letter-spacing: 0; font-weight: 600; }
.stButton button, .stFormSubmitButton button, .stDownloadButton button { border-radius: 999px; font-weight: 700; }
.stButton button[kind="primary"], .stFormSubmitButton button[kind="primaryFormSubmit"] {
    background: var(--red); border: 2px solid #FF6A47; color: #fff; box-shadow: 0 3px 0 #8E1F06; }
.stButton button[kind="primary"]:hover, .stFormSubmitButton button[kind="primaryFormSubmit"]:hover { background:#F2461E; }
.stButton button[kind="primary"]:active { transform: translateY(2px); box-shadow: 0 1px 0 #8E1F06; }
.stButton button[kind="secondary"], .stFormSubmitButton button[kind="secondaryFormSubmit"] {
    background: transparent; border: 2px solid var(--border); color: var(--ink); }
.stButton button[kind="secondary"]:hover, .stFormSubmitButton button[kind="secondaryFormSubmit"]:hover {
    border-color: var(--accent); color: var(--accent-2); }
button:focus-visible, a:focus-visible { outline: 3px solid var(--accent) !important; outline-offset: 2px; }
[data-testid="stVerticalBlockBorderWrapper"]:has(> div > [data-testid="stVerticalBlock"]) {
    background: var(--panel); border: 1px solid var(--border) !important; border-radius: 14px !important; }
[data-testid="stExpander"] details { border: 1px solid var(--border); border-radius: 12px; background: var(--panel); }
[data-testid="stForm"] { border: 1px solid var(--border); border-radius: 12px; }
[data-testid="stImage"] img { border-radius: 10px; }
[data-testid="stDataFrame"], [data-testid="stDataEditor"] { border: 1px solid var(--border); border-radius: 12px; }
.stTabs [data-baseweb="tab-list"] { gap: 4px; border-bottom: 1px solid var(--border); }
.stTabs [data-baseweb="tab"] { padding: 8px 14px; font-weight: 700; }
.stTabs [aria-selected="true"] { color: var(--accent) !important; }
/* header: a card's name bar - yellow frame, holo strip along the bottom */
.pp-header { position:relative; display:flex; justify-content:space-between; align-items:center; gap:16px; flex-wrap:wrap;
    padding: 14px 20px 18px; margin-bottom: 20px; border-radius: 16px; border: 4px solid var(--accent);
    background: linear-gradient(180deg, #1C3388 0%, var(--panel) 100%); box-shadow: 0 6px 0 rgba(0,0,0,.25); overflow:hidden; }
.pp-header:after { content:""; position:absolute; left:0; right:0; bottom:0; height:6px;
    background: linear-gradient(90deg,#ff6b57,#ffb224,#ffe95c,#5fd068,#5db8ff,#a77bff,#ff6b57);
    background-size: 200% 100%; animation: pp-holo 9s linear infinite; }
@keyframes pp-holo { to { background-position: 200% 0; } }
@media (prefers-reduced-motion: reduce) { .pp-header:after { animation: none; } }
.pp-header h1 { margin:0; font-size:1.8rem; font-weight:700; color:var(--ink); line-height:1.15; }
.pp-header p { margin:.25rem 0 0; color:var(--muted); font-size:.95rem; }
.pp-pills { display:flex; gap:6px; flex-wrap:wrap; }
.pp-pill { font-size:.78rem; font-weight:700; padding:4px 11px; border-radius:999px; border:2px solid var(--border);
    color:var(--muted); background:#0F1F5C; white-space:nowrap; }
.pp-pill b { color:var(--ink); font-weight:800; }
.pp-pill.good { border-color:rgba(95,208,104,.55); color:var(--good); }
.pp-pill.warn { border-color:rgba(255,178,36,.6); color:var(--warn); }
.pp-pill.bad  { border-color:rgba(255,107,87,.6); color:var(--bad); }
/* tiles */
.pp-tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin:2px 0 14px; }
.pp-tile { background:var(--panel); border:1px solid var(--border); border-radius:14px; padding:11px 14px; }
.pp-tile .l { color:var(--muted); font-size:.82rem; font-weight:700; }
.pp-tile .v { font-family:'Fredoka',sans-serif; font-size:1.5rem; font-weight:600; font-variant-numeric:tabular-nums; color:var(--ink); line-height:1.25; margin-top:2px;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.pp-tile .s { color:var(--muted); font-size:.76rem; margin-top:1px; }
.pp-tile.accent { border-left:3px solid var(--accent); }
/* section label */
.pp-section { display:flex; align-items:center; gap:10px; margin:18px 0 10px; color:var(--muted);
    font-family:'Fredoka',sans-serif; font-size:1.02rem; font-weight:600; color:var(--accent-2); }
.pp-section:after { content:""; flex:1; height:1px; background:var(--border); }
/* chips */
.pp-chip { display:inline-block; padding:2px 9px; border-radius:6px; font-size:.72rem; font-weight:650;
    letter-spacing:.02em; border:1px solid currentColor; vertical-align:middle; white-space:nowrap; }
.pp-head { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-bottom:4px; }
.pp-head .t { font-weight:650; } .pp-head .n { color:var(--muted); font-weight:600; }
.pp-muted { color:var(--muted); font-size:.85rem; }
/* product cards (Sealed EV) */
.pp-prod { background:var(--panel); border:1px solid var(--border); border-radius:12px; padding:12px; height:100%; }
.pp-prod .img { height:150px; display:flex; align-items:center; justify-content:center; background:#0A1540;
    border-radius:8px; overflow:hidden; margin-bottom:10px; }
.pp-prod .img img { max-height:150px; max-width:100%; object-fit:contain; }
.pp-prod .logo { height:26px; margin-bottom:6px; } .pp-prod .logo img { max-height:26px; max-width:120px; }
.pp-prod .nm { font-weight:650; font-size:.95rem; line-height:1.25; min-height:2.4em; }
.pp-prod .row { display:flex; justify-content:space-between; font-size:.82rem; color:var(--muted); margin-top:4px; }
.pp-prod .row b { color:var(--ink); }
.pp-bar { height:6px; border-radius:4px; background:#0F1F5C; margin-top:8px; overflow:hidden; }
.pp-bar > div { height:100%; border-radius:4px; }
.pp-hero { display:grid; grid-template-columns: 220px 1fr; gap:22px; background:var(--panel);
    border:1px solid var(--border); border-left:3px solid var(--accent); border-radius:12px; padding:16px; margin-bottom:8px; }
.pp-hero .img { background:#0A1540; border-radius:8px; display:flex; align-items:center; justify-content:center; height:200px; overflow:hidden; }
.pp-hero .img img { max-height:200px; max-width:100%; object-fit:contain; }
.pp-hero h3 { margin:2px 0 6px; } .pp-hero .k { color:var(--muted); font-size:.75rem; font-weight:700; }
.pp-hero .stats { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin-top:12px; }
.pp-hero .stats .v { font-size:1.25rem; font-weight:700; }
@media (max-width: 760px) { .pp-hero { grid-template-columns: 1fr; } .pp-hero .stats { grid-template-columns:repeat(2,1fr); } }
/* Sealed EV ranking rows */
.pp-rank { display:grid; grid-template-columns: 44px 96px minmax(0,2.2fr) repeat(4, minmax(0,1fr)); gap:16px;
    align-items:center; background:var(--panel); border:1px solid var(--border); border-radius:12px;
    padding:10px 16px; margin-bottom:8px; }
.pp-rank.top { border-left:3px solid var(--accent); }
.pp-rank .n { font-size:1.15rem; font-weight:700; color:var(--muted); text-align:center; }
.pp-rank.top .n { color:var(--accent-2); }
.pp-rank .img { height:84px; display:flex; align-items:center; justify-content:center; background:#fff;
    border-radius:8px; overflow:hidden; }
.pp-rank .img img { max-height:84px; max-width:100%; object-fit:contain; }
.pp-rank .img img.logo { max-height:44px; max-width:86px; }
.pp-rank .nm { font-weight:650; line-height:1.25; }
.pp-rank .sub { color:var(--muted); font-size:.82rem; margin-top:2px; }
.pp-rank .k { color:var(--muted); font-size:.7rem; font-weight:700; }
.pp-rank .v { font-size:1.1rem; font-weight:700; margin-top:2px; }
.pp-rank .s { color:var(--muted); font-size:.78rem; margin-top:2px; }
@media (max-width: 900px) { .pp-rank { grid-template-columns: 34px 72px 1fr 1fr; }
    .pp-rank .info { grid-column: 3 / 5; } .pp-rank .img { height:64px; } .pp-rank .img img { max-height:64px; } }
</style>"""

COLORS = {"good": "var(--good)", "warn": "var(--warn)", "bad": "var(--bad)", "info": "var(--info)",
          "accent": "var(--accent-2)", "muted": "var(--muted)"}
STATUS_KIND = {"draft": "accent", "accepted": "good", "failed": "bad", "posting": "warn", "posted": "info",
               "ended": "muted"}
LABEL_KIND = {"Invest or open": "good", "Hold / invest": "good", "Open it": "good", "Fair": "info",
              "Don't open": "warn", "Avoid": "bad", "No UK price": "muted"}


def inject():
    st.html(CSS)


def esc(s):
    return html.escape(str(s if s is not None else ""))


def money(v):
    return f"£{v:,.2f}" if isinstance(v, (int, float)) and v is not None else "—"


def header(title, subtitle="", pills=()):
    """pills: [(text, kind)] shown top-right (kind: good/warn/bad/'' )."""
    st.html('<div class="pp-header"><div><h1>' + esc(title) + "</h1>" + (f"<p>{esc(subtitle)}</p>" if subtitle else "")
            + '</div><div class="pp-pills">' + "".join(f'<span class="pp-pill {k}">{t}</span>' for t, k in pills)
            + "</div></div>")


def section(label):
    st.html(f'<div class="pp-section">{esc(label)}</div>')


def tiles(items):
    """items: [(label, value, sub)] or [(label, value, sub, accent: bool)]."""
    st.html('<div class="pp-tiles">' + "".join(
        f'<div class="pp-tile{" accent" if (len(t) > 3 and t[3]) else ""}"><div class="l">{esc(t[0])}</div>'
        f'<div class="v">{esc(t[1])}</div><div class="s">{esc(t[2])}</div></div>' for t in items) + "</div>")


def chip(text, kind="accent"):
    return f'<span class="pp-chip" style="color:{COLORS.get(kind, kind)}">{esc(text)}</span>'


def env_pills():
    env = os.environ.get("EBAY_ENV", "sandbox")
    pills = [(f"eBay <b>{env}</b>", "good" if env == "production" else "warn"),
             (f"Card reading <b>{vision.backend().upper()}</b>", "")]
    lim = limits()
    if lim and lim.get("items_left") is not None:
        k = "bad" if lim["items_left"] == 0 else "warn" if lim["items_left"] <= 3 else "good"
        pills.append((f"Listings left <b>{lim['items_left']}</b> · {money(lim.get('value_left'))}", k))
    return pills


@st.cache_data(ttl=600, show_spinner=False)
def _limits():
    return ebay.selling_limits()


def limits(fresh=False):
    """Selling limits from eBay, kept for 10 minutes for every browser session (each new session used to wait
    1.2 s for them); None if eBay keys aren't set up or eBay can't be reached. fresh=True re-reads them now."""
    if fresh:
        _limits.clear()
    try:
        return _limits()
    except Exception:
        return None


def big_image(url):
    """eBay serves s-l225 thumbnails in search results; ask for a sharper size."""
    return url.replace("s-l225", "s-l500") if url else url


@st.cache_data(max_entries=500, show_spinner=False)
def thumb(path, mtime, edge=480):
    from PIL import Image, ImageOps
    im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    im.thumbnail((edge, edge))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def photo(path, edge=480):
    try:
        return thumb(path, os.path.getmtime(path), edge)
    except (OSError, TypeError, ValueError):
        return None


def data_uri(b):
    return "data:image/jpeg;base64," + base64.b64encode(b).decode() if b else None


@st.cache_data(max_entries=200, show_spinner=False)
def upload_thumb(file_id, _data, edge=360):
    from PIL import Image, ImageOps
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(_data))).convert("RGB")
    im.thumbnail((edge, edge))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return buf.getvalue()


@st.cache_data(max_entries=20, show_spinner=False)
def cached_pairs(key, _named, saved=None):
    """(pairs, how) — see vision.pair_photos_info. key must include `saved`."""
    return vision.pair_photos_info(_named, saved)


_photo_order = None
_file_times = None


def file_times(key):
    """{"name|size": ms} — when each picked/dropped photo was saved on the device (ui/file_times, invisible)."""
    global _file_times
    if _file_times is None:
        import os
        import streamlit.components.v1 as components
        _file_times = components.declare_component(
            "file_times", path=os.path.join(os.path.dirname(__file__), "file_times"))
    return _file_times(key=key, default={}) or {}


def photo_order(items, key):
    """Drag-and-drop photo order (ui/photo_order/index.html, no extra package). items: [{id, src, label}] in the
    current order, shown as front/back pairs; returns the new id order after a drag or ⇄, else None."""
    global _photo_order
    if _photo_order is None:
        import os
        import streamlit.components.v1 as components
        _photo_order = components.declare_component(
            "photo_order", path=os.path.join(os.path.dirname(__file__), "photo_order"))
    return _photo_order(items=items, key=key, default=None)
