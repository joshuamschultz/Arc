/* ============================================================
   ArcUI — Formatters (Intl.NumberFormat, escapeHTML, etc.)
   ============================================================ */

const Fmt = {
  _tokenFmt: new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 }),
  _currencyFmt: new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 4 }),
  _costFmt: new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 }),
  _numberFmt: new Intl.NumberFormat('en-US'),

  tokens(n) {
    if (n == null) return '0';
    return this._tokenFmt.format(n);
  },

  cost(n) {
    if (n == null) return '$0.00';
    if (n < 0.01) return this._currencyFmt.format(n);
    return this._costFmt.format(n);
  },

  latency(ms) {
    if (ms == null || ms === 0) return '0ms';
    if (ms < 1000) return Math.round(ms) + 'ms';
    return (ms / 1000).toFixed(2) + 's';
  },

  number(n) {
    if (n == null) return '0';
    return this._numberFmt.format(n);
  },

  percent(n) {
    if (n == null) return '0%';
    return n.toFixed(1) + '%';
  },

  escapeHTML(str) {
    const el = document.createElement('span');
    el.textContent = str;
    return el.innerHTML;
  },

  traceId(id) {
    if (!id) return '';
    return id.substring(0, 12);
  },

  relativeTime(ts) {
    if (!ts) return '';
    const now = Date.now();
    const diff = now - new Date(ts).getTime();
    if (diff < 60000) return Math.round(diff / 1000) + 's ago';
    if (diff < 3600000) return Math.round(diff / 60000) + 'm ago';
    if (diff < 86400000) return Math.round(diff / 3600000) + 'h ago';
    return Math.round(diff / 86400000) + 'd ago';
  }
};

window.Fmt = Fmt;
