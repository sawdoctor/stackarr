"""SQLite storage (WAL, per-call connections). One file in DATA_DIR, with a
schema covering every Stackarr feature: multi-user, per-user suggestions,
requests, taste signals (positive + negative), 5-star ratings, a library
snapshot for deletion-detection, and import-list items."""
import logging
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager

from . import config

DB_PATH = os.path.join(config.DATA_DIR, "stackarr.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  abs_user_id TEXT UNIQUE,
  username TEXT UNIQUE NOT NULL,
  abs_token TEXT,                              -- this user's ABS token (for their history)
  password_hash TEXT,                          -- local-account password (pbkdf2); NULL = no local password
  role TEXT NOT NULL DEFAULT 'user',           -- user | admin
  created_at TEXT DEFAULT (datetime('now','localtime')),
  last_login TEXT
);
-- external sign-in identities linked to a local account. A user may have several
-- (Audiobookshelf + Kavita + …); the local account is always the canonical one.
CREATE TABLE IF NOT EXISTS user_links (
  provider TEXT NOT NULL,                      -- abs | kavita | komga | calibreweb
  external_id TEXT NOT NULL,
  user_id INTEGER NOT NULL,
  token TEXT DEFAULT '',
  created_at TEXT DEFAULT (datetime('now','localtime')),
  PRIMARY KEY (provider, external_id)
);
CREATE TABLE IF NOT EXISTS suggestions (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  asin TEXT,
  title TEXT NOT NULL,
  author TEXT DEFAULT '',
  narrator TEXT DEFAULT '',
  series TEXT DEFAULT '',
  cover TEXT DEFAULT '',
  reason TEXT DEFAULT '',
  lane TEXT DEFAULT 'foryou',                  -- foryou | series | narrator | discover | importlist
  format TEXT DEFAULT 'audiobook',             -- audiobook | ebook
  score REAL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending',      -- pending | approved | rejected
  extra TEXT DEFAULT '',                        -- e.g. release date for upcoming titles
  created_at TEXT DEFAULT (datetime('now','localtime')),
  decided_at TEXT,
  UNIQUE(user_id, asin, format)
);
CREATE TABLE IF NOT EXISTS requests (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  asin TEXT,
  title TEXT NOT NULL,
  author TEXT DEFAULT '',
  cover TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'queued',       -- queued | handed | available | failed
  detail TEXT DEFAULT '',
  format TEXT DEFAULT 'audiobook',             -- audiobook | ebook
  chaptarr_ref TEXT DEFAULT '',                -- current acquisition task/release id
  attempted_refs TEXT DEFAULT '[]',             -- JSON list of acquisition ids already tried
  source TEXT DEFAULT 'suggestion',            -- suggestion | manual | importlist
  created_at TEXT DEFAULT (datetime('now','localtime')),
  updated_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  kind TEXT NOT NULL,                          -- asin | author | series | narrator
  value TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 0,              -- >0 positive (liked/read), <0 negative (pass/DNF/deleted)
  why TEXT DEFAULT '',
  created_at TEXT DEFAULT (datetime('now','localtime')),
  UNIQUE(user_id, kind, value)
);
CREATE TABLE IF NOT EXISTS ratings (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  asin TEXT NOT NULL,
  title TEXT DEFAULT '',
  author TEXT DEFAULT '',
  stars INTEGER NOT NULL,                      -- 1..5
  review TEXT DEFAULT '',                       -- optional free-text review (shared)
  format TEXT DEFAULT 'audiobook',
  created_at TEXT DEFAULT (datetime('now','localtime')),
  updated_at TEXT,
  UNIQUE(user_id, asin)
);
CREATE TABLE IF NOT EXISTS library (
  item_id TEXT PRIMARY KEY,
  library_id TEXT,
  title TEXT,
  author TEXT,
  asin TEXT,
  series TEXT DEFAULT '',
  series_seq REAL,
  narrator TEXT DEFAULT '',
  format TEXT DEFAULT 'audiobook',             -- audiobook | ebook
  source TEXT DEFAULT 'abs',                    -- backend id the book came from
  first_seen TEXT DEFAULT (datetime('now','localtime')),
  last_seen TEXT,
  gone_at TEXT
);
CREATE TABLE IF NOT EXISTS importlist_items (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  source TEXT NOT NULL,                        -- goodreads | hardcover
  title TEXT NOT NULL,
  author TEXT DEFAULT '',
  asin TEXT DEFAULT '',
  added_at TEXT DEFAULT (datetime('now','localtime')),
  UNIQUE(user_id, source, title, author)
);
CREATE TABLE IF NOT EXISTS shelf (
  user_id INTEGER NOT NULL,
  rkey TEXT NOT NULL,                          -- rating_key (asin / t-slug / gb:/ol:)
  state TEXT NOT NULL,                         -- want | reading | read
  title TEXT DEFAULT '', author TEXT DEFAULT '', cover TEXT DEFAULT '',
  format TEXT DEFAULT 'audiobook',
  added_at TEXT DEFAULT (datetime('now','localtime')),
  finished_at TEXT,                            -- set when state -> read (heatmap/goal)
  PRIMARY KEY (user_id, rkey)
);
CREATE TABLE IF NOT EXISTS book_tags (
  rkey TEXT NOT NULL,
  tag TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'genre',          -- genre | mood | pace | warning
  source TEXT NOT NULL DEFAULT 'auto',         -- auto | user
  PRIMARY KEY (rkey, tag, kind)                -- kind is part of identity: the
);                                             -- same word can be genre AND mood
CREATE TABLE IF NOT EXISTS review_votes (
  user_id INTEGER NOT NULL,
  rating_id INTEGER NOT NULL,
  PRIMARY KEY (user_id, rating_id)
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS ix_shelf_user_state ON shelf(user_id, state);
CREATE INDEX IF NOT EXISTS ix_tags_kind ON book_tags(kind);
CREATE INDEX IF NOT EXISTS ix_sugg_user_status ON suggestions(user_id, status);
CREATE INDEX IF NOT EXISTS ix_req_user_status ON requests(user_id, status);
"""


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)
        # lightweight migrations for existing DBs
        for stmt in ("ALTER TABLE suggestions ADD COLUMN extra TEXT DEFAULT ''",
                     "ALTER TABLE library ADD COLUMN series TEXT DEFAULT ''",
                     "ALTER TABLE library ADD COLUMN series_seq REAL",
                     "ALTER TABLE library ADD COLUMN narrator TEXT DEFAULT ''",
                     "ALTER TABLE library ADD COLUMN format TEXT DEFAULT 'audiobook'",
                     "ALTER TABLE library ADD COLUMN source TEXT DEFAULT 'abs'",
                     "ALTER TABLE suggestions ADD COLUMN format TEXT DEFAULT 'audiobook'",
                     "ALTER TABLE requests ADD COLUMN format TEXT DEFAULT 'audiobook'",
                     "ALTER TABLE requests ADD COLUMN attempted_refs TEXT DEFAULT '[]'",
                     "ALTER TABLE ratings ADD COLUMN review TEXT DEFAULT ''",
                     "ALTER TABLE ratings ADD COLUMN format TEXT DEFAULT 'audiobook'",
                     "ALTER TABLE ratings ADD COLUMN updated_at TEXT",
                     "ALTER TABLE ratings ADD COLUMN spoiler INTEGER DEFAULT 0",
                     "ALTER TABLE signals ADD COLUMN format TEXT DEFAULT ''",
                     "ALTER TABLE users ADD COLUMN password_hash TEXT",
                     "ALTER TABLE users ADD COLUMN email TEXT"):
            try:
                c.execute(stmt)
            except sqlite3.OperationalError:
                pass
        # migrate suggestions UNIQUE(user_id,asin) -> (user_id,asin,format) so an
        # ebook pick can't be dropped just because an audiobook shares its id.
        # Use ONLY this connection (no get_meta/set_meta — those open nested
        # connections that would lock the table and make DROP fail).
        try:
            done = c.execute("SELECT v FROM meta WHERE k='sugg_fmt_uniq'").fetchone()
            if not done:
                cols = ("id,user_id,asin,title,author,narrator,series,cover,reason,"
                        "lane,format,score,status,extra,created_at,decided_at")
                c.executescript(
                    "CREATE TABLE IF NOT EXISTS suggestions_mig ("
                    " id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, asin TEXT,"
                    " title TEXT NOT NULL, author TEXT DEFAULT '', narrator TEXT DEFAULT '',"
                    " series TEXT DEFAULT '', cover TEXT DEFAULT '', reason TEXT DEFAULT '',"
                    " lane TEXT DEFAULT 'foryou', format TEXT DEFAULT 'audiobook',"
                    " score REAL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',"
                    " extra TEXT DEFAULT '', created_at TEXT DEFAULT (datetime('now','localtime')),"
                    " decided_at TEXT, UNIQUE(user_id, asin, format));"
                    f" INSERT OR IGNORE INTO suggestions_mig ({cols}) SELECT {cols} FROM suggestions;"
                    " DROP TABLE suggestions;"
                    " ALTER TABLE suggestions_mig RENAME TO suggestions;"
                    # DROP TABLE above also dropped ix_sugg_user_status (created by
                    # SCHEMA); recreate it or status queries run unindexed till restart.
                    " CREATE INDEX IF NOT EXISTS ix_sugg_user_status ON suggestions(user_id, status);")
                c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('sugg_fmt_uniq','1')")
        except sqlite3.OperationalError as e:
            logging.getLogger("stackarr.db").warning("suggestions migration skipped: %s", e)
        # migrate book_tags PK (rkey,tag) -> (rkey,tag,kind) so the same word can
        # be both a genre and a mood instead of one silently dropping the other.
        try:
            if not c.execute("SELECT v FROM meta WHERE k='tags_pk_kind'").fetchone():
                c.executescript(
                    "CREATE TABLE IF NOT EXISTS book_tags_mig ("
                    " rkey TEXT NOT NULL, tag TEXT NOT NULL,"
                    " kind TEXT NOT NULL DEFAULT 'genre', source TEXT NOT NULL DEFAULT 'auto',"
                    " PRIMARY KEY (rkey, tag, kind));"
                    " INSERT OR IGNORE INTO book_tags_mig (rkey,tag,kind,source)"
                    " SELECT rkey,tag,kind,source FROM book_tags;"
                    " DROP TABLE book_tags;"
                    " ALTER TABLE book_tags_mig RENAME TO book_tags;"
                    " CREATE INDEX IF NOT EXISTS ix_tags_kind ON book_tags(kind);")
                c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('tags_pk_kind','1')")
        except sqlite3.OperationalError as e:
            logging.getLogger("stackarr.db").warning("book_tags migration skipped: %s", e)
        # seed link rows for pre-existing ABS users so they keep their account
        # after the multi-provider switch (match by their stored abs_user_id).
        try:
            for r in c.execute("SELECT id, abs_user_id, abs_token FROM users "
                               "WHERE abs_user_id IS NOT NULL AND abs_user_id<>''"):
                c.execute("INSERT OR IGNORE INTO user_links (provider, external_id, user_id, token) "
                          "VALUES ('abs', ?, ?, ?)", (r["abs_user_id"], r["id"], r["abs_token"] or ""))
        except sqlite3.OperationalError:
            pass


def get_meta(k: str, default: str = "") -> str:
    with conn() as c:
        row = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row["v"] if row else default


def set_meta(k: str, v: str):
    with conn() as c:
        c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def setting(key: str, fallback: str = "") -> str:
    """Runtime setting: in-app value (DB) wins, else env/config fallback.
    Lets service connections be configured in the UI instead of only env."""
    v = get_meta(key, "")
    return v if v else fallback


# --- per-user preferences (stored as "<key>_<user_id>" meta rows) ------------
_PREF_SENTINEL = "\x00unset"


def get_pref(user_id: int, key: str, default: str = "") -> str:
    """A per-user preference. Falls back to the install-wide value of the same
    key (for back-compat with the old global settings), then `default`."""
    v = get_meta(f"{key}_{user_id}", _PREF_SENTINEL)
    if v != _PREF_SENTINEL:
        return v
    return get_meta(key, default)


def set_pref(user_id: int, key: str, value: str):
    set_meta(f"{key}_{user_id}", value)


def rating_key(asin: str, title: str, author: str) -> str:
    """Stable identity for a book in History & ratings: the real ASIN when we
    have one, else a slug of title+author. Most ABS library books have no ASIN,
    so the slug lets them be rated/removed; it's reproducible across page loads
    and shared by the recommender so 'removed' books stop seeding suggestions.
    Alphanumeric+dashes, so it's safe in HTML attributes and JS strings."""
    if asin:
        return asin
    base = f"{(title or '').strip().lower()} {(author or '').split(',')[0].strip().lower()}"
    return "t-" + re.sub(r"[^a-z0-9]+", "-", base).strip("-")


def secret_key() -> str:
    s = get_meta("secret_key")
    if not s:
        s = secrets.token_hex(32)
        set_meta("secret_key", s)
    return s


def get_user(user_id: int) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def get_user_by_username(username: str) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE",
                        (username or "",)).fetchone()
        return dict(row) if row else None


