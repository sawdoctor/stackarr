"""Ebook catalogue metadata — the ebook analogue of audible.py.

Deterministic, no AI, no API key required:
  * **Google Books**  — primary search / detail / by-author / related (categories)
  * **Open Library**  — fallback search + subject-based "related" + cover art
  * **Hardcover**     — the user's *read* shelf, a cross-device "I finished this"
                        signal (e.g. read on a Kindle Stackarr never sees)

Every function returns the same normalised dict the recommender/UI use for
audiobooks, with two differences: ebooks carry an `id` ("gb:…" or "ol:…")
instead of an `asin` (left ""), and `narrator`/`runtime_hours` are empty
(an ebook has `pages` instead). Mirroring the audiobook shape keeps the
format-aware recommender and templates from special-casing everything.
"""
from __future__ import annotations

import logging
import re
import time

import requests

from . import config, db

log = logging.getLogger("stackarr.ebookmeta")


def _get(url, params=None, tries=2):
    """GET with a single retry — keyless Google Books / Open Library are flaky
    (rate-limit, cold cache), and a second attempt usually succeeds. Returns the
    parsed JSON or None."""
    for i in range(tries):
        try:
            r = requests.get(url, params=params or {}, headers=UA, timeout=20)
            if r.status_code == 429 and i + 1 < tries:
                try:                                  # honour Retry-After (capped) — a fixed 0.6s is usually too short
                    wait = min(int(r.headers.get("Retry-After", "2")), 10)
                except (ValueError, TypeError):
                    wait = 2
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if i + 1 >= tries:
                log.debug("ebook fetch failed (%s): %s", url, e)
                return None
            time.sleep(0.6)
    return None

GB_API = "https://www.googleapis.com/books/v1/volumes"
OL_SEARCH = "https://openlibrary.org/search.json"
OL_COVER = "https://covers.openlibrary.org/b/id/{}-L.jpg"
UA = {"User-Agent": "Stackarr/1.x (+https://github.com/katalyst88/stackarr)"}


def _clean_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def _series_from_title(title: str) -> tuple[str, float | None]:
    """Best-effort series pull from a title like 'Mistborn (The Cosmere #2)'.
    Ebook catalogues rarely give structured series data, so this is heuristic
    and deliberately conservative — blank rather than wrong."""
    m = re.search(r"[\(\[]([^()\[\]]+?)\s*#\s*([\d.]+)[\)\]]", title or "")
    if not m:
        return "", None
    try:
        return m.group(1).strip(), float(m.group(2))
    except ValueError:
        return m.group(1).strip(), None


# ---------------------------------------------------------------- Google Books
def _gb_get(params: dict) -> list[dict]:
    params.setdefault("maxResults", 20)
    params.setdefault("printType", "books")
    params.setdefault("country", "US")          # Google requires a country hint
    key = config.GOOGLE_BOOKS_KEY
    if key:
        params["key"] = key
    data = _get(GB_API, params)
    return (data or {}).get("items", []) or []


def _gb_normalize(item: dict) -> dict:
    vi = item.get("volumeInfo") or {}
    title = vi.get("title") or ""
    series, seq = _series_from_title(title)
    isbn = ""
    for ident in vi.get("industryIdentifiers") or []:
        if ident.get("type") == "ISBN_13":
            isbn = ident.get("identifier", "")
            break
        if ident.get("type") == "ISBN_10" and not isbn:
            isbn = ident.get("identifier", "")
    img = ((vi.get("imageLinks") or {}).get("thumbnail")
           or (vi.get("imageLinks") or {}).get("smallThumbnail") or "")
    img = img.replace("http://", "https://").replace("&edge=curl", "")
    try:
        rating = round(float(vi.get("averageRating")), 2) if vi.get("averageRating") else None
    except (TypeError, ValueError):
        rating = None
    return {
        "id": "gb:" + item.get("id", ""), "asin": "", "isbn": isbn,
        "title": title, "subtitle": vi.get("subtitle") or "",
        "author": ", ".join(vi.get("authors") or []), "narrator": "",
        "cover": img, "series": series, "sequence": seq,
        "release_date": (vi.get("publishedDate") or "")[:10],
        "runtime_hours": None, "pages": vi.get("pageCount") or None,
        "rating": rating, "num_ratings": int(vi.get("ratingsCount") or 0),
        "summary": _clean_html(vi.get("description"))[:2500],
        "publisher": vi.get("publisher") or "",
        "format": "ebook",
        "categories": vi.get("categories") or [],
        "language": (vi.get("language") or "").lower(),    # GB uses ISO codes (en)
    }


