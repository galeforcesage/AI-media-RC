/**
 * api.js — HTTP client for the orchestrator and session manager.
 */
const API = (() => {
  const origin = window.location.origin;  // e.g. https://10.0.0.10
  let baseUrl = localStorage.getItem('api_url') || origin;
  let sessionUrl = localStorage.getItem('session_url') || `${origin}/session`;

  function setBaseUrl(url) { baseUrl = url; localStorage.setItem('api_url', url); }
  function setSessionUrl(url) { sessionUrl = url; localStorage.setItem('session_url', url); }

  async function request(url, method = 'GET', body = null) {
    const opts = { method, headers: { 'Content-Type': 'application/json' }, credentials: 'include' };
    if (body) opts.body = JSON.stringify(body);
    const resp = await fetch(url, opts);
    if (resp.status === 401) {
      // Not authenticated — redirect to login
      window.location.href = '/login.html';
      throw new Error('unauthorized');
    }
    return resp.json();
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
    return request(`${sessionUrl}/auth/admin/login`, 'POST', { username, password });
  }

  async function adminCheck() {
    const resp = await fetch(`${sessionUrl}/auth/admin/check`, { credentials: 'include' });
    return resp.json();
  }

  async function adminLogout() {
    return request(`${sessionUrl}/auth/admin/logout`, 'POST');
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

  async function whoami() {
    return request(`${sessionUrl}/whoami`);
  }

  return {
    setBaseUrl, setSessionUrl,
    query, playback, search, playTitle, system, health,
    listDevices, addDevice, deleteDevice, setDefaultDevice,
    resolveSession, listSessions, whoami,
    authCheck, authLogout, adminLogin, adminCheck, adminLogout,
    get baseUrl() { return baseUrl; },
    get sessionUrl() { return sessionUrl; },
  };
})();
