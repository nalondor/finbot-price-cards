#!/usr/bin/env python3
"""Refresh ALL data on the two static Economics HUD pages (data only, no redesign).

Files (repo root): economics-hud.html (phone) and economics-hud-16x9.html (16:9).

Sources
  * Yahoo Finance daily chart API -> `const DATA` price/yield/vol tiles
      SPX ^GSPC, NDX ^IXIC (tile label "Nasdaq"), FTSE ^FTSE, DOW ^DJI, RUT ^RUT, VIX ^VIX,
      10Y ^TNX, 5Y ^FVX, 30Y ^TYX, TLT, IEF, SHY, BND, DXY DX-Y.NYB,
      WTI CL=F, BRENT BZ=F, GOLD GC=F
  * FRED fredgraph.csv -> `const DATA` macro tiles
      CPI = CPIAUCSL YoY %, JOBLESS = UNRATE, GDP = A191RL1Q225SBEA, FED = FEDFUNDS
      (+ CPILFESL core YoY and PAYEMS m/m for the tap-to-enlarge modal text)
  * Treasury.gov daily par yield curve CSV (current + prior year) -> `const REF`
      (reference curve 3M/2Y/7Y/20Y) + curve cells, Treasury note line, modal curve tile
  * BLS CPI release schedule -> "CPI next: ..." note (left alone if unavailable)

Static (no-JS / first paint) text is re-rendered to match DATA/REF for the default
(active) range tab: tile last value, UP/DOWN pill, chg pill, polyline, anchors
(strong[data-a]), vs-core line, ref curve cells, Treasury note, subtitle stamp, etc.

If a source fails, the previous values for those keys are kept. Exit code is nonzero
only if nothing at all could be updated (2) or a requested push failed (3).

Usage:
  python3 tools/rebuild_economics.py            # fetch + rewrite both HTML files
  python3 tools/rebuild_economics.py --dry-run  # fetch + report, write nothing
  python3 tools/rebuild_economics.py --push     # rewrite, git commit, pull --rebase, push
"""
import argparse
import csv
import datetime as dt
import io
import json
import math
import os
import re
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

try:
    import requests  # optional
except Exception:  # pragma: no cover
    requests = None
import urllib.request
import urllib.error

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = ["economics-hud.html", "economics-hud-16x9.html"]
LONDON = ZoneInfo("Europe/London")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Dest": "document",
    "Upgrade-Insecure-Requests": "1",
}

YAHOO = {
    "SPX": "^GSPC", "NDX": "^IXIC", "FTSE": "^FTSE", "10Y": "^TNX", "5Y": "^FVX", "30Y": "^TYX",
    "TLT": "TLT", "DXY": "DX-Y.NYB", "WTI": "CL=F", "BRENT": "BZ=F", "GOLD": "GC=F", "VIX": "^VIX",
    "IEF": "IEF", "SHY": "SHY", "BND": "BND", "DOW": "^DJI", "RUT": "^RUT",
}
FRED_MACRO = {"CPI": "CPIAUCSL", "JOBLESS": "UNRATE", "GDP": "A191RL1Q225SBEA", "FED": "FEDFUNDS"}
FRED_EXTRA = ["CPILFESL", "PAYEMS"]
RANGES = ["1M", "3M", "6M", "1Y"]
# daily windows (trading points) - matches how the original snapshot was built
DAILY_N = {"1M": 22, "3M": 64, "6M": 127}          # 1Y = bars since same date one year earlier
REF_N = {"1M": 22, "3M": 64, "6M": 127, "1Y": 253}  # as /workspace/ref_sparks.py
MACRO_MONTHS = {"1M": 1, "3M": 3, "6M": 6, "1Y": 12}
REF_COLS = {"3M": "3 Mo", "2Y": "2 Yr", "7Y": "7 Yr", "20Y": "20 Yr"}
CORE = "SPX"
MINUS = "\u2212"

LOG = []


def log(msg):
    print(msg, flush=True)


# --------------------------------------------------------------------------- fetch
def http_get(url, headers=None, timeout=45, attempts=5, label=None, accept_json=False):
    """GET with retries/backoff + browser headers. Returns text or raises RuntimeError."""
    h = dict(BROWSER_HEADERS)
    if headers:
        h.update(headers)
    last_err = None
    for i in range(attempts):
        try:
            if requests is not None:
                r = requests.get(url, headers=h, timeout=timeout)
                status, text = r.status_code, r.text
            else:
                req = urllib.request.Request(url, headers=h)
                try:
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        status, text = resp.status, resp.read().decode("utf-8", "replace")
                except urllib.error.HTTPError as e:
                    status, text = e.code, ""
            if status == 200 and text.strip():
                if accept_json:
                    json.loads(text)  # validate
                return text
            last_err = f"HTTP {status}"
        except Exception as e:  # network / timeout / bad json
            last_err = f"{type(e).__name__}: {e}"[:200]
        wait = min(30, 2 * (2 ** i)) if i < attempts - 1 else 0
        log(f"  .. {label or url[:80]} attempt {i + 1}/{attempts} failed ({last_err})"
            + (f"; retry in {wait}s" if wait else ""))
        if wait:
            time.sleep(wait)
    raise RuntimeError(f"{label or url}: {last_err}")


