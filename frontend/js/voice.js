/**
 * voice.js — Voice input via server-side Whisper STT over WebSocket.
 *
 * Captures microphone audio as 16kHz mono PCM16-LE using PCM16Streamer,
 * streams it to /ws/stt, and renders committed + hypothesis text in the
 * input box.  No word stacking — partials replace, finals append.
 */
const Voice = (() => {
  let onResult = null;       // callback(finalText)
  let recording = false;
  let ws = null;
  let streamer = null;
  let silenceTimer = null;

  // Committed/hypothesis state
  let committedText = '';    // finalized text (only grows via "final" messages)
  let hypothesisText = '';   // latest partial (replaced, never appended)

  const SILENCE_DELAY = 3000;  // ms after last partial before auto-stopping

  function isSupported() {
    return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  }

  function init(callback) {
    onResult = callback;
    return isSupported();
  }

  function _wsUrl() {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${window.location.host}/ws/stt`;
  }

  function _updateInput() {
    const input = document.getElementById('text-input');
    if (input) input.value = (committedText + hypothesisText).trim();
  }

  function _resetSilenceTimer() {
    clearTimeout(silenceTimer);
    silenceTimer = setTimeout(() => {
      // User stopped speaking — stop recording, leave text in input for review
      if (recording) stop();
    }, SILENCE_DELAY);
  }

  async function start() {
    if (!isSupported()) return;
    if (recording) { stop(); return; }

    committedText = '';
    hypothesisText = '';
    _updateInput();

    try {
      ws = new WebSocket(_wsUrl());
      ws.binaryType = 'arraybuffer';

      ws.onmessage = (evt) => {
        try {
          const msg = JSON.parse(evt.data);
          if (msg.type === 'partial') {
            // Replace hypothesis — never append
            hypothesisText = msg.text || '';
            _updateInput();
            _resetSilenceTimer();
          } else if (msg.type === 'final') {
            // Server sends the full final transcript — leave in input for user to review/send
            committedText = msg.text || '';
            hypothesisText = '';
            _updateInput();
            clearTimeout(silenceTimer);
            _cleanup();
          }
        } catch (_) { /* ignore malformed */ }
      };

      ws.onerror = (err) => {
        console.warn('STT WebSocket error:', err);
        _cleanup();
      };

      ws.onclose = () => {
        _cleanup();
      };

      // Wait for WS to open before starting audio
      await new Promise((resolve, reject) => {
        ws.onopen = resolve;
        const origErr = ws.onerror;
        ws.onerror = (e) => { if (origErr) origErr(e); reject(e); };
        setTimeout(() => reject(new Error('WS connect timeout')), 5000);
      });

      // Start audio capture
      streamer = new PCM16Streamer.Streamer({
        onData: (pcmBuffer) => {
          if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(pcmBuffer);
          }
        },
        onError: (err) => {
          console.warn('Audio capture error:', err);
          stop();
        },
      });
      await streamer.start();
      recording = true;
      updateButton();
      _resetSilenceTimer();
    } catch (err) {
      console.warn('Voice start failed:', err);
      _cleanup();
    }
  }

  function stop() {
    if (!recording) return;
    clearTimeout(silenceTimer);
    // Tell server we're done — it will respond with a final message
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ action: 'stop' })); } catch {}
    }
    // Stop audio capture
    if (streamer) {
      streamer.stop();
      streamer = null;
    }
    recording = false;
    updateButton();
  }

  function _finish() {
    const text = (committedText + hypothesisText).trim();
    committedText = '';
    hypothesisText = '';
    if (text && onResult) {
      onResult(text);
    }
    _cleanup();
  }

  function _cleanup() {
    clearTimeout(silenceTimer);
    recording = false;
    if (streamer) { streamer.stop(); streamer = null; }
    if (ws) {
      try { ws.close(); } catch {}
      ws = null;
    }
    updateButton();
  }

  function isRecording() { return recording; }

  function updateButton() {
    const btn = document.getElementById('btn-voice');
    if (!btn) return;
    if (recording) {
      btn.classList.add('recording');
      btn.setAttribute('aria-label', 'Listening...');
    } else {
      btn.classList.remove('recording');
      btn.setAttribute('aria-label', 'Hold to speak');
    }
  }

  return { isSupported, init, start, stop, isRecording };
})();
