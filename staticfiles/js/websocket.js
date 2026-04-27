/**
 * websocket.js — AnalyticsMeta real-time WebSocket client
 *
 * Features
 * --------
 * - Explicit connection state machine: IDLE → CONNECTING → CONNECTED →
 *   RECONNECTING → FAILED (prevents concurrent connect() calls)
 * - Exponential back-off with full jitter (prevents thundering herd)
 * - Offline message queue: mutating sends are queued while disconnected and
 *   flushed automatically on reconnect
 * - Per-action debouncing: refresh_widget is debounced per widget-id so rapid
 *   data-change events don't flood the server
 * - Sequence numbers on every outbound message for server-side ordering checks
 * - Server-assigned session_id stored for presence tracking
 * - Clean disconnect: clears timers, drains queue, removes event listeners
 */

'use strict';

const WS_STATES = Object.freeze({
  IDLE:         'idle',
  CONNECTING:   'connecting',
  CONNECTED:    'connected',
  RECONNECTING: 'reconnecting',
  FAILED:       'failed',
});

class RealtimeDashboard {
  constructor(dashboardId, options = {}) {
    this.dashboardId = dashboardId;

    // Config
    this.maxReconnectAttempts = options.maxReconnectAttempts ?? 12;
    this.baseDelay            = options.baseDelay            ?? 1000;   // ms
    this.maxDelay             = options.maxDelay             ?? 30_000; // ms
    this.pingIntervalMs       = options.pingIntervalMs       ?? 25_000; // ms
    this.debug                = options.debug                ?? false;

    // Internal state
    this._ws                  = null;
    this._state               = WS_STATES.IDLE;
    this._reconnectAttempts   = 0;
    this._seq                 = 0;               // outbound message sequence counter
    this._sessionId           = null;            // assigned by server on connect
    this._pingTimer           = null;
    this._reconnectTimer      = null;
    this._messageHandlers     = new Map();
    this._offlineQueue        = [];              // { data, enqueueTime }
    this._offlineQueueLimit   = 50;
    this._debounceTimers      = new Map();       // action+key → timer id

    this._boundBeforeUnload   = () => this.disconnect(true);
    window.addEventListener('beforeunload', this._boundBeforeUnload);

    this._setupDefaultHandlers();
    this.connect();
  }

  // ─── State machine ──────────────────────────────────────────────────────

  get state()       { return this._state; }
  isConnected()     { return this._state === WS_STATES.CONNECTED; }

  _setState(next) {
    if (this._state === next) return;
    this._log(`State: ${this._state} → ${next}`);
    this._state = next;
    this._updateStatusIndicator();
    this._trigger(`state_change`, { state: next });
  }

  // ─── Connection lifecycle ───────────────────────────────────────────────

  connect() {
    if (this._state === WS_STATES.CONNECTING ||
        this._state === WS_STATES.CONNECTED) return;

    this._setState(WS_STATES.CONNECTING);
    const url = this._buildUrl();
    this._log(`Connecting → ${url}`);

    try {
      this._ws = new WebSocket(url);
    } catch (err) {
      this._log('WebSocket constructor threw:', err);
      this._scheduleReconnect();
      return;
    }

    this._ws.onopen    = ()      => this._onOpen();
    this._ws.onmessage = e       => this._onMessage(e);
    this._ws.onclose   = e       => this._onClose(e);
    this._ws.onerror   = e       => this._onError(e);
  }

  _onOpen() {
    this._log('Connected');
    this._reconnectAttempts = 0;
    this._setState(WS_STATES.CONNECTED);
    this._startPing();
    this._flushOfflineQueue();
    this._trigger('connected');
  }

  _onMessage(event) {
    let data;
    try {
      data = JSON.parse(event.data);
    } catch {
      this._log('Unparseable message:', event.data);
      return;
    }

    this._log('↓', data.type, data);

    // Capture server-assigned session id
    if (data.session_id) this._sessionId = data.session_id;

    const handlers = this._messageHandlers.get(data.type) || [];
    for (const h of handlers) {
      try { h(data); } catch (err) { this._log('Handler error:', err); }
    }
  }

  _onClose(event) {
    this._log(`Closed — code=${event.code} reason=${event.reason} wasClean=${event.wasClean}`);
    this._stopPing();

    // 4001 = unauthenticated, 4003 = forbidden — do not reconnect
    if (event.code === 4001 || event.code === 4003) {
      this._setState(WS_STATES.FAILED);
      this._trigger('auth_failed', { code: event.code });
      return;
    }

    this._setState(WS_STATES.RECONNECTING);
    this._trigger('disconnected', { code: event.code });
    this._scheduleReconnect();
  }

  _onError(event) {
    this._log('WebSocket error:', event);
    this._trigger('error', { event });
    // onclose fires immediately after onerror — reconnect logic lives there
  }