def gb_search(query: str, num: int = 12, offset: int = 0) -> list[dict]:
    params = {"q": query, "maxResults": min(num, 40)}
    if offset:
        params["startIndex"] = max(0, int(offset))
    return [_gb_normalize(i) for i in _gb_get(params)
            if (i.get("volumeInfo") or {}).get("title")]


# ---------------------------------------------------------------- Open Library
OL_FIELDS = "key,title,subtitle,author_name,first_publish_year,isbn,cover_i,subject,language,ratings_average,ratings_count,number_of_pages_median"


def _ol_normalize(doc: dict) -> dict:
    title = doc.get("title") or ""
    series, seq = _series_from_title(title)
    cover = OL_COVER.format(doc["cover_i"]) if doc.get("cover_i") else ""
    langs = doc.get("language") or []
    lang = ""
    if langs:
        code = (langs[0] or "").lower()
        # ISO-639-2/B → 639-1; blind [:2] truncation produced wrong tags ('chi'→'ch'
        # not 'zh'), so map known codes and leave unmapped 3-letter codes blank
        # rather than emit an invalid tag that breaks cross-source language matching.
        m639 = {"eng": "en", "ger": "de", "spa": "es", "fre": "fr", "ita": "it",
                "dut": "nl", "por": "pt", "jpn": "ja", "chi": "zh", "rus": "ru",
                "ara": "ar", "kor": "ko", "gre": "el", "cze": "cs", "pol": "pl",
                "swe": "sv", "dan": "da", "nor": "no", "fin": "fi", "tur": "tr", "hin": "hi"}
        # map known 639-2/B codes; for unmapped 3-letter codes keep the [:2]
        # passthrough (NOT "") so a foreign edition still carries a non-blank lang
        # and the language filter can exclude it.
        lang = code if len(code) == 2 else m639.get(code, code[:2])
    try:
        rating = round(float(doc.get("ratings_average")), 2) if doc.get("ratings_average") else None
    except (TypeError, ValueError):
        rating = None
    return {
        "id": "ol:" + (doc.get("key") or ""), "asin": "",
        "isbn": (doc.get("isbn") or [""])[0],
        "title": title, "subtitle": doc.get("subtitle") or "",
        "author": ", ".join(doc.get("author_name") or []), "narrator": "",
        "cover": cover, "series": series, "sequence": seq,
        "release_date": str(doc.get("first_publish_year") or "")[:10],
        "runtime_hours": None, "pages": doc.get("number_of_pages_median") or None,
        "rating": rating, "num_ratings": int(doc.get("ratings_count") or 0),
        "summary": "", "publisher": "",
        "format": "ebook",
        "categories": (doc.get("subject") or [])[:8],
        "language": lang,
    }


def _ol_get(params: dict) -> list[dict]:
    params.setdefault("fields", OL_FIELDS)
    params.setdefault("limit", 20)
    data = _get(OL_SEARCH, params)
    return (data or {}).get("docs", []) or []


def ol_search(query: str, num: int = 12, offset: int = 0) -> list[dict]:
    params = {"q": query, "limit": min(num, 40)}
    if offset:
        params["offset"] = max(0, int(offset))
    return [_ol_normalize(d) for d in _ol_get(params)
            if d.get("title")]


def ol_subject(subject: str, num: int = 12) -> list[dict]:
    """Books in an Open Library subject — the reliable, keyless 'related' source
    (Google Books subject search is rate-limited without a key). Normalises the
    /subjects works[] shape into the standard search-doc fields."""
    slug = re.sub(r"[^a-z0-9]+", "_", (subject or "").lower()).strip("_")
    if not slug:
        return []
    data = _get(f"https://openlibrary.org/subjects/{slug}.json", {"limit": min(num, 40)})
    if data is None:
        return []
    try:
        out = []
        for w in data.get("works", []) or []:
            out.append(_ol_normalize({
                "key": w.get("key", ""), "title": w.get("title", ""),
                "author_name": [a.get("name", "") for a in w.get("authors") or []],
                "first_publish_year": w.get("first_publish_year"),
                "cover_i": w.get("cover_id"), "subject": [subject]}))
        return [b for b in out if b["title"]]
    except Exception as e:
        log.debug("open library subject %s failed: %s", slug, e)
        return []


# ---------------------------------------------------------------- unified API
def _is_modern(b: dict, after: int = 1975) -> bool:
    """Heuristic to drop public-domain classics from subject fallbacks: keep a
    book only if it was first published after `after` (or has no year at all,
    which modern self-/indie titles often do)."""
    yr = (b.get("release_date") or "")[:4]
    if not yr.isdigit():
        return True
    return int(yr) >= after


def _dedup(books: list[dict]) -> list[dict]:
    seen, out = set(), []
    for b in books:
        k = (re.sub(r"[^a-z0-9]+", "", (b.get("title") or "").lower())[:40],
             (b.get("author") or "").split(",")[0].strip().lower())
        if k in seen or not k[0]:
            continue
        seen.add(k)
        out.append(b)
    return out


