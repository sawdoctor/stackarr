/* Static-demo shim: neutralises backend actions and shows a notice.
   Loaded only by the GitHub Pages build (never by the real app). */
(function () {
  // app.js exposes `Stackarr` as a global const (lexical binding), not window.Stackarr,
  // so we must override that same binding the inline page scripts call.
  if (typeof Stackarr === "undefined") return;
  var S = Stackarr;
  var MSG = "Static demo — install Stackarr and connect Audiobookshelf + Shelfmark to add real books.";
  function toast(m) {
    var t = document.getElementById("toast");
    if (!t) { alert(m); return; }
    t.textContent = m; t.className = "show";
    setTimeout(function () { t.className = ""; }, 3200);
  }
  ["decide", "bookRequest", "bookMarkRead", "bookIgnore", "addAllByAuthor", "scan", "rate", "removeFromHistory",
   "undoSignal", "clearRating", "markDnf", "dismissOnboard"].forEach(function (fn) {
    S[fn] = function () { toast(MSG); return false; };
  });
  S.initSearchSuggest = function () {};
  S.initDiscoverGallery = function () {
    var s = document.getElementById("home-discover-section");
    if (s) s.style.display = "none";
  };
  S.initSuggestions = function () {
    var l = document.getElementById("shelf-loader"); if (l) l.hidden = true;
    var c = document.getElementById("sugg-content"); if (c) c.hidden = false;
  };
  // demo has no service worker; clear any stale one so it never caches /api calls
  if (navigator.serviceWorker && navigator.serviceWorker.getRegistrations) {
    navigator.serviceWorker.getRegistrations().then(function (rs) { rs.forEach(function (r) { r.unregister(); }); });
  }
  // search box -> demo notice instead of a dead GET
  document.addEventListener("DOMContentLoaded", function () {
    var f = document.querySelector("form.search");
    if (f) f.addEventListener("submit", function (e) { e.preventDefault(); toast(MSG); });
    var r = document.createElement("a");
    r.className = "demo-ribbon"; r.href = "https://github.com/katalyst88/stackarr";
    r.innerHTML = "DEMO · sample data, actions disabled — <b>Get Stackarr ›</b>";
    document.body.appendChild(r);
  });
})();
