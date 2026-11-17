"""Sealed product EV: every English set back to XY (TCGdex series) + data/sets.toml extras.
Pull rates = estimates from data/pull_rates.toml; card values = eBay (sold > UK asks), Cardmarket only as a
labelled stand-in until a card has eBay data; product price = recent UK sold median (else a marked UK estimate);
product photo = the stock catalogue image (TCGCSV mirror of TCGplayer). Cached in `products`, refreshed daily."""
import re
import statistics
import tomllib
import unicodedata
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import requests

from . import catalog, db, ebay, prices

TCGCSV = "https://tcgcsv.com/tcgplayer/3"          # free daily mirror of TCGplayer's Pokémon catalogue
UA = {"User-Agent": "pokeproj-personal/1.0"}
STOCK_WANT = {"Booster Box": "booster box", "ETB": "elite trainer box", "Bundle": "booster bundle"}
STOCK_SKIP = ("case", "half", "set of", "code card", "pokemon center", "sleeved", "art bundle", "display",
              "build & battle", "build and battle", "bundle of", "exclusive")
GROUP_ALIASES = {"sun and moon": "sm base set"}

SETS_FILE = db.DATA / "sets.toml"
RATES_FILE = db.DATA / "pull_rates.toml"
ERA_ORDER = ["me", "sv", "swsh", "sm", "xy"]
SKIP_SETS = {"xyp", "smp", "swshp", "svp", "sve", "mfb", "fut2020", "cel25cc", "sma", "dc1", "xya", "swsh4.5sv",
             "swsh12.5gg", "swsh9tg", "swsh10tg", "swsh11tg", "swsh12tg", "det1", "g1", "xy0", "mee", "mep", "30th-c"}
# Sets sold only as ETBs / bundles / special collections: no 36-pack booster box
NO_BOOSTER_BOX = {"sm3.5", "sm7.5", "sm115", "swsh3.5", "swsh4.5", "swsh10.5", "swsh12.5", "cel25", "sv03.5",
                  "sv04.5", "sv06.5", "sv08.5", "sv10.5b", "sv10.5w", "xy4.5", "30th"}
# Series names in listing titles; one from another era means another product ('Mega Evolutions Chaos Rising
# Booster Box' is not XY Evolutions). 'Mega Evolution' is both the series and its first set (me01).
SERIES = {"sm": ("sun & moon", "sun and moon"), "swsh": ("sword & shield", "sword and shield", "sword shield"),
          "sv": ("scarlet & violet", "scarlet and violet", "scarlet violet"), "me": ("mega evolution", "mega evolutions")}


def _word_in(a, b):
    return re.search(rf"(?<!\w){re.escape(a)}(?!\w)", b) is not None


def conflicts(set_name, era, names):
    """Pure. Title phrases that mean a different product: any other set's name ('Darkness Ablaze' in a 'Sword and
    Shield ETB' title, 'Prismatic Evolutions' for 'Evolutions'), except names that are series names or part of this
    one, plus other eras' series names. Japanese sets: only longer names containing this one."""
    n = set_name.lower()
    if era not in ERA_ORDER:
        return [o.lower() for o in names if o.lower() != n and _word_in(n, o.lower())]
    series = {w for ws in SERIES.values() for w in ws} | {"xy"}
    # rstrip('s'): 'Celebrations' is not a conflict for '30th Celebration' (sellers write both forms)
    out = [o.lower() for o in names if o.lower() not in series | {n}
           and not (_word_in(o.lower(), n) or _word_in(o.lower().rstrip("s"), n))]
    return out + [w for e, ws in SERIES.items() if e != era for w in ws]


@lru_cache(maxsize=1)
def _set_names():
    return tuple(s["name"] for s in all_sets())


# Products outside the tracked eras whose titles contain a tracked set's name
EXTRA_CONFLICTS = {"cel25": ("30th", "30 celebrations")}   # 2026 '30th Celebration(s)' vs 2021 Celebrations
NO_BUNDLE = {"cel25"}   # 2021 Celebrations had no Booster Bundle (the 2026 30th set does)


def excludes_for(s):
    """Title phrases that mean another product than set `s`'s: see conflicts(), plus EXTRA_CONFLICTS.
    One Piece: every other set code in both spellings ('OP10', 'OP-10')."""
    if s.get("game") == "onepiece":
        return [c for o in op_sets() if o["id"] != s["id"] for c in (o["id"], f"{o['id'][:2]}-{o['id'][2:]}")]
    return conflicts(s["name"], s.get("era") or catalog.era_of(s["id"]), _set_names()) + list(EXTRA_CONFLICTS.get(s["id"], ()))


