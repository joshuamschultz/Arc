/* ============================================================
   ArcUI — RobustWebSocket Client
   Exponential backoff + jitter, heartbeat, first-message auth.
   ============================================================ */

const WS_STATES = { CONNECTING: 0, CONNECTED: 1, RECONNECTING: 2, DISCONNECTED: 3 };

class RobustWebSocket extends EventTarget {
  constructor(url, token, opts = {}) {
    super();
    this._url = url;
    this._token = token;
    this._maxRetries = opts.maxRetries ?? 10;
    this._baseDelay = opts.baseDelay ?? 1000;
    this._maxDelay = opts.maxDelay ?? 30000;
    this._heartbeatInterval = opts.heartbeatInterval ?? 30000;
    this._retries = 0;
    this._ws = null;
    this._heartbeatTimer = null;
    this._state = WS_STATES.DISCONNECTED;
    this._outbox = [];
  }

  get state() { return this._state; }

  connect() {
    this._setState(WS_STATES.CONNECTING);
    try {
      this._ws = new WebSocket(this._url);
    } catch {
      this._scheduleReconnect();
      return;
    }

    this._ws.onopen = () => {
      // First-message auth
      this._ws.send(JSON.stringify({ token: this._token }));
    };

    this._ws.onmessage = (evt) => {
      const data = JSON.parse(evt.data);

      // Handle auth response
      if (data.type === 'auth_ok') {
        this._setState(WS_STATES.CONNECTED);
        this._retries = 0;
        this._startHeartbeat();
        this._flushOutbox();
        return;
      }

      // Handle auth error
      if (data.error) {
        this._setState(WS_STATES.DISCONNECTED);
        this._ws.close();
        return;
      }

      // Handle server ping
      if (data.type === 'ping') {
        this._ws.send(JSON.stringify({ type: 'pong' }));
        return;
      }

      // Dispatch event to listeners
      this.dispatchEvent(new CustomEvent('message', { detail: data }));
    };

    this._ws.onclose = () => {
      this._stopHeartbeat();
      if (this._state !== WS_STATES.DISCONNECTED) {
        this._scheduleReconnect();
      }
    };

    this._ws.onerror = () => {
      // onclose will fire after this
    };
  }

  send(data) {
    if (this._state === WS_STATES.CONNECTED && this._ws?.readyState === WebSocket.OPEN) {
      this._ws.send(typeof data === 'string' ? data : JSON.stringify(data));
    } else {
      this._outbox.push(data);
    }
  }

  disconnect() {
    this._setState(WS_STATES.DISCONNECTED);
    this._stopHeartbeat();
    if (this._ws) {
      this._ws.close();
      this._ws = null;
    }
  }

  _setState(s) {
    if (this._state === s) return;
    this._state = s;
    this.dispatchEvent(new CustomEvent('statechange', { detail: s }));
  }

  _scheduleReconnect() {
    if (this._retries >= this._maxRetries) {
      this._setState(WS_STATES.DISCONNECTED);
      return;
    }
    this._setState(WS_STATES.RECONNECTING);
    const delay = Math.min(
      this._baseDelay * Math.pow(2, this._retries) + Math.random() * 1000,
      this._maxDelay
    );
    this._retries++;
    setTimeout(() => this.connect(), delay);
  }

  _startHeartbeat() {
    this._stopHeartbeat();
    this._heartbeatTimer = setInterval(() => {
      if (this._ws?.readyState === WebSocket.OPEN) {
        this._ws.send(JSON.stringify({ type: 'pong' }));
      }
    }, this._heartbeatInterval);
  }

  _stopHeartbeat() {
    if (this._heartbeatTimer) {
      clearInterval(this._heartbeatTimer);
      this._heartbeatTimer = null;
    }
  }

  _flushOutbox() {
    while (this._outbox.length > 0) {
      const msg = this._outbox.shift();
      this.send(msg);
    }
  }
}

window.RobustWebSocket = RobustWebSocket;
window.WS_STATES = WS_STATES;
