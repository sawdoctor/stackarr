"""Background worker: refreshes the shared library snapshot (detecting
deletions -> negative taste signals), flips requests to 'available' when
they appear in the library, runs the per-user recommender on its interval,
and sends digests. Survives individual failures; daemon thread."""
import logging
import threading
import time

from . import absclient, backends, config, db, formats, notify, recommend

log = logging.getLogger("stackarr.scheduler")


def import_users() -> dict:
    """Provision a linked local account for every user on the connected sources
    that expose an admin roster (Audiobookshelf, …). Additive: creates accounts
    that don't exist yet and back-fills emails; never deletes. Returns counts."""
    created, seen = 0, 0
    for be in backends.ALL:
        if not getattr(be, "can_import_users", False):
            continue
        try:
            if not be.enabled():
                continue
        except Exception:
            continue
        try:
            roster = be.list_users()
        except Exception as e:
            log.warning("user import (%s) failed: %s", be.id, e)
            continue
        for su in roster:
            ext = str(su.get("external_id") or "")
            if not ext:
                continue
            seen += 1
            existed = db.link_get(be.id, ext) is not None
            role = "admin" if su.get("is_admin") else "user"
            u = db.provision_provider_user(be.id, ext, su.get("username", ""), role=role)
            if su.get("email") and not (u.get("email") or ""):
                db.set_email(u["id"], su["email"])
            if not existed:
                created += 1
    log.info("user import: %d new of %d source accounts", created, seen)
    return {"created": created, "seen": seen}


def refresh_library():
    seen = set()
    ok_sources = set()                # backends that reported successfully this cycle
    with db.conn() as c:
        # aggregate the library snapshot across every connected source backend
        # of an *active* format (ABS today; Kavita/Calibre-Web once connected and
        # the format toggle allows them). One source = identical to the old
        # ABS-only behaviour, just now stamped with format/source.
        for backend in backends.sources(formats.mode()):
            try:
                items = backend.library_items()
            except Exception as e:
                log.warning("library refresh failed for %s: %s", backend.id, e)
                continue
            ok_sources.add(backend.id)
            for m in items:
                if not m.get("item_id"):
                    continue
                seen.add(m["item_id"])
                c.execute(
                    "INSERT INTO library (item_id,library_id,title,author,asin,series,series_seq,narrator,format,source,last_seen) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now','localtime')) "
                    "ON CONFLICT(item_id) DO UPDATE SET title=excluded.title,"
                    "author=excluded.author,asin=excluded.asin,series=excluded.series,"
                    "series_seq=excluded.series_seq,narrator=excluded.narrator,"
                    "format=excluded.format,source=excluded.source,"
                    "last_seen=excluded.last_seen,gone_at=NULL",
                    (m["item_id"], m.get("library_id", ""), m["title"], m["author"], m.get("asin", ""),
                     m.get("series", ""), m.get("series_seq"), m.get("narrator", ""),
                     m.get("format", "audiobook"), m.get("source", "abs")))

        # deletions -> "delete habit" negative signal for every user. ONLY sweep
        # items belonging to a source that reported successfully this cycle — a
        # transient backend error (timeout/5xx/429) must NOT mass-delete its books
        # and permanently poison every user's taste with un-retractable -5 signals.
        if seen and ok_sources:
            user_ids = [r["id"] for r in c.execute("SELECT id FROM users")]
            for row in c.execute("SELECT item_id,title,author,asin,source FROM library WHERE gone_at IS NULL"):
                if row["item_id"] in seen:
                    continue
                if (row["source"] or "abs") not in ok_sources:
                    continue                      # its source didn't (successfully) report — leave it
                c.execute("UPDATE library SET gone_at=datetime('now','localtime') WHERE item_id=?", (row["item_id"],))
                if row["asin"]:
                    for uid in user_ids:
                        # upsert: a prior asin signal (e.g. a positive like) must be
                        # overridden to -5, else INSERT OR IGNORE drops it and the
                        # liked-then-deleted book never gets hard-excluded.
                        c.execute("INSERT INTO signals (user_id,kind,value,weight,why) VALUES (?,?,?,?,?) "
                                  "ON CONFLICT(user_id,kind,value) DO UPDATE SET weight=-5, why=excluded.why",
                                  (uid, "asin", row["asin"], -5, f"deleted from library: {row['title']}"))
                log.info("library item gone -> negative: %s", row["title"])

        # requests -> available when their book shows up
        newly_available = []
        for r in c.execute("SELECT id,user_id,title,author,cover,format FROM requests WHERE status IN ('queued','handed','failed')"):
            title = (r['title'] or '').strip().lower()
            if len(title) < 4:           # too short to match safely (e.g. "It")
                continue
            # match the same FORMAT so an audiobook arrival doesn't satisfy an
            # ebook request (and vice-versa)
            hit = c.execute("SELECT 1 FROM library WHERE gone_at IS NULL AND lower(title) LIKE ? "
                            "AND (?='' OR lower(author) LIKE ?) AND format=?",
                            (f"%{title[:40]}%",
                             (r['author'] or '').split(',')[0].lower(),
                             f"%{(r['author'] or '').split(',')[0].lower()}%",
                             r['format'] or 'audiobook')).fetchone()
            if hit:
                c.execute("UPDATE requests SET status='available',updated_at=datetime('now','localtime') WHERE id=?", (r["id"],))
                newly_available.append(dict(r))

    # notify outside the DB transaction (each channel self-gates on its config)
    base = db.get_meta("public_url", "")
    for r in newly_available:
        try:
            notify.request_available(r, base_url=base)
        except Exception as e:
            log.warning("availability notify failed for %s: %s", r.get("title"), e)


