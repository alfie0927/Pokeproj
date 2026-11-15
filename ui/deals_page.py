"""Deal finder: new UK listings and auctions ending soon, identified and priced against market, with resale profit."""
from datetime import datetime, timezone

import streamlit as st

from core import db, deals, inventory, prices
from ui.common import big_image, chip, env_pills, esc, header, money, section, tiles
from ui.ev_page import fmt_age

SORTS = {"Newest": None, "£ profit": "profit", "ROI": "roi", "% of market": "ratio"}


def page():
    header("Deal finder", "New UK listings and auctions ending soon, compared with market value and resale profit",
           env_pills())
    c1, c2, c3, c4, c5 = st.columns([1.4, 1.4, 1, 1.4, 1])
    pct_m = c1.slider("Modern (XY onwards): flag at or under % of market", 20, 95, int(db.setting("deal_pct_modern")), 5)
    pct_v = c2.slider("Vintage / older: flag at or under % of market", 20, 95, int(db.setting("deal_pct_vintage")), 5)
    min_market = c3.number_input("Min market £", 0.0, 500.0, float(db.setting("deal_min_market")), 1.0,
                                 help="Ignore cheap commons: only cards worth at least this much.")
    auto = c4.toggle("Scan constantly in the background", bool(db.setting("deal_auto")),
                     help="New listings every minute, auctions every 10, while the app is running.")
    alert = c4.toggle("Beep + Windows notification on new deals", bool(db.setting("deal_alert")),
                      help="Click the notification to open the listing. Works with the browser tab closed.")
    vals = {"deal_pct_modern": pct_m, "deal_pct_vintage": pct_v, "deal_min_market": min_market, "deal_auto": auto,
            "deal_alert": alert}
    with st.expander("Profit, OCR & auction settings"):
        e1, e2, e3, e4 = st.columns(4)
        vals["postage_cost"] = e1.number_input("Your postage cost when reselling £", 0.0, 20.0,
                                               float(db.setting("postage_cost")), 0.05)
        vals["sell_fee_pct"] = e2.number_input("Selling fee %", 0.0, 30.0, float(db.setting("sell_fee_pct")), 0.5,
                                               help="UK private sellers pay 0 % (buyers pay eBay's protection fee).")
        vals["auction_window_hours"] = e3.number_input("Auctions ending within (hours)", 0.5, 24.0,
                                                       float(db.setting("auction_window_hours")), 0.5)
        vals["deal_ocr"] = e4.toggle("OCR photos when the title has no number", bool(db.setting("deal_ocr")),
                                     help="2-5 s per photo, up to 8 (or 35 s) per scan, after the title matches are done.")
        if e4.button("Test alert"):
            deals.notify("PokéProj test alert", "Deals will look like this", "https://www.ebay.co.uk")
    for k, v in vals.items():
        if db.setting(k) != v:
            db.set_setting(k, v)
    if c5.button("Scan now", type="primary", width="stretch"):
        run_scan(vals["deal_ocr"])
        st.rerun()
    for k in ("last_deal_error", "last_auction_error"):
        if db.setting(k, ""):
            st.warning(f"Last background {'auction ' if 'auction' in k else ''}scan problem: " + db.setting(k))
    st.fragment(run_every=15)(live)()


def run_scan(ocr):
    bar = st.progress(0.0, "Fetching newest listings…")
    try:
        s = deals.scan(ocr, progress=lambda f, t: bar.progress(f, f"Checking: {t}"))
        bar.progress(1.0, "Checking auctions ending soon…")
        a = deals.scan_auctions()
        bar.empty()
        st.toast(f"{s['new']} new · {s['identified']} identified · {s['deals']} deals · {a['kept']} auctions")
    except Exception as e:
        bar.empty()
        st.error(str(e))


