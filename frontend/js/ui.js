/**
 * ui.js — DOM rendering and updates.
 */
const UI = (() => {
  // Element cache
  const el = {};
  const ids = [
    'llm-focus', 'llm-focus-toggle', 'llm-focus-menu',
    'remote-system', 'device-picker',
    'remote-controls', 'now-playing', 'transport-wrapper', 'transport',
    'np-title', 'np-episode', 'np-channel', 'np-state',
    'np-position', 'np-duration', 'seek-slider',
    'play-pause-icon', 'mute-icon', 'volume-slider',
    'btn-commercial-skip',
    'dpad-section', 'dpad-sagetv', 'dpad-channels',
    'btn-ch-up', 'btn-ch-down', 'btn-toggle-cc',
    'messages', 'text-input',
    'footer-status',
    'settings-dialog', 'admin-dialog',
    'setting-default-system', 'setting-api-url',
    'device-list', 'system-output',
    'transcription-grid',
    'service-grid',
    'dvr-grid',
    'gpu-grid',
    'ollama-grid',
  ];

  function cacheElements() {
    ids.forEach(id => { el[id] = document.getElementById(id); });
  }

  // ─── Now‑Playing ──────────────────────────────────────────

  function updateNowPlaying(session, deviceId) {
    const hasDevice = !!deviceId;
    if (el['remote-controls']) el['remote-controls'].hidden = !hasDevice;
    const slider = el['seek-slider'];
    if (!session || !session.title) {
      el['np-title'].textContent = 'No active playback';
      el['np-episode'].textContent = '';
      el['np-channel'].textContent = '';
      el['np-state'].textContent = '';
      slider.value = 0;
      slider.disabled = true;
      slider.classList.add('disabled');
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
      slider.value = pct;
      slider.disabled = false;
      slider.classList.remove('disabled');
      el['np-position'].textContent = formatTime(session.position);
      el['np-duration'].textContent = formatTime(session.duration);
    } else {
      // Live TV or unknown duration — show position only, disable seek
      slider.value = 0;
      slider.disabled = true;
      slider.classList.add('disabled');
      el['np-position'].textContent = session.position > 0 ? formatTime(session.position) : '0:00';
      el['np-duration'].textContent = 'LIVE';
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
    if (sender === 'assistant') {
      // Linkify quoted show titles — "Title" becomes clickable
      // esc() may or may not convert " to &quot; depending on browser
      const safe = esc(text);
      // First pass: convert both &quot; and " forms to clickable links
      let linked = safe
        .replace(/&quot;([^&<>]+?)&quot;/g,
          '<a href="#" class="show-link" data-title="$1">&ldquo;$1&rdquo;</a>')
        .replace(/"([^"<>]+?)"/g,
          '<a href="#" class="show-link" data-title="$1">&ldquo;$1&rdquo;</a>');
      // Second pass: for each line/list-item with 2+ show-links,
      // tag the 2nd+ links with data-show from the 1st link (episod context)
      const temp = document.createElement('div');
      temp.innerHTML = linked;
      for (const li of temp.querySelectorAll('*')) {
        // Only process text-bearing elements (li, p, div, or top-level text)
      }
      // Process each line (split by <br> or within list items)
      const lines = temp.innerHTML.split(/(<br\s*\/?>|\n)/);
      const rebuilt = lines.map(line => {
        const matches = [...line.matchAll(/data-title="([^"]+)"/g)];
        if (matches.length >= 2) {
          const showName = matches[0][1];
          // Tag all links after the first with data-show
          let first = true;
          line = line.replace(/(<a href="#" class="show-link" data-title="[^"]+")>/g,
            (full, prefix) => {
              if (first) { first = false; return full; }
              return prefix + ' data-show="' + showName + '">';
            });
        }
        return line;
      });
      bubble.innerHTML = rebuilt.join('');
    } else {
      bubble.textContent = text;
    }
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
    heading.textContent = 'Matching episodes:';
    bubble.appendChild(heading);

    results.forEach(r => {
      const card = document.createElement('div');
      card.className = 'episode-card';
      card.dataset.recordingId = r.recording_id || '';
      card.dataset.title = r.display_title || r.title || '';
      card.dataset.system = r.system || '';

      // Use parsed display_title if available, otherwise strip date from raw title
      const showName = r.display_title || (r.title || 'Unknown').replace(/\s+\d{4}-\d{2}-\d{2}-\d{4}$/, '');
      const epTitle = r.episode_title || '';
      const se = r.se_label || '';
      const displayTitle = epTitle
        ? `${showName} — ${epTitle}${se ? ' ' + se : ''}`
        : showName;
      const linkTitle = epTitle ? `${showName} — ${epTitle}` : showName;
      const time = r.start_time != null ? ` at ${formatTime(r.start_time)}` : '';
      const channel = r.channel ? ` (${r.channel})` : '';
      const snippet = (r.snippet || '').replace(/<b>/g, '').replace(/<\/b>/g, '');

      card.innerHTML =
        `<div class="ec-text">` +
        `<a class="ec-title ec-transcript-link" href="#" data-recording-id="${esc(r.recording_id || '')}" data-title="${esc(linkTitle)}">${esc(displayTitle)}</a>` +
        `<span class="ec-meta">${esc(channel)}${esc(time)}</span>` +
        (snippet ? `<span class="ec-snippet">${esc(snippet.substring(0, 120))}…</span>` : '') +
        `</div>` +
        `<button class="ec-play-btn" title="Play on device">▶</button>`;

      bubble.appendChild(card);
    });

    el['messages'].appendChild(bubble);
    el['messages'].scrollTop = el['messages'].scrollHeight;
  }

  function clearMessages() {
    el['messages'].innerHTML = '';
  }

  // ─── Picker Sync ──────────────────────────────────────────

  function updatePicker(id, value) {
    const picker = el[id];
    if (picker && picker.value !== value) picker.value = value;
  }

  function updateLLMFocusCheckboxes(systems) {
    const menu = el['llm-focus-menu'];
    if (!menu) return;
    menu.querySelectorAll('input[type="checkbox"]').forEach(cb => {
      cb.checked = systems.includes(cb.value);
    });
    updateLLMFocusLabel(systems);
  }

  function updateLLMFocusLabel(systems) {
    const toggle = el['llm-focus-toggle'];
    if (!toggle) return;
    if (systems.length === 2) toggle.textContent = 'Both ▾';
    else if (systems.includes('sagetv')) toggle.textContent = 'SageTV ▾';
    else toggle.textContent = 'Channels ▾';
  }

  // ─── Device Picker ────────────────────────────────────────

  function updateDevicePicker(devices, selectedId, bridgeDevices, system) {
    const picker = el['device-picker'];
    picker.innerHTML = '<option value="">Select device...</option>';

    // Show registered session-manager devices
    if (devices && devices.length > 0) {
      devices.forEach(d => {
        const opt = document.createElement('option');
        opt.value = d.device_id;
        opt.textContent = d.friendly_name || d.device_id;
        if (d.device_id === selectedId) opt.selected = true;
        picker.appendChild(opt);
      });
    }

    // When Channels DVR is selected, show connected playback devices
    if (system === 'channelsdvr' && bridgeDevices && bridgeDevices.length > 0) {
      const group = document.createElement('optgroup');
      group.label = 'Playback Devices (online)';
      bridgeDevices.forEach(d => {
        const opt = document.createElement('option');
        const name = d.device_name || d.device_model || 'Unknown';
        const icon = d.device_type === 'direct' ? '📺' : '🟢';
        const label = d.device_type === 'direct' ? 'direct' : 'bridge';
        opt.value = `bridge:${name}`;
        opt.textContent = `${icon} ${name} (${d.device_model || ''} · ${label})`;
        if (`bridge:${name}` === selectedId) opt.selected = true;
        group.appendChild(opt);
      });
      picker.appendChild(group);
    }
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
      <th>Name</th><th>ID</th><th>System</th><th>Default</th><th></th>
    </tr></thead>`;
    const tbody = document.createElement('tbody');
    devices.forEach(d => {
      const tr = document.createElement('tr');
      const nameDisplay = d.friendly_name && d.friendly_name !== d.device_id
        ? esc(d.friendly_name)
        : `<em>${esc(d.device_id)}</em>`;
      // Show short context ID for sagetv-ctx- devices
      const shortId = d.device_id.startsWith('sagetv-ctx-')
        ? d.device_id.slice('sagetv-ctx-'.length)
        : d.device_id;
      tr.innerHTML = `
        <td class="device-name-cell" data-action="rename-device" data-id="${esc(d.device_id)}" data-name="${esc(d.friendly_name || '')}" title="Click to rename">${nameDisplay}</td>
        <td class="device-id-cell">${esc(shortId)}</td>
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

  // ─── Admin: Service Grid ─────────────────────────────────

  function renderServiceGrid(services) {
    const grid = el['service-grid'];
    if (!services || Object.keys(services).length === 0) {
      grid.innerHTML = '<p class="empty">No services found.</p>';
      return;
    }
    grid.innerHTML = '';
    Object.entries(services).forEach(([id, svc]) => {
      const card = document.createElement('div');
      card.className = 'service-card';
      const statusCls = svc.status === 'up' ? 'up' : svc.status === 'degraded' ? 'degraded' : 'down';
      const latency = svc.latency_ms != null ? `${svc.latency_ms}ms` : '';

      let detailParts = [`:${svc.port}`];
      if (latency) detailParts.push(latency);

      // Transcription extras (shown in Transcription tab now)
      let extras = '';

      card.innerHTML =
        `<div class="svc-left">` +
          `<span class="svc-dot ${statusCls}"></span>` +
          `<div class="svc-info">` +
            `<span class="svc-name">${esc(svc.name)}</span>` +
            `<span class="svc-detail">${esc(detailParts.join(' · '))}</span>` +
            extras +
          `</div>` +
        `</div>` +
        `<div class="svc-actions">` +
          `<button class="btn-tiny btn-restart" data-service-id="${esc(id)}">↻ Restart</button>` +
        `</div>`;
      grid.appendChild(card);
    });
  }

  function renderDvrGrid(backends) {
    const grid = el['dvr-grid'];
    if (!backends || Object.keys(backends).length === 0) {
      grid.innerHTML = '<p class="empty">No DVR backends configured.</p>';
      return;
    }
    grid.innerHTML = '';
    Object.entries(backends).forEach(([id, svc]) => {
      const card = document.createElement('div');
      card.className = 'service-card';
      const statusCls = svc.status === 'up' ? 'up' : svc.status === 'degraded' ? 'degraded' : 'down';
      const latency = svc.latency_ms != null ? `${svc.latency_ms}ms` : '';
      card.innerHTML =
        `<div class="svc-left">` +
          `<span class="svc-dot ${statusCls}"></span>` +
          `<span class="svc-name">${esc(svc.name)}</span>` +
          `<span class="svc-detail">:${svc.port}${latency ? ' · ' + latency : ''}</span>` +
        `</div>`;
      grid.appendChild(card);
    });
  }

  function renderSystemOutput(text) {
    el['system-output'].textContent = text;
  }

  function renderGpuGrid(info) {
    const grid = el['gpu-grid'];
    if (!grid) return;
    if (!info) {
      grid.innerHTML = '<p class="empty">No GPU info available.</p>';
      return;
    }
    const cards = [];
    const cudaUp = !!info.cuda_available;
    const cudaCls = cudaUp ? 'up' : 'down';
    const cudaText = cudaUp
      ? `Driver ${esc(info.driver_version || '?')}`
      : `unavailable${info.error ? ' · ' + esc(info.error) : ''}`;
    cards.push(
      `<div class="service-card">` +
        `<div class="svc-left">` +
          `<span class="svc-dot ${cudaCls}"></span>` +
          `<span class="svc-name">CUDA</span>` +
          `<span class="svc-detail">${cudaText}</span>` +
        `</div>` +
      `</div>`
    );
    (info.gpus || []).forEach(g => {
      const usedPct = g.memory_total_mb
        ? Math.round((g.memory_used_mb || 0) / g.memory_total_mb * 100)
        : null;
      const memStr = g.memory_total_mb
        ? `${g.memory_used_mb || 0} / ${g.memory_total_mb} MB${usedPct != null ? ' (' + usedPct + '%)' : ''}`
        : '';
      const utilStr = g.utilization_pct != null ? ` · util ${g.utilization_pct}%` : '';
      cards.push(
        `<div class="service-card">` +
          `<div class="svc-left">` +
            `<span class="svc-dot up"></span>` +
            `<span class="svc-name">${esc(g.name)}</span>` +
            `<span class="svc-detail">${memStr}${utilStr}</span>` +
          `</div>` +
        `</div>`
      );
    });
    grid.innerHTML = cards.join('');

    // Ollama models -> Services tab
    const ollamaGrid = el['ollama-grid'];
    if (!ollamaGrid) return;
    const oCards = [];
    (info.ollama_models || []).forEach(m => {
      const onGpu = /GPU/i.test(m.processor) && !/100% CPU/i.test(m.processor);
      const cls = onGpu ? 'up' : 'degraded';
      oCards.push(
        `<div class="service-card">` +
          `<div class="svc-left">` +
            `<span class="svc-dot ${cls}"></span>` +
            `<span class="svc-name">${esc(m.name)}</span>` +
            `<span class="svc-detail">${esc(m.size)} · ${esc(m.processor)}${m.context ? ' · ctx ' + esc(m.context) : ''}</span>` +
          `</div>` +
        `</div>`
      );
    });
    if (oCards.length === 0) {
      oCards.push(`<div class="service-card"><div class="svc-left"><span class="svc-dot unknown"></span><span class="svc-name">Ollama</span><span class="svc-detail">no models loaded</span></div></div>`);
    }
    ollamaGrid.innerHTML = oCards.join('');
  }

  function renderTranscriptionTab(svc) {
    const grid = el['transcription-grid'];
    if (!grid) return;
    if (!svc) {
      grid.innerHTML = '<p class="empty">Transcription service unavailable.</p>';
      return;
    }
    const cards = [];
    // Service status card
    const statusCls = svc.status === 'up' ? 'up' : 'down';
    const latency = svc.latency_ms != null ? `${svc.latency_ms}ms` : '';
    cards.push(
      `<div class="service-card"><div class="svc-left">` +
        `<span class="svc-dot ${statusCls}"></span>` +
        `<span class="svc-name">Transcription</span>` +
        `<span class="svc-detail">:${svc.port}${latency ? ' · ' + latency : ''}</span>` +
      `</div></div>`
    );
    // Diarization + queue
    if (svc.diarization != null) {
      const diarCls = svc.diarization ? 'up' : 'down';
      const diarLabel = svc.diarization ? 'on' : 'off';
      let queueStr = '';
      if (svc.queue) {
        const parts = [];
        if (svc.queue.processing > 0) parts.push(`${svc.queue.processing} processing`);
        if (svc.queue.pending > 0) parts.push(`${svc.queue.pending} pending`);
        if (parts.length) queueStr = ' · ' + parts.join(' · ');
      }
      cards.push(
        `<div class="service-card"><div class="svc-left">` +
          `<span class="svc-dot ${diarCls}"></span>` +
          `<span class="svc-name">Diarization</span>` +
          `<span class="svc-detail">${diarLabel}${queueStr}</span>` +
        `</div></div>`
      );
    }
    // Named shows + diarized (combined info card, no status dot)
    if (svc.named_shows != null) {
      let detail = `${svc.named_shows} shows transcribed`;
      if (svc.diarized_shows != null) detail += ` · ${svc.diarized_shows} with speakers`;
      cards.push(
        `<div class="service-card"><div class="svc-left">` +
          `<span class="svc-name">Library</span>` +
          `<span class="svc-detail">${detail}</span>` +
        `</div></div>`
      );
    }
    // Source breakdown: how many shows came from CC vs STT vs mixed
    if (svc.cc_only_count != null || svc.stt_full_count != null) {
      const cc = svc.cc_only_count || 0;
      const stt = svc.stt_full_count || 0;
      const mixed = svc.mixed_count || 0;
      const parts = [`CC: ${cc}`, `STT: ${stt}`];
      if (mixed > 0) parts.push(`Mixed: ${mixed}`);
      cards.push(
        `<div class="service-card"><div class="svc-left">` +
          `<span class="svc-name">Sources</span>` +
          `<span class="svc-detail">${esc(parts.join(' · '))}</span>` +
        `</div></div>`
      );
    }
    grid.innerHTML = cards.join('');
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
    // Trigger service grid refresh via custom event
    el['admin-dialog'].dispatchEvent(new CustomEvent('admin-opened'));
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

  function updateDpad(system, deviceId) {
    if (!el['dpad-section']) return;
    const hasDevice = !!deviceId;
    el['dpad-section'].hidden = !hasDevice;
    // Show the right sub-panel
    const sagePad = document.getElementById('dpad-sagetv');
    const chPad = document.getElementById('dpad-channels');
    if (sagePad) sagePad.hidden = system !== 'sagetv';
    if (chPad) chPad.hidden = system !== 'channelsdvr';
  }

  return {
    cacheElements, updateNowPlaying, formatTime, updateDpad,
    addMessage, addMessageHTML, addEpisodeCards, clearMessages,
    updatePicker, updateLLMFocusCheckboxes, updateLLMFocusLabel,
    updateDevicePicker, updateStatus,
    renderDeviceList, renderServiceGrid, renderDvrGrid, renderGpuGrid, renderSystemOutput, renderTranscriptionTab,
    openSettings, openAdmin, refreshAdminDevices,
    el: () => el,
  };
})();
