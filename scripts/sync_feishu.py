#!/usr/bin/env python3
"""Sync the Feishu PS Plus catalog to a static JSON consumed by the userscript.

Each record's Steam app id is resolved with this precedence: proofread
``data.csv`` seed > previous cached value > ludoteca.dev curated cross-store
table (corroborated against the Steam store name) > Steam name search kept only
when the candidate's name is close enough to the record's.

Records that are new to Feishu and not in the seed are reported to a Telegram
chat once they are resolved (and a short list is sent for ones that did not
match), so bad matches can be spotted early.

The output file is only rewritten when something other than ``updatedAt``
changed, so a day with no data movement produces no commit at all.
"""
import argparse
import csv
import datetime
import difflib
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

try:
    import ludoteca
except ImportError:
    # Allow running as `python -m scripts.sync_feishu` from the repo root.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import ludoteca
    except ImportError:
        ludoteca = None

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
    """Normalize a name for forgiving cross-platform matching.

    Lowercase, compatibility-decompose + fold accents to ASCII (so
    ``Ragnarök`` == ``Ragnarok``), then keep only alnum/CJK.
    """
    if not text:
        return ""
    s = str(text).lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", s)
    return s


# Word tokens (lowercased, accents folded, possessives dropped) -- used to
# require that a Steam candidate carries the record's discriminating words and
# nothing that turns it into a different product. e.g. the year in "Monster
# Energy Supercross 2025", the "3" in "RollerCoaster Tycoon 3".
_TOKEN_RE = re.compile(r"[0-9a-z\u4e00-\u9fff]+")
_POSSESSIVE_RE = re.compile(r"['\u2019]s\b")
_STOPWORDS = frozenset({"the", "of", "a", "an", "and", "or", "in", "on", "to", "vs", "at"})
# Extra words that mean the store entry is not the game itself.
_NON_GAME_TOKENS = frozenset({
    "soundtrack", "soundtracks", "ost", "artbook", "artbooks", "wallpaper",
    "wallpapers", "demo", "avatar", "avatars", "theme", "themes", "ringtone",
    "ringtones", "screensaver", "screensavers",
})
# Words that mark the same product re-issued or repackaged -- "Horizon
# Forbidden West" -> "... Complete Edition", "Darksiders II" -> "... Deathinitive
# Edition". A candidate that extends the record name and mentions one of these is
# accepted; a tail without any of them (a sequel number, "Prison", a subtitle)
# means a *different* product and is dropped.
_EDITION_WORDS = frozenset({
    "edition", "version", "complete", "deluxe", "definitive", "remaster",
    "remastered", "redux", "goty", "ultimate", "enhanced", "encore",
    "anniversary", "collection", "trilogy", "bundle", "legendary", "imperial",
    "royal", "extended", "final", "cut", "director", "directors", "hd",
    "platinum", "intergrade", "classic", "standard", "video", "game",
})
# Add-on words and bare modifiers (DLC/season pass/upgrade/"Special Color"...).
# They are not discriminators either, but an *add-on* tail is a different
# product from the base game, so unlike the edition words above they do not make
# a prefix-extension acceptable.
_ADDON_WORDS = frozenset({
    "pack", "pass", "season", "expansion", "dlc", "addon", "add", "on",
    "content", "upgrade", "kit", "bonus", "plus", "special", "digital", "gold",
    "premium",
})
# Unambiguous "this is an add-on, not the game" words. An edition word alone is
# not enough to accept an extension: "Dead Space" must not match "Dead Space -
# Digital Deluxe *Upgrade*", nor "Elden Ring" "Shadow of the Erdtree - Deluxe
# *Upgrade*". Bare modifiers (special/digital/gold/premium) stay out of this set
# because they legitimately prefix an edition ("Gold Edition").
_TAIL_BLOCKERS = frozenset({
    "pack", "pass", "season", "expansion", "dlc", "addon", "add", "on",
    "content", "upgrade", "kit", "bonus",
})
_TAIL_QUALIFIERS = _EDITION_WORDS | _ADDON_WORDS


