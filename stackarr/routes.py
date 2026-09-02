"""Routes: pages + the JSON API the front-end uses. Approval, manual
'already read', 5-star ratings, discover, settings, email preview, health."""
import json
import logging
import re

from flask import (Blueprint, jsonify, redirect, render_template, request,
                   session, url_for)

from . import (absclient, audiobridge, audible, audnexus, auth, chaptarr, config, db,
               discover, ebookmeta, formats, notify, recommend, shelfmark, tagging)

log = logging.getLogger("stackarr.routes")
bp = Blueprint("main", __name__)

# Recommendation lanes — single source of truth for their display titles and the
# order they appear on the Suggestions page. To add a lane: give it a key here,
# emit suggestions with that `lane` value from the recommender, and (optionally)
# place it in LANE_ORDER. Both suggestions_page and lane_grid read these.
LANE_TITLES = {
    "series": "Series to finish", "author": "More from authors you love",
    "enjoyed": "Readers also enjoyed", "discover_author": "New authors to discover",
    "narrator": "Narrators you love", "genre": "More in your favourite genres",
    "mood": "Matches your mood", "hidden": "Off the beaten path", "awards": "Award winners",
    "short": "Short listens", "epic": "Epic listens", "upcoming": "New & upcoming",
    "importlist": "From your reading list", "discover": "Popular picks", "foryou": "For you",
}
LANE_ORDER = ["series", "enjoyed", "mood", "discover_author", "author", "narrator", "genre",
              "hidden", "awards", "short", "epic", "upcoming", "importlist", "foryou", "discover"]


# ------------------------------------------------------------------ auth ---
_LOGIN_FAILS = {}          # ip -> (count, first_ts); brute-force throttle
_LOCK_AFTER = 5
_LOCK_WINDOW = 900         # 15 min


@bp.route("/login", methods=["GET", "POST"])
def login():
    import time
    error = ""
    ip = request.remote_addr or "?"
    providers = auth.login_providers()
    first_run = db.user_count() == 0          # no accounts yet -> bootstrap admin
    ctx = {"providers": providers, "first_run": first_run,
           "allow_register": True}

    def _page(err="", code=200):
        return render_template("login.html", error=err, **ctx), code

    def _safe_next():
        # only a local, relative path — reject "//evil", "/\evil" (browsers
        # normalise \ → /), and absolute URLs (open redirect).
        nxt = request.args.get("next") or ""
        return nxt if re.match(r"^/[^/\\]", nxt) else url_for("main.index")

    if request.method == "POST":
        cnt, first = _LOGIN_FAILS.get(ip, (0, 0.0))
        if cnt >= _LOCK_AFTER and (time.time() - first) < _LOCK_WINDOW:
            return _page("Too many attempts — try again in a few minutes.", 429)
        if (time.time() - first) >= _LOCK_WINDOW:
            cnt, first = 0, time.time()

        action = request.form.get("action", "signin")
        provider = (request.form.get("provider") or "local").strip()
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if action == "register":
            invite = (request.form.get("invite") or "").strip()
            want = db.get_meta("invite_code", "")
            if not first_run and want and invite != want:
                error = "That invite code isn't right."
            elif not username or not password:
                error = "Pick a username and password."
            else:
                u = auth.register_local(username, password, request.form.get("email", "").strip())
                if u:
                    _LOGIN_FAILS.pop(ip, None)
                    return redirect(_safe_next())
                error = "That username is taken."
        else:
            if provider == "local":
                u = auth.do_login_local(username, password)
            else:
                u = auth.do_login_provider(provider, username, password)
            if u:
                _LOGIN_FAILS.pop(ip, None)
                return redirect(_safe_next())
            error = "Wrong username or password."

        _LOGIN_FAILS[ip] = (cnt + 1, first or time.time())
        return _page(error)
    return _page()


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("main.login"))


# ----------------------------------------------------------------- pages ---
ONBOARD_THRESHOLD = 5      # below this many ratings, nudge the quick-rate flow


def _onboarding_books(u, limit=12):
    """Finished-in-ABS books the user hasn't rated yet — the seed set for the
    quick-rate onboarding card. Most have no ASIN, so we key on rating_key."""
    try:
        hist = absclient.listening_history(u["abs_token"])
    except Exception:
        return []
    with db.conn() as c:
        lib = {r["item_id"]: dict(r) for r in
               c.execute("SELECT item_id,title,author,asin FROM library")}
        rated = {r["asin"] for r in c.execute(
            "SELECT asin FROM ratings WHERE user_id=? AND asin<>''", (u["id"],))}
        hidden = {r["value"] for r in c.execute(
            "SELECT value FROM signals WHERE user_id=? AND kind='hist_hidden'", (u["id"],))}
    out, seen = [], set()
    for h in hist:
        if not h.get("finished"):
            continue
        m = lib.get(h["item_id"]) or {}
        title, author, asin = m.get("title", ""), m.get("author", ""), (m.get("asin") or "").strip()
        rk = db.rating_key(asin, title, author)
        if not title or rk in rated or rk in hidden or rk in seen:
            continue
        seen.add(rk)
        out.append({"rkey": rk, "title": title, "author": author,
                    "cover": url_for("main.cover", item_id=h["item_id"])})
        if len(out) >= limit:
            break
    return out


@bp.route("/")
@auth.login_required
def index():
    return redirect(url_for("main.home_page"))


@bp.route("/home")
@auth.login_required
def home_page():
    """Dashboard hub: what you're reading, what's next, goal progress, a few
    fresh picks, and new from authors you follow — links out to everything."""
    import datetime
    u = auth.current_user()
    shelves, counts = _shelves_data(u)
    # a few fresh picks + new-from-upcoming + goal + up-next count
    with db.conn() as c:
        fresh = [dict(r) for r in c.execute(
            "SELECT id,title,author,cover,reason,format,asin FROM suggestions "
            "WHERE user_id=? AND status='pending' AND lane NOT IN ('upcoming') ORDER BY score DESC LIMIT 8", (u["id"],))]
        upcoming = [dict(r) for r in c.execute(
            "SELECT id,title,author,cover,reason,format,asin,extra FROM suggestions "
            "WHERE user_id=? AND lane='upcoming' AND status='pending' ORDER BY extra LIMIT 6", (u["id"],))]  # ASC: soonest first (match Upcoming page)
        want_n = c.execute("SELECT COUNT(*) n FROM shelf WHERE user_id=? AND state='want'", (u["id"],)).fetchone()["n"]
        library_n = c.execute(
            "SELECT COUNT(*) n FROM library WHERE gone_at IS NULL"
        ).fetchone()["n"]
    year = datetime.date.today().year
    read_year = sum(1 for d in _finish_dates_all(u) if d.startswith(str(year)))
    try:
        goal = int(db.get_meta(f"goal_{u['id']}", "0") or 0)
    except ValueError:
        goal = 0
    return render_template("home.html", fresh=fresh, upcoming=upcoming,
                           goal=goal, read_year=read_year, year=year, want_n=want_n, library_n=library_n,
                           shelves=shelves, counts=counts)


@bp.route("/suggestions")
@auth.login_required
def suggestions_page():
    u = auth.current_user()
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM suggestions WHERE user_id=? AND status='pending' ORDER BY lane,score DESC",
            (u["id"],))]
        for r in rows:
            r["available"] = _owned(c, r["asin"], r["title"], r["author"])
    lanes = {}
    for r in rows:
        lanes.setdefault(r["lane"], []).append(r)
    lane_titles = LANE_TITLES
    lanes = {k: lanes[k] for k in LANE_ORDER if k in lanes}
    # authors to feature as browse cards — only SUGGESTED authors (new discoveries /
    # readers-also-enjoyed), NOT the "authors you love" backlist lane.
    seen_a, rec_authors = set(), []
    for r in sorted(rows, key=lambda x: x["score"], reverse=True):
        if r["lane"] == "author":
            continue
        a = (r["author"] or "").split(",")[0].strip()
        if a and a.lower() not in seen_a:
            seen_a.add(a.lower())
            rec_authors.append(a)
    rec_authors = rec_authors[:14]
    from . import discover
    # dashboard rows (only render if content exists)
    try:
        recently_added = absclient.recent_added(14)
    except Exception:
        recently_added = []
    with db.conn() as c:
        recent_requests = [dict(r) for r in c.execute(
            "SELECT title, author, cover, status, asin FROM requests WHERE user_id=? "
            "ORDER BY id DESC LIMIT 14", (u["id"],))]
        rated_count = c.execute("SELECT COUNT(*) FROM ratings WHERE user_id=?", (u["id"],)).fetchone()[0]
        onboard_off = bool(c.execute(
            "SELECT 1 FROM signals WHERE user_id=? AND kind='onboard_dismissed'", (u["id"],)).fetchone())
    # quick-rate onboarding: only when taste is thin and not dismissed
    onboard_books = [] if (rated_count >= ONBOARD_THRESHOLD or onboard_off) else _onboarding_books(u)
    with db.conn() as c:
        vibes_done = bool(c.execute("SELECT 1 FROM signals WHERE user_id=? AND kind='vibes_done'", (u["id"],)).fetchone())
    show_vibes = not vibes_done and rated_count < ONBOARD_THRESHOLD
    return render_template("suggestions.html", lanes=lanes, lane_titles=lane_titles,
                           genres=discover.DEFAULT_GENRES, rec_authors=rec_authors,
                           abs_base=absclient.abs_url(),
                           recently_added=recently_added, recent_requests=recent_requests,
                           onboard_books=onboard_books, onboard_target=ONBOARD_THRESHOLD,
                           show_vibes=show_vibes, all_moods=tagging.ALL_MOODS)


@bp.route("/lane/<lane>")
@auth.login_required
def lane_grid(lane):
    u = auth.current_user()
    titles = LANE_TITLES
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM suggestions WHERE user_id=? AND lane=? AND status='pending' ORDER BY score DESC",
            (u["id"], lane))]
        for r in rows:
            r["available"] = _owned(c, r["asin"], r["title"], r["author"])
    return render_template("lane.html", rows=rows, lane=lane, title=titles.get(lane, lane))


@bp.route("/library")
@auth.login_required
def library_page():
    """Shared server-wide library: ABS audiobooks + Kavita ebooks.

    Availability belongs to the Stackarr installation, not to an individual
    user's provider account, so every logged-in user sees the same inventory.
    """
    with db.conn() as c:
        books = [dict(r) for r in c.execute("""
            SELECT item_id, title, author, asin, series, format, source
            FROM library
            WHERE gone_at IS NULL
            ORDER BY lower(title), lower(author), format
        """)]

    return render_template("library.html", books=books)


@bp.route("/discover")
@auth.login_required
def discover_page():
    return render_template("discover.html")


@bp.route("/requests")
@auth.login_required
def requests_page():
    u = auth.current_user()
    admin = u["role"] == "admin"
    wanted = bool(request.args.get("wanted"))            # Sonarr-style: couldn't-grab list
    where = "WHERE status='failed'" if wanted else "WHERE 1=1"
    with db.conn() as c:
        if admin and request.args.get("all"):
            rows = [dict(r) for r in c.execute(f"SELECT r.*, u.username FROM requests r "
                    f"JOIN users u ON u.id=r.user_id {where.replace('status','r.status')} ORDER BY r.id DESC LIMIT 200")]
        else:
            rows = [dict(r) for r in c.execute(f"SELECT * FROM requests {where} AND user_id=? ORDER BY id DESC LIMIT 200", (u["id"],))]
        # admins get the approval queue (everyone's pending requests)
        approvals = []
        if admin:
            approvals = [dict(r) for r in c.execute(
                "SELECT r.*, u.username FROM requests r JOIN users u ON u.id=r.user_id "
                "WHERE r.status='pending_approval' ORDER BY r.id")]
    # a normal user shouldn't see their pending_approval rows twice; they already
    # appear in their own list with a 'pending_approval' status badge.
    return render_template("requests.html", requests=rows, admin=admin, wanted=wanted,
                           approvals=approvals)


def _finish_dates_all(u) -> list[str]:
    """Every book the user has FINISHED, as ISO dates — combining Audiobookshelf
    listening history, connected ebook sources, and the manual 'read' shelf. Each
    book counts ONCE: a title finished in Audiobookshelf AND marked read on the
    shelf is deduped by rating-key so the goal/heatmap aren't inflated."""
    import datetime
    # library item_id -> rating-key, so a source finish can be matched to a shelf finish
    with db.conn() as c:
        lib = {r["item_id"]: db.rating_key(r["asin"] or "", r["title"], r["author"])
               for r in c.execute("SELECT item_id,title,author,asin FROM library")}
    finishes: dict[str, str] = {}      # book key -> earliest finish date

    def _add(key, date):
        if not key:
            return
        if key not in finishes or date < finishes[key]:
            finishes[key] = date

    try:
        for h in absclient.listening_history(u["abs_token"]):
            if h.get("finished") and h.get("last_update"):
                d = datetime.date.fromtimestamp(h["last_update"] / 1000).isoformat()
                _add(lib.get(h["item_id"]) or "abs:" + str(h["item_id"]), d)
    except Exception:
        pass
    if formats.show("ebook"):
        from . import backends
        for be in backends.sources("ebook"):
            try:
                for h in be.reading_history(u):
                    if h.get("finished") and h.get("last_update"):
                        d = datetime.date.fromtimestamp(h["last_update"] / 1000).isoformat()
                        _add(lib.get(h["item_id"]) or "eb:" + str(h["item_id"]), d)
            except Exception:
                pass
    for rkey, d in db.finished_keyed(u["id"]):     # manual read-shelf finishes
        _add(rkey, d)
    return list(finishes.values())


def _heatmap(dates: list[str]) -> dict:
    """Build a GitHub-style heatmap: 53 weeks x 7 days ending today, each cell a
    finish count. Returns {weeks: [[{date,count}]], total, max, months}."""
    import datetime
    from collections import Counter
    counts = Counter(d for d in dates if d)
    today = datetime.date.today()
    start = today - datetime.timedelta(days=today.weekday() + 1 + 52 * 7)   # Sunday ~53wk ago
    weeks, cur = [], start
    mx = 0
    while cur <= today:
        week = []
        for _ in range(7):
            ds = cur.isoformat()
            n = counts.get(ds, 0)
            mx = max(mx, n)
            week.append({"date": ds, "count": n, "future": cur > today})
            cur += datetime.timedelta(days=1)
        weeks.append(week)
    return {"weeks": weeks, "total": sum(counts.values()), "max": mx}