def load_sets():
    with open(SETS_FILE, "rb") as f:
        return tomllib.load(f)["set"]


def load_rates():
    with open(RATES_FILE, "rb") as f:
        return tomllib.load(f)


def rates_for(set_id, cfg=None, era=None):
    """Pure. (rates, bulk_per_pack, extra_sets) for a set: per-set override, else its era's defaults."""
    cfg = cfg or load_rates()
    era = era or catalog.era_of(set_id)
    base = cfg.get("eras", {}).get(era or "", {})
    over = cfg.get("sets", {}).get(set_id, {})
    return (over.get("rates") or base.get("rates", {}), over.get("bulk_per_pack", base.get("bulk_per_pack", 0.1)),
            over.get("extra_sets", {}))


def products_for(set_id, set_name, release=""):
    """Pure. Default sealed products for an English set: (kind, name, packs, ebay_query)."""
    era = catalog.era_of(set_id) or "sv"
    name = set_name.replace("&", "and") if "&" in set_name else set_name
    q = f'"{name}"' if not name.isdigit() else f'"pokemon {name}"'
    base_ex = " sealed -japanese -korean -chinese -case -opened -empty -resealed -costco"
    out = []
    # each product excludes titles naming another product type (combo deals like "ETB & Booster Bundle")
    if set_id not in NO_BOOSTER_BOX and not set_id.endswith(".5"):
        out.append(("Booster Box", f"{set_name} Booster Box", 36,
                    f'{q} "booster box"{base_ex} -half -"elite trainer" -etb -bundle'))
    out.append(("ETB", f"{set_name} Elite Trainer Box", 9 if era in ("sv", "me") else 8,
                f'{q} "elite trainer box"{base_ex} -centre -center -"booster bundle" -"booster box"'))
    if (era in ("sv", "me") or (era == "swsh" and release >= "2021")) and set_id not in NO_BUNDLE:
        out.append(("Bundle", f"{set_name} Booster Bundle", 6,
                    f'{q} "booster bundle"{base_ex} -"elite trainer" -etb -"booster box"'))
    return out


def era_sets(lang="en"):
    """All booster-pack sets of the four English eras, newest first: [{id, name, era, logo, release}]."""
    out = []
    for era in ERA_ORDER:
        for s in catalog.series_sets(era, lang):
            if s["id"] in SKIP_SETS:
                continue
            full = catalog.set_(lang, s["id"]) or {}
            out.append({"id": s["id"], "name": s["name"], "era": era, "logo": catalog.set_logo(full or s),
                        "release": full.get("releaseDate", ""), "lang": lang})
    return sorted(out, key=lambda s: s["release"], reverse=True)


# ---------- card values (cached) ----------

def card_value(lang, brief, use_ebay=False, max_age_days=1):
    """(gbp, source) for a raw NM copy. Cached in card_values; sources: tracked sales > UK asks > Cardmarket."""
    key = f"{lang}:{brief['id']}"
    r = db.q("SELECT value, source, at FROM card_values WHERE card_key=?", (key,))
    if r and datetime.fromisoformat(r[0]["at"]) > datetime.now(timezone.utc) - timedelta(days=max_age_days):
        return r[0]["value"], r[0]["source"]
    c = catalog.card(lang, brief["id"])
    if not c:
        return 0.0, "none"
    name = catalog.english_name(lang, c)
    if use_ebay and name:
        try:
            prices.refresh_card_asks(key, name, catalog.printed_number(c), lang)
        except ebay.EbayError:
            pass
    v, _, basis = prices.market_for(key)
    src = basis.split("· ")[-1] if v else "none"
    if not v:   # no eBay data for this card yet: EU reference so the set isn't valued at £0 (labelled in the UI)
        v = catalog.cardmarket_gbp(c, db.setting("eur_gbp"))
        src = "Cardmarket (no eBay data yet)" if v else "none"
    if (v or 0) >= 5:   # cards under £5 hold ~7 % of EV: not worth Browse budget
        prices.follow(key, 2 if v >= 20 else 1, "sealed EV hit card")
    db.run("INSERT OR REPLACE INTO card_values VALUES (?,?,?,?)", (key, v or 0.0, src, datetime.now(timezone.utc).isoformat()))
    return (v or 0.0), src


