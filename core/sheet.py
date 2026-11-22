"""Keep your Excel workbook's Pokemon sheet in step with the cards this app lists and sells.

Columns (as in the workbook): A card, B buy price, C listing price, D sold price; this app also writes the eBay
SKU (PP-<draft id>) in column E so it finds the same row again. Writing goes through Excel itself (COM), which
keeps the workbook's macros, stock data types, FILTER formulas and pictures intact (openpyxl would drop them).
"""
import datetime
import json
import re
import shutil
import threading
import time
from pathlib import Path

from . import db

ROOT = Path(__file__).resolve().parent.parent
SHEET, FIRST_ROW = "Pokemon", 18          # card rows start under the totals block
A, B, C, D, E = 1, 2, 3, 4, 5
_lock = threading.Lock()


def workbook_path():
    p = db.setting("sheet_path")
    if p:
        return Path(p)
    found = sorted(ROOT.glob("*.xlsm"))
    return found[0] if found else None


def card_label(d):
    """Pure. How the workbook names a card: 'Charizard ex 183/165', graded 'Rayquaza VMAX TG20/TG30 ACE 9'."""
    if not (d.get("name") and d.get("number")):   # card not identified: the listing title says what it is
        return (d.get("title") or d.get("name") or "")[:60]
    s = f"{d['name']} {d['number']}".strip()
    if d.get("graded") and d.get("grader"):
        s += f" {d['grader'].upper()} {d.get('grade') or ''}".rstrip()
    return s


def items():
    """[{sku, name, buy, listing, sold}] for every card this app posted (live or ended)."""
    out = []
    for r in db.q("SELECT id, listing_id, data_json FROM drafts WHERE status IN ('posted', 'ended') AND listing_id IS NOT NULL"):
        d = json.loads(r["data_json"])
        buy = None
        if d.get("inventory_id"):
            got = db.q("SELECT cost FROM inventory WHERE id=?", (d["inventory_id"],))
            buy = got[0]["cost"] if got else None
        sold = db.q("SELECT price FROM sales WHERE source='own' AND url LIKE ? ORDER BY sold_at DESC LIMIT 1",
                    (f"%/{r['listing_id']}",))
        out.append({"sku": f"PP-{r['id']}", "name": card_label(d), "buy": buy, "listing": d.get("price"),
                    "sold": sold[0]["price"] if sold else None})
    return out


def match_key(s):
    """Pure. 'Kingdra TG03/TG30' and 'Kingdra TG03' -> 'kingdra tg03'; 'Mega Greninja ex 114/083' == '... 114/83'."""
    s = re.sub(r"/\S*", "", str(s or "").lower())
    return " ".join(re.sub(r"\b([a-z]*)0+(\d)", r"\1\2", s).split())


def plan(rows, items, first_row=FIRST_ROW):
    """Pure. rows: the sheet's A..E values from first_row down ([[a, b, c, d, e], ...]).
    Returns [(row, col, value)] to write: a row already tagged with the SKU is updated; else the first untagged,
    unsold row with the same card name is claimed (you typed it in yourself); else a new row goes at the end.
    Never overwrites your buy or sold figures; the listing price follows eBay."""
    tagged = {str(r[4]).strip(): i for i, r in enumerate(rows) if r[4] not in (None, "")}
    claimed, writes = set(), []
    end = first_row + max([i for i, r in enumerate(rows) if any(v not in (None, "") for v in r)], default=-1) + 1
    for it in items:
        i = tagged.get(it["sku"])
        if i is None:
            i = next((j for j, r in enumerate(rows) if j not in claimed and r[4] in (None, "") and r[3] in (None, "")
                      and match_key(r[0]) == match_key(it["name"])), None)
            if i is not None:
                writes.append((first_row + i, E, it["sku"]))
        if i is None:   # new row
            writes += [(end, A, it["name"]), (end, E, it["sku"])] + \
                      [(end, col, it[k]) for col, k in ((B, "buy"), (C, "listing"), (D, "sold")) if it[k] is not None]
            end += 1
            continue
        claimed.add(i)
        r, row = rows[i], first_row + i
        if it["buy"] is not None and r[1] in (None, ""):
            writes.append((row, B, it["buy"]))
        if it["listing"] is not None and r[2] != it["listing"]:
            writes.append((row, C, it["listing"]))
        if it["sold"] is not None and r[3] in (None, ""):
            writes.append((row, D, it["sold"]))
    return writes


