"""Audiobook acquisition through the existing ABR -> Shelfmark -> TorBox bridge."""

import logging
import os

import requests

log = logging.getLogger("stackarr.audiobridge")


def bridge_url() -> str:
    return os.environ.get("AUDIOBOOK_BRIDGE_URL", "").rstrip("/")


def bridge_token() -> str:
    return os.environ.get("AUDIOBOOK_BRIDGE_TOKEN", "")


def job_status(asin: str) -> dict | None:
    """Return the bridge's current asynchronous job state.

    Network/bridge errors are treated as unknown so a temporary outage never
    falsely marks an otherwise valid request as failed.
    """
    asin = (asin or "").strip()
    if not asin or not bridge_url() or not bridge_token():
        return None

    try:
        r = requests.get(
            f"{bridge_url()}/status/{asin}",
            headers={"X-Bridge-Token": bridge_token()},
            timeout=20,
        )
    except requests.RequestException as e:
        log.warning("audiobook bridge status failed for %r: %s", asin, e)
        return None

    if r.status_code == 404:
        return None

    if r.status_code != 200:
        log.warning(
            "audiobook bridge status returned HTTP %s for %r",
            r.status_code, asin,
        )
        return None

    try:
        data = r.json()
    except ValueError:
        return None

    return data if isinstance(data, dict) else None


def add_and_search(title: str, author: str, asin: str = "") -> dict:
    """Hand one audiobook to the existing, proven ABB/TorBox bridge."""

    title = (title or "").strip()
    author = (author or "").strip()
    asin = (asin or "").strip()

    if not bridge_url():
        return {"ok": False, "detail": "Audiobook bridge URL is not configured."}

    if not bridge_token():
        return {"ok": False, "detail": "Audiobook bridge token is not configured."}

    if not title:
        return {"ok": False, "detail": "Audiobook title is required."}

    # The existing bridge intentionally keys its jobs by Audible ASIN.
    if not asin:
        return {
            "ok": False,
            "detail": "This audiobook has no Audible ASIN, so it cannot be handed to the existing ABB/TorBox bridge safely.",
        }

    payload = {
        "asin": asin,
        "title": title,
        "authors": author,
        "narrators": "",
        "requested_by": "Stackarr",
    }

    try:
        r = requests.post(
            f"{bridge_url()}/abr/request",
            json=payload,
            headers={"X-Bridge-Token": bridge_token()},
            timeout=30,
        )
    except requests.RequestException as e:
        log.warning("audiobook bridge request failed for %r: %s", title, e)
        return {
            "ok": False,
            "detail": f"Audiobook bridge could not be reached: {e}",
        }

    try:
        data = r.json()
    except ValueError:
        data = {}

    if r.status_code not in (200, 202):
        error = str(data.get("error") or f"HTTP {r.status_code}")
        log.warning(
            "audiobook bridge rejected title=%r asin=%r status=%s error=%r",
            title, asin, r.status_code, error,
        )
        return {
            "ok": False,
            "detail": f"Audiobook bridge rejected the request: {error}",
        }

    status = str(data.get("status") or "accepted")

    log.info(
        "audiobook handoff accepted title=%r author=%r asin=%r bridge_status=%r",
        title, author, asin, status,
    )

    return {
        "ok": True,
        "detail": f"Handed audiobook to ABB/TorBox bridge (status: {status}).",
        "ref": asin,
    }
