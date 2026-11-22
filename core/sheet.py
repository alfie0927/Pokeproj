"""Keep your Excel workbook's Pokemon sheet in step with the cards this app lists and sells.

Columns (as in the workbook): A card, B buy price, C listing price, D sold price; this app also writes the eBay
SKU (PP-<draft id>) in column E so it finds the same row again. Cards ticked "Add to spreadsheet" get a new row
(A name, C listing price). A sale goes in where you'd type it: column C of 'Unsold Stuff', next to the card; your
sheet's macro then moves it into that card's C and D on the Pokemon tab. Sold prices are net (what eBay pays you
after fees, minus your posting cost). Writing goes through Excel itself (COM), which
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
UNSOLD = "Unsold Stuff"                   # FILTER of unsold rows; column D = the Pokemon row number
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
    """[{sku, name, listing, sold, gross, add}] for every card this app posted (live or ended). sold = net £."""
    out = []
    for r in db.q("SELECT id, data_json FROM drafts WHERE status IN ('posted', 'ended') AND listing_id IS NOT NULL"):
        d = json.loads(r["data_json"])
        out.append({"sku": f"PP-{r['id']}", "name": card_label(d), "listing": d.get("price"),
                    "sold": d.get("sold_net"), "gross": d.get("sold_gross"), "add": bool(d.get("to_sheet"))})
    return out


def match_key(s):
    """Pure. 'Kingdra TG03/TG30' and 'Kingdra TG03' -> 'kingdra tg03'; 'Mega Greninja ex 114/083' == '... 114/83'."""
    s = re.sub(r"/\S*", "", str(s or "").lower())
    return " ".join(re.sub(r"\b([a-z]*)0+(\d)", r"\1\2", s).split())


def plan(rows, items, first_row=FIRST_ROW):
    """Pure. rows: the sheet's A..E values from first_row down ([[a, b, c, d, e], ...]).
    Returns (writes [(row, col, value)], sells [(row, net £)]).
    A row tagged with the SKU is this card's row; else the first untagged, unsold row with the same card name is
    claimed (you typed it in yourself). No row: a new one at the end, only for cards ticked "Add to spreadsheet".
    Unsold: C follows the eBay price. Sold with D empty: a sale to enter via Unsold Stuff. A sold row still showing
    the gross price an earlier sync wrote gets the net price instead. Never touches your buy price."""
    tagged = {str(r[4]).strip(): i for i, r in enumerate(rows) if r[4] not in (None, "")}
    claimed, writes, sells = set(tagged.values()), [], []
    end = first_row + max([i for i, r in enumerate(rows) if any(v not in (None, "") for v in r)], default=-1) + 1
    for it in items:
        i = tagged.get(it["sku"])
        if i is None and (it["add"] or it["sold"] is not None):
            i = next((j for j, r in enumerate(rows) if j not in claimed and r[4] in (None, "") and r[3] in (None, "")
                      and match_key(r[0]) == match_key(it["name"])), None)
            if i is not None:
                writes.append((first_row + i, E, it["sku"]))
        if i is None and not it["add"]:
            continue
        if i is None:   # new row
            row, r = end, [it["name"], None, it["listing"], None, it["sku"]]
            writes += [(row, A, it["name"]), (row, E, it["sku"])] + ([(row, C, it["listing"])] if it["listing"] else [])
            end += 1
        else:
            claimed.add(i)
            row, r = first_row + i, rows[i]
        if it["sold"] is None:
            if it["listing"] is not None and r[3] in (None, "") and r[2] != it["listing"]:
                writes.append((row, C, it["listing"]))
        elif r[3] in (None, ""):
            sells.append((row, it["sold"]))
        elif it["gross"] is not None and r[3] == it["gross"] and it["sold"] != it["gross"]:
            writes += [(row, C, it["sold"]), (row, D, it["sold"])]
    return writes, sells


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
                xl.AutomationSecurity = 1          # macros on, so your Unsold Stuff macro handles sales
                wb, opened = _retry(lambda: xl.Workbooks.Open(str(path))), True
            try:
                ws = _retry(lambda: wb.Worksheets(SHEET))
                last = max(_retry(lambda: ws.Cells(ws.Rows.Count, A).End(-4162).Row), FIRST_ROW)   # -4162 = xlUp
                vals = _retry(lambda: ws.Range(ws.Cells(FIRST_ROW, A), ws.Cells(last, E)).Value)
                rows = [list(r) for r in (vals if isinstance(vals[0], tuple) else (vals,))]
                writes, sells = plan(rows, its)
                for row, col, v in writes:
                    _retry(lambda: setattr(ws.Cells(row, col), "Value", v))
                for row, v in sells:
                    _sell(xl, wb, ws, row, v)
                if opened:
                    _retry(wb.Save)
            finally:
                if opened:
                    _retry(lambda: wb.Close(False))
                    xl.Quit()
        finally:
            pythoncom.CoUninitialize()
    n_rows = len({r for r, _, _ in writes} | {r for r, _ in sells})
    msg = (f"Spreadsheet: {n_rows} row(s) updated, {len(sells)} sale(s) entered"
           + ("" if opened or not (writes or sells) else " (written into your open workbook: save it in Excel)"))
    db.set_setting("sheet_last", {"at": datetime.datetime.now().isoformat(timespec="seconds"), "msg": msg})
    return msg


def _sell(xl, wb, ws, row, v):
    """Type the net sale price into Unsold Stuff column C on the line for Pokemon row `row`, as you would; the
    sheet's macro copies it to that row's C and D and clears the cell. If macros didn't run, do the same here."""
    us = _retry(lambda: wb.Worksheets(UNSOLD))
    _retry(xl.Calculate)
    last = max(_retry(lambda: us.Cells(us.Rows.Count, D).End(-4162).Row), 2)
    col = _retry(lambda: us.Range(us.Cells(2, D), us.Cells(last, D)).Value)
    col = [c[0] for c in col] if isinstance(col, tuple) else [col]
    k = next((j for j, x in enumerate(col) if isinstance(x, (int, float)) and int(x) == row), None)
    if k is not None:
        xl.EnableEvents = True
        _retry(lambda: setattr(us.Cells(2 + k, C), "Value", v))
        if _retry(lambda: us.Cells(2 + k, C).Value) in (None, ""):
            return                                   # macro moved it
        _retry(lambda: us.Cells(2 + k, C).ClearContents())
    for c in (C, D):                                 # macros off, or the row isn't in the filter yet
        _retry(lambda: setattr(ws.Cells(row, c), "Value", v))


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
