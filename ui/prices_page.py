"""Prices: one-box search, market overview (movers, watchlist, recently viewed) and a detailed card view."""
import os
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from core import catalog, db, ebay, inventory, prices
from ui.common import env_pills, esc, header, money, section, tiles

ERA_FILTERS = {"All": None, "Scarlet & Violet": "sv", "Sword & Shield": "swsh", "Sun & Moon": "sm", "XY": "xy",
               "Older": "old"}


def page():
    header("Card prices", "UK market value per condition from eBay: sold prices first, else the cheaper UK listings",
           env_pills())
    if "card" in st.session_state:
        card_view(*st.session_state.card)
        return
    search()
    overview()


# ---------------------------------------------------------------- search
def search():
    with st.form("search", border=False):
        c1, c2 = st.columns([5, 1])
        text = c1.text_input("Search", st.session_state.get("q", ""), label_visibility="collapsed",
                             placeholder="Try: charizard 199 · sv08 238 · 199/165 · Kingdra TG03 · pikachu sv08 · リザードン")
        go = c2.form_submit_button("Search", type="primary", width="stretch")
    era = st.segmented_control("Era", list(ERA_FILTERS), default="All", key="era_f") or "All"
    if go and text.strip():
        st.session_state.q = text
        st.session_state.results = run_search(text)
    res = st.session_state.get("results")
    if res is None:
        return
    lang, cards = res
    want = ERA_FILTERS[era]
    if want:
        cards = [c for c in cards if (catalog.era_of(c["id"]) or "old") == want]
    if not cards:
        st.info("No cards found. Use the English name (or Japanese for Japanese cards), a number like 199 or TG05, "
                "or a set id like sv08.")
        return
    section(f"{len(cards)} result(s)")
    cols = st.columns(6)
    watched = {r["card_key"] for r in db.q("SELECT card_key FROM watch")}
    for i, c in enumerate(cards[:48]):
        s = catalog.set_(lang, c["id"].rsplit("-", 1)[0]) or {}
        with cols[i % 6]:
            if c.get("image"):
                st.image(catalog.image(c, "low"), width="stretch")
            st.markdown(f"**{c['name']}** · {c['localId']}  \n<span class='pp-muted'>{esc(s.get('name', ''))} · "
                        f"{esc((s.get('releaseDate') or '')[:4])}</span>", unsafe_allow_html=True)
            b1, b2 = st.columns(2)
            if b1.button("Open", key=f"open{c['id']}", width="stretch"):
                open_card(lang, c["id"])
            key = f"{lang}:{c['id']}"
            if b2.button("★" if key in watched else "☆", key=f"w{c['id']}", width="stretch",
                         help="Watch: tracked daily and shown on the overview"):
                toggle_watch(lang, c["id"], c["name"], key in watched)


@st.cache_data(ttl=3600, show_spinner="Searching the catalog…")
def run_search(text):
    q = catalog.parse_search(text)
    lang, name, number, set_hint = q["lang"], q["name"], q["number"], q["set"]
    if set_hint and not name and not number:
        cards = catalog._get(f"/{lang}/cards?set.id=eq:{set_hint}") or []
    else:
        cards = catalog.search(lang, name, number)
        if not cards and number and name:                     # unpadded/padded number mismatch: name only
            cards = [c for c in catalog.search(lang, name) if c["localId"].lstrip("0").lower() == number.lstrip("0").lower()]
        if set_hint:
            cards = [c for c in cards if c["id"].lower().startswith(set_hint.lower() + "-")]
    # newest sets first (release dates from the set objects, cached)
    rel = {sid: (catalog.set_(lang, sid) or {}).get("releaseDate", "") for sid in {c["id"].rsplit("-", 1)[0] for c in cards[:120]}}
    cards = sorted(cards[:120], key=lambda c: rel.get(c["id"].rsplit("-", 1)[0], ""), reverse=True)
    return lang, cards


def open_card(lang, card_id):
    st.session_state.card = (lang, card_id)
    st.rerun()