def ev_per_pack(rates, values_by_rarity, bulk):
    """rates: {rarity: expected cards per pack}; values_by_rarity: {rarity: [gbp,...]}. Pure, tested."""
    contrib = {r: p * (sum(values_by_rarity.get(r) or [0]) / max(len(values_by_rarity.get(r) or []), 1))
               for r, p in rates.items()}
    return bulk + sum(contrib.values()), contrib


def product_metrics(packs, price, ev_pack, promo_value=0.0):
    ev = packs * ev_pack + promo_value
    per_pack = price / packs if price else None
    return {"product_ev": round(ev, 2), "price": price, "ev_per_pack": round(ev_pack, 2),
            "gbp_per_pack": round(per_pack, 2) if per_pack else None,
            "score": round(ev_pack - per_pack, 2) if per_pack else None,
            "ratio": round(ev / price, 2) if price else None}


def set_breakdown(s, use_ebay=False, cfg=None):
    """Values every hit card of a set (incl. attached sub-sets like Galarian Gallery).
    Returns (ev_pack, contrib, cards[(card, id, rarity, value, source)])."""
    lang = s.get("lang", "en")
    rates, bulk, extra = (s["rates"], s.get("bulk_per_pack", 0.1), {}) if s.get("rates") else rates_for(s["id"], cfg, s.get("era"))
    if s.get("game") == "onepiece":
        return op_breakdown(s, rates, bulk)
    jobs = [(r, b) for r in rates for b in catalog.cards_by_rarity(lang, s["id"], r)]
    for sub, sub_rates in extra.items():   # "*" = every card of the sub-set (TCGdex gives some galleries no rarity)
        jobs += [(f"{sub}:{r}", b) for r in sub_rates
                 for b in (catalog.cards_by_rarity(lang, sub, r) if r != "*" else (catalog.set_(lang, sub) or {}).get("cards") or [])]
    with ThreadPoolExecutor(4) as ex:
        vals = list(ex.map(lambda rb: card_value(lang, rb[1], use_ebay), jobs))
    by, by_ebay, cards = {}, {}, []
    for (r, b), (v, src) in zip(jobs, vals):
        by.setdefault(r, []).append(v)
        by_ebay.setdefault(r, []).append(v if is_ebay(src) else 0.0)
        cards.append({"card": b["name"], "id": b["id"], "rarity": r, "value": v, "source": src,
                      "number": b.get("localId"), "image": catalog.image(b, "low")})
    all_rates = dict(rates, **{f"{sub}:{r}": p for sub, sr in extra.items() for r, p in sr.items()})
    ev_pack, contrib = ev_per_pack(all_rates, by, bulk)
    ebay_pack, _ = ev_per_pack(all_rates, by_ebay, 0)
    share = round(ebay_pack / (ev_pack - bulk), 3) if ev_pack > bulk else 0.0
    return ev_pack, contrib, sorted(cards, key=lambda c: -c["value"]), share, {"rates": all_rates, "bulk": bulk}


def breakdown(info, packs):
    """Pure. info: the stored ev:<set> dict (cards, rates, bulk). How a product of `packs` packs gets its EV:
    expected pulls of one card = its rarity's rate per pack × packs ÷ cards of that rarity; £ = value × pulls.
    Returns (card rows, best first; rarity rows; totals {ev_pack, product_ev, bulk})."""
    cards, rates, bulk = info.get("cards") or [], info.get("rates") or {}, float(info.get("bulk") or 0)
    n = {}
    for c in cards:
        n[c["rarity"]] = n.get(c["rarity"], 0) + 1
    rows = []
    for c in cards:
        rate = float(rates.get(c["rarity"]) or 0)
        pulls = rate * packs / n[c["rarity"]]
        rows.append({"image": c.get("image"), "card": c["card"], "number": c.get("number"), "rarity": c["rarity"],
                     "value": round(c["value"], 2), "source": c["source"], "rate": rate, "one_in": round(1 / rate) if rate else None,
                     "cards_in_rarity": n[c["rarity"]], "pulls": round(pulls, 3), "gbp": round(c["value"] * pulls, 2)})
    rows.sort(key=lambda r: -r["gbp"])
    rar = []
    for r, k in n.items():
        vals = [c["value"] for c in cards if c["rarity"] == r]
        rate = float(rates.get(r) or 0)
        rar.append({"rarity": r, "cards": k, "avg_value": round(sum(vals) / k, 2), "rate": rate,
                    "one_in": round(1 / rate) if rate else None, "per_pack": round(rate * sum(vals) / k, 2),
                    "per_product": round(rate * sum(vals) / k * packs, 2)})
    rar.sort(key=lambda r: -r["per_product"])
    ev_pack = bulk + sum(r["per_pack"] for r in rar)
    return rows, rar, {"ev_pack": round(ev_pack, 2), "product_ev": round(packs * ev_pack, 2), "bulk": round(bulk * packs, 2)}


