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

  /* A file the page failed to load is asked for once more. A server answer
     that is not OK is ours to fix and is reported with its status; a network
     drop or a content blocker is not, and the file is loaded again instead. */
  function recheck(el) {
    var url = el.src || el.href;
    if (!realFetch) return;
    realFetch(url, { cache: 'no-store', credentials: 'same-origin' }).then(function (r) {
      if (!r.ok) { report('SITE_ASSET_FAILED', short(url) + ' ' + r.status); return; }
      if (el.getAttribute('data-ow-retry') || !el.parentNode) return;
      var again = document.createElement(el.tagName);
      for (var i = 0; i < el.attributes.length; i++) again.setAttribute(el.attributes[i].name, el.attributes[i].value);
      again.setAttribute('data-ow-retry', '1');
      el.parentNode.insertBefore(again, el.nextSibling);
    }).catch(function () {});
  }

  window.addEventListener('error', function (e) {
    var el = e.target;
    if (el && el !== window && (el.tagName === 'SCRIPT' || el.tagName === 'LINK')) {
      if (ours(el.src || el.href)) recheck(el);
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
