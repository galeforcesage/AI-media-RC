/**
 * api.js — HTTP client for the orchestrator and session manager.
 */
const API = (() => {
  const origin = window.location.origin;
  let baseUrl = localStorage.getItem('api_url') || origin;
  let sessionUrl = localStorage.getItem('session_url') || `${origin}/session`;

  function setBaseUrl(url) { baseUrl = url; localStorage.setItem('api_url', url); }
  function setSessionUrl(url) { sessionUrl = url; localStorage.setItem('session_url', url); }

  async function request(url, method = 'GET', body = null) {
    const opts = { method, headers: { 'Content-Type': 'application/json' }, credentials: 'include' };
    if (body) opts.body = JSON.stringify(body);
    let resp;
    try {
      resp = await fetch(url, opts);
    } catch (err) {
      return { error: 'Network error. Check your connection.' };
    }
    if (resp.status === 401) {
      window.location.href = '/login.html';
      throw new Error('unauthorized');
    }
    const text = await resp.text();
    try {
      return JSON.parse(text);
    } catch (_) {
      return { error: resp.ok ? 'Unexpected response from server.' : 'Server error (' + resp.status + '). Try again.' };
    }
  }

  // Auth
  async function authCheck() {
    const resp = await fetch(`${sessionUrl}/auth/check`, { credentials: 'include' });
    return resp.json();
  }

  async function authLogout() {
    return request(`${sessionUrl}/auth/logout`, 'POST');
  }

  async function adminLogin(username, password) {
    const resp = await fetch(`${sessionUrl}/auth/admin/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ username, password }),
    });
    return resp.json();
  }

  async function adminCheck() {
    const resp = await fetch(`${sessionUrl}/auth/admin/check`, { credentials: 'include' });
    return resp.json();
  }

  async function adminLogout() {
    return request(`${sessionUrl}/auth/admin/logout`, 'POST');
  }

  // Orchestrator endpoints
  async function query(text, systems) {
    return request(`${baseUrl}/api/query`, 'POST', { prompt: text, systems: systems || undefined });
  }

  /**
   * Streaming query via SSE — calls onStatus(msg) for each status update,
   * then returns the final result object.
   */
  async function queryStream(text, systems, onStatus, signal, onToken) {
    const resp = await fetch(`${baseUrl}/api/query/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ prompt: text, systems: systems || undefined }),
      signal: signal || undefined,
    });
    if (resp.status === 401) { window.location.href = '/login.html'; throw new Error('unauthorized'); }
    if (!resp.ok) return { error: 'Server error (' + resp.status + ')' };

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let finalResult = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop(); // keep incomplete line in buffer
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const evt = JSON.parse(line.slice(6));
          if (evt.type === 'status' && onStatus) onStatus(evt.message);
          else if (evt.type === 'token' && onToken) onToken(evt.token);
          else if (evt.type === 'result') finalResult = evt.data;
        } catch (_) { /* skip malformed */ }
      }
    }
    return finalResult || { error: 'No response from server.' };
  }

  async function playback(action, params = {}) {
    const target = params.system || 'sagetv';
    const deviceId = params.device_id || '';
    // Strip meta-fields from payload — they go as top-level request fields
    const { system: _s, device_id: _d, ...payload } = params;
    return request(`${baseUrl}/api/playback`, 'POST', {
      action,
      target,
      device_id: deviceId || undefined,
      payload,
    });
  }

  async function search(query) {
    return request(`${baseUrl}/api/search?q=${encodeURIComponent(query)}`);
  }

  async function getTranscript(recordingId) {
    return request(`${baseUrl}/api/transcript/${encodeURIComponent(recordingId)}`);
  }

  async function searchTranscripts(query) {
    return request(`${baseUrl}/api/transcript/search?q=${encodeURIComponent(query)}`);
  }

  async function playTitle(title, system) {
    // Search for the title, then start playback of the first result
    const results = await search(title);
    const target = system || 'sagetv';
    const items = results[target]?.results || results[target]?.items || [];
    if (items.length > 0) {
      const item = items[0];
      const id = item.id || item.recording_id || item.program_id;
      const deviceId = State.get().deviceId;
      return playback('play', { system: target, device_id: deviceId, id, title });
    }
    // Fallback: try playing by title directly
    const deviceId = State.get().deviceId;
    return playback('play', { system: target, device_id: deviceId, title });
  }

  async function system(action, params = {}) {
    return request(`${baseUrl}/api/system`, 'POST', { action, payload: params });
  }

  async function health() {
    return request(`${baseUrl}/api/health`);
  }

  async function services() {
    return request(`${baseUrl}/api/services`);
  }

  async function gpu() {
    return request(`${baseUrl}/api/gpu`);
  }

  async function alerts(opts = {}) {
    const params = new URLSearchParams();
    if (opts.limit) params.set('limit', opts.limit);
    if (opts.severity) params.set('severity', opts.severity);
    if (opts.since_ts) params.set('since_ts', opts.since_ts);
    const qs = params.toString();
    return request(`${baseUrl}/api/alerts${qs ? '?' + qs : ''}`);
  }

  async function clearAlerts() {
    return request(`${baseUrl}/api/alerts/clear`, 'POST');
  }

  // Session Manager endpoints
  async function listDevices() {
    return request(`${sessionUrl}/devices`);
  }

  async function bridgeDevices() {
    return request(`${baseUrl}/api/bridge/devices`);
  }

  async function bridgeStatus(deviceName) {
    return request(`${baseUrl}/api/bridge/status?device=${encodeURIComponent(deviceName)}`);
  }

  async function addDevice(data) {
    return request(`${sessionUrl}/devices`, 'POST', data);
  }

  async function updateDevice(deviceId, updates) {
    return request(`${sessionUrl}/devices/${deviceId}`, 'PUT', { updates });
  }

  async function discoverDevices() {
    return request(`${sessionUrl}/devices/discover`, 'POST');
  }

  async function deleteDevice(deviceId) {
    return request(`${sessionUrl}/devices/${deviceId}`, 'DELETE');
  }

  async function setDefaultDevice(deviceId) {
    return request(`${sessionUrl}/devices/${deviceId}/default`, 'POST');
  }

  async function resolveSession(deviceId) {
    const path = deviceId ? `/sessions/resolve/${deviceId}` : '/sessions/resolve';
    return request(`${sessionUrl}${path}`);
  }

  async function listSessions() {
    return request(`${sessionUrl}/sessions`);
  }

  async function whoami() {
    return request(`${sessionUrl}/whoami`);
  }

  /**
   * WebSocket-based streaming query. Opens a persistent connection for
   * real-time token delivery and tool call visibility.
   *
   * @param {string} prompt - User query text
   * @param {object} callbacks - {onToken, onStatus, onToolCall, onDone, onError}
   * @param {string[]} systems - DVR systems to query
   * @returns {object} - {close: Function} to abort the query
   */
  function queryWS(prompt, callbacks = {}, systems) {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${window.location.host}/ws/query`);
    let closed = false;

    ws.onopen = () => {
      ws.send(JSON.stringify({ prompt, systems: systems || undefined }));
    };

    ws.onmessage = (evt) => {
      try {
        const msg = JSON.parse(evt.data);
        switch (msg.type) {
          case 'token':
            if (callbacks.onToken) callbacks.onToken(msg.text);
            break;
          case 'status':
            if (callbacks.onStatus) callbacks.onStatus(msg.message);
            break;
          case 'tool_call':
            if (callbacks.onToolCall) callbacks.onToolCall(msg);
            break;
          case 'done':
            if (callbacks.onDone) callbacks.onDone(msg);
            ws.close();
            break;
          case 'error':
            if (callbacks.onError) callbacks.onError(msg.message);
            ws.close();
            break;
        }
      } catch (_) { /* skip malformed */ }
    };

    ws.onerror = () => {
      if (!closed && callbacks.onError) callbacks.onError('WebSocket connection failed');
    };

    ws.onclose = () => { closed = true; };

    return {
      close: () => { closed = true; ws.close(); },
    };
  }

  /**
   * Connect to the server event stream for push-based state updates.
   * Replaces polling when WebSocket is available.
   *
   * @param {Function} onEvent - Called with {type, data, ts} for each event
   * @returns {object} - {close: Function}
   */
  function connectEvents(onEvent) {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${window.location.host}/ws/events`);
    let reconnectTimer = null;

    ws.onmessage = (evt) => {
      try {
        const msg = JSON.parse(evt.data);
        if (onEvent) onEvent(msg);
      } catch (_) {}
    };

    ws.onclose = () => {
      // Auto-reconnect after 5 seconds
      reconnectTimer = setTimeout(() => {
        if (onEvent) connectEvents(onEvent);
      }, 5000);
    };

    return {
      close: () => {
        clearTimeout(reconnectTimer);
        ws.close();
      },
    };
  }

  return {
    setBaseUrl, setSessionUrl,
    query, queryStream, queryWS, connectEvents, playback, search, getTranscript, searchTranscripts, playTitle, system, health, services, gpu, alerts, clearAlerts,
    listDevices, bridgeDevices, bridgeStatus, addDevice, updateDevice, discoverDevices, deleteDevice, setDefaultDevice,
    resolveSession, listSessions, whoami,
    authCheck, authLogout, adminLogin, adminCheck, adminLogout,
    get baseUrl() { return baseUrl; },
    get sessionUrl() { return sessionUrl; },
  };
})();
