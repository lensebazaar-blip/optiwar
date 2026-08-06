/**
 * Optiwar AI Chat Widget — Phase 1A
 * Vanilla JS, polling-based, MariaDB source of truth.
 * Modes: small (default), half, full, closed
 */
(function() {
  'use strict';
  var _t0 = performance.now();
  console.log('[ChatWidget] Script executing at', _t0.toFixed(0), 'ms');

  var cfg = window.__optiwarChat || {};
  var userEmail = cfg.email || '';
  var userName = cfg.name || '';
  var customerId = cfg.customerId || null;
  if (!userEmail) return;

  var API = '/api/chat';
  var POLL_INTERVAL = 4000;
  var TYPING_TIMEOUT = 20000;
  var sessionId = null;
  var messages = [];
  var pollTimer = null;
  var typingTimer = null;
  var isOpen = false;
  var isTyping = false;
  var lastPollTime = null;
  var initialLoaded = false;  // history rendered once; later polls reconcile, never wipe
  var currentMode = 'small'; // small | half | full

  // ─── Styles ───
  var styles = document.createElement('style');
  styles.textContent = `
.ow-chat-btn{position:fixed;bottom:20px;right:20px;z-index:9999;width:clamp(60px,12vw,80px);height:clamp(60px,12vw,80px);border-radius:50%;border:none;background:radial-gradient(circle at center,#0f172a,#020617);cursor:pointer;box-shadow:0 4px 20px rgba(14,165,233,0.4),0 0 40px rgba(56,189,248,0.15);display:grid;place-items:center;transition:transform .2s;overflow:hidden;padding:0}
.ow-chat-btn:hover{transform:scale(1.1)}
.ow-chat-btn .ec-circle{position:absolute;border-radius:50%;border:2px solid rgba(56,189,248,.18)}.ow-chat-btn .ec-c1{width:38%;height:38%;animation:ecRingSpin 16s linear infinite}.ow-chat-btn .ec-c2{width:64%;height:64%;animation:ecRingSpinR 20s linear infinite}.ow-chat-btn .ec-c3{width:90%;height:90%;animation:ecRingSpin 24s linear infinite}.ow-chat-btn .ec-energy{position:absolute;inset:0;display:grid;place-items:center}.ow-chat-btn .ec-energy span{position:absolute;width:8px;height:8px;border-radius:50%;background:#fff;box-shadow:0 0 6px #67e8f9,0 0 12px #38bdf8,0 0 18px #0ea5e9}.ow-chat-btn .ec-orbit1{animation:ecOrbit 8s linear infinite}.ow-chat-btn .ec-orbit1 span{transform:translateY(-12px)}.ow-chat-btn .ec-orbit2{animation:ecOrbit 8s linear infinite 2.6s}.ow-chat-btn .ec-orbit2 span{transform:translateY(-20px)}.ow-chat-btn .ec-orbit3{animation:ecOrbit 8s linear infinite 5.2s}.ow-chat-btn .ec-orbit3 span{transform:translateY(-28px)}@keyframes ecRingSpin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}@keyframes ecRingSpinR{from{transform:rotate(360deg)}to{transform:rotate(0deg)}}@keyframes ecOrbit{0%{opacity:0;transform:rotate(0deg)}10%{opacity:1}40%{opacity:1}50%{opacity:0}100%{opacity:0;transform:rotate(360deg)}}
.ow-chat-panel{position:fixed;z-index:9998;background:#fff;display:none;flex-direction:column;overflow:hidden;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;transition:all .3s ease}
.ow-chat-panel.open{display:flex}
/* Small widget mode (default) */
.ow-chat-panel.ow-mode-small{bottom:88px;right:20px;width:370px;max-width:calc(100vw - 32px);height:520px;max-height:calc(100vh - 120px);border-radius:16px;box-shadow:0 8px 40px rgba(0,0,0,0.15)}
/* Half window mode — right half of screen */
.ow-chat-panel.ow-mode-half{top:0;right:0;width:50%;height:100vh;border-radius:0;box-shadow:-4px 0 20px rgba(0,0,0,0.1)}
/* Full screen mode — below site headers */
.ow-chat-panel.ow-mode-full{top:0;left:0;width:100%;height:100vh;border-radius:0;box-shadow:none}
.ow-chat-panel.ow-mode-nav{bottom:0;right:0;width:370px;max-width:calc(100vw - 32px);height:25vh;border-radius:16px 16px 0 0;box-shadow:0 -4px 20px rgba(0,0,0,0.12)}
.ow-chat-header{background:#1F93FF;color:#fff;padding:10px 12px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
.ow-chat-header-left{display:flex;align-items:center;gap:8px;min-width:0;flex-shrink:1}
.ow-chat-header-left .ow-avatar{width:32px;height:32px;border-radius:50%;background:rgba(255,255,255,0.2);display:flex;align-items:center;justify-content:center;flex-shrink:0}
.ow-chat-header-left .ow-avatar svg{width:18px;height:18px}
.ow-chat-header-info{min-width:0}
.ow-chat-header-info h3{margin:0;font-size:14px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ow-chat-header-info .ow-session-id{font-size:10px;opacity:0.8;margin-top:1px}
.ow-chat-toolbar{display:flex;align-items:center;gap:2px;flex-shrink:0}
.ow-chat-toolbar button{background:rgba(255,255,255,0.15);border:none;color:#fff;cursor:pointer;width:28px;height:28px;border-radius:6px;display:flex;align-items:center;justify-content:center;transition:background .2s;font-size:15px;line-height:1}
.ow-chat-toolbar button:hover{background:rgba(255,255,255,0.35)}
.ow-chat-toolbar button.ow-active{background:rgba(255,255,255,0.45)}
.ow-chat-messages{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:8px}
.ow-msg{max-width:100%;padding:10px 14px;border-radius:12px;font-size:14px;line-height:1.5;word-wrap:break-word}
.ow-msg a{color:inherit;text-decoration:underline}
.ow-msg a.ow-action-btn{display:inline-block;margin-top:6px;padding:8px 14px;background:#1F93FF;color:#fff;border-radius:8px;text-decoration:none;font-weight:600;font-size:13px}
.ow-msg a.ow-action-btn:hover{background:#177ae0}
.ow-msg-user{align-self:flex-end;background:#1F93FF;color:#fff;border-bottom-right-radius:4px}
.ow-msg-ai{align-self:flex-start;background:#f0f2f5;color:#1a1a1a;border-bottom-left-radius:4px}
.ow-msg-system{align-self:center;background:transparent;color:#666;font-size:12px;font-style:italic;text-align:center;max-width:100%}
.ow-typing{align-self:flex-start;padding:12px 16px;background:#f0f2f5;border-radius:12px;border-bottom-left-radius:4px}
.ow-typing span{display:inline-block;width:8px;height:8px;border-radius:50%;background:#999;margin:0 2px;animation:owBounce 1.4s ease-in-out infinite}
.ow-typing span:nth-child(2){animation-delay:0.2s}
.ow-typing span:nth-child(3){animation-delay:0.4s}
@keyframes owBounce{0%,60%,100%{transform:translateY(0)}30%{transform:translateY(-6px)}}
.ow-chat-input{display:flex;padding:12px;border-top:1px solid #e8e8e8;gap:8px;flex-shrink:0}
.ow-chat-input input{flex:1;border:1px solid #ddd;border-radius:20px;padding:10px 16px;font-size:14px;outline:none;transition:border-color .2s}
.ow-chat-input input:focus{border-color:#1F93FF}
.ow-chat-input button{width:36px;height:36px;border-radius:50%;border:none;background:#1F93FF;color:#fff;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.ow-chat-input button:disabled{background:#ccc;cursor:not-allowed}
.ow-unread{position:absolute;top:-4px;right:-4px;width:18px;height:18px;background:#e74c3c;border-radius:50%;font-size:10px;color:#fff;display:none;align-items:center;justify-content:center;font-weight:bold}
.ow-msg-wrap{display:flex;flex-direction:column;max-width:85%}
.ow-msg-wrap.ow-wrap-user{align-self:flex-end}
.ow-msg-wrap.ow-wrap-ai{align-self:flex-start}
.ow-msg-wrap.ow-wrap-system{align-self:center}
.ow-msg-time{font-size:10px;color:#999;margin-top:2px;padding:0 4px}
.ow-wrap-user .ow-msg-time{text-align:right}
.ow-wrap-ai .ow-msg-time{text-align:left}
.ow-wrap-system .ow-msg-time{text-align:center}
/* Reset confirm overlay */
.ow-reset-confirm{position:absolute;bottom:0;left:0;right:0;background:#fff;border-top:1px solid #e8e8e8;padding:16px;display:none;flex-direction:column;align-items:center;gap:10px;z-index:10}
.ow-reset-confirm.show{display:flex}
.ow-reset-confirm p{margin:0;font-size:14px;color:#333;text-align:center}
.ow-reset-confirm .ow-reset-btns{display:flex;gap:8px}
.ow-reset-confirm button{padding:8px 16px;border-radius:8px;border:none;font-size:13px;cursor:pointer;font-weight:500}
.ow-reset-confirm .ow-reset-yes{background:#e74c3c;color:#fff}
.ow-reset-confirm .ow-reset-no{background:#f0f2f5;color:#333}
@media(max-width:768px){
.ow-chat-panel.ow-mode-small{bottom:0;right:0;width:100%;height:50vh;max-width:100%;max-height:50vh;border-radius:16px 16px 0 0}
.ow-chat-panel.ow-mode-half{width:100%}
.ow-chat-panel.ow-mode-nav{width:100%;max-width:100%;height:25vh;max-height:25vh;border-radius:16px 16px 0 0}
}

.ow-choice-menu{position:fixed;bottom:90px;right:20px;z-index:9999;display:none;flex-direction:column;gap:10px;animation:owMenuFadeIn .2s ease}
.ow-choice-menu.open{display:flex}
.ow-choice-option{display:flex;align-items:center;gap:12px;padding:14px 20px;background:#fff;border:1.5px solid #e2e8f0;border-radius:14px;cursor:pointer;transition:all .2s;box-shadow:0 4px 16px rgba(0,0,0,0.08);min-width:180px}
.ow-choice-option:hover{border-color:#6366f1;background:#faf5ff;transform:translateY(-2px);box-shadow:0 8px 24px rgba(99,102,241,0.15)}
.ow-choice-option .ow-co-icon{width:40px;height:40px;border-radius:10px;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.ow-choice-option .ow-co-icon.text-icon{background:linear-gradient(135deg,#6366f1,#4f46e5)}
.ow-choice-option .ow-co-icon.voice-icon{background:linear-gradient(135deg,#0ea5e9,#0284c7)}
.ow-choice-option .ow-co-icon svg{width:20px;height:20px}
.ow-choice-option .ow-co-info h4{margin:0;font-size:14px;font-weight:600;color:#1e293b}
.ow-choice-option .ow-co-info p{margin:2px 0 0;font-size:11px;color:#64748b}
@keyframes owMenuFadeIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
@media(max-width:480px){.ow-choice-menu{bottom:85px;right:12px}}
`;
  document.head.appendChild(styles);

  // ─── DOM ───
  var btn = document.createElement('button');
  btn.className = 'ow-chat-btn';
  btn.setAttribute('aria-label', 'Chat with Optiwar AI');
  btn.innerHTML = '<div class="ec-circle ec-c1"></div><div class="ec-circle ec-c2"></div><div class="ec-circle ec-c3"></div><div class="ec-energy ec-orbit1"><span></span></div><div class="ec-energy ec-orbit2"><span></span></div><div class="ec-energy ec-orbit3"><span></span></div><span class="ow-unread" id="ow-unread"></span>';

  // ─── Choice Menu ───
  var choiceMenu = document.createElement('div');
  choiceMenu.className = 'ow-choice-menu';
  choiceMenu.id = 'ow-choice-menu';
  choiceMenu.innerHTML = '<div class="ow-choice-option" id="ow-choice-text"><div class="ow-co-icon text-icon"><svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg></div><div class="ow-co-info"><h4>Text Chat</h4><p>Type your questions</p></div></div><div class="ow-choice-option" id="ow-choice-voice"><div class="ow-co-icon voice-icon"><svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2"><path d="M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3z"/><path d="M19 10v2a7 7 0 01-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg></div><div class="ow-co-info"><h4>Voice Chat</h4><p>Talk with AI assistant</p></div></div>';
  document.body.appendChild(choiceMenu);

  var panel = document.createElement('div');
  panel.className = 'ow-chat-panel ow-mode-small';
  panel.innerHTML = `
<div class="ow-chat-header">
  <div class="ow-chat-header-left">
    <div class="ow-chat-header-info">
      <h3>Optiwar AI Assistant</h3>
      <div class="ow-session-id" id="ow-session-label"></div>
    </div>
  </div>
  <div class="ow-chat-toolbar">
    <button id="ow-mode-full" title="Full Page">&#x26F6;</button>
    <button id="ow-mode-half" title="Half Window">&#x25E8;</button>
    <button id="ow-mode-small" title="Small Widget">&#x2B1C;</button>
    <button id="ow-reset-btn" title="Reset Chat">&#x21BB;</button>
    <button id="ow-close" title="Close">&#x2715;</button>
  </div>
</div>
<div class="ow-chat-messages" id="ow-messages"></div>
<div class="ow-reset-confirm" id="ow-reset-overlay">
  <p>Start a fresh conversation?<br><span style="font-size:12px;color:#666">Current chat will be resolved.</span></p>
  <div class="ow-reset-btns">
    <button class="ow-reset-yes" id="ow-reset-yes">Yes, reset</button>
    <button class="ow-reset-no" id="ow-reset-no">Cancel</button>
  </div>
</div>
<div class="ow-chat-input"><input type="text" id="ow-input" placeholder="Type a message..." autocomplete="off"><button id="ow-send" aria-label="Send"><svg width="18" height="18" viewBox="0 0 24 24" fill="white"><path d="M2 21l21-9L2 3v7l15 2-15 2z"/></svg></button></div>
`;

  document.body.appendChild(btn);
  document.body.appendChild(panel);

  var msgContainer = document.getElementById('ow-messages');
  var input = document.getElementById('ow-input');
  var sendBtn = document.getElementById('ow-send');
  var closeBtn = document.getElementById('ow-close');
  var sessionLabel = document.getElementById('ow-session-label');
  var unreadBadge = document.getElementById('ow-unread');
  var modeFullBtn = document.getElementById('ow-mode-full');
  var modeHalfBtn = document.getElementById('ow-mode-half');
  var modeSmallBtn = document.getElementById('ow-mode-small');
  var resetBtn = document.getElementById('ow-reset-btn');
  var resetOverlay = document.getElementById('ow-reset-overlay');
  var resetYes = document.getElementById('ow-reset-yes');
  var resetNo = document.getElementById('ow-reset-no');

  // ─── Events ───
  var choiceMenuOpen = false;
  btn.onclick = function() {
    choiceMenuOpen = !choiceMenuOpen;
    choiceMenu.classList.toggle('open', choiceMenuOpen);
  };

  // Choice menu options
  document.getElementById('ow-choice-text').onclick = function() {
    choiceMenuOpen = false;
    choiceMenu.classList.remove('open');
    try { localStorage.setItem('ow_chat_mode', 'text'); } catch(e) {}
    togglePanel(true);
  };
  document.getElementById('ow-choice-voice').onclick = function() {
    choiceMenuOpen = false;
    choiceMenu.classList.remove('open');
    try { localStorage.setItem('ow_chat_mode', 'voice'); } catch(e) {}
    if (window.owVoiceWidget) window.owVoiceWidget.open();
  };

  // Close choice menu when clicking elsewhere
  document.addEventListener('click', function(e) {
    if (choiceMenuOpen && !btn.contains(e.target) && !choiceMenu.contains(e.target)) {
      choiceMenuOpen = false;
      choiceMenu.classList.remove('open');
    }
  });

  // Auto-open chat if navigated here by AI (hash #owchat) — use compact nav mode
  if (window.location.hash === '#owchat') {
    setTimeout(function() {
      setMode('nav');
      togglePanel(true);
    }, 600);
  }
  closeBtn.onclick = function() { togglePanel(false); };
  sendBtn.onclick = sendMessage;
  input.onkeydown = function(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  };

  // Mode buttons
  modeFullBtn.onclick = function() { setMode('full'); };
  modeHalfBtn.onclick = function() { setMode('half'); };
  modeSmallBtn.onclick = function() { setMode('small'); };

  // Reset chat
  resetBtn.onclick = function() { resetOverlay.classList.add('show'); };
  resetNo.onclick = function() { resetOverlay.classList.remove('show'); };
  resetYes.onclick = function() {
    resetOverlay.classList.remove('show');
    resetChat();
  };

  function setMode(mode) {
    currentMode = mode;
    panel.classList.remove('ow-mode-small', 'ow-mode-half', 'ow-mode-full', 'ow-mode-nav');
    panel.classList.add('ow-mode-' + mode);
    // Highlight active button
    modeFullBtn.classList.toggle('ow-active', mode === 'full');
    modeHalfBtn.classList.toggle('ow-active', mode === 'half');
    modeSmallBtn.classList.toggle('ow-active', mode === 'small');
    // Hide chat button when panel is open or in non-small mode
    btn.style.display = (isOpen || mode !== 'small') ? 'none' : 'flex';
    scrollToBottom();
  }

  function togglePanel(open) {
    console.log('[ChatWidget] togglePanel(' + open + ') at', (performance.now() - _t0).toFixed(0), 'ms');
    isOpen = open;
    panel.classList.toggle('open', open);
    if (open) {
      // Always hide button and choice menu when panel is open
      btn.style.display = 'none';
      choiceMenu.classList.remove('open');
      choiceMenuOpen = false;
      unreadBadge.style.display = 'none';
      if (!sessionId) startSession();
      else startPolling();
      setTimeout(function() { input.focus(); }, 100);
    } else {
      stopPolling();
      // Reset to small mode when closing
      setMode('small');
      btn.style.display = 'grid';
    }
  }

  function resetChat() {
    // Resolve current session
    if (sessionId) {
      apiCall('POST', '/resolve', { session_id: sessionId }).catch(function() {});
    }
    // Clear state
    sessionId = null;
    messages = [];
    lastPollTime = null;
    initialLoaded = false;
    stopPolling();
    msgContainer.innerHTML = '';
    sessionLabel.textContent = '';
    // Start fresh
    startSession();
  }

  // ─── API ───
  function apiCall(method, path, body) {
    var opts = { method: method, headers: { 'Content-Type': 'application/json' } };
    if (body) opts.body = JSON.stringify(body);
    return fetch(API + path, opts).then(function(r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }

  function startSession() {
    console.log('[ChatWidget] startSession() at', (performance.now() - _t0).toFixed(0), 'ms after script load');
    apiCall('POST', '/start', {
      email: userEmail,
      name: userName,
      customer_id: customerId,
      page_url: window.location.href
    }).then(function(data) {
      sessionId = data.session_id;
      sessionLabel.textContent = 'Session: ' + sessionId.replace('chat_', '#');
      loadMessages();
      startPolling();
    }).catch(function() {
      renderSystemMsg('Failed to connect. Please refresh the page.');
    });
  }

  function _newClientMsgId() {
    try { if (window.crypto && crypto.randomUUID) return crypto.randomUUID(); } catch (e) {}
    return 'cm_' + Date.now() + '_' + Math.random().toString(16).slice(2);
  }

  // Send with ONE soft-retry on the retryable AI_TEMPORARILY_UNAVAILABLE 503.
  // The customer message is rendered once and carries a stable client_message_id
  // that the retry reuses (backend is idempotent on it) so no duplicate appears.
  function sendMessage() {
    var text = input.value.trim();
    if (!text || !sessionId || isTyping) return;

    input.value = '';
    renderMsg('user', text);
    showTyping();

    var cmid = _newClientMsgId();

    function attempt(triesLeft) {
      fetch(API + '/message', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId,
          content: text,
          page_url: window.location.href,
          client_message_id: cmid
        })
      }).then(function(r) {
        return r.json().catch(function() { return {}; }).then(function(data) {
          return { status: r.status, ok: r.ok, data: data };
        });
      }).then(function(res) {
        var err = res.data && res.data.error;
        var retryable = res.status === 503 && err && err.code === 'AI_TEMPORARILY_UNAVAILABLE' && err.retryable;
        if (retryable && triesLeft > 0) {
          var wait = Math.max(1, parseInt((err.retry_after_seconds || 3), 10)) * 1000;
          renderSystemMsg('Assistant is busy\u2014retrying shortly\u2026');
          setTimeout(function() { attempt(triesLeft - 1); }, wait);
          return;
        }
        if (!res.ok) { onFinalFailure(retryable); return; }
        onSuccess(res.data);
      }).catch(function() {
        // Network error: treat as non-retryable here (avoid duplicate sends on flaky links)
        onFinalFailure(false);
      });
    }

    function onSuccess(data) {
      hideTyping();
      if (data.reply) {
        var replyTime = new Date().toISOString();
        messages.push({ source: 'ai', content: data.reply, id: data.message_id || Date.now(), created_at: replyTime });
        renderMsgDirect('ai', data.reply, false, replyTime);
        scrollToBottom();
        lastPollTime = replyTime;
      }
      if (data.actions && data.actions.indexOf('human_handover') >= 0) {
        renderSystemMsg('Connecting you to our support team...');
      }
      if (data.navigate_url) {
        var acrAction = data.action && data.action.action_id ? data.action : null;
        setTimeout(function() {
          reportActionResult(acrAction, true, 'dispatched');
          window.location.href = data.navigate_url;
        }, 1500);
      }
    }

    // ACR A1: tell the server whether a structured action actually executed.
    // Uses sendBeacon so it survives the page unload that navigation triggers.
    function reportActionResult(action, success, code) {
      if (!action || !action.action_id) return;
      var payload = JSON.stringify({
        session_id: sessionId, action_id: action.action_id,
        success: !!success, failure_code: success ? null : code
      });
      try {
        if (navigator.sendBeacon) {
          navigator.sendBeacon(API + '/action-result', new Blob([payload], { type: 'application/json' }));
          return;
        }
      } catch (e) {}
      try { fetch(API + '/action-result', { method: 'POST', keepalive: true,
        headers: { 'Content-Type': 'application/json' }, body: payload }); } catch (e) {}
    }

    function onFinalFailure(wasBusy) {
      hideTyping();
      if (wasBusy) {
        renderSystemMsg('The assistant is still busy. Your message was kept\u2014tap send to try again.');
      } else {
        renderMsg('ai', "Sorry, I'm having trouble right now. Please try again.");
      }
      // Preserve the customer's typed text so they can resend without retyping.
      if (!input.value) input.value = text;
    }

    attempt(1);
  }

  function loadMessages() {
    var url = '/messages/' + sessionId;
    if (lastPollTime) url += '?since=' + encodeURIComponent(lastPollTime);
    apiCall('GET', url).then(function(data) {
      if (!initialLoaded) {
        // First load renders existing server history exactly once. Only clear the
        // container when there is history to paint, so an empty new session never
        // wipes a client-rendered notice (e.g. a retry/busy note shown before the
        // first poll returns). After this, we never replace the container again.
        if (data.messages.length > 0) {
          messages = data.messages.slice();
          msgContainer.innerHTML = '';
          data.messages.forEach(function(m) {
            var type = m.source === 'customer' ? 'user' : (m.source === 'system' ? 'system' : 'ai');
            renderMsgDirect(type, m.content, true, m.created_at);
          });
          scrollToBottom();
        }
        initialLoaded = true;
      } else {
        // Reconcile by stable id (fallback content+source): append only genuinely
        // new server messages. The container is never rebuilt, so transient
        // client-only notices (retry / still-busy) survive normal polling.
        var newMsgs = data.messages.filter(function(m) {
          if (m.source === 'customer') return false;
          return !messages.some(function(ex) {
            return ex.id === m.id || (ex.content === m.content && ex.source === m.source);
          });
        });
        newMsgs.forEach(function(m) {
          messages.push(m);
          var type = m.source === 'customer' ? 'user' : (m.source === 'system' ? 'system' : 'ai');
          renderMsgDirect(type, m.content, false, m.created_at);
          if (!isOpen && m.source !== 'customer') showUnread();
        });
        if (newMsgs.length > 0) {
          hideTyping();
          scrollToBottom();
        }
      }
      if (data.messages.length > 0) {
        lastPollTime = data.messages[data.messages.length - 1].created_at;
      }
      if (data.session_status === 'resolved') {
        handleResolved();
      }
    }).catch(function() {});
  }

  // ─── Polling ───
  function startPolling() {
    stopPolling();
    pollTimer = setInterval(loadMessages, POLL_INTERVAL);
  }
  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  // Immediately poll when tab becomes visible again (browsers throttle timers in background tabs)
  document.addEventListener('visibilitychange', function() {
    if (!document.hidden && isOpen && sessionId) {
      loadMessages();
    }
  });
  window.addEventListener('focus', function() {
    if (isOpen && sessionId) {
      loadMessages();
    }
  });

  // ─── Render ───
  function renderMsg(type, content) {
    var now = new Date().toISOString();
    messages.push({ source: type === 'user' ? 'customer' : type, content: content, id: Date.now(), created_at: now });
    renderMsgDirect(type, content, false, now);
    scrollToBottom();
  }

  function renderMsgDirect(type, content, skipScroll, timestamp) {
    var wrap = document.createElement('div');
    wrap.className = 'ow-msg-wrap ow-wrap-' + (type === 'user' ? 'user' : (type === 'system' ? 'system' : 'ai'));
    var div = document.createElement('div');
    if (type === 'system') {
      div.className = 'ow-msg ow-msg-system';
      div.textContent = content;
    } else {
      div.className = 'ow-msg ow-msg-' + (type === 'user' ? 'user' : 'ai');
      div.innerHTML = formatContent(content);
    }
    wrap.appendChild(div);
    var timeEl = document.createElement('div');
    timeEl.className = 'ow-msg-time';
    timeEl.textContent = formatTime(timestamp);
    wrap.appendChild(timeEl);
    msgContainer.appendChild(wrap);
    if (!skipScroll) scrollToBottom();
  }

  function renderSystemMsg(text) {
    renderMsgDirect('system', text, false);
    scrollToBottom();
  }

  function formatContent(text) {
    text = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    text = text.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    text = text.replace(/\[([^\]]+)\]\(([^)]+)\)/g, function(m, label, href) {
      // ACR fallback links start with the ▶ marker and render as action buttons.
      var cls = label.indexOf('\u25b6') === 0 ? ' class="ow-action-btn"' : '';
      return '<a href="' + href + '" target="_self"' + cls + '>' + label + '</a>';
    });
    text = text.replace(/(https?:\/\/[^\s<]+)/g, function(url) {
      if (text.indexOf('href="' + url) >= 0) return url;
      return '<a href="' + url + '" target="_self">' + url + '</a>';
    });
    text = text.replace(/\n/g, '<br>');
    return text;
  }

  function formatTime(ts) {
    var d;
    if (ts) {
      d = new Date(ts);
      if (isNaN(d.getTime())) d = new Date();
    } else {
      d = new Date();
    }
    var h = d.getHours();
    var m = d.getMinutes();
    var ampm = h >= 12 ? 'PM' : 'AM';
    h = h % 12;
    if (h === 0) h = 12;
    var mm = m < 10 ? '0' + m : '' + m;
    return h + ':' + mm + ' ' + ampm;
  }

  function showTyping() {
    isTyping = true;
    sendBtn.disabled = true;
    var el = document.createElement('div');
    el.className = 'ow-typing';
    el.id = 'ow-typing-indicator';
    el.innerHTML = '<span></span><span></span><span></span>';
    msgContainer.appendChild(el);
    scrollToBottom();
    typingTimer = setTimeout(function() {
      hideTyping();
      loadMessages();
    }, TYPING_TIMEOUT);
  }

  function hideTyping() {
    isTyping = false;
    sendBtn.disabled = false;
    if (typingTimer) { clearTimeout(typingTimer); typingTimer = null; }
    var el = document.getElementById('ow-typing-indicator');
    if (el) el.remove();
  }

  function showUnread() {
    unreadBadge.style.display = 'flex';
  }

  function scrollToBottom() {
    setTimeout(function() { msgContainer.scrollTop = msgContainer.scrollHeight; }, 50);
  }

  function handleResolved() {
    renderSystemMsg('This chat has been resolved. Starting a fresh session...');
    sessionId = null;
    messages = [];
    lastPollTime = null;
    initialLoaded = false;
    stopPolling();
    setTimeout(function() {
      msgContainer.innerHTML = '';
      startSession();
    }, 2000);
  }

  // ─── Init: check for existing session ───
  apiCall('GET', '/status?email=' + encodeURIComponent(userEmail)).then(function(data) {
    if (data.has_active) {
      sessionId = data.session_id;
      sessionLabel.textContent = 'Session: ' + sessionId.replace('chat_', '#');
    }
  }).catch(function() {});
})();
