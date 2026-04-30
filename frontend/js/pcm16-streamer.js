/**
 * pcm16-streamer.js
 * Browser microphone → 16 kHz mono Int16LE PCM → callback(ArrayBuffer)
 *
 * Uses AudioWorklet when available (Chrome, Edge, Safari 17+, iOS 17+),
 * falls back to ScriptProcessorNode for older browsers.
 */
const PCM16Streamer = (() => {
  // ─── AudioWorklet processor source ────────────────────────
  const WORKLET_CODE = `
class PCM16Processor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = options.processorOptions || {};
    this.inputRate = opts.inputSampleRate || sampleRate;
    this.targetRate = opts.targetSampleRate || 16000;
    this.ratio = this.inputRate / this.targetRate;
    this.fractional = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0] || input[0].length === 0) return true;
    const samples = input[0];

    // Downsample via linear interpolation
    const outLen = Math.floor((samples.length + this.fractional) / this.ratio);
    if (outLen <= 0) return true;
    const int16 = new Int16Array(outLen);
    for (let i = 0; i < outLen; i++) {
      const srcIdx = (i * this.ratio) - this.fractional;
      const idx0 = Math.floor(srcIdx);
      const frac = srcIdx - idx0;
      const s0 = idx0 >= 0 && idx0 < samples.length ? samples[idx0] : 0;
      const s1 = idx0 + 1 < samples.length ? samples[idx0 + 1] : s0;
      const val = s0 + frac * (s1 - s0);
      int16[i] = Math.max(-32768, Math.min(32767, Math.round(val * 32767)));
    }
    this.fractional = (this.fractional + samples.length) % this.ratio;

    this.port.postMessage(int16.buffer, [int16.buffer]);
    return true;
  }
}
registerProcessor('pcm16-processor', PCM16Processor);
`;

  // ─── Linear resampler for ScriptProcessor fallback ────────
  class LinearResampler {
    constructor(fromRate, toRate) {
      this.ratio = fromRate / toRate;
      this.fractional = 0;
    }
    process(float32) {
      const outLen = Math.floor((float32.length + this.fractional) / this.ratio);
      if (outLen <= 0) return new Float32Array(0);
      const out = new Float32Array(outLen);
      for (let i = 0; i < outLen; i++) {
        const srcIdx = (i * this.ratio) - this.fractional;
        const idx0 = Math.floor(srcIdx);
        const frac = srcIdx - idx0;
        const s0 = idx0 >= 0 && idx0 < float32.length ? float32[idx0] : 0;
        const s1 = idx0 + 1 < float32.length ? float32[idx0 + 1] : s0;
        out[i] = s0 + frac * (s1 - s0);
      }
      this.fractional = (this.fractional + float32.length) % this.ratio;
      return out;
    }
  }

  function floatToInt16(float32) {
    const int16 = new Int16Array(float32.length);
    for (let i = 0; i < float32.length; i++) {
      int16[i] = Math.max(-32768, Math.min(32767, Math.round(float32[i] * 32767)));
    }
    return int16;
  }

  // ─── Main class ──────────────────────────────────────────
  class Streamer {
    constructor({ onData, onError }) {
      this.onData = onData;
      this.onError = onError || console.error;
      this.audioContext = null;
      this.stream = null;
      this.source = null;
      this.node = null;
    }

    async start() {
      try {
        this.stream = await navigator.mediaDevices.getUserMedia({
          audio: { channelCount: 1, sampleRate: 16000, echoCancellation: true, noiseSuppression: true },
        });
        this.audioContext = new (window.AudioContext || window.webkitAudioContext)();
        this.source = this.audioContext.createMediaStreamSource(this.stream);

        if (this.audioContext.audioWorklet) {
          await this._startWorklet();
        } else {
          this._startScriptProcessor();
        }
      } catch (err) {
        this.onError(err);
      }
    }

    stop() {
      try { this.node?.disconnect(); } catch {}
      try { this.source?.disconnect(); } catch {}
      try { this.stream?.getTracks().forEach(t => t.stop()); } catch {}
      try { this.audioContext?.close(); } catch {}
      this.node = null;
      this.source = null;
      this.stream = null;
      this.audioContext = null;
    }

    async _startWorklet() {
      const blob = new Blob([WORKLET_CODE], { type: 'application/javascript' });
      const url = URL.createObjectURL(blob);
      try {
        await this.audioContext.audioWorklet.addModule(url);
      } finally {
        URL.revokeObjectURL(url);
      }
      this.node = new AudioWorkletNode(this.audioContext, 'pcm16-processor', {
        processorOptions: {
          inputSampleRate: this.audioContext.sampleRate,
          targetSampleRate: 16000,
        },
      });
      this.node.port.onmessage = (e) => {
        if (e.data instanceof ArrayBuffer) this.onData(e.data);
      };
      this.source.connect(this.node);
      // Connect to destination to keep the graph alive (silent output)
      this.node.connect(this.audioContext.destination);
    }

    _startScriptProcessor() {
      const bufSize = 4096;
      this.node = this.audioContext.createScriptProcessor(bufSize, 1, 1);
      const resampler = new LinearResampler(this.audioContext.sampleRate, 16000);

      this.node.onaudioprocess = (e) => {
        const input = e.inputBuffer.getChannelData(0);
        const resampled = resampler.process(input);
        const int16 = floatToInt16(resampled);
        this.onData(int16.buffer);
      };
      this.source.connect(this.node);
      this.node.connect(this.audioContext.destination);
    }
  }

  return { Streamer };
})();
