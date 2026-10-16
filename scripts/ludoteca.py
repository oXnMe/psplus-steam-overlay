#!/usr/bin/env python3
"""High-precision PS -> Steam app id resolution via ludoteca.dev.

ludoteca.dev (https://ludoteca.dev) publishes open, curated cross-store
equivalence data under ODbL 1.0. We use it as the *primary* source when
resolving a PlayStation game to a Steam app id: a confident, unique Steam id
wins outright and never touches Steam's fuzzy name search.

Data is used through two paths:

  * Bulk table  (preferred) ``/datos/ludoteca-equivalencias.csv.gz``
      - ~3.7 MB gzip, regenerated weekly, one row per game with a ``steam``
        column and a ``playstation`` column (``product:<code>`` / ``concept:<id>``).
      - Downloaded once and cached locally, keyed by its ``generado`` timestamp
        (from ``/datos/ludoteca-equivalencias.json``), so repeat runs reuse it.
      - No rate limit.

  * REST API     (fallback) ``/api/v1/buscar`` + ``/api/v1/id/<id>``
      - 60 requests/minute/IP. Used only for games *absent* from the weekly bulk
        table (e.g. brand-new releases), to keep the request budget tiny.

Attribution (required by ODbL): "Contiene información de ludoteca.dev
(https://ludoteca.dev), disponible bajo la Open Database License (ODbL) 1.0."
"""
import csv
import gzip
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

LUDOTECA_HOST = "https://ludoteca.dev"
DATOS_META_URL = LUDOTECA_HOST + "/datos/ludoteca-equivalencias.json"
DATOS_CSV_URL = LUDOTECA_HOST + "/datos/ludoteca-equivalencias.csv.gz"
API_BASE = LUDOTECA_HOST + "/api/v1"

API_PER_MINUTE = 60  # documented limit, per IP

# Windows CSV fields can exceed the default 128 KB field limit.
try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:  # pragma: no cover
    csv.field_size_limit(2 ** 31 - 1)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def norm(text):
    """Normalize a title for matching: lowercase, NFKC, drop ™/®, alnum+CJK only."""
    if not text:
        return ""
    s = str(text).lower()
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[^\w\u4e00-\u9fff]+", " ", s)   # \w keeps ascii letters/digits/_
    return re.sub(r"\s+", " ", s).strip()


def ps_product_code(url):
    """Extract the PS Store product code from a psstore URL, or ''."""
    m = re.search(r"/product/([^/?#]+)", url or "")
    return m.group(1) if m else ""


def product_sku(code):
    """``REGION-PPSAxxxxx_00-<SKU>`` -> ``<SKU>`` (stable across regions)."""
    parts = (code or "").split("-")
    return parts[-1] if len(parts) >= 3 else ""


def _split_multi(value):
    return [p.strip() for p in (value or "").split("|") if p.strip()]


