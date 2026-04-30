/* ============================================================
   ArcUI — Agent Controls (SPEC-022 §5.12, §D-010)

   Renders Pause / Restart buttons that hit the existing
   POST /api/agents/{id}/control endpoint (from SPEC-016). A Deploy
   button is rendered but disabled with tooltip "Coming soon" — the
   spec scopes deploy out (SPEC-022 §Out of Scope).

   API:  ARC.AgentControls.mount(rootEl, agentId) -> {dispose}
   ============================================================ */

(function () {
  'use strict';

  function authHeaders() {
    var token = window.localStorage.getItem('arcui_viewer_token') || '';
    return token ? { Authorization: 'Bearer ' + token } : {};
  }

  function postControl(agentId, action) {
    return fetch('/api/agents/' + encodeURIComponent(agentId) + '/control', {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
      body: JSON.stringify({ action: action }),
    }).then(function (r) { return r.json().catch(function () { return {}; }); });
  }

  function mount(rootEl, agentId) {
    rootEl.innerHTML =
      '<div class="agent-controls">' +
        '<button type="button" class="btn ac-pause" data-action="pause">Pause</button>' +
        '<button type="button" class="btn ac-restart" data-action="restart">Restart</button>' +
        '<button type="button" class="btn ac-deploy" disabled ' +
          'title="Coming soon">Deploy</button>' +
        '<span class="ac-status" aria-live="polite"></span>' +
      '</div>';

    var statusEl = rootEl.querySelector('.ac-status');

    function setStatus(text, kind) {
      if (!statusEl) return;
      statusEl.textContent = text || '';
      statusEl.className = 'ac-status' + (kind ? ' ' + kind : '');
    }

    function onClick(ev) {
      var btn = ev.target.closest('.btn[data-action]');
      if (!btn) return;
      var action = btn.dataset.action;
      btn.disabled = true;
      setStatus(action + '…', 'pending');
      postControl(agentId, action).then(function (resp) {
        if (resp && resp.ok !== false && !resp.error) {
          setStatus(action + ' sent', 'ok');
        } else {
          setStatus((resp && resp.error) || (action + ' failed'), 'bad');
        }
      }).catch(function () {
        setStatus(action + ' failed', 'bad');
      }).then(function () {
        btn.disabled = false;
        setTimeout(function () { setStatus(''); }, 3000);
      });
    }

    rootEl.addEventListener('click', onClick);

    return {
      dispose: function () { rootEl.removeEventListener('click', onClick); },
    };
  }

  window.ARC = window.ARC || {};
  window.ARC.AgentControls = { mount: mount };
})();
