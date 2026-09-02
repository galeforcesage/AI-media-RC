/**
 * state.js — Application state management.
 */
const State = (() => {
  // Safely parse llm_focus — may be a JSON array or a legacy plain string
  let _llmFocus;
  try {
    const raw = localStorage.getItem('llm_focus');
    const parsed = raw ? JSON.parse(raw) : null;
    _llmFocus = Array.isArray(parsed) ? parsed : [parsed || 'sagetv'];
  } catch (_) {
    const raw = localStorage.getItem('llm_focus');
    _llmFocus = raw ? [raw] : ['sagetv', 'channelsdvr'];
  }

  const state = {
    llmFocus: _llmFocus,
    system: localStorage.getItem('system') || 'sagetv',
    deviceId: localStorage.getItem('device_id') || '',
    sessionId: '',  // Resolved session_id from session manager
    devices: [],
    bridgeDevices: [],  // Connected Channels Bridge devices
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
    // Auto-discover SageTV contexts before listing
    try { await API.discoverDevices(); } catch (_) { /* non-fatal */ }
    try {
      const data = await API.listDevices();
      if (data.success) {
        set({ devices: data.devices });
      }
    } catch (e) {
      console.warn('Failed to load devices:', e);
    }
    // Also refresh bridge devices
    await refreshBridgeDevices();
  }

  async function refreshBridgeDevices() {
    try {
      const data = await API.bridgeDevices();
      set({ bridgeDevices: data.devices || [] });
    } catch (e) {
      console.warn('Failed to load bridge devices:', e);
    }
  }

  async function refreshSession() {
    if (!state.deviceId) {
      set({ session: null });
      return;
    }
    try {
      // Bridge devices: get playback status directly from the device via MCP
      if (state.deviceId.startsWith('bridge:')) {
        const deviceName = state.deviceId.slice('bridge:'.length);
        const data = await API.bridgeStatus(deviceName);
        if (data && data.success && data.data) {
          const s = data.data;
          const np = s.now_playing || {};
          // Channels DVR status: { status, playback_time, now_playing: { title, episode_title, ... } }
          const isStopped = !s.status || s.status === 'stopped';
          if (isStopped) {
            set({ session: null, connected: true });
          } else {
            set({
              session: {
                title: np.title || '',
                episode: np.episode_title || '',
                channel: np.channel_number || '',
                state: s.status === 'paused' ? 'paused' : 'playing',
                position: s.playback_time || 0,
                duration: s.duration || 0,
              },
              connected: true,
            });
          }
        } else {
          set({ session: null, connected: true });
        }
        return;
      }
      const data = await API.resolveSession(state.deviceId);
      const sessionId = data?.session?.session_id || '';
      // Extract the inner session object for UI (title, state, position, etc.)
      const sess = data?.session;
      set({ session: sess || null, sessionId, connected: true });
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

  let _eventStream = null;

  function startPolling(intervalMs = 7000) {
    stopPolling();
    refreshSession();
    refreshBridgeDevices();

    // Try WebSocket events stream first (push-based, no polling needed)
    try {
      _eventStream = API.connectEvents((evt) => {
        if (evt.type === 'playback_update' && evt.data) {
          set({ session: evt.data, connected: true });
        } else if (evt.type === 'health' && evt.data) {
          set({ connected: evt.data.status === 'ok' });
        }
      });
    } catch (_) {
      _eventStream = null;
    }

    // Fall back to polling if WS not available or as a supplement
    state.polling = setInterval(refreshSession, intervalMs);
    state._bridgePoll = setInterval(refreshBridgeDevices, 30000);
  }

  function stopPolling() {
    if (state.polling) {
      clearInterval(state.polling);
      state.polling = null;
    }
    if (state._bridgePoll) {
      clearInterval(state._bridgePoll);
      state._bridgePoll = null;
    }
    if (_eventStream) {
      _eventStream.close();
      _eventStream = null;
    }
  }

  return { get, set, onChange, refreshDevices, refreshBridgeDevices, refreshSession, checkHealth, startPolling, stopPolling };
})();
