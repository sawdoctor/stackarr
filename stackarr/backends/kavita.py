"""Kavita as an ebook source backend. Connected (not logged-into) via a Kavita
API key — Stackarr exchanges it for a short-lived JWT through Kavita's plugin
auth endpoint. Kavita exposes reliable per-user reading progress, so it's the
preferred ebook "what have I finished" signal.

Note on identity: ABS owns login. Kavita is connected with one API key, so its
reading progress reflects that Kavita account (fine for a household/single-user
install — JD's case). Per-user Kavita mapping isn't attempted here."""
from __future__ import annotations

import datetime
import logging
import time

import requests

from .. import config, db
from .base import Backend

log = logging.getLogger("stackarr.kavita")


def _url() -> str:
    return db.setting("kavita_url", config.KAVITA_URL).rstrip("/")


def _key() -> str:
    return db.setting("kavita_api_key", config.KAVITA_API_KEY)


def _parse_dt(s: str) -> int:
    """Kavita ISO timestamp -> epoch ms. Kavita's 'never' is year 0001."""
    if not s or s.startswith("0001"):
        return 0
    try:
        dt = datetime.datetime.fromisoformat(s[:26])
        if dt.tzinfo is None:                       # Kavita/.NET emits offset-less UTC;
            dt = dt.replace(tzinfo=datetime.timezone.utc)   # don't read it as host-local
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return 0


def _clean_series_name(name: str) -> str:
    """E-Books imported as loose files give messy series names like
    'A Crown of Swords (The Wheel of Time' — drop a dangling '(' fragment."""
    name = (name or "").strip()
    if name.count("(") > name.count(")"):
        name = name.rsplit("(", 1)[0].strip()
    return name


