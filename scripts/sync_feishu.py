#!/usr/bin/env python3
"""Sync the Feishu PS Plus catalog to a static JSON consumed by the userscript."""
import argparse
import csv
import datetime
import html
import json
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

FEISHU_HOST = "https://open.feishu.cn"
STEAM_HOST = "https://store.steampowered.com"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_PATH = os.path.join(ROOT, "data", "psplus-games.json")

# Hardcoded: the "状态" field name is fixed in the Feishu bitable.
STATUS_FIELD_NAME = "状态"

# Display text per tier/status option.
STATUS_DISPLAY = {
    "1": {
        "即将会免": "即将会免",
        "领取中": "会免领取中",
        "截止领取": "会免过",
    },
    "2": {
        "即将入库": "即将入库",
        "在库": "在库",
        "即将出库": "即将出库",
        "已出库": "已出库",
    },
    "3": {
        "即将入库": "即将入库",
        "在库": "在库",
        "即将出库": "即将出库",
        "已出库": "已出库",
    },
}

STATUS_OPTION_FALLBACK = {
    "optWou7g3R": {"name": "即将会免", "color": 2},
    "optoMnMCF1": {"name": "领取中", "color": 7},
    "opt47fY4xF": {"name": "截止领取", "color": 5},
    "optyVkiXTy": {"name": "即将入库", "color": 2},
    "optVPBodJh": {"name": "在库", "color": 7},
    "opt8bVNI6h": {"name": "即将出库", "color": 3},
    "optgcaHGyb": {"name": "已出库", "color": 5},
    "optCmSC3ui": {"name": "", "color": 6},
}


def _request_raw(method, url, token=None, data=None, timeout=30):
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; PSPlusSync/1.0)",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        sys.stderr.write("HTTPError %s for %s: %s\n" % (e.code, url,
                         e.read().decode("utf-8", "replace")))
        raise


def http(method, url, token=None, data=None, timeout=30):
    return json.loads(_request_raw(method, url, token=token, data=data, timeout=timeout))


STEAM_DELAY = float(os.environ.get("STEAM_REQUEST_DELAY", "1.0"))
STEAM_MAX_RETRIES = int(os.environ.get("STEAM_MAX_RETRIES", "4"))
STEAM_RETRY_BASE = 2.0

_steam_lock = threading.Lock()
_last_steam = {"t": 0.0}


def steam_request(method, url, timeout=30, max_retries=None):
    """GET a Steam URL with global rate limiting + 429/5xx retry/backoff."""
    global STEAM_MAX_RETRIES
    if max_retries is None:
        max_retries = STEAM_MAX_RETRIES
    for attempt in range(max_retries + 1):
        with _steam_lock:
            elapsed = time.monotonic() - _last_steam["t"]
            if elapsed < STEAM_DELAY:
                time.sleep(STEAM_DELAY - elapsed)
            _last_steam["t"] = time.monotonic()
        try:
            return _request_raw(method, url, timeout=timeout)
        except urllib.error.HTTPError as e:
            retryable = e.code in (429, 500, 502, 503, 504)
            if retryable and attempt < max_retries:
                ra = getattr(e, "headers", {}).get("Retry-After")
                if ra and str(ra).isdigit():
                    wait = float(ra)
                else:
                    wait = STEAM_RETRY_BASE * (2 ** attempt)
                sys.stderr.write(
                    "steam HTTP %s for %s (attempt %d/%d), sleeping %.1fs\n"
                    % (e.code, url, attempt + 1, max_retries, wait))
                time.sleep(wait)
                continue
            raise


def get_tenant_token(app_id, app_secret):
    url = FEISHU_HOST + "/open-apis/auth/v3/tenant_access_token/internal"
    r = http("POST", url, data={"app_id": app_id, "app_secret": app_secret})
    if r.get("code") != 0:
        raise RuntimeError("get tenant token failed: %r" % r)
    return r["tenant_access_token"]