  _scheduleReconnect() {
    if (this._reconnectAttempts >= this.maxReconnectAttempts) {
      this._setState(WS_STATES.FAILED);
      this._trigger('reconnect_failed');
      this._showNotification('Live updates unavailable — please refresh.', 'error', 0);
      return;
    }

    this._reconnectAttempts++;
    // Full-jitter exponential back-off
    const cap   = Math.min(this.maxDelay, this.baseDelay * 2 ** this._reconnectAttempts);
    const delay = Math.random() * cap;

    this._log(`Reconnect attempt ${this._reconnectAttempts} in ${Math.round(delay)}ms`);
    this._trigger('reconnecting', { attempt: this._reconnectAttempts, delay });

    this._reconnectTimer = setTimeout(() => this.connect(), delay);
  }

  disconnect(silent = false) {
    clearTimeout(this._reconnectTimer);
    this._stopPing();
    window.removeEventListener('beforeunload', this._boundBeforeUnload);

    if (this._ws) {
      this._ws.onclose = null; // suppress auto-reconnect
      this._ws.close(1000, 'client_disconnect');
      this._ws = null;
    }

    if (!silent) this._setState(WS_STATES.IDLE);
  }

  // ─── Ping / keepalive ───────────────────────────────────────────────────

  _startPing() {
    this._stopPing();
    this._pingTimer = setInterval(() => {
      if (this.isConnected()) {
        this._sendRaw({ type: 'ping', timestamp: Date.now() });
      }
    }, this.pingIntervalMs);
  }

  _stopPing() {
    if (this._pingTimer) { clearInterval(this._pingTimer); this._pingTimer = null; }
  }

  // ─── Sending ────────────────────────────────────────────────────────────

  /**
   * Send immediately if connected; otherwise enqueue.
   * @param {Object} data
   * @param {boolean} [queue=true]  whether to queue when offline
   */
  send(data, queue = true) {
    const msg = { ...data, seq: ++this._seq };
    if (this.isConnected()) {
      this._sendRaw(msg);
    } else if (queue) {
      this._enqueue(msg);
    }
  }

  _sendRaw(msg) {
    try {
      this._ws.send(JSON.stringify(msg));
      this._log('↑', msg.type, msg);
    } catch (err) {
      this._log('Send failed:', err);
    }
  }

  _enqueue(msg) {
    if (this._offlineQueue.length >= this._offlineQueueLimit) {
      this._offlineQueue.shift(); // drop oldest
    }
    this._offlineQueue.push({ data: msg, enqueueTime: Date.now() });
    this._log(`Queued (${this._offlineQueue.length} pending)`, msg.type);
  }

  _flushOfflineQueue() {
    const staleCutoff = Date.now() - 60_000; // discard messages older than 60s
    const toSend = this._offlineQueue.filter(e => e.enqueueTime > staleCutoff);
    this._offlineQueue = [];
    if (toSend.length) {
      this._log(`Flushing ${toSend.length} queued messages`);
      toSend.forEach(e => this._sendRaw(e.data));
    }
  }

  // ─── Debounced send helpers ─────────────────────────────────────────────

  /**
   * Debounce a send by a composite key (e.g. 'refresh_widget:uuid').
   */
  _debounceSend(key, data, wait = 400) {
    if (this._debounceTimers.has(key)) {
      clearTimeout(this._debounceTimers.get(key));
    }
    this._debounceTimers.set(key, setTimeout(() => {
      this._debounceTimers.delete(key);
      this.send(data);
    }, wait));
  }

  // ─── Public action API ──────────────────────────────────────────────────

  refreshWidget(widgetId) {
    // Debounced per widget — rapid data updates won't spam the server
    this._debounceSend(`refresh_widget:${widgetId}`, {
      type:      'refresh_widget',
      widget_id: widgetId,
    }, 300);
  }

  updateLayout(layout, widgets) {
    // Layout saves are queued so they survive a brief disconnect
    this.send({ type: 'update_layout', layout, widgets });
  }

  addWidget(widgetData) {
    this.send({ type: 'add_widget', widget: widgetData });
  }

  deleteWidget(widgetId) {
    this.send({ type: 'delete_widget', widget_id: widgetId });
  }

  requestInsights() {
    this.send({ type: 'request_insights' }, false); // don't queue — user must retry
  }

  // ─── Message handler registry ───────────────────────────────────────────

  on(type, handler) {
    if (!this._messageHandlers.has(type)) this._messageHandlers.set(type, []);
    this._messageHandlers.get(type).push(handler);
    return this; // fluent
  }

  off(type, handler) {
    const list = this._messageHandlers.get(type);
    if (!list) return;
    const idx = list.indexOf(handler);
    if (idx !== -1) list.splice(idx, 1);
  }

  // ─── Default message handlers ───────────────────────────────────────────

