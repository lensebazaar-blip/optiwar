/* Hostile-input escaping tests for the shared <picture> renderer (Phase 4 gate 8).
 * Run: node tests/test_ow_picture.js
 * Loads static/ow_picture.js in a minimal window/document shim and asserts that
 * NO caller-supplied string can break out of an attribute or inject markup. */
'use strict';
const fs = require('fs');
const path = require('path');

// Minimal shim so ow_picture.js (browser IIFE) can attach to window.
const window = {};
global.window = window;
const src = fs.readFileSync(path.join(__dirname, '..', 'static', 'ow_picture.js'), 'utf8');
// eslint-disable-next-line no-eval
eval(src);
const render = window.owPictureHTML;

let failures = 0;
function check(name, cond) {
  if (!cond) { failures++; console.error('FAIL:', name); }
  else console.log('ok  :', name);
}

const baseMedia = {
  src: '/static/catalog/BB02/BB02_1.jpg?v=abc123',
  zoom: '/static/catalog/BB02/BB02_1.jpg?v=abc123',
  has_derivatives: true,
  avif: '/static/x-200.avif?v=abc123 200w, /static/x-400.avif?v=abc123 400w',
  webp: '/static/x-200.webp?v=abc123 200w',
  jpg: '/static/x-200.jpg?v=abc123 200w'
};

// 1. hostile alt with quote + tag + event handler
let out = render(baseMedia, { alt: '"><img src=x onerror=alert(1)>', width: 400, height: 400 });
check('alt: no raw closing-quote breakout', !out.includes('alt=""><img'));
check('alt: quote escaped', out.includes('&quot;'));
check('alt: < escaped (no injected <img src=x)', !/onerror=alert/.test(out.replace(/&gt;|&lt;/g, '')) || !out.includes('<img src=x'));
check('alt: raw <img src=x not present', !out.includes('<img src=x onerror'));

// 2. hostile class value
out = render(baseMedia, { alt: 'ok', cls: 'a" onmouseover="evil()', width: 400, height: 400 });
check('cls: quote escaped, no onmouseover attribute', !/ onmouseover="evil/.test(out));

// 3. hostile & ampersand in alt
out = render(baseMedia, { alt: 'Tom & Jerry <b>', width: 400, height: 400 });
check('alt: ampersand escaped to &amp;', out.includes('Tom &amp; Jerry'));
check('alt: <b> escaped', out.includes('&lt;b&gt;') && !out.includes('<b>'));

// 4. non-numeric width/height coerced to default (no attribute breakout / NaN)
out = render(baseMedia, { alt: 'ok', width: '400" onload="x', height: 'abc' });
check('width: hostile string not emitted verbatim', !out.includes('onload="x'));
check('width: coerced to numeric default 400', /width="400"/.test(out));
check('height: non-numeric coerced to 400', /height="400"/.test(out));

// 5. zoom option emits ONLY the fixed handler, never arbitrary
out = render(baseMedia, { alt: 'ok', zoom: true, width: 400, height: 400 });
check('zoom: fixed onclick present', out.includes('onclick="openZoom(this)"'));
out = render(baseMedia, { alt: 'ok', zoom: false, extra: 'onclick="evil()"', width: 400, height: 400 });
check('extra option is ignored (deprecated/removed)', !out.includes('evil()'));

// 6. hostile src/zoom from a compromised media object are attribute-escaped
out = render({ src: '/x.jpg"><script>alert(1)</script>', has_derivatives: false }, { alt: 'ok', width: 400, height: 400 });
check('src: quote escaped (no attribute breakout)', !out.includes('.jpg"><script>'));
check('src: <script> not injected as raw markup', !out.includes('<script>alert(1)'));

// 7. missing/empty media returns empty string (no crash)
check('null media -> empty string', render(null, {}) === '');
check('media without src -> empty string', render({ has_derivatives: true, avif: 'x 200w' }, {}) === '');

// 8. no-derivatives path returns a bare <img> (still escaped)
out = render({ src: '/static/a.jpg', has_derivatives: false }, { alt: '<x>', width: 400, height: 400 });
check('no-deriv: bare <img>, alt escaped', out.startsWith('<img') && out.includes('&lt;x&gt;') && !out.includes('<picture>'));

console.log('\n' + (failures === 0 ? 'ALL PASS' : failures + ' FAILURE(S)'));
process.exit(failures === 0 ? 0 : 1);
