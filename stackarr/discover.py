"""Discover: deterministic genre-trending picks and a cold-start popular set.
No personalisation, no AI — recent, well-rated catalog entries in given
genres. Used for the Discover tab and as the new-user fallback."""
import logging
import time

from . import audible, config

log = logging.getLogger("stackarr.discover")

DEFAULT_GENRES = ["Science Fiction & Fantasy", "Mystery, Thriller & Suspense",
                  "Literature & Fiction", "Biographies & Memoirs", "History"]

# Discover is remote-catalogue data and does not need to be refetched on every
# page visit. Cached base records are copied before returning because routes.py
# adds the current request/library state to them.
_EBOOK_PAGE_CACHE = {}
_EBOOK_CACHE_TTL = 3600

_LANGUAGE_CODES = {
    "english": "en", "german": "de", "spanish": "es", "french": "fr",
    "italian": "it", "dutch": "nl", "portuguese": "pt", "japanese": "ja",
    "chinese": "zh", "russian": "ru", "arabic": "ar", "korean": "ko",
}


def _language_ok(language: str) -> bool:
    """Accept either language names ('english') or ISO codes ('en')."""
    lang = (language or "").strip().lower()
    target = (config.TARGET_LANGUAGE or "").strip().lower()
    if not lang or not target:
        return True
    if lang == target:
        return True
    return (_LANGUAGE_CODES.get(target) == lang
            or _LANGUAGE_CODES.get(lang) == target)


def genre_new(genres: list[str], num_per: int = 6) -> list[dict]:
    """Genre browse / cold-start catalogue.

    Ebook-only installs use Hardcover's popular catalogue with the same
    confidence filtering as endless Discover. Google/Open Library remain
    the fallback. Audiobook installs keep the existing Audible behaviour.
    """
    from . import formats, ebookmeta

    out, seen = [], set()

    if formats.active() == ["ebook"]:
        for g in genres or DEFAULT_GENRES:
            source = ebookmeta.hardcover_discover(g, num=num_per, offset=0)
            if source is None:
                source = ebookmeta.search_paged(g, num=num_per, offset=0)

            for item in source:
                b = dict(item)
                bid = b.get("id") or b.get("asin") or ""
                if not bid or bid in seen:
                    continue
                if not _language_ok(b.get("language", "")):
                    continue

                seen.add(bid)
                b["asin"] = bid
                b["format"] = "ebook"
                b["genre"] = g
                out.append(b)

        # Across multiple lanes (popular/cold start), favour books with the
        # strongest real Hardcover readership. Single-genre browse retains
        # effectively the same popularity ordering.
        out.sort(
            key=lambda b: (
                b.get("users_read_count") or 0,
                b.get("rating") or 0,
                b.get("release_date") or "",
            ),
            reverse=True,
        )
        return out

    for g in genres or DEFAULT_GENRES:
        for b in audible.search(g, num=num_per * 3):
            if not b.get("asin") or b["asin"] in seen:
                continue
            if (b.get("rating") or 0) < config.SUGGEST_RATING_FLOOR:
                continue
            if (b.get("language") or "english").lower() != config.TARGET_LANGUAGE:
                continue
            seen.add(b["asin"])
            out.append(b)

    out.sort(
        key=lambda b: (
            b.get("rating") or 0,
            b.get("release_date") or "",
        ),
        reverse=True,
    )
    return out

def popular(num: int = 12) -> list[dict]:
    return genre_new(DEFAULT_GENRES, num_per=4)[:num]


def page(n: int, genres: list[str] | None = None) -> list[dict]:
    """One page of endless-scroll discovery.

    Ebook-only installs use the ebook catalogue. Audiobook/both installs keep
    the existing Audible-led discovery behaviour.
    """
    g = genres or DEFAULT_GENRES
    genre = g[n % len(g)]

    from . import formats

    if formats.active() == ["ebook"]:
        from . import ebookmeta

        cache_key = ("hardcover-v1", tuple(g), int(n), (config.TARGET_LANGUAGE or "").lower())
        now = time.monotonic()
        cached = _EBOOK_PAGE_CACHE.get(cache_key)
        if cached and now - cached[0] < _EBOOK_CACHE_TTL:
            return [dict(b) for b in cached[1]]

        # n rotates genres; every complete genre rotation advances the remote
        # catalogue offset instead of repeating page zero forever.
        batch = 24
        cycle = n // len(g)
        offset = cycle * batch

        # Hardcover is the primary visual discovery catalogue: it gives us
        # popularity + community genre confidence. Google Books/Open Library
        # remain the fallback and continue to power explicit search.
        source = ebookmeta.hardcover_discover(genre, num=batch, offset=offset)
        if source is None:
            source = ebookmeta.search_paged(genre, num=batch, offset=offset)

        out = []
        for item in source:
            b = dict(item)
            b["asin"] = b.get("id", "")
            if not b["asin"]:
                continue
            if not _language_ok(b.get("language", "")):
                continue
            b["format"] = "ebook"
            b["genre"] = genre
            out.append(b)

        # routes.py mutates returned dictionaries with live ownership/request
        # state, so keep pristine copies in the catalogue cache.
        _EBOOK_PAGE_CACHE[cache_key] = (now, [dict(b) for b in out])
        return out

    audpage = n // len(g)
    out = []
    for b in audible.search(genre, num=18, page=audpage):
        if not b.get("asin") or (b.get("rating") or 0) < config.SUGGEST_RATING_FLOOR:
            continue
        if (b.get("language") or "english").lower() != config.TARGET_LANGUAGE:
            continue
        b["genre"] = genre
        out.append(b)
    return out