@bp.route("/insights")
@auth.login_required
def insights_page():
    import datetime
    u = auth.current_user()
    hist = absclient.listening_history(u["abs_token"])
    stats = absclient.listening_stats(u["abs_token"])
    # ebook reading history from connected ebook sources (joined to library)
    from . import backends
    ebook_hist = []
    if formats.show("ebook"):
        for be in backends.sources("ebook"):
            try:
                ebook_hist += be.reading_history(u)
            except Exception:
                pass
    with db.conn() as c:
        lib = {r["item_id"]: dict(r) for r in c.execute("SELECT item_id,title,author,format FROM library")}
        ratings = [dict(r) for r in c.execute("SELECT stars,asin,title,author,format FROM ratings WHERE user_id=?", (u["id"],))]
        req_avail = c.execute("SELECT COUNT(*) n FROM requests WHERE user_id=? AND status='available'", (u["id"],)).fetchone()["n"]
    authors, finished, in_prog = {}, 0, 0
    by_format = {"audiobook": 0, "ebook": 0}
    finish_dates = []
    for h in hist:
        if h["finished"]:
            finished += 1; by_format["audiobook"] += 1
            if h.get("last_update"):
                finish_dates.append(datetime.date.fromtimestamp(h["last_update"] / 1000).isoformat())
        elif h["progress"] > 0.02:
            in_prog += 1
        m = lib.get(h["item_id"])
        if m and m["author"]:
            a = m["author"].split(",")[0].split(" - ")[0].strip()
            if a:
                authors[a] = authors.get(a, 0) + 1
    for h in ebook_hist:
        if h.get("finished"):
            finished += 1; by_format["ebook"] += 1
            if h.get("last_update"):
                finish_dates.append(datetime.date.fromtimestamp(h["last_update"] / 1000).isoformat())
        elif (h.get("progress") or 0) > 0.02:
            in_prog += 1
    finish_dates += db.finished_dates(u["id"])      # read-shelf finishes
    # mood profile from cached tags of rated books (bounded, no fetch)
    from collections import Counter
    moodc = Counter()
    for r in ratings[:120]:
        rk = db.rating_key(r["asin"], r["title"], r["author"])
        for m in db.tags_for(rk).get("mood", []):
            moodc[m] += 1
    top_moods = moodc.most_common(6)
    heat = _heatmap(finish_dates)
    year = datetime.date.today().year
    read_year = sum(1 for d in finish_dates if d.startswith(str(year)))
    try:
        goal = int(db.get_meta(f"goal_{u['id']}", "0") or 0)
    except ValueError:
        goal = 0
    hours = round(stats["total_seconds"] / 3600)
    star_vals = [r["stars"] for r in ratings]
    facts = []
    if hours:
        facts.append(("⏳", f"{hours:,} hours", "listened all-time" + (f" — about {round(hours/24):,} days" if hours >= 48 else "")))
    if top := (sorted(authors.items(), key=lambda x: x[1], reverse=True)[:1] or [None])[0]:
        facts.append(("✍️", top[0], f"your most-read author ({top[1]} books)"))
    if star_vals:
        facts.append(("⭐", f"{round(sum(star_vals)/len(star_vals),1)} avg", f"across {len(star_vals)} rated books"))
    if top_moods:
        facts.append(("🎭", top_moods[0][0], "your most-read mood"))
    if req_avail:
        facts.append(("📚", f"{req_avail}", "books added via Stackarr"))
    top_authors = sorted(authors.items(), key=lambda x: x[1], reverse=True)[:10]
    return render_template("insights.html", total=len(hist), finished=finished, in_progress=in_prog,
                           hours=hours, req_avail=req_avail, facts=facts, top_authors=top_authors,
                           by_format=by_format, heat=heat, top_moods=top_moods,
                           goal=goal, read_year=read_year, year=year)


@bp.route("/history")
@auth.login_required
def history_page():
    """Books you've read — finished in Audiobookshelf, rated, or marked read —
    each with a 1-5 star rating control that feeds the recommender."""
    u = auth.current_user()
    hist = absclient.listening_history(u["abs_token"])
    with db.conn() as c:
        lib = {r["item_id"]: dict(r) for r in
               c.execute("SELECT item_id,title,author,asin FROM library")}
        rated = {r["asin"]: dict(r) for r in c.execute(
            "SELECT asin,title,author,stars FROM ratings WHERE user_id=? AND asin<>''", (u["id"],))}
        read_sig = c.execute(
            "SELECT value,why FROM signals WHERE user_id=? AND kind='asin' "
            "AND (why LIKE 'already read:%' OR why LIKE 'marked read:%')", (u["id"],)).fetchall()
        # books the user explicitly removed from history (stays gone even though
        # it's still finished in Audiobookshelf)
        hidden = {r["value"] for r in c.execute(
            "SELECT value FROM signals WHERE user_id=? AND kind='hist_hidden'", (u["id"],))}

    books, seen = [], set()

    def key(asin, title):
        return (asin or "").lower() or (title or "").strip().lower()

    def add(asin, title, author, cover, when, fmt="audiobook"):
        k = key(asin, title)
        if not k or k in seen:
            return
        seen.add(k)
        rk = db.rating_key(asin, title, author)
        if rk in hidden:
            return
        # no stored cover (ebook / marked-read item) -> resolve it lazily via /coverart
        if not cover:
            cover = url_for("main.coverart", asin=asin or "", title=title or "",
                            author=author or "", fmt=fmt)
        books.append({"asin": asin or "", "rkey": rk, "title": title or "Untitled",
                      "author": author or "", "cover": cover, "format": fmt,
                      "stars": (rated.get(rk) or {}).get("stars", 0), "when": when})

    # 1) finished in Audiobookshelf (the real listening history, with covers)
    for h in hist:
        if not h["finished"]:
            continue
        m = lib.get(h["item_id"]) or {}
        add((m.get("asin") or "").strip(), m.get("title", ""), m.get("author", ""),
            url_for("main.cover", item_id=h["item_id"]), h["last_update"], "audiobook")
    # 1b) finished ebooks from connected ebook sources (Kavita/Calibre-Web)
    if formats.show("ebook"):
        from . import backends
        with db.conn() as c:
            elib = {r["item_id"]: dict(r) for r in
                    c.execute("SELECT item_id,title,author FROM library WHERE format='ebook'")}
        for be in backends.sources("ebook"):
            try:
                for h in be.reading_history(u):
                    if not h.get("finished"):
                        continue
                    m = elib.get(h["item_id"]) or {}
                    if m.get("title"):
                        add("", m["title"], m.get("author", ""), "", h.get("last_update", 0), "ebook")
            except Exception:
                pass
    # 2) books you've rated that aren't already listed (stored key is either a
    #    real ASIN or a "t-…" slug; only the former is a usable book ASIN)
    for stored, r in rated.items():
        add(stored if stored.startswith("B0") else "", r["title"], r["author"], "", 0)
    # 3) titles you marked read in Stackarr
    for s in read_sig:
        val = s["value"] or ""
        title = s["why"].split(":", 1)[1].strip() if ":" in s["why"] else val
        add(val if val.startswith("B0") else "", title, "", "", 0)

    # optionally drop already-rated books entirely (a "rate it and it's gone"
    # workflow) — otherwise keep them, sunk to the bottom.
    hide_rated = db.get_pref(u["id"], "hide_rated_history", "0") == "1"
    if hide_rated:
        books = [b for b in books if not b["stars"]]
    # unrated float to the top (the to-do pile); rated sink to the bottom.
    # within each group, most-recent first.
    books.sort(key=lambda b: (b["stars"] > 0, -(b["when"] or 0)))
    rated_n = sum(1 for b in books if b["stars"])
    return render_template("history.html", books=books, rated_n=rated_n, hide_rated=hide_rated)


_INFER_NON_SERIES = re.compile(
    r"\b(unabridged|abridged|edition|audiobook|novel|novella|boxset|box set|collection|complete)\b", re.I)


def _infer_series(title: str):
    """Recover a (series_name, sequence) from a title when the source left the
    series field blank. Two common shapes:
      * trailing parenthetical — "Leviathan Falls (The Expanse Book 9)"
      * leading numbered prefix — "The Expanse 05 Nemesis Games"
    Returns (None, None) when nothing confident matches (so unrelated standalone
    titles like "1984" never get grouped). Conservative on purpose."""
    t = (title or "").strip()
    m = re.search(r"\(([^()]+?)(?:[, ]+Book\s+(\d+(?:\.\d+)?))?\)\s*$", t, re.I)
    if m and not _INFER_NON_SERIES.search(m.group(1)):
        name = m.group(1).strip()
        if name and not name[0].isdigit() and len(name) > 2:
            return name, (float(m.group(2)) if m.group(2) else None)
    m = re.match(r"^(.+?)\s+(\d{1,3}(?:\.\d+)?)\s+([A-Za-z].*)$", t)
    if m and len(m.group(1).strip()) > 2 and any(ch.isalpha() for ch in m.group(1)):
        return m.group(1).strip(), float(m.group(2))
    return None, None


@bp.route("/series")
@auth.login_required
def series_page():
    """Up Next: series you're collecting, how far you are, and the next book
    (with its state) — built from your library + the engine's series picks."""
    u = auth.current_user()
    # which library items has the user actually FINISHED (read), so we can show
    # reading progress separately from what's downloaded.
    finished_ids, inprogress_ids = set(), set()
    try:
        for h in absclient.listening_history(u["abs_token"]):
            if h.get("finished"):
                finished_ids.add(h["item_id"])
            elif (h.get("progress") or 0) > 0.02:
                inprogress_ids.add(h["item_id"])
    except Exception:
        pass
    if formats.show("ebook"):
        from . import backends
        for be in backends.sources("ebook"):
            try:
                for h in be.reading_history(u):
                    if h.get("finished"):
                        finished_ids.add(h["item_id"])
                    elif 0.02 < (h.get("progress") or 0) < 1:
                        inprogress_ids.add(h["item_id"])
            except Exception:
                pass
    with db.conn() as c:
        # Include books with NO series field — many audiobook rips encode the
        # series in the title ("The Expanse 05 …") and some ebook feeds drop the
        # series metadata. _infer_series recovers those so they still group.
        libr = [dict(r) for r in c.execute(
            "SELECT item_id,title,author,series,series_seq,asin,format FROM library "
            "WHERE gone_at IS NULL ORDER BY series, series_seq")]
        sugg = [dict(r) for r in c.execute(
            "SELECT id,title,author,series,asin,cover,reason FROM suggestions "
            "WHERE user_id=? AND lane='series' AND status='pending' ORDER BY score DESC", (u["id"],))]
        reqs = [dict(r) for r in c.execute(
            "SELECT title,status FROM requests WHERE user_id=?", (u["id"],))]

    def norm(s):
        return (s or "").strip().lower()

    next_by_series = {}
    for s in sugg:
        next_by_series.setdefault(norm(s["series"]), s)

    def req_status(title):
        nt = norm(title)
        for rq in reqs:
            rt = norm(rq["title"])
            if rt and nt and (rt[:30] in nt or nt[:30] in rt):
                return rq["status"]
        return None

    def _series_key(name):
        # merge "The Expanse" / "Expanse" and casing variants into one group
        return re.sub(r"^the\s+", "", (name or "").strip(), flags=re.I).lower()

    # group by effective series: the stored field, else one inferred from the
    # title. Variants collapse by normalised key; the longest seen name displays.
    groups, display_name = {}, {}
    for b in libr:
        series = (b.get("series") or "").strip()
        if not series:
            series, inferred_seq = _infer_series(b["title"])
            if not series:
                continue
            if b.get("series_seq") is None and inferred_seq is not None:
                b["series_seq"] = inferred_seq
        key = _series_key(series)
        if key not in display_name or len(series) > len(display_name[key]):
            display_name[key] = series
        groups.setdefault(key, []).append(b)
    # relabel groups from the normalised key to the chosen display name
    groups = {display_name[k]: v for k, v in groups.items()}

    def _seq_from_title(t):
        # ABS often stores the series name without a number; recover it from the
        # title, e.g. "Cradle Book 5 - Ghostwater", "Reaper: Cradle, Book 10".
        m = re.search(r"\bbook\s+(\d+(?:\.\d+)?)\b", (t or ""), re.I) or re.search(r"#\s*(\d+(?:\.\d+)?)", t or "")
        try:
            return float(m.group(1)) if m else None
        except (ValueError, TypeError):
            return None

    both = formats.multi()
    cards = []
    for name, books in groups.items():
        # effective sequence (series field, else parsed from title) + read flag
        for b in books:
            b["seq"] = b["series_seq"] if b["series_seq"] is not None else _seq_from_title(b["title"])
            b["finished"] = b["item_id"] in finished_ids
            b["reading"] = b["item_id"] in inprogress_ids
        books.sort(key=lambda b: b["seq"] if b["seq"] is not None else 0)
        seqs = [b["seq"] for b in books if b["seq"] is not None]
        read_seqs = [b["seq"] for b in books if b["seq"] is not None and b["finished"]]
        audio_seqs = {round(b["seq"], 1) for b in books
                      if b["seq"] is not None and (b.get("format") or "audiobook") == "audiobook"}
        ebook_seqs = {round(b["seq"], 1) for b in books
                      if b["seq"] is not None and b.get("format") == "ebook"}
        all_seqs = audio_seqs | ebook_seqs
        nxt = next_by_series.get(norm(name))
        fmts = {b.get("format") or "audiobook" for b in books}
        cards.append({"name": name, "owned": len(books),
                      "highest": max(seqs) if seqs else None,
                      "read_to": max(read_seqs) if read_seqs else None,
                      "read_count": sum(1 for b in books if b["finished"]),
                      "reading_now": any(b["item_id"] in inprogress_ids for b in books),
                      "books": books,
                      "format": (books[0].get("format") or "audiobook") if len(fmts) == 1 else "both",
                      "missing_audio": (both and len(all_seqs - audio_seqs) > 0),
                      "missing_ebook": (both and len(all_seqs - ebook_seqs) > 0),
                      "author": (books[0].get("author") if books else ""),
                      "next": nxt, "next_status": req_status(nxt["title"]) if nxt else None})
    cards = [x for x in cards if x["owned"] >= 2 or x["next"]]
    cards.sort(key=lambda x: (-x["owned"], x["name"].lower()))
    have_next = sum(1 for c in cards if c["next"])
    return render_template("series.html", series=cards, have_next=have_next)


