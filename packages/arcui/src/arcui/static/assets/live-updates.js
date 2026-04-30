/* ============================================================
   ArcUI — Live Updates Binding (SPEC-022 §5.4, §7)

   Wires the existing RobustWebSocket to the SPEC-022 subscribe protocol:

     send → {type: 'subscribe:agent',   agent_id: '<id>'}
     send → {type: 'unsubscribe:agent', agent_id: '<id>'}
     recv → {type: 'file_change', agent_id, event_type, path, payload}

   On receipt the module dispatches a window-level `arc:event` CustomEvent
   carrying the message detail. Pages (agent-detail, agents, policy, etc.)
   listen for `arc:event` and re-render.

   Reconnect handling: when the WS transitions from RECONNECTING → CONNECTED
   we re-fire `subscribe:agent` for every tracked agent. Tab visibility
   change does NOT unsubscribe — operators commonly switch tabs while
   watching a long-running agent.

   API:
     ARC.LiveUpdates.attach(ws)        — wire to a RobustWebSocket instance
     ARC.LiveUpdates.subscribe(id)     — track + send subscribe
     ARC.LiveUpdates.unsubscribe(id)   — drop tracking + send unsubscribe
     ARC.LiveUpdates.setActive(id)     — convenience for SPA route changes:
                                         drops the previous, subscribes the
                                         new (no-op if same)
     ARC.LiveUpdates.subscribeRoster(ids) — fleet page subscribes to all
                                            agents at once
   ============================================================ */

(function () {
  'use strict';

  var _ws = null;
  var _subs = new Set();
  var _activeAgent = null;
  var _roster = new Set();
  var _attached = false;

  function send(msg) {
    if (!_ws) return;
    try {
      _ws.send(msg);
    } catch (e) { /* outbox swallows */ }
  }

  function subscribe(agentId) {
    if (!agentId || _subs.has(agentId)) return;
    _subs.add(agentId);
    send({ type: 'subscribe:agent', agent_id: agentId });
  }

  function unsubscribe(agentId) {
    if (!agentId || !_subs.has(agentId)) return;
    _subs.delete(agentId);
    send({ type: 'unsubscribe:agent', agent_id: agentId });
  }

  function setActive(agentId) {
    if (_activeAgent === agentId) return;
    if (_activeAgent && !_roster.has(_activeAgent)) {
      unsubscribe(_activeAgent);
    }
    _activeAgent = agentId || null;
    if (agentId) subscribe(agentId);
  }

  function subscribeRoster(agentIds) {
    var next = new Set(agentIds || []);
    // Unsubscribe leavers (unless they're the active agent)
    _roster.forEach(function (id) {
      if (!next.has(id) && id !== _activeAgent) unsubscribe(id);
    });
    // Subscribe joiners
    next.forEach(function (id) { subscribe(id); });
    _roster = next;
  }

  function _onMessage(evt) {
    var data = evt && evt.detail;
    if (!data) return;
    if (data.type !== 'file_change') return;
    // Re-emit as a window-level arc:event so any page can listen
    window.dispatchEvent(new CustomEvent('arc:event', { detail: data }));
  }

  function _onStateChange(evt) {
    var state = evt && evt.detail;
    // 1 == CONNECTED in WS_STATES — but we don't import the constant.
    // Re-subscribe whenever we enter CONNECTED with non-empty subs.
    if (state === (window.WS_STATES && window.WS_STATES.CONNECTED)) {
      var ids = Array.from(_subs);
      _subs = new Set();
      ids.forEach(subscribe);
    }
  }

  function attach(ws) {
    if (_attached) return;
    _ws = ws;
    _attached = true;
    if (ws && typeof ws.addEventListener === 'function') {
      ws.addEventListener('message', _onMessage);
      ws.addEventListener('statechange', _onStateChange);
    }
  }

  function _state() {
    // Test/debug helper — exposes internals without leaking mutability.
    return {
      activeAgent: _activeAgent,
      subscriptions: Array.from(_subs),
      roster: Array.from(_roster),
      attached: _attached,
    };
  }

  window.ARC = window.ARC || {};
  window.ARC.LiveUpdates = {
    attach: attach,
    subscribe: subscribe,
    unsubscribe: unsubscribe,
    setActive: setActive,
    subscribeRoster: subscribeRoster,
    _state: _state,
  };
})();