def user_count() -> int:
    with conn() as c:
        return c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]


def all_users() -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, username, email, role, last_login, created_at FROM users ORDER BY id")]


def admin_emails() -> list[str]:
    with conn() as c:
        return [r["email"] for r in c.execute(
            "SELECT email FROM users WHERE role='admin' AND email IS NOT NULL AND email<>''")]


def set_role(user_id: int, role: str):
    if role in ("user", "admin"):
        with conn() as c:
            c.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))


def _unique_username(c, base: str) -> str:
    base = (base or "user").strip() or "user"
    name, i = base, 1
    while c.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE", (name,)).fetchone():
        i += 1
        name = f"{base}{i}"
    return name


def create_local_user(username: str, password: str, role: str = "user", email: str = "") -> dict | None:
    """Create a username/password local account. Returns None if the name is
    taken or blank."""
    from werkzeug.security import generate_password_hash
    username = (username or "").strip()
    if not username:
        return None
    with conn() as c:
        if c.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone():
            return None
        c.execute("INSERT INTO users (username, password_hash, role, email, last_login) "
                  "VALUES (?,?,?,?, datetime('now','localtime'))",
                  (username, generate_password_hash(password) if password else None, role, email or ""))
        return dict(c.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone())


def verify_local(username: str, password: str) -> dict | None:
    """Check a local username/password. None if no local password is set or it
    doesn't match."""
    from werkzeug.security import check_password_hash
    u = get_user_by_username(username)
    if not u or not u.get("password_hash"):
        return None
    if check_password_hash(u["password_hash"], password or ""):
        with conn() as c:
            c.execute("UPDATE users SET last_login=datetime('now','localtime') WHERE id=?", (u["id"],))
        return u
    return None