def live():
    """Re-rendered every 15 s so finds from the background scanner appear without a reload."""
    last = db.setting("last_deal_scan", {}) or {}
    rows = [dict(r) for r in deals.feed()]
    auctions = [dict(r) for r in deals.auctions_feed()]
    today = db.q("SELECT COUNT(*) n FROM deal_seen WHERE seen_at >= date('now')")[0]["n"]
    tiles([("Last scan", fmt_age(last.get("at")), f"{last.get('new', 0)} new listings · {last.get('identified', 0)} identified"),
           ("Checked today", today, f"UK listings · {prices.calls_today():,} eBay calls used"),
           ("Deals in feed", len(rows), "last 48 hours", bool(rows)),
           ("Auctions to watch", len(auctions), f"ending within {db.setting('auction_window_hours'):g} h", bool(auctions))])
    t1, t2 = st.tabs([f"Buy now ({len(rows)})", f"Auctions ending soon ({len(auctions)})"])
    with t1:
        buy_now(rows)
    with t2:
        auction_list(auctions)


def buy_now(rows):
    c1, c2, c3 = st.columns([1.2, 1, 1])
    sort = c1.segmented_control("Sort", list(SORTS), default="Newest", key="deal_sort") or "Newest"
    min_profit = c2.number_input("Min profit £", -50.0, 1000.0, float(db.setting("deal_min_profit")), 1.0)
    if min_profit != float(db.setting("deal_min_profit")):
        db.set_setting("deal_min_profit", min_profit)
    sold_only = c3.toggle("Sold-price backed only", key="deal_sold_only",
                          help="Hide deals valued from current UK listings; keep ones valued from real sales.")
    for r in rows:   # deals from before profit was stored
        if r["profit"] is None and r["market"]:
            r["profit"], r["roi"] = deals.flip(r["total"], r["market"])
    rows = [r for r in rows if (r["profit"] or 0) >= min_profit and (not sold_only or "sold" in (r["basis"] or ""))]
    if SORTS[sort]:
        rows.sort(key=lambda r: (r[SORTS[sort]] or 0), reverse=SORTS[sort] != "ratio")
    if not rows:
        st.info("Nothing here yet. Deals appear as listings arrive (background scan every minute, or **Scan now**). "
                "Only listings whose card and condition can be read are compared.")
        return
    shown = st.session_state.get("deal_shown", 20)   # drawing every deal each refresh took over a second
    for r in rows[:shown]:
        with st.container(border=True):
            a, b, c = st.columns([1, 3.2, 1.3])
            if r["image"]:
                a.image(big_image(r["image"]), width="stretch")
            with b:
                st.markdown(f"**{esc(r['name'])}** · {esc(r['set_name'] or '')} · {esc(r['number'] or '')}")
                st.html(chip(f"{r['ratio']:.0%} of market", "good" if r["ratio"] <= 0.5 else "info")
                        + chip(r["basis"] or "", "good" if "sold" in (r["basis"] or "") else "")
                        + chip(speed(r["card_key"], r["bucket"]), "") + chip(r["bucket"] or "", "")
                        + chip(r["rarity"] or "", "") + chip(r["era"] or "", "warn" if r["era"] == "vintage" else "")
                        + chip("listed " + fmt_age(r["listed_at"]) if r["listed_at"] else "found " + fmt_age(r["found_at"]), "")
                        + chip(r["location"] or "UK", ""))
                st.markdown(price_line(r["total"], r["market"], r["profit"], r["roi"]), unsafe_allow_html=True)
                st.caption(esc(r["title"]))
            with c:
                st.link_button("Buy on eBay →", r["url"], type="primary", width="stretch")
                bought(r, "deals", r["total"])
                if st.button("Hide", key=f"hide_{r['item_id']}", width="stretch"):
                    db.run("UPDATE deals SET status='hidden' WHERE item_id=?", (r["item_id"],))
                    st.rerun()
    if len(rows) > shown:
        st.button(f"Show 20 more ({len(rows) - shown} more in the feed)", key="deal_more",
                  on_click=lambda: st.session_state.update(deal_shown=shown + 20))


