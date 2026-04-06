/**
 * state.js — Application state management.
 */
const State = (() => {
  const state = {
    llmFocus: JSON.parse(localStorage.getItem('llm_focus') || '["sagetv","channelsdvr"]'),
    system: localStorage.getItem('system') || 'sagetv',
    deviceId: localStorage.getItem('device_id') || '',
    sessionId: '',  // Resolved session_id from session manager
    devices: [],
    session: null,  // PlaybackContext from session manager
    connected: false,
    polling: null,
  };

  const listeners = [];

  function get() { return { ...state }; }

  function set(updates) {
    Object.assign(state, updates);
    if (updates.llmFocus !== undefined) localStorage.setItem('llm_focus', JSON.stringify(updates.llmFocus));
    if (updates.system !== undefined) localStorage.setItem('system', updates.system);
    if (updates.deviceId !== undefined) localStorage.setItem('device_id', updates.deviceId);
    listeners.forEach(fn => fn(state));
  }

  function onChange(fn) { listeners.push(fn); }

  async function refreshDevices() {
    try {
      const data = await API.listDevices();
      if (data.success) {
        set({ devices: data.devices });
      }
    } catch (e) {
      console.warn('Failed to load devices:', e);
    }
  }

  async function refreshSession() {
    if (!state.deviceId) {
      set({ session: null });
      return;
    }
    try {
      const data = await API.resolveSession(state.deviceId);
      const sessionId = data?.session?.session_id || '';
      set({ session: data, sessionId, connected: true });
    } catch (e) {
      console.warn('Session resolve failed:', e);
      set({ connected: false });
    }
  }

  async function checkHealth() {
    try {
      const data = await API.health();
      set({ connected: data.status === 'ok' });
    } catch {
      set({ connected: false });
    }
  }

  function startPolling(intervalMs = 3000) {
    stopPolling();
    refreshSession();
    state.polling = setInterval(refreshSession, intervalMs);
  }

  function stopPolling() {
    if (state.polling) {
      clearInterval(state.polling);
      state.polling = null;
    }
  }

  return { get, set, onChange, refreshDevices, refreshSession, checkHealth, startPolling, stopPolling };
})();