def set_password(user_id: int, password: str):
    from werkzeug.security import generate_password_hash
    with conn() as c:
        c.execute("UPDATE users SET password_hash=? WHERE id=?",
                  (generate_password_hash(password) if password else None, user_id))


def set_email(user_id: int, email: str):
    with conn() as c:
        c.execute("UPDATE users SET email=? WHERE id=?", (email or "", user_id))


def update_abs(user_id: int, abs_user_id: str, token: str):
    with conn() as c:
        c.execute("UPDATE users SET abs_user_id=?, abs_token=? WHERE id=?",
                  (abs_user_id, token, user_id))


# --- external provider links (abs / kavita / komga / calibreweb) -------------
def link_get(provider: str, external_id: str) -> int | None:
    with conn() as c:
        r = c.execute("SELECT user_id FROM user_links WHERE provider=? AND external_id=?",
                      (provider, str(external_id))).fetchone()
        return r["user_id"] if r else None


def link_set(provider: str, external_id: str, user_id: int, token: str = ""):
    with conn() as c:
        c.execute("INSERT INTO user_links (provider, external_id, user_id, token) VALUES (?,?,?,?) "
                  "ON CONFLICT(provider, external_id) DO UPDATE SET user_id=excluded.user_id, "
                  "token=excluded.token", (provider, str(external_id), user_id, token or ""))