def search(query: str, num: int = 12) -> list[dict]:
    """Search ebooks. Google Books first (richer metadata + summaries), topped
    up from Open Library so thin GB results still fill a row."""
    books = gb_search(query, num)
    if len(books) < num:
        books += ol_search(query, num - len(books))
    return _dedup(books)[:num]


def search_paged(query: str, num: int = 24, offset: int = 0) -> list[dict]:
    """Paged catalogue search for endless-scroll discovery.

    Fetch the same offset from both catalogues, then deduplicate.  This is kept
    separate from search() so normal title search behaviour does not change.
    """
    books = gb_search(query, num, offset=offset)
    books += ol_search(query, num, offset=offset)
    return _dedup(books)[:num]


def by_author(author: str, num: int = 25) -> list[dict]:
    author = (author or "").replace('"', '')          # a stray quote closes the inauthor:"..." phrase
    books = gb_search(f'inauthor:"{author}"', num)
    if len(books) < num:
        books += [_ol_normalize(d) for d in
                  _ol_get({"author": author, "limit": num}) if d.get("title")]
    return _dedup(books)[:num]


def by_id(book_id: str) -> dict | None:
    """Resolve a single ebook by its 'gb:'/'ol:' id."""
    if not book_id:
        return None
    if book_id.startswith("gb:"):
        data = _get(f"{GB_API}/{book_id[3:]}", {"country": "US"})
        return _gb_normalize(data) if data else None
    if book_id.startswith("ol:"):
        docs = _ol_get({"q": "key:" + book_id[3:], "limit": 1})
        return _ol_normalize(docs[0]) if docs else None
    return None


def similar(book: dict, num: int = 8) -> list[dict]:
    """Ebooks have no 'listeners also enjoyed' endpoint, so approximate it
    deterministically: other well-known books in the same primary subject /
    category, minus the seed and its own author dominating."""
    cats = book.get("categories") or []
    out = []
    for cat in cats[:2]:
        out += gb_search(f'subject:"{cat}"', num)
        if len(out) < num:                       # GB throttled / sparse -> Open Library subjects
            # OL subject feeds skew heavily to pre-1970 public-domain classics
            # (Alice, Dickens…), which are rarely the next read for someone in
            # contemporary genre fiction — keep only modern titles in the fallback.
            out += [b for b in ol_subject(cat, num * 2) if _is_modern(b)]
    out = [b for b in _dedup(out)
           if b.get("id") != book.get("id")
           and (b.get("title") or "").lower() != (book.get("title") or "").lower()]
    return out[:num]


def find_id(title: str, author: str) -> str:
    hits = search(f"{title} {author}".strip(), num=1)
    return hits[0].get("id", "") if hits else ""


# ---------------------------------------------------------------- Hardcover read shelf
def hardcover_reading(token: str) -> list[dict]:
    """The user's Hardcover *currently reading* shelf (status_id 2) — a strong
    'what I'm into right now' seed."""
    return _hardcover_shelf(token, 2)


def hardcover_read(token: str) -> list[dict]:
    """The user's Hardcover *read* shelf (status_id 3) — a cross-device read
    signal for ebooks. Returns [{title, author, isbn}]."""
    return _hardcover_shelf(token, 3)


def _hardcover_shelf(token: str, status_id: int) -> list[dict]:
    if not token:
        return []
    q = {"query": "{ me { user_books(where: {status_id: {_eq: %d}}) "
                  "{ book { title isbns contributions { author { name } } } } } }" % status_id}
    try:
        r = requests.post("https://api.hardcover.app/v1/graphql",
                          headers={"Authorization": token if token.lower().startswith("bearer ") else f"Bearer {token}",
                                   "Content-Type": "application/json"},
                          json=q, timeout=20)
        r.raise_for_status()
        j = r.json()
        if j.get("errors"):              # 200 + data:null errors shouldn't look like an empty shelf
            log.warning("hardcover shelf GraphQL error: %s", j["errors"])
            return []
        out = []
        me = ((j.get("data") or {}).get("me") or [{}])
        for ub in (me[0] or {}).get("user_books", []):
            bk = ub.get("book") or {}
            authors = [c.get("author", {}).get("name", "") for c in bk.get("contributions") or []]
            if bk.get("title"):
                out.append({"title": bk["title"],
                            "author": ", ".join(filter(None, authors)),
                            "isbn": (bk.get("isbns") or [""])[0] if bk.get("isbns") else ""})
        return out
    except Exception as e:
        log.warning("hardcover read-shelf failed: %s", e)
        return []


def hardcover_token() -> str:
    return db.setting("hardcover_token", config.HARDCOVER_TOKEN)