class KavitaBackend(Backend):
    id = "kavita"
    label = "Kavita"
    media_format = "ebook"
    is_login = False
    supports_progress = True
    can_write_progress = True
    can_login = True

    def verify_login(self, username: str, password: str) -> dict | None:
        try:
            r = requests.post(f"{_url()}/api/Account/login",
                              json={"username": username, "password": password}, timeout=20)
            if r.status_code != 200:
                return None
            d = r.json() or {}
            if not d.get("token"):
                return None
            return {"external_id": str(d.get("username") or username),
                    "username": d.get("username") or username,
                    "token": d.get("token", ""), "is_admin": False}
        except Exception as e:
            log.warning("kavita login failed for %s: %s", username, e)
            return None

    _jwt = ""
    _jwt_exp = 0.0

    # --- auth -------------------------------------------------------------
    def _token(self) -> str:
        if self._jwt and time.time() < self._jwt_exp:
            return self._jwt
        r = requests.post(f"{_url()}/api/Plugin/authenticate",
                          params={"apiKey": _key(), "pluginName": "Stackarr"}, timeout=20)
        r.raise_for_status()
        self._jwt = r.json().get("token", "")
        self._jwt_exp = time.time() + 9 * 60          # JWT lives ~10 min; refresh early
        return self._jwt

    def _get(self, path: str, **kw):
        h = {"Authorization": f"Bearer {self._token()}"}
        return requests.get(f"{_url()}{path}", headers=h, timeout=30, **kw)

    def _post(self, path: str, **kw):
        h = {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"}
        return requests.post(f"{_url()}{path}", headers=h, timeout=30, **kw)

    # --- connection -------------------------------------------------------
    def enabled(self) -> bool:
        return bool(_url() and _key())

    def test(self) -> dict:
        try:
            r = self._get("/api/library/libraries")
            r.raise_for_status()
            n = len(r.json())
            return {"ok": True, "detail": f"Connected — {n} librar{'y' if n == 1 else 'ies'}"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def mark_read(self, user: dict, item_id: str, finished: bool = True) -> bool:
        if not (item_id or "").startswith("kavita:"):
            return False
        try:
            parts = item_id.split(":")
            sid = int(parts[1])

            if len(parts) >= 3:
                vid = int(parts[2])
                ep = "/api/Reader/mark-volume-read" if finished else "/api/Reader/mark-volume-unread"
                r = self._post(ep, json={
                    "seriesId": sid,
                    "volumeId": vid,
                    "generateReadingSession": False,
                })
            else:
                ep = "/api/Reader/mark-read" if finished else "/api/Reader/mark-unread"
                r = self._post(ep, json={"seriesId": sid})

            return r.status_code in (200, 204)
        except Exception as e:
            log.warning("kavita mark_read failed: %s", e)
            return False
    # --- data -------------------------------------------------------------
    def _all_series(self, raise_on_error: bool = False) -> list[dict]:
        # Paginate — a single PageSize=2000 silently drops series past the first
        # page in large libraries. When raise_on_error, propagate so a transient
        # failure surfaces as an error (refresh_library skips the backend) rather
        # than an empty list that reads as "zero books" and mass-deletes them.
        out, page = [], 1
        while page <= 50:                       # safety cap (~100k series)
            try:
                r = self._post("/api/series/all-v2", params={"PageNumber": page, "PageSize": 2000}, json={})
                r.raise_for_status()
                batch = r.json() or []
            except Exception as e:
                log.warning("kavita series list failed (page %s): %s", page, e)
                if raise_on_error:
                    raise
                return out
            out.extend(batch)
            if len(batch) < 2000:
                break
            page += 1
        return out

    def library_items(self) -> list[dict]:
        out = []

        for s in self._all_series(raise_on_error=True):
            sid = s.get("id")
            if sid is None:
                continue

            series = _clean_series_name(
                s.get("name") or s.get("originalName") or ""
            )
            if not series:
                continue

            r = self._get("/api/Series/volumes", params={"seriesId": sid})
            r.raise_for_status()
            volumes = r.json() or []

            emitted = False

            for v in volumes:
                vid = v.get("id")
                seq = v.get("number")

                for ch in v.get("chapters") or []:
                    cid = ch.get("id")
                    if cid is None:
                        continue

                    files = ch.get("files") or []
                    path = files[0].get("filePath", "") if files else ""

                    title = path.rsplit("/", 1)[-1]
                    if "." in title:
                        title = title.rsplit(".", 1)[0]

                    for suffix in (" (epub)", " (pdf)", " (mobi)", " (azw3)"):
                        if title.lower().endswith(suffix):
                            title = title[:-len(suffix)]

                    title = title.strip() or series

                    out.append(self._tag({
                        "item_id": f"kavita:{sid}:{vid}:{cid}",
                        "library_id": str(s.get("libraryId", "")),
                        "title": title,
                        "author": "",
                        "asin": "",
                        "series": series,
                        "series_seq": seq,
                        "narrator": "",
                    }))
                    emitted = True

            if not emitted:
                out.append(self._tag({
                    "item_id": f"kavita:{sid}",
                    "library_id": str(s.get("libraryId", "")),
                    "title": series,
                    "author": "",
                    "asin": "",
                    "series": series,
                    "series_seq": None,
                    "narrator": "",
                }))

        return out

    def reading_history(self, user: dict) -> list[dict]:
        if db.user_count() > 1:
            return []

        out = []

        for s in self._all_series():
            sid = s.get("id")
            if sid is None:
                continue

            try:
                r = self._get("/api/Series/volumes", params={"seriesId": sid})
                r.raise_for_status()
                volumes = r.json() or []
            except Exception as e:
                log.warning("kavita history volumes failed for %s: %s", sid, e)
                continue

            for v in volumes:
                vid = v.get("id")
                if vid is None:
                    continue

                for ch in v.get("chapters") or []:
                    cid = ch.get("id")
                    if cid is None:
                        continue

                    try:
                        r = self._get("/api/Reader/get-progress",
                                      params={"chapterId": cid})
                        if r.status_code != 200:
                            continue
                        progress = r.json() or {}
                    except Exception:
                        continue

                    page = progress.get("pageNum") or 0
                    if page <= 0:
                        continue

                    pages = max(
                        [int(f.get("pages") or 0)
                         for f in (ch.get("files") or [])] or [0]
                    )

                    out.append({
                        "item_id": f"kavita:{sid}:{vid}:{cid}",
                        "finished": bool(pages and page >= pages),
                        "progress": min(page / pages, 1.0) if pages else 0.0,
                        "last_update": _parse_dt(
                            progress.get("lastModifiedUtc") or ""
                        ),
                    })

        out.sort(key=lambda x: x["last_update"], reverse=True)
        return out

