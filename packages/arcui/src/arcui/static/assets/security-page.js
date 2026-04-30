/* ============================================================
   ArcUI — Security & Audit Fleet Page (SPEC-022 §6.3)

   Sections:
     - Live audit tail (AuditViewer over /api/team/audit)
     - Control actions log (filtered to action=agent.control)
     - Failed-policy events (filtered to outcome=deny)
     - Connection security panel (mTLS status, current viewer DID)

   API:  ARC.SecurityPage.mount(panelEl) -> {refresh, dispose}
   Data: GET /api/team/audit
   ============================================================ */

(function () {
  'use strict';

  function escapeText(s) {
    return String(s == null ? '' : s).replace(/[&<>]/g, function (c) {
      return c === '&' ? '&amp;' : c === '<' ? '&lt;' : '&gt;';
    });
  }

  function fetchPage(opts) {
    var qs = '?limit=' + (opts.limit || 50) + '&offset=' + (opts.offset || 0);
    if (opts.filter) qs += '&filter=' + encodeURIComponent(opts.filter);
    return Promise.resolve(window.fetchAPI('/api/team/audit' + qs)).then(function (data) {
      return data || { events: [], total: 0 };
    });
  }

  function fetchControlActions() {
    return Promise.resolve(window.fetchAPI('/api/team/audit?filter=control&limit=20'))
      .then(function (data) { return (data && data.events) || []; });
  }

  function fetchPolicyDenials() {
    return Promise.resolve(window.fetchAPI('/api/team/audit?filter=deny&limit=20'))
      .then(function (data) { return (data && data.events) || []; });
  }

  function renderConnectionPanel() {
    var token = window.localStorage.getItem('arcui_viewer_token') || '';
    var hasToken = token ? 'present' : 'missing';
    return (
      '<div class="card mb-20">' +
        '<div class="card-header"><span class="card-title">Connection Security</span></div>' +
        '<div class="card-body">' +
          '<div class="kv"><span class="k">Viewer token</span><span class="v">' + hasToken + '</span></div>' +
          '<div class="kv"><span class="k">Transport</span><span class="v">WebSocket /ws + REST</span></div>' +
          '<div class="kv"><span class="k">mTLS</span><span class="v muted">' +
            '— exposed by gateway in production deployments' +
          '</span></div>' +
        '</div>' +
      '</div>'
    );
  }

  function renderEventList(events, kind) {
    if (!events.length) return '<div class="empty-state">No ' + escapeText(kind) + '</div>';
    return '<ul class="ad-list">' + events.map(function (e) {
      return '<li><span class="muted">' + escapeText(e.timestamp || '') + '</span> ' +
        '<b>' + escapeText(e.action || '') + '</b> ' +
        escapeText(e.target || '') +
        ' <span class="muted">' + escapeText(e.outcome || '') + '</span></li>';
    }).join('') + '</ul>';
  }

  function mount(panelEl) {
    panelEl.innerHTML =
      '<div class="page-header"><h1>Security &amp; Audit</h1></div>' +
      renderConnectionPanel() +
      '<div class="grid-2 mb-20">' +
        '<div class="card">' +
          '<div class="card-header"><span class="card-title">Recent Control Actions</span></div>' +
          '<div class="card-body sp-controls"><div class="loading">Loading…</div></div>' +
        '</div>' +
        '<div class="card">' +
          '<div class="card-header"><span class="card-title">Policy Denials</span></div>' +
          '<div class="card-body sp-denials"><div class="loading">Loading…</div></div>' +
        '</div>' +
      '</div>' +
      '<div class="card">' +
        '<div class="card-header"><span class="card-title">Audit Tail</span></div>' +
        '<div class="card-body sp-audit"></div>' +
      '</div>';

    var auditMount = panelEl.querySelector('.sp-audit');
    var auditInstance = window.ARC.AuditViewer.mount(auditMount, {
      fetchPage: fetchPage,
      pageSize: 50,
    });

    function refreshControls() {
      var el = panelEl.querySelector('.sp-controls');
      fetchControlActions().then(function (events) {
        el.innerHTML = renderEventList(events, 'control actions');
      });
    }
    function refreshDenials() {
      var el = panelEl.querySelector('.sp-denials');
      fetchPolicyDenials().then(function (events) {
        el.innerHTML = renderEventList(events, 'policy denials');
      });
    }
    refreshControls();
    refreshDenials();

    function onArcEvent(ev) {
      var msg = ev && ev.detail;
      // Anything audit-flavored refreshes the page
      if (!msg) return;
      var t = msg.event_type || msg.type;
      if (t && (t.indexOf('audit') >= 0 || t === 'gateway.fs.changed' || t === 'control:invoked')) {
        auditInstance.refresh();
        refreshControls();
        refreshDenials();
      }
    }
    window.addEventListener('arc:event', onArcEvent);

    return {
      refresh: function () {
        auditInstance.refresh();
        refreshControls();
        refreshDenials();
      },
      dispose: function () {
        try { auditInstance.dispose(); } catch (e) { /* noop */ }
        window.removeEventListener('arc:event', onArcEvent);
      },
    };
  }

  window.ARC = window.ARC || {};
  window.ARC.SecurityPage = { mount: mount };
})();