def is_ebay(source):
    return bool(source) and source != "none" and not source.startswith("Cardmarket")


# ---------- products: price, image, rating ----------

def product_price(key, query, kind, force=False, exclude=(), ref=None):
    """(price, basis, n): recent UK sold median ('sold', >=3 sales) else the low quartile of current UK listings
    ('est', >=3 listings) else 'thin' (1-2 sales or listings: shown, but ranked after better-backed prices), via
    prices.market_for(bucket='Sealed'). Also records sales: vanished listings (infer_sales) and units sold from
    multi-quantity listings (check_quantity_sales, boxes and ETBs only). ref: outside reference price (£)."""
    try:
        prices.refresh_sealed_asks(key, query, force=force, exclude=exclude, ref=ref)
        prices.infer_sales(key)
        if kind in ("Booster Box", "ETB"):
            prices.check_quantity_sales(key)
    except ebay.EbayError:
        pass
    v, stats, _ = prices.market_for(key, bucket="Sealed")
    kind_ = (stats or {}).get("kind")
    basis = "sold" if kind_ in ("sold", "override") else "thin" if kind_ == "thin" else "est" if v else None
    return v, basis, (stats or {}).get("n") or 0


# ---------- consistent product photos (stock catalogue images) ----------

def _norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower().replace("&", "and")).strip()


def _group_key(name):
    """'SWSH07: Evolving Skies' / 'SM - Team Up' / 'XY - Evolutions' -> 'evolving skies' / 'team up' / 'evolutions'."""
    return _norm(re.sub(r"^(?:SV|SWSH|SM|XY|ME)\d*\s*[:\-]\s*", "", name))


def _groups():
    c = db.setting("tcgcsv_groups") or {}
    if c.get("at", "") < (datetime.now(timezone.utc) - timedelta(days=30)).isoformat():
        res = requests.get(f"{TCGCSV}/groups", headers=UA, timeout=30).json()["results"]
        g = {}
        for x in res:
            g.setdefault(_group_key(x["name"]), x["groupId"])
        c = {"at": datetime.now(timezone.utc).isoformat(), "groups": g}
        db.set_setting("tcgcsv_groups", c)
    return c["groups"]


@lru_cache(maxsize=128)
def _group_products(gid):
    return requests.get(f"{TCGCSV}/{gid}/products", headers=UA, timeout=30).json()["results"]


def pick_stock(products, kind):
    """Pure. The plain product of this kind (no cases, halves, Pokémon Center or code cards): shortest name wins."""
    want = STOCK_WANT.get(kind)
    c = [p for p in products if want and want in p["name"].lower() and not any(x in p["name"].lower() for x in STOCK_SKIP)]
    best = min(c, key=lambda p: (len(p["name"]), p["name"]), default=None)
    return best["imageUrl"].replace("_200w.", "_400w.") if best and best.get("imageUrl") else None


def stock_image(set_name, kind):
    """Stock photo URL for a set's Booster Box / ETB / Bundle, or None (page shows the set logo instead)."""
    try:
        g, n = _groups(), _norm(set_name)
        gid = (g.get(GROUP_ALIASES.get(n, n)) or g.get(n + " base set") or g.get("scarlet and violet " + n)
               or g.get(n.replace("pokemon ", "")))   # '151' is 'SV: Scarlet & Violet 151'
        return pick_stock(_group_products(gid), kind) if gid else None
    except (requests.RequestException, ValueError, KeyError):
        return None