def fetch_yahoo(sym):
    """Daily closes [(date, close)] in exchange-local dates, oldest first."""
    q = urllib.request.quote(sym, safe="")
    last_err = None
    for host in ("query2", "query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{q}?range=2y&interval=1d&includePrePost=false"
        try:
            txt = http_get(url, headers={"Accept": "application/json"}, attempts=3, label=f"Yahoo {sym}", accept_json=True)
            res = json.loads(txt)["chart"]["result"][0]
            off = int(res["meta"].get("gmtoffset") or 0)
            ts = res.get("timestamp") or []
            closes = res["indicators"]["quote"][0]["close"]
            by_date = {}
            for t, c in zip(ts, closes):
                if c is None or (isinstance(c, float) and math.isnan(c)):
                    continue
                d = dt.datetime.fromtimestamp(t + off, dt.timezone.utc).date()
                by_date[d] = float(c)
            series = sorted(by_date.items())
            if len(series) < 260:
                raise RuntimeError(f"only {len(series)} bars")
            return series
        except Exception as e:
            last_err = e
            time.sleep(2)
    raise RuntimeError(f"Yahoo {sym}: {last_err}")


def fetch_fred(series_id):
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    txt = http_get(url, headers={"Referer": f"https://fred.stlouisfed.org/series/{series_id}",
                                 "Accept": "text/csv,text/html;q=0.9,*/*;q=0.8"},
                   timeout=40, attempts=4, label=f"FRED {series_id}")
    rows = list(csv.reader(io.StringIO(txt)))
    if not rows or len(rows[0]) < 2:
        raise RuntimeError(f"FRED {series_id}: unexpected CSV")
    out = []
    for r in rows[1:]:
        if len(r) < 2 or r[1].strip() in ("", "."):
            continue
        try:
            out.append((dt.date.fromisoformat(r[0].strip()), float(r[1])))
        except ValueError:
            continue
    if len(out) < 24:
        raise RuntimeError(f"FRED {series_id}: only {len(out)} rows")
    return out


def fetch_treasury(today):
    rows = {}
    for y in (today.year, today.year - 1):
        url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
               f"daily-treasury-rates.csv/{y}/all?type=daily_treasury_yield_curve"
               f"&field_tdr_date_value={y}&page&_format=csv")
        txt = http_get(url, headers={"Accept": "text/csv,*/*;q=0.8"}, timeout=120, attempts=4, label=f"Treasury {y}")
        n = 0
        for r in csv.DictReader(io.StringIO(txt)):
            try:
                m, d, yy = r["Date"].split("/")
                rows[dt.date(int(yy), int(m), int(d))] = r
                n += 1
            except Exception:
                continue
        if n == 0:
            raise RuntimeError(f"Treasury {y}: no rows")
    return sorted((d, r) for d, r in rows.items() if d <= today)


def fetch_cpi_schedule():
    """[(reference month date, release date)] from the BLS CPI schedule page."""
    txt = http_get("https://www.bls.gov/schedule/news_release/cpi.htm",
                   headers={"Sec-Fetch-Site": "none"}, timeout=30, attempts=3, label="BLS CPI schedule")
    out = []
    for ref, rel in re.findall(r"<td>\s*([A-Z][a-z]+ \d{4})\s*</td>\s*<td>\s*([A-Z][a-z]{2,4}\.? \d{1,2}, \d{4})\s*</td>", txt):
        try:
            refd = dt.datetime.strptime(ref, "%B %Y").date()
            reld = dt.datetime.strptime(rel.replace(".", "").replace("Sept", "Sep"), "%b %d, %Y").date()
            out.append((refd, reld))
        except ValueError:
            continue
    if not out:
        # looser fallback: strip tags and scan rows
        flat = re.sub(r"<[^>]+>", "|", txt)
        for ref, rel in re.findall(r"([A-Z][a-z]+ \d{4})\|+\s*\|*([A-Z][a-z]{2,4}\.? \d{1,2}, \d{4})", flat):
            try:
                out.append((dt.datetime.strptime(ref, "%B %Y").date(),
                            dt.datetime.strptime(rel.replace(".", "").replace("Sept", "Sep"), "%b %d, %Y").date()))
            except ValueError:
                continue
    if not out:
        raise RuntimeError("BLS CPI schedule: no rows parsed")
    return out


# --------------------------------------------------------------------------- build
def spark_daily(vals, maxpts=48):
    """Floor downsample to <=48 pts, viewBox 140x32, x 2..138, y inverted into 2..30."""
    if len(vals) > maxpts:
        step = (len(vals) - 1) / (maxpts - 1)
        vals = [vals[int(i * step)] for i in range(maxpts)]
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    n = len(vals)
    return " ".join(f"{2 + 136 * i / (n - 1):.1f},{30 - 28 * (v - lo) / rng:.1f}" for i, v in enumerate(vals))


def spark_ref(vals, maxpts=48):
    """Same as /workspace/ref_sparks.py (round-index downsample)."""
    if len(vals) > maxpts:
        step = (len(vals) - 1) / (maxpts - 1)
        vals = [vals[round(i * step)] for i in range(maxpts)]
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    n = len(vals)
    return " ".join(f"{2 + 136 * i / (n - 1):.1f},{30 - 28 * (v - lo) / rng:.1f}" for i, v in enumerate(vals))


