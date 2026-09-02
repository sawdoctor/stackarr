"""Shelfmark handoff. When a suggestion is approved, Stackarr asks Shelfmark
(the *arr book backend) to add the author/book and search for it. Stackarr
never touches a download client directly — Shelfmark owns grab/import."""
import logging
import re
import time

import requests

from . import config, db

log = logging.getLogger("stackarr.chaptarr")


def url() -> str:
    return db.setting("chaptarr_url", config.CHAPTARR_URL).rstrip("/")


def api_key() -> str:
    return db.setting("chaptarr_api_key", config.CHAPTARR_API_KEY)


def root_folder() -> str:
    rf = db.setting("chaptarr_root_folder", config.CHAPTARR_ROOT_FOLDER)
    # Guard against Git-Bash path mangling (a unix /path passed through Git Bash
    # at container-create time becomes "C:/Program Files/Git/path"). Recover the
    # real container path so Shelfmark doesn't reject it as an invalid path.
    m = re.search(r"/Git(/.*)$", rf)
    if rf.startswith("C:") and "Program Files/Git" in rf and m:
        rf = m.group(1)
    return rf


def _profile(key: str, fallback: int) -> int:
    try:
        return int(db.setting(key, str(fallback)))
    except ValueError:
        return fallback


def _h():
    return {"X-Api-Key": api_key(), "Content-Type": "application/json"}


def configured() -> bool:
    return bool(url() and api_key())


def monitored_keys() -> set[str]:
    """author names Shelfmark already manages, lowercased, for dedupe."""
    keys = set()
    try:
        for a in requests.get(f"{url()}/api/v1/author", headers=_h(), timeout=20).json():
            keys.add((a.get("authorName") or "").lower())
    except Exception as e:
        log.debug("chaptarr monitored_keys failed: %s", e)
    return keys


def health() -> list[dict]:
    """Shelfmark's own health warnings (e.g. the api.chaptarr.com metadata
    outage) — surfaced in Stackarr's connection test so degraded metadata is
    visible. Each: {type, message}."""
    try:
        r = requests.get(f"{url()}/api/v1/health", headers=_h(), timeout=15)
        return [{"type": h.get("type", ""), "message": h.get("message", "")} for h in r.json()] if r.ok else []
    except Exception:
        return []


def can_grab(title: str, author: str) -> bool:
    """Cheap pre-check: does Shelfmark's catalogue know this author/title? Lets
    the UI flag 'no releases yet' before queueing a dead-end."""
    if not configured():
        return False
    try:
        r = requests.get(f"{url()}/api/v1/author/lookup", headers=_h(),
                         params={"term": author or title}, timeout=30)
        return bool(r.ok and r.json())
    except Exception:
        return False


def queue_status() -> dict:
    """Live download state keyed by lowercased title — {title: status} where
    status is downloading | importing | queued. Drives real request progress."""
    out = {}
    try:
        page = 1
        while page <= 50:                                # safety cap
            r = requests.get(f"{url()}/api/v1/queue", headers=_h(),
                             params={"page": page, "pageSize": 200,
                                     "includeAuthor": "true", "includeBook": "true"}, timeout=20)
            if not r.ok:
                break
            data = r.json() or {}
            records = data.get("records", [])
            for rec in records:
                title = ((rec.get("book") or {}).get("title")
                         or (rec.get("title") or "")).lower().strip()
                if not title:
                    continue
                st = (rec.get("status") or "").lower()
                tdl = (rec.get("trackedDownloadState") or "").lower()
                if "import" in tdl or st == "completed":
                    out[title] = "importing"
                elif st in ("downloading", "queued", "paused", "delay"):
                    out[title] = "downloading"
                else:
                    out[title] = st or "downloading"
            # stop once we've seen everything (or the page came back short)
            if len(records) < 200 or len(out) >= (data.get("totalRecords") or 0):
                break
            page += 1
    except Exception as e:
        log.debug("chaptarr queue_status failed: %s", e)
    return out


