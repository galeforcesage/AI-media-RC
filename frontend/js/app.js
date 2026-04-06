/**
 * app.js — Main initialization and event binding.
 */
(function () {
  'use strict';

  // ─── Boot ─────────────────────────────────────────────────

  document.addEventListener('DOMContentLoaded', async () => {
    // Auth gate: check if logged in, redirect if not
    try {
      const check = await API.authCheck();
      if (!check.authenticated) {
        window.location.href = '/login.html';
        return;
      }
    } catch (_) {
      window.location.href = '/login.html';
      return;
    }

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
    UI.updateLLMFocusCheckboxes(state.llmFocus);
    UI.updatePicker('remote-system', state.system);
    UI.updateDevicePicker(state.devices, state.deviceId);
    UI.updateNowPlaying(state.session);
  }

  // ─── Admin Auth Helper ──────────────────────────────────

  async function ensureAdmin(actionLabel) {
    /**
     * Ensure the user has a valid admin session.
     * Returns true if authenticated, false if they cancelled.
     */
    const check = await API.adminCheck();
    if (check.authenticated) return true;

    // Show admin login dialog — retries on wrong password
    return new Promise((resolve) => {
      const dlg = document.getElementById('sudo-dialog');
      const pwdInput = document.getElementById('sudo-password');
      const userInput = document.getElementById('admin-username');
      const errMsg = document.getElementById('sudo-error');
      document.getElementById('sudo-prompt-text').textContent =
        `"${actionLabel}" requires admin authentication.`;
      errMsg.style.display = 'none';
      errMsg.textContent = '';
      pwdInput.value = '';
      dlg.showModal();

      async function handler() {
        dlg.removeEventListener('close', handler);
        if (dlg.returnValue === 'ok') {
          const username = userInput ? userInput.value.trim() : 'admin';
          const password = pwdInput.value;
          pwdInput.value = '';
          try {
            const result = await API.adminLogin(username, password);
            if (result.success) {
              errMsg.style.display = 'none';
              resolve(true);
              return;
            }
            // Wrong credentials — show error and re-open
            errMsg.textContent = result.error || 'Invalid credentials.';
            errMsg.style.display = 'block';
            dlg.addEventListener('close', handler);
            dlg.showModal();
          } catch (err) {
            errMsg.textContent = 'Login error: ' + err.message;
            errMsg.style.display = 'block';
            dlg.addEventListener('close', handler);
            dlg.showModal();
          }
        } else {
          pwdInput.value = '';
          errMsg.style.display = 'none';
          resolve(false);
        }
      }
      dlg.addEventListener('close', handler);
    });
  }

  // ─── Event Binding ────────────────────────────────────────

  function bindEvents() {
    // LLM Focus checkbox dropdown
    const llmToggle = document.getElementById('llm-focus-toggle');
    const llmMenu = document.getElementById('llm-focus-menu');
    llmToggle.addEventListener('click', () => {
      const open = llmMenu.classList.toggle('open');
      llmToggle.setAttribute('aria-expanded', open);
    });
    document.addEventListener('click', (e) => {
      if (!e.target.closest('#llm-focus')) {
        llmMenu.classList.remove('open');
        llmToggle.setAttribute('aria-expanded', 'false');
      }
    });
    llmMenu.addEventListener('change', () => {
      const checked = [...llmMenu.querySelectorAll('input:checked')].map(cb => cb.value);
      if (checked.length === 0) {
        // Don't allow unchecking everything — re-check the one just unchecked
        event.target.checked = true;
        return;
      }
      State.set({ llmFocus: checked });
      UI.updateLLMFocusLabel(checked);
    });

    // Remote Control system picker
    document.getElementById('remote-system').addEventListener('change', e => {
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

    // Episode card clicks — search + play on bound device
    document.getElementById('messages').addEventListener('click', async (e) => {
      const card = e.target.closest('.episode-card');
      if (!card) return;
      const title = card.dataset.title;
      const system = card.dataset.system || State.get().system;
      if (!title) return;

      card.classList.add('loading');
      UI.addMessage(`Playing "${title}" on ${system}…`, 'assistant');

      try {
        const result = await API.playTitle(title, system);
        if (result && result.error) {
          UI.addMessage('Playback error: ' + result.error, 'error');
        } else {
          State.refreshSession();
        }
      } catch (err) {
        UI.addMessage('Playback error: ' + err.message, 'error');
      } finally {
        card.classList.remove('loading');
      }
    });

    // Footer buttons
    document.getElementById('btn-settings').addEventListener('click', () => UI.openSettings());
    document.getElementById('btn-admin').addEventListener('click', () => UI.openAdmin());
    document.getElementById('btn-close-admin').addEventListener('click', () => {
      document.getElementById('admin-dialog').close();
    });
    document.getElementById('btn-logout').addEventListener('click', async () => {
      await API.authLogout();
      window.location.href = '/login.html';
    });

    // Settings save
    document.getElementById('settings-dialog').addEventListener('close', (e) => {
      if (e.target.returnValue === 'save') {
        const sys = document.getElementById('setting-default-system').value;
        State.set({ llmFocus: [sys], system: sys });
      }
    });

    // Admin tabs
    document.querySelectorAll('.admin-tabs .tab').forEach(tab => {
      tab.addEventListener('click', () => {
        document.querySelectorAll('.admin-tabs .tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
        tab.classList.add('active');
        document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
        if (tab.dataset.tab === 'services') refreshServices();
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

    // Add device — open dialog with auto-detected IP
    document.getElementById('btn-add-device').addEventListener('click', async () => {
      const dlg = document.getElementById('add-device-dialog');
      // Auto-detect client IP
      try {
        const info = await API.whoami();
        if (info.ip) document.getElementById('device-ip').value = info.ip;
      } catch (_) { /* leave blank */ }
      dlg.showModal();
    });

    document.getElementById('add-device-form').addEventListener('close', async function () {
      const dlg = document.getElementById('add-device-dialog');
      if (dlg.returnValue !== 'save') return;
    });

    document.getElementById('add-device-form').addEventListener('submit', async function (e) {
      const dlg = document.getElementById('add-device-dialog');
      if (e.submitter && e.submitter.value === 'cancel') return;
      const name = document.getElementById('device-name').value.trim();
      if (!name) return;
      await API.addDevice({
        friendly_name: name,
        system: document.getElementById('device-system').value,
        ip_address: document.getElementById('device-ip').value.trim(),
        platform: document.getElementById('device-platform').value,
      });
      document.getElementById('device-name').value = '';
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

    // Service grid: refresh button
    document.getElementById('btn-refresh-services').addEventListener('click', refreshServices);

    // Auto-refresh services when admin dialog opens
    document.getElementById('admin-dialog').addEventListener('admin-opened', refreshServices);

    // Service grid: restart delegation
    document.getElementById('service-grid').addEventListener('click', async (e) => {
      const btn = e.target.closest('.btn-restart[data-service-id]');
      if (!btn) return;
      const svcId = btn.dataset.serviceId;
      const label = `Restart ${svcId}`;
      if (!confirm(`${label}?`)) return;
      if (!await ensureAdmin(label)) return;

      btn.disabled = true;
      btn.textContent = '⟳ …';
      try {
        const restartMap = {
          orchestrator:   { action: 'restart_rc_service', params: { service: 'orchestrator' } },
          mcp_sagetv:     { action: 'restart_rc_service', params: { service: 'mcp-sagetv' } },
          mcp_channels:   { action: 'restart_rc_service', params: { service: 'mcp-channels' } },
          mcp_linux:      { action: 'restart_rc_service', params: { service: 'mcp-linux' } },
          session_mgr:    { action: 'restart_rc_service', params: { service: 'session-manager' } },
          transcription:  { action: 'restart_rc_service', params: { service: 'transcription' } },
        };
        const spec = restartMap[svcId];
        if (!spec) throw new Error('Unknown service: ' + svcId);
        const data = await API.system(spec.action, spec.params);
        if (data.error) {
          showServiceMessage(data.error, true);
        } else {
          showServiceMessage(`${svcId} restarted successfully`);
        }
        // Refresh grid after a short delay to let service come back
        setTimeout(refreshServices, 3000);
      } catch (err) {
        showServiceMessage('Error: ' + err.message, true);
      } finally {
        btn.disabled = false;
        btn.textContent = '↻ Restart';
      }
    });
  }

  async function refreshServices() {
    try {
      const data = await API.services();
      if (data.services) UI.renderServiceGrid(data.services);
    } catch (e) {
      console.error('Failed to refresh services:', e);
    }
  }

  function showServiceMessage(text, isError = false) {
    const grid = document.getElementById('service-grid');
    let msg = grid.parentElement.querySelector('.svc-message');
    if (!msg) {
      msg = document.createElement('p');
      msg.className = 'svc-message';
      grid.parentElement.insertBefore(msg, grid);
    }
    msg.textContent = text;
    msg.style.color = isError ? 'var(--danger)' : '#66bb6a';
    msg.style.display = 'block';
    setTimeout(() => { msg.style.display = 'none'; }, 6000);
  }

  // ─── Send Helpers ─────────────────────────────────────────

  async function handleSend() {
    const input = document.getElementById('text-input');
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    UI.addMessage(text, 'user');

    // Show thinking indicator
    const thinking = document.createElement('div');
    thinking.className = 'message thinking';
    thinking.textContent = 'Thinking';
    const msgContainer = document.getElementById('messages');
    msgContainer.appendChild(thinking);
    msgContainer.scrollTop = msgContainer.scrollHeight;

    try {
      const data = await API.query(text, State.get().llmFocus);

      thinking.remove();
      const response = data.response || data.llm_response || data.error || 'No response from server.';
      UI.addMessage(response, data.error ? 'error' : 'assistant');

      // Render clickable episode cards if transcript results were returned
      const results = data.transcript_results;
      if (results && results.length > 0) {
        UI.addEpisodeCards(results);
      }
    } catch (e) {
      thinking.remove();
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
