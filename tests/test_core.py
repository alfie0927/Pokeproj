"""Plain-assert checks for the money/pricing logic. Run: .venv\\Scripts\\python tests\\test_core.py"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import ev, listing, prices  # noqa: E402


def test_bucket():
    b = prices.bucket
    assert b("Charizard ex 199/165 PSA 10 Gem Mint Pokemon 151") == "PSA 10"
    assert b("Charizard ex 199/165 PSA GEM MT 10") == "PSA 10"
    assert b("Beckett 9.5 Charizard ex 199/165") == "BGS 9.5"
    assert b("CGC Pristine 10 Charizard ex") == "CGC 10"
    assert b("Charizard ex 199/165 SIR Pokemon 151 NM") == "Raw"
    assert b("Charizard ex 199/165 creased damaged") == "Raw played"
    assert b("Charizard ex 199/165 Lightly Played LP") == "Raw played"
    assert b("Charizard ex 199/165 330 HP card") == "Raw"  # card HP isn't 'heavily played'


def test_clean():
    items = [
        {"item_id": "1", "title": "Charizard PSA 10", "sold_price": {"value": 300}, "shipping": {"value": 5}, "sold_at": "2026-11-01", "flags": []},
        {"item_id": "2", "title": "lot of cards", "sold_price": {"value": 30}, "shipping": None, "sold_at": "2026-11-01", "flags": ["lot"]},
        {"item_id": "3", "title": "Charizard", "sold_price": {"value": 99}, "shipping": {"value": 0}, "sold_at": "2026-11-01", "flags": ["outlier"], "best_offer_price_known": True},
        {"item_id": "4", "title": "Charizard", "sold_price": {"value": 120}, "shipping": {"value": 0}, "sold_at": "2026-11-01", "flags": [], "best_offer_price_known": False},
    ]
    rows = prices.clean(items)
    assert [r[0] for r in rows] == ["1", "3"]  # lot + hidden best offer dropped, mixed-condition outlier kept
    assert rows[0][1] == "PSA 10" and rows[0][4] == 5


def test_summarize():
    today = date(2026, 11, 7)
    sales = [(10.0, "2026-11-06"), (11.0, "2026-11-05"), (12.0, "2026-11-01"), (11.5, "2026-10-20"),
             (10.5, "2026-09-01"), (500.0, "2026-11-04")]  # 500 = typo outlier
    s = prices.summarize(sales, today)
    assert s["n"] == 5 and s["market"] == 11.25 and s["window"] == "30 d", s   # 4 sales in 30 days: their median
    assert s["d7"] == 11.0 and s["last_sold"] == "2026-11-06"
    assert prices.summarize([(5.0, "2026-11-01")], today)["low_confidence"]
    assert prices.summarize([], today) is None
    # Best Offer (true price hidden, <= list): dropped when 3+ clean sales exist, else kept and flagged
    bo = [(10.0, "2026-11-06", 0), (10.0, "2026-11-05", 0), (10.0, "2026-11-04", 0), (14.0, "2026-11-03", 1)]
    s = prices.summarize(bo, today)
    assert s["market"] == 10.0 and s["n"] == 3 and not s["incl_best_offer"]
    s = prices.summarize(bo[:2] + bo[3:], today)
    assert s["n"] == 3 and s["incl_best_offer"]
    # only old sales: still used, flagged stale, window = latest N
    s = prices.summarize([(20.0, "2026-01-01"), (22.0, "2026-02-01"), (24.0, "2026-03-01")], today)
    assert s["stale"] and s["market"] == 22.0 and s["window"] == "last 3"


def test_target_price():
    tp = listing.target_price
    assert tp("market", 10, 100.0, None, 0) == (90.0, "market", None)
    assert tp("lowest", 5, 100.0, 80.0, 1.5) == (74.5, "lowest", None)   # 80*0.95 - 1.5 postage
    p, used, warn = tp("lowest", 5, 100.0, None, 0)
    assert (p, used) == (95.0, "market") and warn                         # no listing -> market fallback
    assert tp("market", 5, None, None, 0)[0] is None                      # no data -> manual
    assert tp("market", 50, 1.0, None, 0.85)[0] == 0.99                   # floor


def test_tracked_postage():
    db = _fresh_db()
    from core import ebay
    for k, v in {"fulfillment_policy": "LETTER", "postage": 1.55, "postage_tracked": 3.5, "tracked_over": 20.0}.items():
        db.set_setting(k, v)
    assert ebay.postage_for(50.0) == ("LETTER", 1.55)                     # no tracked policy chosen: always letter
    db.set_setting("tracked_policy", "TRACKED")
    assert ebay.postage_for(19.99) == ("LETTER", 1.55) and ebay.postage_for(20.0) == ("TRACKED", 3.5)
    pr = lambda m: listing.priced("market", 0, m, None)[0]
    assert pr(15.0) == 13.45                                              # 15 - 1.55 letter
    assert pr(40.0) == 36.5                                               # 40 - 3.50 tracked
    assert pr(22.0) == 19.99                  # 22-3.50 = 18.50 drops under the line: letter, just under it
    assert pr(None) is None


def test_draft_save_keeps_typed_price():
    ck, nr = listing.cond_key, listing.needs_reprice
    # raw card: the form's default grader/grade boxes ('PSA', '10') vs saved None/None is NOT a change
    assert ck(False, "PSA", "10", "NM") == ck(False, None, None, "NM")
    assert ck(False, None, None, "LP") != ck(False, None, None, "NM")          # real raw-condition change
    assert ck(True, "PSA", "10", None) != ck(True, "PSA", "9", None)            # real grade change
    assert not nr(13.03, 11.50, False, False, False)     # typed a price, Save & accept: keep 11.50
    assert nr(13.03, 13.03, False, True, False)          # condition changed, price untouched: re-price
    assert not nr(13.03, 11.50, False, True, False)      # condition changed AND typed price: typed wins
    assert nr(13.03, 11.50, False, False, True)          # 'Save & re-price' clicked: re-price


def test_parse_search():
    from core.catalog import parse_search as ps
    assert ps("charizard 199") == {"lang": "en", "name": "charizard", "number": "199", "set": ""}
    assert ps("sv08 238") == {"lang": "en", "name": "", "number": "238", "set": "sv08"}
    assert ps("199/165")["number"] == "199" and ps("TG05")["number"] == "TG05"
    assert ps("charizard ex 199/165") == {"lang": "en", "name": "charizard ex", "number": "199", "set": ""}
    assert ps("pikachu sv08")["set"] == "sv08" and ps("pikachu sv08")["name"] == "pikachu"
    assert ps("リザードン 201")["lang"] == "ja" and ps("リザードン 201")["number"] == "201"
    assert ps("mew")["name"] == "mew" and ps("mew")["number"] == ""               # 'mew' is a name, not a set
    assert ps("Kingdra TG03/TG30") == {"lang": "en", "name": "Kingdra", "number": "TG03", "set": ""}


def test_xml_first():
    from core import ebay
    xml = ('<GetItemResponse><Ack>Success</Ack><Item><StartPrice currencyID="GBP">9.0</StartPrice>'
           '<SellingStatus><ListingStatus>Active</ListingStatus></SellingStatus></Item></GetItemResponse>')
    assert ebay.xml_first(xml, "StartPrice") == "9.0" and ebay.xml_first(xml, "ListingStatus") == "Active"
    assert ebay.xml_first(xml, "Missing") is None


def test_ev():
    rates = {"SIR": 0.05, "IR": 0.1}
    vals = {"SIR": [100.0, 50.0], "IR": [10.0, 20.0, 30.0]}
    ev_pack, contrib = ev.ev_per_pack(rates, vals, bulk=0.2)
    assert round(ev_pack, 4) == round(0.2 + 0.05 * 75 + 0.1 * 20, 4)  # 5.95
    m = ev.product_metrics(packs=36, price=180.0, ev_pack=ev_pack, promo_value=0)
    assert m["gbp_per_pack"] == 5.0 and m["score"] == 0.95 and m["product_ev"] == 214.2
    assert ev.product_metrics(9, 0, 5.0)["score"] is None


def test_ev_eras():
    from core import catalog
    assert [catalog.era_of(s) for s in ("sv08", "swsh12.5gg", "sm115", "xy7", "SV2a", "zzz")] == ["sv", "swsh", "sm", "xy", "sv", None]
    cfg = {"eras": {"sv": {"bulk_per_pack": 0.15, "rates": {"Rare": 1.0, "Double rare": 0.2}}},
           "sets": {"sv03.5": {"rates": {"Rare": 1.0, "Hyper rare": 0.019}}}}
    assert ev.rates_for("sv08", cfg) == ({"Rare": 1.0, "Double rare": 0.2}, 0.15, {})       # era default
    assert ev.rates_for("sv03.5", cfg)[0] == {"Rare": 1.0, "Hyper rare": 0.019}             # per-set override
    kinds = lambda sid, name, rel="": [k for k, *_ in ev.products_for(sid, name, rel)]
    assert kinds("sv08", "Surging Sparks", "2024-11-08") == ["Booster Box", "ETB", "Bundle"]
    assert kinds("sv03.5", "151", "2023-09-22") == ["ETB", "Bundle"]                        # no booster box
    assert kinds("sm12", "Cosmic Eclipse", "2019-11-01") == ["Booster Box", "ETB"]           # no bundles then
    assert next(p for k, n, p, q in ev.products_for("sm12", "Cosmic Eclipse") if k == "ETB") == 8
    assert next(p for k, n, p, q in ev.products_for("sv08", "Surging Sparks") if k == "ETB") == 9
    assert '"pokemon 151"' in ev.products_for("sv03.5", "151")[0][3]
    assert ev.open_label(1.2) == "Worth opening" and ev.open_label(0.5) == "Avoid opening" and ev.open_label(None) == "No price"
    assert ev.overall(0.8, 12.0) == "Hold / invest" and ev.overall(1.3, 0.0) == "Open it"
    assert ev.overall(0.13, None) == "Don't open"        # e.g. Evolving Skies box: priced for collectors, not opening
    assert ev.overall(0.6, -8.0) == "Avoid"              # poor to open AND price falling
    assert ev.overall(None, 3.0) == "No UK price"
    today = date(2026, 11, 10)
    hist = [("2026-10-20", 100.0), ("2026-11-01", 110.0), ("2026-11-10", 120.0)]
    assert ev.trend30(hist, today) == 20.0
    assert ev.trend30(hist[-2:], today) == 9.1
    assert ev.trend30([("2026-11-08", 100.0), ("2026-11-10", 150.0)], today) is None          # < 7 days of history
    assert ev.kind_of("Surging Sparks Elite Trainer Box") == "ETB"


def test_sealed_filter():
    junk = prices.SEALED_JUNK
    assert not prices.title_ok("Surging Sparks Elite Trainer Box Sleeves Sealed", ["Surging Sparks"], junk)
    assert prices.title_ok("Pokemon Surging Sparks Elite Trainer Box ETB Sealed", ["Surging Sparks"], junk)
    q, inc, exc = prices.parse_query(next(p[3] for p in ev.products_for("sv08", "Surging Sparks") if p[0] == "Bundle"))
    ok = lambda t: prices.sealed_title_ok(t, inc, exc)
    assert ok("Pokémon TCG Scarlet & Violet Surging Sparks Booster Bundle 6 Packs, New Sealed")    # real listing
    assert not ok("Pokemon TCG Costco Surging Sparks ETB & Booster Bundle - New and Sealed")       # combo deal
    assert not ok("2x Surging Sparks English Pokémon Booster Bundle Boxes, New & Sealed")
    assert not ok("Pokemon Surging Sparks Booster Bundle Sealed Box X25")
    assert not ok("Pokémon TCG Sealed Booster Bundle 2-Pack Surging Sparks Etb And Booster Bundle")
    q, inc, exc = prices.parse_query(next(p[3] for p in ev.products_for("sv08", "Surging Sparks") if p[0] == "ETB"))
    assert not prices.sealed_title_ok("Surging Sparks Pokemon Centre Elite Trainer Box SEALED", inc, exc)
    assert prices.sealed_title_ok("Pokemon TCG Surging Sparks Elite Trainer Box - New & Sealed", inc, exc)
    rows = [("a", 8.0), ("b", 11.0), ("c", 95.0), ("d", 99.0), ("e", 105.0)]
    assert [r[0] for r in prices.drop_cheap(rows, 1)] == ["c", "d", "e"]                   # sleeves at £8/£11 dropped
    assert prices.drop_cheap(rows[:2], 1) == rows[:2]                                       # too few to judge


def test_title_ok():
    ok = prices.title_ok
    assert ok("Charizard ex 199/165 SIR Pokemon 151 NM", ["Charizard"], number="199/165", lang="en")
    assert not ok("Charizard ex 199/165 SIR Japanese", ["Charizard"], number="199/165", lang="en")
    assert ok("Charizard ex 201/165 SV2a Japanese SAR", ["Charizard"], number="201/165", lang="ja")
    assert not ok("Charizard ex 201/165 SAR", ["Charizard"], number="201/165", lang="ja")
    assert not ok("Charizard ex 1199/165 custom proxy", ["Charizard"], number="199/165", lang="en")
    assert ok("Pikachu 25/165 Pokemon 151", ["Pikachu"], number="025/165", lang="en")       # unpadded seller
    assert not ok("Pikachu 125/165", ["Pikachu"], number="025/165", lang="en")
    q, inc, exc = prices.parse_query('"151" "booster bundle" sealed -opened -"pokemon center"')
    assert q == "151 booster bundle sealed" and exc == ["opened", "pokemon center"]
    assert ok("Pokemon 151 Booster Bundle Sealed New", inc, prices.SEALED_JUNK + tuple(exc))
    assert not ok("Pokemon 151 Booster Bundle Pokemon Center", inc, prices.SEALED_JUNK + tuple(exc))


def test_ask_bucket():
    ab = prices.ask_bucket
    assert ab({"title": "Charizard ex 199/165 PSA 10", "conditionId": "2750"}) == "PSA 10"
    assert ab({"title": "Charizard ex 199/165 graded slab", "conditionId": "2750"}) is None
    assert ab({"title": "Charizard ex 199/165 NM", "conditionId": "4000"}) == "Raw"
    assert ab({"title": "Charizard ex 199/165 PSA 10 ready", "conditionId": "4000"}) is None


def _fresh_db():
    import tempfile
    from core import db
    db.DB, db._ready = Path(tempfile.mkdtemp()) / "t.db", False
    return db


def test_market_priority():
    db = _fresh_db()
    k = "en:test-1"
    assert prices.market_for(k) == (None, None, "Raw")                                   # no eBay data: no value
    db.many("INSERT INTO asks (item_id, seen_on, card_key, bucket, title, total, url) VALUES (?,?,?,?,?,?,?)",
            [(f"a{i}", "2026-11-07", k, "Raw", "t", v, "") for i, v in enumerate([90.0, 95.0, 100.0])])
    v, st_, basis = prices.market_for(k)
    assert v == 92.5 and st_["kind"] == "asks" and "asks ×3 (low quartile)" in basis, basis   # cheaper asks sell
    assert prices.market_for(k, condition="LP")[0] == 74.0                               # 92.5 × 0.8 (setting)
    assert not prices.solid(st_)                                                         # 3 asks: not enough for deals
    db.many(prices.SALE_INSERT,
            [(f"s{i}", k, "Raw", "t", v, 0, "2026-11-0" + str(i + 1), "", "", "ebay", 0) for i, v in enumerate([80.0, 82.0, 84.0])])
    v, st_, basis = prices.market_for(k)
    assert v == 82.0 and "sold ×3" in basis and "ebay" in basis and prices.solid(st_), basis   # sold beats asks
    assert prices.market_for(k, "PSA", "10")[0] is None                                  # no PSA data
    k2 = "en:test-2"
    db.run(prices.SALE_INSERT, ("t1", k2, "Raw", "t", 30.0, 0, "2026-11-05", "", "", "ebay", 0))
    v, st_, basis = prices.market_for(k2)
    assert v == 30.0 and st_["kind"] == "thin" and "thin: 1 sale" in basis and not prices.solid(st_)
    db.set_setting(f"override:{k}:Raw", {"value": 50.0})
    assert prices.market_for(k) == (50.0, {"kind": "override", "n": None}, "Raw · manual override")
    assert prices.market_for(k, condition="LP")[0] == 40.0


def test_title_junk():
    ok = lambda t: prices.title_ok(t, ["Umbreon"], number="215/203", lang="en")
    assert ok("Umbreon VMAX 215/203 Evolving Skies Secret Rare Full Art Holo")
    assert ok("2021 POKEMON EVOLVING SKIES 215/203 UMBREON VMAX SECRET RARE")
    assert not ok("Umbreon VMAX 215/203 Evolving Skies Extended Art Binder Insert 9-Pocket")
    assert not ok("METAL Card Umbreon VMAX Evolving Skies 215/203 For Display")
    assert not ok("Pokemon Modern Chase Cards Sticker Pack Umbreon 215/203")
    assert not ok("Umbreon VMAX 215/203 Evolving Skies Secret Rare Full Art Holo Spanish")
    assert not ok("Umbreon VMAX 215/163 Fusao de Golpe Ultra Rare Holo 2021")          # same number, other set
    assert prices.title_ok("Mew ex 232/091 Paldean Fates", ["Mew"], number="232/91", lang="en")   # 091 == 91
    assert prices.junk_title("Charizard ex 199/165 Keyring") and not prices.junk_title("Charizard ex 199/165 NM")


def test_played_ratio():
    rows = [(f"c{i}", b, t) for i in range(10) for b, t in (("Raw", 10.0), ("Raw played", 7.0)) for _ in range(3)]
    assert prices.played_ratio_from(rows) == (0.7, 10)
    assert prices.played_ratio_from(rows[:-6]) == (None, 9)                               # 9 cards: not enough yet
    assert prices.played_ratio_from(rows[:5]) == (None, 0)                                # needs 3 of each per card


def test_refresh_plan():
    from datetime import datetime, timedelta, timezone
    db = _fresh_db()
    now = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
    prices.follow("en:a", 1, "scanner", now=now)
    prices.follow("en:b", 3, "watched", now=now - timedelta(days=2))
    prices.follow("en:c", 2, "viewed", now=now)
    prices.follow("en:old", 3, "watched", now=now - timedelta(days=40))                 # unseen 30+ days: dropped
    prices.follow("en:a", 2, "viewed", now=now)                                           # higher priority wins
    prices.follow("en:a", 1, "scanner", now=now)                                          # ...and is kept
    db.run("INSERT INTO fetches VALUES (?,?)", ("ask:en:c", (now - timedelta(hours=1)).isoformat()))   # fresh: skip
    plan = [(r["card_key"], r["priority"]) for r in prices.refresh_plan(now)]
    assert plan == [("en:b", 3), ("en:a", 2)], plan
    assert not db.q("SELECT 1 FROM followed WHERE card_key='en:old'")


def test_judge():
    from datetime import datetime, timezone
    now = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
    auc, fix = {"buying": "AUCTION", "qty_sold": 0}, {"buying": "FIXED_PRICE", "qty_sold": 0}
    ship = {"shippingOptions": [{"shippingCost": {"value": "1.50"}}]}
    assert prices.judge(auc, None, now) == ("gone", None)
    assert prices.judge(auc, {"itemEndDate": "2026-11-10T00:00:00.000Z", "bidCount": 3}, now) == ("active", None)
    assert prices.judge(auc, {"itemEndDate": "2026-11-09T10:00:00.000Z", "bidCount": 0}, now) == ("ended", None)
    assert prices.judge(auc, {"itemEndDate": "2026-11-09T10:00:00.000Z", "bidCount": 5,
                              "currentBidPrice": {"value": "41.00"}, **ship}, now) == ("sold", (41.0, 1.5, "2026-11-09", False))
    sold_out = {"price": {"value": "99.99"}, "itemEndDate": "2026-11-08T20:00:00.000Z",
                "estimatedAvailabilities": [{"estimatedSoldQuantity": 1, "estimatedAvailabilityStatus": "OUT_OF_STOCK"}]}
    assert prices.judge(fix, sold_out, now) == ("sold", (99.99, 0.0, "2026-11-08", False))
    assert prices.judge(fix, {**sold_out, "buyingOptions": ["FIXED_PRICE", "BEST_OFFER"]}, now)[1][3] is True
    seller_ended = {"price": {"value": "99.99"}, "itemEndDate": "2026-11-08T20:00:00.000Z",
                    "estimatedAvailabilities": [{"estimatedSoldQuantity": 0, "estimatedAvailabilityStatus": "IN_STOCK"}]}
    assert prices.judge(fix, seller_ended, now) == ("ended", None)
    still_live = {"price": {"value": "99.99"}, "estimatedAvailabilities": [{"estimatedAvailabilityStatus": "IN_STOCK"}]}
    assert prices.judge(fix, still_live, now) == ("active", None)
    # multi-quantity listing that sold out before its end date: out of stock counts as sold today
    multi = {"price": {"value": "5.00"}, "estimatedAvailabilities": [{"estimatedSoldQuantity": 7, "estimatedAvailabilityStatus": "OUT_OF_STOCK"}]}
    assert prices.judge(fix, multi, now) == ("sold", (5.0, 0.0, "2026-11-09", False))


def test_parse_lines():
    from core import vision
    L = lambda t, cy, h=0.03: (t, 0.5, cy, h)
    p = vision.parse_lines([L("Charizard ex", 0.05, 0.06), L("HP 330", 0.05), L("Burning Darkness", 0.6),
                            L("199/165", 0.95), L("MEW", 0.96), L("Illustration rare", 0.97)])
    assert (p["number"], p["set_codes"], p["name"], p["language"], p["graded"]) == ("199/165", ["MEW"], "Charizard ex", "en", False)
    p = vision.parse_lines([L("Pikachu", 0.05, 0.06), L("HP 60/", 0.05), L("025/", 0.95), L("165", 0.95)])   # split number
    assert p["number"] == "025/165" and p["set_codes"] == []
    p = vision.parse_lines([L("リザードンeX", 0.05, 0.06), L("m330", 0.06), L("G", 0.95), L("sv2a", 0.95), L("201/165SAR", 0.96)])
    assert (p["number"], p["set_codes"], p["language"], p["name"]) == ("201/165", ["SV2a"], "ja", "リザードンeX")   # real OCR output
    p = vision.parse_lines([L("Pikachuex", 0.05, 0.06), L("SSPE", 0.95), L("238/191", 0.95), L("MEWEN", 0.96)])
    assert p["set_codes"] == ["SSPE", "SSP", "MEW"]                                                # language mark stripped
    p = vision.parse_lines([L("PSA", 0.05), L("2023 POKEMON SV EN-151", 0.08), L("#199 CHARIZARD EX", 0.1, 0.02),
                            L("GEM MT 10", 0.12), L("81234567", 0.13), L("Charizard ex", 0.4, 0.05), L("199/165", 0.9)])
    assert (p["graded"], p["grader"], p["grade"], p["cert"], p["number"]) == (True, "PSA", "10", "81234567", "199/165")
    assert vision.parse_lines([L("HP 330", 0.05), L("330/", 0.05)])["number"] is None              # HP is not a number
    # stage labels and evolution lines are never the name, even when OCR makes them the tallest text
    assert vision.parse_lines([L("STAGEZ", 0.03, 0.08), L("Charizard ex", 0.06, 0.05)])["name"] == "Charizard ex"
    assert vision.parse_lines([L("リザードか5進化", 0.03, 0.08), L("リザードンeX", 0.06, 0.05)])["name"] == "リザードンeX"


def test_pick_card():
    from core import vision
    mk = lambda i, local, total, date: {"id": i, "localId": local, "set": {"cardCount": {"official": total}, "releaseDate": date}}
    assert vision.printed_full(mk("x", "TG03", 30, "")) == "TG03/TG30"
    assert vision.printed_full(mk("x", "GG34", 70, "")) == "GG34/GG70"
    assert vision.printed_full(mk("x", "033", 163, "")) == "033/163"
    kingdra = [mk("swsh10tg-TG03", "TG03", 30, "2022-05-27"), mk("swsh5-33", "033", 163, "2021-03-19"),
               mk("neo1-8", "8", 111, "2000-12-16"), mk("dp6-7", "7", 100, "2008-08-20")]
    # real OCR reads from the user's photos, look-alike characters and all
    best, cands = vision.pick_card(kingdra, ["E TG03/1G30020222", "0"])
    assert best["id"] == "swsh10tg-TG03" and cands == [best]
    snorlax = [mk("swsh11tg-TG10", "TG10", 30, "2022-09-09"), mk("swsh11-143", "143", 196, "2022-09-09"),
               mk("ex15-10", "10", 100, "2006-11-01")]
    assert vision.pick_card(snorlax, ["G TGIO/TGBO★"])[0]["id"] == "swsh11tg-TG10"
    mareep = [mk("swsh12.5gg-GG34", "GG34", 70, "2023-01-20"), mk("swsh7-54", "054", 203, "2021-08-27")]
    assert vision.pick_card(mareep, ["EGGB4/GG70★"])[0]["id"] == "swsh12.5gg-GG34"
    best, cands = vision.pick_card(kingdra, ["Pokemon/Nintendo"])        # nothing useful read: no pick, newest first
    assert best is None and [c["id"] for c in cands][:2] == ["swsh10tg-TG03", "swsh5-33"]
    # the first full read already has the gallery number: its bottom lines pick the card with no extra OCR pass
    L = lambda t, cy: (t, 0.5, cy, 0.02)
    lines = [L("Miltank", 0.05), L("Miltankproduces highlynutritious milk,", 0.84), L("M", 0.955), L("GG24/GG70", 0.957),
             L("and", 0.96), L("©2023 Pokemon/Nintendo/Creotures/", 0.975), L("GAMEFREAK", 0.98)]
    assert vision.bottom_reads(lines) == ["M", "GG24/GG70", "and", "©2023 Pokemon/Nintendo/Creotures/", "GAMEFREAK"]
    miltank = [mk("swsh12.5gg-GG24", "GG24", 70, "2023-01-20"), mk("sv04-147", "147", 182, "2023-11-03"),
               mk("swsh10-126", "126", 189, "2022-05-27")]
    assert vision.pick_card(miltank, vision.bottom_reads(lines))[0]["id"] == "swsh12.5gg-GG24"
    assert vision.pick_card(miltank, ["GGPAVGG70★", "1GCPAIGG70★"])[0] is None   # the number bands alone misread it


def test_card_quad():
    import numpy as np
    import cv2
    from core import vision
    img = np.full((2800, 2000, 3), 20, np.uint8)
    rect = ((1000, 1400), (1300, 1800), 8)                               # card-shaped bright box, slightly rotated
    cv2.fillPoly(img, [cv2.boxPoints(rect).astype(np.int32)], (200, 190, 60))
    q = vision.card_quad(img)
    assert q is not None and vision.warp_card(img, q).shape == (2100, 1500, 3)
    assert vision.card_quad(np.full((2800, 2000, 3), 20, np.uint8)) is None


def test_pair_photos():
    import numpy as np
    from PIL import Image
    from core import vision
    rng = np.random.default_rng(1)
    back = rng.integers(0, 255, (64, 48, 3))
    mk = lambda a: Image.fromarray(np.clip(a, 0, 255).astype("uint8"))
    backs = [mk(back * s + rng.normal(0, 12, back.shape)) for s in (0.8, 1.0, 1.1, 0.9, 1.05)]  # exposure + noise
    fronts = [mk(rng.integers(0, 255, back.shape)) for _ in range(5)]
    # the user's real upload order: F B B F B F B F B F (backs before fronts from card 2 on)
    seq = [("a1", fronts[0]), ("a2", backs[0]), ("b1", backs[1]), ("b2", fronts[1]), ("c1", backs[2]),
           ("c2", fronts[2]), ("d1", backs[3]), ("d2", fronts[3]), ("e1", backs[4]), ("e2", fronts[4])]
    pairs = vision.pair_photos(seq)
    assert [(seq[f][0], seq[b][0]) for f, b in pairs] == [("a1", "a2"), ("b2", "b1"), ("c2", "c1"), ("d2", "d1"), ("e2", "e1")]
    # one card: nothing to compare, so filename order decides (natural sort: IMG_9 before IMG_10)
    assert vision.pair_photos([("IMG_10", backs[0]), ("IMG_9", fronts[0])]) == [(1, 0)]


def test_deal_helpers():
    from core import deals
    briefs = [{"id": "a", "name": "Pikachu V"}, {"id": "b", "name": "Pikachu VMAX"}, {"id": "c", "name": "Pikachu"}]
    assert [b["id"] for b in deals.title_candidates("Pokemon Pikachu VMAX 044/185 Vivid Voltage NM", briefs)] == ["b"]
    assert [b["id"] for b in deals.title_candidates("Pikachu 044/185 near mint", briefs)] == ["c"]
    assert [b["id"] for b in deals.title_candidates("Pikachu-V 043/185", briefs)] == ["a"]
    assert deals.title_candidates("Charizard 004/102", briefs) == []
    assert deals.condition_of({"title": "Pikachu 044/185 PSA 10", "conditionId": "2750"}) == ("PSA", "10", None)
    assert deals.condition_of({"title": "Pikachu 044/185 played", "conditionId": "4000"}) == (None, None, "LP")
    assert deals.condition_of({"title": "Pikachu 044/185", "conditionId": "4000"}) == (None, None, "NM")
    assert deals.condition_of({"title": "Pikachu 044/185 PSA 10", "conditionId": "4000"}) is None
    assert all(deals.is_modern(s) for s in ("sv03.5", "swsh7", "sm115", "xy7", "S12a", "SV2a"))
    assert not any(deals.is_modern(s) for s in ("base1", "ex14", "dp1", "bw11", "neo1", None))


def test_judge_qty():
    item = lambda n: {"estimatedAvailabilities": [{"estimatedSoldQuantity": n}]}
    assert prices.judge_qty(None, item(12)) == (12, 0)            # first look: baseline only
    assert prices.judge_qty(12, item(15)) == (15, 3)              # 3 units sold since
    assert prices.judge_qty(15, item(15)) == (15, 0)
    assert prices.judge_qty(15, item(14)) == (14, 0)              # eBay's estimate went down: never negative


def test_sealed_market():
    db = _fresh_db()
    k = "sealed:en:sv03.5:151 Elite Trainer Box"
    db.many("INSERT INTO asks (item_id, seen_on, card_key, bucket, title, total, url) VALUES (?,?,?,?,?,?,?)",
            [(f"a{i}", "2026-11-12", k, "Sealed", "151 ETB", v, "") for i, v in enumerate([100.0, 110.0, 120.0, 130.0])])
    v, st_, basis = prices.market_for(k, bucket="Sealed")
    assert st_["kind"] == "asks" and v == 107.5 and "Sealed ·" in basis, basis            # estimate, no multiplier
    db.many(prices.SALE_INSERT, [(f"s{i}", k, "Sealed", "151 ETB", p, 0, "2026-11-1" + str(i), "", "", "ebay", 0)
                                 for i, p in enumerate([95.0, 98.0, 101.0])])
    v, st_, basis = prices.market_for(k, bucket="Sealed")
    assert st_["kind"] == "sold" and v == 98.0 and "sold ×3" in basis, basis


def test_pick_stock():
    prods = [{"name": "Evolving Skies Booster Box Case", "imageUrl": "case_200w.jpg"},
             {"name": "Evolving Skies Half Booster Box", "imageUrl": "half_200w.jpg"},
             {"name": "Code Card - Evolving Skies Elite Trainer Box [Glaceon]", "imageUrl": "code_200w.jpg"},
             {"name": "Evolving Skies Pokemon Center Elite Trainer Box [Umbreon] (Exclusive)", "imageUrl": "pc_200w.jpg"},
             {"name": "Evolving Skies Elite Trainer Box [Set of 2]", "imageUrl": "two_200w.jpg"},
             {"name": "Evolving Skies Elite Trainer Box [Flareon/Jolteon/Umbreon/Leafeon]", "imageUrl": "etb_200w.jpg"},
             {"name": "Evolving Skies Booster Box", "imageUrl": "box_200w.jpg"}]
    assert ev.pick_stock(prods, "Booster Box") == "box_400w.jpg"
    assert ev.pick_stock(prods, "ETB") == "etb_400w.jpg"
    assert ev.pick_stock(prods, "Bundle") is None
    assert ev._group_key("SWSH07: Evolving Skies") == "evolving skies" and ev._group_key("SM - Team Up") == "team up"
    assert ev._norm("Pokémon GO") == "pokemon go"


def test_rank_order():
    rows = [{"price": None, "ratio": None, "product_ev": 300, "packs": 36},
            {"price": 100, "ratio": 1.2, "product_ev": 120, "packs": 9},
            {"price": 200, "ratio": 1.2, "product_ev": 240, "packs": 36},
            {"price": 100, "ratio": 0.8, "product_ev": 80, "packs": 9}]
    order = sorted(rows, key=ev.rank_key)
    assert [r["product_ev"] for r in order] == [240, 120, 80, 300]   # same ratio: bigger £ profit first; unpriced last


def test_flip_and_max_bid():
    from core import deals
    assert deals.flip(40.0, 100.0, postage_out=1.55, fee_pct=0) == (58.45, 1.46)
    assert deals.flip(40.0, 100.0, postage_out=1.55, fee_pct=10) == (48.45, 1.21)
    assert deals.max_bid(100.0, 0.7, 2.5) == 67.5
    assert deals.max_bid(3.0, 0.5, 2.5) == 0.0


def test_inventory_pnl():
    from core import inventory
    rows = [{"cost": 40.0, "status": "owned", "sold_price": None, "market": 70.0},
            {"cost": 10.0, "status": "listed", "sold_price": None, "market": None},      # no value yet: not in unrealised
            {"cost": 20.0, "status": "sold", "sold_price": 35.0, "market": None}]
    p = inventory.pnl(rows)
    assert p == {"items": 3, "held": 2, "sold": 1, "cost_held": 50.0, "value_held": 70.0, "unrealised": 30.0,
                 "realised": 15.0}, p


def test_word_lists():
    titles = ["charizard ex 199/165 sleeves included", "pikachu sleeve", "umbreon vmax 215/203 near mint", "mew code card",
              "lugia v alt art (not a proxy)", "gengar 24k gold plated", "eevee lottery", "snorlax español 143", ""]
    for t in titles:   # one compiled pattern per list must answer exactly like a search per word
        for words in (prices.TITLE_JUNK, prices.OTHER_LANG, prices.SEALED_JUNK + ("pokemon center",), ()):
            assert prices._has_any(t, words) == any(prices._has(t, w) for w in words), (t, words[:2])


def test_get_items_and_lowest():
    from core import ebay
    real = ebay.get_item, ebay.search_active
    asked = []
    nm = {"conditionDescriptors": [{"name": "Card Condition", "values": [{"content": "Near Mint or Better"}]}]}

    def fake_item(i):
        asked.append(i)
        if i == "bad":
            raise ebay.EbayError("boom")
        return None if i == "gone" else (nm if i in ("c2", "c6") else {"conditionDescriptors": []})
    try:
        ebay.get_item = fake_item
        got = ebay.get_items(["c1", "gone", "bad", "c2"])
        assert got[0] == {"conditionDescriptors": []} and got[1] is None and isinstance(got[2], ebay.EbayError) and got[3] == nm
        assert ebay.get_items([]) == []
        ebay.search_active = lambda *a, **k: [{"itemId": f"c{n}", "title": "Pikachu 25", "price": {"value": str(10 - n)}}
                                              for n in range(1, 8)]          # c7 is cheapest (£3) ... c1 dearest (£9)
        asked.clear()
        low = ebay.lowest_listing("pikachu 25", lambda t: True)
        assert low["total"] == 4.0, low                                    # c6: the cheapest listing that is NM
        assert sorted(asked) == ["c3", "c4", "c5", "c6", "c7"]              # first batch of 5 only
        ebay.search_active = lambda *a, **k: [{"itemId": "x", "title": "t", "price": {"value": "1"}}]
        assert ebay.lowest_listing("q", lambda t: True) is None
    finally:
        ebay.get_item, ebay.search_active = real


def test_ask_daily():
    from datetime import date as d_, timedelta
    db = _fresh_db()
    today, yday = d_.today().isoformat(), (d_.today() - timedelta(days=1)).isoformat()
    k = "en:test-9"
    rows = [(f"y{i}", yday, k, "Raw", "t", v, "") for i, v in enumerate([10.0, 12.0, 14.0])] + \
           [(f"t{i}", today, k, "Raw", "t", v, "") for i, v in enumerate([20.0, 22.0, 24.0, 26.0, 500.0])] + \
           [("p1", today, k, "PSA 10", "t", 90.0, "")]
    db.many("INSERT INTO asks (item_id, seen_on, card_key, bucket, title, total, url) VALUES (?,?,?,?,?,?,?)", rows)
    assert prices._daily_rows([(k, today, "Raw", v) for v in (20.0, 22.0, 24.0, 26.0, 500.0)]) == [(k, today, "Raw", 24.0, 5)]
    prices._ensure_rollup(force=True)                                     # build from stored snapshots
    assert prices.ask_history(k) == [(yday, "Raw", 12.0), (today, "PSA 10", 90.0), (today, "Raw", 24.0)]
    up, down = prices.movers()
    assert [(m[0], m[1], m[2]) for m in up] == [(k, 100.0, 24.0)] and down == []
    assert prices.tracking_stats() == (1, 2)
    db.run("DELETE FROM asks WHERE item_id='t4'")                         # a later snapshot of the same day rewrites it
    prices.rollup_day(k, today)
    assert prices.ask_history(k)[-1] == (today, "Raw", 23.0) and db.q("SELECT n FROM ask_daily WHERE seen_on=? AND bucket='Raw'", (today,))[0]["n"] == 4


def test_infer_sales_asks_once():
    from datetime import datetime, timedelta, timezone
    from core import ebay
    db = _fresh_db()
    now = datetime.now(timezone.utc)
    k, yday = "en:test-7", (now - timedelta(days=1)).isoformat()
    db.many(prices.TRACK_UPSERT, [(i, k, "Raw", "t", 5.0, 1.0, "", "FIXED_PRICE", None, yday, yday) for i in ("live", "sold", "bad")])
    db.run("INSERT INTO fetches VALUES (?,?)", ("ask:" + k, now.isoformat()))      # today's snapshot: none of them in it
    asked, real = [], ebay.get_item

    def fake(i):
        asked.append(i)
        if i == "bad":
            raise ebay.EbayError("timeout")
        av = {"estimatedAvailabilityStatus": "OUT_OF_STOCK", "estimatedSoldQuantity": 1} if i == "sold" else \
            {"estimatedAvailabilityStatus": "IN_STOCK", "estimatedSoldQuantity": 0}
        return {"price": {"value": "5.00"}, "estimatedAvailabilities": [av]}
    try:
        ebay.get_item = fake
        assert prices.infer_sales(k) == {"active": 1, "sold": 1, "error": 1} and sorted(asked) == ["bad", "live", "sold"]
        assert [r["price"] for r in db.q("SELECT price FROM sales WHERE card_key=?", (k,))] == [5.0]
        asked.clear()
        assert prices.infer_sales(k) == {"error": 1} and asked == ["bad"]   # still-live one isn't asked about again today
    finally:
        ebay.get_item = real


def test_delete_drafts():
    import tempfile
    db = _fresh_db()
    up, other = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    for i, status in enumerate(["draft", "accepted", "failed", "processing", "posted", "ended", "skipped", "posting"], 1):
        f = (other if i == 2 else up) / f"{i}_front.jpg"      # draft 2's photo lives outside the uploads folder
        f.write_bytes(b"x")
        db.run("INSERT INTO drafts (id, front_path, back_path, status, data_json) VALUES (?,?,?,?,'{}')", (i, str(f), None, status))
    db.run("INSERT INTO inventory (id, card_key, name, cost, status, draft_id) VALUES (1, 'en:a', 'A', 1, 'listed', 1)")
    db.run("INSERT INTO inventory (id, card_key, name, cost, status, draft_id) VALUES (2, 'en:b', 'B', 1, 'listed', 5)")
    assert listing.unposted_count() == 4 and listing.delete_drafts(up) == 4
    assert [r["status"] for r in db.q("SELECT status FROM drafts ORDER BY id")] == ["posted", "ended", "skipped", "posting"]
    assert sorted(p.name for p in up.iterdir()) == ["5_front.jpg", "6_front.jpg", "7_front.jpg", "8_front.jpg"]
    assert (other / "2_front.jpg").exists()                   # never deletes files outside the uploads folder
    inv = {r["id"]: (r["status"], r["draft_id"]) for r in db.q("SELECT id, status, draft_id FROM inventory")}
    assert inv == {1: ("owned", None), 2: ("listed", 5)}      # back to owned; the live listing's item is untouched
    assert listing.delete_drafts(up) == 0


def test_scan_helpers():
    from core import deals
    a, b = {"id": "sv1-1"}, {"id": "sv1-2"}
    hits = [("i1", a, "en", None), ("i2", b, "en", None), ("i3", a, "en", None), ("i4", a, "ja", None)]
    assert [[h[0] for h in g] for g in deals.by_card(hits)] == [["i1", "i3"], ["i2"], ["i4"]]   # one lookup per card
    take = deals.allowance(2)
    assert [take(), take(), take(), take()] == [True, True, False, False]
    assert deals.pace(3000) == (True, 60) and deals.pace(800) == (True, 180) and deals.pace(0) == (False, 180)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