def list_fields(app_token, table_id, token):
    url = (FEISHU_HOST + "/open-apis/bitable/v1/apps/%s/tables/%s/fields?page_size=500"
           % (app_token, table_id))
    r = http("GET", url, token=token)
    if r.get("code") != 0:
        raise RuntimeError("list fields failed: %r" % r)
    return r["data"]["items"]


def list_records(app_token, table_id, token):
    items = []
    page_token = None
    while True:
        url = (FEISHU_HOST + "/open-apis/bitable/v1/apps/%s/tables/%s/records?page_size=500"
               % (app_token, table_id))
        if page_token:
            url += "&page_token=" + urllib.parse.quote(page_token)
        r = http("GET", url, token=token)
        if r.get("code") != 0:
            raise RuntimeError("list records failed: %r" % r)
        items.extend(r["data"]["items"])
        if r["data"].get("has_more"):
            page_token = r["data"]["page_token"]
        else:
            break
    return items


def normalize(text):
    """Normalize a name for forgiving cross-platform matching (lowercase, NFKC, keep alnum/CJK)."""
    if not text:
        return ""
    s = str(text).lower()
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", s)
    return s


def parse_tier(value):
    if isinstance(value, list):
        value = value[0] if value else ""
    if not value:
        return ""
    m = re.search(r"[123]", str(value))
    return m.group(0) if m else ""


def first_text(value):
    """Coerce a Feishu field value (str / list / list[{text}]) to a plain string."""
    if value is None or value == "":
        return ""
    if isinstance(value, list):
        if not value:
            return ""
        item = value[0]
        if isinstance(item, dict):
            return str(item.get("text", ""))
        return str(item)
    return str(value)