def _ensure_tag(label: str = "stackarr") -> int | None:
    """Ensure a Shelfmark tag exists; return its id (so Stackarr-added books are
    tagged for easy filtering in Shelfmark). Best-effort."""
    try:
        for t in requests.get(f"{url()}/api/v1/tag", headers=_h(), timeout=15).json():
            if (t.get("label") or "").lower() == label:
                return t.get("id")
        r = requests.post(f"{url()}/api/v1/tag", headers=_h(), json={"label": label}, timeout=15)
        return r.json().get("id") if r.ok else None
    except Exception:
        return None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _find_author(foreign_id: str, name: str) -> dict | None:
    """Locate an author already in Shelfmark by foreign id (preferred) or name.
    Raises on a transport/HTTP error instead of swallowing it — otherwise a
    transient failure looks like 'author absent' and add_and_search POSTs a
    DUPLICATE author. The caller's outer try turns a raise into a clean retry."""
    r = requests.get(f"{url()}/api/v1/author", headers=_h(), timeout=20)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        return None
    for a in data:
        if foreign_id and a.get("foreignAuthorId") == foreign_id:
            return a
        if _norm(a.get("authorName")) == _norm(name):
            return a
    return None


def _author_books(author_id: int) -> list[dict]:
    try:
        return requests.get(f"{url()}/api/v1/book", headers=_h(),
                            params={"authorId": author_id}, timeout=40).json() or []
    except Exception:
        return []


def _match_book(books: list[dict], title: str, asin: str) -> dict | None:
    """Pick the specific book a recommendation refers to: ASIN first (exact),
    then exact normalized title, then a title prefix match."""
    asin = (asin or "").strip().upper()
    if asin:
        for b in books:
            if (b.get("asin") or "").upper() == asin or (b.get("audibleASIN") or "").upper() == asin:
                return b
    nt = _norm(title)
    if not nt:
        return None
    for b in books:
        if _norm(b.get("title")) == nt:
            return b
    # Prefix fallback only for a specific-enough title AND only when unambiguous —
    # a short/common title ("it", "dune") prefix-matches the wrong book otherwise,
    # and we'd confidently search+grab the wrong title.
    if len(nt) >= 8:
        cands = [b for b in books if _norm(b.get("title")).startswith(nt)]
        if len(cands) == 1:
            return cands[0]
    return None


def _ensure_media_monitored(author_obj: dict, media: str, rf: str,
                            qp: int | None = None, mp: int | None = None) -> None:
    """Shelfmark's current schema gates book monitoring behind per-media author
    monitoring (audiobook/ebookMonitorExisting + matching root folder + profiles).
    Make sure the active media type is fully set up — crucially when REUSING an
    author first added for the OTHER format, whose {media}RootFolderPath/profiles
    are unset, so a cross-format request isn't 400'd. Idempotent; best-effort."""
    changed = False
    if not author_obj.get("monitored"):
        author_obj["monitored"] = True; changed = True
    pfx = "audiobook" if media == "audiobook" else "ebook"
    if not author_obj.get(f"{pfx}MonitorExisting"):
        author_obj[f"{pfx}MonitorExisting"] = 1; changed = True
    if qp and not author_obj.get(f"{pfx}QualityProfileId"):
        author_obj[f"{pfx}QualityProfileId"] = qp; changed = True
    if mp and not author_obj.get(f"{pfx}MetadataProfileId"):
        author_obj[f"{pfx}MetadataProfileId"] = mp; changed = True
    if rf and not author_obj.get(f"{pfx}RootFolderPath"):
        author_obj[f"{pfx}RootFolderPath"] = rf; changed = True
    if changed and author_obj.get("id"):
        try:
            requests.put(f"{url()}/api/v1/author/{author_obj['id']}", headers=_h(),
                         json=author_obj, timeout=30)
        except Exception as e:
            log.debug("chaptarr _ensure_media_monitored PUT failed: %s", e)


def mark_read(title: str, author: str) -> bool:
    """A book you've already read shouldn't be grabbed. If Shelfmark is managing
    it and the book is monitored, unmonitor it so it won't be searched/downloaded.
    Best-effort; returns True only if something was actually unmonitored. Shelfmark
    has no 'read' state, so unmonitoring is the meaningful equivalent."""
    if not configured():
        return False
    try:
        look = requests.get(f"{url()}/api/v1/author/lookup", headers=_h(),
                            params={"term": author or title}, timeout=30)
        results = look.json() if look.ok else []
        if not results:
            return False
        a = results[0]
        existing = _find_author(a.get("foreignAuthorId", ""), a.get("authorName") or author)
        if not existing:
            return False
        target = _match_book(_author_books(existing["id"]), title, "")
        if not target or not (target.get("monitored") or target.get("audiobookMonitored")
                              or target.get("ebookMonitored")):
            return False
        r = requests.put(f"{url()}/api/v1/book/monitor", headers=_h(),
                         json={"bookIds": [target["id"]], "monitored": False}, timeout=20)
        return bool(r.ok)
    except Exception as e:
        log.debug("chaptarr mark_read failed: %s", e)
        return False