@bp.route("/taste")
@auth.login_required
def taste_page():
    """See and undo everything that shapes your recommendations: ratings,
    did-not-finish, passed/ignored, already-read seeds, and removed books."""
    u = auth.current_user()
    with db.conn() as c:
        ratings = [dict(r) for r in c.execute(
            "SELECT asin,title,author,stars FROM ratings WHERE user_id=? ORDER BY stars DESC, title",
            (u["id"],))]
        sigs = [dict(r) for r in c.execute(
            "SELECT id,kind,value,weight,why FROM signals WHERE user_id=? ORDER BY id DESC", (u["id"],))]

    def labelled(s):
        why = s.get("why") or ""
        s = dict(s)
        s["label"] = why.split(":", 1)[1].strip() if ":" in why else (s.get("value") or "")
        return s

    def why_is(s, *prefixes):
        return s["kind"] == "asin" and any((s["why"] or "").startswith(p) for p in prefixes)

    passed = [labelled(s) for s in sigs if why_is(s, "passed:")]
    dnf = [labelled(s) for s in sigs if (s["why"] or "").startswith("dnf:")]
    readseed = [labelled(s) for s in sigs if why_is(s, "already read:", "marked read:")]
    removed = [labelled(s) for s in sigs if s["kind"] == "hist_hidden"]
    return render_template("taste.html", ratings=ratings, passed=passed, dnf=dnf,
                           readseed=readseed, removed=removed)


@bp.route("/book/<path:asin>")
@auth.login_required
def book_page(asin):
    # ebook ids are "gb:…"/"ol:…" and resolve via the ebook catalogue; real
    # audiobook ASINs go to Audible + Audnexus as before.
    if asin.startswith(("gb:", "ol:", "hc:")):
        from . import ebookmeta
        b = ebookmeta.by_id(asin) or {}
        b["format"] = "ebook"
        b["asin"] = asin            # keep the gb:/ol: id as the page identity for actions
    else:
        b = audible.by_asin(asin) or {}
        b.setdefault("asin", asin)
        b.setdefault("format", "audiobook")
        ax = audnexus.book(asin) or {}
        if ax.get("genres"):
            b["genres"] = ax["genres"]
        if ax.get("series") and not b.get("series"):
            b["series"], b["sequence"] = ax["series"], ax.get("sequence")
    # Catalogue lookups can transiently fail (rate-limit / timeout). Fall back to
    # what Stackarr already knows about this book so the page never shows a bare
    # "Unknown" — we usually have its title/author/cover cached from a suggestion.
    if not (b.get("title") or "").strip():
        cached = _cached_book(asin)
        b.update({k: v for k, v in cached.items() if v and not b.get(k)})
    b.setdefault("title", "Unknown")
    b["state"] = _state_for(asin, b.get("title", ""), b.get("author", ""))
    u = auth.current_user()
    key = db.rating_key(asin if not asin.startswith(("gb:", "ol:", "hc:")) else "",
                        b.get("title", ""), b.get("author", "")) if asin.startswith(("gb:", "ol:", "hc:")) else asin
    with db.conn() as c:
        req = c.execute("SELECT status, detail FROM requests WHERE asin=? AND asin<>'' ORDER BY id DESC LIMIT 1",
                        (asin,)).fetchone()
        my = c.execute("SELECT stars, review FROM ratings WHERE user_id=? AND asin=?",
                       (u["id"], key)).fetchone()
    b["req_detail"] = (req["detail"] if req else "") or ""
    try:
        tags = tagging.fetch_for(key, b.get("title", ""), b.get("author", ""), b.get("categories"))
    except Exception:
        tags = {}
    # duplicate/upgrade: which formats of this title you already own
    owned_formats = []
    if formats.multi() and b.get("title"):
        a1 = (b.get("author") or "").split(",")[0].strip().lower()
        with db.conn() as c:
            for r in c.execute("SELECT DISTINCT format FROM library WHERE gone_at IS NULL "
                               "AND lower(title)=? AND (?='' OR lower(author) LIKE ?)",
                               ((b["title"] or "").strip().lower(), a1, f"%{a1}%")):
                owned_formats.append(r["format"])
    return render_template("book.html", b=b, rate_key=key, owned_formats=owned_formats,
                           community=db.community_rating(key), reviews=db.reviews_for(key),
                           my_stars=(my["stars"] if my else 0), my_review=(my["review"] if my else ""),
                           tags=tags, shelf=db.shelf_state(u["id"], key))


@bp.route("/browse")
@auth.login_required
def browse_page():
    from . import discover
    genre = request.args.get("genre", "").strip()
    author = request.args.get("author", "").strip()
    mood = request.args.get("mood", "").strip().lower()
    # mood -> catalogue search terms (curated; deterministic, keyless)
    MOOD_TERMS = {
        "funny": "humorous comic novel", "dark": "grimdark dark fantasy",
        "romantic": "romance novel", "tense": "psychological thriller",
        "adventurous": "adventure novel", "reflective": "literary fiction",
        "epic": "epic fantasy saga", "cozy": "cozy mystery", "emotional": "emotional literary fiction",
        "whimsical": "whimsical fantasy", "mysterious": "mystery detective novel",
        "bleak": "dystopian fiction", "intense": "war thriller",
        "tender": "coming of age novel", "thought-provoking": "philosophical fiction",
        "fast-paced": "fast-paced thriller", "slow-paced": "literary slow burn novel",
    }
    if author:
        books, title, kind = audible.by_author(author, num=40), author, "author"
    elif mood:
        term = MOOD_TERMS.get(mood, mood)
        src = ebookmeta.search(term, 40) if (formats.show("ebook") and not formats.show("audiobook")) else audible.search(term, num=40)
        for b in src:
            b.setdefault("asin", b.get("id", ""))
        books, title, kind = src, mood, "mood"
    elif genre:
        books, title, kind = discover.genre_new([genre], num_per=40), genre, "genre"
    else:
        return redirect(url_for("main.discover_page"))
    seen, uniq = set(), []
    for b in books:
        if b.get("asin") and b["asin"] not in seen:
            seen.add(b["asin"])
            b["state"] = _state_for(b["asin"], b["title"], b["author"])
            uniq.append(b)
    return render_template("browse.html", books=uniq, title=title, kind=kind, author=author)


@bp.route("/api/library/refresh", methods=["POST"])
@auth.login_required
def api_library_refresh():
    """Re-scan all connected libraries (ABS/Kavita/Calibre/Komga/OPDS) so newly
    added books + series show up. Used by the 'Check library' buttons."""
    wait = _cooldown("library_refresh", 20)
    if wait:
        return jsonify({"ok": False, "detail": f"Just scanned — try again in {wait}s."}), 429
    from . import scheduler
    try:
        scheduler.refresh_library()
    except Exception as e:
        return jsonify({"ok": False, "detail": str(e)})
    with db.conn() as c:
        n = c.execute("SELECT COUNT(*) n FROM library WHERE gone_at IS NULL").fetchone()["n"]
    return jsonify({"ok": True, "detail": f"Library re-scanned — {n} books"})


@bp.route("/api/series/missing")
@auth.login_required
def api_series_missing():
    """For a series you own, fetch the full series from the catalogue and report
    which entries you're missing (the gaps), so you can complete it."""
    u = auth.current_user()
    name = (request.args.get("series") or "").strip()
    if not name:
        return jsonify({"error": "series required"}), 400
    from collections import Counter
    with db.conn() as c:
        owned = [dict(r) for r in c.execute(
            "SELECT title, series_seq, author FROM library WHERE gone_at IS NULL AND lower(series)=?",
            (name.lower(),))]
    owned_seqs = {round(o["series_seq"], 1) for o in owned if o["series_seq"] is not None}
    owned_titles = {recommend._norm(o["title"]) for o in owned}
    # The author who writes this series (the most common among the books you own).
    # Used to (a) scope a fuller catalogue fetch and (b) reject a same-named series
    # by a different author — CompleteSeries' "layered evidence" idea.
    authors = Counter((o.get("author") or "").split(",")[0].strip()
                      for o in owned if o.get("author"))
    series_author = authors.most_common(1)[0][0] if authors else ""
    nn = recommend._norm(name)

    def _author_ok(cand_author: str) -> bool:
        if not series_author or not cand_author:
            return True                      # can't disambiguate -> don't drop it
        sa, ca = series_author.lower(), cand_author.lower()
        return sa in ca or ca.split(",")[0] in sa

    # A single keyword search misses long series and entries the name search doesn't
    # surface. Pool the author's catalogue (most reliable for a series) with keyword
    # searches, then dedupe — far more complete than one 40-result query.
    pool = []
    if series_author:
        pool += audible.by_author(series_author, num=50)
    pool += audible.search(name, num=50)
    if series_author:
        pool += audible.search(f"{name} {series_author}", num=50)
    full = {}
    for b in pool:
        if recommend._norm(b.get("series")) != nn or b.get("sequence") is None:
            continue
        if not _author_ok(b.get("author", "")):
            continue
        seq = round(b["sequence"], 1)
        if seq not in full or b.get("num_ratings", 0) > full[seq].get("num_ratings", 0):
            full[seq] = b
    entries = []
    for seq in sorted(full):
        b = full[seq]
        # owned if we hold that position OR a library title matches (series_seq is
        # often null in ABS, so a seq-only check would over-report missing books).
        owned_flag = seq in owned_seqs or recommend._norm(b["title"]) in owned_titles
        entries.append({"seq": seq, "title": b["title"], "asin": b["asin"], "cover": b.get("cover", ""),
                        "author": b.get("author", ""), "owned": owned_flag})
    missing = [e for e in entries if not e["owned"]]
    return jsonify({"ok": True, "series": name, "total": len(entries),
                    "owned": len(entries) - len(missing), "missing": missing, "entries": entries})


@bp.route("/api/series/repair", methods=["POST"])
@auth.admin_required
def api_series_repair():
    """Find library books whose series field is blank but recoverable from the
    title, and (when apply=true) write the series back to Audiobookshelf so it's
    fixed everywhere — not just inferred on the Up Next display. Calibre-Web has
    no write API, so only ABS audiobook items can be repaired. Admin-only."""
    apply_now = bool((request.get_json(silent=True) or {}).get("apply"))
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT item_id,title,format FROM library WHERE gone_at IS NULL AND (series IS NULL OR series='')")]
    proposals = []
    for r in rows:
        # only ABS audiobook items have a writable, un-namespaced id
        if (r.get("format") or "audiobook") != "audiobook" or ":" in (r["item_id"] or ""):
            continue
        name, seq = _infer_series(r["title"])
        if name:
            proposals.append({"item_id": r["item_id"], "title": r["title"], "series": name, "seq": seq})
    if not apply_now:
        return jsonify({"ok": True, "count": len(proposals), "proposals": proposals[:60]})
    written = sum(1 for p in proposals if absclient.set_series(p["item_id"], p["series"], p["seq"]))
    if written:
        try:
            from . import scheduler
            scheduler.refresh_library()
        except Exception as e:
            log.debug("post-repair refresh failed: %s", e)
    return jsonify({"ok": True, "written": written, "count": len(proposals),
                    "detail": f"Repaired {written} of {len(proposals)} item(s) in Audiobookshelf."})


@bp.route("/api/series/add", methods=["POST"])
@auth.login_required
def api_series_add():
    """Bulk series acquisition is disabled during the controlled ebook migration."""
    return jsonify({
        "ok": False,
        "detail": "Whole-series acquisition is disabled. Request one specific ebook at a time."
    }), 400


def _shelves_data(u):
    """Want / Reading / Read shelves, with 'Reading' auto-populated from
    in-progress state across every connected library (ABS + ebook sources +
    Hardcover), merged with anything set manually. Returns (shelves, counts)."""
    shelves = {s: db.shelf_list(u["id"], s) for s in ("reading", "want", "read")}
    seen_titles = {(it.get("title") or "").strip().lower() for it in shelves["reading"]}
    # A book you've finished (on the 'read' shelf) must not resurface as
    # "currently reading" just because an external source still reports progress.
    # Match on the shelf's stored rkey AND the title+author slug, since the auto
    # sources key on the library asin while the read shelf often keys on the slug.
    read_keys = set()
    for r in shelves["read"]:
        if r.get("rkey"):
            read_keys.add(r["rkey"])
        if r.get("title"):
            read_keys.add(db.rating_key("", r["title"], r.get("author", "")))

    def _add_reading(title, author, cover, fmt, rkey=""):
        t = (title or "").strip()
        if not t or t.lower() in seen_titles:
            return
        if (rkey and rkey in read_keys) or db.rating_key("", t, author) in read_keys:
            return
        seen_titles.add(t.lower())
        shelves["reading"].append({"rkey": rkey or db.rating_key("", t, author), "title": t,
                                   "author": author or "", "cover": cover or "", "format": fmt, "auto": True})

    # The 'read' shelf mirrors 'reading': seed it from what you've actually
    # FINISHED across connected libraries, merged with anything shelved by hand.
    # Without this the Home "Read" shelf sat at 0 while the goal ring (which reads
    # finish dates) showed the right count — finished audiobooks never landed here.
    read_titles = {(it.get("title") or "").strip().lower() for it in shelves["read"]}

    def _add_read(title, author, cover, fmt, rkey=""):
        t = (title or "").strip()
        if not t or t.lower() in read_titles:
            return
        read_titles.add(t.lower())
        shelves["read"].append({"rkey": rkey or db.rating_key("", t, author), "title": t,
                                "author": author or "", "cover": cover or "", "format": fmt, "auto": True})
    try:
        with db.conn() as c:
            lib = {r["item_id"]: dict(r) for r in c.execute("SELECT item_id,title,author,asin,format FROM library")}
        for h in absclient.listening_history(u["abs_token"]):
            m = lib.get(h["item_id"]) or {}
            if not m.get("title"):
                continue
            # pass the real ASIN as the key so the card links to the book page
            rk = (m.get("asin") or "").strip()
            if h.get("finished"):
                _add_read(m["title"], m.get("author", ""), url_for("main.cover", item_id=h["item_id"]),
                          "audiobook", rkey=rk)
            elif (h.get("progress") or 0) > 0.02:
                _add_reading(m["title"], m.get("author", ""), url_for("main.cover", item_id=h["item_id"]),
                             "audiobook", rkey=rk)
        if formats.show("ebook"):
            from . import backends
            for be in backends.sources("ebook"):
                try:
                    for h in be.reading_history(u):
                        m = lib.get(h["item_id"]) or {}
                        if not m.get("title"):
                            continue
                        if h.get("finished"):
                            _add_read(m["title"], m.get("author", ""), "", "ebook")
                        elif 0.02 < (h.get("progress") or 0) < 1:
                            _add_reading(m["title"], m.get("author", ""), "", "ebook")
                except Exception:
                    pass
        for it in ebookmeta.hardcover_reading(ebookmeta.hardcover_token()):
            _add_reading(it.get("title", ""), it.get("author", ""), "", "ebook")
    except Exception as e:
        log.debug("shelves auto-reading failed: %s", e)
    counts = dict(db.shelf_counts(u["id"]))
    counts["reading"] = len(shelves["reading"])
    counts["read"] = len(shelves["read"])
    return shelves, counts


