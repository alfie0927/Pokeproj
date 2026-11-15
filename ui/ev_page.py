"""Sealed EV ranking: each Booster Box / ETB / Bundle's recent UK sold price against the value of opening it."""
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from core import db, ev
from ui.common import LABEL_KIND, chip, env_pills, esc, header, money, section, tiles

ERAS = {"All eras": None, "Scarlet & Violet": "sv", "Sword & Shield": "swsh", "Sun & Moon": "sm", "XY": "xy"}
KINDS = [("Booster Boxes", "Booster Box"), ("Elite Trainer Boxes", "ETB"), ("Booster Bundles", "Bundle")]
SHOW_LABEL = {"No UK price": "Awaiting UK sales"}
LABEL_HELP = {
    "Invest or open": "Worth more opened than sealed, and the sealed price is rising.",
    "Hold / invest": "Sealed price rising over 30 days: collectors are paying up.",
    "Open it": "Cards inside are worth clearly more than the price (EV ≥ 1.15× price).",
    "Fair": "Cards inside are worth about the price.",
    "Don't open": "Worth more sealed than opened: a collector product.",
    "Avoid": "Poor value to open and the sealed price is falling.",
    "No UK price": "Not enough UK sales or listings yet to price it."}


def page():
    header("Sealed EV", "Booster boxes and ETBs ranked by value: recent UK sold price vs what the cards inside are worth",
           env_pills())
    n = db.q("SELECT COUNT(*) n, MAX(updated_at) at FROM products")[0]
    c1, c2 = st.columns([3, 1])
    era = c1.segmented_control("Era", list(ERAS), default="All eras", key="ev_era") or "All eras"
    if c2.button("↻ Refresh sealed data", width="stretch", type="primary" if not n["n"] else "secondary"):
        refresh()
    if not n["n"]:
        st.info("No sealed data yet. Press **Refresh sealed data**: the first run values every hit card in ~60 sets "
                "and prices every Booster Box, ETB and Bundle (a few minutes). After that it refreshes daily.")
        return
    st.caption(f"Updated {fmt_age(n['at'])} · **Price** = median of recent UK eBay sales (`sold ×N`), or a UK estimate "
               "from the cheaper current listings (`est.`) until 3 sales are confirmed · **EV** = expected value of "
               "the cards inside, from estimated pull rates (data/pull_rates.toml) and UK card prices.")
    want = ERAS[era]
    for tab, (_, kind) in zip(st.tabs([t for t, _ in KINDS]), KINDS):
        with tab:
            ranking(kind, want)
    set_details(want)


def refresh():
    bar = st.progress(0.0, "Starting…")
    n = ev.refresh_all(progress=lambda i, total, name: bar.progress(i / max(total, 1), f"{i}/{total} · {name}"),
                       max_age_hours=0)
    bar.progress(1.0, f"Refreshed {n} set(s)")
    st.rerun()