def link_claim(provider: str, external_id: str, user_id: int, token: str = "") -> bool:
    """Atomically link a provider identity to user_id, refusing to reassign it if
    another account already owns it (only the token is refreshed for the owner).
    Returns True if user_id owns the link afterward. Fixes the check-then-set race
    where two concurrent links could steal an identity from each other."""
    with conn() as c:
        c.execute("INSERT INTO user_links (provider, external_id, user_id, token) VALUES (?,?,?,?) "
                  "ON CONFLICT(provider, external_id) DO UPDATE SET token=excluded.token "
                  "WHERE user_links.user_id=excluded.user_id",
                  (provider, str(external_id), user_id, token or ""))
        r = c.execute("SELECT user_id FROM user_links WHERE provider=? AND external_id=?",
                      (provider, str(external_id))).fetchone()
        return bool(r and r["user_id"] == user_id)


def claim_run(user_id: int, stale_seconds: int = 3600) -> bool:
    """Atomically claim the per-user recommender run. True = claimed (proceed);
    False = another run holds it. Self-heals a crashed run after stale_seconds.
    Replaces the read-then-write flag that let concurrent runs both proceed."""
    import time as _t
    k = f"running_{user_id}"
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO meta (k,v) VALUES (?, '0')", (k,))
        cur = c.execute(
            "UPDATE meta SET v=? WHERE k=? AND (v='' OR v='0' OR CAST(v AS REAL) < ?)",
            (str(_t.time()), k, _t.time() - stale_seconds))
        return cur.rowcount > 0


