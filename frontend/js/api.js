/**
 * api.js — HTTP client for the orchestrator and session manager.
 */
const API = (() => {
  let baseUrl = localStorage.getItem('api_url') || 'http://127.0.0.1:8000';
  let sessionUrl = localStorage.getItem('session_url') || 'http://127.0.0.1:8769';

  function setBaseUrl(url) { baseUrl = url; localStorage.setItem('api_url', url); }
  function setSessionUrl(url) { sessionUrl = url; localStorage.setItem('session_url', url); }

  async function request(url, method = 'GET', body = null) {
    const opts = { method, headers: { 'Content-Type': 'application/json' } };
    if (body) opts.body = JSON.stringify(body);
    const resp = await fetch(url, opts);
    return resp.json();
  }

  // Orchestrator endpoints
  async function query(text) {
    return request(`${baseUrl}/api/query`, 'POST', { prompt: text });
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

  // Session Manager endpoints
  async function listDevices() {
    return request(`${sessionUrl}/devices`);
  }

  async function addDevice(data) {
    return request(`${sessionUrl}/devices`, 'POST', data);
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

  return {
    setBaseUrl, setSessionUrl,
    query, playback, search, playTitle, system, health,
    listDevices, addDevice, deleteDevice, setDefaultDevice,
    resolveSession, listSessions,
    get baseUrl() { return baseUrl; },
    get sessionUrl() { return sessionUrl; },
  };
})();