def spark_macro(vals):
    """Macro prints: x 2.5..137.5, y inverted into 2.5..29.5 (as original snapshot)."""
    n = len(vals)
    lo, hi = min(vals), max(vals)
    out = []
    for i, v in enumerate(vals):
        x = 2.5 + 135 * i / (n - 1) if n > 1 else 70.0
        y = 29.5 - 27 * (v - lo) / (hi - lo) if hi > lo else 16.0
        out.append(f"{x:.1f},{y:.1f}")
    return " ".join(out)


def one_year_before(d):
    try:
        return d.replace(year=d.year - 1)
    except ValueError:
        return d.replace(year=d.year - 1, day=28)


def months_before(d, n):
    m = d.month - n
    y = d.year
    while m <= 0:
        m += 12
        y -= 1
    return dt.date(y, m, min(d.day, 28))


def build_daily(key, series, old):
    dates = [d for d, _ in series]
    vals = [v for _, v in series]
    last = vals[-1]
    e = {"label": old["label"], "kind": old["kind"], "last": last,
         "sparks": {}, "chg": {}, "dir": {}, "anchors": {}, "vsCore": {}}
    for rg in RANGES:
        if rg == "1Y":
            cut = one_year_before(dates[-1])
            w = [v for d, v in series if d >= cut]
        else:
            w = vals[-DAILY_N[rg]:]
        a = w[0]
        e["sparks"][rg] = spark_daily(w)
        e["chg"][rg] = round((last / a - 1) * 100, 2)
        e["dir"][rg] = "up" if last >= a else "down"
        e["anchors"][rg] = round(a, 3 if old["kind"] == "yield" else 2)
        e["vsCore"][rg] = None
    for k in old:
        if k not in e:
            e[k] = old[k]
    meta = {"asof": dates[-1], "last": last, "prev": vals[-2] if len(vals) > 1 else None}
    return e, meta


def yoy(series, ndigits=2):
    d = dict(series)
    out = []
    for day, v in series:
        p = dt.date(day.year - 1, day.month, 1)
        if p in d:
            x = (v / d[p] - 1) * 100
            out.append((day, round(x, ndigits) if ndigits is not None else x))
    return out


def build_macro(key, series, old):
    end = series[-1][0]
    last = round(series[-1][1], 2)
    e = {"label": old["label"], "kind": old["kind"], "last": last,
         "sparks": {}, "chg": {}, "dir": {}, "anchors": {}, "vsCore": {}}
    for rg in RANGES:
        cut = months_before(end, MACRO_MONTHS[rg])
        w = [round(v, 2) for d, v in series if d >= cut]
        if len(w) < 2:
            w = [round(v, 2) for _, v in series[-2:]]
        a = w[0]
        c = round(last - a, 2)
        e["sparks"][rg] = spark_macro(w)
        e["chg"][rg] = c
        e["dir"][rg] = "up" if c >= 0 else "down"
        e["anchors"][rg] = a
        e["vsCore"][rg] = None
    e["end"] = end.isoformat()
    for k in old:
        if k not in e:
            e[k] = old[k]
    return e


def build_ref(rows):
    out = {}
    day = {}
    for k, c in REF_COLS.items():
        s = [(d, float(r[c])) for d, r in rows if (r.get(c) or "").strip() not in ("", "N/A")]
        vals = [v for _, v in s]
        e = {"last": vals[-1], "sparks": {}, "chg": {}, "dir": {}, "anchors": {}}
        for rg, n in REF_N.items():
            v = vals[-n:]
            e["sparks"][rg] = spark_ref(v)
            e["chg"][rg] = round((v[-1] - v[0]) * 100)
            e["dir"][rg] = "up" if v[-1] >= v[0] else "down"
            e["anchors"][rg] = v[0]
        out[k] = e
        day[k] = round((vals[-1] - vals[-2]) * 100)
    return out, day


# --------------------------------------------------------------------------- format
def js_round(v):
    return math.floor(v + 0.5)


def fmt_int(v):
    return f"{js_round(v):,}"


def fmt_price(key, kind, v):
    if kind in ("yield", "macro"):
        return f"{v:.2f}%"
    if key in ("VIX", "DXY"):
        return f"{v:.2f}"
    if key == "GOLD":
        return "$" + fmt_int(v)
    if key in ("TLT", "IEF", "SHY", "BND", "WTI", "BRENT"):
        return f"${v:.2f}"
    return fmt_int(v)


def fmt_chg(c, kind):
    sign = "+" if c >= 0 else MINUS
    if kind == "macro":
        return f"{sign}{abs(c):.2f} pt"
    return f"{sign}{abs(c):.1f}%"


def fmt_pct2(c):
    return ("+" if c >= 0 else MINUS) + f"{abs(c):.2f}%"


def fmt_bp(bp):
    return ("+" if bp > 0 else (MINUS if bp < 0 else "")) + f"{abs(bp)} bp"


def dshort(d):  # Mon 5 Oct
    return f"{d:%a} {d.day} {d:%b}"