def name_tokens(text):
    """Word tokens of a name: lowercase, accents folded, possessives dropped."""
    s = str(text or "").lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = _POSSESSIVE_RE.sub("", s)
    return _TOKEN_RE.findall(s)


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


def search_steam_candidates(term, lang, max_candidates=4):
    """Ranked Steam search candidates (exact normalized-name matches first).

    Tries the results page (cc=us, then cc=cn); falls back to storesearch only
    when the page is empty. Deduped by app id, capped at ``max_candidates``.
    """
    q = normalize(term)
    items = _search_web_once(term, lang, "us")
    if not items:
        items = _search_web_once(term, lang, "cn")
    if not items:
        it = search_steam_storesearch(term, lang)
        return [it] if it else []
    exact = [it for it in items if normalize(it.get("name", "")) == q]
    rest = [it for it in items if normalize(it.get("name", "")) != q]
    out, seen = [], set()
    for it in exact + rest:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        out.append(it)
        if len(out) >= max_candidates:
            break
    return out


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


_app_info_cache = {}


def steam_app_info(app_id):
    """Return {'name', 'type', 'ok'} for a Steam app via appdetails (cached).

    ``name``/``type`` are None when the app has no store page (delisted) or the
    request failed; ``ok`` is True only when a store name was obtained. ``type``
    is informational only -- we no longer reject by store type (PS Plus lists
    chapters/expansions as titles, so a DLC match can be legitimate).
    """
    key = str(app_id)
    if key in _app_info_cache:
        return _app_info_cache[key]
    info = {"name": None, "type": None, "ok": False}
    url = STEAM_HOST + "/api/appdetails?appids=%s&filters=basic" % urllib.parse.quote(key)
    try:
        r = json.loads(steam_request("GET", url))
        d = r.get(key) or {}
        if d.get("success"):
            data = d.get("data") or {}
            info["name"] = data.get("name")
            info["type"] = data.get("type")
            info["ok"] = bool(data.get("name"))
    except Exception as e:
        sys.stderr.write("steam appdetails failed for %s: %s\n" % (key, e))
    _app_info_cache[key] = info
    return info


# A Steam candidate is accepted for a record only when its store name is close
# enough to the record's own name. This replaced the old store-`type` filter:
# DLC/chapter matches are fine (PS Plus often lists a chapter as its own title)
# as long as the names line up; unrelated search hits get dropped instead.
NAME_SIM_MIN = 0.75
# Score given to a record name extended by an edition/add-on tail.
_TAIL_SCORE = 0.9
# Store `type` ranking: a real game page is preferred over a DLC/soundtrack
# page when both names match (the PS Plus entry is usually the game itself).
_TYPE_RANK = {"game": 2}


def _type_rank(store_type):
    if store_type in _TYPE_RANK:
        return _TYPE_RANK[store_type]
    return 1 if not store_type else 0


def _extension_tail(rec_tokens, cand_tokens):
    """Tokens by which one token sequence extends the other, or None if neither.

    ``[]`` when the two are token-identical; ``None`` when they diverge.
    """
    n = len(rec_tokens)
    if n and cand_tokens[:n] == rec_tokens:
        return cand_tokens[n:]
    m = len(cand_tokens)
    if m and rec_tokens[:m] == cand_tokens:
        return rec_tokens[m:]
    return None