def interval_hours() -> int:
    try:
        return max(int(db.get_meta("suggest_interval_hours", str(config.SUGGEST_INTERVAL_HOURS))), 1)
    except ValueError:
        return config.SUGGEST_INTERVAL_HOURS


def run_for_user(user_id: int, force: bool = False) -> int:
    """Run the recommender for one user if due (or forced), notify on new
    picks, and stamp the per-user last-run time. Returns picks added."""
    if not config.SUGGEST_ENABLED:
        return 0
    key = f"suggest_run_{user_id}"
    last = db.get_meta(key)
    if not force and last and (time.time() - float(last)) / 3600 < interval_hours():
        return 0
    # Atomically claim the run so a double-tap of /api/run-now (force=True skips
    # the due-check) or an overlap with the background cycle can't run two
    # recommenders concurrently and blow past the per-lane/author caps.
    if not db.claim_run(user_id):
        return 0
    added = 0
    try:
        db.set_meta(key, str(time.time()))     # inside try so finally always releases the lock
        with db.conn() as c:
            pending = c.execute("SELECT COUNT(*) n FROM suggestions WHERE user_id=? AND status='pending'",
                                (user_id,)).fetchone()["n"]
        room = max(config.SUGGEST_MAX_PENDING - pending, 0)
        if not room:
            return 0
        active = formats.active()
        # audiobook first (it's primary and dedupes against nothing), ebook
        # second so it skips any title already picked as an audiobook. Split the
        # remaining room across active formats.
        share = max(room // len(active), 1)
        if "audiobook" in active:
            try:
                added += recommend.run(user_id, share)
            except Exception as e:
                log.warning("audiobook recommend failed for user %s: %s", user_id, e)
        if "ebook" in active and added < room:    # only if room remains
            try:
                from . import recommend_ebook
                added += recommend_ebook.run(user_id, room - added)
            except Exception as e:
                log.warning("ebook recommend failed for user %s: %s", user_id, e)
    finally:
        db.release_run(user_id)
    if added:
        with db.conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT title,author,reason,cover FROM suggestions "
                "WHERE user_id=? AND status='pending' ORDER BY score DESC", (user_id,))]
        # per-user recipient + per-user throttle so each user gets their own list
        # and one user's digest can't suppress everyone else's (multi-user safe).
        u = db.get_user(user_id) or {}
        # global SMTP 'to' is an acceptable recipient only on a single-user install;
        # in multi-user it would leak this user's private list to the admin mailbox.
        notify.suggestion_digest(rows, base_url=db.get_meta("public_url", ""),
                                 to=u.get("email") or None, throttle_key=f"last_email_ts_{user_id}",
                                 allow_global=(db.user_count() == 1))
    return added


