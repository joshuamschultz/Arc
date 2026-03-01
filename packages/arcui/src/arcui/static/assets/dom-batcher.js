/* ============================================================
   ArcUI — DOM Batcher (requestAnimationFrame batching)
   ============================================================ */

class DOMBatcher {
  constructor() {
    this._queue = [];
    this._rafId = null;
  }

  schedule(fn) {
    this._queue.push(fn);
    if (!this._rafId) {
      this._rafId = requestAnimationFrame(() => this._flush());
    }
  }

  _flush() {
    const batch = this._queue.splice(0);
    for (const fn of batch) {
      try { fn(); } catch (e) { console.error('[DOMBatcher]', e); }
    }
    this._rafId = null;
    if (this._queue.length > 0) {
      this._rafId = requestAnimationFrame(() => this._flush());
    }
  }
}

window.domBatcher = new DOMBatcher();
