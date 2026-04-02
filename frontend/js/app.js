/**
 * app.js — Main initialization and event binding.
 */
(function () {
  'use strict';

  // ─── Boot ─────────────────────────────────────────────────

  document.addEventListener('DOMContentLoaded', () => {
    UI.cacheElements();
    bindEvents();
    Voice.init(handleVoiceResult);
    State.onChange(onStateChange);
    State.checkHealth();
    State.refreshDevices();
    State.startPolling(3000);
  });

  // ─── State Change Handler ─────────────────────────────────

  function onStateChange(state) {
    UI.updateStatus(state.connected);
    UI.updateDevicePicker(state.devices, state.deviceId);
    UI.updateNowPlaying(state.session);
  }

  // ─── Event Binding ────────────────────────────────────────

  function bindEvents() {
    // System picker
    document.getElementById('system-picker').addEventListener('change', e => {
      State.set({ system: e.target.value });
    });

    // Device picker
    document.getElementById('device-picker').addEventListener('change', e => {
      State.set({ deviceId: e.target.value });
      State.refreshSession();
    });

    // Transport controls
    document.getElementById('btn-play-pause').addEventListener('click', () => sendPlayback('play_pause'));
    document.getElementById('btn-stop').addEventListener('click', () => sendPlayback('stop'));
    document.getElementById('btn-skip-back').addEventListener('click', () => sendPlayback('skip_back', { seconds: 10 }));
    document.getElementById('btn-skip-forward').addEventListener('click', () => sendPlayback('skip_forward', { seconds: 30 }));
    document.getElementById('btn-commercial-skip').addEventListener('click', () => sendPlayback('commercial_skip'));

    // Volume
    document.getElementById('volume-slider').addEventListener('input', e => {
      sendPlayback('volume', { level: parseInt(e.target.value, 10) });
    });
    document.getElementById('btn-mute').addEventListener('click', () => sendPlayback('mute_toggle'));

    // Seek
    document.getElementById('seek-slider').addEventListener('change', e => {
      const session = State.get().session;
      if (session && session.duration > 0) {
        const position = (e.target.value / 100) * session.duration;
        sendPlayback('seek', { position: Math.floor(position) });
      }
    });

    // Text input
    const textInput = document.getElementById('text-input');
    const btnSend = document.getElementById('btn-send');
    textInput.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend(); }
    });
    btnSend.addEventListener('click', handleSend);

    // Voice
    document.getElementById('btn-voice').addEventListener('click', () => {
      if (Voice.isRecording()) Voice.stop();
      else Voice.start();
    });

    // Footer buttons
    document.getElementById('btn-settings').addEventListener('click', () => UI.openSettings());
    document.getElementById('btn-admin').addEventListener('click', () => UI.openAdmin());
    document.getElementById('btn-close-admin').addEventListener('click', () => {
      document.getElementById('admin-dialog').close();
    });

    // Settings save
    document.getElementById('settings-dialog').addEventListener('close', (e) => {
      if (e.target.returnValue === 'save') {
        const sys = document.getElementById('setting-default-system').value;
        const url = document.getElementById('setting-api-url').value;
        State.set({ system: sys });
        if (url) API.setBaseUrl(url);
      }
    });

    // Admin tabs
    document.querySelectorAll('.admin-tabs .tab').forEach(tab => {
      tab.addEventListener('click', () => {
        document.querySelectorAll('.admin-tabs .tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
        tab.classList.add('active');
        document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
      });
    });

    // Admin device actions (delegation)
    document.getElementById('device-list').addEventListener('click', async (e) => {
      const btn = e.target.closest('button[data-action]');
      if (!btn) return;
      const action = btn.dataset.action;
      const id = btn.dataset.id;
      if (action === 'delete-device') {
        await API.deleteDevice(id);
        UI.refreshAdminDevices();
        State.refreshDevices();
      } else if (action === 'set-default') {
        await API.setDefaultDevice(id);
        UI.refreshAdminDevices();
        State.refreshDevices();
      }
    });

    // Add device
    document.getElementById('btn-add-device').addEventListener('click', async () => {
      const name = prompt('Device name:');
      if (!name) return;
      await API.addDevice({ name, type: 'web', client_id: State.get().system });
      UI.refreshAdminDevices();
      State.refreshDevices();
    });

    // Admin system buttons
    document.getElementById('btn-docker-status').addEventListener('click', async () => {
      try {
        const data = await API.system('docker_status');
        UI.renderSystemOutput(JSON.stringify(data, null, 2));
      } catch (e) {
        UI.renderSystemOutput('Error: ' + e.message);
      }
    });
    document.getElementById('btn-restart-sagetv').addEventListener('click', async () => {
      if (!confirm('Restart SageTV container?')) return;
      try {
        const data = await API.system('restart_container', { container: 'sagetv-server' });
        UI.renderSystemOutput(JSON.stringify(data, null, 2));
      } catch (e) {
        UI.renderSystemOutput('Error: ' + e.message);
      }
    });
    document.getElementById('btn-restart-channels').addEventListener('click', async () => {
      if (!confirm('Restart Channels DVR service?')) return;
      try {
        const data = await API.system('restart_service', { service: 'channels-dvr' });
        UI.renderSystemOutput(JSON.stringify(data, null, 2));
      } catch (e) {
        UI.renderSystemOutput('Error: ' + e.message);
      }
    });
  }

  // ─── Send Helpers ─────────────────────────────────────────

  async function handleSend() {
    const input = document.getElementById('text-input');
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    UI.addMessage(text, 'user');

    try {
      const data = await API.query(text);
      const response = data.response || data.error || JSON.stringify(data);
      UI.addMessage(response, 'assistant');
    } catch (e) {
      UI.addMessage('Error: ' + e.message, 'error');
    }
  }

  function handleVoiceResult(transcript) {
    document.getElementById('text-input').value = transcript;
    handleSend();
  }

  async function sendPlayback(action, params = {}) {
    const state = State.get();
    try {
      await API.playback(action, { system: state.system, device_id: state.deviceId, ...params });
      State.refreshSession();
    } catch (e) {
      UI.addMessage('Playback error: ' + e.message, 'error');
    }
  }
})();
