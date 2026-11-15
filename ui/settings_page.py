"""Settings: pricing defaults, postage, eBay policies, item location, data refresh."""
import os

import streamlit as st

from core import db, ebay, prices, vision
from ui.common import env_pills, header, limits, money, section


def page():
    header("Settings", "Pricing defaults, postage, eBay policies and your item location", env_pills())
    missing = [k for k in ("EBAY_APP_ID", "EBAY_CERT_ID") if not os.environ.get(k)]
    if missing:
        st.warning("Missing in .streamlit/secrets.toml: " + ", ".join(missing))
    section("Pricing & listing defaults")
    with st.form("prefs", border=True):
        c1, c2, c3 = st.columns(3)
        vals = {
            "pct_below": c1.number_input("Default X % below", 0.0, 90.0, float(db.setting("pct_below"))),
            "postage": c1.number_input("Postage you charge buyers £", 0.0, 50.0, float(db.setting("postage")),
                                       help="Match your eBay postage policy (0 for free postage). Suggested prices "
                                            "compare totals, so this is taken off the suggested item price."),
            "tracked_over": c1.number_input("Use the tracked policy from item price £", 0.0, 10000.0,
                                            float(db.setting("tracked_over")), 1.0,
                                            help="Only if a tracked postage policy is chosen under eBay business policies."),
            "postage_tracked": c1.number_input("Tracked postage you charge buyers £", 0.0, 50.0,
                                               float(db.setting("postage_tracked")),
                                               help="Match the price in your tracked postage policy."),
            "ebay_username": c1.text_input("Your eBay username", db.setting("ebay_username"),
                                           help="Your own listings are excluded from 'cheapest listing'."),
            "mult_LP": c2.number_input("LP × raw NM value", 0.0, 1.0, float(db.setting("mult_LP"))),
            "mult_MP": c2.number_input("MP ×", 0.0, 1.0, float(db.setting("mult_MP"))),
            "mult_HP": c2.number_input("HP ×", 0.0, 1.0, float(db.setting("mult_HP"))),
            "stale_hours": c3.number_input("Refresh asking prices after (hours)", 1, 720, int(db.setting("stale_hours"))),
            "eur_gbp": c3.number_input("EUR→GBP rate (Cardmarket EU reference only)", 0.5, 1.5, float(db.setting("eur_gbp"))),
            "browse_budget": c3.number_input("eBay Browse calls per day the app may use", 500, 5000,
                                             int(db.setting("browse_budget")), 100,
                                             help="eBay's free limit is 5,000/day. The daily refresh, deal scanner "
                                                  "and 6-hourly checks stop at this number."),
            "postcode": c3.text_input("Item location postcode", db.setting("postcode")),
        }
        vals["footer"] = st.text_area("Description footer", db.setting("footer"))
        if st.form_submit_button("Save settings", type="primary"):
            for k, v in vals.items():
                db.set_setting(k, v)
            st.success("Saved")
    c1, c2 = st.columns(2)
    with c1.container(border=True):
        st.markdown("**eBay business policies**")
        if st.button("Load my policies from eBay"):
            try:
                st.session_state.policies = ebay.policies()
            except Exception as e:
                st.error(str(e))
        pol = st.session_state.get("policies")
        if pol:
            with st.form("pol", border=False):
                picks = {}
                for kind in ("fulfillment", "payment", "return"):
                    opts = pol[kind]
                    cur = db.setting(f"{kind}_policy")
                    names = list(opts)
                    idx = next((i for i, n in enumerate(names) if opts[n] == cur), 0)
                    picks[kind] = st.selectbox({"fulfillment": "Postage"}.get(kind, kind.title()), names, index=idx) if names else None
                tracked_opts = ["(none)"] + list(pol["fulfillment"])
                cur = db.setting("tracked_policy")
                tracked = st.selectbox(f"Tracked postage, from £{float(db.setting('tracked_over')):g}", tracked_opts,
                                       index=next((i for i, n in enumerate(tracked_opts) if pol["fulfillment"].get(n) == cur), 0),
                                       help="Optional. Listings at or above the price set under Pricing & listing "
                                            "defaults use this policy; cheaper ones use Postage above.")
                if st.form_submit_button("Use these policies"):
                    for kind, n in picks.items():
                        if n:
                            db.set_setting(f"{kind}_policy", pol[kind][n])
                    db.set_setting("tracked_policy", pol["fulfillment"].get(tracked, ""))
                    st.success("Saved")
    with c2.container(border=True):
        st.markdown("**Item location & account**")
        if st.button("Create / check inventory location"):
            try:
                ebay.ensure_location(db.setting("location_key"), db.setting("postcode"))
                st.success("Location ready")
            except Exception as e:
                st.error(str(e))
        lim = limits(fresh=st.button("Check selling limits"))
        if lim:
            st.caption(f"Listings left this period: **{lim.get('items_left')}** · value left {money(lim.get('value_left'))}")
    section("Data")
    c1, c2 = st.columns(2)
    with c1.container(border=True):
        st.markdown("**Daily refresh**")
        st.caption("Snapshots UK prices, checks which followed listings sold, refreshes sealed EV. Schedule "
                   "`refresh_daily.py` (see README) so it runs while the app is closed. Last run: "
                   + (db.setting("last_refresh", "") or "never")[:16].replace("T", " "))
        if st.button("Run daily refresh now"):
            import refresh_daily
            with st.spinner("Refreshing… (a few minutes)"):
                try:
                    refresh_daily.main()
                    st.success("Done")
                except Exception as e:
                    st.error(str(e))
    with c2.container(border=True):
        st.markdown("**Your own sales**")
        st.caption("Adds cards you sold through this app to the sold-price history (needs the sell.fulfillment scope).")
        if st.button("Sync my sold orders"):
            try:
                st.success(f"Added {prices.sync_own_sales()} of your sales")
            except Exception as e:
                st.error(str(e))
    section("eBay price data")
    c1, c2 = st.columns(2)
    with c1.container(border=True):
        st.markdown("**Followed cards & sales**")
        pri = {r["priority"]: r["n"] for r in db.q("SELECT priority, COUNT(*) n FROM followed GROUP BY priority")}
        week = db.q("SELECT source, COUNT(*) n FROM sales WHERE sold_at >= date('now', '-7 day') GROUP BY source")
        st.caption(f"Browse calls today **{prices.calls_today():,} / {int(db.setting('browse_budget')):,}** · followed: "
                   f"**{pri.get(3, 0)}** watched/draft/deal (every 6 h), **{pri.get(2, 0)}** viewed, "
                   f"**{pri.get(1, 0)}** seen by the scanner (daily, as budget allows)")
        st.caption("Sales confirmed in the last 7 days: "
                   + (", ".join(f"**{r['n']}** {r['source']}" for r in week) or "none yet: they appear as followed "
                      "listings end (the daily refresh checks them)"))
        lp, n = prices.played_ratio_from([(r["card_key"], r["bucket"], r["t"]) for r in db.q(
            "SELECT card_key, bucket, price+shipping t FROM sales WHERE bucket IN ('Raw', 'Raw played')")])
        st.caption(f"Played (LP) multiplier: **{lp}** learned from {n} cards" if lp else
                   f"Played (LP) multiplier: Settings value until 10 cards have 3+ raw and 3+ played sales ({n} so far)")
    with c2.container(border=True):
        st.markdown("**Real sold prices (Marketplace Insights)**")
        st.caption("eBay's sold-items API gives true UK sold prices (90 days) but is restricted: apply for access "
                   "on developer.ebay.com (your keyset: request Marketplace Insights / Application Growth Check). "
                   "Once approved it's used automatically, ahead of inferred sales.")
        denied = db.setting("insights_denied_at")
        st.caption("Status: " + (f"not approved (checked {denied[:16].replace('T', ' ')}; retried weekly)"
                                 if denied else "not checked yet / working"))
        if st.button("Test Marketplace Insights access"):
            ok, msg = prices.test_insights()
            (st.success if ok else st.warning)(msg)
    st.caption(f"Card reading: **{vision.backend()}** — set VISION_BACKEND = \"claude\" + ANTHROPIC_API_KEY in "
               "secrets for AI identification and condition grading.")