def dlong(d):  # Mon 5 Oct 2026
    return f"{d:%a} {d.day} {d:%b} {d.year}"


def london_stamp(now):  # Tue 6 Oct 2026 · ~6:58am London
    h = now.hour % 12 or 12
    return f"{dlong(now)} · ~{h}:{now.minute:02d}{'am' if now.hour < 12 else 'pm'} London"


def tclass(v):
    return "up" if v >= 0 else "down"


# --------------------------------------------------------------------------- html
class Doc:
    def __init__(self, name, text):
        self.name, self.s, self.warn = name, text, []

    def sub(self, pattern, repl, what, count=1, flags=re.S, required=False):
        new, n = re.subn(pattern, repl, self.s, count=count, flags=flags)
        if n == 0 and required:
            self.warn.append(f"{self.name}: pattern not found for {what}")
        self.s = new
        return n


def extract_json(s, name):
    m = re.search(r"const " + name + r" = (\{.*?\});\s*\n", s)
    if not m:
        raise RuntimeError(f"const {name} not found")
    return json.loads(m.group(1)), m


def render_tiles(doc, DATA, rng):
    s = doc.s
    starts = [(m.start(), m.group(1)) for m in re.finditer(r'<div class="spark-tile[^"]*" data-key="([A-Z0-9]+)">', s)]
    pieces, pos = [], 0
    for i, (st, key) in enumerate(starts):
        en = starts[i + 1][0] if i + 1 < len(starts) else len(s)
        pieces.append(s[pos:st])
        blk = s[st:en]
        d = DATA.get(key)
        if d:
            blk = render_tile(blk, key, d, DATA, rng, doc)
        pieces.append(blk)
        pos = en
    pieces.append(s[pos:])
    doc.s = "".join(pieces)
    return len(starts)


def render_tile(blk, key, d, DATA, rng, doc):
    kind = d["kind"]
    dirv = d["dir"][rng]
    chg = d["chg"][rng]
    color = "#39d98a" if dirv == "up" else "#ff6b7a"
    reps = [
        (r'(<span class="spark-last" data-last>)[^<]*(</span>)', lambda m: m.group(1) + fmt_price(key, kind, d["last"]) + m.group(2)),
        (r'<span class="dir-pill [a-z]+" data-dir>[^<]*</span>', lambda m: f'<span class="dir-pill {dirv}" data-dir>{"UP" if dirv == "up" else "DOWN"}</span>'),
        (r'<span class="pct [a-z]+" data-chg>[^<]*</span>', lambda m: f'<span class="pct {tclass(chg)}" data-chg>{fmt_chg(chg, kind)}</span>'),
        (r'(<polyline data-line[^>]*?\sstroke=")[^"]*(")', lambda m: m.group(1) + color + m.group(2)),
        (r'(<polyline data-line[^>]*?\spoints=")[^"]*(")', lambda m: m.group(1) + d["sparks"][rng] + m.group(2)),
    ]
    for r in RANGES:
        reps.append((r'(<strong data-a="' + r + r'">)[^<]*(</strong>)',
                     lambda m, r=r: m.group(1) + fmt_price(key, kind, d["anchors"][r]) + m.group(2)))
    if key == CORE:
        vs = '<div class="vs-core">Core indicator</div>'
    elif d["vsCore"].get(rng) is None:
        vs = ('<div class="vs-core">vs S&amp;P core: n/a (macro print)</div>' if kind == "macro"
              else '<div class="vs-core">vs S&amp;P core: n/a (yield/vol)</div>')
    else:
        v = d["vsCore"][rng]
        vs = f'<div class="vs-core">vs S&amp;P core: <em class="{tclass(v)}">{fmt_chg(v, "price")}</em></div>'
    reps.append((r'(<div data-vs>)<div class="vs-core">.*?</div>(</div>)', lambda m: m.group(1) + vs + m.group(2)))
    for pat, fn in reps:
        blk, n = re.subn(pat, fn, blk, count=1, flags=re.S)
        if n == 0:
            doc.warn.append(f"{doc.name}: tile {key}: no match for {pat[:40]}")
    return blk


