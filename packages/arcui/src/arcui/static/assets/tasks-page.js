/* ============================================================
   ArcUI — Tasks Fleet Page (SPEC-022 §6.1)

   Lists tasks across all agents. Aggregator endpoint already merges
   per-agent tasks.json files server-side.

   API:  ARC.TasksPage.mount(panelEl) -> {refresh, dispose}
   Data: GET /api/team/tasks
   ============================================================ */

(function () {
  'use strict';

  function escapeText(s) {
    return String(s == null ? '' : s).replace(/[&<>]/g, function (c) {
      return c === '&' ? '&amp;' : c === '<' ? '&lt;' : '&gt;';
    });
  }

  function statusClass(status) {
    var v = String(status || '').toLowerCase();
    if (v === 'done' || v === 'completed') return 'tp-status-done';
    if (v === 'in_progress' || v === 'in-progress') return 'tp-status-running';
    if (v === 'failed' || v === 'error') return 'tp-status-bad';
    return 'tp-status-neutral';
  }

  function render(panelEl, items, filter) {
    var visible = filter === 'all' ? items : items.filter(function (t) {
      return String(t.status || '').toLowerCase() === filter;
    });
    panelEl.innerHTML =
      '<div class="page-header"><h1>Tasks</h1></div>' +
      '<div class="tp-toolbar mb-20">' +
        ['all', 'pending', 'in_progress', 'done', 'failed'].map(function (f) {
          return '<button type="button" class="tp-filter ' + (filter === f ? 'active' : '') +
            '" data-filter="' + f + '">' + escapeText(f) + '</button>';
        }).join('') +
      '</div>' +
      (visible.length
        ? '<table class="data-table"><thead><tr>' +
            '<th>Agent</th><th>ID</th><th>Subject</th><th>Status</th><th>Owner</th>' +
          '</tr></thead><tbody>' +
          visible.map(function (t) {
            return '<tr>' +
              '<td>' + escapeText(t.agent_id || '') + '</td>' +
              '<td class="mono">' + escapeText(t.id || '') + '</td>' +
              '<td>' + escapeText(t.subject || t.description || '') + '</td>' +
              '<td><span class="' + statusClass(t.status) + '">' + escapeText(t.status || '') + '</span></td>' +
              '<td>' + escapeText(t.owner || '') + '</td>' +
              '</tr>';
          }).join('') +
          '</tbody></table>'
        : '<div class="empty-state">No tasks</div>');
  }

  function mount(panelEl) {
    var items = [];
    var filter = 'all';

    function load() {
      panelEl.innerHTML = '<div class="loading">Loading tasks…</div>';
      return Promise.resolve(window.fetchAPI('/api/team/tasks')).then(function (resp) {
        items = (resp && (resp.tasks || resp)) || [];
        render(panelEl, items, filter);
      }).catch(function () {
        panelEl.innerHTML = '<div class="empty-state">Failed to load tasks</div>';
      });
    }

    function onClick(ev) {
      var btn = ev.target.closest('.tp-filter');
      if (btn) {
        filter = btn.dataset.filter;
        render(panelEl, items, filter);
      }
    }

    panelEl.addEventListener('click', onClick);
    load();

    function onArcEvent(ev) {
      var msg = ev && ev.detail;
      if (msg && msg.event_type === 'tasks:updated') load();
    }
    window.addEventListener('arc:event', onArcEvent);

    return {
      refresh: load,
      dispose: function () {
        panelEl.removeEventListener('click', onClick);
        window.removeEventListener('arc:event', onArcEvent);
      },
    };
  }

  window.ARC = window.ARC || {};
  window.ARC.TasksPage = { mount: mount };
})();
