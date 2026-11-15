"""Inventory & profit: cards you bought (from a deal, an auction, or anywhere), what they cost, what they're worth
now, and what you made when they sold."""
from datetime import date

from . import db, prices


def add(card_key, name, cost, set_name=None, number=None, bucket="Raw", image=None, source_url=None, notes=""):
    return db.run("""INSERT INTO inventory (card_key, name, set_name, number, bucket, image, source_url, cost,
                     bought_at, status, notes) VALUES (?,?,?,?,?,?,?,?,?,'owned',?)""",
                  (card_key, name, set_name, number, bucket or "Raw", image, source_url, cost,
                   date.today().isoformat(), notes))


def mark_sold(inv_id, price, sold_at=None):
    db.run("UPDATE inventory SET status='sold', sold_price=?, sold_at=? WHERE id=?",
           (price, sold_at or date.today().isoformat(), inv_id))


def set_listed(inv_id, draft_id=None):
    db.run("UPDATE inventory SET status='listed', draft_id=COALESCE(?, draft_id) WHERE id=? AND status!='sold'",
           (draft_id, inv_id))


def market_now(row):
    """Current market value for an inventory row's card + condition bucket (None without eBay data)."""
    b = row["bucket"] or "Raw"
    if b.startswith("Raw"):
        return prices.market_for(row["card_key"], condition="LP" if b == "Raw played" else "NM")[0]
    grader, _, grade = b.partition(" ")
    return prices.market_for(row["card_key"], grader, grade)[0]


def pnl(rows):
    """Pure. rows: dicts with cost, status, sold_price, market (value now or None).
    -> {items, held, cost_held, value_held, unrealised, realised, sold}."""
    held = [r for r in rows if r["status"] != "sold"]
    sold = [r for r in rows if r["status"] == "sold"]
    cost_held = round(sum(r["cost"] or 0 for r in held), 2)
    valued = [r for r in held if r.get("market") is not None]
    value_held = round(sum(r["market"] for r in valued), 2)
    return {"items": len(rows), "held": len(held), "sold": len(sold), "cost_held": cost_held,
            "value_held": value_held,
            "unrealised": round(value_held - sum(r["cost"] or 0 for r in valued), 2),
            "realised": round(sum((r["sold_price"] or 0) - (r["cost"] or 0) for r in sold), 2)}