def steam_name_match(record_names, cand_name):
    """Judge a Steam candidate name against the record's English/Chinese names.

    Returns ``(score, ok)``; accepted as soon as *any* record name qualifies:

    * every meaningful word of the record name must appear in the candidate
      (so a missing discriminator -- "Monster Energy Supercross **2025**",
      "RollerCoaster Tycoon **3**" -- or a different chapter is dropped);
    * the candidate must not add a number the record lacks (a sequel: "No
      Straight Roads" does not become "**NO STRAIGHT ROADS 2**");
    * if the candidate merely extends the record name, the extra words must
      include a *re-release* word ("... Complete Edition", "Darksiders II
      **Deathinitive Edition**") and must not include an add-on word
      ("... Deluxe **Upgrade**", "... Ruin Rising Pack") -- a tail with no
      re-release word at all ("Dead Star **Prison**") is a different product;
    * otherwise fall back to a name-similarity ratio >= ``NAME_SIM_MIN``.

    Candidates carrying a non-game extra (soundtrack/artbook/demo...) are
    rejected outright. Comparing both names matters when the Chinese name was
    searched but the store name is English (``黑手党 故乡`` -> english page).
    """
    if not cand_name:
        return 0.0, False
    c_norm = normalize(cand_name)
    if not c_norm:
        return 0.0, False
    cand_tokens = name_tokens(cand_name)
    cand_set = set(cand_tokens)

    rec_tokens_all = set()
    for raw in record_names:
        if raw:
            rec_tokens_all.update(name_tokens(raw))
    if (cand_set - rec_tokens_all) & _NON_GAME_TOKENS:
        return 0.0, False

    best = 0.0
    for raw in record_names:
        if not raw:
            continue
        v = normalize(raw)
        if not v:
            continue
        rec_tokens = name_tokens(raw)
        # Edition/add-on words never discriminate one product from another (the
        # store page may or may not carry them), so they are not required; the
        # tail rule below still vets whatever extra words appear.
        meaningful = {t for t in rec_tokens
                      if t not in _STOPWORDS and t not in _TAIL_QUALIFIERS}
        if meaningful and not meaningful <= cand_set:
            continue                                   # discriminator missing
        if any(t.isdigit() for t in cand_set - set(rec_tokens)):
            continue                                   # an added number = sequel
        tail = _extension_tail(rec_tokens, cand_tokens)
        if v == c_norm:
            score = 1.0
        elif tail is not None:
            # The tail must identify a re-release of the same product
            # ("Complete Edition", "Deathinitive Edition"). A tail that only
            # adds DLC words ("... Ruin Rising Pack") is a *different* product
            # from the base game, and one with no such word at all ("Prison")
            # is unrelated. An explicit add-on word ("... Deluxe Upgrade") is
            # rejected even when an edition word is present.
            if not any(t in _EDITION_WORDS for t in tail):
                continue
            if any(t in _TAIL_BLOCKERS for t in tail):
                continue
            score = _TAIL_SCORE
        else:
            score = difflib.SequenceMatcher(None, v, c_norm).ratio()
            if score < NAME_SIM_MIN:
                continue
        return score, True
    return best, False


def _steam_candidate_pool(name_en, name_cn, per_term=5):
    """Deduped Steam search candidates for the English then Chinese name."""
    pool, seen = [], set()
    for term, lang in ((name_en, "english"), (name_cn, "schinese")):
        if not term:
            continue
        for it in search_steam_candidates(term, lang, max_candidates=per_term):
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            pool.append(it)
    return pool


def resolve_steam_game(name_en, name_cn, max_checks=8):
    """Search Steam for the record's game, keeping the best-matching candidate.

    Acceptance is by *name* (see ``steam_name_match``) instead of store type; an
    exact name wins outright, an edition/DLC tail is accepted, an unrelated hit
    is dropped. Among acceptable candidates a store ``type`` of ``game`` is
    preferred (the DLC's page only wins when nothing closer is a game).
    Returns ``(app_id_str, steam_name, type, score)`` or None.
    """
    names = (name_en, name_cn)
    if not any(names):
        return None
    best = None
    for it in _steam_candidate_pool(name_en, name_cn)[:max_checks]:
        info = steam_app_info(it["id"])
        sname = info.get("name") or it.get("name") or ""
        score, ok = steam_name_match(names, sname)
        if not ok:
            continue
        rank = (_type_rank(info.get("type")), score)
        if best is None or rank > best[0]:
            best = (rank, (str(it["id"]), sname, info.get("type"), score))
        if rank == (2, 1.0):
            break   # real game with the exact name: nothing can beat it
    return best[1] if best else None


