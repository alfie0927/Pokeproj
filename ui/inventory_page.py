"""Inventory & profit: what you bought, what it cost, what it's worth now, and what you made."""
import pandas as pd
import streamlit as st

from core import db, inventory
from ui.common import env_pills, header, money, section, tiles

STATUSES = ["owned", "listed", "sold"]


def page():
    header("Inventory", "Cards you bought: cost, value now, and profit", env_pills())
    rows = [dict(r) for r in db.q("SELECT * FROM inventory ORDER BY id DESC")]
    for r in rows:
        r["market"] = inventory.market_now(r) if r["status"] != "sold" else None
    p = inventory.pnl(rows)
    tiles([("Holding", p["held"], f"{p['sold']} sold so far", True),
           ("Cost of holdings", money(p["cost_held"]), "what you paid"),
           ("Value now", money(p["value_held"]), "eBay market, your cards' conditions"),
           ("Unrealised profit", money(p["unrealised"]), "value now − cost (valued cards)"),
           ("Realised profit", money(p["realised"]), "sold price − cost")])
    with st.expander("＋ Add a card manually"):
        st.caption("Easier: press **Bought** on a deal or auction, or **I own this** on a card's Prices page "
                   "(those fill in the card for you).")
        c1, c2, c3 = st.columns([2, 1, 1])
        key = c1.text_input("Card key (e.g. en:sv03.5-199, from the Prices page URL / card view)")
        name = c2.text_input("Name")
        cost = c3.number_input("Cost £", 0.0, 100000.0, 0.0, 0.5)
        if st.button("Add", disabled=not (key and ":" in key and name)):
            inventory.add(key.strip(), name.strip(), cost)
            st.rerun()
    if not rows:
        st.info("No inventory yet. Press **Bought** on a deal in the Deal finder and it lands here with its cost.")
        return
    section("Your cards")
    df = pd.DataFrame([{"id": r["id"], "Photo": r["image"], "Card": f"{r['name']} {r['number'] or ''}".strip(),
                        "Set": r["set_name"], "Condition": r["bucket"], "Bought": r["bought_at"], "Cost £": r["cost"],
                        "Value now £": r["market"],
                        "Profit £": (r["market"] - (r["cost"] or 0)) if r["market"] is not None else
                                    ((r["sold_price"] or 0) - (r["cost"] or 0)) if r["status"] == "sold" else None,
                        "Status": r["status"], "Sold £": r["sold_price"], "Notes": r["notes"] or "",
                        "Source": r["source_url"]} for r in rows])
    m = st.column_config.NumberColumn(format="£%.2f")
    edited = st.data_editor(
        df, hide_index=True, width="stretch", row_height=64, key="inv_editor",
        disabled=["id", "Photo", "Card", "Set", "Condition", "Bought", "Value now £", "Profit £", "Source"],
        column_config={"id": None, "Photo": st.column_config.ImageColumn(width="small"), "Cost £": m,
                       "Value now £": m, "Profit £": m, "Sold £": m,
                       "Status": st.column_config.SelectboxColumn(options=STATUSES, required=True),
                       "Source": st.column_config.LinkColumn(display_text="eBay ↗")})
    c1, c2, c3 = st.columns([1, 2, 1])
    if c1.button("Save changes", type="primary"):
        for (_, a), (_, b) in zip(df.iterrows(), edited.iterrows()):
            if any(a[k] != b[k] and not (pd.isna(a[k]) and pd.isna(b[k])) for k in ("Cost £", "Status", "Sold £", "Notes")):
                sold = None if pd.isna(b["Sold £"]) else float(b["Sold £"])
                db.run("UPDATE inventory SET cost=?, status=?, sold_price=?, notes=?, "
                       "sold_at=CASE WHEN ?='sold' THEN COALESCE(sold_at, date('now')) ELSE sold_at END WHERE id=?",
                       (float(b["Cost £"] or 0), b["Status"], sold, b["Notes"], b["Status"], int(a["id"])))
        st.rerun()
    owned = [r for r in rows if r["status"] == "owned"]
    pick = c2.selectbox("List one on eBay", [None] + [r["id"] for r in owned], label_visibility="collapsed",
                        format_func=lambda i: "Pick a card to list…" if i is None else
                        next(f"{r['name']} {r['number'] or ''} · {r['bucket']}" for r in owned if r["id"] == i))
    if c3.button("List this →", disabled=pick is None, width="stretch"):
        r = next(x for x in owned if x["id"] == pick)
        lang, cid = r["card_key"].split(":", 1)
        st.session_state.prefill = {"lang": lang, "id": cid, "name": r["name"], "number": r["number"] or "",
                                    "set": r["set_name"] or "", "inventory_id": r["id"]}
        st.session_state.goto = "list"
        st.rerun()