# Auto-add tiers: lane allow-set (None = all lanes) and a per-cycle cap.
AUTO_TIERS = {
    "conservative": ({"series"}, 3),
    "moderate": ({"series", "author", "importlist"}, 5),
    "aggressive": (None, 10),
}


def auto_approve(user_id: int) -> int:
    """Automatic acquisition is disabled on the controlled ebook branch.

    Suggestions/recommendations may still be generated, but every ebook
    acquisition must originate from one explicit user request or approval.
    """
    return 0

def suggestion_cycle():
    with db.conn() as c:
        users = [r["id"] for r in c.execute("SELECT id FROM users")]
    for uid in users:
        run_for_user(uid)
        try:
            auto_approve(uid)
        except Exception as e:
            log.warning("auto-approve failed for user %s: %s", uid, e)


def new_release_radar():
    """Follow-author new-release radar: for authors users read (4-5★ ratings +
    positive author signals), surface a brand-new release once and notify.
    Off by default (notify_newrelease_enabled); capped + deduped so it's cheap."""
    import datetime
    if db.get_meta("notify_newrelease_enabled", "0") != "1":
        return
    today = datetime.date.today()
    cutoff = str(today - datetime.timedelta(days=30))
    base = db.get_meta("public_url", "")
    # check the catalogue of the primary active format
    fmt = formats.primary()
    if fmt == "ebook":
        from . import ebookmeta
        lookup = lambda a: ebookmeta.by_author(a, num=6)
    else:
        from . import audible
        lookup = lambda a: audible.by_author(a, num=6)
    with db.conn() as c:
        authors = {r["author"].split(",")[0].strip()
                   for r in c.execute("SELECT author FROM ratings WHERE stars>=4 AND author<>''")}
        authors |= {r["value"].split(",")[0].strip()
                    for r in c.execute("SELECT value FROM signals WHERE kind='author' AND weight>0")}
    for author in list(authors)[:20]:
        if not author:
            continue
        try:
            books = lookup(author)
        except Exception:
            continue
        for b in books:
            rd = b.get("release_date") or ""
            if not (cutoff <= rd <= str(today)):       # released in the last 30 days
                continue
            key = f"nr:{(b.get('asin') or b.get('id') or b.get('title',''))}"
            if db.get_meta(key):
                continue
            b.setdefault("format", fmt)
            try:
                notify.new_release(b, base_url=base)
                db.set_meta(key, str(today))     # mark done only AFTER a successful notify,
                log.info("new-release radar: %s — %s", b.get("title"), author)  # else a transient
            except Exception as e:               # failure would suppress the alert forever
                log.warning("new-release notify failed: %s", e)


def _loop():
    while True:
        try:
            refresh_library()
        except Exception as e:
            log.warning("refresh cycle failed: %s", e)
        try:
            suggestion_cycle()
        except Exception as e:
            log.warning("suggestion cycle failed: %s", e)
        try:
            new_release_radar()
        except Exception as e:
            log.warning("new-release radar failed: %s", e)
        try:
            _daily_user_sync()
        except Exception as e:
            log.warning("user sync failed: %s", e)
        time.sleep(config.LIBRARY_REFRESH_MINUTES * 60)


def _daily_user_sync():
    """Run import_users at most once a day, when the admin has enabled the daily
    sync setting (off by default — import is otherwise an explicit admin action)."""
    if db.get_meta("user_sync", "0") != "1":
        return
    last = db.get_meta("last_user_sync_ts", "")
    if last and (time.time() - float(last)) < 86400:
        return
    import_users()
    db.set_meta("last_user_sync_ts", str(int(time.time())))


def start():
    threading.Thread(target=_loop, name="stackarr-worker", daemon=True).start()
    log.info("background worker started (every %d min)", config.LIBRARY_REFRESH_MINUTES)