def resolve_unmatched(ludoteca_idx, limiter, psstore, name_en, name_cn, allow_api=True):
    """Resolve one record to a Steam id: ludoteca (curated) + Steam cross-check.

    Order:
      1. ludoteca bulk index -- exact PS product code > English title > PS SKU
         suffix > Chinese title, accepting only a unique, non-empty Steam id.
      2. ludoteca REST API  -- only when the game is absent from the bulk table.
      3. Steam name search   -- the closest-named candidate above the floor.

    ludoteca is curated but not infallible, so its id is only trusted once the
    Steam store name for that id lines up with the record name; when it does
    not, the name-similar Steam candidate wins (falling back to ludoteca's guess
    if Steam has nothing better). Returns (steam_id_str | None, source | None).
    """
    lud_sid = lud_source = None
    if ludoteca is not None and ludoteca_idx is not None:
        sid, source, status = ludoteca.resolve_local(
            ludoteca_idx, psstore, name_en, name_cn)
        if sid:
            lud_sid, lud_source = str(sid), "ludoteca:" + (source or "")
        elif status == "absent" and allow_api and limiter is not None:
            sid = ludoteca.resolve_api(ludoteca_idx, name_en, name_cn, limiter=limiter)
            if sid:
                lud_sid, lud_source = str(sid), "ludoteca:api"

    if lud_sid:
        sname = steam_app_info(lud_sid).get("name")
        # Unverifiable (delisted / request failed) or name-confirmed -> trust it.
        if not sname or steam_name_match((name_en, name_cn), sname)[1]:
            return lud_sid, lud_source
        sys.stderr.write(
            "ludoteca id %s (%r) name-mismatch for %r -> trying Steam search\n"
            % (lud_sid, sname, name_en or name_cn))

    steam = resolve_steam_game(name_en, name_cn)
    if steam:
        return steam[0], "steam"
    return lud_sid, lud_source


TELEGRAM_API_URL = "https://api.telegram.org/bot%s/sendMessage"
# New records matched in one run: send one message each up to this count, then
# switch to a single digest (avoids flooding the chat on a big import).
TELEGRAM_MAX_INDIVIDUAL = 10
# Seconds between consecutive messages (Telegram tolerates ~1 msg/s per chat).
TELEGRAM_GAP = 0.5
# Telegram caps a message at 4096 chars; stay well below that.
TELEGRAM_TEXT_LIMIT = 3500


def steam_store_url(steam_id):
    """Store URL for an app/sub/bundle id ('s93325' -> /sub/, 'b16609' -> /bundle/)."""
    s = str(steam_id or "").strip()
    if s.isdigit():
        return STEAM_HOST + "/app/%s/" % s
    if len(s) > 1 and s[0] in ("s", "b") and s[1:].isdigit():
        return STEAM_HOST + ("/sub/%s/" if s[0] == "s" else "/bundle/%s/") % s[1:]
    return ""


def telegram_config():
    """(token, chat_id) when configured, else None.

    Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID to enable the new-record notices;
    TELEGRAM_DISABLE=1 turns them off without unsetting the credentials.
    """
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        return None
    if (os.environ.get("TELEGRAM_DISABLE") or "").lower() in ("1", "true", "yes"):
        return None
    return token, chat_id


