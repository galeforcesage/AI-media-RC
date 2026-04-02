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
    return request(`${baseUrl}/query`, 'POST', { text });
  }

  async function playback(action, params = {}) {
    return request(`${baseUrl}/playback`, 'POST', { action, ...params });
  }

  async function search(query) {
    return request(`${baseUrl}/search`, 'POST', { query });
  }

  async function system(action, params = {}) {
    return request(`${baseUrl}/system`, 'POST', { action, ...params });
  }

  async function health() {
    return request(`${baseUrl}/health`);
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
    query, playback, search, system, health,
    listDevices, addDevice, deleteDevice, setDefaultDevice,
    resolveSession, listSessions,
    get baseUrl() { return baseUrl; },
    get sessionUrl() { return sessionUrl; },
  };
})();
