/* Errors customers meet in the browser, sent to the server so admin@optiwar.com
   hears about them (error_alerts.py). This site's own code and requests only:
   other sites' scripts and offline visitors are not ours to fix. */
(function () {
  var ENDPOINT = '/api/chat/dev-defect', MAX = 5, sent = 0, seen = {};
  var realFetch = window.fetch && window.fetch.bind(window);
  var here = location.origin;

  function report(code, where) {
    var text = String(where || '').slice(0, 120), key = code + '|' + text;
    if (seen[key] || sent >= MAX || !realFetch) return;
    seen[key] = 1; sent++;
    realFetch(ENDPOINT, {
      method: 'POST', credentials: 'same-origin', keepalive: true,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: code, where: text, page: location.pathname })
    }).catch(function () {});
  }
  window.owReportError = report;

  function ours(url) { return String(url || '').indexOf(here) === 0; }
  function short(url) { return String(url || '').replace(here, '').split('?')[0]; }

  window.addEventListener('error', function (e) {
    var el = e.target;
    if (el && el !== window && (el.tagName === 'SCRIPT' || el.tagName === 'LINK')) {
      if (ours(el.src || el.href)) report('SITE_ASSET_FAILED', short(el.src || el.href));
      return;
    }
    if (!ours(e.filename)) return;
    report('SITE_JS_ERROR', short(e.filename) + ':' + e.lineno + ' ' + e.message);
  }, true);

  window.addEventListener('unhandledrejection', function (e) {
    var r = e.reason || {};
    if (String(r.stack || '').indexOf(here) < 0) return;
    report('SITE_PROMISE_ERROR', (r.message || String(r)));
  });

  function httpFailed(status, method, url) {
    if (status >= 500 || status === 413) report('SITE_HTTP_' + status, method + ' ' + short(url));
  }
  function absolute(url) { try { return new URL(url, location.href).href; } catch (x) { return ''; } }

  if (realFetch) {
    window.fetch = function (input, init) {
      var url = absolute(input && input.url ? input.url : input);
      var method = (init && init.method) || (input && input.method) || 'GET';
      return realFetch(input, init).then(function (r) {
        if (ours(url)) httpFailed(r.status, method, url);
        return r;
      });
    };
  }
  if (window.XMLHttpRequest) {
    var open = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (method, url) {
      var target = absolute(url), xhr = this;
      if (ours(target)) xhr.addEventListener('loadend', function () { httpFailed(xhr.status, method, target); });
      return open.apply(xhr, arguments);
    };
  }
})();