def telegram_send(token, chat_id, text):
    """Send one HTML message. Returns True on success; never raises."""
    body = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(
        TELEGRAM_API_URL % token, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        if not payload.get("ok"):
            sys.stderr.write("telegram rejected the message: %s\n"
                             % str(payload.get("description"))[:200])
            return False
        return True
    except Exception as e:
        sys.stderr.write("telegram send failed: %s\n" % e)
        return False


def _tg_escape(value):
    return html.escape(str(value or ""), quote=False)


def format_match_message(record_id, steam_id, name_en, name_cn, steam_name, source):
    """One new-record match, as an HTML message."""
    url = steam_store_url(steam_id)
    lines = [
        "<b>PS Plus 新增匹配</b>",
        "recordId: <code>%s</code>" % _tg_escape(record_id),
        "appid: <code>%s</code>" % _tg_escape(steam_id),
        "英文名称: %s" % (_tg_escape(name_en) or "—"),
        "中文名称: %s" % (_tg_escape(name_cn) or "—"),
        "Steam 名称: %s" % (_tg_escape(steam_name) or "（取不到商店名/已下架）"),
        "Steam 链接: %s" % ('<a href="%s">%s</a>' % (url, _tg_escape(url))
                            if url else "—"),
    ]
    if source:
        lines.append("来源: %s" % _tg_escape(source))
    return "\n".join(lines)


def format_match_line(index, record_id, steam_id, name_en, name_cn, steam_name, source):
    """Compact one-entry digest line for a new-record match."""
    url = steam_store_url(steam_id)
    title = _tg_escape(name_en or name_cn) or "—"
    return ("%d. <code>%s</code> · <code>%s</code> · %s\n"
            "    → %s\n    %s%s"
            % (index, _tg_escape(record_id), _tg_escape(steam_id), title,
               _tg_escape(steam_name) or "（无商店名）", url,
               ("  · " + _tg_escape(source)) if source else ""))


def _telegram_chunks(header, lines):
    """Split lines into messages that stay under Telegram's length cap."""
    chunks, cur, size = [], [], len(header)
    for line in lines:
        if cur and size + len(line) + 1 > TELEGRAM_TEXT_LIMIT:
            chunks.append(cur)
            cur, size = [], len(header)
        cur.append(line)
        size += len(line) + 1
    if cur:
        chunks.append(cur)
    return ["%s\n%s" % (header, "\n".join(c)) for c in chunks]


def _notify_new_matches(matched, unmatched):
    """Push the records matched (and not matched) this run to Telegram.

    ``matched`` items are ``(recordId, steam_id, name_en, name_cn, steam_name,
    source)``; ``unmatched`` items are ``(recordId, name_en, name_cn)``. Only
    records that are *new to Feishu and resolved in this run* are passed in, so
    a normal daily run (no new rows) sends nothing.
    Returns the number of matched records reported.
    """
    if not matched and not unmatched:
        return 0
    cfg = telegram_config()
    if cfg is None:
        print("telegram: not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID), "
              "skipping %d match + %d unmatched notice(s)" % (len(matched), len(unmatched)))
        return 0
    token, chat_id = cfg

    sent = 0
    if len(matched) <= TELEGRAM_MAX_INDIVIDUAL:
        for item in matched:
            if telegram_send(token, chat_id, format_match_message(*item)):
                sent += 1
            time.sleep(TELEGRAM_GAP)
    else:
        for text in _telegram_chunks(
                "<b>PS Plus 新增匹配（%d 条）</b>" % len(matched),
                [format_match_line(i + 1, *item) for i, item in enumerate(matched)]):
            if telegram_send(token, chat_id, text):
                sent += len(matched)
            time.sleep(TELEGRAM_GAP)

    if unmatched:
        for text in _telegram_chunks(
                "<b>PS Plus 新增记录未匹配（%d 条）</b>" % len(unmatched),
                ["%d. <code>%s</code> · %s · %s"
                 % (i + 1, _tg_escape(rid), _tg_escape(en) or "—", _tg_escape(cn) or "—")
                 for i, (rid, en, cn) in enumerate(unmatched)] +
                ["\n人工核对后写入 data/data.csv"]):
            telegram_send(token, chat_id, text)
            time.sleep(TELEGRAM_GAP)

    print("telegram: reported %d new match(es), %d unmatched" % (sent, len(unmatched)))
    return sent


def notify_new_matches(matched, unmatched):
    """Best-effort Telegram report: never lets a notice failure break the sync."""
    try:
        return _notify_new_matches(matched, unmatched)
    except Exception as e:
        sys.stderr.write("telegram notify skipped (%s)\n" % e)
        return 0


def load_previous():
    """Load the prior output keyed by Feishu record_id (cache for Steam lookups)."""
    try:
        with open(OUTPUT_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}
    return {g.get("recordId"): g for g in data.get("games", []) if g.get("recordId")}


def load_seed(path):
    """Load the proofread recordId -> steamAppId mapping (verbatim).

    Format: plain number = Steam app id; 's<number>' = sub id; 'b<number>' = bundle id;
    '' (empty) = human-confirmed no Steam match (never auto-searched). All non-numeric
    prefixes are preserved verbatim into psplus-games.json for the userscript to route.
    """
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


def _data_key(count, games):
    """Content fingerprint of the payload, timestamp excluded.

    Key order is irrelevant (``sort_keys``) so a formatting change alone is not
    mistaken for a data change, while any edit to a record -- including adding a
    field -- still shows up.
    """
    return json.dumps({"count": count, "games": games},
                      ensure_ascii=False, indent=2, sort_keys=True)


def write_output(games):
    """Write the payload, leaving the file untouched when only the date differs.

    ``updatedAt`` records the last time the *data* changed, not the last run: the
    userscript compares it to decide whether to refresh its local cache, so a
    daily touch would only produce empty commits (and pointless cache
    refreshes). When the new content would be identical the existing file is
    left alone, keeping ``git status`` clean for local runs too.

    Returns True when the file was written, False when it was already current.
    """
    out = {
        "updatedAt": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(games),
        "games": games,
    }
    key = _data_key(out["count"], games)

    previous = None
    try:
        with open(OUTPUT_PATH, encoding="utf-8") as fh:
            previous = json.load(fh)
    except (OSError, ValueError):
        previous = None
    if isinstance(previous, dict) and isinstance(previous.get("games"), list):
        if _data_key(len(previous["games"]), previous["games"]) == key:
            print("No data changes since %s; %s left untouched"
                  % (previous.get("updatedAt") or "?", os.path.basename(OUTPUT_PATH)))
            return False

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    return True


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
        help="Ignore cached appIds and re-resolve every record "
             "(ludoteca, then Steam search). Use once to clear stale/wrong "
             "matches after changing the resolution logic.")
    parser.add_argument(
        "--seed-csv", nargs="?", const=os.path.join(ROOT, "data", "data.csv"),
        default=None,
        help="Proofread mapping CSV (recordId,steamAppId). When a record_id is "
             "present here, its id is used verbatim and that record is never "
             "re-queried from Steam. Sub ids are written as 's<number>' (e.g. "
             "'s93325'); plain numbers are normal app ids. Only Feishu rows "
             "ABSENT from this file are resolved (ludoteca, then Steam search). "
             "Defaults to data/data.csv if it exists (so dropping a proofread "
             "data.csv into the repo just works).")
    parser.add_argument(
        "--no-ludoteca", action="store_true", default=False,
        help="Skip ludoteca.dev resolution and use the Steam name search only "
             "(also settable via env LUDOTECA_DISABLE=1).")
    parser.add_argument(
        "--no-ludoteca-api", action="store_true", default=False,
        help="Use only the ludoteca bulk table; do not call its REST API for "
             "games absent from it.")
    parser.add_argument(
        "--ludoteca-cache", default=None,
        help="Directory for the cached ludoteca bulk table "
             "(default <repo>/.cache/ludoteca, or env LUDOTECA_CACHE_DIR).")
    parser.add_argument(
        "--ludoteca-refresh", action="store_true", default=False,
        help="Force re-download of the ludoteca bulk equivalence table.")
    args = parser.parse_args()

    global STEAM_DELAY, STEAM_MAX_RETRIES
    if args.delay is not None:
        STEAM_DELAY = args.delay
    if args.retries is not None:
        STEAM_MAX_RETRIES = args.retries

    # Load the ludoteca bulk equivalence table once (primary, curated matcher).
    use_ludoteca = (not args.no_ludoteca and
                    os.environ.get("LUDOTECA_DISABLE", "").lower() not in ("1", "true", "yes"))
    ludoteca_idx = None
    ludoteca_limiter = None
    if use_ludoteca:
        if ludoteca is None:
            print("WARN: ludoteca module unavailable; falling back to Steam search")
        else:
            cache_dir = (args.ludoteca_cache or os.environ.get("LUDOTECA_CACHE_DIR")
                         or os.path.join(ROOT, ".cache", "ludoteca"))
            try:
                ludoteca_idx = ludoteca.load_index(cache_dir, refresh=args.ludoteca_refresh)
                ludoteca_limiter = ludoteca.RateLimiter()
                print("ludoteca table %s: %d rows (%d product codes, %d titles)"
                      % (ludoteca_idx.generado, ludoteca_idx.rows,
                         len(ludoteca_idx.by_code), len(ludoteca_idx.by_title)))
            except Exception as e:
                sys.stderr.write(
                    "WARN: ludoteca load failed (%s); falling back to Steam search\n" % e)
                ludoteca_idx = None

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
    ludoteca_hits = 0
    new_records = 0
    status_changes = 0
    unmatched_status = set()
    # New Feishu rows resolved in this run -> reported to Telegram so wrong
    # matches get spotted early. Items: (recordId, steam_id, en, cn, steam, src)
    new_matched = []
    new_unmatched = []

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

            # Precedence: proofread CSV > previous cache > ludoteca > Steam search.
            seed_val = seed_map.get(record_id)   # raw string ("s..."/"b...") or None
            steam_app_id = None
            if seed_val is not None:
                steam_app_id = seed_val if seed_val != "" else None
            else:
                if not args.force_steam:
                    steam_app_id = prev.get("steamAppId")
                if not steam_app_id:
                    resolved, source = resolve_unmatched(
                        ludoteca_idx, ludoteca_limiter, psstore, name_en, name_cn,
                        allow_api=not args.no_ludoteca_api)
                    steam_app_id = resolved
                    if resolved:
                        if source and source.startswith("ludoteca"):
                            ludoteca_hits += 1
                        else:
                            steam_calls += 1
                        # Report a brand-new row the moment it gets matched.
                        if not prev:
                            new_matched.append((
                                record_id, resolved, name_en, name_cn,
                                steam_app_info(resolved).get("name") or "", source))
                    elif not prev:
                        new_unmatched.append((record_id, name_en, name_cn))

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

    written = write_output(json_games)
    summary = ("ludoteca: %d, steam lookups: %d, new records: %d, status changes: %d"
               % (ludoteca_hits, steam_calls, new_records, status_changes))
    if written:
        print("Wrote %d games to %s (%s)" % (len(games), OUTPUT_PATH, summary))
    else:
        print("No change, %d games in %s already current (%s)"
              % (len(games), OUTPUT_PATH, summary))
    if unmatched_status:
        sample = ", ".join(sorted(unmatched_status)[:10])
        print("WARN: %d status value(s) could not be resolved to an option name: %s"
              % (len(unmatched_status), sample))

    # Best-effort: notifies only the rows that are new to Feishu *and* were
    # resolved by this run, so a steady-state sync stays silent.
    notify_new_matches(new_matched, new_unmatched)


if __name__ == "__main__":
    main()
