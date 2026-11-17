"""Photo pair -> draft listing -> posted eBay listing."""
import json
import re
import string
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import catalog, db, ebay, prices, vision

def target_price(strategy, pct, market, lowest_total, postage):
    """Returns (item_price, basis, warning). Pure, tested."""
    warn = None
    if strategy == "lowest" and lowest_total is None:
        strategy, warn = "market", "No same-condition listing found; priced from market value"
    base = lowest_total if strategy == "lowest" else market
    if base is None:
        return None, None, "No market data: set the price manually"
    return max(round(base * (1 - pct / 100) - postage, 2), 0.99), strategy, warn


def priced(strategy, pct, market, lowest_total):
    """target_price with the postage the price ends up on: letter below `tracked_over`, tracked from it up.
    If taking the dearer tracked postage off drops the price under the line, it stays letter just under it."""
    letter = ebay.postage_for(None)
    price, used, warn = target_price(strategy, pct, market, lowest_total, letter[1])
    if price is not None and ebay.postage_for(price) != letter:
        p2 = target_price(strategy, pct, market, lowest_total, ebay.postage_for(price)[1])[0]
        price = p2 if ebay.postage_for(p2) != letter else round(float(db.setting("tracked_over")) - 0.01, 2)
    return price, used, warn


def warnings(price, market, stats, read, matched):
    w = []
    if market and price and not 0.6 * market <= price + ebay.postage_for(price)[1] <= 1.5 * market:
        w.append(f"Price is far from market value (£{market:.2f})")
    if not stats or stats["n"] < 3:
        w.append("Thin data: fewer than 3 comparable sales/listings for this condition — check the price")
    if read.uncertain_fields:
        w.append("Uncertain: " + ", ".join(read.uncertain_fields))
    if not read.pair_ok:
        w.append("Photo 2 doesn't look like the back of this card — check the pairing")
    if not matched:
        w.append("Card not found in catalog — check name/set/number")
    return w


def title_for(d):
    cond = f"{d['grader']} {d['grade']}" if d["graded"] else d["condition"] or "NM"
    parts = [d["name"], d["number"], d["set_name"], d.get("rarity") or "",
             "Japanese" if d["language"] == "ja" else "", cond, "Pokemon Card"]
    t = re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()
    while len(t) > 80 and d.get("rarity") and d["rarity"] in t:  # drop rarity first, then truncate
        t = t.replace(d["rarity"] + " ", "")
    return t[:80]


def description_for(d, read):
    cond = f"Graded {d['grader']} {d['grade']}" + (f" (cert {d['cert_number']})" if d.get("cert_number") else "") \
        if d["graded"] else {"NM": "Near Mint or better", "LP": "Lightly Played", "MP": "Moderately Played",
                             "HP": "Heavily Played"}[d["condition"] or "NM"]
    notes = "".join(f"<li>{n}</li>" for n in read.condition_notes)
    return (f"<p><b>{d['name']}</b> — {d['number']} — {d['set_name']}"
            f"{' — ' + d['rarity'] if d.get('rarity') else ''} — {'Japanese' if d['language'] == 'ja' else 'English'}</p>"
            f"<p><b>Condition:</b> {cond}</p><p>{read.condition_summary}</p>"
            + (f"<ul>{notes}</ul>" if notes else "") + f"<p>{db.setting('footer')}</p>")


def aspects_for(d):
    return {"Game": "Pokémon TCG", "Card Name": d["name"], "Character": d["name"].split(" ")[0],
            "Set": d["set_name"], "Card Number": d["number"], "Rarity": d.get("rarity"),
            "Language": "Japanese" if d["language"] == "ja" else "English",
            "Manufacturer": "The Pokémon Company", "Graded": "Yes" if d["graded"] else "No",
            "Professional Grader": d["grader"] if d["graded"] else None, "Grade": d["grade"] if d["graded"] else None,
            "Certification Number": d.get("cert_number") if d["graded"] else None,
            "Features": {"reverse_holo": "Reverse Holo", "holo": "Holo", "first_edition": "1st Edition"}.get(d["variant"])}


def cond_key(graded, grader, grade, condition):
    """Pure. What actually defines a card's condition: grader/grade only for slabs, raw condition only for raw cards.
    (The form always shows grader/grade boxes, so their defaults must not count as a change on raw cards.)"""
    return (True, grader, str(grade)) if graded else (False, condition or "NM")


def needs_reprice(old, new_price, changed_card, changed_cond, reprice_clicked):
    """Pure. Recompute the price? Always on 'Save & re-price'; on a card/condition change only if the user
    didn't type their own price in the same save (a typed price always wins)."""
    typed = abs((new_price or 0) - (old or 0)) > 0.004
    return reprice_clicked or ((changed_card or changed_cond) and not typed)