def release_run(user_id: int):
    set_meta(f"running_{user_id}", "0")


def link_remove(provider: str, user_id: int):
    with conn() as c:
        c.execute("DELETE FROM user_links WHERE provider=? AND user_id=?", (provider, user_id))


def links_for(user_id: int) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT provider, external_id, token FROM user_links WHERE user_id=?", (user_id,))]


def provision_provider_user(provider: str, external_id: str, username: str,
                            token: str = "", role: str = "user") -> dict:
    """Find-or-create the local account behind a provider identity and link it.
    Existing link → that account (kept). No link → a fresh local account,
    auto-linked. The local account is always the canonical identity."""
    uid = link_get(provider, external_id)
    if uid:
        u = get_user(uid)
        if u:
            with conn() as c:
                # never downgrade an admin; otherwise honour the provider's role
                c.execute("UPDATE users SET last_login=datetime('now','localtime'), "
                          "role=CASE WHEN role='admin' THEN 'admin' ELSE ? END WHERE id=?",
                          (role, u["id"]))
                if token:
                    c.execute("UPDATE user_links SET token=? WHERE provider=? AND external_id=?",
                              (token, provider, str(external_id)))
            return get_user(u["id"])
    with conn() as c:
        name = _unique_username(c, username)
        c.execute("INSERT INTO users (username, role, last_login) "
                  "VALUES (?,?, datetime('now','localtime'))", (name, role))
        new_uid = c.execute("SELECT id FROM users WHERE username=? COLLATE NOCASE", (name,)).fetchone()["id"]
        # Claim the link without clobbering a concurrent first-login that beat us
        # to it; if we lost the race, discard our orphan account and return theirs.
        c.execute("INSERT INTO user_links (provider, external_id, user_id, token) VALUES (?,?,?,?) "
                  "ON CONFLICT(provider, external_id) DO NOTHING",
                  (provider, str(external_id), new_uid, token or ""))
        owner = c.execute("SELECT user_id FROM user_links WHERE provider=? AND external_id=?",
                          (provider, str(external_id))).fetchone()["user_id"]
        if owner != new_uid:
            c.execute("DELETE FROM users WHERE id=?", (new_uid,))
    return get_user(owner)


# --- shared ratings & reviews (community signal across all Stackarr users) ---
def community_rating(key: str) -> dict:
    """Aggregate star rating for a book across every user. {avg, count}."""
    with conn() as c:
        r = c.execute("SELECT ROUND(AVG(stars),1) a, COUNT(*) n FROM ratings WHERE asin=?",
                      (key,)).fetchone()
    return {"avg": r["a"] or 0, "count": r["n"] or 0}


def reviews_for(key: str) -> list[dict]:
    """Text reviews other users have left for a book, most-helpful first then
    newest. Carries id (for voting), spoiler flag, and helpful-vote count."""
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT r.id, r.stars, r.review, r.spoiler, r.created_at, u.username, "
            "(SELECT COUNT(*) FROM review_votes v WHERE v.rating_id=r.id) AS votes "
            "FROM ratings r JOIN users u ON u.id=r.user_id "
            "WHERE r.asin=? AND r.review<>'' ORDER BY votes DESC, COALESCE(r.updated_at,r.created_at) DESC",
            (key,))]


# --- personal shelves (want / reading / read) -------------------------------
def shelf_set(user_id: int, rkey: str, state: str, title="", author="", cover="", fmt="audiobook"):
    """Put a book on a shelf (want|reading|read). 'read' stamps finished_at
    (drives the goal + heatmap). state='' removes it from shelves."""
    with conn() as c:
        if not state:
            c.execute("DELETE FROM shelf WHERE user_id=? AND rkey=?", (user_id, rkey))
            return
        fin = "datetime('now','localtime')" if state == "read" else "NULL"
        c.execute(
            f"INSERT INTO shelf (user_id,rkey,state,title,author,cover,format,finished_at) "
            f"VALUES (?,?,?,?,?,?,?,{fin}) "
            f"ON CONFLICT(user_id,rkey) DO UPDATE SET state=excluded.state, "
            f"title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE shelf.title END, "
            f"author=CASE WHEN excluded.author<>'' THEN excluded.author ELSE shelf.author END, "
            f"cover=CASE WHEN excluded.cover<>'' THEN excluded.cover ELSE shelf.cover END, "
            f"format=excluded.format, "
            f"finished_at=CASE WHEN excluded.state='read' AND shelf.finished_at IS NULL "
            f"THEN datetime('now','localtime') WHEN excluded.state<>'read' THEN NULL ELSE shelf.finished_at END",
            (user_id, rkey, state, title, author, cover, fmt))


