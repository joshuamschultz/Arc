/* ============================================================
   ArcUI — Connection Status Banner UI
   ============================================================ */

class ConnectionUI {
  constructor(bannerEl) {
    this._banner = bannerEl;
    this._staleTimer = null;
    this._staleThreshold = 30000; // 30s without data = stale
  }

  update(state) {
    if (!this._banner) return;
    const labels = {
      [WS_STATES.CONNECTING]: 'Connecting...',
      [WS_STATES.CONNECTED]: 'Connected',
      [WS_STATES.RECONNECTING]: 'Reconnecting...',
      [WS_STATES.DISCONNECTED]: 'Disconnected',
    };
    const classes = {
      [WS_STATES.CONNECTING]: 'connecting',
      [WS_STATES.CONNECTED]: 'connected',
      [WS_STATES.RECONNECTING]: 'connecting',
      [WS_STATES.DISCONNECTED]: 'disconnected',
    };

    this._banner.className = 'connection-banner ' + (classes[state] || 'disconnected');
    this._banner.innerHTML =
      '<span class="connection-dot"></span>' +
      '<span>' + (labels[state] || 'Unknown') + '</span>';

    if (state === WS_STATES.CONNECTED) {
      this._startStaleCheck();
    } else {
      this._stopStaleCheck();
    }
  }

  markFresh() {
    this._lastData = Date.now();
  }

  _startStaleCheck() {
    this._lastData = Date.now();
    this._stopStaleCheck();
    this._staleTimer = setInterval(() => {
      const elapsed = Date.now() - this._lastData;
      if (elapsed > this._staleThreshold) {
        const staleEls = document.querySelectorAll('.stale-overlay');
        staleEls.forEach(el => el.classList.add('stale'));
      } else {
        const staleEls = document.querySelectorAll('.stale-overlay');
        staleEls.forEach(el => el.classList.remove('stale'));
      }
    }, 5000);
  }

  _stopStaleCheck() {
    if (this._staleTimer) {
      clearInterval(this._staleTimer);
      this._staleTimer = null;
    }
  }
}

window.ConnectionUI = ConnectionUI;