def update_html(doc, DATA, REF, ctx):
    s = doc.s
    m = re.search(r'data-range="(\w+)" class="active"', s)
    rng = m.group(1) if m else "6M"

    # 1) JSON blobs
    _, dm = extract_json(s, "DATA")
    doc.s = s[:dm.start(1)] + json.dumps(DATA, separators=(",", ":")) + s[dm.end(1):]
    _, rm = extract_json(doc.s, "REF")
    doc.s = doc.s[:rm.start(1)] + json.dumps(REF) + doc.s[rm.end(1):]

    # 2) tiles
    render_tiles(doc, DATA, rng)

    # 3) stamps
    now = ctx["now"]
    doc.sub(r'(<p class="subtitle">)[A-Z][a-z]{2} \d{1,2} [A-Z][a-z]{2} \d{4} · ~\d{1,2}:\d{2}[ap]m London',
            lambda m: m.group(1) + london_stamp(now), "subtitle stamp", required=True)
    doc.sub(r'(<title>Economics · (?:16:9 )?HUD · )\d{1,2} [A-Z][a-z]{2} \d{4}',
            lambda m: m.group(1) + f"{now.day} {now:%b} {now.year}", "title date")
    doc.sub(r'(<h2 id="modal-title">Economics · )\d{1,2} [A-Z][a-z]{2} \d{4}',
            lambda m: m.group(1) + f"{now.day} {now:%b} {now.year}", "modal title date")

    y = ctx.get("yahoo_meta", {})
    if "SPX" in y:
        doc.sub(r'(<div class="section-label">Major indices · Yahoo live )[A-Z][a-z]{2} \d{1,2} [A-Z][a-z]{2}',
                lambda m: m.group(1) + dshort(y["SPX"]["asof"]), "indices label date")

    # 4) reference curve cells + Treasury note
    tsy = ctx.get("tsy")
    for k, e in REF.items():
        bp = tsy["day"][k] if tsy else None

        def cell(m, e=e, bp=bp):
            out = m.group(1) + f'<div class="yld">{e["last"]:.2f}%</div>'
            if bp is None:
                return out + m.group(2)
            cls = "chg up" if bp > 0 else ("chg down" if bp < 0 else "chg")
            return out + f'<div class="{cls}">{fmt_bp(bp)}</div>'
        doc.sub(r'(<div class="curve-cell ref-cell" data-ref="' + k + r'"><div class="ref-line"><div class="tenor">[^<]*</div>)'
                r'<div class="yld">[^<]*</div>(<div class="chg[^"]*">[^<]*</div>)', cell, f"ref cell {k}", required=True)
    if tsy:
        t10 = [DATA[k]["last"] for k in ("10Y", "5Y", "30Y")]
        tape_date = y.get("10Y", {}).get("asof")
        tape = ("Yahoo tape " + (dshort(tape_date) if tape_date else "") + ": ").replace(" :", ":")
        tape += f"10Y {t10[0]:.2f}% · 5Y {t10[1]:.2f}% · 30Y {t10[2]:.2f}%"
        head = (f"Official Daily Treasury Par Yield Curve · {dshort(tsy['date'])} · day vs {dshort(tsy['prev_date'])}"
                f" · 2s10s {tsy['s2s10']:+d} bp")

        def note(m):
            inline = "note-inline" in m.group(1)
            return m.group(1) + (head + " · " + tape if inline else head + ". " + tape + ".") + m.group(2)
        doc.sub(r'(<p class="note(?:-inline)?">)Official Daily Treasury Par Yield Curve[^<]*(</p>)', note,
                "treasury note", required=True)

    # 5) CPI next
    nxt = ctx.get("cpi_next")
    if nxt:
        refm, reld = nxt
        doc.sub(r'CPI next: [A-Z][a-z]{2} print due [A-Z][a-z]{2} \d{1,2} [A-Z][a-z]{2}\.',
                f"CPI next: {refm:%b} print due {dshort(reld)}.", "CPI next")

    # 6) tap-to-enlarge modal (phone file only has it)
    if 'id="modal-title"' in doc.s:
        update_modal(doc, DATA, ctx)


