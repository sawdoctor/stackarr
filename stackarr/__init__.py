"""Stackarr app factory. Configures Flask for optional subpath/iframe use
(so it embeds cleanly in an nzb360 webview or behind a reverse proxy),
loads the DB, and starts the background worker."""
import logging
import sys
from datetime import timedelta

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from . import auth, config, db, scheduler


class _PrefixMiddleware:
    """Serve the app under URL_BASE without baking the prefix into routes."""
    def __init__(self, app, prefix=""):
        self.app, self.prefix = app, prefix

    def __call__(self, environ, start_response):
        if self.prefix and environ.get("PATH_INFO", "").startswith(self.prefix):
            environ["SCRIPT_NAME"] = self.prefix
            environ["PATH_INFO"] = environ["PATH_INFO"][len(self.prefix):] or "/"
        return self.app(environ, start_response)


def _setup_logging():
    import os
    from logging.handlers import RotatingFileHandler
    os.makedirs(config.DATA_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
    root.handlers.clear()
    console = logging.StreamHandler(); console.setFormatter(fmt); root.addHandler(console)
    fileh = RotatingFileHandler(config.LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fileh.setFormatter(fmt); root.addHandler(fileh)


def create_app() -> Flask:
    _setup_logging()
    problems = config.validate()
    if problems:
        for p in problems:
            logging.error("config: %s", p)
        sys.exit(1)

    db.init()
    app = Flask(__name__)
    app.secret_key = db.secret_key()
    app.permanent_session_lifetime = timedelta(days=90)
    # cookie hardening: HttpOnly (default), SameSite=Lax (CSRF mitigation),
    # Secure when served over HTTPS.
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=config.SECURE_COOKIES)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    if config.URL_BASE:
        app.wsgi_app = _PrefixMiddleware(app.wsgi_app, config.URL_BASE)

    # Allow embedding in nzb360 / dashboards (override Flask's default deny).
    @app.after_request
    def _security_headers(resp):
        resp.headers.pop("X-Frame-Options", None)   # CSP frame-ancestors supersedes it
        resp.headers["Content-Security-Policy"] = f"frame-ancestors {config.FRAME_ANCESTORS}"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        return resp

    from . import audible
    app.jinja_env.filters["hires"] = lambda u: audible._hi_res(u or "")

    from flask import request, abort
    from urllib.parse import urlparse

    @app.before_request
    def _csrf_guard():
        # CSRF: cookie-authenticated state changes must be same-origin. Requests
        # authenticated by header (X-Api-Key / KOReader kosync x-auth-*) carry no
        # session cookie, so they stay exempt — that's how automation isn't blocked.
        if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
            return
        origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
        if origin:
            if urlparse(origin).netloc and urlparse(origin).netloc != request.host:
                abort(403)                      # cross-origin → reject
        elif request.cookies.get(app.config.get("SESSION_COOKIE_NAME", "session")):
            # No Origin AND no Referer on a cookie-authed mutation: fail closed
            # rather than open. Real browsers always send one of the two same-origin
            # (Referrer-Policy is same-origin, so Referer is present); only forged
            # cross-site requests strip both.
            abort(403)

    from .routes import bp
    app.register_blueprint(bp)

    from . import formats

    @app.context_processor
    def _inject():
        u = auth.current_user()
        ctx = {"accent": config.ACCENT, "app_name": config.APP_NAME,
               "url_base": config.URL_BASE, "user": u,
               "version": config.VERSION, "stage": config.RELEASE_STAGE}
        ctx.update(formats.flags(u))    # per-user multi_format / show_audio / show_ebook / …
        return ctx

    if not config._bool("STACKARR_NO_SCHED", False):
        scheduler.start()
    return app