def _retry(fn, wait=120):
    """Excel answers 'call was rejected' (RPC_E_CALL_REJECTED / SERVERCALL_RETRYLATER) while it's busy, e.g.
    refreshing the workbook's stock data on open: try again for up to `wait` seconds."""
    import pywintypes
    end = time.time() + wait
    while True:
        try:
            return fn()
        except pywintypes.com_error as e:
            if e.args[0] not in (-2147418111, -2147417846) or time.time() > end:
                raise
            time.sleep(0.5)


def sync():
    """Write items() into the workbook. If you have it open in Excel, the changes go into the open window (save it
    there); otherwise Excel opens it hidden, writes, saves and closes. A dated backup is kept before the first
    write each day (data/sheet_backups). Returns a one-line summary."""
    import pythoncom
    import win32com.client
    path = workbook_path()
    if not path or not path.exists():
        return "No .xlsm workbook found (Settings → spreadsheet path)."
    its = items()
    backup(path)
    with _lock:
        pythoncom.CoInitialize()
        try:
            xl, wb, opened = None, None, False
            try:
                xl = win32com.client.GetActiveObject("Excel.Application")
                wb = next((w for w in xl.Workbooks if Path(w.FullName).resolve() == path.resolve()), None)
            except Exception:
                pass
            if wb is None:
                xl = win32com.client.DispatchEx("Excel.Application")
                xl.Visible, xl.DisplayAlerts = False, False
                wb, opened = _retry(lambda: xl.Workbooks.Open(str(path))), True
            try:
                ws = _retry(lambda: wb.Worksheets(SHEET))
                last = max(_retry(lambda: ws.Cells(ws.Rows.Count, A).End(-4162).Row), FIRST_ROW)   # -4162 = xlUp
                vals = _retry(lambda: ws.Range(ws.Cells(FIRST_ROW, A), ws.Cells(last, E)).Value)
                rows = [list(r) for r in (vals if isinstance(vals[0], tuple) else (vals,))]
                writes = plan(rows, its)
                for row, col, v in writes:
                    _retry(lambda: setattr(ws.Cells(row, col), "Value", v))
                if opened:
                    _retry(wb.Save)
            finally:
                if opened:
                    _retry(lambda: wb.Close(False))
                    xl.Quit()
        finally:
            pythoncom.CoUninitialize()
    n_rows = len({r for r, _, _ in writes})
    msg = (f"Spreadsheet: {n_rows} row(s) updated from {len(its)} listed card(s)"
           + ("" if opened or not writes else " (written into your open workbook: save it in Excel)"))
    db.set_setting("sheet_last", {"at": datetime.datetime.now().isoformat(timespec="seconds"), "msg": msg})
    return msg


def backup(path):
    folder = ROOT / "data" / "sheet_backups"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{path.stem} {datetime.date.today().isoformat()}{path.suffix}"
    if not dest.exists():
        shutil.copy2(path, dest)


def sync_quiet():
    """sync() that never raises (for after posting / price pushes / the daily job). Returns the summary or error."""
    if not db.setting("sheet_auto"):
        return None
    try:
        return sync()
    except Exception as e:
        msg = f"Spreadsheet update failed: {e}"
        db.set_setting("sheet_last", {"at": datetime.datetime.now().isoformat(timespec="seconds"), "msg": msg})
        return msg