def trend(history, days=30, min_span=7, today=None):
    """Pure. history: [(date, median)] -> % change between the earliest point within `days` and the latest,
    or None until the history spans at least `min_span` days."""
    today = today or date.today()
    pts = sorted((d, v) for d, v in history if date.fromisoformat(d) >= today - timedelta(days=days) and v)
    if len(pts) < 2 or (date.fromisoformat(pts[-1][0]) - date.fromisoformat(pts[0][0])).days < min_span:
        return None
    return round((pts[-1][1] - pts[0][1]) / pts[0][1] * 100, 1)


def trend30(history, today=None):
    return trend(history, 30, 7, today)


def trend7(history, today=None):
    """Week-on-week: earliest point within 7 days vs the latest, once they are 4+ days apart."""
    return trend(history, 7, 4, today)


def price_histories(keys):
    """{product key: [(day, median UK asking price)]} from the daily roll-up (one query for the whole page)."""
    prices._ensure_rollup()
    out = {k: [] for k in keys}
    for r in db.q("SELECT card_key, seen_on, median FROM ask_daily WHERE card_key LIKE 'sealed:%' AND bucket='Sealed' "
                  "ORDER BY seen_on"):
        if r["card_key"] in out:
            out[r["card_key"]].append((r["seen_on"], round(r["median"], 2)))
    return out


# ---------- One Piece (TCGCSV: card list, rarities and TCGplayer market prices; box price from eBay UK) ----------

OP_TCGCSV = "https://tcgcsv.com/tcgplayer/68"
OP_CODE = re.compile(r"^(OP|EB)(\d\d)(?:-EB\d\d)?$")      # 'OP09', 'EB03', TCGCSV's odd 'OP15-EB04'; not 'OP18 RE'
OP_PAREN = re.compile(r"\(([^)]*)\)")


@lru_cache(maxsize=64)
def _op_get(path):
    return requests.get(f"{OP_TCGCSV}/{path}", headers=UA, timeout=30).json()["results"]


def op_sets():
    """Released One Piece booster sets (OP / EB), newest first: [{id 'OP09', name, era 'op', game, gid, release, products}]."""
    today = date.today().isoformat()
    out = []
    for g in _op_get("groups"):
        m = OP_CODE.match(g.get("abbreviation") or "")
        rel = (g.get("publishedOn") or "")[:10]
        if not m or rel > today:
            continue
        code = m[1] + m[2]
        q = (f'"one piece" "booster box" "({code},{code[:2]}-{code[2:]})" sealed -case -display -japanese -korean '
             f'-chinese -opened -empty -resealed')
        out.append({"id": code, "name": g["name"], "era": "op", "lang": "en", "game": "onepiece", "gid": g["groupId"],
                    "release": rel, "logo": None,
                    "products": [{"kind": "Booster Box", "name": f"{code} {g['name']} Booster Box", "packs": 24, "ebay_query": q}]})
    return sorted(out, key=lambda s: s["release"], reverse=True)


def op_rarity(name, rarity):
    """Pure. TCGCSV lists parallel arts as their own products: 'Shanks (004) (Manga)' SR -> 'Manga rare',
    'Nami (SP)' -> 'SP', 'Buggy (Parallel)' / '(Alternate Art)' / '(Wanted Poster)' R -> 'R alt art', plain -> rarity."""
    if not rarity:
        return None
    tags = [t.strip().lower() for t in OP_PAREN.findall(name) if not t.strip().isdigit()]
    if any(w in t for t in tags for w in ("manga", "super", "gold", "silver")):   # case-level hits, all ~1 per 12 boxes
        return "Manga rare"
    if "sp" in tags:
        return "SP"
    if rarity in ("TR", "DON!!") or not tags:
        return rarity
    return f"{rarity} alt art"


def op_cards(gid):
    """Cards of a One Piece set: [{name, id, rarity, number, usd, image}] (usd = TCGplayer market, Normal else Foil)."""
    pr = {}
    for p in _op_get(f"{gid}/prices"):
        pr.setdefault(p["productId"], {})[p.get("subTypeName")] = p.get("marketPrice")
    out = []
    for p in _op_get(f"{gid}/products"):
        ext = {e["name"]: e["value"] for e in p.get("extendedData") or []}
        r = op_rarity(p["name"], ext.get("Rarity"))
        if not r:
            continue
        v = pr.get(p["productId"], {})
        out.append({"name": p["name"], "id": str(p["productId"]), "rarity": r, "number": ext.get("Number"),
                    "usd": float(v.get("Normal") or v.get("Foil") or 0), "image": (p.get("imageUrl") or "").replace("_200w.", "_400w.")})
    return out