  _setupDefaultHandlers() {
    this.on('pong', () => { /* keepalive ack — no UI action */ });

    this.on('dashboard_state', data => {
      this._log('Received full dashboard state');
      this._trigger('dashboard_state', data);
    });

    this.on('widget_update', data => {
      this._log('Widget updated:', data.widget_id);
      this._dispatchDOM('widget-update', { widgetId: data.widget_id, data: data.data });
    });

    this.on('layout_updated', data => {
      this._log('Layout updated by', data.updated_by);
      // Only apply remote layout changes (not our own echo)
      if (data.updated_by && data.updated_by !== window._currentUserEmail) {
        this._dispatchDOM('layout-update', { layout: data.layout, widgets: data.widgets });
        this._showNotification(`Layout updated by ${data.updated_by}`, 'info');
      }
    });

    this.on('layout_conflict', data => {
      this._log('Layout conflict detected:', data);
      this._showNotification(
        `Layout conflict: ${data.message || 'Another user saved simultaneously. Reload to get latest.'}`,
        'warning', 8000
      );
    });

    this.on('widget_added', data => {
      this._dispatchDOM('widget-add', { widget: data.widget });
    });

    this.on('widget_deleted', data => {
      this._dispatchDOM('widget-remove', { widgetId: data.widget_id });
    });

    this.on('user_joined', data => {
      this._showNotification(`${data.user} joined`, 'info', 3000);
    });

    this.on('user_left', data => {
      this._showNotification(`${data.user} left`, 'info', 2500);
    });

    this.on('insights_generation_started', data => {
      this._showNotification('Generating AI insights…', 'info', 4000);
    });

    this.on('insights_generation_progress', data => {
      this._trigger('insights_progress', data);
    });

    this.on('insights_complete', data => {
      this._showNotification('AI insights ready!', 'success', 5000);
      this._trigger('insights_complete', data);
    });

    this.on('error', data => {
      this._log('Server error:', data.message);
      this._showNotification(`Error: ${data.message}`, 'error', 6000);
    });

    this.on('rate_limited', data => {
      this._showNotification(`Rate limited — slow down (${data.retry_after}s)`, 'warning', 5000);
    });
  }

  // ─── UI helpers ─────────────────────────────────────────────────────────

  _updateStatusIndicator() {
    const el = document.getElementById('realtimeIndicator');
    if (!el) return;

    const map = {
      [WS_STATES.CONNECTED]:    { cls: 'bg-green-500',  label: 'Live'         },
      [WS_STATES.CONNECTING]:   { cls: 'bg-yellow-400', label: 'Connecting…'  },
      [WS_STATES.RECONNECTING]: { cls: 'bg-yellow-400', label: 'Reconnecting…'},
      [WS_STATES.FAILED]:       { cls: 'bg-red-500',    label: 'Offline'      },
      [WS_STATES.IDLE]:         { cls: 'bg-gray-400',   label: 'Disconnected' },
    };
    const { cls, label } = map[this._state] || map[WS_STATES.IDLE];
    el.className = `realtime-indicator ${cls}`;
    el.innerHTML = `<i class="fas fa-circle text-white text-[8px] mr-1"></i>${label}`;
  }

  /**
   * Show a toast notification.
   * @param {string}  message
   * @param {'info'|'success'|'warning'|'error'} type
   * @param {number}  duration  ms; 0 = persistent until manually closed
   */
  _showNotification(message, type = 'info', duration = 3500) {
    const colorMap = {
      info:    'bg-[#03466e]',
      success: 'bg-green-600',
      warning: 'bg-yellow-500',
      error:   'bg-red-600',
    };
    const iconMap = {
      info:    'fa-info-circle',
      success: 'fa-check-circle',
      warning: 'fa-exclamation-triangle',
      error:   'fa-times-circle',
    };

    const toast = document.createElement('div');
    toast.className = [
      'fixed bottom-5 right-5 z-[9999] flex items-center gap-2',
      'px-4 py-2.5 rounded-lg shadow-xl text-white text-sm max-w-sm',
      'animate-slide-up transition-opacity',
      colorMap[type] || colorMap.info,
    ].join(' ');

    toast.innerHTML = `
      <i class="fas ${iconMap[type] || iconMap.info} flex-shrink-0"></i>
      <span class="flex-1">${this._escHtml(message)}</span>
      <button class="ml-1 opacity-70 hover:opacity-100 flex-shrink-0" aria-label="dismiss">
        <i class="fas fa-times text-xs"></i>
      </button>`;

    toast.querySelector('button').addEventListener('click', () => toast.remove());
    document.body.appendChild(toast);

    if (duration > 0) {
      setTimeout(() => {
        toast.classList.add('opacity-0');
        setTimeout(() => toast.remove(), 300);
      }, duration);
    }
  }

  // ─── Internal event bus (DOM) ───────────────────────────────────────────

  _dispatchDOM(name, detail) {
    document.dispatchEvent(new CustomEvent(name, { detail }));
  }

  _trigger(name, detail = {}) {
    document.dispatchEvent(new CustomEvent(`dashboard:${name}`, { detail }));
  }

  // ─── Misc ───────────────────────────────────────────────────────────────

  _buildUrl() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${location.host}/ws/dashboard/${this.dashboardId}/`;
  }

  _escHtml(str) {
    return String(str)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  _log(...args) {
    if (this.debug) console.log('[WS]', ...args);
  }
}

window.RealtimeDashboard = RealtimeDashboard;
window.WS_STATES = WS_STATES;
