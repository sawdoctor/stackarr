# Changelog

All notable changes to Stackarr.

## [1.6.7] - 2026-07-04

Fixes from a user field report (thanks Mr Hynesy).

### Fixed
- **Star ratings now stick.** Rating a library book (Suggestions onboarding /
  History) wrote under a *recomputed* slug that differed from the key the page
  rendered from, so the write landed somewhere History never read back — it
  looked saved but wasn't. `api_rate` now trusts the canonical `t-…` key the
  client sends instead of recomputing it.
- **Rating errors are no longer swallowed.** The front-end showed a success
  toast regardless of the response; `rate()` / `submitReview()` now check the
  result, surface the real error, and revert the optimistic star fill on failure.
- **Collapsed sidebar can be expanded again.** The collapse toggle hid *itself*
  when collapsed, trapping you in the narrow rail. It now stays visible and flips
  its arrow (« ⇄ »).
- **Sidebar stays put.** As a stretched flex child it scrolled away despite
  `position: sticky`; `align-self: flex-start` lets sticky pin it.
- **Home "Read" shelf populates.** Finished audiobooks/ebooks now seed the Read
  shelf the same way in-progress titles seed Reading, so it matches the
  read-this-year count instead of sitting at 0.

### Security
- **Settings → Reading is admin-gated.** The shared Goodreads RSS / Hardcover
  token were rendered to every signed-in user (read-only; writes were already
  admin-only). The panel and the values are now admin-only.

### UI
- Native `prompt()` dialogs (library sign-in, deny-request reason) replaced with
  an in-app modal that matches the rest of the UI.

## [1.6.0] - 2026-06-14

### Recommendation quality (deterministic, no AI) — see [RECOMMENDATIONS.md](RECOMMENDATIONS.md)
- **Mood/pace matching** — a taste profile (your picked vibes + the moods of what
  you read, derived from catalogue subjects) scores every candidate by mood
  overlap. New "Matches your mood" lane.
- **Serendipity** — a bonus for well-rated, lesser-known books + an "Off the
  beaten path" lane, to beat the "too obvious" problem.
- **Adventurousness dial** (Settings) — Comfort ↔ Discovery.
- **Smarter negatives** — a DNF nudges down the whole *mood*, not just the title.
- **Mood-aware cold start** — a vibe picker shapes picks before any history.
- **Format-isolated taste** by default (audiobook vs ebook), cross-format opt-in.
  Light household collaborative boost.

### Reading features
- **My shelves** (Want / Reading / Read) + a **reading-goal ring**.
- **Insights** rebuilt: format split, GitHub-style **activity heatmap**, goal
  ring, **mood profile**.
- **Author** and **narrator** pages (Follow an author → new-release radar);
  **Upcoming** page; **Surprise me**; **series completion bars** on Up Next.
- **Reviews** gain helpful-votes + spoiler tags; **mood tags** + **content
  warnings** on book pages; **More / Less like this**; **get the other format**.
- **Check libraries** re-scans ABS/Kavita/Calibre for books added outside
  Chaptarr; requests skip Chaptarr if already owned.

### UI
- Format filter + per-card format **icons** across Suggestions / Up Next /
  Requests / History / Shelves / Upcoming; mobile header + request-row redesign.

## [1.5.1] - 2026-06-13
- Removed the "Recently rated by readers" row from the home page.

## [1.5.0] - 2026-06-13

### Added
- **Multi-format support: eBooks alongside audiobooks.** A format toggle
  (Settings → General → Library format): `audiobook` (default), `ebook`, or
  `both`. Default keeps existing installs unchanged until flipped.
- **Pluggable library backends.** New `backends/` package with a `Backend`
  interface; Audiobookshelf stays the login + audiobook source, with **Kavita**
  (API key — reliable per-user reading progress) and **Calibre-Web** (OPDS;
  read/unread only — the UI says so) as connected eBook sources in Settings →
  Connections.
- **eBook recommendation engine** (`recommend_ebook.py`): seeds from connected
  eBook sources' reading history + the Hardcover *read* shelf; candidates from
  **Google Books + Open Library** (no API key); author-backlist, subject-similar,
  reading-list and popular lanes (subject fallback filters out public-domain
  classics so picks stay contemporary).
- **Format-aware UI** in `both` mode: per-card format badges + an All/Audiobooks/
  eBooks filter; single-format installs never render the other format's chrome.
  eBook picks hand off to Chaptarr in the eBook media type/profiles.
- **Shared ratings & reviews**: community average ★ and everyone's written
  reviews on each book page, plus a **Recently rated by readers** home row.
- **New-release radar**: optional alert when an author you rate 4–5★ publishes
  (Settings → Notifications; off by default).

### Fixed / hardened
- Book detail pages fall back to cached title/author and **retry** the keyless
  eBook catalogue fetches, so a flaky Google Books / Open Library call no longer
  shows "Unknown".
