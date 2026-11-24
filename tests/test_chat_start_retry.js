/* Run: node tests/test_chat_start_retry.js
 * Executes the widget's startSession function with deterministic fetch and
 * timer stubs so retry behavior stays covered without a browser. */
'use strict';
const fs = require('fs');
const path = require('path');

const repo = path.join(__dirname, '..');
const widget = fs.readFileSync(path.join(repo, 'static/js/chat-widget.js'), 'utf8');
const base = fs.readFileSync(path.join(repo, 'templates/base.html'), 'utf8');
const deploy = fs.readFileSync(path.join(repo, 'deploy/deploy.py'), 'utf8');

const lifecycleStart = widget.indexOf("  window.addEventListener('pagehide'");
const lifecycleEnd = widget.indexOf('\n\n  var API', lifecycleStart);
if (lifecycleStart < 0 || lifecycleEnd < 0) throw new Error('missing page lifecycle listeners');
const lifecycleSource = widget.slice(lifecycleStart, lifecycleEnd);

function extractFunction(source, signature) {
  const start = source.indexOf(signature);
  if (start < 0) throw new Error('missing function: ' + signature);
  const opening = source.indexOf('{', start);
  let depth = 0;
  for (let i = opening; i < source.length; i += 1) {
    if (source[i] === '{') depth += 1;
    if (source[i] === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error('unterminated function: ' + signature);
}

const startSessionSource = extractFunction(widget, 'function startSession()');
const failures = [];
function check(name, condition) {
  if (!condition) failures.push(name);
  console.log((condition ? 'ok  : ' : 'FAIL: ') + name);
}

function harness(apiCallImpl) {
  const state = { calls: [], timers: [], defects: [], messages: [], listeners: {} };
  const create = new Function('state', 'apiCallImpl', `
    var _t0 = 0;
    var performance = {now: function() { return 0; }};
    var console = {log: function() {}};
    var sessionId = null;
    var pendingEntry = 'support-entry';
    var pageLeaving = false;
    var sessionStartInProgress = false;
    var sessionLabel = {textContent: ''};
    var window = {
      location: {href: 'https://example.test/page'},
      addEventListener: function(name, callback) { state.listeners[name] = callback; }
    };
    ${lifecycleSource}
    function apiCall(method, path, body) {
      state.calls.push({method: method, path: path, body: body});
      return apiCallImpl(method, path, body);
    }
    function setTimeout(callback, delay) {
      state.timers.push({callback: callback, delay: delay});
      return state.timers.length;
    }
    function reportDefect(code, where) {
      state.defects.push({code: code, where: where});
    }
    function renderSystemMsg(text) { state.messages.push(text); }
    function loadMessages() {}
    function startPolling() {}
    ${startSessionSource}
    return {
      startSession: startSession,
      firePagehide: function() { state.listeners.pagehide(); },
      firePageshow: function(event) { state.listeners.pageshow(event); },
      setSessionId: function(value) { sessionId = value; },
      getSessionId: function() { return sessionId; }
    };
  `);
  state.widget = create(state, apiCallImpl);
  return state;
}

async function flushPromises() {
  for (let i = 0; i < 6; i += 1) await Promise.resolve();
}

async function run() {
  const outcomes = [
    () => Promise.reject(new TypeError('Failed to fetch')),
    () => Promise.reject(new TypeError('Failed to fetch')),
    () => Promise.reject(new TypeError('Failed to fetch'))
  ];
  const retries = harness(() => outcomes.shift()());
  retries.widget.startSession();
  retries.widget.startSession();
  await flushPromises();
  check('network failure schedules the first retry after 1000 ms',
        retries.calls.length === 1 && retries.timers[0].delay === 1000);
  retries.timers[0].callback();
  await flushPromises();
  check('second network failure schedules the retry after 3000 ms',
        retries.calls.length === 2 && retries.timers[1].delay === 3000);
  retries.timers[1].callback();
  await flushPromises();
  check('all retries preserve the original entry',
        retries.calls.length === 3 &&
        retries.calls.every(call => call.body.entry === 'support-entry'));
  check('defect is reported only after the third network failure',
        retries.defects.length === 1 &&
        retries.defects[0].code === 'CHAT_START_FAILED' &&
        retries.defects[0].where === 'Failed to fetch after 3 tries');
  check('existing failure message is rendered once after the third failure',
        retries.messages.length === 1 &&
        retries.messages[0] === 'Failed to connect. Please refresh the page.');

  const httpError = harness(() => Promise.reject(new Error('HTTP 500')));
  httpError.widget.startSession();
  await flushPromises();
  check('HTTP 500 is reported immediately without retry',
        httpError.calls.length === 1 && httpError.timers.length === 0 &&
        httpError.defects.length === 1 &&
        httpError.defects[0].where === 'HTTP 500');

  const leaving = harness(() => Promise.reject(new TypeError('Failed to fetch')));
  leaving.widget.startSession();
  await flushPromises();
  leaving.widget.firePagehide();
  leaving.timers[0].callback();
  await flushPromises();
  check('page departure suppresses retries, reports, and rendering',
        leaving.calls.length === 1 && leaving.defects.length === 0 &&
        leaving.messages.length === 0);

  const existing = harness(() => Promise.reject(new TypeError('Failed to fetch')));
  existing.widget.startSession();
  await flushPromises();
  existing.widget.setSessionId('chat_existing');
  existing.timers[0].callback();
  await flushPromises();
  check('retry stops when another session id is already set',
        existing.calls.length === 1 && existing.defects.length === 0 &&
        existing.messages.length === 0);

  const restored = harness(() => Promise.resolve({session_id: 'chat_restored'}));
  restored.widget.firePagehide();
  restored.widget.startSession();
  check('pagehide suppresses a new start until the page is restored',
        restored.calls.length === 0);
  restored.widget.firePageshow({persisted: true});
  restored.widget.startSession();
  await flushPromises();
  check('persisted pageshow allows startSession again',
        restored.calls.length === 1 &&
        restored.widget.getSessionId() === 'chat_restored');

  check('only pagehide marks departure and persisted pageshow clears it',
        /addEventListener\('pagehide', function\(\) \{\s*pageLeaving = true;/.test(widget) &&
        /addEventListener\('pageshow', function\(e\) \{\s*if \(e\.persisted\) pageLeaving = false;/.test(widget) &&
        !/addEventListener\('beforeunload'/.test(widget));
  check('widget cache-buster is v=33',
        /chat-widget\.js'\) \+ '\?v=33'/.test(base));
  check('widget remains in DEPLOY_SET',
        /"static\/js\/chat-widget\.js"/.test(deploy));

  if (failures.length) {
    console.error('\n' + failures.length + ' failure(s): ' + failures.join(', '));
    process.exitCode = 1;
  } else {
    console.log('\nALL PASS');
  }
}

run().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
