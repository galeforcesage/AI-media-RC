/**
 * voice.js — Voice input via Web Speech API or MediaRecorder fallback.
 */
const Voice = (() => {
  let recognition = null;
  let recording = false;
  let onResult = null;

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
    recognition.continuous = false;
    recognition.interimResults = false;
    recognition.lang = 'en-US';

    recognition.onresult = (event) => {
      const transcript = event.results[0][0].transcript;
      recording = false;
      if (onResult) onResult(transcript);
    };

    recognition.onerror = (event) => {
      console.warn('Speech recognition error:', event.error);
      recording = false;
    };

    recognition.onend = () => {
      recording = false;
      updateButton();
    };

    return true;
  }

  function start() {
    if (!recognition) return;
    if (recording) { stop(); return; }
    try {
      recognition.start();
      recording = true;
      updateButton();
    } catch (e) {
      console.warn('Recognition start failed:', e);
    }
  }

  function stop() {
    if (!recognition) return;
    try { recognition.stop(); } catch { /* noop */ }
    recording = false;
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