def add_and_search(title: str, author: str, asin: str = "", fmt: str = "audiobook",
                   root_folder_override: str = "") -> dict:
    """Ensure the author exists in Shelfmark (tagged 'stackarr'), then monitor +
    search. `fmt` (audiobook | ebook) decides the media type + profiles.

    Shelfmark uses a media-split schema: monitoring lives in
    audiobook/ebookMonitorExisting + audiobook/ebookRootFolderPath, NOT the
    generic monitored/rootFolderPath, and book monitoring is rejected unless the
    author is monitored for that media type. So we (1) add/find the author with
    the media-split fields set, (2) when the request names a specific title, pin
    that book and monitor+search just it (avoids grabbing the whole backlist),
    else fall back to monitoring all the author's books + an author search.
    Returns {ok, ref, detail}. Fails gracefully if Shelfmark is unavailable."""
    if not configured():
        return {"ok": False, "detail": "Stackarr isn't connected to Shelfmark yet — add it in Settings → Connections."}
    # Audiobook + ebook each have their own quality/metadata profile pair in
    # Shelfmark; the active media type's pair becomes the author's primary.
    ab_qp = _profile("chaptarr_quality_profile_id", config.CHAPTARR_QUALITY_PROFILE_ID)
    ab_mp = _profile("chaptarr_metadata_profile_id", config.CHAPTARR_METADATA_PROFILE_ID)
    eb_qp = _profile("chaptarr_ebook_quality_profile_id", config.CHAPTARR_EBOOK_QUALITY_PROFILE_ID)
    eb_mp = _profile("chaptarr_ebook_metadata_profile_id", config.CHAPTARR_EBOOK_METADATA_PROFILE_ID)
    media = "ebook" if fmt == "ebook" else "audiobook"
    qp, mp = (eb_qp, eb_mp) if media == "ebook" else (ab_qp, ab_mp)
    rf = (root_folder_override or root_folder()).rstrip("/") or root_folder()
    tag_id = _ensure_tag("stackarr")
    try:
        look = requests.get(f"{url()}/api/v1/author/lookup",
                            headers=_h(), params={"term": author or title}, timeout=60)
        # Distinguish a real outage / auth / rate-limit from a genuine catalogue
        # miss, so a config error isn't reported to the user as "not found" and
        # auto_approve can tell "retry later" (stop) from "skip this one" (continue).
        if look.status_code >= 500:
            return {"ok": False, "retry": True, "detail": "Shelfmark's book database is offline right now — we've kept this; try again shortly."}
        if look.status_code in (401, 403):
            return {"ok": False, "detail": "Shelfmark rejected the API key — check Settings → Connections."}
        if look.status_code == 429:
            return {"ok": False, "retry": True, "detail": "Shelfmark is rate-limited right now — try again shortly."}
        results = look.json() if look.ok else []
        if not results:
            return {"ok": False, "detail": f"Shelfmark couldn't find “{author or title}” in its catalogue."}
        a = results[0]
        foreign_id = a.get("foreignAuthorId", "")
        author_obj = _find_author(foreign_id, a.get("authorName") or author)
        if author_obj is None:
            folder = a.get("folder") or a.get("authorName") or author
            # Set BOTH the media-split fields (current Shelfmark) and the legacy
            # fields (older builds) so the handoff works across schema versions.
            a.update(
                mediaType=media, selectedMediaType=media, lastSelectedMediaType=media,
                qualityProfileId=qp, metadataProfileId=mp,
                audiobookQualityProfileId=ab_qp, audiobookMetadataProfileId=ab_mp,
                ebookQualityProfileId=eb_qp, ebookMetadataProfileId=eb_mp,
                rootFolderPath=rf, path=f"{rf.rstrip('/')}/{folder}",
                audiobookMonitorExisting=(1 if media == "audiobook" else 0),
                ebookMonitorExisting=(1 if media == "ebook" else 0),
                audiobookMonitorFuture=False, ebookMonitorFuture=False,
                tags=([tag_id] if tag_id else []),
                monitored=True, monitorNewItems="none",
                # Don't auto-grab on add; we monitor + search precisely below.
                addOptions={"monitor": "none", "searchForMissingBooks": False})
            # Pin the per-media root so the pick lands in the configured folder,
            # but Shelfmark 400s ("root folder does not have <media> defaults
            # configured") if that folder isn't set up for this media type. So
            # retry once without it, letting Shelfmark fall back to its default root.
            media_root_key = f"{media}RootFolderPath"
            a[media_root_key] = rf
            r = requests.post(f"{url()}/api/v1/author", headers=_h(), json=a, timeout=90)
            if not r.ok and "defaults configured" in (r.text or ""):
                a.pop(media_root_key, None)
                r = requests.post(f"{url()}/api/v1/author", headers=_h(), json=a, timeout=90)
            if not r.ok:
                body = r.text or ""
                if r.status_code in (502, 503) or "V5 API" in body or "author info" in body:
                    return {"ok": False, "retry": True, "detail": "Shelfmark's metadata service is down right now, so it can't add "
                            "books — this is a Shelfmark-side issue, not Stackarr. We've kept your pick; retry shortly."}
                if "Invalid Path" in body:
                    return {"ok": False, "detail": "Shelfmark rejected the root folder path — check Settings → Connections → "
                            "Shelfmark root folder matches a path that exists inside the Shelfmark container."}
                return {"ok": False, "detail": "Shelfmark couldn't add this one right now — please try again in a bit."}
            author_obj = r.json()
        author_id = author_obj.get("id")
        # Gate: the author must be monitored AND set up for this media type before
        # any book can be monitored — critical when reusing an author first added
        # for the other format (its {media} root/profiles would be unset → 400).
        _ensure_media_monitored(author_obj, media, rf, qp, mp)
        # Books populate via an async author refresh — poll briefly.
        books = []
        for _ in range(8):
            books = _author_books(author_id)
            if books:
                break
            time.sleep(2)
        if not books:
            return {"ok": True, "ref": str(author_id),
                    "detail": f"Added “{author_obj.get('authorName')}” to Shelfmark; it'll search once its catalogue finishes loading."}
        # Monitor the target book(s) then dispatch a search. These calls are
        # checked (HTTP status + transport errors) so we never claim "it's
        # searching now" when nothing actually started — the old code ignored the
        # responses and reported success on a 4xx/5xx or a swallowed timeout,
        # leaving the UI stuck on "Requested". The author IS created either way, so
        # a failure here returns ok:False (retry is idempotent via _find_author).
        def _dispatch(book_ids, command):
            try:
                mr = requests.put(f"{url()}/api/v1/book/monitor", headers=_h(),
                                  json={"bookIds": book_ids, "monitored": True}, timeout=30)
                if not mr.ok:
                    return False
                cr = requests.post(f"{url()}/api/v1/command", headers=_h(), json=command, timeout=20)
                return bool(cr.ok)
            except Exception as e:
                log.debug("chaptarr dispatch failed: %s", e)
                return False

        # A request that names a specific title → grab just that book. An
        # author-level add (title is the author name / no match) → whole backlist.
        is_author_level = _norm(title) == _norm(author)
        target = None if is_author_level else _match_book(books, title, asin)
        if target:
            ok = _dispatch([target["id"]], {"name": "BookSearch", "bookIds": [target["id"]]})
            name = target.get("title") or title
        elif is_author_level:
            # genuine whole-author add → monitor everything + author search
            ok = _dispatch([b["id"] for b in books], {"name": "AuthorSearch", "authorId": author_id})
            name = author_obj.get("authorName") or author
        else:
            # a specific title was requested but isn't among the author's books — do
            # NOT fall back to grabbing the entire backlist (that also re-monitored
            # books the user had marked read, silently undoing it).
            return {"ok": False, "ref": str(author_id),
                    "detail": f"Added “{author_obj.get('authorName')}” to Shelfmark, but couldn't find “{title}” to search."}
        if ok:
            return {"ok": True, "ref": str(author_id),
                    "detail": f"Sent “{name}” to Shelfmark — it's searching now."}
        return {"ok": False, "ref": str(author_id),
                "detail": f"Added “{author_obj.get('authorName')}” to Shelfmark, but the search didn't start — retry shortly."}
    except Exception:
        return {"ok": False, "detail": "Couldn't reach Shelfmark — check it's running and connected in Settings."}