@bp.route("/shelves")
@auth.login_required
def shelves_page():
    # Shelves are now part of the Home hub — keep the URL working for bookmarks.
    return redirect(url_for("main.home_page"))


@bp.route("/api/adventurousness", methods=["POST"])
@auth.login_required
def api_adventurousness():
    """Comfort (0) ↔ Discovery (100) dial — biases familiar vs new-author lanes."""
    u = auth.current_user()
    try:
        n = max(0, min(100, int(request.get_json(force=True).get("value", 50))))
    except (ValueError, TypeError):
        n = 50
    db.set_meta(f"adventurousness_{u['id']}", str(n))
    return jsonify({"ok": True, "value": n})


@bp.route("/api/vibes", methods=["POST"])
@auth.login_required
def api_vibes():
    """Mood-aware cold start: the user picks a few vibes; we store them as
    positive mood signals so even a brand-new account gets shaped picks."""
    u = auth.current_user()
    moods = request.get_json(force=True).get("moods") or []
    with db.conn() as c:
        for m in moods[:8]:
            m = str(m).strip().lower()
            if m:
                c.execute("INSERT INTO signals (user_id,kind,value,weight,why) VALUES (?,?,?,?,?) "
                          "ON CONFLICT(user_id,kind,value) DO UPDATE SET weight=signals.weight+3",
                          (u["id"], "mood", m, 3, "vibe pick"))
        c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why) VALUES (?,?,?,?,?)",
                  (u["id"], "vibes_done", "1", 0, "picked vibes"))
    return jsonify({"ok": True})


@bp.route("/api/account/password", methods=["POST"])
@auth.login_required
def api_account_password():
    u = auth.current_user()
    b = request.get_json(force=True)
    new = (b.get("password") or "").strip()
    if len(new) < 6:
        return jsonify({"error": "Password must be at least 6 characters."}), 400
    # if a password is already set, require the current one
    if u.get("password_hash") and not db.verify_local(u["username"], b.get("current") or ""):
        return jsonify({"error": "Current password is wrong."}), 403
    db.set_password(u["id"], new)
    return jsonify({"ok": True})


@bp.route("/api/account/email", methods=["POST"])
@auth.login_required
def api_account_email():
    u = auth.current_user()
    db.set_email(u["id"], (request.get_json(force=True).get("email") or "").strip())
    return jsonify({"ok": True})


@bp.route("/api/account/link", methods=["POST"])
@auth.login_required
def api_account_link():
    """Link an external sign-in method to the current account."""
    u = auth.current_user()
    b = request.get_json(force=True)
    ok = auth.link_provider(u, (b.get("provider") or "").strip(),
                            b.get("username", ""), b.get("password", ""))
    if not ok:
        return jsonify({"error": "Couldn't verify those credentials, or that account is already linked to someone else."}), 400
    return jsonify({"ok": True})


@bp.route("/api/account/unlink", methods=["POST"])
@auth.login_required
def api_account_unlink():
    u = auth.current_user()
    provider = (request.get_json(force=True).get("provider") or "").strip()
    # don't let someone strip their ONLY way back in
    links = db.links_for(u["id"])
    if not u.get("password_hash") and len(links) <= 1:
        return jsonify({"error": "Set a password first — this is your only way to sign in."}), 400
    db.link_remove(provider, u["id"])
    if provider == "abs":
        db.update_abs(u["id"], None, "")
    return jsonify({"ok": True})


@bp.route("/api/goal", methods=["POST"])
@auth.login_required
def api_goal():
    u = auth.current_user()
    try:
        n = max(0, int(request.get_json(force=True).get("goal", 0)))
    except (ValueError, TypeError):
        n = 0
    db.set_meta(f"goal_{u['id']}", str(n))
    return jsonify({"ok": True, "goal": n})


@bp.route("/upcoming")
@auth.login_required
def upcoming_page():
    """New & upcoming books from authors you read — a browsable radar."""
    u = auth.current_user()
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM suggestions WHERE user_id=? AND lane='upcoming' AND status='pending' ORDER BY extra", (u["id"],))]
        for r in rows:
            r["available"] = _owned(c, r["asin"], r["title"], r["author"])
    import datetime
    today = str(datetime.date.today())
    return render_template("upcoming.html", rows=rows, today=today)


@bp.route("/author/<path:name>")
@auth.login_required
def author_page(name):
    """An author hub: their catalogue, what you own, and a Follow toggle that
    drives the new-release radar."""
    name = name.strip()
    books = audible.by_author(name, num=40)
    if formats.show("ebook") and not formats.show("audiobook"):
        books = ebookmeta.by_author(name, num=40)
        for b in books:
            b["asin"] = b.get("id", "")
    seen, uniq = set(), []
    for b in books:
        k = (b.get("title") or "").lower()
        if b.get("asin") and k not in seen:
            seen.add(k)
            b["state"] = _state_for(b["asin"], b["title"], b["author"])
            uniq.append(b)
    uniq.sort(key=lambda b: b.get("release_date") or "", reverse=True)
    u = auth.current_user()
    following = bool(db.get_meta(f"follow_{u['id']}_{name.lower()}"))
    return render_template("author.html", author=name, books=uniq, following=following)


@bp.route("/api/follow", methods=["POST"])
@auth.login_required
def api_follow():
    u = auth.current_user()
    b = request.get_json(force=True)
    name = (b.get("author") or "").strip()
    if not name:
        return jsonify({"error": "author required"}), 400
    key = f"follow_{u['id']}_{name.lower()}"
    now = bool(db.get_meta(key))
    db.set_meta(key, "" if now else "1")
    # a follow is a positive author signal too (and seeds the radar)
    if not now:
        with db.conn() as c:
            c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why) VALUES (?,?,?,?,?)",
                      (u["id"], "author", name, 4, f"followed: {name}"))
    return jsonify({"ok": True, "following": not now})


@bp.route("/narrator/<path:name>")
@auth.login_required
def narrator_page(name):
    """A narrator hub: audiobooks they narrate + what you own."""
    name = name.strip()
    seen, uniq = set(), []
    for b in audible.search(name, num=40):
        if name.lower() in (b.get("narrator") or "").lower() and b.get("asin") and b["asin"] not in seen:
            seen.add(b["asin"])
            b["state"] = _state_for(b["asin"], b["title"], b["author"])
            uniq.append(b)
    return render_template("narrator.html", narrator=name, books=uniq)


@bp.route("/api/author/add", methods=["POST"])
@auth.login_required
def api_author_add():
    """Bulk author acquisition is disabled during the controlled ebook migration."""
    return jsonify({
        "ok": False,
        "detail": "All-books-by-author acquisition is disabled. Request one specific ebook at a time."
    }), 400


@bp.route("/settings")
@auth.login_required
def settings_page():
    g = db.setting
    is_admin = auth.current_user()["role"] == "admin"
    # Instance-wide integration secrets (service creds, SMTP password, webhook
    # URLs). These only ever render inside `{% if is_admin %}` blocks, but we ALSO
    # withhold the values server-side so a single future ungated field can't dump
    # the ABS admin token / SMTP password to a non-admin who curls /settings.
    conn = {"abs_url": g("abs_url", config.ABS_URL),
            "abs_admin_token": g("abs_admin_token", config.ABS_ADMIN_TOKEN),
            "chaptarr_url": g("chaptarr_url", config.CHAPTARR_URL),
            "chaptarr_api_key": g("chaptarr_api_key", config.CHAPTARR_API_KEY),
            "chaptarr_root_folder": g("chaptarr_root_folder", config.CHAPTARR_ROOT_FOLDER),
            "chaptarr_quality_profile_id": g("chaptarr_quality_profile_id", str(config.CHAPTARR_QUALITY_PROFILE_ID)),
            "chaptarr_metadata_profile_id": g("chaptarr_metadata_profile_id", str(config.CHAPTARR_METADATA_PROFILE_ID)),
            "chaptarr_webhook_token": db.get_meta("chaptarr_webhook_token") or _ensure_webhook_token(),
            "public_url": db.get_meta("public_url", ""),
            "kavita_url": g("kavita_url", config.KAVITA_URL),
            "kavita_api_key": g("kavita_api_key", config.KAVITA_API_KEY),
            "calibreweb_url": g("calibreweb_url", config.CALIBREWEB_URL),
            "calibreweb_user": g("calibreweb_user", config.CALIBREWEB_USER),
            "calibreweb_pass": g("calibreweb_pass", config.CALIBREWEB_PASS),
            "komga_url": g("komga_url", config.KOMGA_URL),
            "komga_user": g("komga_user", config.KOMGA_USER),
            "komga_pass": g("komga_pass", config.KOMGA_PASS),
            "opds_url": g("opds_url", config.OPDS_URL),
            "opds_user": g("opds_user", config.OPDS_USER),
            "opds_pass": g("opds_pass", config.OPDS_PASS)} if is_admin else {}
    return render_template("settings.html",
                           email_configured=notify.email_configured(),
                           email_enabled=db.get_meta("email_enabled", "0") == "1",
                           email_theme=notify.current_theme(),
                           email_frequency=db.get_meta("email_frequency", "immediate"),
                           discord_configured=bool(db.setting("discord_webhook", config.DISCORD_WEBHOOK)),
                           discord_webhook=db.setting("discord_webhook", config.DISCORD_WEBHOOK) if is_admin else "",
                           discord_enabled=db.get_meta("discord_enabled", "0") == "1",
                           notify_avail_enabled=db.get_meta("notify_avail_enabled", "0") == "1",
                           notify_newrelease_enabled=db.get_meta("notify_newrelease_enabled", "0") == "1",
                           custom_webhook=db.setting("custom_webhook", "") if is_admin else "",
                           themes=list(notify.THEMES),
                           auto_add_level=db.get_meta("auto_add_level", "off"),
                           interval_hours=db.get_meta("suggest_interval_hours", str(config.SUGGEST_INTERVAL_HOURS)),
                           language=db.get_meta("language", config.TARGET_LANGUAGE),
                           languages=["english","german","spanish","french","italian","dutch","portuguese","japanese","any"],
                           smtp=notify.smtp_settings() if is_admin else {},
                           conn=conn,
                           abs_ebooks=db.get_meta("abs_ebooks", "1" if config.ABS_EBOOKS else "0") == "1",
                           koreader_sync=db.get_meta("koreader_sync", "1" if config.KOREADER_SYNC else "0") == "1",
                           reading={"goodreads_rss": g("goodreads_rss", config.GOODREADS_RSS) if is_admin else "",
                                    "hardcover_token": g("hardcover_token", config.HARDCOVER_TOKEN) if is_admin else ""},
                           hide_rated_history=db.get_pref(auth.current_user()["id"], "hide_rated_history", "0") == "1",
                           format_mode=formats.mode(),
                           cross_format_taste=db.get_pref(auth.current_user()["id"], "cross_format_taste", "0") == "1",
                           adventurousness=db.get_meta(f"adventurousness_{auth.current_user()['id']}", str(config.ADVENTUROUSNESS)),
                           log_level=config.LOG_LEVEL,
                           account=_account_ctx(auth.current_user()),
                           require_approval=db.get_meta("require_approval", "1") == "1",
                           user_sync=db.get_meta("user_sync", "0") == "1",
                           manage_users=_manage_users_ctx() if auth.current_user()["role"] == "admin" else [],
                           notify_prefs=_notify_prefs_ctx(auth.current_user()),
                           smtp_ready=notify.smtp_ready(),
                           is_admin=is_admin)


def _manage_users_ctx() -> list[dict]:
    """User list for the admin Users panel, with each user's trusted flag."""
    out = []
    for u in db.all_users():
        out.append({**u, "trusted": db.get_pref(u["id"], "trusted", "0") == "1"})
    return out


def _notify_prefs_ctx(u: dict) -> dict:
    return {k: db.get_pref(u["id"], f"notify_{k}", "1") == "1"
            for k in ("approved", "available", "denied")}


def _account_ctx(u: dict) -> dict:
    """Identity + linked sign-in methods for the Settings → Account section."""
    linked = {l["provider"] for l in db.links_for(u["id"])}
    provs = []
    for p in auth.login_providers():           # connected sources that can authenticate
        provs.append({"id": p["id"], "label": p["label"], "linked": p["id"] in linked})
    return {"username": u["username"], "email": u.get("email") or "",
            "role": u["role"], "has_password": bool(u.get("password_hash")),
            "providers": provs}