def parse_feishu_date(raw):
    """Feishu date field -> 'YYYY年M月D日', or None.

    Accepts a 'YYYY-MM-DD'/ISO string or a Unix timestamp (seconds/millis).
    Date fields come back as a UTC millisecond number at 00:00:00; we shift
    to UTC+8 so the printed day matches the Feishu calendar."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.lstrip("-").isdigit()):
        try:
            ts = float(raw)
        except (ValueError, TypeError):
            return None
        if ts > 1e12:        # milliseconds
            ts /= 1000.0
        try:
            d = (datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc) +
                 datetime.timedelta(hours=8)).date()
        except (ValueError, OverflowError, OSError):
            return None
        return "%d年%d月%d日" % (d.year, d.month, d.day)
    # String date / datetime.
    s = str(raw).replace("T", " ").split(" ")[0]
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if not m:
        return None
    return "%d年%d月%d日" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def month_sort(text):
    nums = re.findall(r"\d+", text or "")
    if len(nums) >= 2:
        return "%04d-%02d" % (int(nums[0]), int(nums[1]))
    return ""


def _nth_weekday(year, month, n, weekday=1):
    # weekday: Mon=0..Sun=6. PS Plus refreshes on Tuesdays (1).
    first = datetime.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + datetime.timedelta(days=offset + 7 * (n - 1))


def _next_month(year, month, delta=1):
    total = (year * 12 + (month - 1)) + delta
    return total // 12, total % 12 + 1


def status_date(ms, tier, status_name, exit_date=None):
    if status_name in ("即将出库", "已出库"):
        return exit_date or "出库时间未知"
    if not ms or len(ms) < 7:
        return None
    try:
        year, month = int(ms[:4]), int(ms[5:7])
    except ValueError:
        return None
    if tier == "1":
        if status_name == "即将会免":
            d = _nth_weekday(year, month, 1)
        elif status_name in ("领取中", "会免过", "截止领取"):
            y, m = _next_month(year, month)
            d = _nth_weekday(y, m, 1)
        else:
            return None
    elif tier in ("2", "3"):
        if status_name in ("即将入库", "在库"):
            d = _nth_weekday(year, month, 3)
        else:
            return None
    else:
        return None
    return "%d年%d月%d日" % (d.year, d.month, d.day)


def build_status_options(fields, status_field_name):
    for f in fields:
        if f.get("name") == status_field_name:
            prop = f.get("property", {}) or {}
            opts = prop.get("options", []) or []
            m = {}
            for o in opts:
                oid = o.get("id")
                oname = o.get("name", "")
                if oid:
                    m[oid] = {"name": oname, "color": o.get("color")}
                if oname:
                    m[oname] = {"name": oname, "color": o.get("color")}
            return m
    return {}


# Each result row is an <a> carrying the numeric app id, with its title in a span.title.
_SEARCH_LINK_RE = re.compile(
    r'<a\b[^>]*href="https?://store\.steampowered\.com/app/(\d+)/[^"]*"',
    re.IGNORECASE,
)
_TITLE_RE = re.compile(r'class="title"[^>]*>([^<]*)', re.IGNORECASE)


def parse_search_html(html_text):
    """Parse the /search/?term= results page into a list of {id, name, url}."""
    items = []
    start = html_text.find('id="search_resultsRows"')
    region = html_text[start:] if start != -1 else html_text
    for m in _SEARCH_LINK_RE.finditer(region):
        app_id = int(m.group(1))
        # Scan only within this anchor so its title stays paired with its id.
        close = region.find("</a>", m.end())
        seg = region[m.start():close] if close != -1 else region[m.start():m.start() + 4000]
        tm = _TITLE_RE.search(seg)
        name = html.unescape(tm.group(1).strip()) if tm else ""
        items.append({
            "id": app_id,
            "name": name,
            "url": "https://store.steampowered.com/app/%d/" % app_id,
        })
    return items


def _search_web_once(term, lang, cc):
    url = (STEAM_HOST + "/search/?term=%s&cc=%s&l=%s"
           % (urllib.parse.quote(term), cc, lang))
    try:
        body = steam_request("GET", url)
    except Exception as e:
        sys.stderr.write("steam web search failed for %r: %s\n" % (term, e))
        return []
    return parse_search_html(body)


def search_steam_web(term, lang):
    """Search the full /search/?term= results page (cc=us first, then cc=cn); exact-name match else top result."""
    primary_cc = "us"
    secondary_cc = "cn"
    q = normalize(term)
    for cc in (primary_cc, secondary_cc):
        items = _search_web_once(term, lang, cc)
        if not items:
            continue
        for it in items:
            if normalize(it.get("name", "")) == q:
                return it
        return items[0]
    return None


def search_steam_storesearch(term, lang):
    # Fallback used only when the results page is empty; cc=us.
    cc = "us"
    url = (STEAM_HOST + "/api/storesearch/?term=%s&cc=%s&l=%s"
           % (urllib.parse.quote(term), cc, lang))
    try:
        body = steam_request("GET", url)
    except Exception as e:
        sys.stderr.write("steam storesearch failed for %r: %s\n" % (term, e))
        return None
    try:
        r = json.loads(body)
    except ValueError:
        return None
    if not isinstance(r, dict):
        return None
    items = r.get("items") or []
    if not items:
        return None
    q = normalize(term)
    for it in items:
        if normalize(it.get("name", "")) == q:
            return it
    return items[0]


def search_steam(term, lang):
    """Search Steam: results page first, storesearch fallback."""
    it = search_steam_web(term, lang)
    if it:
        return it
    return search_steam_storesearch(term, lang)


def resolve_steam(name_en, name_cn):
    """Return (app_id, steam_name) by searching Steam; English first, then Chinese."""
    for cand, lang in ((name_en, "english"), (name_cn, "schinese")):
        if not cand:
            continue
        it = search_steam(cand, lang)
        if it:
            return it.get("id"), it.get("name")
    return None, None


def load_previous():
    """Load the prior output keyed by Feishu record_id (cache for Steam lookups)."""
    try:
        with open(OUTPUT_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}
    return {g.get("recordId"): g for g in data.get("games", []) if g.get("recordId")}


def load_seed(path):
    """Load the proofread recordId -> steamAppId mapping (verbatim; 's<number>' = sub id, '' = no Steam match)."""
    if not path:
        return {}
    try:
        fh = open(path, encoding="utf-8-sig")
    except (FileNotFoundError, OSError):
        return {}
    mapping = {}
    with fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rid = (row.get("recordId") or "").strip()
            if not rid:
                continue
            mapping[rid] = (row.get("steamAppId") or "").strip()
    return mapping


def main():
    parser = argparse.ArgumentParser(
        description="Sync Feishu PS Plus catalog to JSON.")
    parser.add_argument(
        "--delay", type=float, default=None,
        help="Minimum seconds between Steam requests (default 1.0, or env STEAM_REQUEST_DELAY).")
    parser.add_argument(
        "--retries", type=int, default=None,
        help="Max retries on Steam 429/5xx (default 4, or env STEAM_MAX_RETRIES).")
    parser.add_argument(
        "--force-steam", action="store_true", default=False,
        help="Ignore cached Steam appIds and re-resolve every record from Steam "
             "(use once to clear stale/wrong matches after changing the search logic).")
    parser.add_argument(
        "--seed-csv", nargs="?", const=os.path.join(ROOT, "data", "data.csv"),
        default=None,
        help="Proofread mapping CSV (recordId,steamAppId). When a record_id is "
             "present here, its id is used verbatim and that record is never "
             "re-queried from Steam. Sub ids are written as 's<number>' (e.g. "
             "'s93325'); plain numbers are normal app ids. Only Feishu rows "
             "ABSENT from this file are resolved via Steam search. Defaults to "
             "data/data.csv if it exists (so dropping a proofread data.csv into "
             "the repo just works).")
    args = parser.parse_args()

    global STEAM_DELAY, STEAM_MAX_RETRIES
    if args.delay is not None:
        STEAM_DELAY = args.delay
    if args.retries is not None:
        STEAM_MAX_RETRIES = args.retries

    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    app_token = os.environ.get("FEISHU_APP_TOKEN")
    raw_table_ids = os.environ.get("FEISHU_TABLE_IDS") or os.environ.get("FEISHU_TABLE_ID")
    table_ids = [t.strip() for t in (raw_table_ids or "").split(",") if t.strip()]

    missing = [name for name, val in (
        ("FEISHU_APP_ID", app_id),
        ("FEISHU_APP_SECRET", app_secret),
        ("FEISHU_APP_TOKEN", app_token),
    ) if not val]
    if missing:
        raise SystemExit("Missing required env vars: " + ", ".join(missing))
    if not table_ids:
        raise SystemExit("FEISHU_TABLE_IDS (or FEISHU_TABLE_ID) env var is required")

    token = get_tenant_token(app_id, app_secret)

    status_options = dict(STATUS_OPTION_FALLBACK)
    for tid in table_ids:
        fields = list_fields(app_token, tid, token)
        opts = build_status_options(fields, STATUS_FIELD_NAME)
        print("table %s: loaded %d status options from fields" % (tid, len(opts)))
        status_options.update(opts)

    prev_by_record = load_previous()

    seed_path = args.seed_csv
    if seed_path is None and os.path.exists(os.path.join(ROOT, "data", "data.csv")):
        seed_path = os.path.join(ROOT, "data", "data.csv")
    seed_map = load_seed(seed_path)
    if seed_map:
        print("Loaded %d proofread ids from %s" % (len(seed_map), seed_path))

    steam_calls = 0
    new_records = 0
    status_changes = 0
    unmatched_status = set()

    games = []
    for tid in table_ids:
        records = list_records(app_token, tid, token)
        for rec in records:
            f = rec.get("fields", {}) or {}
            record_id = rec.get("record_id", "")
            prev = prev_by_record.get(record_id, {})
            if not prev:
                new_records += 1

            tier = parse_tier(f.get("档位"))
            if not tier:
                continue

            name_cn = first_text(f.get("中文名称"))
            name_en = first_text(f.get("英文名称"))

            # Tier 1 uses 会免月份; tier 2/3 uses 入库月份. Fallback between them.
            month = first_text(f.get("会免月份")) or first_text(f.get("入库月份"))
            # Explicit exit date (tier 2/3 games leave on staggered dates).
            exit_date = parse_feishu_date(f.get("出库日期"))

            status_raw = f.get(STATUS_FIELD_NAME)
            status_id = ""
            status_text = ""
            if isinstance(status_raw, list) and status_raw:
                v = status_raw[0]
                if isinstance(v, dict):
                    status_id = v.get("id", "") or ""
                    status_text = v.get("text") or v.get("name", "") or ""
                else:
                    status_id = str(v)
                    status_text = str(v)
            elif isinstance(status_raw, dict):
                status_id = status_raw.get("id", "") or ""
                status_text = status_raw.get("text") or status_raw.get("name", "") or ""
            elif status_raw:
                status_id = str(status_raw)
                status_text = str(status_raw)
            if isinstance(status_raw, list) and status_raw and isinstance(status_raw[0], dict):
                v = status_raw[0]
                if v.get("id") and v.get("text"):
                    status_options[v["id"]] = {"name": v["text"], "color": v.get("color")}
            opt = status_options.get(status_id) or status_options.get(status_text)
            if opt:
                status_name = opt.get("name", "")
                color = opt.get("color")
            else:
                status_name = status_text or status_id
                color = None
                if status_id or status_text:
                    unmatched_status.add(status_id or status_text)

            display_map = STATUS_DISPLAY.get(tier, {})
            status_display = display_map.get(status_name, status_name)
            ms = month_sort(month)
            status_date_str = status_date(ms, tier, status_name, exit_date)

            if prev and prev.get("statusName") is not None:
                prev_status = prev.get("statusName")
                prev_date = prev.get("statusDate")
                if prev_status != status_name:
                    status_changes += 1
                    print("status change %s: %s -> %s" % (record_id, prev_status, status_name))
                elif prev_date != status_date_str:
                    status_changes += 1
                    print("status date change %s (%s): %s -> %s"
                          % (record_id, status_name, prev_date, status_date_str))

            psstore = f.get("psstore")
            if isinstance(psstore, dict):
                psstore = psstore.get("link") or psstore.get("text") or ""
            elif not isinstance(psstore, str):
                psstore = ""

            versions = f.get("版本") or []
            if not isinstance(versions, list):
                versions = [versions]

            # Precedence: proofread CSV > previous cache > Steam search (new records only).
            seed_val = seed_map.get(record_id)   # raw string ("s..." for subs) or None
            steam_app_id = None
            if seed_val is not None:
                steam_app_id = seed_val if seed_val != "" else None
            else:
                if not args.force_steam:
                    steam_app_id = prev.get("steamAppId")
                if not steam_app_id:
                    steam_app_id = resolve_steam(name_en, name_cn)[0]
                    if steam_app_id:
                        steam_calls += 1

            games.append({
                "recordId": record_id,
                "steamAppId": steam_app_id,
                "tier": int(tier),
                "month": month,
                "monthSort": ms,
                "statusName": status_name,
                "statusDisplay": status_display,
                "statusDate": status_date_str,
                "versions": versions,
                "psstore": psstore,
                "color": color,
            })

    games.sort(key=lambda g: (g["monthSort"], g["tier"]), reverse=True)

    published_fields = ("recordId", "steamAppId", "tier", "month",
                        "monthSort", "statusName", "statusDisplay", "statusDate",
                        "versions", "psstore", "color")
    json_games = [{k: g[k] for k in published_fields if k in g} for g in games]

    out = {
        "updatedAt": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(json_games),
        "games": json_games,
    }
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print("Wrote %d games to %s (steam lookups: %d, new records: %d, status changes: %d)"
          % (len(games), OUTPUT_PATH, steam_calls, new_records, status_changes))
    if unmatched_status:
        sample = ", ".join(sorted(unmatched_status)[:10])
        print("WARN: %d status value(s) could not be resolved to an option name: %s"
              % (len(unmatched_status), sample))


if __name__ == "__main__":
    main()