- Audited with 882 automated click-throughs across every page/function
  (0 console / HTTP / server errors).

## [1.4.0] - 2026-06-13

### Added
- **Up Next** (new nav item): a series tracker — every series you're collecting,
  how far you are, and the next book with its state (in library / requested /
  ready to add). Built from your library's series metadata (read straight from
  Audiobookshelf) plus the engine's series picks.
- **Taste** (new nav item): see and undo everything that shapes your picks —
  ratings, **did-not-finish**, passed/ignored, already-read seeds, and removed
  books — in one place. DNF is a new negative signal.
- **Quick-rate onboarding**: when your ratings are sparse, the home page shows a
  card to rate books you've already listened to, instantly sharpening picks.
- **Availability notifications**: get told when a requested book lands in your
  Audiobookshelf library. Off by default; fans out across email / Discord /
  Apprise / a new **custom webhook** channel.
- **Auto-add to Chaptarr** (Settings → Suggestions): optional tiered
  auto-approval — Off (default) / Conservative (next-in-series) / Moderate
  (series + loved authors + reading list) / Aggressive (any strong pick), each
  capped per cycle. Skips owned books and pauses if Chaptarr can't add.
- Library now stores **series, sequence, and narrator** (from Audiobookshelf).

### Changed
- A round of **animation polish**: staggered list/card entrances, hover lift on
  series cards, a star "pop" on rating, nav micro-interactions — all suppressed
  under `prefers-reduced-motion`.

## [1.3.0] - 2026-06-13

### Changed
- **History & ratings** redesigned as a clean, scannable list:
  square cover thumbnail · title/author · a prominent 1–5★ rating on the right.
  Unrated books float to the top; rating one sinks it to the bottom "done" pile.
  Stars light up to the cursor on hover and fill in the app's accent colour.

### Added
- Rating now works for **library books with no ASIN** (most of them): ratings
  key on a stable title+author slug when there's no ASIN, so every read book is
  ratable. The title/author are captured on the rating so the recommender's
  author boost still applies.
- **Remove from history** (✕ on each row). A removed book is gone for good — it
  no longer shows in History even though it's still finished in Audiobookshelf,
  **and it no longer seeds suggestions**.
- **Settings → My reading → "Hide books from history after rating"**: when on,
  rating a book removes it from the list instead of sinking it to the bottom.

### Fixed
- **Goodreads reading-list import** was failing for everyone — Goodreads 403s the
  default `python-requests` User-Agent. Now sends a browser User-Agent; the
  public per-shelf RSS imports correctly.

## [1.2.0] - 2026-06-13

### Added
- **Android APK** — a configurable WebView client (enter your server URL on first
  launch) as an alternative to the PWA. Built by CI and attached to each Release.

## [1.1.0] - 2026-06-13

### Added
- **History & ratings** page (new sidebar item): every book you've finished in
  Audiobookshelf, rated, or marked read, shown as cards with an always-visible
  1–5★ rating control that feeds the recommender. Included in the demo.

### Fixed
- Reading-list import (`importlists.all_for_user`) now honours the Goodreads /
  Hardcover values set in **Settings** (DB), not just the env defaults — the
  in-app reading-list panel was previously inert.

## [1.0.0] - 2026-06-13
First stable release.

### Added
- Static **demo site** on GitHub Pages (`tools/build_demo.py` → `docs/`), built
  from the real templates with sample data and no backend.
- `RELEASING.md`: docs + demo regeneration are now mandatory release steps.

### Changed
- "Approve" renamed to **"Add book"** throughout.
- Home gains an **"Authors you might like"** card row (suggested authors only) →
  author grid with "add all books by this author".
- Docs accuracy pass.

### Fixed
- CI: `docker-publish` now sets up Buildx so the gha build cache works.

## [0.1.23-pre] - 2026-06-13
First public-candidate build (pre-release, vibecoded).

### Added
- Deterministic, **no-AI** recommendation engine across 13 lanes: series-next,
  authors you love, readers also enjoyed, new authors to discover, narrators,
  genres, hidden gems, award winners, short/epic listens, new & upcoming, and
  your reading list. Per-lane output with author diversity + popularity dampening.
- **Multi-user** login via Audiobookshelf accounts; per-user taste, approval queue.
- Approved picks handed to **Chaptarr**; nothing downloaded directly.
- Home merges personal lanes + genre cards + Recently Added / Recent Requests +
  a Discover gallery. Book detail pages, genre/author browse ("add all by author"),
  search typeahead, per-row "See all" grids.
- Insights (Spotify-wrapped style), 1–5★ ratings, "already read" seeding,
  Goodreads/Hardcover import, email (3 themes) / Discord / Apprise digests
  (off by default), in-app connection + SMTP settings with Test buttons, logs panel.
- Installable PWA, light/dark themes, responsive, nzb360-webview friendly.
- CI publishes the image to GHCR; Docker + compose.

### Notes
- Pre-release: not yet tested for general use.