def price_draft(d, strategy, pct):
    """(Re)compute market, lowest listing and price for a draft dict in place.
    Market = median of a live eBay UK search for '<name> <number>' (prices.searched_market); tracked sold/asks
    (prices.market_for) only when that search finds nothing."""
    card_key, lang = d.get("card_key"), d["language"]
    market, stats, basis = (None, None, None)
    first = d["name"].split(" ")[0]
    pool = ThreadPoolExecutor(1)   # the cheapest-listing lookup runs alongside the market lookup below
    low = pool.submit(ebay.lowest_listing, f"{d['name']} {d['number'].split('/')[0]}" + (" japanese" if lang == "ja" else ""),
                      lambda t: prices.title_ok(t, [first], number=d["number"], lang=lang),
                      d["grader"] if d["graded"] else None, d["grade"], d["condition"] or "NM")
    pool.shutdown(wait=False)
    if card_key:
        try:
            prices.refresh(card_key, prices.card_query(d["name"], d["number"], lang, d["variant"]))
        except prices.SoldCompsError as e:
            d.setdefault("errors", []).append(str(e))
        try:
            prices.refresh_card_asks(card_key, d["name"], d["number"], lang)
            prices.infer_sales(card_key)
        except ebay.EbayError as e:
            d.setdefault("errors", []).append(str(e))
        prices.follow(card_key, 3, "draft", d["name"], d["number"])
    grader = d["grader"] if d["graded"] else None
    try:   # market = live eBay search for 'name number' (e.g. 'Charizard ex 183/165')
        market, stats, basis = prices.searched_market(d["name"], d["number"], lang, grader, d["grade"], d["condition"])
    except (ebay.EbayError, ebay.requests.RequestException) as e:   # eBay down/timeout: fall back, don't crash
        d.setdefault("errors", []).append(str(e))
    if market is None and card_key:   # search found nothing: fall back to tracked sold/asks
        market, stats, basis = prices.market_for(card_key, grader, d["grade"], d["condition"])
    lowest = None
    try:
        lowest = low.result()
    except ebay.EbayError as e:
        d.setdefault("errors", []).append(str(e))
    price, used, warn = priced(strategy, pct, market, lowest and lowest["total"])
    d.update(market=market, market_basis=basis, comps_n=stats and stats["n"], lowest=lowest,
             price=price, strategy=used, strategy_warning=warn)
    return stats


def build_draft(front, back, strategy, pct, card=None):
    """card=(lang, card_id) when the user already chose the card (e.g. 'List this card' on the Prices page):
    photos are still read for slab/grade, but the catalog card is the one given."""
    read = vision.read_card(front, back)
    lang = "ja" if read.language == "ja" else "en"
    if card:
        lang = card[0]
        cands = [c for c in [catalog.card(*card)] if c]
        if cands:
            read.name_en = catalog.english_name(lang, cands[0]) or cands[0]["name"]
            read.uncertain_fields = [f for f in read.uncertain_fields if f not in ("name", "number", "set_code")]
    elif getattr(read, "candidate_ids", None) is not None:     # OCR backend already resolved against the catalog
        cands = [c for c in (catalog.card(lang, i) for i in read.candidate_ids) if c]
    else:
        cands = catalog.find(lang, read.name_en, read.number, read.set_code)
    c = cands[0] if cands else None
    d = {"read": read.model_dump(), "candidates": [x["id"] for x in cands], "language": lang,
         "card_key": f"{lang}:{c['id']}" if c else None, "card_id": c and c["id"],
         "image_ref": catalog.image(c) if c else None,
         "name": read.name_en, "number": catalog.printed_number(c) if c else read.number,
         "set_name": (c["set"]["name"] if c and lang == "en" else read.set_name_en) or read.set_code or "",
         "rarity": string.capwords(c.get("rarity") or "") if c else None, "variant": read.variant,
         "graded": read.graded, "grader": read.grader, "grade": read.grade, "cert_number": read.cert_number,
         "condition": None if read.graded else (read.condition or "NM")}
    stats = price_draft(d, strategy, pct)
    d["title"], d["description"], d["aspects"] = title_for(d), description_for(d, read), aspects_for(d)
    d["warnings"] = warnings(d["price"], d["market"], stats, read, bool(c)) + \
        ([d["strategy_warning"]] if d["strategy_warning"] else []) + (["Several catalog matches — pick the right one"] if len(cands) > 1 else [])
    return d


def apply_grid_row(d, title, condition, price, strategy, pct):
    """Apply one review-grid row to a draft dict in place (same rules as the form: typed price wins)."""
    old_price = d.get("price")
    changed_cond = (not d.get("graded")) and (condition or "NM") != (d.get("condition") or "NM")
    d.update(title=title[:80], price=price)
    if not d.get("graded"):
        d["condition"] = condition or "NM"
    if needs_reprice(old_price, price, False, changed_cond, False):
        price_draft(d, strategy, pct)
    d["aspects"] = aspects_for(d)
    return d