def toggle_watch(lang, card_id, name, on):
    key = f"{lang}:{card_id}"
    if on:
        db.run("DELETE FROM watch WHERE card_key=?", (key,))
    else:
        db.run("INSERT OR REPLACE INTO watch VALUES (?,?,?,?,?)",
               (key, lang, card_id, name, datetime.now(timezone.utc).isoformat()))
        prices.follow(key, 3, "watched", name)   # refreshed every 6 h while the app runs
    st.rerun()


# ---------------------------------------------------------------- overview
def overview():
    n_cards, days = prices.tracking_stats()
    n_tracked = db.q("SELECT COUNT(*) n FROM tracked")[0]["n"]
    n_sales = db.q("SELECT COUNT(*) n FROM sales")[0]["n"]
    section("Market overview")
    tiles([("Cards tracked", n_cards, "with UK price history", True),
           ("UK listings followed", n_tracked, "checked daily for sales"),
           ("Confirmed sales", n_sales, "inferred from ended listings"),
           ("History", f"{days} day(s)", "of daily price snapshots")])
    up, down = prices.movers()
    c1, c2 = st.columns(2)
    with c1:
        section("Rising (7 days)")
        movers_table(up, "No risers yet — price history needs a few days of snapshots.")
    with c2:
        section("Falling (7 days)")
        movers_table(down, "No fallers yet — price history needs a few days of snapshots.")
    watchlist()
    recent()


def movers_table(rows, empty):
    if not rows:
        st.caption(empty)
        return
    st.dataframe(pd.DataFrame([{"Card": n, "Median ask": m, "Change": p / 100} for k, p, m, n in rows]),
                 hide_index=True, width="stretch",
                 column_config={"Median ask": st.column_config.NumberColumn(format="£%.2f"),
                                "Change": st.column_config.NumberColumn(format="percent")})


def watchlist():
    rows = db.q("SELECT w.*, s.image, s.set_name, s.number FROM watch w LEFT JOIN cards_seen s USING(card_key) "
                "ORDER BY w.added_at DESC")
    section(f"Watchlist ({len(rows)})")
    if not rows:
        st.caption("Star cards in search results to watch them: they're refreshed daily and shown here with a trend line.")
        return
    data = []
    for r in rows:
        hist = [v for d, b, v in prices.ask_history(r["card_key"]) if b == "Raw"]
        mv = prices.market_for(r["card_key"])[0]
        data.append({"Card": r["image"], "Name": f"{r['name']} {r['number'] or ''}".strip(), "Set": r["set_name"],
                     "Market": mv, "Trend": hist or [0], "key": r["card_key"]})
    df = pd.DataFrame(data)
    st.dataframe(df.drop(columns=["key"]), hide_index=True, width="stretch", row_height=70,
                 column_config={"Card": st.column_config.ImageColumn(width="small"),
                                "Market": st.column_config.NumberColumn(format="£%.2f"),
                                "Trend": st.column_config.LineChartColumn("Raw median ask", width="medium")})
    pick = st.selectbox("Open a watched card", [""] + [d["key"] for d in data],
                        format_func=lambda k: next((d["Name"] for d in data if d["key"] == k), "—") if k else "—")
    if pick:
        lang, cid = pick.split(":", 1)
        open_card(lang, cid)


def recent():
    rows = db.q("SELECT * FROM cards_seen ORDER BY last_seen DESC LIMIT 8")
    if not rows:
        return
    section("Recently viewed")
    cols = st.columns(8)
    for col, r in zip(cols, rows):
        with col:
            if r["image"]:
                st.image(r["image"], width="stretch")
            if st.button(r["name"][:18], key=f"rv{r['card_key']}", width="stretch"):
                open_card(r["lang"], r["card_id"])