# ------------------------------------------------------------------- api ---
def _library_title_match(request_title, library_title) -> bool:
    """Conservative title match, allowing an edition suffix such as
    'The War of the Worlds (Illustrated)'."""
    import re
    req = re.sub(r"\s+", " ", (request_title or "").strip().lower())
    lib = re.sub(r"\s+", " ", (library_title or "").strip().lower())
    if len(req) < 4 or not lib:
        return False
    if req == lib:
        return True

    # Edition suffixes, e.g. "The Hobbit (Illustrated)".
    if lib.startswith(req + " (") or lib.startswith(req + " ["):
        return True

    # Audiobookshelf commonly prefixes a series/book label, e.g.
    # "Arcane Ascension, Book 1 - Sufficiently Advanced Magic".
    # _owned() separately requires the correct author and format.
    for sep in (" - ", " – ", " — "):
        if lib.endswith(sep + req):
            return True

    return False


def _library_author_match(request_author, library_author) -> bool:
    """Compare author names while ignoring punctuation/spaces in initials."""
    import re
    req = re.sub(r"[^a-z0-9]", "", (request_author or "").split(",")[0].lower())
    lib = re.sub(r"[^a-z0-9]", "", (library_author or "").split(",")[0].lower())
    return bool(req and lib and (req in lib or lib in req))


def _owned(c, asin, title, author, fmt=None) -> bool:
    """True only if the requested format is genuinely present.

    Prefer ASIN. Otherwise require a conservative title match plus author.
    Kavita currently reports blank authors in its series feed, so for Kavita
    only we permit the conservative title match when the library author is blank.
    """
    fclause = " AND format=?" if fmt else ""
    fargs = (fmt,) if fmt else ()

    if asin and c.execute(
        "SELECT 1 FROM library WHERE gone_at IS NULL AND asin=? AND asin<>''" + fclause,
        (asin,) + fargs
    ).fetchone():
        return True

    rows = c.execute(
        "SELECT title,author,source FROM library WHERE gone_at IS NULL" + fclause,
        fargs
    ).fetchall()

    for row in rows:
        if not _library_title_match(title, row["title"]):
            continue
        if _library_author_match(author, row["author"]):
            return True
        if (row["source"] or "").lower() == "kavita" and not (row["author"] or "").strip():
            return True

    return False


def _ensure_webhook_token() -> str:
    """A stable per-install token for the Chaptarr Connect webhook URL."""
    import secrets
    t = db.get_meta("chaptarr_webhook_token", "")
    if not t:
        t = secrets.token_urlsafe(16)
        db.set_meta("chaptarr_webhook_token", t)
    return t


def _req_format(body) -> str:
    """The media format an action applies to: the explicit choice when both
    formats are active, else the single active format (assumed)."""
    f = (body or {}).get("format")
    if formats.multi() and f in ("audiobook", "ebook"):
        return f
    return formats.primary()


def _cached_book(asin) -> dict:
    """Best-effort title/author/cover for a book Stackarr has seen, from the
    suggestions / requests / library tables — the fallback when a live catalogue
    lookup fails so the detail page degrades gracefully instead of 'Unknown'."""
    if not asin:
        return {}
    with db.conn() as c:
        # rating-key slug ("t-…") rows live keyed on rkey/asin in shelf + ratings,
        # so a 'currently reading' / rated book still resolves to a real page.
        for q in ("SELECT title, author, cover FROM suggestions WHERE asin=? ORDER BY id DESC LIMIT 1",
                  "SELECT title, author, cover FROM requests WHERE asin=? ORDER BY id DESC LIMIT 1",
                  "SELECT title, author, cover FROM shelf WHERE rkey=? LIMIT 1"):
            row = c.execute(q, (asin,)).fetchone()
            if row and (row["title"] or "").strip():
                return {"title": row["title"], "author": row["author"], "cover": row["cover"]}
        row = c.execute("SELECT title, author FROM ratings WHERE asin=? AND title<>'' LIMIT 1",
                        (asin,)).fetchone()
        if row:
            return {"title": row["title"], "author": row["author"]}
        row = c.execute("SELECT title, author FROM library WHERE asin=? AND asin<>'' LIMIT 1",
                        (asin,)).fetchone()
        if row:
            return {"title": row["title"], "author": row["author"]}
    return {}


def _state_for(asin, title, author, fmt=None):
    with db.conn() as c:
        owned = _owned(c, asin, title, author, fmt=fmt)

        if fmt:
            req = c.execute(
                "SELECT status FROM requests "
                "WHERE asin=? AND asin<>'' AND format=? "
                "ORDER BY id DESC LIMIT 1",
                (asin, fmt),
            ).fetchone()
        else:
            req = c.execute(
                "SELECT status FROM requests "
                "WHERE asin=? AND asin<>'' "
                "ORDER BY id DESC LIMIT 1",
                (asin,),
            ).fetchone()

    return "available" if owned else (req["status"] if req else "none")


@bp.route("/api/discover")
@auth.login_required
def api_discover():
    try:
        pg = int(request.args.get("page", "0") or 0)
    except (ValueError, TypeError):
        pg = 0
    books = discover.page(pg)
    for b in books:
        b["state"] = _state_for(
            b["asin"],
            b["title"],
            b["author"],
            fmt=b.get("format"),
        )
    return jsonify(books)


def _search_catalog(q, num):
    """Search the catalogue of the active format(s).

    In both mode, return an interleaved audiobook + ebook result set.
    """
    from . import ebookmeta

    active = formats.active()

    ebooks = []
    if "ebook" in active:
        for x in ebookmeta.search(q, num=num):
            b = dict(x, asin=x.get("id", ""), format="ebook")
            if b["asin"]:
                ebooks.append(b)

    audio = []
    if "audiobook" in active:
        for x in audible.search(q, num=num):
            if not x.get("asin"):
                continue
            b = dict(x)
            b["format"] = "audiobook"
            audio.append(b)

    if active == ["ebook"]:
        return ebooks[:num]

    if active == ["audiobook"]:
        return audio[:num]

    out = []
    total = max(len(audio), len(ebooks))

    for i in range(total):
        if i < len(audio):
            out.append(audio[i])
        if i < len(ebooks):
            out.append(ebooks[i])
        if len(out) >= num:
            break

    return out[:num]


@bp.route("/api/suggest")
@auth.login_required
def api_suggest():
    """Typeahead for the top search box — titles/authors/series as you type."""
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    return jsonify([{"asin": x["asin"], "title": x["title"], "author": x["author"],
                     "series": x.get("series", ""), "cover": x["cover"]}
                    for x in _search_catalog(q, 7)])


@bp.route("/api/ignore", methods=["POST"])
@auth.login_required
def api_ignore():
    u = auth.current_user()
    body = request.get_json(force=True)
    asin = body.get("asin", "")
    with db.conn() as c:
        if asin:
            c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why) VALUES (?,?,?,?,?)",
                      (u["id"], "asin", asin, -3, f"ignored: {body.get('title','')}"))
            c.execute("UPDATE suggestions SET status='rejected' WHERE user_id=? AND asin=? AND status='pending'",
                      (u["id"], asin))
    return jsonify({"ok": True})


@bp.route("/api/markread-book", methods=["POST"])
@auth.login_required
def api_markread_book():
    """Mark-as-read by asin from the detail page: positive seed + drop from queue."""
    u = auth.current_user()
    body = request.get_json(force=True)
    asin, title, author = body.get("asin", ""), body.get("title", ""), body.get("author", "")
    with db.conn() as c:
        if author:
            c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why) VALUES (?,?,?,?,?)",
                      (u["id"], "author", author.split(",")[0], 3, f"already read: {title}"))
        if asin:
            c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why) VALUES (?,?,?,?,?)",
                      (u["id"], "asin", asin, -1, f"already read: {title}"))
            c.execute("UPDATE suggestions SET status='rejected' WHERE user_id=? AND asin=? AND status='pending'",
                      (u["id"], asin))
    # Propagate: mark finished in ABS (if in this user's library) + unmonitor in Chaptarr.
    abs_finished, chaptarr_unmonitored = False, False
    tok = u.get("abs_token")
    if tok and title:
        try:
            item_id = absclient.find_item(title, author)
            if item_id:
                abs_finished = absclient.set_finished(tok, item_id, True)
        except Exception:
            pass
    try:
        chaptarr_unmonitored = chaptarr.mark_read(title, author)
    except Exception:
        pass
    return jsonify({"ok": True, "matched": title,
                    "abs_finished": abs_finished, "chaptarr_unmonitored": chaptarr_unmonitored})


@bp.route("/api/search")
@auth.login_required
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    books = _search_catalog(q, 12)
    for b in books:
        b["state"] = _state_for(
            b["asin"],
            b["title"],
            b["author"],
            fmt=b.get("format"),
        )
    return jsonify(books)


def _needs_approval(user) -> bool:
    """Should this user's grab wait for an admin? Admins self-approve; a global
    'manual approval' switch (default on) requires it for everyone else, unless
    that user is marked trusted."""
    if not user or user.get("role") == "admin":
        return False
    if db.get_meta("require_approval", "1") != "1":
        return False
    return db.get_pref(user["id"], "trusted", "0") != "1"


def _base_url() -> str:
    try:
        return db.get_meta("public_url", "") or request.host_url.rstrip("/")
    except Exception:
        return db.get_meta("public_url", "")


# Lightweight in-process cooldown for the expensive endpoints (library re-scan,
# recommender run). Keyed per user so one account can't hammer the shared
# ABS/Chaptarr scans; in-memory is fine on single-process waitress.
_LAST_HEAVY: dict = {}


def _cooldown(action: str, seconds: int) -> int:
    """0 if `action` is allowed now (and stamps it), else the seconds to wait."""
    import time
    u = auth.current_user()
    key = (u["id"] if u else request.remote_addr, action)
    now, last = time.time(), _LAST_HEAVY.get((u["id"] if u else request.remote_addr, action), 0)
    if now - last < seconds:
        return int(seconds - (now - last)) + 1
    _LAST_HEAVY[key] = now
    return 0


def _queue_for_approval(user, item, source):
    """Record a request that needs an admin's approval and ping the admins.
    `item` has title/author and optionally asin/cover/format. Used by both
    single-book grabs and the bulk series/author 'add all' paths."""
    with db.conn() as c:
        c.execute("INSERT INTO requests (user_id,asin,title,author,cover,status,detail,source,format) "
                  "VALUES (?,?,?,?,?,?,?,?,?)",
                  (user["id"], item.get("asin", ""), item["title"], item.get("author", ""),
                   item.get("cover", ""), "pending_approval", "Waiting for an admin to approve",
                   source, item.get("format", "audiobook")))
    try:
        notify.request_pending(item, user.get("username", "A user"), db.admin_emails(), _base_url())
    except Exception as e:
        log.debug("pending-approval notify failed: %s", e)
    return {"ok": True, "pending": True, "detail": "Request sent — an admin will approve it shortly."}


def _acquisition_handoff(
    title,
    author,
    asin="",
    fmt="audiobook",
    excluded_refs=None,
):
    """Single acquisition dispatcher for the controlled migration.

    Ebooks go to the separate ebook Shelfmark.
    Audiobooks through Stackarr remain disabled until the ebook route is proven.
    """
    fmt = (fmt or "").strip().lower()

    if fmt == "ebook":
        return shelfmark.add_and_search(
            title,
            author,
            asin,
            fmt="ebook",
            excluded_refs=excluded_refs,
        )

    if fmt == "audiobook":
        return audiobridge.add_and_search(
            title,
            author,
            asin,
        )

    return {
        "ok": False,
        "detail": f"Unsupported acquisition format: {fmt or 'unknown'}",
    }


def _hand_off_request(user_id, book, source):
    fmt = (book.get("format") or "audiobook").strip().lower()
    user = db.get_user(user_id)

    # Hold non-admin, non-trusted requests for approval instead of grabbing.
    if _needs_approval(user):
        return _queue_for_approval(user, book, source)

    # Do not search again when this exact ebook has already been successfully
    # handed off, queued, or marked available anywhere in Stackarr.
    def _request_key(value):
        return "".join(
            ch for ch in (value or "").casefold()
            if ch.isalnum()
        )

    wanted_title = _request_key(book.get("title", ""))
    wanted_author = _request_key(book.get("author", ""))

    with db.conn() as c:
        previous = c.execute(
            "SELECT user_id,title,author,status "
            "FROM requests "
            "WHERE format=? AND status IN ('handed','queued','available')",
            (fmt,),
        ).fetchall()

        for row in previous:
            if _request_key(row["title"]) != wanted_title:
                continue

            stored_author = _request_key(row["author"])
            if wanted_author and stored_author and stored_author != wanted_author:
                continue

            return {
                "ok": True,
                "detail": "Already requested — no new search performed.",
            }

    # Bypass Chaptarr if the book is already in a connected library (the user may
    # have added it straight to Audiobookshelf / Kavita / Calibre-Web). Record it
    # as available rather than redundantly asking Chaptarr to grab it.
    with db.conn() as c:
        if _owned(c, book.get("asin", ""), book.get("title", ""), book.get("author", ""), fmt=fmt):
            c.execute("INSERT INTO requests (user_id,asin,title,author,cover,status,detail,source,format) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      (user_id, book.get("asin", ""), book["title"], book.get("author", ""),
                       book.get("cover", ""), "available", "Already in your library", source, fmt))
            return {"ok": True, "detail": "Already in your library — marked available."}
    res = _acquisition_handoff(
        book["title"],
        book.get("author", ""),
        book.get("asin", ""),
        fmt=fmt,
    )
    status = "handed" if res["ok"] else "failed"
    ref = str(res.get("ref") or "").strip()
    attempted_refs = [ref] if fmt == "ebook" and ref else []

    with db.conn() as c:
        c.execute(
            "INSERT INTO requests "
            "(user_id,asin,title,author,cover,status,detail,chaptarr_ref,attempted_refs,source,format) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                user_id,
                book.get("asin", ""),
                book["title"],
                book.get("author", ""),
                book.get("cover", ""),
                status,
                res.get("detail", ""),
                ref,
                json.dumps(attempted_refs),
                source,
                fmt,
            ),
        )
    return res