def save_new(front, back):
    return db.run("INSERT INTO drafts (front_path, back_path, status, data_json) VALUES (?,?,'processing','{}')",
                  (str(front), str(back)))


def save(draft_id, d, status):
    db.run("UPDATE drafts SET data_json=?, status=? WHERE id=?", (json.dumps(d), status, draft_id))


UNPOSTED = ("draft", "accepted", "failed", "processing")   # never 'posted'/'ended' (on eBay) or 'posting' (mid-post)
_MARKS = ",".join("?" * len(UNPOSTED))


def unposted_count():
    return db.q(f"SELECT COUNT(*) n FROM drafts WHERE status IN ({_MARKS})", UNPOSTED)[0]["n"]


def delete_drafts(uploads_dir):
    """Delete every draft that never reached eBay, and its uploaded photos (only files directly inside uploads_dir).
    Inventory items being listed from one go back to 'owned'. Live and ended listings are untouched.
    Returns how many drafts were deleted."""
    rows = db.q(f"SELECT id, front_path, back_path FROM drafts WHERE status IN ({_MARKS})", UNPOSTED)
    with db.conn() as c:   # one transaction
        c.execute("UPDATE inventory SET status='owned', draft_id=NULL WHERE status='listed' AND draft_id IN "
                  f"(SELECT id FROM drafts WHERE status IN ({_MARKS}))", UNPOSTED)
        c.execute(f"DELETE FROM drafts WHERE status IN ({_MARKS})", UNPOSTED)
    for r in rows:
        for p in (r["front_path"], r["back_path"]):
            if p and Path(p).parent == Path(uploads_dir):
                Path(p).unlink(missing_ok=True)
    return len(rows)


def sync_live_prices():
    """Make the app's price for every posted card equal the price actually on eBay (picks up Seller Hub edits).
    Returns [(draft_id, old, new)] for the ones that changed; listings that ended are marked 'ended'."""
    from concurrent.futures import ThreadPoolExecutor
    rows = db.q("SELECT id, listing_id, data_json FROM drafts WHERE status='posted' AND listing_id IS NOT NULL")
    with ThreadPoolExecutor(4) as ex:
        live = list(ex.map(lambda r: _safe(ebay.listed_price, r["listing_id"]), rows))
    changed = []
    for r, got in zip(rows, live):
        if not got:
            continue
        price, status = got
        d = json.loads(r["data_json"])
        if status and status != "Active":
            db.run("UPDATE drafts SET status='ended' WHERE id=?", (r["id"],))
        if abs((d.get("price") or 0) - price) > 0.004:
            changed.append((r["id"], d.get("price"), price))
            d["price"] = price
            db.run("UPDATE drafts SET data_json=? WHERE id=?", (json.dumps(d), r["id"]))
    return changed


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception:
        return None


def update_live_price(draft_id, price):
    """Set the price on the live eBay listing, then record it (only after eBay accepted it)."""
    ebay.set_price(f"PP-{draft_id}", price)
    d = json.loads(db.q("SELECT data_json FROM drafts WHERE id=?", (draft_id,))[0]["data_json"])
    d["price"] = round(price, 2)
    db.run("UPDATE drafts SET data_json=? WHERE id=?", (json.dumps(d), draft_id))


def revise(draft_id):
    """Push a posted draft's current title, description, item specifics and condition to its live listing
    (post_listing is idempotent by SKU and never re-publishes or re-prices a live offer)."""
    row = db.q("SELECT * FROM drafts WHERE id=?", (draft_id,))[0]
    d = json.loads(row["data_json"])
    d["image_urls"] = d.get("image_urls") or [ebay.upload_image(row["front_path"]), ebay.upload_image(row["back_path"])]
    ebay.post_listing(f"PP-{draft_id}", d, d["image_urls"])
    db.run("UPDATE drafts SET data_json=? WHERE id=?", (json.dumps(d), draft_id))
    return row["listing_id"]


def post(draft_id):
    row = db.q("SELECT * FROM drafts WHERE id=?", (draft_id,))[0]
    if row["status"] == "posted":
        return row["listing_id"]
    d = json.loads(row["data_json"])
    db.run("UPDATE drafts SET status='posting', error=NULL WHERE id=?", (draft_id,))
    try:
        urls = d.get("image_urls") or [ebay.upload_image(row["front_path"]), ebay.upload_image(row["back_path"])]
        d["image_urls"] = urls  # keep, so a retry doesn't re-upload
        listing_id = ebay.post_listing(f"PP-{draft_id}", d, urls)
    except Exception as e:
        db.run("UPDATE drafts SET status='failed', error=?, data_json=? WHERE id=?", (str(e)[:2000], json.dumps(d), draft_id))
        raise
    db.run("UPDATE drafts SET status='posted', listing_id=?, data_json=? WHERE id=?", (listing_id, json.dumps(d), draft_id))
    return listing_id
