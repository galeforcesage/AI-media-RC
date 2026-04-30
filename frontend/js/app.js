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
    State.startPolling(7000);
  });

  // ─── State Change Handler ─────────────────────────────────

  function onStateChange(state) {
    UI.updateStatus(state.connected);
    UI.updateLLMFocusCheckboxes(state.llmFocus);
    UI.updatePicker('remote-system', state.system);
    UI.updateDevicePicker(state.devices, state.deviceId, state.bridgeDevices, state.system);
    UI.updateNowPlaying(state.session, state.deviceId);
    UI.updateDpad(state.system, state.deviceId);
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
    llmMenu.addEventListener('change', (e) => {
      const checked = [...llmMenu.querySelectorAll('input:checked')].map(cb => cb.value);
      if (checked.length === 0) {
        // Don't allow unchecking everything — re-check the one just unchecked
        e.target.checked = true;
        return;
      }
      State.set({ llmFocus: checked });
      UI.updateLLMFocusLabel(checked);
    });

    // Remote Control system picker
    document.getElementById('remote-system').addEventListener('change', e => {
      State.set({ system: e.target.value });
      State.refreshBridgeDevices();
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

    // D-pad — Channels DVR
    document.getElementById('btn-ch-up').addEventListener('click', () => sendPlayback('channel_up'));
    document.getElementById('btn-ch-down').addEventListener('click', () => sendPlayback('channel_down'));
    document.getElementById('btn-toggle-cc').addEventListener('click', () => sendPlayback('toggle_cc'));

    // D-pad — SageTV navigation
    document.getElementById('btn-nav-up').addEventListener('click', () => sendPlayback('nav_up'));
    document.getElementById('btn-nav-down').addEventListener('click', () => sendPlayback('nav_down'));
    document.getElementById('btn-nav-left').addEventListener('click', () => sendPlayback('nav_left'));
    document.getElementById('btn-nav-right').addEventListener('click', () => sendPlayback('nav_right'));
    document.getElementById('btn-nav-select').addEventListener('click', () => sendPlayback('nav_select'));
    document.getElementById('btn-nav-back').addEventListener('click', () => sendPlayback('nav_back'));
    document.getElementById('btn-close').addEventListener('click', () => sendPlayback('close'));
    document.getElementById('btn-nav-options').addEventListener('click', () => sendPlayback('nav_options'));
    document.getElementById('btn-toggle-cc-sage').addEventListener('click', () => sendPlayback('toggle_cc'));
    document.getElementById('btn-ch-up-sage').addEventListener('click', () => sendPlayback('channel_up'));
    document.getElementById('btn-ch-down-sage').addEventListener('click', () => sendPlayback('channel_down'));

    // SageTV quick-nav buttons
    document.getElementById('btn-open-home').addEventListener('click', () => sendPlayback('open_home'));
    document.getElementById('btn-open-guide').addEventListener('click', () => sendPlayback('open_guide'));
    document.getElementById('btn-open-recordings').addEventListener('click', () => sendPlayback('open_recordings'));
    document.getElementById('btn-open-live-tv').addEventListener('click', () => sendPlayback('open_live_tv'));

    // Seek
    document.getElementById('seek-slider').addEventListener('change', e => {
      const session = State.get().session;
      if (session && session.duration > 0) {
        const targetPos = (e.target.value / 100) * session.duration;
        const state = State.get();
        if (state.system === 'channelsdvr') {
          // Channels DVR only supports relative seek
          const relativeSeconds = Math.floor(targetPos - (session.position || 0));
          sendPlayback('seek', { seconds: relativeSeconds });
        } else {
          sendPlayback('seek', { position: Math.floor(targetPos) });
        }
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

    // Show title clicks — search and show metadata popup
    document.getElementById('messages').addEventListener('click', async (e) => {
      const link = e.target.closest('.show-link');
      if (!link) return;
      e.preventDefault();
      const title = link.dataset.title;
      if (!title) return;
      const showContext = link.dataset.show || '';
      showMetadataPopup(title, showContext);
    });

    // Close button for show-info dialog
    document.getElementById('show-info-close').addEventListener('click', () => {
      document.getElementById('show-info-dialog').close();
    });

    // View Transcript button clicks (delegated from show-info-body)
    document.getElementById('show-info-body').addEventListener('click', (e) => {
      const btn = e.target.closest('.btn-view-transcript');
      if (!btn) return;
      const recordingId = btn.dataset.recordingId;
      const txTitle = btn.dataset.title;
      if (!recordingId) return;
      document.getElementById('show-info-dialog').close();
      showTranscriptDialog(recordingId, txTitle);
    });

    // Close button for transcript dialog
    document.getElementById('transcript-close').addEventListener('click', () => {
      document.getElementById('transcript-dialog').close();
    });

    // Transcript edit/save/cancel
    document.getElementById('transcript-edit').addEventListener('click', () => {
      const body = document.getElementById('transcript-body');
      const pre = body.querySelector('.transcript-text');
      if (!pre) return;
      pre.contentEditable = 'true';
      pre.classList.add('editing');
      pre.focus();
      document.getElementById('transcript-edit').hidden = true;
      document.getElementById('transcript-save').hidden = false;
      document.getElementById('transcript-cancel-edit').hidden = false;
    });

    document.getElementById('transcript-cancel-edit').addEventListener('click', () => {
      const body = document.getElementById('transcript-body');
      const pre = body.querySelector('.transcript-text');
      if (!pre) return;
      pre.contentEditable = 'false';
      pre.classList.remove('editing');
      pre.textContent = pre.dataset.original || pre.textContent;
      document.getElementById('transcript-edit').hidden = false;
      document.getElementById('transcript-save').hidden = true;
      document.getElementById('transcript-cancel-edit').hidden = true;
    });

    document.getElementById('transcript-save').addEventListener('click', async () => {
      const body = document.getElementById('transcript-body');
      const pre = body.querySelector('.transcript-text');
      if (!pre) return;
      const recordingId = pre.dataset.recordingId;
      const newText = pre.textContent;
      pre.contentEditable = 'false';
      pre.classList.remove('editing');
      document.getElementById('transcript-edit').hidden = false;
      document.getElementById('transcript-save').hidden = true;
      document.getElementById('transcript-cancel-edit').hidden = true;
      pre.dataset.original = newText;
      // TODO: save to backend when endpoint is available
      UI.addMessage('Transcript saved locally (server save coming soon).', 'assistant');
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
      // Handle rename by clicking the name cell
      const nameCell = e.target.closest('[data-action="rename-device"]');
      if (nameCell) {
        const id = nameCell.dataset.id;
        const currentName = nameCell.dataset.name || '';
        const newName = prompt('Enter a friendly name for this device:', currentName);
        if (newName !== null && newName.trim() !== '') {
          await API.updateDevice(id, { friendly_name: newName.trim() });
          UI.refreshAdminDevices();
          State.refreshDevices();
        }
        return;
      }

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
      if (data.dvr_backends) UI.renderDvrGrid(data.dvr_backends);
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

  let _activeAbort = null;

  function cancelQuery() {
    if (_activeAbort) {
      _activeAbort.abort();
      _activeAbort = null;
    }
  }

  async function handleSend() {
    const input = document.getElementById('text-input');
    const text = input.value.trim();
    if (!text) return;
    if (_activeAbort) {
      // Already processing — flash the input to signal the user
      input.classList.add('input-blocked');
      setTimeout(() => input.classList.remove('input-blocked'), 400);
      return;
    }
    // Stop mic if still recording
    if (Voice.isRecording()) Voice.stop();
    input.value = '';
    const sendBtn = document.getElementById('btn-send');
    if (sendBtn) sendBtn.disabled = true;
    UI.addMessage(text, 'user');

    // Show thinking indicator with inline cancel button
    const thinking = document.createElement('div');
    thinking.className = 'message thinking';
    thinking.innerHTML = '<div class="think-current">Thinking</div>' +
      '<button class="think-cancel" title="Cancel" aria-label="Cancel request">✕</button>';
    thinking.querySelector('.think-cancel').addEventListener('click', cancelQuery);
    const msgContainer = document.getElementById('messages');
    msgContainer.appendChild(thinking);
    msgContainer.scrollTop = msgContainer.scrollHeight;

    _activeAbort = new AbortController();

    // Streaming state: when tokens arrive, switch from "thinking" to live text
    let streamEl = null;
    let streamedText = '';
    let statusLog = [];

    try {
      const data = await API.queryStream(text, State.get().llmFocus, (status) => {
        // Status callback — if we were streaming tokens, a new status
        // means the LLM is doing tool calls; revert to thinking indicator
        if (streamEl) {
          streamEl.remove();
          streamEl = null;
          streamedText = '';
        }
        thinking.style.display = '';
        // Accumulate status log so user can see the full chain of events
        statusLog.push(status);
        const logHtml = statusLog.slice(0, -1)
          .map(s => `<div class="think-prev">${esc(s)}</div>`).join('');
        thinking.innerHTML = logHtml +
          `<div class="think-current">${esc(status)}</div>` +
          '<button class="think-cancel" title="Cancel" aria-label="Cancel request">✕</button>';
        thinking.querySelector('.think-cancel').addEventListener('click', cancelQuery);
        msgContainer.scrollTop = msgContainer.scrollHeight;
      }, _activeAbort.signal, (token) => {
        // Token callback — progressively render LLM output
        if (!streamEl) {
          thinking.style.display = 'none';
          streamEl = document.createElement('div');
          streamEl.className = 'message assistant';
          msgContainer.appendChild(streamEl);
        }
        streamedText += token;
        streamEl.textContent = streamedText;
        msgContainer.scrollTop = msgContainer.scrollHeight;
      });

      // Convert thinking indicator into a persistent status log
      if (statusLog.length > 0) {
        thinking.className = 'message status-log';
        thinking.style.display = '';
        thinking.innerHTML = statusLog.map(s => `<div class="status-step">${esc(s)}</div>`).join('');
      } else {
        thinking.remove();
      }

      // If we streamed the response, finalize with proper HTML rendering
      if (streamEl) {
        streamEl.remove();
        streamEl = null;
      }

      const response = data.response || data.llm_response || data.error || 'No response from server.';
      UI.addMessage(response, data.error ? 'error' : 'assistant');

      // Render clickable episode cards if transcript results were returned
      const results = data.transcript_results;
      if (results && results.length > 0) {
        UI.addEpisodeCards(results);
      }
    } catch (e) {
      thinking.remove();
      if (streamEl) streamEl.remove();
      if (e.name === 'AbortError') {
        UI.addMessage('Cancelled.', 'assistant');
      } else {
        UI.addMessage('Error: ' + e.message, 'error');
      }
    } finally {
      _activeAbort = null;
      const inp = document.getElementById('text-input');
      inp.focus();
      const btn = document.getElementById('btn-send');
      if (btn) btn.disabled = false;
    }
  }

  function handleVoiceResult(transcript) {
    document.getElementById('text-input').value = transcript;
    handleSend();
  }

  async function showMetadataPopup(title, showContext = '') {
    const dialog = document.getElementById('show-info-dialog');
    const titleEl = document.getElementById('show-info-title');
    const bodyEl = document.getElementById('show-info-body');

    // If showContext is provided, this is an episode-title click
    // Search for the parent show, then filter to the episode
    const searchTerm = showContext || title;
    const episodeFilter = showContext ? title : '';

    titleEl.textContent = episodeFilter ? `${searchTerm} — ${title}` : title;
    bodyEl.innerHTML = '<p class="show-info-loading">Searching...</p>';
    dialog.showModal();

    try {
      // Search DVR and transcripts in parallel
      const [data, txData] = await Promise.all([
        API.search(searchTerm),
        API.searchTranscripts(searchTerm).catch(() => ({ results: [] })),
      ]);
      const txResults = txData?.results || [];

      // Check if the response itself is an error
      if (!data || data.error) {
        bodyEl.innerHTML = '<p>Show information could not be retrieved at this time.</p>';
        return;
      }

      const items = [];

      // Collect results from all systems
      let anySystemReachable = false;
      for (const [sys, res] of Object.entries(data)) {
        if (sys === 'transcripts') continue;
        if (res?.error) continue; // that backend is down
        anySystemReachable = true;
        const list = res?.data?.results || res?.results || res?.data || [];
        if (Array.isArray(list)) {
          list.forEach(r => items.push({ ...r, _system: sys }));
        }
      }

      if (!anySystemReachable) {
        bodyEl.innerHTML = '<p>Show information could not be retrieved — DVR services are currently unavailable.</p>';
        return;
      }

      // Helper: extract description from any result format
      function getDesc(r) {
        const show = (r.Airing || {}).Show || {};
        return r.description || r.summary || r.synopsis || show.ShowDescription || '';
      }
      // Helper: extract show title from any result format
      function getShowTitle(r) {
        const show = (r.Airing || {}).Show || {};
        return r.title || r.show_title || show.ShowTitle || '';
      }
      // Helper: extract episode title from any result format
      function getEpTitle(r) {
        const show = (r.Airing || {}).Show || {};
        return r.episode_title || r.episode || show.ShowEpisode || '';
      }

      // Filter to items whose show title or episode title matches the clicked text
      const q = title.toLowerCase();
      const sq = searchTerm.toLowerCase();
      let exact;
      if (episodeFilter) {
        // Episode click: find recordings of the parent show, then filter to matching episode
        const epQ = episodeFilter.toLowerCase();
        exact = items.filter(r => {
          const show = getShowTitle(r).toLowerCase();
          const ep = getEpTitle(r).toLowerCase();
          return (show === sq || show.includes(sq)) &&
                 (ep === epQ || ep.includes(epQ));
        });
        // Fallback: show all episodes of that show if exact episode not found
        if (exact.length === 0) {
          exact = items.filter(r => {
            const show = getShowTitle(r).toLowerCase();
            return show === sq || show.includes(sq);
          });
        }
      } else {
        exact = items.filter(r => {
          const show = getShowTitle(r).toLowerCase();
          const ep = getEpTitle(r).toLowerCase();
          return show === q || ep === q || show.includes(q) || ep.includes(q);
        });
      }
      const display = exact.length > 0 ? exact : (episodeFilter ? [] : items);

      if (display.length === 0) {
        bodyEl.innerHTML = '<p>No recordings or upcoming episodes found for this title.</p>';
        return;
      }

      // Determine if this is a show-name click (not an episode name)
      const isShowClick = !episodeFilter && display.some(r => getShowTitle(r).toLowerCase() === q);
      const isEpisodeClick = episodeFilter || display.some(r => getEpTitle(r).toLowerCase() === q);

      // For show-name clicks on Channels recordings, look for a SageTV series description
      let seriesDesc = '';
      if (isShowClick && !isEpisodeClick) {
        const sage = items.find(r => {
          const show = (r.Airing || {}).Show || {};
          return r._system === 'sagetv' && getShowTitle(r).toLowerCase().includes(q)
            && show.ShowDescription;
        });
        if (sage) {
          seriesDesc = (sage.Airing || {}).Show.ShowDescription;
        }
      }

      let html = '';

      // Show series description header or unavailable notice
      if (isShowClick && !isEpisodeClick) {
        if (seriesDesc) {
          html += `<div class="si-series-desc">${esc(seriesDesc)}</div>`;
        } else {
          html += '<div class="si-series-unavail">Series description not available.</div>';
        }
        bodyEl.innerHTML = html;
        return;
      }

      display.slice(0, 10).forEach(r => {
        // Normalize fields across Channels (flat) and SageTV (nested Airing.Show)
        const airing = r.Airing || {};
        const show = airing.Show || {};
        const showTitle = r.title || r.show_title || show.ShowTitle || title;
        const ep = r.episode_title || r.episode || show.ShowEpisode || '';
        const seRaw = r.season_episode || '';
        let seStr = seRaw;
        if (!seStr) {
          const season = r.season != null ? r.season : show.ShowSeasonNumber;
          const epNum = r.episode_number != null ? r.episode_number : (r.episode != null ? r.episode : show.ShowEpisodeNumber);
          const s = season != null ? `S${String(season).padStart(2,'0')}` : '';
          const e = epNum != null ? `E${String(epNum).padStart(2,'0')}` : '';
          seStr = (s || e) ? `${s}${e}` : '';
        }
        const ch = airing.Channel || {};
        const channel = r.channel || r.channel_name || ch.ChannelName || '';
        const dur = r.duration_min || r.duration || 0;
        const duration = dur ? `${Math.round(dur > 1000 ? dur / 60 : dur)} min` : '';
        const airDate = r.air_date || r.original_air_date || r.original_date || r.recorded || r.start_time || '';
        const desc = getDesc(r);
        const rating = r.content_rating || '';
        const system = r._system === 'upcoming' ? 'scheduled' : (r._system || '');
        const img = r.image || '';
        const cast = r.cast || show.ShowCast || [];
        const genres = r.genres || show.ShowGenres || show.ShowCategory || [];
        const genreList = Array.isArray(genres) ? genres : (genres ? [genres] : []);
        const castList = Array.isArray(cast) ? cast : (cast ? [cast] : []);

        // Find matching transcript by title+episode
        const epLower = ep.toLowerCase();
        const showLower = showTitle.toLowerCase();
        const txMatch = txResults.find(t =>
          t.title && t.title.toLowerCase() === showLower &&
          (!ep || (t.episode && t.episode.toLowerCase() === epLower))
        );

        html += '<div class="show-info-card">';
        if (img) html += `<img class="si-thumb" src="${esc(img)}" alt="" loading="lazy">`;
        html += `<div class="si-body">`;
        html += `<div class="si-title">${esc(showTitle)}`;
        if (seStr) html += ` <span class="si-ep">${esc(seStr)}</span>`;
        html += '</div>';
        if (ep) html += `<div class="si-episode">${esc(ep)}</div>`;
        const meta = [channel, duration, airDate, rating, system].filter(Boolean);
        if (meta.length) html += `<div class="si-meta">${meta.map(esc).join(' · ')}</div>`;
        if (r.watched != null) {
          const wClass = r.watched ? 'si-watched' : 'si-unwatched';
          const wText = r.watched ? '✅ Watched' : '❌ Unwatched';
          html += `<div class="${wClass}">${wText}</div>`;
        }
        if (desc) html += `<div class="si-desc">${esc(desc)}</div>`;
        if (genreList.length) html += `<div class="si-genres">${genreList.map(esc).join(', ')}</div>`;
        if (castList.length) html += `<div class="si-cast">Cast: ${castList.slice(0, 6).map(esc).join(', ')}</div>`;
        if (txMatch) {
          html += `<button class="btn-view-transcript" data-recording-id="${esc(txMatch.recording_id)}" data-title="${esc(showTitle + (ep ? ' — ' + ep : ''))}">📝 View Transcript</button>`;
        } else {
          html += `<div class="si-no-transcript">No transcript available</div>`;
        }
        html += '</div></div>';
      });

      bodyEl.innerHTML = html;
    } catch (err) {
      bodyEl.innerHTML = `<p class="si-error">Error: ${esc(err.message)}</p>`;
    }
  }

  async function showTranscriptDialog(recordingId, txTitle) {
    const dialog = document.getElementById('transcript-dialog');
    const titleEl = document.getElementById('transcript-title');
    const metaEl = document.getElementById('transcript-meta');
    const bodyEl = document.getElementById('transcript-body');
    const actionsEl = document.getElementById('transcript-actions');

    titleEl.textContent = txTitle || 'Transcript';
    metaEl.innerHTML = '';
    bodyEl.innerHTML = '<p class="show-info-loading">Loading transcript...</p>';
    actionsEl.hidden = true;
    document.getElementById('transcript-edit').hidden = false;
    document.getElementById('transcript-save').hidden = true;
    document.getElementById('transcript-cancel-edit').hidden = true;
    dialog.showModal();

    try {
      const data = await API.getTranscript(recordingId);
      if (!data || data.error) {
        bodyEl.innerHTML = `<p>Transcript not found.</p>`;
        return;
      }

      // Meta bar
      const metaParts = [];
      if (data.word_count) metaParts.push(`${data.word_count.toLocaleString()} words`);
      if (data.duration) metaParts.push(`${Math.round(data.duration / 60)} min`);
      if (data.system) metaParts.push(data.system);
      metaEl.textContent = metaParts.join(' · ');

      // Summary
      let html = '';
      if (data.summary) {
        html += `<div class="transcript-summary"><strong>Summary:</strong> ${esc(data.summary)}</div>`;
      }
      if (data.keywords && data.keywords.length) {
        html += `<div class="transcript-keywords">${data.keywords.map(k => `<span class="tx-keyword">${esc(k)}</span>`).join(' ')}</div>`;
      }

      // Transcript text
      const text = data.transcript || '(No transcript text available)';
      html += `<pre class="transcript-text" data-recording-id="${esc(recordingId)}" data-original="${esc(text)}">${esc(text)}</pre>`;

      bodyEl.innerHTML = html;
      actionsEl.hidden = false;
    } catch (err) {
      bodyEl.innerHTML = `<p class="si-error">Error: ${esc(err.message)}</p>`;
    }
  }

  function esc(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  async function sendPlayback(action, params = {}) {
    const state = State.get();
    const pbParams = { system: state.system, ...params };
    // If a bridge device is selected, pass its name as 'device' for the MCP tool
    if (state.deviceId.startsWith('bridge:')) {
      pbParams.device = state.deviceId.slice('bridge:'.length);
    } else if (state.deviceId) {
      pbParams.device_id = state.deviceId;
    }
    // For play_pause on SageTV, send action_hint based on UI state
    if (action === 'play_pause' && state.system === 'sagetv' && state.session) {
      pbParams.action_hint = state.session.state === 'playing' ? 'pause' : 'play';
    }
    try {
      await API.playback(action, pbParams);
      State.refreshSession();
    } catch (e) {
      UI.addMessage('Playback error: ' + e.message, 'error');
    }
  }
})();