@bp.route("/api/request", methods=["POST"])
@auth.login_required
def api_request():
    u = auth.current_user()
    book = request.get_json(force=True)
    if not book.get("title"):
        return jsonify({"error": "title required"}), 400
    return jsonify(_hand_off_request(u["id"], book, "manual"))


@bp.route("/api/suggestion/<int:sid>/<verdict>", methods=["POST"])
@auth.login_required
def api_suggestion(sid, verdict):
    u = auth.current_user()
    if verdict not in ("approve", "reject", "read"):
        return jsonify({"error": "bad verdict"}), 400
    with db.conn() as c:
        row = c.execute("SELECT * FROM suggestions WHERE id=? AND user_id=?", (sid, u["id"])).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        row = dict(row)
        # a suggestion can have no ASIN (ebook/inferred picks) — fall back to the
        # title/author key so the negative signal is never silently dropped (the
        # signals.value column is NOT NULL) and the title sticks as "don't show".
        sig_val = row.get("asin") or db.rating_key("", row.get("title", ""), row.get("author", ""))
        # 'approve' is NOT committed here — we hand off first (below) and only
        # consume the suggestion on success, so a failed grab leaves it re-approvable.
        if verdict in ("reject", "read"):
            c.execute("UPDATE suggestions SET status='rejected',decided_at=datetime('now','localtime') WHERE id=?",
                      (sid,))
        if verdict == "reject":
            c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why) VALUES (?,?,?,?,?)",
                      (u["id"], "asin", sig_val, -3, f"passed: {row['title']}"))
        elif verdict == "read":
            # treat like the manual 'already read' seed: positive author/series,
            # never re-suggest this title, and drop it from the queue.
            if row.get("author"):
                c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why) VALUES (?,?,?,?,?)",
                          (u["id"], "author", row["author"].split(",")[0], 3, f"already read: {row['title']}"))
            if row.get("series"):
                c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why) VALUES (?,?,?,?,?)",
                          (u["id"], "series", row["series"], 2, f"already read: {row['title']}"))
            c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why) VALUES (?,?,?,?,?)",
                      (u["id"], "asin", sig_val, -1, f"already read: {row['title']}"))
    if verdict == "approve":
        res = _hand_off_request(u["id"], row, "suggestion")
        # consume the suggestion ONLY on a real handoff — not when it was merely
        # queued for admin approval (ok:True, pending:True); else a later denial
        # leaves the pick 'approved' and it silently vanishes from the user's lane.
        if res.get("ok") and not res.get("pending"):
            with db.conn() as c:
                c.execute("UPDATE suggestions SET status='approved',decided_at=datetime('now','localtime') WHERE id=?",
                          (sid,))
        return jsonify(res)
    return jsonify({"status": "rejected"})


@bp.route("/api/mark-read", methods=["POST"])
@auth.login_required
def api_mark_read():
    """Manually tell Stackarr you've already read a title — positive taste
    seed, no download. Also writes finished to ABS (if it's in this user's
    library) and unmonitors it in Chaptarr so it won't be grabbed."""
    u = auth.current_user()
    body = request.get_json(force=True)
    title, authr = body.get("title", "").strip(), body.get("author", "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    fmt = _req_format(body)
    hit = ebookmeta.search(f"{title} {authr}", num=1) if fmt == "ebook" else audible.search(f"{title} {authr}", num=1)
    b = (hit[0] if hit else {"asin": "", "title": title, "author": authr})
    bid = b.get("asin") or b.get("id") or ""
    with db.conn() as c:
        if b.get("author"):
            c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why,format) VALUES (?,?,?,?,?,?)",
                      (u["id"], "author", b["author"].split(",")[0], 3, f"marked read: {b['title']}", fmt))
        if b.get("series"):
            c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why,format) VALUES (?,?,?,?,?,?)",
                      (u["id"], "series", b["series"], 2, f"marked read: {b['title']}", fmt))
        c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why,format) VALUES (?,?,?,?,?,?)",
                  (u["id"], "asin", bid or title, -1, f"already read: {b['title']}", fmt))
    # also put it on the 'read' shelf so it counts toward goal + heatmap
    rk = db.rating_key(bid if not bid.startswith(("gb:", "ol:", "hc:")) else "", b.get("title", title), b.get("author", authr))
    # prefer the on-screen cover the client sent over the (often coverless) re-search
    # hit, so the read shelf stores a real cover instead of needing a /coverart lookup.
    cover = b.get("cover") or body.get("cover", "")
    db.shelf_set(u["id"], rk, "read", b.get("title", title), b.get("author", authr), cover, fmt)
    # Propagate outward: mark finished in ABS (if it's in this user's library) and
    # unmonitor it in Chaptarr (don't grab what you've already read).
    abs_finished, chaptarr_unmonitored = False, False
    mt, ma = b.get("title", title), b.get("author", authr)
    tok = u.get("abs_token")
    if tok:
        try:
            item_id = absclient.find_item(mt, ma)
            if item_id:
                abs_finished = absclient.set_finished(tok, item_id, True)
        except Exception:
            pass
    try:
        chaptarr_unmonitored = chaptarr.mark_read(mt, ma)
    except Exception:
        pass
    return jsonify({"ok": True, "matched": mt,
                    "abs_finished": abs_finished, "chaptarr_unmonitored": chaptarr_unmonitored})


@bp.route("/api/rate", methods=["POST"])
@auth.login_required
def api_rate():
    """Rate a read book 1-5 stars to sharpen suggestions."""
    u = auth.current_user()
    body = request.get_json(force=True)
    asin = (body.get("asin") or "").strip()
    try:
        stars = int(body.get("stars", 0))
    except (TypeError, ValueError):
        stars = 0
    if not 1 <= stars <= 5:
        return jsonify({"error": "stars 1-5 required"}), 400
    # The client sends title/author for library books (which usually have no
    # real ASIN); only look them up on Audible for genuine ASINs. The author is
    # what the recommender boosts on, so we must capture it either way.
    title, author = (body.get("title") or "").strip(), (body.get("author") or "").strip()
    review = (body.get("review") or "").strip()[:1500]
    spoiler = 1 if body.get("spoiler") else 0
    fmt = body.get("format") or ("ebook" if asin.startswith(("gb:", "ol:", "hc:")) else "audiobook")
    if (not title or not author) and asin and not asin.startswith(("t-", "gb:", "ol:", "hc:")):
        meta = audible.by_asin(asin) or {}
        title = title or meta.get("title", "")
        author = author or meta.get("author", "")
    # Canonical rating key so writes and reads agree. When the client sends a
    # "t-…" slug it is ALREADY the exact key the read paths (History, onboarding,
    # shelves) rendered from — trust it verbatim. Recomputing the slug here from
    # title+author could produce a DIFFERENT key (e.g. an older row keyed without
    # the author, or a title that normalises differently), so the write landed
    # under a key History never reads back: it looked saved but the stars never
    # stuck. For real ASINs the key is the ASIN; ebook ids (gb:/ol:) fall through
    # to the title+author slug the ebook read paths use.
    if asin.startswith("t-"):
        key = asin
    else:
        real_asin = asin if (asin and not asin.startswith(("gb:", "ol:", "hc:"))) else ""
        key = db.rating_key(real_asin, title, author)
    if not key or key == "t-":
        return jsonify({"error": "need an asin or title + author"}), 400
    with db.conn() as c:
        # a blank review on an update must not wipe an existing one
        c.execute("INSERT INTO ratings (user_id,asin,title,author,stars,review,spoiler,format,updated_at) "
                  "VALUES (?,?,?,?,?,?,?,?,datetime('now','localtime')) "
                  "ON CONFLICT(user_id,asin) DO UPDATE SET "
                  "stars=excluded.stars, title=excluded.title, author=excluded.author, "
                  "review=CASE WHEN excluded.review<>'' THEN excluded.review ELSE ratings.review END, "
                  "spoiler=excluded.spoiler, format=excluded.format, updated_at=datetime('now','localtime')",
                  (u["id"], key, title, author, stars, review, spoiler, fmt))
    return jsonify({"ok": True, "community": db.community_rating(key)})


@bp.route("/api/history/remove", methods=["POST"])
@auth.login_required
def api_history_remove():
    """Remove a book from History & ratings for good. Drops any rating and
    records a 'hist_hidden' marker keyed on the rating key, so the book stays
    gone even though it's still finished in Audiobookshelf."""
    u = auth.current_user()
    body = request.get_json(force=True)
    key = (body.get("key") or "").strip()
    title = (body.get("title") or "").strip()
    if not key:
        return jsonify({"error": "key required"}), 400
    why = f"removed: {title}" if title else "removed from history"
    with db.conn() as c:
        c.execute("DELETE FROM ratings WHERE user_id=? AND asin=?", (u["id"], key))
        c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why) "
                  "VALUES (?,?,?,?,?)", (u["id"], "hist_hidden", key, 0, why))
    return jsonify({"ok": True})


@bp.route("/api/signal/<int:sid>/delete", methods=["POST"])
@auth.login_required
def api_signal_delete(sid):
    """Undo a taste signal (un-hide, un-pass, drop a read-seed or DNF)."""
    u = auth.current_user()
    with db.conn() as c:
        c.execute("DELETE FROM signals WHERE id=? AND user_id=?", (sid, u["id"]))
    return jsonify({"ok": True})


@bp.route("/api/rating/delete", methods=["POST"])
@auth.login_required
def api_rating_delete():
    """Clear a rating (book returns to unrated)."""
    u = auth.current_user()
    key = (request.get_json(force=True).get("key") or "").strip()
    if not key:
        return jsonify({"error": "key required"}), 400
    with db.conn() as c:
        c.execute("DELETE FROM ratings WHERE user_id=? AND asin=?", (u["id"], key))
    return jsonify({"ok": True})


@bp.route("/api/dnf", methods=["POST"])
@auth.login_required
def api_dnf():
    """Mark a book you didn't finish — a negative taste signal that stops it
    (and exact-title re-suggestions) from coming back."""
    u = auth.current_user()
    body = request.get_json(force=True)
    title, authr = (body.get("title") or "").strip(), (body.get("author") or "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    fmt = _req_format(body)
    hit = ebookmeta.search(f"{title} {authr}", num=1) if fmt == "ebook" else audible.search(f"{title} {authr}", num=1)
    b = hit[0] if hit else {"asin": "", "title": title, "author": authr}
    key = b.get("asin") or b.get("id") or db.rating_key("", b.get("title", title), b.get("author", authr))
    with db.conn() as c:
        c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why,format) VALUES (?,?,?,?,?,?)",
                  (u["id"], "asin", key, -2, f"dnf: {b.get('title', title)}", fmt))
        # propagate to moods: a DNF nudges down the moods that book carries
        for m in tagging.derive(b.get("categories") or []).get("mood", []):
            c.execute("INSERT INTO signals (user_id,kind,value,weight,why,format) VALUES (?,?,?,?,?,?) "
                      "ON CONFLICT(user_id,kind,value) DO UPDATE SET weight=signals.weight-0.5",
                      (u["id"], "mood", m, -0.5, f"dnf mood: {b.get('title', title)}", fmt))
    return jsonify({"ok": True, "matched": b.get("title", title)})


@bp.route("/api/onboard/dismiss", methods=["POST"])
@auth.login_required
def api_onboard_dismiss():
    """Hide the quick-rate onboarding card for good."""
    u = auth.current_user()
    with db.conn() as c:
        c.execute("INSERT OR IGNORE INTO signals (user_id,kind,value,weight,why) "
                  "VALUES (?,?,?,?,?)", (u["id"], "onboard_dismissed", "1", 0, "dismissed onboarding"))
    return jsonify({"ok": True})


@bp.route("/api/requests/status")
@auth.login_required
def api_requests_status():
    """Live per-request status, merging the stored status with Chaptarr's queue
    (downloading / importing) so the Requests page shows real progress."""
    u = auth.current_user()
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id,title,status,chaptarr_ref FROM requests "
            "WHERE user_id=? AND status IN ('queued','handed')",
            (u["id"],),
        )]
    live = shelfmark.queue_status() if rows else {}
    out = {}
    for r in rows:
        ref = str(r.get("chaptarr_ref") or "").strip()
        hit = live.get(ref) if ref else None
        out[r["id"]] = hit or r["status"]
    return jsonify(out)


@bp.route("/api/webhook/chaptarr", methods=["POST"])
def api_webhook_chaptarr():
    """Chaptarr Connect webhook → real-time request updates. Token-protected via
    ?token= (set the same token in Settings). On an import/download event we run
    the library refresh, which flips a request to 'available' ONLY when the book
    actually appears in the library in that request's format, and notifies the
    real requester. Delegating here avoids the old fuzzy title-match that could
    flip the wrong user's or wrong-format request."""
    import secrets as _secrets
    token = db.get_meta("chaptarr_webhook_token", "")
    if not token or not _secrets.compare_digest(request.args.get("token") or "", token):
        return jsonify({"error": "bad token"}), 403
    body = request.get_json(silent=True) or {}
    event = (body.get("eventType") or "").lower()
    if event in ("download", "bookfileimported", "import"):
        try:
            from . import scheduler
            scheduler.refresh_library()
        except Exception as e:
            log.warning("webhook refresh failed: %s", e)
    return jsonify({"ok": True})