# ---------------------------------------------------------------- card view
def card_view(lang, card_id):
    c = catalog.card(lang, card_id)
    key = f"{lang}:{card_id}"
    top1, top2, top3 = st.columns([1, 1, 4])
    if top1.button("← Back", width="stretch"):
        del st.session_state.card
        st.rerun()
    watched = bool(db.q("SELECT 1 FROM watch WHERE card_key=?", (key,)))
    name_en = catalog.english_name(lang, c) or c["name"]
    if top2.button("★ Watching" if watched else "☆ Watch", width="stretch"):
        toggle_watch(lang, card_id, name_en, watched)
    left, right = st.columns([1, 3])
    if catalog.image(c):
        left.image(catalog.image(c), width="stretch")
    else:
        left.html('<div class="pp-tile" style="height:300px;display:flex;align-items:center;justify-content:center">'
                  '<span class="pp-muted">No catalog image for this card</span></div>')
    num = catalog.printed_number(c)
    s = c.get("set") or {}
    right.markdown(f"### {name_en} · {num}")
    right.markdown(f"<span class='pp-muted'>{esc(s.get('name', ''))} · {esc(c.get('rarity') or '')} · "
                   f"{'Japanese' if lang == 'ja' else 'English'}</span>", unsafe_allow_html=True)
    name_en = right.text_input("Name used for eBay searches", name_en, key=f"n{key}")
    b1, b2, b3, b4 = right.columns(4)
    force_ask = b1.button("↻ Refresh UK prices", width="stretch")
    fetch_sold = b2.button("Fetch sold (SoldComps)", disabled=not os.environ.get("SOLDCOMPS_KEY"), width="stretch")
    if b3.button("List this card →", type="primary", width="stretch",
                 help="Go to List cards with this card pre-selected; just add the photos"):
        st.session_state.prefill = {"lang": lang, "id": card_id, "name": name_en, "number": num, "set": s.get("name", "")}
        st.session_state.goto = "list"          # app.py switches page on the next run
        st.rerun()
    with b4.popover("＋ I own this", width="stretch"):
        cond = st.selectbox("Condition", ["Raw", "Raw played", "PSA 10", "PSA 9", "BGS 9.5", "CGC 10"], key=f"ic{key}")
        cost = st.number_input("You paid £", 0.0, 100000.0, 0.0, 0.5, key=f"ip{key}")
        if st.button("Add to inventory", type="primary", key=f"ia{key}"):
            inventory.add(key, name_en, cost, s.get("name"), num, cond, catalog.image(c))
            st.toast(f"{name_en} added to Inventory")
    prices.remember_card(lang, c, name_en)
    prices.follow(key, 2, "viewed", name_en, num)   # keep tracking it so sold prices build up
    if name_en:
        try:
            prices.refresh_card_asks(key, name_en, num, lang, force=force_ask)
            prices.infer_sales(key)
        except ebay.EbayError as e:
            right.error(f"eBay prices unavailable: {e}")
        if fetch_sold:
            try:
                prices.refresh(key, prices.card_query(name_en, num, lang), force=True)
            except prices.SoldCompsError as e:
                right.error(str(e))
    sold, asks = prices.values(key), prices.ask_values(key)
    cm = catalog.cardmarket_gbp(c, db.setting("eur_gbp"))   # shown as EU reference only, never used
    buckets = sorted(set(sold) | set(asks), key=lambda b: (not b.startswith("Raw"), b))
    vals = {}
    for b in buckets:
        mv, _, basis = prices.market_for(key, *(b.split(" ") if not b.startswith("Raw") else (None, None)))
        vals[b] = (mv, basis.split("· ")[-1])
    raw = vals.get("Raw", (None, ""))
    graded = [(b, v) for b, v in vals.items() if not b.startswith("Raw") and v[0]]
    best_g = max(graded, key=lambda x: x[1][0]) if graded else None
    a_raw = asks.get("Raw") or {}
    with right:
        tiles([("Raw NM value", money(raw[0]), raw[1] or "no eBay data yet", True),
               ("Cheapest raw UK", money(a_raw.get("low")), f"{a_raw.get('n', 0)} UK listings"),
               ("Top graded", money(best_g[1][0]) if best_g else "—", best_g[0] if best_g else "no graded listings"),
               ("EU reference", money(cm), "Cardmarket, not used for prices")])
    if not sold and not asks:
        st.info("No eBay data yet for this card. Add eBay keys in .streamlit/secrets.toml — UK asking prices are free.")
        return
    section("Value by condition")
    g1, g2 = st.columns([2, 3])
    chart = pd.DataFrame([{"Condition": b, "£": v[0]} for b, v in vals.items() if v[0] and b != "Raw played"])
    if not chart.empty:
        g1.bar_chart(chart, x="Condition", y="£", horizontal=True, color="#7C5CFF")
    rows = []
    for b in buckets:
        a, s_ = asks.get(b) or {}, sold.get(b) or {}
        rows.append({"Condition": b, "Market": vals[b][0] if b != "Raw played" else None,
                     "Based on": vals[b][1] if b != "Raw played" else "", "Median ask": a.get("market"),
                     "Cheapest": a.get("low"), "Listings": a.get("n"), "Sold median": s_.get("market"),
                     "Sales": s_.get("n"), "Last sold": s_.get("last_sold")})
    money_cols = {k: st.column_config.NumberColumn(format="£%.2f") for k in ("Market", "Median ask", "Cheapest", "Sold median")}
    g2.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config=money_cols)
    section("Price history")
    pts = [{"date": d, "£": v, "series": f"{b} ask"} for d, b, v in prices.ask_history(key)]
    pts += [{"date": r["sold_at"][:10], "£": r["price"] + r["shipping"], "series": f"{r['bucket']} sold"} for r in prices.comps(key)]
    if pts:
        df = pd.DataFrame(pts)
        df["date"] = pd.to_datetime(df["date"])
        st.scatter_chart(df, x="date", y="£", color="series", height=280)
    tracked = db.q("SELECT status, COUNT(*) n FROM tracked WHERE card_key=? GROUP BY status", (key,))
    st.caption("Sold prices are inferred from UK listings this app follows that end sold (auctions at the final "
               "bid; Best Offer sales flagged, since the accepted offer is hidden). Listings followed: "
               + (", ".join(f"{t['n']} {t['status']}" for t in tracked) or "none yet")
               + ". Market = sold median (30 days, or the last 10 sales) once there are 3+ sales; until then the "
                 "low quartile of today's UK listings. Watch the card to check it every 6 hours.")
    current = db.q("SELECT image, bucket, total, title, url FROM asks WHERE card_key=? AND seen_on=(SELECT MAX(seen_on) "
                   "FROM asks WHERE card_key=?) ORDER BY total", (key, key))
    section(f"Live UK listings ({len(current)})")
    if current:
        st.dataframe(pd.DataFrame([dict(r) for r in current]), hide_index=True, width="stretch", row_height=56,
                     column_config={"image": st.column_config.ImageColumn("Photo", width="small"),
                                    "bucket": "Condition", "total": st.column_config.NumberColumn("Total", format="£%.2f"),
                                    "title": st.column_config.TextColumn("Title", width="large"),
                                    "url": st.column_config.LinkColumn("eBay", display_text="Open ↗")})
    if sold:
        with st.expander("Sold comps"):
            st.dataframe(pd.DataFrame(prices.comps(key)), hide_index=True, width="stretch",
                         column_config={"url": st.column_config.LinkColumn("eBay", display_text="Open ↗")})
    with st.expander("Override market value (e.g. from eBay's Price Guide)"):
        ob = st.selectbox("Condition", buckets or ["Raw"], key=f"ob{key}")
        cur = db.setting(f"override:{key}:{ob}") or {}
        ov = st.number_input("£ (0 clears)", 0.0, 100000.0, float(cur.get("value") or 0.0), 0.5, key=f"ov{key}")
        if st.button("Save override", key=f"os{key}"):
            db.set_setting(f"override:{key}:{ob}", {"value": ov} if ov else None)
            st.rerun()