def update_modal(doc, DATA, ctx):
    tsy, y, fr, now = ctx.get("tsy"), ctx.get("yahoo_meta", {}), ctx.get("fred", {}), ctx["now"]
    S = doc.sub
    if tsy:
        c = tsy["curve"]
        S(r'(<div class="name">Treasury curve \()[^)]*(\)</div>)', lambda m: m.group(1) + f"{tsy['date']:%a} {tsy['date'].day} {tsy['date']:%b}" + m.group(2), "modal curve date")
        S(r'(<div class="price">)10Y [0-9.]+%(</div>)', lambda m: m.group(1) + f"10Y {c['10 Yr']:.2f}%" + m.group(2), "modal 10Y")
        for col, lab in (("2 Yr", "2Y"), ("30 Yr", "30Y")):
            S(r'<span class="pct [a-z]+">' + lab + r' [0-9.]+%</span>',
              f'<span class="pct {tclass(tsy["daycols"][col])}">{lab} {c[col]:.2f}%</span>', f"modal {lab}")
        par = " · ".join(f"{lab} {c[col]:.2f}" for col, lab in (("3 Mo", "3M"), ("2 Yr", "2Y"), ("5 Yr", "5Y"), ("7 Yr", "7Y"), ("10 Yr", "10Y"), ("20 Yr", "20Y"), ("30 Yr", "30Y")))
        S(r'Full par curve:[^<]*', f"Full par curve: {par}. 2s10s {tsy['s2s10']:+d} bp. Day vs {tsy['prev_date'].day} {tsy['prev_date']:%b}.", "modal par curve")
        S(r'(U\.S\. Treasury Daily Par Yield Curve Rates \(close )[^)]*(\))', lambda m: m.group(1) + dlong(tsy["date"]) + m.group(2), "modal sources curve date")

    def dpct(k):
        mm = y.get(k)
        return (mm["last"] / mm["prev"] - 1) * 100 if mm and mm.get("prev") else None

    def p(k):
        return fmt_price(k, DATA[k]["kind"], y[k]["last"])
    if "TLT" in y:
        S(r'(<div class="price">)TLT \$[0-9.,]+(</div>)', lambda m: m.group(1) + "TLT " + p("TLT") + m.group(2), "modal TLT")
    for k, lab in (("TLT", "TLT"), ("IEF", "IEF"), ("SPX", None), ("NDX", "NDX"), ("FTSE", "FTSE"), ("WTI", "WTI"), ("BRENT", "Brent")):
        if lab and k in y and dpct(k) is not None:
            v = dpct(k)
            S(r'<span class="pct [a-z]+">' + lab + r' [+\u2212-][0-9.]+%</span>', f'<span class="pct {tclass(v)}">{lab} {fmt_pct2(v)}</span>', f"modal {lab} day")
    if "SHY" in y and "BND" in y:
        S(r'SHY \$[0-9.]+ \([^)]*\) · BND \$[0-9.]+ \([^)]*\)', f"SHY {p('SHY')} ({fmt_pct2(dpct('SHY'))}) · BND {p('BND')} ({fmt_pct2(dpct('BND'))})", "modal SHY/BND")
    if "SPX" in y:
        S(r'(<div class="price">S&amp;P )[0-9,]+(</div>)', lambda m: m.group(1) + fmt_int(y["SPX"]["last"]) + m.group(2), "modal SPX")
    if all(k in y for k in ("DOW", "NDX", "FTSE", "RUT", "VIX")):
        S(r'Dow [0-9,]+ \([^)]*\) · Nasdaq [0-9,]+ · FTSE 100 [0-9,]+ · Russell [0-9,]+ · VIX \d+\.\d+',
          f"Dow {fmt_int(y['DOW']['last'])} ({fmt_pct2(dpct('DOW'))}) · Nasdaq {fmt_int(y['NDX']['last'])} · FTSE 100 {fmt_int(y['FTSE']['last'])}"
          f" · Russell {fmt_int(y['RUT']['last'])} · VIX {y['VIX']['last']:.2f}", "modal equities blurb")
    if "WTI" in y:
        S(r'(<div class="price">)WTI \$[0-9.,]+(</div>)', lambda m: m.group(1) + "WTI " + p("WTI") + m.group(2), "modal WTI")
    if all(k in y for k in ("BRENT", "DXY", "GOLD")):
        S(r'Brent \$[0-9.,]+ \(BZ=F\) · DXY [0-9.]+ \([^)]*\) · Gold ~\$[0-9,]+',
          f"Brent {p('BRENT')} (BZ=F) · DXY {y['DXY']['last']:.2f} ({fmt_pct2(dpct('DXY'))}) · Gold ~{p('GOLD')}", "modal oil blurb")
    asof = f"{dshort(now)} ~{(now.hour % 12) or 12}:{now.minute:02d}{'am' if now.hour < 12 else 'pm'} London"
    if "WTI" in y:
        S(r'Yahoo futures (?:live|as of) [A-Z][a-z]{2} \d{1,2} [A-Z][a-z]{2}(?: ~\d{1,2}:\d{2}[ap]m London)?', f"Yahoo futures as of {asof}", "modal futures stamp")
    if "TLT" in y:
        S(r'(BND \$[0-9.]+ \([^)]*\)\. )Yahoo (?:live|close) [A-Z][a-z]{2} \d{1,2} [A-Z][a-z]{2}', lambda m: m.group(1) + "Yahoo close " + dshort(y["TLT"]["asof"]), "modal bond stamp")
    if y:
        S(r'(gold: )Yahoo Finance (?:live|as of) [A-Z][a-z]{2} \d{1,2} [A-Z][a-z]{2}(?: ~\d{1,2}:\d{2}[ap]m London)?', lambda m: m.group(1) + "Yahoo Finance as of " + asof, "modal sources yahoo")
    if "SPX" in y:
        start6 = None
        try:
            start6 = ctx["spx_6m_start"]
        except KeyError:
            pass
        if start6:
            S(r'(Six-month sparklines: Yahoo daily closes \(~)[A-Z][a-z]{2}\u2013[A-Z][a-z]{2} \d{4}(\))',
              lambda m: m.group(1) + f"{start6:%b}\u2013{y['SPX']['asof']:%b %Y}" + m.group(2), "modal sparkline months")

    # macro text
    if "CPI" in fr:
        cpi_d, cpi_v = fr["CPI"]
        S(r'(<div class="price">CPI )[0-9.]+%(</div>)', lambda m: m.group(1) + f"{cpi_v:.1f}%" + m.group(2), "modal CPI")
        S(r'CPI-U [A-Z][a-z]{2} YoY \(BLS\)', f"CPI-U {cpi_d:%b} YoY (BLS)", "modal CPI month")
        S(r'(CPI: BLS )[A-Z][a-z]{2} \d{4}', lambda m: m.group(1) + f"{cpi_d:%b %Y}", "modal sources CPI")
    if "CPILFESL" in fr:
        S(r'<span class="pct flat">Core [0-9.]+%</span>', f'<span class="pct flat">Core {fr["CPILFESL"][1]:.1f}%</span>', "modal core CPI")
    nxt = ctx.get("cpi_next")
    if nxt:
        S(r'; [A-Z][a-z]{2} CPI due \d{1,2} [A-Z][a-z]{2}\.', f"; {nxt[0]:%b} CPI due {nxt[1].day} {nxt[1]:%b}.", "modal CPI due")
    if "JOBLESS" in fr:
        ud, uv = fr["JOBLESS"]
        S(r'<span class="pct ([a-z]+)">U-3 [0-9.]+%</span>', lambda m: f'<span class="pct {m.group(1)}">U-3 {uv:.1f}%</span>', "modal U-3")
        S(r'Unemployment [A-Z][a-z]{2} [0-9.]+%', f"Unemployment {ud:%b} {uv:.1f}%", "modal unemployment")
        S(r'(Unemployment / NFP: BLS )[A-Z][a-z]{2} \d{4}', lambda m: m.group(1) + f"{ud:%b %Y}", "modal sources unemployment")
    if "PAYEMS" in fr:
        nfp = fr["PAYEMS"]
        def nfp_rep(m):
            same = m.group(1) == f"{nfp:+d}"
            return f"NFP {nfp:+d}k" + ((m.group(2) or "") if same else "")
        S(r'NFP ([+\u2212-]?\d+)k( \([^)]*\))?', nfp_rep, "modal NFP")
    if "GDP" in fr:
        gd, gv = fr["GDP"]
        q = (gd.month - 1) // 3 + 1
        S(r'(<div class="price">GDP )[+\u2212-]?[0-9.]+%(</div>)', lambda m: m.group(1) + ("+" if gv >= 0 else MINUS) + f"{abs(gv):.1f}%" + m.group(2), "modal GDP")

        def gdp_rep(m):
            return f"Real GDP Q{q} SAAR" + ((m.group(2) or "") if m.group(1) == str(q) else ", latest estimate")
        S(r'Real GDP Q(\d) SAAR(, [a-z]+ estimate)?', gdp_rep, "modal GDP quarter")

        def gdp_src(m):
            return f"GDP: BEA Q{q} {gd.year}" + (m.group(3) if m.group(1) == str(q) and m.group(2) == str(gd.year) else " latest estimate")
        S(r'GDP: BEA Q(\d) (\d{4})( [a-z]+ estimate)', gdp_src, "modal sources GDP")
    if "FED" in fr:
        fd, fv = fr["FED"]
        S(r'<span class="pct flat">EFFR [0-9.]+%</span>', f'<span class="pct flat">EFFR {fv:.2f}%</span>', "modal EFFR")
        S(r'Effective fed funds [A-Z][a-z]{2} [0-9.]+% \(FRED\)', f"Effective fed funds {fd:%b} {fv:.2f}% (FRED)", "modal fed funds")
        S(r'(Fed funds: FRED FEDFUNDS )[A-Z][a-z]{2} \d{4}', lambda m: m.group(1) + f"{fd:%b %Y}", "modal sources fed")