@bp.route("/api/requests/<int:rid>/approve", methods=["POST"])
@auth.admin_required
def api_request_approve(rid):
    with db.conn() as c:
        row = c.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        row = dict(row)
        if row["status"] != "pending_approval":
            return jsonify({"error": "not awaiting approval"}), 400
        # compare-and-swap: only the writer that flips it out of pending_approval
        # proceeds, so two concurrent admin approvals can't both grab it.
        claimed = c.execute("UPDATE requests SET status='approving' WHERE id=? AND status='pending_approval'",
                            (rid,)).rowcount
    if not claimed:
        return jsonify({"error": "already being processed"}), 409
    book = {"asin": row["asin"], "title": row["title"], "author": row["author"],
            "cover": row["cover"], "format": row["format"]}
    # a 'both' request must hand off BOTH formats — chaptarr maps 'both'→audiobook,
    # so the approval path silently dropped the ebook half (the direct path splits).
    fmts = ["audiobook", "ebook"] if book["format"] == "both" else [book["format"]]
    rmap = {
        f: _acquisition_handoff(
            book["title"],
            book["author"],
            book["asin"],
            fmt=f,
        )
        for f in fmts
    }
    ok_any = any(r.get("ok") for r in rmap.values())
    oks = [f for f in fmts if rmap[f].get("ok")]
    res = (rmap[fmts[0]] if len(fmts) == 1
           else {"ok": ok_any, "ref": "", "detail": (f"Sent as {' + '.join(oks)}." if oks
                  else rmap[fmts[-1]].get("detail", "Couldn't add right now."))})
    # a transient Chaptarr outage (retry) must keep the row re-approvable, not
    # burn the admin's approval into a 'failed' state that can't be re-approved.
    retryable = not ok_any and any(r.get("retry") for r in rmap.values())
    status = "handed" if ok_any else ("pending_approval" if retryable else "failed")
    with db.conn() as c:
        c.execute("UPDATE requests SET status=?,detail=?,chaptarr_ref=?,updated_at=datetime('now','localtime') WHERE id=?",
                  (status, res.get("detail", ""), res.get("ref", ""), rid))
    requester = db.get_user(row["user_id"])
    if requester and requester.get("email") and db.get_pref(row["user_id"], "notify_approved", "1") == "1":
        try:
            notify.request_approved(book, requester["email"], _base_url())
        except Exception as e:
            log.debug("approved notify failed: %s", e)
    return jsonify({"ok": res["ok"], "status": status, "detail": res.get("detail", "")})


@bp.route("/api/requests/<int:rid>/deny", methods=["POST"])
@auth.admin_required
def api_request_deny(rid):
    reason = (request.get_json(silent=True) or {}).get("reason", "").strip()
    with db.conn() as c:
        row = c.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        row = dict(row)
        c.execute("UPDATE requests SET status='denied',detail=?,updated_at=datetime('now','localtime') WHERE id=?",
                  (reason or "Not approved", rid))
    requester = db.get_user(row["user_id"])
    if requester and requester.get("email") and db.get_pref(row["user_id"], "notify_denied", "1") == "1":
        try:
            notify.request_denied({"title": row["title"], "author": row.get("author", ""),
                                   "format": row.get("format", "audiobook")}, requester["email"], reason, _base_url())
        except Exception as e:
            log.debug("denied notify failed: %s", e)
    return jsonify({"ok": True})


@bp.route("/api/requests/check", methods=["POST"])
@auth.login_required
def api_requests_check():
    """Re-scan all connected libraries now and flip any request to 'available'
    if its book has appeared (e.g. the user added it outside Chaptarr). Returns
    how many flipped."""
    wait = _cooldown("requests_check", 20)
    if wait:
        return jsonify({"ok": False, "detail": f"Just checked — try again in {wait}s."}), 429
    from . import scheduler
    u = auth.current_user()
    before = after = 0
    with db.conn() as c:
        before = c.execute("SELECT COUNT(*) n FROM requests WHERE user_id=? AND status IN ('queued','handed','failed')",
                          (u["id"],)).fetchone()["n"]
    try:
        scheduler.refresh_library()
    except Exception as e:
        return jsonify({"ok": False, "detail": str(e)})
    with db.conn() as c:
        after = c.execute("SELECT COUNT(*) n FROM requests WHERE user_id=? AND status IN ('queued','handed','failed')",
                         (u["id"],)).fetchone()["n"]
    flipped = max(before - after, 0)
    return jsonify({"ok": True, "flipped": flipped,
                    "detail": f"{flipped} now available" if flipped else "No new matches in your libraries"})


@bp.route("/api/requests/retry-all", methods=["POST"])
@auth.login_required
def api_retry_all():
    """Re-send every failed request to Chaptarr in one go (the Wanted list)."""
    u = auth.current_user()
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM requests WHERE user_id=? AND status='failed' ORDER BY id", (u["id"],))]
    ok = 0
    for row in rows:
        with db.conn() as c:
            c.execute("DELETE FROM requests WHERE id=?", (row["id"],))
        res = _hand_off_request(u["id"], row, row.get("source", "manual"))
        if res.get("ok"):
            ok += 1
    return jsonify({"ok": True, "retried": len(rows), "succeeded": ok,
                    "detail": f"Retried {len(rows)} — {ok} handed off" if rows else "Nothing to retry"})


@bp.route("/api/request/<int:rid>/retry", methods=["POST"])
@auth.login_required
def api_retry(rid):
    u = auth.current_user()
    with db.conn() as c:
        row = c.execute("SELECT * FROM requests WHERE id=? AND user_id=?", (rid, u["id"])).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        row = dict(row)
        # only failed/denied requests are retryable — don't clobber an available
        # or pending-approval row.
        if row["status"] not in ("failed", "denied"):
            return jsonify({"error": "Only failed requests can be retried."}), 400
        was_denied = row["status"] == "denied"
        c.execute("DELETE FROM requests WHERE id=?", (rid,))
    # a denial is an explicit admin decision — re-requesting it goes BACK through
    # approval (unless the requester is admin), never straight to a grab.
    if was_denied and u["role"] != "admin":
        return jsonify(_queue_for_approval(u, row, row.get("source", "manual")))
    return jsonify(_hand_off_request(u["id"], row, row["source"]))


@bp.route("/api/request/<int:rid>", methods=["DELETE"])
@auth.login_required
def api_request_delete(rid):
    u = auth.current_user()
    with db.conn() as c:
        c.execute("DELETE FROM requests WHERE id=? AND user_id=?", (rid, u["id"]))
    return jsonify({"ok": True})


@bp.route("/api/shelf", methods=["POST"])
@auth.login_required
def api_shelf():
    """Set (or clear) a book's personal shelf: want | reading | read | ''."""
    u = auth.current_user()
    b = request.get_json(force=True)
    rkey = (b.get("key") or b.get("asin") or "").strip()
    state = (b.get("state") or "").strip()
    if not rkey or state not in ("", "want", "reading", "read"):
        return jsonify({"error": "key + valid state required"}), 400
    db.shelf_set(u["id"], rkey, state, b.get("title", ""), b.get("author", ""),
                 b.get("cover", ""), b.get("format") or "audiobook")
    synced = None
    if state == "read":
        synced = _push_read_to_source(u, b.get("title", ""), b.get("author", ""),
                                      b.get("format") or "audiobook")
    return jsonify({"ok": True, "state": state, "counts": db.shelf_counts(u["id"]),
                    "synced": synced})


def _push_read_to_source(u, title, author, fmt):
    """Best-effort: tell the originating library app (Audiobookshelf, Komga,
    Kavita…) that this book is finished, so 'Read' in Stackarr propagates back.
    Returns the source's label if it synced, else None — marking locally never
    depends on the push succeeding."""
    tl = (title or "").strip().lower()
    if not tl:
        return None
    al = (author or "").split(",")[0].strip().lower()
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT item_id,author,format FROM library WHERE lower(title)=? AND gone_at IS NULL", (tl,))]
    if not rows:
        return None

    def _score(r):
        s = 0
        if (r.get("format") or "audiobook") == fmt:
            s += 2
        if al and al in (r.get("author") or "").lower():
            s += 1
        return s

    rows.sort(key=_score, reverse=True)
    row = rows[0]
    item_id, rfmt = row["item_id"], (row.get("format") or "audiobook")
    try:
        if rfmt == "audiobook":
            if absclient.set_finished(u.get("abs_token"), item_id):
                return "Audiobookshelf"
        else:
            from . import backends
            for be in backends.sources("ebook"):
                if getattr(be, "can_write_progress", False) and be.mark_read(u, item_id):
                    return be.label
    except Exception as e:
        log.debug("mark-read push failed: %s", e)
    return None


@bp.route("/api/feedback", methods=["POST"])
@auth.login_required
def api_feedback():
    """Lightweight 'more like this' / 'less like this' — nudges the engine via
    author (+ mood) signals without a full rating."""
    u = auth.current_user()
    b = request.get_json(force=True)
    author = (b.get("author") or "").split(",")[0].strip()
    direction = b.get("direction")          # "more" | "less"
    fmt = _req_format(b)
    if not author or direction not in ("more", "less"):
        return jsonify({"error": "author + direction required"}), 400
    w = 2 if direction == "more" else -2
    with db.conn() as c:
        c.execute("INSERT INTO signals (user_id,kind,value,weight,why,format) VALUES (?,?,?,?,?,?) "
                  "ON CONFLICT(user_id,kind,value) DO UPDATE SET weight=signals.weight+excluded.weight",
                  (u["id"], "author", author, w, f"{direction} like: {b.get('title','')}", fmt))
    return jsonify({"ok": True})


@bp.route("/api/review/vote", methods=["POST"])
@auth.login_required
def api_review_vote():
    u = auth.current_user()
    try:
        rid = int(request.get_json(force=True).get("rating_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "rating_id required"}), 400
    return jsonify({"ok": True, "votes": db.review_vote(u["id"], rid)})


@bp.route("/api/get-other-format", methods=["POST"])
@auth.login_required
def api_get_other_format():
    """Own/seen a title in one format → request the other format (audiobook⇄ebook)."""
    u = auth.current_user()
    b = request.get_json(force=True)
    title, author = (b.get("title") or "").strip(), (b.get("author") or "").strip()
    other = "ebook" if b.get("format") == "audiobook" else "audiobook"
    if not title:
        return jsonify({"error": "title required"}), 400
    hit = (ebookmeta.search if other == "ebook" else audible.search)(f"{title} {author}", num=1)
    pick = hit[0] if hit else {"title": title, "author": author}
    pick["format"] = other
    pick["asin"] = pick.get("asin") or pick.get("id", "")
    return jsonify(_hand_off_request(u["id"], pick, "manual"))


@bp.route("/api/surprise")
@auth.login_required
def api_surprise():
    """One strong pick on demand — the highest-scored pending suggestion (of the
    active/echosen format), or a popular fallback. Optional ?mood= filter."""
    u = auth.current_user()
    fmt = request.args.get("format") or ""
    where = "AND format=?" if fmt in ("audiobook", "ebook") else ""
    args = [u["id"]] + ([fmt] if where else [])
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            f"SELECT * FROM suggestions WHERE user_id=? AND status='pending' {where} "
            "ORDER BY score DESC LIMIT 40", args)]
    mood = request.args.get("mood", "").lower().strip()
    if mood:
        rows = [r for r in rows if mood in [m.lower() for m in db.tags_for(
            db.rating_key(r["asin"], r["title"], r["author"])).get("mood", [])]] or rows
    if not rows:
        pop = discover.popular(8)
        rows = [{"asin": x["asin"], "title": x["title"], "author": x["author"],
                 "cover": x.get("cover", ""), "reason": "A popular pick to get you started",
                 "format": "audiobook"} for x in pop if x.get("asin")]
    if not rows:
        return jsonify({"ok": False})
    import hashlib
    # deterministic-but-varied: rotate by minute so repeated taps differ
    idx = int(hashlib.md5(request.args.get("n", "0").encode()).hexdigest(), 16) % len(rows)
    return jsonify({"ok": True, "book": rows[idx]})


SETTING_KEYS = {
    "email_theme", "language", "email_frequency", "suggest_interval_hours",
    "smtp_host", "smtp_port", "smtp_user", "smtp_pass", "smtp_from", "smtp_to",
    "abs_url", "abs_admin_token",
    "chaptarr_url", "chaptarr_api_key", "chaptarr_root_folder",
    "chaptarr_quality_profile_id", "chaptarr_metadata_profile_id",
    "goodreads_rss", "hardcover_token", "discord_webhook", "custom_webhook",
    "auto_add_level", "formats",
    "kavita_url", "kavita_api_key",
    "calibreweb_url", "calibreweb_user", "calibreweb_pass",
    "komga_url", "komga_user", "komga_pass",
    "opds_url", "opds_user", "opds_pass",
    "chaptarr_webhook_token",
}
BOOL_KEYS = {"email_enabled", "discord_enabled",
             "notify_avail_enabled", "notify_newrelease_enabled",
             "abs_ebooks", "koreader_sync"}
# per-user boolean preferences (written via /api/prefs, never the global endpoint)
USER_BOOL_PREFS = {"cross_format_taste", "hide_rated_history"}
# per-user email notification toggles (also via /api/prefs)
NOTIFY_PREF_KEYS = {"notify_approved", "notify_available", "notify_denied"}


@bp.route("/api/settings", methods=["POST"])
@auth.admin_required
def api_settings():
    """Install-wide configuration — admin only (server URLs, API keys, SMTP,
    format mode, …). Per-user preferences go through /api/prefs."""
    body = request.get_json(force=True)
    for k, v in body.items():
        if k in BOOL_KEYS:
            db.set_meta(k, "1" if v else "0")
        elif k in SETTING_KEYS:
            db.set_meta(k, str(v).strip())
    return jsonify({"ok": True})


@bp.route("/api/prefs", methods=["POST"])
@auth.login_required
def api_prefs():
    """Per-user preferences any signed-in user may set for themselves."""
    u = auth.current_user()
    body = request.get_json(force=True)
    for k, v in body.items():
        if k in USER_BOOL_PREFS or k in NOTIFY_PREF_KEYS:
            db.set_pref(u["id"], k, "1" if v else "0")
    return jsonify({"ok": True})


@bp.route("/api/admin/approval-mode", methods=["POST"])
@auth.admin_required
def api_admin_approval_mode():
    """Toggle global manual-approval vs auto-approve for non-admin requests."""
    require = bool(request.get_json(force=True).get("require_approval"))
    db.set_meta("require_approval", "1" if require else "0")
    return jsonify({"ok": True, "require_approval": require})


@bp.route("/api/admin/import-users", methods=["POST"])
@auth.admin_required
def api_admin_import_users():
    """Import accounts from the connected sources (e.g. Audiobookshelf) as linked
    local accounts. Also flips the daily-sync setting when asked."""
    from . import scheduler
    body = request.get_json(silent=True) or {}
    if "sync" in body:
        db.set_meta("user_sync", "1" if body["sync"] else "0")
        return jsonify({"ok": True, "sync": bool(body["sync"])})
    res = scheduler.import_users()
    return jsonify({"ok": True, **res,
                    "detail": f"{res['created']} new account(s) imported from {res['seen']} source user(s)."})


