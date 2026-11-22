# PokéProj — Streamlit entry point. Pages live in ui/, all logic in core/.
import os

import streamlit as st

try:
    os.environ.update({k: str(v) for k, v in st.secrets.items() if not isinstance(v, dict)})
except Exception:
    pass  # no secrets file yet; pages show what's missing

from core import db, deals  # noqa: E402
from ui import common, deals_page, ev_page, inventory_page, list_page, prices_page, settings_page  # noqa: E402

st.set_page_config(page_title="PokéProj", page_icon="🃏", layout="wide")
st.logo(os.path.join(os.path.dirname(__file__), "ui", "logo.svg"), size="large")
common.inject()
st.cache_resource(deals.start_background)()   # one background deal scanner per server process

pages = {"list": st.Page(list_page.page, title="List cards", icon=":material/sell:", default=True),
         "prices": st.Page(prices_page.page, title="Card prices", icon=":material/monitoring:", url_path="prices"),
         "ev": st.Page(ev_page.page, title="Sealed EV", icon=":material/inventory_2:", url_path="ev"),
         "deals": st.Page(deals_page.page, title="Deal finder", icon=":material/local_offer:", url_path="deals"),
         "inventory": st.Page(inventory_page.page, title="Inventory", icon=":material/inventory:", url_path="inventory"),
         "settings": st.Page(settings_page.page, title="Settings", icon=":material/settings:", url_path="settings")}
nav = st.navigation({"Buy": [pages["deals"], pages["inventory"]], "Sell": [pages["list"]],
                     "Research": [pages["prices"], pages["ev"]], "Account": [pages["settings"]]})

with st.sidebar:
    counts = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM drafts GROUP BY status")}
    st.caption(f"**{counts.get('draft', 0)}** to review · **{counts.get('accepted', 0)}** ready · "
               f"**{counts.get('posted', 0)}** live")
    last = (db.setting("last_refresh", "") or "")[:16].replace("T", " ")
    st.caption(f"eBay {os.environ.get('EBAY_ENV', 'sandbox')} · last data refresh {last or 'never'}")

goto = st.session_state.pop("goto", None)
if goto in pages:
    st.switch_page(pages[goto])
nav.run()
