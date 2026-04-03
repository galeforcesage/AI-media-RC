/**
 * ui.js — DOM rendering and updates.
 */
const UI = (() => {
  // Element cache
  const el = {};
  const ids = [
    'system-picker', 'device-picker',
    'np-title', 'np-episode', 'np-channel', 'np-state',
    'np-position', 'np-duration', 'seek-slider',
    'play-pause-icon', 'mute-icon', 'volume-slider',
    'btn-commercial-skip',
    'messages', 'text-input',
    'footer-status',
    'settings-dialog', 'admin-dialog',
    'setting-default-system', 'setting-api-url',
    'device-list', 'system-output',
    'transcription-stats', 'transcription-jobs',
  ];

  function cacheElements() {
    ids.forEach(id => { el[id] = document.getElementById(id); });
  }

  // ─── Now‑Playing ──────────────────────────────────────────

  function updateNowPlaying(session) {
    if (!session || !session.title) {
      el['np-title'].textContent = 'No active playback';
      el['np-episode'].textContent = '';
      el['np-channel'].textContent = '';
      el['np-state'].textContent = '';
      el['seek-slider'].value = 0;
      el['np-position'].textContent = '0:00';
      el['np-duration'].textContent = '0:00';
      el['play-pause-icon'].textContent = '▶';
      return;
    }
    el['np-title'].textContent = session.title || '';
    el['np-episode'].textContent = session.episode || '';
    el['np-channel'].textContent = session.channel || '';
    el['np-state'].textContent = session.state || '';

    el['play-pause-icon'].textContent = session.state === 'playing' ? '⏸' : '▶';

    if (session.duration > 0) {
      const pct = (session.position / session.duration) * 100;
      el['seek-slider'].value = pct;
      el['np-position'].textContent = formatTime(session.position);
      el['np-duration'].textContent = formatTime(session.duration);
    }

    // Show commercial skip for both SageTV (Comskip plugin) and Channels DVR
    el['btn-commercial-skip'].hidden = false;
  }

  function formatTime(seconds) {
    if (!seconds || seconds < 0) return '0:00';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
    return `${m}:${String(s).padStart(2, '0')}`;
  }

  // ─── Chat Messages ────────────────────────────────────────

  function addMessage(text, sender = 'user') {
    const bubble = document.createElement('div');
    bubble.className = `message ${sender}`;
    bubble.textContent = text;
    el['messages'].appendChild(bubble);
    el['messages'].scrollTop = el['messages'].scrollHeight;
  }

  function addMessageHTML(html, sender = 'assistant') {
    const bubble = document.createElement('div');
    bubble.className = `message ${sender}`;
    bubble.innerHTML = html;
    el['messages'].appendChild(bubble);
    el['messages'].scrollTop = el['messages'].scrollHeight;
  }

  function addEpisodeCards(results, sender = 'assistant') {
    if (!results || results.length === 0) return;
    const bubble = document.createElement('div');
    bubble.className = `message ${sender} episode-results`;

    const heading = document.createElement('div');
    heading.className = 'episode-results-heading';
    heading.textContent = 'Matching episodes — click to play:';
    bubble.appendChild(heading);

    results.forEach(r => {
      const card = document.createElement('button');
      card.className = 'episode-card';
      card.dataset.recordingId = r.recording_id || '';
      card.dataset.title = r.title || '';
      card.dataset.system = r.system || '';

      const title = r.title || 'Unknown';
      const ep = r.episode_title ? ` — ${r.episode_title}` : '';
      const time = r.start_time != null ? ` at ${formatTime(r.start_time)}` : '';
      const channel = r.channel ? ` (${r.channel})` : '';
      const snippet = (r.snippet || '').replace(/<b>/g, '').replace(/<\/b>/g, '');

      card.innerHTML =
        `<span class="ec-title">${esc(title)}${esc(ep)}</span>` +
        `<span class="ec-meta">${esc(channel)}${esc(time)}</span>` +
        (snippet ? `<span class="ec-snippet">${esc(snippet.substring(0, 120))}…</span>` : '');

      bubble.appendChild(card);
    });

    el['messages'].appendChild(bubble);
    el['messages'].scrollTop = el['messages'].scrollHeight;
  }

  function clearMessages() {
    el['messages'].innerHTML = '';
  }

  // ─── Device Picker ────────────────────────────────────────

  function updateDevicePicker(devices, selectedId) {
    const picker = el['device-picker'];
    picker.innerHTML = '<option value="">Select device...</option>';
    devices.forEach(d => {
      const opt = document.createElement('option');
      opt.value = d.device_id;
      opt.textContent = d.friendly_name || d.device_id;
      if (d.device_id === selectedId) opt.selected = true;
      picker.appendChild(opt);
    });
  }

  // ─── Connection Status ────────────────────────────────────

  function updateStatus(connected) {
    const status = el['footer-status'];
    if (connected) {
      status.textContent = 'Connected';
      status.classList.add('connected');
      status.classList.remove('disconnected');
    } else {
      status.textContent = 'Disconnected';
      status.classList.add('disconnected');
      status.classList.remove('connected');
    }
  }

  // ─── Admin: Device List ───────────────────────────────────

  function renderDeviceList(devices) {
    const container = el['device-list'];
    if (!devices || devices.length === 0) {
      container.innerHTML = '<p class="empty">No devices registered.</p>';
      return;
    }
    const table = document.createElement('table');
    table.className = 'admin-table';
    table.innerHTML = `<thead><tr>
      <th>Name</th><th>Type</th><th>System</th><th>Default</th><th></th>
    </tr></thead>`;
    const tbody = document.createElement('tbody');
    devices.forEach(d => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${esc(d.friendly_name || d.device_id)}</td>
        <td>${esc(d.platform || '-')}</td>
        <td>${esc(d.system || '-')}</td>
        <td>${d.is_default ? '★' : ''}</td>
        <td>
          <button class="btn-tiny btn-danger" data-action="delete-device" data-id="${esc(d.device_id)}">✕</button>
          ${d.is_default ? '' : `<button class="btn-tiny" data-action="set-default" data-id="${esc(d.device_id)}">Set Default</button>`}
        </td>`;
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    container.innerHTML = '';
    container.appendChild(table);
  }

  function renderSystemOutput(text) {
    el['system-output'].textContent = text;
  }

  function renderTranscriptionStats(stats) {
    if (!stats) {
      el['transcription-stats'].innerHTML = '<p class="empty">No stats available.</p>';
      return;
    }
    el['transcription-stats'].innerHTML = `
      <p>Total transcripts: <strong>${stats.total || 0}</strong></p>
      <p>Pending jobs: <strong>${stats.pending || 0}</strong></p>
      <p>In progress: <strong>${stats.in_progress || 0}</strong></p>`;
  }

  // ─── Settings Dialog ──────────────────────────────────────

  function openSettings() {
    el['setting-default-system'].value = State.get().system;
    el['setting-api-url'].value = API.baseUrl;
    el['settings-dialog'].showModal();
  }

  function openAdmin() {
    el['admin-dialog'].showModal();
    refreshAdminDevices();
  }

  async function refreshAdminDevices() {
    try {
      const data = await API.listDevices();
      if (data.success) renderDeviceList(data.devices);
    } catch (e) {
      renderDeviceList([]);
    }
  }

  // ─── Helpers ──────────────────────────────────────────────

  function esc(str) {
    const d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
  }

  return {
    cacheElements, updateNowPlaying, formatTime,
    addMessage, addMessageHTML, addEpisodeCards, clearMessages,
    updateDevicePicker, updateStatus,
    renderDeviceList, renderSystemOutput, renderTranscriptionStats,
    openSettings, openAdmin, refreshAdminDevices,
    el: () => el,
  };
})();