def op_image(s):
    return pick_stock(_op_get(f"{s['gid']}/products"), "Booster Box")


def op_box_usd(s):
    """TCGplayer market price (USD) of the set's booster box, or None: a sanity floor for eBay UK listings (UK eBay
    carries many £10 'sealed booster box' scam listings for One Piece)."""
    prods = {p["productId"]: p for p in _op_get(f"{s['gid']}/products")}
    want = STOCK_WANT["Booster Box"]
    for p in _op_get(f"{s['gid']}/prices"):
        n = (prods.get(p["productId"]) or {}).get("name", "").lower()
        if want in n and not any(x in n for x in STOCK_SKIP) and p.get("marketPrice"):
            return float(p["marketPrice"])
    return None


def op_breakdown(s, rates, bulk):
    """set_breakdown() for a One Piece set: values are TCGplayer market prices in £ (usd_gbp setting)."""
    fx, by, cards = float(db.setting("usd_gbp")), {}, []
    for c in op_cards(s["gid"]):
        if c["rarity"] not in rates:
            continue
        v = round(c["usd"] * fx, 2)
        by.setdefault(c["rarity"], []).append(v)
        cards.append({"card": c["name"], "id": c["id"], "rarity": c["rarity"], "value": v, "source": "TCGplayer market (USD→£)",
                      "number": c["number"], "image": c["image"]})
    ev_pack, contrib = ev_per_pack(rates, by, bulk)
    return ev_pack, contrib, sorted(cards, key=lambda c: -c["value"]), 0.0, {"rates": rates, "bulk": bulk}


def open_label(ratio):
    """Pure. Open value: expected card value ÷ price."""
    if ratio is None:
        return "No price"
    return "Worth opening" if ratio >= 1.15 else "Fair" if ratio >= 0.95 else "Poor" if ratio >= 0.75 else "Avoid opening"


LABEL_ORDER = ["Invest or open", "Hold / invest", "Open it", "Fair", "Don't open", "Avoid", "No UK price"]


def rank_key(r):
    """Pure. Ranking order: priced first (1-2 data points after better-backed prices), then EV ÷ price, then
    £ profit if opened, then cheapest per pack."""
    if not r.get("price"):
        return (2, 0, 0, 0)
    return (int(r.get("basis") == "thin"), -(r["ratio"] or 0), -((r["product_ev"] or 0) - r["price"]), r["price"] / max(r["packs"] or 1, 1))


def overall(ratio, trend):
    """Pure. One label from open value (EV ÷ price) + 30-day sealed price trend (None while history builds).
    Sealed priced far above its contents is a collector/investment product: 'Don't open', not 'Avoid',
    unless its price is also falling."""
    if ratio is None:
        return "No UK price"
    if trend is not None and trend >= 5:
        return "Invest or open" if ratio >= 1.15 else "Hold / invest"
    if trend is not None and trend <= -5 and ratio < 0.95:
        return "Avoid"
    return "Open it" if ratio >= 1.15 else "Fair" if ratio >= 0.95 else "Don't open"


def refresh_set(s, use_ebay=False, cfg=None, force_prices=False):
    """Compute EV for one set and refresh all its products. Returns the product rows written."""
    ev_pack, contrib, cards, share, meta = set_breakdown(s, use_ebay, cfg)
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    prods = s.get("products") or [{"kind": k, "name": n, "packs": p, "ebay_query": q}
                                  for k, n, p, q in products_for(s["id"], s["name"], s.get("release", ""))]
    for p in prods:
        key = f"sealed:{s.get('lang', 'en')}:{s['id']}:{p['name']}"
        kind = p.get("kind") or kind_of(p["name"])
        ref = (op_box_usd(s) or 0) * float(db.setting("usd_gbp")) if s.get("game") == "onepiece" else None
        price, basis, n = (p.get("price"), "manual", 0) if p.get("price") else \
            product_price(key, p["ebay_query"], kind, force=force_prices, exclude=excludes_for(s), ref=ref or None)
        old = db.q("SELECT image FROM products WHERE key=?", (key,))
        img = old[0]["image"] if old and "tcgplayer" in (old[0]["image"] or "") else \
            op_image(s) if s.get("game") == "onepiece" else stock_image(s["name"], kind)
        promo = promo_value(s.get("lang", "en"), p.get("promos", [])) if p.get("promos") else 0.0
        m = product_metrics(p["packs"], price, ev_pack, promo)
        hist = [(d, v) for d, b, v in prices.ask_history(key) if b == "Sealed"]
        row = {"key": key, "set_id": s["id"], "lang": s.get("lang", "en"),
               "era": s.get("era") or catalog.era_of(s["id"]) or "", "set_name": s["name"], "logo": s.get("logo"),
               "release": s.get("release", ""), "kind": kind, "name": p["name"], "packs": p["packs"],
               "query": p["ebay_query"], "price": price, "n": n, "image": img, "ev_pack": m["ev_per_pack"],
               "product_ev": m["product_ev"], "ratio": m["ratio"], "score": m["score"], "trend30": trend30(hist),
               "updated_at": now, "basis": basis, "sold_n": n if basis == "sold" else 0, "ebay_share": share}
        db.run(f"INSERT OR REPLACE INTO products ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
               tuple(row.values()))
        rows.append(row)
    db.set_setting(f"ev:{s['id']}", {"ev_pack": round(ev_pack, 2), "contrib": contrib, "top": cards[:12], "at": now,
                                      "ebay_share": share, "cards": cards, **meta})
    return rows


