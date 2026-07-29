/**
 * Optiwar Voice Chat Widget
 * Uses browser SpeechRecognition for STT → DeepSeek AI → browser speechSynthesis/ElevenLabs for TTS
 */
(function() {
  'use strict';

  // ─── Config ───
  var cfg = window.__optiwarChat || {};
  var CUSTOMER_ID = cfg.customerId || null;
  var USER_EMAIL = cfg.email || '';
  var USER_NAME = cfg.name || '';

  // ─── State ───
  var voicePanel = null;
  var isListening = false;
  var isProcessing = false;
  var isSpeaking = false;
  var recognition = null;
  var voiceSessionId = null;
  var voiceHistory = [];
  var analyser = null;
  var audioCtx = null;
  var animFrame = null;
  var micStream = null;

  // ─── Create Voice Panel ───
  function createVoicePanel() {
    var styles = document.createElement('style');
    styles.textContent = `
.ow-voice-panel{position:fixed;z-index:9998;background:#0f172a;display:none;flex-direction:column;overflow:hidden;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;border-radius:20px;box-shadow:0 20px 60px rgba(0,0,0,0.5),0 0 40px rgba(56,189,248,0.1)}
.ow-voice-panel.open{display:flex}
.ow-voice-panel{bottom:20px;right:20px;width:360px;height:520px}
@media(max-width:480px){.ow-voice-panel{bottom:0;right:0;width:100%;height:60vh;border-radius:20px 20px 0 0}}

.ow-voice-header{display:flex;align-items:center;justify-content:space-between;padding:16px 20px;background:linear-gradient(135deg,#1e293b,#0f172a);border-bottom:1px solid rgba(56,189,248,0.1)}
.ow-voice-header h3{color:#e2e8f0;font-size:15px;margin:0;font-weight:600}
.ow-voice-header-btns{display:flex;gap:8px}
.ow-voice-header-btns button{background:rgba(255,255,255,0.08);border:none;color:#94a3b8;cursor:pointer;width:30px;height:30px;border-radius:8px;display:flex;align-items:center;justify-content:center;transition:all .2s;font-size:16px}
.ow-voice-header-btns button:hover{background:rgba(255,255,255,0.15);color:#fff}

.ow-voice-viz{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;position:relative;overflow:hidden}
.ow-voice-canvas{width:100%;height:200px}
.ow-voice-status{color:#94a3b8;font-size:13px;margin-top:12px;text-align:center;min-height:20px;transition:color .3s}
.ow-voice-status.active{color:#38bdf8}
.ow-voice-status.speaking{color:#a78bfa}

.ow-voice-orb{width:120px;height:120px;border-radius:50%;background:radial-gradient(circle at 30% 30%,#1e40af,#0f172a);box-shadow:0 0 40px rgba(56,189,248,0.2),inset 0 0 30px rgba(56,189,248,0.1);position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);transition:all .3s}
.ow-voice-orb.listening{box-shadow:0 0 60px rgba(56,189,248,0.5),inset 0 0 40px rgba(56,189,248,0.2);background:radial-gradient(circle at 30% 30%,#2563eb,#1e293b)}
.ow-voice-orb.speaking{box-shadow:0 0 60px rgba(167,139,250,0.5),inset 0 0 40px rgba(167,139,250,0.2);background:radial-gradient(circle at 30% 30%,#7c3aed,#1e293b)}
.ow-voice-orb.processing{animation:orbPulse 1.5s ease-in-out infinite}
@keyframes orbPulse{0%,100%{transform:translate(-50%,-50%) scale(1)}50%{transform:translate(-50%,-50%) scale(1.08)}}

.ow-voice-mic-btn{width:64px;height:64px;border-radius:50%;border:none;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .2s;margin-top:20px;position:relative;z-index:2}
.ow-voice-mic-btn.idle{background:linear-gradient(135deg,#3b82f6,#2563eb);box-shadow:0 4px 20px rgba(59,130,246,0.4)}
.ow-voice-mic-btn.listening{background:linear-gradient(135deg,#ef4444,#dc2626);box-shadow:0 4px 20px rgba(239,68,68,0.4);animation:micPulse 1s ease-in-out infinite}
.ow-voice-mic-btn.disabled{background:#475569;cursor:not-allowed;box-shadow:none}
.ow-voice-mic-btn svg{width:28px;height:28px}
@keyframes micPulse{0%,100%{transform:scale(1)}50%{transform:scale(1.05)}}

.ow-voice-transcript{max-height:140px;overflow-y:auto;padding:12px 20px;border-top:1px solid rgba(56,189,248,0.08);background:rgba(15,23,42,0.8)}
.ow-voice-transcript-item{padding:6px 0;font-size:12px;line-height:1.4;border-bottom:1px solid rgba(255,255,255,0.03)}
.ow-voice-transcript-item:last-child{border-bottom:none}
.ow-voice-transcript-item .ow-vt-label{font-weight:600;font-size:10px;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:2px}
.ow-voice-transcript-item.user .ow-vt-label{color:#38bdf8}
.ow-voice-transcript-item.ai .ow-vt-label{color:#a78bfa}
.ow-voice-transcript-item .ow-vt-text{color:#cbd5e1}
`;
    document.head.appendChild(styles);

    voicePanel = document.createElement('div');
    voicePanel.className = 'ow-voice-panel';
    voicePanel.innerHTML = `
<div class="ow-voice-header">
  <h3>Voice Assistant</h3>
  <div class="ow-voice-header-btns">
    <button id="ow-voice-close" title="Close">&times;</button>
  </div>
</div>
<div class="ow-voice-viz">
  <div class="ow-voice-orb" id="ow-voice-orb"></div>
  <canvas class="ow-voice-canvas" id="ow-voice-canvas"></canvas>
  <div class="ow-voice-status" id="ow-voice-status">Tap the mic to start talking</div>
  <button class="ow-voice-mic-btn idle" id="ow-voice-mic">
    <svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2"><path d="M12 1a3 3 0 00-3 3v8a3 3 0 006 0V4a3 3 0 00-3-3z"/><path d="M19 10v2a7 7 0 01-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>
  </button>
</div>
<div class="ow-voice-transcript" id="ow-voice-transcript"></div>
`;
    document.body.appendChild(voicePanel);

    // Events
    document.getElementById('ow-voice-close').onclick = closeVoicePanel;
    document.getElementById('ow-voice-mic').onclick = toggleListening;
  }

  // ─── Open/Close Voice Panel ───
  function openVoicePanel() {
    if (!voicePanel) createVoicePanel();
    voicePanel.classList.add('open');
    // Hide the choice menu
    var menu = document.getElementById('ow-choice-menu');
    if (menu) menu.style.display = 'none';
    // Hide main chat btn
    var chatBtn = document.querySelector('.ow-chat-btn');
    if (chatBtn) chatBtn.style.display = 'none';
    initAudioContext();
  }

  function closeVoicePanel() {
    if (voicePanel) voicePanel.classList.remove('open');
    stopListening();
    stopSilenceLoop();
    // Show main chat btn
    var chatBtn = document.querySelector('.ow-chat-btn');
    if (chatBtn) chatBtn.style.display = 'grid';
    if (animFrame) cancelAnimationFrame(animFrame);
    if (micStream) { micStream.getTracks().forEach(function(t){t.stop();}); micStream = null; }
  }

  // ─── Audio Context for Waveform ───
  function initAudioContext() {
    if (!audioCtx) {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
  }

  function startWaveform(stream) {
    micStream = stream;
    var source = audioCtx.createMediaStreamSource(stream);
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 256;
    source.connect(analyser);
    drawWaveform();
  }

  function drawWaveform() {
    var canvas = document.getElementById('ow-voice-canvas');
    if (!canvas || !analyser) return;
    var ctx = canvas.getContext('2d');
    var W = canvas.width = canvas.offsetWidth * 2;
    var H = canvas.height = canvas.offsetHeight * 2;
    ctx.scale(1, 1);

    var bufferLength = analyser.frequencyBinCount;
    var dataArray = new Uint8Array(bufferLength);

    function draw() {
      animFrame = requestAnimationFrame(draw);
      analyser.getByteFrequencyData(dataArray);

      ctx.clearRect(0, 0, W, H);

      var barWidth = (W / bufferLength) * 2;
      var x = 0;
      for (var i = 0; i < bufferLength; i++) {
        var barHeight = (dataArray[i] / 255) * H * 0.7;
        var hue = isListening ? 200 : (isSpeaking ? 270 : 210);
        var alpha = 0.4 + (dataArray[i] / 255) * 0.6;
        ctx.fillStyle = 'hsla(' + hue + ', 80%, 60%, ' + alpha + ')';
        ctx.fillRect(x, H - barHeight, barWidth - 1, barHeight);
        x += barWidth;
      }
    }
    draw();
  }

  function stopWaveform() {
    if (animFrame) { cancelAnimationFrame(animFrame); animFrame = null; }
    if (micStream) { micStream.getTracks().forEach(function(t){t.stop();}); micStream = null; }
    // Clear canvas
    var canvas = document.getElementById('ow-voice-canvas');
    if (canvas) {
      var ctx = canvas.getContext('2d');
      ctx.clearRect(0, 0, canvas.width, canvas.height);
    }
  }

  // ─── Speech Recognition ───
  function initRecognition() {
    var SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
      setStatus('Speech recognition not supported in this browser', '');
      return null;
    }
    recognition = new SpeechRecognition();
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.lang = 'en-US';

    recognition.onresult = function(event) {
      var transcript = '';
      for (var i = event.resultIndex; i < event.results.length; i++) {
        transcript += event.results[i][0].transcript;
      }
      setStatus(transcript, 'active');
      if (event.results[event.results.length - 1].isFinal) {
        stopListening();
        processVoiceInput(transcript);
      }
    };

    recognition.onerror = function(event) {
      if (event.error === 'no-speech') {
        setStatus('No speech detected. Tap mic to try again.', '');
      } else if (event.error === 'not-allowed') {
        setStatus('Microphone access denied. Please allow mic access.', '');
      } else {
        setStatus('Error: ' + event.error, '');
      }
      stopListening();
    };

    recognition.onend = function() {
      if (isListening) {
        // Stopped unexpectedly, restart if still in listening mode
        isListening = false;
        updateMicBtn();
        updateOrb();
      }
    };

    return recognition;
  }

  function toggleListening() {
    if (isProcessing || isSpeaking) return;
    if (isListening) {
      stopListening();
    } else {
      startListening();
    }
  }

  function startListening() {
    if (!recognition) initRecognition();
    if (!recognition) return;

    isListening = true;
    updateMicBtn();
    updateOrb();
    setStatus('Listening...', 'active');

    // Unlock audio on user gesture (critical for iOS TTS playback)
    initAudioContext();
    if (audioCtx.state === 'suspended') audioCtx.resume();
    // Start silent audio loop — keeps iOS audio session active for later TTS playback
    startSilenceLoop();

    // Get mic access for waveform
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function(stream) {
      startWaveform(stream);
    }).catch(function() {});

    try {
      recognition.start();
    } catch(e) {
      // Already started
    }
  }

  function stopListening() {
    isListening = false;
    updateMicBtn();
    updateOrb();
    if (recognition) {
      try { recognition.stop(); } catch(e) {}
    }
    stopWaveform();
  }

  // ─── Process Voice Input ───
  function processVoiceInput(text) {
    if (!text || !text.trim()) return;

    isProcessing = true;
    updateMicBtn();
    updateOrb();
    setStatus('Thinking...', 'active');
    addTranscript('user', text);

    // Start voice session if needed
    var sessionPromise;
    if (!voiceSessionId) {
      sessionPromise = apiCall('POST', '/start', {
        customer_id: CUSTOMER_ID,
        email: USER_EMAIL,
        name: USER_NAME,
        page_url: window.location.href
      }).then(function(data) {
        voiceSessionId = data.session_id;
        return voiceSessionId;
      });
    } else {
      sessionPromise = Promise.resolve(voiceSessionId);
    }

    sessionPromise.then(function(sid) {
      return apiCall('POST', '/message', {
        session_id: sid,
        content: text,
        page_url: window.location.href
      });
    }).then(function(data) {
      var reply = data.reply || 'Sorry, I could not process that.';
      // Strip any action tags
      reply = reply.replace(/\[ACTION:[^\]]*\]/g, '').trim();
      if (!reply) reply = 'Let me help you with that.';

      isProcessing = false;
      addTranscript('ai', reply);

      // Clean text for TTS: remove markdown, URLs, and special formatting
      var spokenText = reply
        .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')  // [text](url) → text
        .replace(/https?:\/\/[^\s)]+/g, '')        // bare URLs
        .replace(/\*\*([^*]+)\*\*/g, '$1')         // **bold** → bold
        .replace(/\*([^*]+)\*/g, '$1')             // *italic* → italic
        .replace(/[#]+\s/g, '')                    // ### headers
        .replace(/\n-\s/g, '\n')                   // - list items
        .replace(/\n\d+\.\s/g, '\n')              // 1. numbered lists
        .replace(/[€₹]\s*/g, '')                   // currency symbols (spoken as word)
        .replace(/\s{2,}/g, ' ')                   // collapse whitespace
        .trim();
      if (!spokenText) spokenText = reply;
      speakResponse(spokenText);
    }).catch(function(err) {
      console.error('[VoiceWidget] API error:', err);
      isProcessing = false;
      updateMicBtn();
      updateOrb();
      setStatus('Error getting response. Tap mic to try again.', '');
    });
  }

  // ─── Text-to-Speech (ElevenLabs via backend, gTTS fallback) ───
  // Strategy: Keep an <audio> element playing silence in a loop from the mic tap.
  // When TTS audio arrives, swap the src. iOS allows this because element is already "playing".
  var ttsPlayer = null;
  var ttsObjectUrl = null;
  // 1-second silent MP3 (loopable) to keep audio element active on iOS
  var SILENCE_SRC = 'data:audio/mp3;base64,//uQxAAAAAANIAAAAAExBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//uQxBEAAADSAAAAAFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV';

  function initTtsPlayer() {
    if (!ttsPlayer) {
      ttsPlayer = document.createElement('audio');
      ttsPlayer.setAttribute('playsinline', '');
      ttsPlayer.setAttribute('webkit-playsinline', '');
      ttsPlayer.id = 'ow-voice-tts-player';
      document.body.appendChild(ttsPlayer);
    }
  }

  function startSilenceLoop() {
    // Start playing silence in a loop — keeps iOS audio session active
    initTtsPlayer();
    ttsPlayer.src = SILENCE_SRC;
    ttsPlayer.loop = true;
    ttsPlayer.volume = 0.01;
    var p = ttsPlayer.play();
    if (p) p.catch(function(e) { console.warn('[VoiceWidget] silence play fail:', e); });
  }

  function stopSilenceLoop() {
    if (ttsPlayer) {
      ttsPlayer.loop = false;
      ttsPlayer.pause();
    }
  }

  function speakResponse(text) {
    isSpeaking = true;
    updateOrb();
    setStatus('Speaking...', 'speaking');

    // Revoke old blob URL
    if (ttsObjectUrl) {
      URL.revokeObjectURL(ttsObjectUrl);
      ttsObjectUrl = null;
    }

    // Fetch audio from backend TTS endpoint
    fetch('/api/chat/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text })
    })
    .then(function(resp) {
      if (!resp.ok) throw new Error('TTS HTTP ' + resp.status);
      return resp.blob();
    })
    .then(function(blob) {
      ttsObjectUrl = URL.createObjectURL(blob);
      initTtsPlayer();

      // Stop the silence loop and swap to real audio
      ttsPlayer.loop = false;
      ttsPlayer.volume = 1.0;
      ttsPlayer.onended = function() {
        ttsPlayer.onended = null;
        finishSpeaking();
      };
      ttsPlayer.onerror = function(e) {
        console.error('[VoiceWidget] Audio playback error:', e);
        ttsPlayer.onerror = null;
        finishSpeaking();
      };

      // Swap src — since element is already playing (silence loop), iOS allows this
      ttsPlayer.src = ttsObjectUrl;
      ttsPlayer.load();
      var p = ttsPlayer.play();
      if (p) p.catch(function(e) {
        console.error('[VoiceWidget] TTS play rejected:', e);
        finishSpeaking();
      });
    })
    .catch(function(err) {
      console.error('[VoiceWidget] TTS fetch error:', err);
      finishSpeaking();
    });
  }

  function finishSpeaking() {
    isSpeaking = false;
    if (ttsObjectUrl) {
      URL.revokeObjectURL(ttsObjectUrl);
      ttsObjectUrl = null;
    }
    // Resume silence loop for next response
    startSilenceLoop();
    updateOrb();
    updateMicBtn();
    // Auto-listen for continuous conversation
    setTimeout(function() {
      if (voicePanel && voicePanel.classList.contains('open') && !isListening && !isSpeaking) {
        startListening();
      }
    }, 600);
  }

  // ─── UI Helpers ───
  function setStatus(text, cls) {
    var el = document.getElementById('ow-voice-status');
    if (el) {
      el.textContent = text;
      el.className = 'ow-voice-status' + (cls ? ' ' + cls : '');
    }
  }

  function updateMicBtn() {
    var btn = document.getElementById('ow-voice-mic');
    if (!btn) return;
    btn.className = 'ow-voice-mic-btn';
    if (isProcessing || isSpeaking) btn.classList.add('disabled');
    else if (isListening) btn.classList.add('listening');
    else btn.classList.add('idle');
  }

  function updateOrb() {
    var orb = document.getElementById('ow-voice-orb');
    if (!orb) return;
    orb.className = 'ow-voice-orb';
    if (isListening) orb.classList.add('listening');
    else if (isSpeaking) orb.classList.add('speaking');
    else if (isProcessing) orb.classList.add('processing');
  }

  function addTranscript(role, text) {
    var container = document.getElementById('ow-voice-transcript');
    if (!container) return;
    var div = document.createElement('div');
    div.className = 'ow-voice-transcript-item ' + role;
    div.innerHTML = '<div class="ow-vt-label">' + (role === 'user' ? 'You' : 'Optiwar AI') + '</div><div class="ow-vt-text">' + escapeHtml(text) + '</div>';
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
  }

  function escapeHtml(str) {
    var div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  // ─── API Helper (reuse from chat widget pattern) ───
  function apiCall(method, path, body) {
    return new Promise(function(resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open(method, '/api/chat' + path, true);
      xhr.setRequestHeader('Content-Type', 'application/json');
      xhr.onload = function() {
        if (xhr.status >= 200 && xhr.status < 300) {
          try { resolve(JSON.parse(xhr.responseText)); }
          catch(e) { reject(e); }
        } else { reject(new Error(xhr.status)); }
      };
      xhr.onerror = function() { reject(new Error('Network error')); };
      xhr.send(body ? JSON.stringify(body) : null);
    });
  }

  // ─── Expose globally ───
  window.owVoiceWidget = {
    open: openVoicePanel,
    close: closeVoicePanel
  };
})();