def auction_list(rows):
    st.caption("UK auctions ending soon where the current bid + postage is under your threshold. **Max bid** keeps the "
               "total at or under that line. The app never bids; open the auction to bid yourself.")
    if not rows:
        last = db.setting("last_auction_scan") or {}
        st.info(f"No auctions under the line right now (last check {fmt_age(last.get('at'))}, "
                f"{last.get('checked', 0)} auctions ending soon checked).")
        return
    now = datetime.now(timezone.utc)
    for r in rows:
        mins = (datetime.fromisoformat(r["end_at"]) - now).total_seconds() / 60
        with st.container(border=True):
            a, b, c = st.columns([1, 3.2, 1.3])
            if r["image"]:
                a.image(big_image(r["image"]), width="stretch")
            with b:
                st.markdown(f"**{esc(r['name'])}** · {esc(r['set_name'] or '')} · {esc(r['number'] or '')}")
                st.html(chip(f"ends in {mins:.0f} min" if mins < 90 else f"ends in {mins / 60:.1f} h",
                             "bad" if mins < 15 else "warn" if mins < 60 else "info")
                        + chip(f"{r['bids']} bid{'s' * (r['bids'] != 1)}", "") + chip(r["basis"] or "", "good" if "sold" in (r["basis"] or "") else "")
                        + chip(speed(r["card_key"], r["bucket"]), "") + chip(r["bucket"] or "", "")
                        + chip(r["era"] or "", "warn" if r["era"] == "vintage" else "") + chip(r["location"] or "UK", ""))
                profit, roi = deals.flip(r["bid"] + r["postage"], r["market"])
                st.markdown(f"<span style='font-size:1.4rem;font-weight:700'>{money(r['bid'])}</span> "
                            f"<span style='color:var(--muted)'>current bid + {money(r['postage'])} postage · market "
                            f"{money(r['market'])} · <b style='color:var(--ink)'>max bid {money(r['max_bid'])}</b> · "
                            f"profit at current bid <b style='color:var(--good)'>{money(profit)}</b></span>",
                            unsafe_allow_html=True)
                st.caption(esc(r["title"]))
            with c:
                st.link_button("Open auction →", r["url"], type="primary", width="stretch")
                bought(r, "auctions", r["bid"] + r["postage"])
                if st.button("Hide", key=f"ahide_{r['item_id']}", width="stretch"):
                    db.run("UPDATE auctions SET status='hidden' WHERE item_id=?", (r["item_id"],))
                    st.rerun()


def price_line(total, market, profit, roi):
    colour = "var(--good)" if (profit or 0) > 0 else "var(--bad)"
    return (f"<span style='font-size:1.5rem;font-weight:700'>{money(total)}</span> "
            f"<span style='color:var(--muted)'>delivered · market {money(market)} · profit "
            f"<b style='color:{colour}'>{money(profit)}</b>"
            + (f" ({roi:.0%} ROI)" if roi is not None else "") + "</span>")


def speed(card_key, bucket):
    s = prices.values(card_key).get(bucket or "Raw")
    n = (s or {}).get("n30") or 0
    return f"{n} sold / 30 d" if n else "no UK sales seen yet"


def bought(r, table_, cost):
    with st.popover("Bought", width="stretch"):
        paid = st.number_input("You paid £ (delivered)", 0.0, 100000.0, float(round(cost, 2)), 0.5,
                               key=f"paid_{table_}_{r['item_id']}")
        if st.button("Add to inventory", key=f"inv_{table_}_{r['item_id']}", type="primary"):
            inventory.add(r["card_key"], r["name"], paid, r["set_name"], r["number"], r["bucket"], r["image"], r["url"])
            db.run(f"UPDATE {table_} SET status='bought' WHERE item_id=?", (r["item_id"],))
            st.toast(f"{r['name']} added to Inventory")
            st.rerun()
