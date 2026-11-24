"""List cards: upload photos -> review grid (condition, price, accept in one place) -> post, then live listings."""
import datetime
import json
import os
import re
import string
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import streamlit as st

from core import catalog, db, ebay, inventory, listing, prices, sheet, vision
from ui.common import (STATUS_KIND, cached_pairs, chip, data_uri, env_pills, esc, file_times, header, limits, money, photo, photo_order,
                       section, tiles, upload_thumb)

UPLOADS = db.DATA / "uploads"
CONDITIONS = ["NM", "LP", "MP", "HP"]


def page():
    header("List cards", "Photos in, eBay listings out", env_pills())
    counts = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM drafts GROUP BY status")}
    lim = limits()
    tiles([("To review", counts.get("draft", 0), "drafts waiting", True),
           ("Ready to post", counts.get("accepted", 0) + counts.get("failed", 0), "accepted + retries"),
           ("Live on eBay", counts.get("posted", 0), "listed by this app"),
           ("eBay allowance", lim["items_left"] if lim and lim.get("items_left") is not None else "—",
            f"listings left · {money(lim.get('value_left'))} value" if lim else "connect eBay in Settings")])
    upload_panel()
    review()
    live_listings()


# ---------------------------------------------------------------- 1. upload
@st.cache_data(ttl=3600, show_spinner=False)
def lan_ip():
    """This PC's address on the home network (no traffic is sent: UDP connect only picks the route)."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.168.1.1", 1))
            return s.getsockname()[0]
        except OSError:
            return "this-pc"


def upload_panel():
    st.session_state.setdefault("up_n", 0)
    pre = st.session_state.get("prefill")          # set by 'List this card' on the Prices page
    with st.expander("1 · Upload photos" + (f" for {pre['name']}" if pre else ""),
                     expanded=bool(pre) or not db.q("SELECT 1 FROM drafts WHERE status IN ('draft','accepted') LIMIT 1")):
        if pre:
            c1, c2 = st.columns([4, 1])
            c1.info(f"Listing **{pre['name']}** ({pre['number']}, {pre['set']}). Upload its front and back photos; "
                    "the card is already chosen, the photos are only read for slab/grade.")
            if c2.button("Cancel", key="cancel_prefill"):
                st.session_state.pop("prefill")
                st.rerun()
        st.caption(f"From your phone (same Wi-Fi): open **http://{lan_ip()}:8501** and upload there. Pick the photos "
                   "in the order you took them (front, back, front, back…).")
        saved_at = file_times(key=f"ft{st.session_state.up_n}")
        files = st.file_uploader("Front and back of each card, any order — backs are recognised automatically",
                                 type=["jpg", "jpeg", "png", "webp"], accept_multiple_files=True,
                                 key=f"up{st.session_state.up_n}")
        c1, c2 = st.columns([3, 1])
        strategies = {"lowest": "X% below cheapest same-condition listing (sells fastest)",
                      "market": "X% below market value"}
        cur = db.setting("strategy")
        strategy = c1.radio("Pricing", list(strategies), format_func=strategies.get, horizontal=True,
                            index=list(strategies).index(cur) if cur in strategies else 0)
        pct = c2.number_input("X %", 0.0, 90.0, float(db.setting("pct_below")), 1.0)
        to_sheet = st.checkbox("Add to spreadsheet", bool(db.setting("sheet_add")),
                               help="New cards that aren't in your workbook yet get a row on the Pokemon tab "
                                    "(card name in A, listing price in C). Change it per card in the Sheet column below.")
        files = files or []
        if files and len(files) % 2:
            st.error(f"{len(files)} photos — need an even number (front + back per card).")
            return
        if pre and len(files) > 2:
            st.error("Listing one chosen card: upload exactly 2 photos (front + back).")
            return
        if not files:
            return
        ids = tuple(x.file_id for x in files)
        saved = [saved_at.get(f"{x.name}|{x.size}") for x in files]
        idx, how = cached_pairs((ids, tuple(saved)), [(x.name, x.getvalue()) for x in files], saved)
        auto = [i for pair in idx for i in pair]
        if len(auto) != len(files):   # fronts and backs didn't come out even: the rest go last, for you to place
            st.warning("Couldn't find a back for every front. Arrange the photos below: each card is front, then back.")
            auto += [i for i in range(len(files)) if i not in auto]
        orders = st.session_state.setdefault("photo_orders", {})
        order = orders.get(ids) or auto
        moved = photo_order([{"id": i, "src": data_uri(upload_thumb(files[i].file_id, files[i].getvalue(), 240)),
                              "label": files[i].name} for i in order], key=f"po{st.session_state.up_n}_{hash(ids)}")
        if moved and sorted(moved) == sorted(order) and moved != order:
            orders[ids] = order = moved
        pairs = [(files[order[j]], files[order[j + 1]]) for j in range(0, len(order) - 1, 2)]
        manual = ids in orders
        st.caption(("Your order (dragged). " if manual else f"Order from {how}. ")
                   + "Drag a photo onto another to swap them, or press ⇄ to swap a card's front and back.")
        if st.button(f"Identify & price {len(pairs)} card(s)", type="primary"):
            orders.pop(ids, None)
            db.set_setting("strategy", strategy)
            db.set_setting("sheet_add", to_sheet)
            process(pairs, strategy, pct, (pre["lang"], pre["id"]) if pre else None, to_sheet)


def process(pairs, strategy, pct, card=None, to_sheet=False):
    UPLOADS.mkdir(parents=True, exist_ok=True)
    jobs = []
    for f, b in pairs:
        did = listing.save_new("", "")
        paths = []
        for side, up in (("front", f), ("back", b)):
            p = UPLOADS / f"{did}_{side}{os.path.splitext(up.name)[1].lower()}"
            p.write_bytes(up.getvalue())
            paths.append(p)
        db.run("UPDATE drafts SET front_path=?, back_path=? WHERE id=?", (str(paths[0]), str(paths[1]), did))
        jobs.append((did, *paths))
    inv_id = (st.session_state.get("prefill") or {}).get("inventory_id")   # listing a card from Inventory
    bar, done = st.progress(0.0, "Identifying cards…"), {"ok": 0, "pick": 0, "fail": 0}
    with ThreadPoolExecutor(4) as ex:
        futs = {ex.submit(listing.build_draft, fr, bk, strategy, pct, card): did for did, fr, bk in jobs}
        for i, fut in enumerate(as_completed(futs)):
            did = futs[fut]
            try:
                d = fut.result()
                d["to_sheet"] = to_sheet
                if inv_id:
                    d["inventory_id"] = inv_id
                    inventory.set_listed(inv_id, did)
                listing.save(did, d, "draft")
                done["pick" if len(d.get("candidates") or []) > 1 else "ok"] += 1
            except Exception as e:
                db.run("UPDATE drafts SET status='failed', error=? WHERE id=?", (f"Processing failed: {e}", did))
                done["fail"] += 1
            bar.progress((i + 1) / len(jobs), f"{i + 1}/{len(jobs)} done")
    st.session_state.process_summary = done
    st.session_state.up_n += 1                       # fresh, empty uploader
    st.session_state.pop("prefill", None)
    st.rerun()


# ---------------------------------------------------------------- 2. review grid
@st.cache_data(ttl=86400, show_spinner=False)
def card_label(lang, card_id):
    """'Crown Zenith Galarian Gallery · GG10/GG70 · 2023' for the card picker (TCGdex calls are cached)."""
    c = catalog.card(lang, card_id) or {}
    s = c.get("set") or {}
    return " · ".join(x for x in (s.get("name"), vision.printed_full(c) if c else card_id,
                                  (s.get("releaseDate") or "")[:4]) if x)


def _rows(filter_status, text):
    rows = db.q("SELECT * FROM drafts WHERE status IN ('draft','accepted','failed','posting') ORDER BY id")
    out = []
    for r in rows:
        if filter_status != "All" and r["status"] != filter_status.lower():
            continue
        if text and text.lower() not in (r["data_json"] or "").lower():
            continue
        out.append(r)
    return out


def review():
    section("2 · Review & accept")
    s = st.session_state.pop("process_summary", None)
    if s:
        st.success(f"{s['ok'] + s['pick']} card(s) identified" + (f" · {s['pick']} need you to pick the right card"
                   if s["pick"] else "") + (f" · {s['fail']} failed" if s["fail"] else ""))
    f1, f2, f3 = st.columns([2, 3, 1.3])
    status = f1.segmented_control("Show", ["All", "Draft", "Accepted", "Failed"], default="All", key="flt_status") or "All"
    text = f2.text_input("Filter", placeholder="Search drafts by card, set, number…", label_visibility="collapsed",
                         key="flt_text")
    n_del = listing.unposted_count()
    with f3.popover(f"Delete all drafts ({n_del})", disabled=not n_del, width="stretch"):
        st.write(f"Deletes all **{n_del}** draft(s) that aren't on eBay (to review, accepted, failed or stuck "
                 "processing) together with their uploaded photos. Live listings are not touched. "
                 "This can't be undone.")
        if st.button(f"Yes, delete {n_del} draft(s)", type="primary", key="del_all_drafts"):
            st.toast(f"Deleted {listing.delete_drafts(UPLOADS)} draft(s)")
            st.rerun()
    rows = _rows(status, text)
    broken = [r for r in rows if not r["data_json"] or r["data_json"] == "{}"]
    rows = [r for r in rows if r not in broken]
    for r in broken:
        c1, c2 = st.columns([5, 1])
        c1.error(f"#{r['id']}: {r['error'] or 'still processing'}")
        if c2.button("Delete", key=f"del{r['id']}"):
            db.run("DELETE FROM drafts WHERE id=?", (r["id"],))
            st.rerun()
    if not rows:
        st.caption("Nothing to review. Upload photos above.")
        return
    data = [json.loads(r["data_json"]) for r in rows]
    df = pd.DataFrame([{
        "Photo": data_uri(photo(r["front_path"], 160)), "#": r["id"], "Title": d["title"],
        "Condition": f"{d['grader']} {d['grade']}" if d.get("graded") else (d.get("condition") or "NM"),
        "Market £": d.get("market"), "Cheapest £": (d.get("lowest") or {}).get("total"),
        "Price £": float(d.get("price") or 0), "Buyer pays £": round((d.get("price") or 0) + ebay.postage_for(d.get("price") or 0)[1], 2),
        "Status": r["status"], "Sheet": bool(d.get("to_sheet")), "Accept": r["status"] == "accepted",
        "Checks": len([w for w in d.get("warnings", []) if "photo pairing" not in w or "," in w])}
        for r, d in zip(rows, data)])
    graded_opts = sorted({v for v in df["Condition"] if v not in CONDITIONS})
    edited = st.data_editor(
        df, hide_index=True, key=f"grid_{status}_{text}", width="stretch", row_height=64,
        disabled=["Photo", "#", "Market £", "Cheapest £", "Buyer pays £", "Status", "Checks"],
        column_config={
            "Photo": st.column_config.ImageColumn(width="small"),
            "#": st.column_config.NumberColumn(width="small"),
            "Title": st.column_config.TextColumn(max_chars=80, width="large"),
            "Condition": st.column_config.SelectboxColumn(options=CONDITIONS + graded_opts, width="small",
                                                          help="Raw condition. Slabs: change grade in Details."),
            "Market £": st.column_config.NumberColumn(format="£%.2f", width="small"),
            "Cheapest £": st.column_config.NumberColumn(format="£%.2f", width="small",
                                                        help="Cheapest same-condition UK listing, incl. postage"),
            "Price £": st.column_config.NumberColumn(format="£%.2f", min_value=0.0, step=0.5, width="small",
                                                     help="Exact item price that goes on eBay"),
            "Buyer pays £": st.column_config.NumberColumn(format="£%.2f", width="small",
                                                          help="Price + postage (letter or tracked, see Settings; "
                                                               "private sellers: eBay adds its buyer fee on top)"),
            "Sheet": st.column_config.CheckboxColumn(width="small", help="Add to spreadsheet when posted"),
            "Accept": st.column_config.CheckboxColumn(width="small"),
            "Checks": st.column_config.NumberColumn(width="small", help="Things to double-check, see Details")})
    changed = [(r, d, e) for r, d, (_, e), (_, o) in zip(rows, data, edited.iterrows(), df.iterrows())
               if any(e[k] != o[k] for k in ("Title", "Condition", "Price £", "Sheet", "Accept"))]
    b1, b2, b3 = st.columns([1.3, 1.3, 3])
    if b1.button(f"Save {len(changed)} change(s)", type="primary", disabled=not changed):
        save_grid(changed)
    pending = [r["id"] for r, d in zip(rows, data) if r["status"] == "draft" and (d.get("price") or 0) > 0]
    if b2.button(f"Accept all {len(pending)}", disabled=not pending):
        for did in pending:
            db.run("UPDATE drafts SET status='accepted' WHERE id=?", (did,))
        st.rerun()
    b3.caption("Edit condition, price or title right in the table, tick Accept, then Save. A price you type is "
               "exactly what goes on eBay; changing only the condition re-prices automatically.")
    for r, d in zip(rows, data):
        draft_details(r, d)
    post_bar()


def save_grid(changed):
    strategy, pct = db.setting("strategy"), db.setting("pct_below")
    for r, d, e in changed:
        cond = e["Condition"] if e["Condition"] in CONDITIONS else d.get("condition")
        listing.apply_grid_row(d, e["Title"], cond, float(e["Price £"] or 0), d.get("strategy") or strategy, pct)
        d["to_sheet"] = bool(e["Sheet"])
        listing.save(r["id"], d, "accepted" if e["Accept"] else ("draft" if r["status"] == "accepted" else r["status"]))
    st.toast(f"Saved {len(changed)} draft(s)")
    st.rerun()


def draft_details(r, d):
    did = r["id"]
    kind = STATUS_KIND.get(r["status"], "accent")
    checks = [w.replace("photo pairing (not checked by OCR), ", "").replace("photo pairing (not checked by OCR)", "")
              for w in d.get("warnings", []) + d.get("errors", [])]
    checks = [w for w in checks if w.strip().rstrip(":") != "Uncertain"]
    with st.expander(f"#{did} · {d['title']}" + (f"  ·  ⚠ {len(checks)}" if checks else "")):
        st.html(f'<div class="pp-head">{chip(r["status"], kind)}<span class="pp-muted">Market '
                f'{esc(money(d.get("market")))} ({esc((d.get("market_basis") or "no data").split("· ")[-1])})'
                + (f' · cheapest same condition {esc(money(d["lowest"]["total"]))}' if d.get("lowest") else "")
                + "</span></div>")
        if r["error"]:
            st.error(r["error"])
        for w in checks:
            st.warning(w)
        a, b, c, info = st.columns([1, 1, 1, 3])
        a.image(photo(r["front_path"]) or r["front_path"], caption="front")
        b.image(photo(r["back_path"]) or r["back_path"], caption="back")
        if d.get("image_ref"):
            c.image(d["image_ref"], caption="catalog match")
        slug = re.sub(r"[^a-z0-9]+", "-", f"{d['name']} {d['number']}".lower()).strip("-")   # charizard-ex-183-165
        info.markdown(f"[Open on pokepulse](https://pokepulse.io/cards/{slug})" + (
            f" · [Cheapest same-condition listing]({d['lowest']['url']}) · {money(d['lowest']['total'])}" if d.get("lowest") else ""))
        with info.form(f"f{did}"):
            f1, f2, f3 = st.columns(3)
            cands = d.get("candidates") or []
            opts = [None] + cands
            card_id = f1.selectbox("Catalog card", opts, index=opts.index(d.get("card_id")) if d.get("card_id") in cands else 0,
                                   format_func=lambda i: "— pick the card —" if i is None else card_label(d["language"], i)) \
                if len(cands) > 1 else d.get("card_id")
            graded = f1.checkbox("Graded slab", d["graded"])
            grader = f2.selectbox("Grader", list(ebay.GRADER_IDS), index=list(ebay.GRADER_IDS).index(d["grader"]) if d["grader"] in ebay.GRADER_IDS else 0)
            grade = f2.selectbox("Grade", list(ebay.GRADE_IDS), index=list(ebay.GRADE_IDS).index(str(d["grade"])) if str(d["grade"]) in ebay.GRADE_IDS else 0)
            cert = f3.text_input("Cert number", d.get("cert_number") or "")
            cond = f3.selectbox("Raw condition", CONDITIONS, index=CONDITIONS.index(d["condition"] or "NM"))
            p1, p2 = st.columns(2)
            own = p1.number_input("Market £ (your own figure, e.g. from pokepulse; 0 = eBay solds)",
                                  0.0, 100000.0, float(d.get("own_market") or 0.0), 0.5)
            price = p2.number_input(f"Price £ (item; buyer also pays £{ebay.postage_for(d.get('price'))[1]:.2f} postage)",
                                    0.0, 100000.0, float(d.get("price") or 0.0), 0.5)
            title = st.text_input("Title", d["title"], max_chars=80)
            desc = st.text_area("Description (HTML)", d["description"], height=130)
            s1, s2, s3 = st.columns(3)
            accept = s1.form_submit_button("Save & accept", type="primary")
            reprice = s2.form_submit_button("Save & re-price")
            skip = s3.form_submit_button("Discard")
        if skip:
            db.run("UPDATE drafts SET status='skipped' WHERE id=?", (did,))
            st.rerun()
        if accept or reprice:
            changed_card = card_id and card_id != d.get("card_id")
            changed_cond = listing.cond_key(graded, grader, grade, cond) != \
                listing.cond_key(d["graded"], d["grader"], d["grade"], d["condition"])
            old_price = d.get("price")
            changed_own = abs(own - float(d.get("own_market") or 0)) > 0.004
            d["own_market"] = own or None
            d.update(graded=graded, grader=grader if graded else None, grade=grade if graded else None,
                     cert_number=cert if graded else None, condition=None if graded else cond,
                     price=price, title=title, description=desc)
            if changed_card:
                cc = catalog.card(d["language"], card_id)
                d.update(card_id=card_id, card_key=f"{d['language']}:{card_id}", image_ref=catalog.image(cc),
                         number=catalog.printed_number(cc), rarity=string.capwords(cc.get("rarity") or ""),
                         set_name=cc["set"]["name"] if d["language"] == "en" else d["set_name"])
            if listing.needs_reprice(old_price, price, changed_card, changed_cond or changed_own, reprice):
                listing.price_draft(d, d.get("strategy") or db.setting("strategy"), db.setting("pct_below"))
            d["aspects"] = listing.aspects_for(d)
            listing.save(did, d, "accepted" if accept else "draft")
            st.rerun()


# ---------------------------------------------------------------- 3. post
def post_bar():
    accepted = [r["id"] for r in db.q("SELECT id FROM drafts WHERE status='accepted' OR "
                                      "(status='failed' AND data_json NOT IN ('', '{}'))")]
    section("3 · Post to eBay")
    lim = limits()
    blocked = lim is not None and lim.get("items_left") == 0
    c1, c2 = st.columns([1.3, 4])
    go = c1.button(f"Post {len(accepted)} accepted", type="primary", disabled=not accepted or blocked)
    if blocked:
        c2.warning("eBay's monthly selling limit is used up (0 listings left), so eBay would refuse new listings. "
                   "Drafts stay saved: post them when your limit resets.")
    elif lim and lim.get("items_left") is not None and accepted and len(accepted) > lim["items_left"]:
        c2.warning(f"Only {lim['items_left']} listing(s) left this period; posting stops when eBay refuses the rest.")
    else:
        c2.caption("Posts one by one with a progress bar; you can cancel between cards. Retries never duplicate.")
    if go:
        post_all(accepted)
        limits(fresh=True)
        with st.spinner("Updating your spreadsheet…"):
            sheet.sync_quiet()
        st.rerun()
    show_post_results()


def post_all(ids):
    titles = {r["id"]: json.loads(r["data_json"] or "{}").get("title") for r in
              db.q(f"SELECT id, data_json FROM drafts WHERE id IN ({','.join('?' * len(ids))})", ids)}
    bar, t0, results = st.progress(0.0, f"Posting 0/{len(ids)}…"), time.time(), []
    # Clicking any button stops this run at its next screen update, i.e. after the current card; results are saved
    # after every card, so a cancel keeps what was done.
    st.button("Cancel posting (stops after the current card)", key="cancel_posting")
    st.session_state.post_results = (results, 0.0, len(ids), False)
    for i, did in enumerate(ids, 1):
        try:
            results.append((did, titles.get(did), True, ebay.listing_url(listing.post(did))))
        except Exception as e:
            results.append((did, titles.get(did), False, str(e)[:400]))
        ok = sum(r[2] for r in results)
        el = time.time() - t0
        st.session_state.post_results = (results, el, len(ids), i == len(ids))
        left = el / i * (len(ids) - i)
        bar.progress(i / len(ids), f"Posting {i}/{len(ids)} · {ok} posted · {i - ok} failed · {el:.0f}s"
                                   + (f" · ~{left:.0f}s left" if i < len(ids) else ""))


def show_post_results():
    if "post_results" not in st.session_state:
        return
    results, secs, total, finished = st.session_state.post_results
    ok = [r for r in results if r[2]]
    bad = [r for r in results if not r[2]]
    st.progress(len(results) / max(total, 1), f"Last run: {len(ok)} posted · {len(bad)} failed · {secs:.0f}s"
                + ("" if finished else f" · cancelled, {total - len(results)} not posted (still accepted)"))
    for did, title, _, url in ok:
        st.success(f"#{did} {title or ''} — [view on eBay]({url})")
    for did, title, _, err in bad:
        st.error(f"#{did} {title or ''} — {err}")
    if st.button("Clear results"):
        del st.session_state.post_results
        st.rerun()


# ---------------------------------------------------------------- 4. live
def live_market(d):
    if not d.get("card_key"):
        return None
    try:
        return prices.item_market(d["card_key"], d.get("grader") if d.get("graded") else None, d.get("grade"),
                                  d.get("condition") or "NM")[0]
    except Exception:
        return None


@st.cache_data(ttl=900, show_spinner=False)
def _sync_live():
    """Live prices read from eBay at most every 15 minutes for all sessions (was ~3 s on every new browser session);
    the Sync button and a price push clear it."""
    ended = db.q("SELECT COUNT(*) n FROM drafts WHERE status='ended'")[0]["n"]
    out = listing.sync_live_prices()
    if db.q("SELECT COUNT(*) n FROM drafts WHERE status='ended'")[0]["n"] > ended:   # maybe sold: record the sale
        threading.Thread(target=_record_sales, daemon=True).start()                 # + spreadsheet, off the page
    return out


def _record_sales():
    try:
        prices.sync_own_sales()
        sheet.sync_quiet()
    except Exception:
        pass   # the daily job and Settings → sync sold catch up


def live_listings():
    if not db.q("SELECT 1 FROM drafts WHERE status='posted' LIMIT 1"):
        return
    section("Live on eBay")
    h1, h2 = st.columns([1, 4])
    if h1.button("↻ Sync from eBay"):
        _sync_live.clear()
    try:
        synced = _sync_live()
    except Exception as e:
        synced = []
        h2.warning(f"Couldn't read prices from eBay: {e}")
    posted = db.q("SELECT * FROM drafts WHERE status='posted' ORDER BY id DESC LIMIT 100")
    if synced:
        h2.caption("Picked up from eBay: " + ", ".join(f"#{i} £{o:.2f}→£{n:.2f}" for i, o, n in synced))
    else:
        h2.caption("Prices are the ones actually on eBay, including changes made in Seller Hub.")
    pct, stale_days = float(db.setting("pct_below")), int(db.setting("stale_days"))
    fill = st.session_state.pop("live_fill", False)
    rows, current, today = [], {}, datetime.date.today()
    for p in posted:
        d = json.loads(p["data_json"])
        mkt = live_market(d)
        price = float(d.get("price") or 0)
        postage = ebay.postage_for(price)[1]   # ponytail: assumes the policy matching today's price, not the one posted with
        sugg = listing.priced("market", pct, mkt, None)[0] if mkt else None
        listed = (d.get("posted_at") or p["created_at"] or "")[:10]
        days = (today - datetime.date.fromisoformat(listed)).days if listed else 0
        need, new = listing.stale_check(price, mkt, days, stale_days, pct)
        current[p["id"]] = price
        rows.append({"Photo": data_uri(photo(p["front_path"], 160)), "#": p["id"], "Title": d.get("title"),
                     "Price £": new if fill and new else price, "+ postage": postage, "Market now": mkt,
                     "vs market": (price / mkt - 1) if mkt else None, "Suggested £": new or sugg,
                     "Needs": f"⚠ {need}" if need else "", "Added": listed, "Listing": ebay.listing_url(p["listing_id"])})
    df = pd.DataFrame(rows)
    flagged = sum(bool(r["Needs"]) for r in rows)
    st.caption(f"**Market now** = median eBay sold price without postage (as pokepulse shows it) for the card in its "
               f"condition · **Suggested** = {pct:g} % below market (your default); for a flagged listing (more than "
               f"10 % above market, or unsold after {stale_days} days) it is also at least 5 % below today's price. "
               "Edit **Price £** and press Push to change it on eBay.")
    if flagged and st.button(f"Fill in suggested prices for the {flagged} flagged listing(s)",
                             help="Only fills the table. Nothing changes on eBay until you press Push."):
        st.session_state.live_fill = True
        st.session_state.pop("live_editor", None)
        st.rerun()
    edited = st.data_editor(df, hide_index=True, key="live_editor", width="stretch", row_height=64,
                            disabled=["Photo", "#", "Title", "+ postage", "Market now", "vs market", "Suggested £",
                                      "Needs", "Added", "Listing"],
                            column_config={"Photo": st.column_config.ImageColumn(width="small"),
                                           "Listing": st.column_config.LinkColumn(display_text="Open ↗"),
                                           "Price £": st.column_config.NumberColumn(min_value=0.99, step=0.5, format="£%.2f"),
                                           "+ postage": st.column_config.NumberColumn(format="£%.2f"),
                                           "Market now": st.column_config.NumberColumn(format="£%.2f"),
                                           "Suggested £": st.column_config.NumberColumn(format="£%.2f"),
                                           "vs market": st.column_config.NumberColumn(format="percent",
                                                                                      help="Your total vs market; + = above")})
    changes = [(int(b["#"]), float(b["Price £"])) for _, b in edited.iterrows()
               if abs(current[int(b["#"])] - float(b["Price £"])) > 0.004]
    if st.button(f"Push {len(changes)} price change(s) to eBay", type="primary", disabled=not changes):
        for did, price in changes:
            try:
                listing.update_live_price(did, price)
                st.success(f"#{did} now £{price:.2f} on eBay")
            except Exception as e:
                st.error(f"#{did}: {e}")
        _sync_live.clear()
        st.session_state.pop("live_editor", None)
        with st.spinner("Updating your spreadsheet…"):
            sheet.sync_quiet()
        st.rerun()