def shelf_state(user_id: int, rkey: str) -> str:
    with conn() as c:
        r = c.execute("SELECT state FROM shelf WHERE user_id=? AND rkey=?", (user_id, rkey)).fetchone()
        return r["state"] if r else ""


def shelf_list(user_id: int, state: str) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM shelf WHERE user_id=? AND state=? ORDER BY COALESCE(finished_at,added_at) DESC",
            (user_id, state))]


def shelf_counts(user_id: int) -> dict:
    with conn() as c:
        rows = c.execute("SELECT state, COUNT(*) n FROM shelf WHERE user_id=? GROUP BY state", (user_id,)).fetchall()
    return {r["state"]: r["n"] for r in rows}


def finished_dates(user_id: int) -> list[str]:
    """YYYY-MM-DD of every 'read'-shelf finish — feeds the goal ring + heatmap."""
    with conn() as c:
        return [r["d"] for r in c.execute(
            "SELECT substr(finished_at,1,10) d FROM shelf WHERE user_id=? AND state='read' AND finished_at IS NOT NULL",
            (user_id,))]


def finished_keyed(user_id: int) -> list[tuple]:
    """(rating_key, YYYY-MM-DD) for every 'read'-shelf finish, so callers can
    dedup a shelf finish against the same book finished in Audiobookshelf."""
    with conn() as c:
        return [(r["rkey"], r["d"]) for r in c.execute(
            "SELECT rkey, substr(finished_at,1,10) d FROM shelf "
            "WHERE user_id=? AND state='read' AND finished_at IS NOT NULL", (user_id,))]


# --- book tags (mood / pace / genre / content-warning) ----------------------
def set_tags(rkey: str, tags: list[tuple], replace_kind: str | None = None):
    """tags: list of (tag, kind). If replace_kind given, clear that kind's
    'auto' tags for this book first (so a re-fetch refreshes cleanly)."""
    with conn() as c:
        if replace_kind:
            c.execute("DELETE FROM book_tags WHERE rkey=? AND kind=? AND source='auto'", (rkey, replace_kind))
        for tag, kind in tags:
            t = (tag or "").strip()
            if t:
                c.execute("INSERT OR IGNORE INTO book_tags (rkey,tag,kind,source) VALUES (?,?,?,?)",
                          (rkey, t, kind, "auto"))


def tags_for(rkey: str) -> dict:
    with conn() as c:
        rows = c.execute("SELECT tag, kind FROM book_tags WHERE rkey=?", (rkey,)).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["kind"], []).append(r["tag"])
    return out


def has_tags(rkey: str) -> bool:
    with conn() as c:
        return bool(c.execute("SELECT 1 FROM book_tags WHERE rkey=? LIMIT 1", (rkey,)).fetchone())


def review_vote(user_id: int, rating_id: int) -> int:
    """Toggle a helpful vote; returns the new vote count for that review."""
    with conn() as c:
        ex = c.execute("SELECT 1 FROM review_votes WHERE user_id=? AND rating_id=?",
                       (user_id, rating_id)).fetchone()
        if ex:
            c.execute("DELETE FROM review_votes WHERE user_id=? AND rating_id=?", (user_id, rating_id))
        else:
            c.execute("INSERT OR IGNORE INTO review_votes (user_id,rating_id) VALUES (?,?)", (user_id, rating_id))
        return c.execute("SELECT COUNT(*) n FROM review_votes WHERE rating_id=?", (rating_id,)).fetchone()["n"]


def recent_ratings(limit: int = 14) -> list[dict]:
    """Most-recently-rated books across all users — the 'Recently rated'
    discovery row. One row per book (latest rating wins)."""
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT r.asin, r.title, r.author, r.stars, r.review, r.format, u.username, "
            "COALESCE(r.updated_at, r.created_at) AS ts "
            "FROM ratings r JOIN users u ON u.id=r.user_id "
            "WHERE r.title<>'' ORDER BY ts DESC LIMIT 120")]
    seen, out = set(), []
    for r in rows:
        k = (r["title"] or "").strip().lower()
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(r)
        if len(out) >= limit:
            break
    return out