def _http_get(url, timeout=60):
    headers = {
        "User-Agent": ("Mozilla/5.0 (compatible; PSPlusSync/1.0; "
                       "+https://github.com/oXnMe/psplus-steam-overlay)"),
        "Accept": "*/*",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


class RateLimiter:
    """Simple thread-safe 'at most N calls per 60s' limiter."""

    def __init__(self, per_minute=API_PER_MINUTE):
        self.interval = 60.0 / max(1, per_minute)
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self.interval:
                time.sleep(self.interval - delta)
            self._last = time.monotonic()


# --------------------------------------------------------------------------- #
# index
# --------------------------------------------------------------------------- #
class Index:
    """Equivalence indexes built from the ludoteca bulk table."""

    __slots__ = ("by_code", "by_sku", "by_title", "by_id", "by_slug",
                 "generado", "rows")

    def __init__(self):
        self.by_code = {}    # product code  -> set(steam)
        self.by_sku = {}     # SKU suffix    -> set(steam)
        self.by_title = {}   # norm(title)   -> set(steam)
        self.by_id = {}      # LDT-xxxx      -> steam
        self.by_slug = {}    # slug          -> steam
        self.generado = None
        self.rows = 0


def _add(mapping, key, steam):
    if not key:
        return
    bucket = mapping.get(key)
    if bucket is None:
        bucket = set()
        mapping[key] = bucket
    bucket.add(steam or "")


def build_index_from_csv(path):
    idx = Index()
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            steam = (row.get("steam") or "").strip()
            title = (row.get("titulo") or "").strip()
            lid = (row.get("ludoteca_id") or "").strip()
            slug = (row.get("slug") or "").strip()
            idx.rows += 1
            if title:
                _add(idx.by_title, norm(title), steam)
            if lid:
                idx.by_id[lid] = steam
            if slug:
                idx.by_slug[slug] = steam
            for part in _split_multi(row.get("playstation")):
                if part.startswith("product:"):
                    code = part[len("product:"):]
                    _add(idx.by_code, code, steam)
                    _add(idx.by_sku, product_sku(code), steam)
                # concept: ids are a separate namespace; not needed for our joins.
            # A single row can list several Steam ids too.
            for extra in _split_multi(steam):
                pass
    return idx


def load_index(cache_dir, timeout=120, refresh=False):
    """Return an Index, downloading the bulk CSV (and caching it) as needed.

    The cache file is keyed by the table's ``generado`` timestamp, so a same-week
    rerun never re-downloads. Raises on network failure (caller decides fallback).
    """
    meta = json.loads(_http_get(DATOS_META_URL, timeout=timeout).decode("utf-8"))
    generado = (meta.get("generado") or "").strip()
    safe = re.sub(r"[^0-9A-Za-z]+", "", generado) or "latest"
    os.makedirs(cache_dir, exist_ok=True)
    csv_path = os.path.join(cache_dir, "ludoteca-%s.csv" % safe)

    if refresh or not os.path.exists(csv_path):
        blob = _http_get(DATOS_CSV_URL, timeout=timeout)
        try:
            text = gzip.decompress(blob)
        except OSError:
            text = blob  # some proxies may hand us the raw CSV
        tmp = csv_path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(text)
        os.replace(tmp, csv_path)
        # Drop older caches for other generations.
        for name in os.listdir(cache_dir):
            if name.startswith("ludoteca-") and name != os.path.basename(csv_path):
                try:
                    os.remove(os.path.join(cache_dir, name))
                except OSError:
                    pass

    idx = build_index_from_csv(csv_path)
    idx.generado = generado
    return idx


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
def _pick(steam_set):
    """('hit', id) | ('no_steam', None) | ('ambiguous', None)."""
    s = {x for x in steam_set if x}
    if len(s) == 1:
        return "hit", next(iter(s))
    if not s:
        return "no_steam", None
    return "ambiguous", None


def resolve_local(index, psstore_url, name_en, name_cn=None):
    """Resolve with the bulk index. Returns (steam_id|None, source, status).

    Precedence: exact PS product code > English title > PS SKU suffix > Chinese
    title. Across duplicate-title records we aggregate their Steam ids and only
    accept a unique non-empty one, which rejects the PS-only twin of a game.
    """
    if index is None:
        return None, None, "absent"

    code = ps_product_code(psstore_url)
    best_status = "absent"

    def consider(source, steam_set):
        nonlocal best_status
        status, steam = _pick(steam_set)
        if status == "hit":
            return steam, source, "hit"
        # remember a weaker signal for reporting; keep looking.
        if status == "ambiguous":
            best_status = "ambiguous"
        elif status == "no_steam" and best_status == "absent":
            best_status = "no_steam"
        return None

    if code and code in index.by_code:
        r = consider("code", index.by_code[code])
        if r:
            return r

    if name_en:
        key = norm(name_en)
        if key in index.by_title:
            r = consider("title", index.by_title[key])
            if r:
                return r

    if code:
        sku = product_sku(code)
        if sku in index.by_sku:
            r = consider("sku", index.by_sku[sku])
            if r:
                return r

    if name_cn:
        key = norm(name_cn)
        if key in index.by_title:
            r = consider("title-cn", index.by_title[key])
            if r:
                return r

    return None, None, best_status


def buscar(index, query, limiter=None, limit=5):
    """Search ludoteca by text; returns a list of result dicts."""
    if not query:
        return []
    if limiter:
        limiter.wait()
    url = "%s/buscar?q=%s&limite=%d" % (API_BASE, urllib.parse.quote(query), limit)
    try:
        raw = _http_get(url, timeout=30)
        data = json.loads(raw.decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError, OSError) as e:
        sys.stderr.write("ludoteca buscar failed for %r: %s\n" % (query, e))
        return []
    return data.get("resultados") or []


def fetch_steam_by_id(index, lid, limiter=None):
    """Steam id for a ludoteca id: bulk index first, API fallback."""
    if lid in index.by_id:
        return index.by_id[lid] or None
    if limiter:
        limiter.wait()
    try:
        raw = _http_get("%s/id/%s" % (API_BASE, urllib.parse.quote(lid)), timeout=30)
        data = json.loads(raw.decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError, OSError) as e:
        sys.stderr.write("ludoteca id lookup failed for %r: %s\n" % (lid, e))
        return None
    for store in data.get("tiendas", []):
        if store.get("clave") == "steam":
            return str(store.get("id") or "") or None
    return None


def resolve_api(index, name_en, name_cn=None, limiter=None):
    """Fallback for games absent from the bulk table: search then read steam id.

    Only accepts a result whose normalized title matches our query, so we never
    guess. Returns a steam id string or None.
    """
    if index is None:
        return None
    for query in (name_en, name_cn):
        if not query:
            continue
        qn = norm(query)
        results = buscar(index, query, limiter=limiter)
        # exact normalized-title matches first
        for r in sorted(results, key=lambda r: norm(r.get("titulo", "")) != qn):
            if norm(r.get("titulo", "")) != qn:
                continue
            steam = fetch_steam_by_id(index, r.get("id", ""), limiter=limiter)
            if steam:
                return steam
    return None


# --------------------------------------------------------------------------- #
# CLI (handy for manual inspection / testing)
# --------------------------------------------------------------------------- #
def _default_cache_dir():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, ".cache", "ludoteca")


def _main(argv):
    import argparse
    p = argparse.ArgumentParser(description="ludoteca.dev helper")
    p.add_argument("cmd", choices=["build", "resolve", "search", "info"])
    p.add_argument("--name", default="")
    p.add_argument("--psstore", default="")
    p.add_argument("--cache", default=_default_cache_dir())
    p.add_argument("--refresh", action="store_true")
    args = p.parse_args(argv)

    if args.cmd == "build":
        idx = load_index(args.cache, refresh=args.refresh)
        print("index built: generado=%s rows=%d codes=%d skus=%d titles=%d"
              % (idx.generado, idx.rows, len(idx.by_code), len(idx.by_sku),
                 len(idx.by_title)))
        return 0

    idx = load_index(args.cache, refresh=args.refresh)
    if args.cmd == "info":
        print("generado=%s rows=%d codes=%d skus=%d titles=%d"
              % (idx.generado, idx.rows, len(idx.by_code), len(idx.by_sku),
                 len(idx.by_title)))
        return 0
    if args.cmd == "search":
        for r in buscar(idx, args.name, RateLimiter()):
            print(r)
        return 0
    # resolve
    steam, source, status = resolve_local(idx, args.psstore, args.name)
    print("local:", steam, source, status)
    if steam is None and status == "absent":
        print("api:", resolve_api(idx, args.name, limiter=RateLimiter()))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
