/**
 * voice.js — Voice input via Web Speech API or MediaRecorder fallback.
 */
const Voice = (() => {
  let recognition = null;
  let recording = false;
  let onResult = null;
  let silenceTimer = null;
  let pendingTranscript = '';
  let finalTranscript = '';   // accumulated final text across onresult events
  const SILENCE_DELAY = 2000; // ms to wait after last speech before submitting

  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;

  function isSupported() {
    return !!SpeechRecognition;
  }

  function init(callback) {
    onResult = callback;
    if (!SpeechRecognition) {
      console.warn('Web Speech API not supported in this browser.');
      return false;
    }
    recognition = new SpeechRecognition();
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = 'en-US';

    recognition.onresult = (event) => {
      // Only process new / changed results (from resultIndex onwards)
      // to avoid re-concatenating already-finalized text.
      let interim = '';
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i];
        if (result.isFinal) {
          finalTranscript += result[0].transcript;
        } else {
          interim += result[0].transcript;
        }
      }
      pendingTranscript = finalTranscript + interim;
      // Show words in the text box as they are spoken
      const input = document.getElementById('text-input');
      if (input) input.value = pendingTranscript;

      // Reset the silence timer — submit after 2s of no new speech
      clearTimeout(silenceTimer);
      silenceTimer = setTimeout(() => {
        if (pendingTranscript.trim() && onResult) {
          const text = pendingTranscript.trim();
          pendingTranscript = '';
          finalTranscript = '';
          stop();
          onResult(text);
        }
      }, SILENCE_DELAY);
    };

    recognition.onerror = (event) => {
      console.warn('Speech recognition error:', event.error);
      recording = false;
      clearTimeout(silenceTimer);
    };

    recognition.onend = () => {
      // If we're still supposed to be recording (browser auto-stopped), restart
      if (recording) {
        try { recognition.start(); } catch { /* noop */ }
        return;
      }
      updateButton();
    };

    return true;
  }

  function start() {
    if (!recognition) return;
    if (recording) { stop(); return; }
    try {
      pendingTranscript = '';
      finalTranscript = '';
      recognition.start();
      recording = true;
      updateButton();
    } catch (e) {
      console.warn('Recognition start failed:', e);
    }
  }

  function stop() {
    if (!recognition) return;
    clearTimeout(silenceTimer);
    recording = false;
    pendingTranscript = '';
    finalTranscript = '';
    try { recognition.stop(); } catch { /* noop */ }
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