@bp.route("/api/admin/user/<int:uid>", methods=["POST"])
@auth.admin_required
def api_admin_user(uid):
    """Admin: set a user's role or trusted flag."""
    me = auth.current_user()
    body = request.get_json(force=True)
    target = db.get_user(uid)
    if not target:
        return jsonify({"error": "no such user"}), 404
    if "role" in body:
        # don't let an admin demote themselves into a lockout
        if uid == me["id"] and body["role"] != "admin":
            return jsonify({"error": "You can't remove your own admin role."}), 400
        db.set_role(uid, "admin" if body["role"] == "admin" else "user")
    if "trusted" in body:
        db.set_pref(uid, "trusted", "1" if body["trusted"] else "0")
    return jsonify({"ok": True})


@bp.route("/api/test/<service>", methods=["POST"])
@auth.admin_required
def api_test(service):
    body = request.get_json(silent=True) or {}
    # Apply the values typed in the form ONLY for the duration of the test, then
    # restore them — a failed or abandoned test must not silently overwrite the
    # working saved credentials/URLs (the user still clicks Save to persist).
    overrides = {k: str(v).strip() for k, v in body.items() if k in SETTING_KEYS}
    prev = {k: db.get_meta(k, "") for k in overrides}
    for k, v in overrides.items():
        db.set_meta(k, v)
    try:
        try:
            if service == "chaptarr":
                import requests as rq
                r = rq.get(f"{chaptarr.url()}/api/v1/system/status",
                           headers={"X-Api-Key": chaptarr.api_key()}, timeout=15)
                if not r.ok:
                    return jsonify({"ok": False, "detail": f"HTTP {r.status_code}"})
                warns = chaptarr.health()
                msg = "Connected"
                if warns:
                    msg += " — heads up: " + "; ".join(w["message"][:80] for w in warns[:2])
                return jsonify({"ok": True, "detail": msg, "warnings": warns})
            # every library source backend (abs / kavita / calibreweb) self-tests
            from . import backends
            b = backends.by_id(service)
            if b is not None:
                return jsonify(b.test())
        except Exception as e:
            return jsonify({"ok": False, "detail": str(e)})
        return jsonify({"ok": False, "detail": "unknown service"}), 404
    finally:
        for k, v in prev.items():
            db.set_meta(k, v)


@bp.route("/api/email/preview/<theme>")
@auth.login_required
def api_email_preview(theme):
    if theme not in notify.THEMES:
        return "unknown theme", 404
    sample = [{"title": "The Way of Kings", "author": "Brandon Sanderson", "cover": "",
               "reason": "Next in The Stormlight Archive after “Words of Radiance”"},
              {"title": "Project Hail Mary", "author": "Andy Weir", "cover": "",
               "reason": "Listeners who finished “The Martian” also enjoyed this"}]
    return notify.render_digest(sample, theme=theme, base_url=request.host_url.rstrip("/"))


@bp.route("/api/run-now", methods=["POST"])
@auth.login_required
def api_run_now():
    """Manual history scan — kicks the recommender in the background so the
    loader animation can play, same as first login."""
    wait = _cooldown("run_now", 30)
    if wait:
        return jsonify({"ok": False, "detail": f"A refresh just ran — try again in {wait}s.", "started": False}), 429
    import threading
    from . import scheduler
    u = auth.current_user()
    threading.Thread(target=scheduler.run_for_user, args=(u["id"],),
                     kwargs={"force": True}, daemon=True).start()
    return jsonify({"ok": True, "started": True})


@bp.route("/api/suggestions/status")
@auth.login_required
def api_suggestions_status():
    u = auth.current_user()
    with db.conn() as c:
        pending = c.execute("SELECT COUNT(*) n FROM suggestions WHERE user_id=? AND status='pending'",
                            (u["id"],)).fetchone()["n"]
    return jsonify({"pending": pending,
                    "running": db.get_meta(f"running_{u['id']}", "0") == "1"})


LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}


def _read_logs(min_level: str, limit: int) -> list[str]:
    import os, re
    if not os.path.exists(config.LOG_FILE):
        return []
    thresh = LOG_LEVELS.get(min_level, 20)
    out, last_lvl = [], 20
    with open(config.LOG_FILE, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = re.match(r"^[\d\-]+ [\d:]+ \[(\w+)\]", line)   # the leading "date time [LEVEL]"
            if m:
                last_lvl = LOG_LEVELS.get(m.group(1), 20)
            # unbracketed continuation/traceback lines inherit the prior level so a
            # filtered view keeps the traceback body, not just the bare message.
            if last_lvl >= thresh:
                out.append(line.rstrip())
    return out[-limit:]


@bp.route("/api/logs")
@auth.admin_required
def api_logs():
    level = request.args.get("level", "INFO").upper()
    try:
        limit = int(request.args.get("limit", "300"))
    except (ValueError, TypeError):
        limit = 300
    return jsonify({"lines": _read_logs(level, limit)})


@bp.route("/api/logs/download")
@auth.admin_required
def api_logs_download():
    from flask import Response
    level = request.args.get("level", "DEBUG").upper()
    body = "\n".join(_read_logs(level, 200000)) or "(no log entries)"
    return Response(body, mimetype="text/plain",
                    headers={"Content-Disposition": f"attachment; filename=stackarr-{level.lower()}.log"})


@bp.route("/cover/<item_id>")
@auth.login_required
def cover(item_id):
    """Proxy an Audiobookshelf cover through Stackarr so the browser (which
    can't reach host.docker.internal) gets it same-origin, token hidden."""
    from flask import Response
    import requests as rq
    try:
        r = rq.get(f"{absclient.abs_url()}/api/items/{item_id}/cover",
                   headers={"Authorization": f"Bearer {absclient.admin_token()}"}, timeout=20)
        if r.ok and r.content:
            return Response(r.content, mimetype=r.headers.get("Content-Type", "image/jpeg"),
                            headers={"Cache-Control": "max-age=86400"})
    except Exception:
        pass
    # ABS has no art for this item -> fall back to the Audible cover by title/author
    try:
        m = absclient.item_detail(item_id)
        hits = audible.search(f"{m.get('title','')} {m.get('author','')}", num=1)
        if hits and hits[0].get("cover"):
            return redirect(hits[0]["cover"])
    except Exception:
        pass
    return redirect(url_for("static", filename="cover-placeholder.svg"))


@bp.route("/coverart")
@auth.login_required
def coverart():
    """Resolve cover art for a book that has no stored cover (ebooks, and
    'marked read' items not in Audiobookshelf). Looks the cover up by ASIN, then
    by title+author — eBooks via the book metadata APIs, audiobooks via Audible —
    and caches the resolved URL so it's a one-time lookup per book. Redirects to
    the art, or a placeholder when nothing is found."""
    asin = (request.args.get("asin") or "").strip()
    title = (request.args.get("title") or "").strip()
    author = (request.args.get("author") or "").strip()
    fmt = (request.args.get("fmt") or "").strip()
    placeholder = url_for("static", filename="cover-placeholder.svg")
    if not title and not asin.startswith("B0"):
        return redirect(placeholder)
    cache_key = "cart:" + db.rating_key(asin if asin.startswith("B0") else "", title, author)
    cached = db.get_meta(cache_key, "")
    if cached:
        return redirect(placeholder if cached == "none" else cached)
    cover, failed = "", False
    try:
        if asin.startswith("B0"):
            cover = (audible.by_asin(asin) or {}).get("cover", "")
        if not cover and title:
            q = f"{title} {author}".strip()
            hits = ebookmeta.search(q, 1) if (fmt == "ebook" or asin.startswith(("gb:", "ol:", "hc:"))) else audible.search(q, num=1)
            if hits:
                cover = hits[0].get("cover", "")
    except Exception as e:
        failed = True
        log.debug("coverart lookup failed for %s: %s", title, e)
    # Cache only a real hit. The metadata helpers SWALLOW timeouts/429s and return
    # empty without raising, so `failed` can't be trusted — caching "none" on a
    # swallowed rate-limit would blank the cover forever. A genuine miss simply
    # retries on the next render (cheap) rather than persisting a permanent blank.
    if cover:
        db.set_meta(cache_key, cover)
    return redirect(cover or placeholder)


# ---- KOReader progress sync (kosync protocol) ----------------------------
# Experimental: lets KOReader e-readers sync reading progress to Stackarr.
# kosync only carries a document *hash* (no title), so it can't feed
# recommendations — it's a sync relay. Enable with KOREADER_SYNC.
def _kosync_on() -> bool:
    return db.get_meta("koreader_sync", "1" if config.KOREADER_SYNC else "0") == "1"


def _kosync_key(user: str, doc: str) -> str:
    """Storage key for a user's progress on a document. Both segments are hashed
    to fixed-length hex so the user/doc boundary is unambiguous — the old
    `kosync_{user}_{doc}` form could be made to collide with another kosync
    account's row via a crafted `document` value. (Off-by-default feature; any
    pre-existing progress simply re-syncs from the device on next open.)"""
    import hashlib
    h = lambda s: hashlib.sha1((s or "").encode("utf-8", "replace")).hexdigest()
    return f"kosync_{h(user)}_{h(doc)}"


def _kosync_check():
    """The kosync username when the x-auth-user/x-auth-key headers match a
    registered user, else None. KOReader sends the key as an md5 of the password,
    so we store and compare that key verbatim (constant-time)."""
    import secrets
    user = request.headers.get("x-auth-user", "")
    key = request.headers.get("x-auth-key", "")
    if not user:
        return None
    stored = db.get_meta(f"kosync_user_{user}", "")
    return user if (stored and secrets.compare_digest(stored, key)) else None


@bp.route("/users/create", methods=["POST"])
def kosync_create():
    if not _kosync_on():
        return jsonify({"message": "disabled"}), 403
    b = request.get_json(silent=True) or {}
    user = (b.get("username") or "").strip()
    pw = b.get("password") or ""
    # require a non-empty password — an empty key would be both unusable to auth
    # with AND silently overwritable later (the "already registered" guard treats
    # an empty stored value as "not registered").
    if not user or not pw:
        return jsonify({"message": "Invalid request"}), 400
    if db.get_meta(f"kosync_user_{user}", ""):
        return jsonify({"message": "Username is already registered."}), 402
    db.set_meta(f"kosync_user_{user}", pw)
    return jsonify({"username": user}), 201


@bp.route("/users/auth")
def kosync_auth():
    if not _kosync_on():
        return jsonify({"message": "disabled"}), 403
    if not _kosync_check():
        return jsonify({"message": "Unauthorized"}), 401
    return jsonify({"authorized": "OK"})


@bp.route("/syncs/progress", methods=["PUT"])
def kosync_put():
    if not _kosync_on():
        return jsonify({"message": "disabled"}), 403
    user = _kosync_check()
    if not user:
        return jsonify({"message": "Unauthorized"}), 401
    b = request.get_json(silent=True) or {}
    doc = b.get("document", "")
    if not doc:
        return jsonify({"message": "Invalid request"}), 400
    import json as _json
    db.set_meta(_kosync_key(user, doc), _json.dumps({
        "progress": b.get("progress", ""), "percentage": b.get("percentage", 0),
        "device": b.get("device", ""), "device_id": b.get("device_id", "")}))
    return jsonify({"document": doc, "timestamp": 0})


@bp.route("/syncs/progress/<doc>")
def kosync_get(doc):
    if not _kosync_on():
        return jsonify({"message": "disabled"}), 403
    user = _kosync_check()
    if not user:
        return jsonify({"message": "Unauthorized"}), 401
    import json as _json
    raw = db.get_meta(_kosync_key(user, doc), "")
    if not raw:
        return jsonify({})
    d = _json.loads(raw)
    return jsonify({"document": doc, "progress": d.get("progress", ""),
                    "percentage": d.get("percentage", 0), "device": d.get("device", ""),
                    "device_id": d.get("device_id", ""), "timestamp": 0})


@bp.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "app": config.APP_NAME,
                    "version": config.VERSION, "stage": config.RELEASE_STAGE})


# ------------------------------------------------------------------- pwa ---
@bp.route("/manifest.webmanifest")
def manifest():
    base = config.URL_BASE or ""
    body = {
        "name": config.APP_NAME, "short_name": config.APP_NAME,
        "description": "Audiobook recommendations from your listening history",
        "start_url": f"{base}/", "scope": f"{base}/", "display": "standalone",
        "background_color": "#0f172a", "theme_color": "#0f172a",
        "icons": [
            {"src": f"{base}/static/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
            {"src": f"{base}/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": f"{base}/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }
    return jsonify(body)


@bp.route("/sw.js")
def service_worker():
    base = config.URL_BASE or ""
    js = (
        f"const C='stackarr-{config.VERSION}';\n"      # tie cache to version so app.js/css auto-bust each release
        # Only static assets are precached/cached. Authenticated HTML pages (home,
        # settings, requests — which contain per-user data) are NEVER cached, so a
        # shared device can't serve one user's page to the next.
        f"const SHELL=['{base}/static/style.css','{base}/static/app.js','{base}/static/icon.svg'];\n"
        "self.addEventListener('install',e=>{e.waitUntil(caches.open(C).then(c=>c.addAll(SHELL)).then(()=>self.skipWaiting()))});\n"
        "self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==C).map(k=>caches.delete(k)))).then(()=>self.clients.claim()))});\n"
        "self.addEventListener('fetch',e=>{const u=new URL(e.request.url);"
        "if(e.request.method!=='GET'||!u.pathname.includes('/static/')){return}"   # static assets only
        # NETWORK-FIRST: always try the live asset (so a deploy is instantly picked
        # up and a stale SW can never serve a broken old app.js), falling back to
        # cache only when offline. Cache-first previously made the app look "down"
        # after a deploy until the SW happened to update.
        "e.respondWith(fetch(e.request).then(r=>{const cp=r.clone();caches.open(C).then(c=>c.put(e.request,cp));return r}).catch(()=>caches.match(e.request)))});\n"
    )
    from flask import Response
    return Response(js, mimetype="application/javascript",
                    headers={"Service-Worker-Allowed": f"{base}/"})
