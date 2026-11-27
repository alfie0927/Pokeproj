"""Daily job: snapshot UK listings for every followed card (priority order, within the Browse budget),
then check which of their tracked listings sold.
Run: .venv\\Scripts\\python refresh_daily.py   (schedule it — see README). Needs eBay production keys in secrets."""
import json
import os
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sec = ROOT / ".streamlit" / "secrets.toml"
if sec.exists():
    with open(sec, "rb") as f:
        os.environ.update({k: str(v) for k, v in tomllib.load(f).items() if not isinstance(v, dict)})

from core import catalog, db, ebay, ev, prices  # noqa: E402


def seed_followed():
    """Make sure every card worth following is in `followed` (see prices.follow for priorities)."""
    for r in db.q("SELECT card_key, name FROM watch"):
        prices.follow(r["card_key"], 3, "watched", r["name"])
    for r in db.q("SELECT data_json FROM drafts WHERE status IN ('draft', 'accepted', 'posted')"):
        d = json.loads(r["data_json"])
        if d.get("card_key"):
            prices.follow(d["card_key"], 3, "draft / listing", d.get("name"), d.get("number"))
    for s in ev.load_sets():                      # data/sets.toml hit cards (sealed EV)
        for rarity in s["rates"]:
            for b in catalog.cards_by_rarity(s["lang"], s["id"], rarity):
                prices.follow(f"{s['lang']}:{b['id']}", 1, "sets.toml hit card")
    # cards tracked before following existed: added once, then they age out unless seen again
    db.run("""INSERT OR IGNORE INTO followed (card_key, lang, card_id, priority, last_seen, reason)
              SELECT DISTINCT card_key, substr(card_key, 1, instr(card_key, ':') - 1),
                     substr(card_key, instr(card_key, ':') + 1), 1, ?, 'tracked before'
              FROM tracked WHERE card_key LIKE 'en:%' OR card_key LIKE 'ja:%'""",
           (datetime.now(timezone.utc).isoformat(),))


def main():
    seed_followed()
    plan = prices.refresh_plan()
    before = prices.calls_today()
    budget = db.setting("browse_budget")
    print(f"{len(plan)} followed cards due; Browse calls used today {before}/{budget}", file=sys.stderr)
    totals = prices.refresh_followed(plan, max_calls=int(0.4 * budget))   # leaves the rest for the deal scanner
    if os.environ.get("EBAY_REFRESH_TOKEN"):
        try:
            totals["own"] = prices.sync_own_sales()
        except ebay.EbayError as e:
            print(f"own orders: {e}", file=sys.stderr)
    from core import labels, listing, sheet
    if db.setting("watcher_offers_auto") and os.environ.get("EBAY_REFRESH_TOKEN"):
        try:
            _, sent, errs = listing.watcher_offers(send=True)
            print(f"watcher offers sent: {sent}" + (f"; errors: {errs}" if errs else ""), file=sys.stderr)
        except Exception as e:
            print(f"watcher offers: {e}", file=sys.stderr)
    if db.setting("label_auto"):
        try:
            print(f"labels emailed: {labels.check()}", file=sys.stderr)
        except Exception as e:
            print(f"labels: {e}", file=sys.stderr)
    print(sheet.sync_quiet() or "spreadsheet sync off", file=sys.stderr)
    print(f"cards refreshed {totals.pop('cards')}/{len(plan)}"
          + (" (stopped at this job's share of the Browse budget; the rest go first tomorrow)" if totals.pop("stopped") else "")
          + f"; listings checked -> {totals or 'nothing to check yet'}; calls used {prices.calls_today() - before}")
    n = ev.refresh_all(progress=lambda i, n, name: print(f"sealed {i}/{n} {name}", file=sys.stderr))
    print(f"sealed products refreshed for {n} set(s)")
    db.set_setting("last_refresh", datetime.now(timezone.utc).isoformat())


if __name__ == "__main__":
    main()