# --------------------------------------------------------------------------- git
def git(*args, check=True):
    r = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r


def git_push(stamp):
    git("add", *FILES)
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        log("git: nothing to commit (files unchanged)")
    else:
        git("commit", "-m", f"Economics HUDs: data refresh {stamp}")
        log("git: committed " + git("rev-parse", "--short", "HEAD").stdout.strip())
    for i in range(5):
        pr = git("pull", "--rebase", "origin", "main", check=False)
        if pr.returncode != 0:
            git("rebase", "--abort", check=False)
            log(f"git pull --rebase failed (attempt {i + 1}): {pr.stderr.strip()[:300]}")
        else:
            ps = git("push", "origin", "HEAD:main", check=False)
            if ps.returncode == 0:
                log("git: pushed " + git("rev-parse", "--short", "HEAD").stdout.strip() + " to origin/main")
                return True
            log(f"git push failed (attempt {i + 1}): {ps.stderr.strip()[:300]}")
        time.sleep(5 * (i + 1))
    return False


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--push", action="store_true", help="git add/commit/pull --rebase/push after rebuild")
    ap.add_argument("--dry-run", action="store_true", help="fetch and report only; do not write files")
    args = ap.parse_args()

    now = dt.datetime.now(LONDON)
    today_ny = dt.datetime.now(ZoneInfo("America/New_York")).date()
    stamp = now.strftime("%Y-%m-%d %H:%M London")
    log(f"Economics HUD rebuild @ {london_stamp(now)}  (repo {REPO})")

    texts = {f: open(os.path.join(REPO, f), encoding="utf-8").read() for f in FILES}
    DATA, _ = extract_json(texts[FILES[0]], "DATA")
    REF, _ = extract_json(texts[FILES[0]], "REF")
    updated, kept = [], []
    ctx = {"now": now, "yahoo_meta": {}, "fred": {}}

    # ---- Yahoo
    log("Yahoo daily history ...")
    for key, sym in YAHOO.items():
        if key not in DATA:
            continue
        try:
            series = fetch_yahoo(sym)
            e, meta = build_daily(key, series, DATA[key])
            old_last = DATA[key]["last"]
            if old_last and abs(e["last"] / old_last - 1) > 0.5:
                log(f"  ! {key}: last moved >50% ({old_last} -> {e['last']}); accepting but check")
            DATA[key] = e
            ctx["yahoo_meta"][key] = meta
            if key == "SPX":
                ctx["spx_6m_start"] = series[-DAILY_N["6M"]][0]
            updated.append(f"{key} ({sym}) last {e['last']:.4g} @ {meta['asof']}")
        except Exception as ex:
            kept.append(f"{key} ({sym}): {ex}")
        time.sleep(0.4)
    # vs core (price kinds only) from whatever SPX chg we have
    for key, e in DATA.items():
        if key in YAHOO and key in ctx["yahoo_meta"] or key == CORE:
            for rg in RANGES:
                e["vsCore"][rg] = (round(e["chg"][rg] - DATA[CORE]["chg"][rg], 2)
                                   if e["kind"] == "price" else None)

    # ---- FRED
    log("FRED macro ...")
    for key, sid in FRED_MACRO.items():
        try:
            s = fetch_fred(sid)
            raw = yoy(s, None) if key == "CPI" else s
            if key == "CPI":
                s = yoy(s)
            DATA[key] = build_macro(key, s, DATA[key])
            ctx["fred"][key] = (raw[-1][0], raw[-1][1])  # unrounded, for 1-dp modal text
            updated.append(f"{key} ({sid}) {DATA[key]['last']} @ {DATA[key]['end']}")
        except Exception as ex:
            kept.append(f"{key} ({sid}): {ex}")
    for sid in FRED_EXTRA:
        try:
            s = fetch_fred(sid)
            if sid == "CPILFESL":
                y = yoy(s, None)
                ctx["fred"][sid] = (y[-1][0], y[-1][1])
                updated.append(f"modal Core CPI ({sid}) {y[-1][1]:.2f}% YoY @ {y[-1][0]}")
            else:
                ctx["fred"][sid] = int(round(s[-1][1] - s[-2][1]))
                updated.append(f"modal NFP ({sid}) {ctx['fred'][sid]:+d}k @ {s[-1][0]}")
        except Exception as ex:
            kept.append(f"modal {sid}: {ex}")

    # ---- Treasury
    log("Treasury.gov par yield curve (slow) ...")
    try:
        rows = fetch_treasury(today_ny)
        if len(rows) < REF_N["1Y"] + 2:
            raise RuntimeError(f"only {len(rows)} rows")
        REF, day = build_ref(rows)
        (d1, r1), (d0, r0) = rows[-1], rows[-2]
        cols = ["3 Mo", "2 Yr", "5 Yr", "7 Yr", "10 Yr", "20 Yr", "30 Yr"]
        curve = {c: float(r1[c]) for c in cols}
        ctx["tsy"] = {"date": d1, "prev_date": d0, "day": day, "curve": curve,
                      "daycols": {c: round((float(r1[c]) - float(r0[c])) * 100) for c in cols},
                      "s2s10": round((curve["10 Yr"] - curve["2 Yr"]) * 100)}
        updated.append("REF 3M/2Y/7Y/20Y @ %s: %s" % (d1, " ".join(f"{k} {v['last']:.2f}" for k, v in REF.items())))
    except Exception as ex:
        kept.append(f"REF (Treasury.gov): {ex}")

    # ---- BLS schedule
    try:
        sched = fetch_cpi_schedule()
        cut = dt.datetime.now(ZoneInfo("America/New_York"))
        fut = [(rf, rl) for rf, rl in sched if dt.datetime(rl.year, rl.month, rl.day, 8, 30, tzinfo=ZoneInfo("America/New_York")) > cut]
        if fut:
            ctx["cpi_next"] = min(fut, key=lambda x: x[1])
            updated.append(f"CPI next: {ctx['cpi_next'][0]:%b %Y} print due {ctx['cpi_next'][1]}")
    except Exception as ex:
        kept.append(f"CPI next note (BLS schedule): {ex}")

    data_updates = [u for u in updated if not u.startswith(("CPI next", "modal"))]
    # ---- write
    warns = []
    for f in FILES:
        doc = Doc(f, texts[f])
        update_html(doc, DATA, REF, ctx)
        warns += doc.warn
        bad = [a for a in ("\u2191", "\u2193", "\u25b2", "\u25bc") if a in doc.s]
        if bad:
            warns.append(f"{f}: unicode arrows present {bad}")
        if args.dry_run:
            log(f"[dry-run] {f}: would change {sum(1 for a, b in zip(texts[f].splitlines(), doc.s.splitlines()) if a != b)} lines")
        elif doc.s != texts[f]:
            with open(os.path.join(REPO, f), "w", encoding="utf-8") as fh:
                fh.write(doc.s)
            log(f"wrote {f}")
    if not os.path.exists(os.path.join(REPO, ".nojekyll")) and not args.dry_run:
        open(os.path.join(REPO, ".nojekyll"), "w").close()

    log("\n==== SUMMARY ====")
    log(f"UPDATED ({len(updated)}):")
    for u in updated:
        log("  + " + u)
    log(f"KEPT previous values ({len(kept)}):")
    for k in kept:
        log("  = " + k)
    for w in warns:
        log("  ! " + w)
    if not data_updates:
        log("Nothing could be updated -> exit 2")
        return 2
    if args.push and not args.dry_run:
        if not git_push(stamp):
            log("PUSH FAILED -> exit 3")
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