def fmt_age(iso):
    if not iso:
        return "never"
    mins = (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() / 60
    return "just now" if mins < 2 else f"{mins:.0f} min ago" if mins < 90 else f"{mins / 60:.0f} h ago" if mins < 2880 \
        else f"{mins / 1440:.0f} days ago"


def ranking(kind, era):
    rows = ev.ranked(kind, era)
    if not rows:
        st.caption("No products of this kind for the selected era.")
        return
    priced = [r for r in rows if r["price"]]
    best = priced[0] if priced else None
    tiles([("Ranked", len(priced), f"{len(rows) - len(priced)} awaiting UK sales", True),
           ("Best value", best["set_name"] if best else "—", f"{best['ratio']:.2f}× EV ÷ price" if best else ""),
           ("Sold-price backed", sum(r["basis"] == "sold" for r in priced), "3+ recent UK sales; rest are est."),
           ("Worth opening", sum(r["ratio"] >= 1.15 for r in priced), "EV ≥ 1.15× price")])
    if st.toggle("Table view", False, key=f"tbl{kind}{era}"):
        table(priced + [r for r in rows if not r["price"]])
        return
    st.html("".join(row_html(i, r) for i, r in enumerate(priced, 1)))
    unpriced = [r for r in rows if not r["price"]]
    if unpriced:
        with st.expander(f"Awaiting UK sales ({len(unpriced)})"):
            st.html("".join(row_html(None, r) for r in unpriced))


def row_html(rank, r):
    ratio, profit = r["ratio"], r["profit"]
    colour = "var(--good)" if (ratio or 0) >= 1.15 else "var(--info)" if (ratio or 0) >= 0.95 else \
        "var(--warn)" if (ratio or 0) >= 0.6 else "var(--bad)"
    img = (f'<img src="{esc(r["image"])}">' if r["image"] else
           f'<img class="logo" src="{esc(r["logo"])}">' if r["logo"] else '<span class="pp-muted">—</span>')
    label = r["label"]
    basis = (f'sold ×{r["sold_n"]}' if r["basis"] == "sold" else "est." if r["basis"] == "est" else
             "manual" if r["basis"] == "manual" else "")
    share = r["ebay_share"]
    trend = f' · trend {r["trend30"]:+.0f}%' if r["trend30"] is not None else ""
    return f"""<div class="pp-rank{' top' if rank == 1 else ''}">
      <div class="n">{f'#{rank}' if rank else '–'}</div>
      <div class="img">{img}</div>
      <div class="info">
        <div class="nm">{esc(title(r))}</div>
        <div class="sub">{esc(r["set_name"])} · {esc((r["release"] or "")[:4])} · {r["packs"]} packs</div>
        <div style="margin-top:6px">{chip(SHOW_LABEL.get(label, label), LABEL_KIND.get(label, "accent"))}
          <span class="pp-muted" title="Share of the EV priced from eBay data (the rest uses Cardmarket until eBay
          data arrives)">EV {f"{share:.0%}" if share is not None else "–"} eBay-priced{trend}</span></div>
      </div>
      <div class="col"><div class="k">UK price</div><div class="v">{money(r["price"])}</div>
        <div class="s">{chip(basis, "good" if r["basis"] == "sold" else "") if basis else ""}</div></div>
      <div class="col"><div class="k">EV if opened</div><div class="v">{money(r["product_ev"])}</div>
        <div class="s">{money(r["ev_pack"])} / pack</div></div>
      <div class="col"><div class="k">Profit if opened</div>
        <div class="v" style="color:{'var(--good)' if (profit or 0) >= 0 else 'var(--bad)'}">{
            ('+' if profit >= 0 else '−') + money(abs(profit)) if profit is not None else '—'}</div>
        <div class="s">{money(r["price"] / r["packs"]) + " / pack" if r["price"] else ""}</div></div>
      <div class="col ratio"><div class="k">EV ÷ price</div><div class="v">{f"{ratio:.2f}×" if ratio else "—"}</div>
        <div class="pp-bar"><div style="width:{min((ratio or 0) / 1.5, 1) * 100:.0f}%;background:{colour}"></div></div></div>
    </div>"""


def title(r):
    """'Elite Trainer Box' from sets.toml -> '151 Elite Trainer Box'."""
    return r["name"] if r["set_name"].lower() in r["name"].lower() else f"{r['set_name']} {r['name']}"


def table(rows):
    df = pd.DataFrame([{"#": i if r["price"] else None, "Photo": r["image"] or r["logo"], "Product": title(r),
                        "Verdict": SHOW_LABEL.get(r["label"], r["label"]), "UK price": r["price"],
                        "Price basis": (f"sold ×{r['sold_n']}" if r["basis"] == "sold" else r["basis"] or ""),
                        "EV if opened": r["product_ev"], "Profit if opened": r["profit"], "EV ÷ price": r["ratio"],
                        "EV/pack": r["ev_pack"], "£/pack": (r["price"] / r["packs"]) if r["price"] else None,
                        "EV eBay-priced": r["ebay_share"], "Released": (r["release"] or "")[:10]}
                       for i, r in enumerate(rows, 1)])
    m = st.column_config.NumberColumn(format="£%.2f")
    st.dataframe(df, hide_index=True, width="stretch", row_height=64,
                 column_config={"Photo": st.column_config.ImageColumn(width="small"), "UK price": m,
                                "EV if opened": m, "Profit if opened": m, "EV/pack": m, "£/pack": m,
                                "EV ÷ price": st.column_config.NumberColumn(format="%.2f×"),
                                "EV eBay-priced": st.column_config.NumberColumn(format="percent")})


def set_details(era):
    sets = db.q("SELECT DISTINCT set_id, set_name, release FROM products" + (" WHERE era=?" if era else "")
                + " ORDER BY release DESC", (era,) if era else ())
    if not sets:
        return
    section("Inside a set")
    pick = st.selectbox("Set", [s["set_id"] for s in sets], key="ev_set",
                        format_func=lambda sid: next(f"{s['set_name']} ({(s['release'] or '')[:4]})" for s in sets if s["set_id"] == sid))
    info = db.setting(f"ev:{pick}") or {}
    if not info:
        return
    c1, c2 = st.columns([2, 3])
    with c1:
        tiles([("EV per pack", money(info.get("ev_pack")), "raw NM values of the hit cards", True),
               ("eBay-priced", f"{info['ebay_share']:.0%}" if info.get("ebay_share") is not None else "—",
                "of the EV; rest Cardmarket for now")])
        contrib = {k: v for k, v in (info.get("contrib") or {}).items() if v > 0.005}
        if contrib:
            st.bar_chart(pd.Series(contrib, name="£ per pack").sort_values(), horizontal=True, color="#7C5CFF")
    with c2:
        st.markdown("**Chase cards**")
        top = pd.DataFrame(info.get("top") or [])
        if not top.empty:
            st.dataframe(top[["card", "rarity", "value", "source"]], hide_index=True, width="stretch",
                         column_config={"value": st.column_config.NumberColumn("Value", format="£%.2f")})