def kind_of(name):
    n = name.lower()
    return "Booster Box" if "booster box" in n else "ETB" if "elite trainer" in n or "etb" in n else \
        "Bundle" if "bundle" in n else "Other"


def all_sets():
    """Era sets + sets.toml extras (toml wins on id clash; it may carry rates/products/lang)."""
    extras = {s["id"]: s for s in load_sets()}
    out = []
    for s in era_sets():
        if s["id"] in extras:
            e = extras.pop(s["id"])
            s = {**s, **e, "era": s["era"]}
        out.append(s)
    for e in extras.values():                           # e.g. Japanese sets
        full = catalog.set_(e.get("lang", "en"), e["id"]) or {}
        out.append({**e, "era": e.get("lang", "en") if e.get("lang") == "ja" else catalog.era_of(e["id"]) or "",
                    "logo": catalog.set_logo(full), "release": full.get("releaseDate", "")})
    try:
        out += op_sets()
    except (requests.RequestException, ValueError, KeyError) as e:   # TCGCSV down: Pokémon refresh still runs
        db.set_setting("ev_error:onepiece", str(e)[:300])
    return out


def stale_sets(max_age_hours=20):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    fresh = {r["set_id"] for r in db.q("SELECT set_id FROM products WHERE updated_at > ?", (cutoff,))}
    return [s for s in all_sets() if s["id"] not in fresh]


def refresh_all(use_ebay=False, progress=None, max_age_hours=20):
    """Refresh every stale set. progress(i, n, set_name) is called per set. Returns the number refreshed."""
    todo, cfg = stale_sets(max_age_hours), load_rates()
    for i, s in enumerate(todo, 1):
        if progress:
            progress(i, len(todo), s["name"])
        try:
            refresh_set(s, use_ebay, cfg)
        except Exception as e:                           # one bad set must not stop the batch
            db.set_setting(f"ev_error:{s['id']}", str(e)[:300])
    return len(todo)


def promo_value(lang, promo_ids):
    total = 0.0
    for pid in promo_ids:
        c = catalog.card(lang, pid)
        if c:
            total += card_value(lang, {"id": pid, "name": c["name"]})[0]
    return total


def ranked(kind=None, era=None, game="pokemon"):
    """Products for the page, best first; unpriced last. game: 'pokemon' (every era but One Piece) or 'onepiece'.
    Each row carries its UK price history and the week-on-week change."""
    sql, args = "SELECT * FROM products WHERE 1=1", []
    if kind:
        sql += " AND kind=?"; args.append(kind)
    if era:
        sql += " AND era=?"; args.append(era)
    else:
        sql += " AND era" + ("='op'" if game == "onepiece" else "!='op'")
    rows = [dict(r) for r in db.q(sql, args)]
    hist = price_histories([r["key"] for r in rows])
    for r in rows:
        r["open"], r["label"] = open_label(r["ratio"]), overall(r["ratio"], r["trend30"])
        r["profit"] = round(r["product_ev"] - r["price"], 2) if r["price"] else None
        r["history"], r["trend7"] = hist[r["key"]], trend7(hist[r["key"]])
    return sorted(rows, key=rank_key)
