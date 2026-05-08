/* ============================================================
   ArcUI — Agent Detail Page: shared helpers + TAB_LABELS

   Sibling of agent-detail.js. Owns the helpers reused across every
   renderer module (escText/escAttr, api, initials, modelShort,
   kvRow, card, fmtMtime/fmtBytes/fmtMtimeIso, sessionShort,
   classification + status badges) plus the tab list and tabHeader
   builder.

   IIFE pattern only — no `type="module"`, no bundler. Registers on
   the shared namespace ``ARC.AgentDetail._shared`` so each renderer
   sibling pulls helpers via:

     var _S = window.ARC.AgentDetail._shared;
     var escText = _S.escText;
     ...

   Load order: this file FIRST, then specialty renderer files, then
   the slim ``agent-detail.js`` last.
   ============================================================ */

(function () {
  'use strict';

  var TAB_LABELS = [
    ['overview',  'Overview'],
    ['identity',  'Identity'],
    ['sessions',  'Sessions'],
    ['skills',    'Skills'],
    ['memory',    'Memory'],
    ['policy',    'Policy'],
    ['tools',     'Tools'],
    ['telemetry', 'Telemetry'],
    ['files',     'Files'],
  ];

  function escText(s) {
    return String(s == null ? '' : s).replace(/[&<>]/g, function (c) {
      return c === '&' ? '&amp;' : c === '<' ? '&lt;' : '&gt;';
    });
  }
  function escAttr(s) { return escText(s).replace(/"/g, '&quot;'); }
  function api(p) { return Promise.resolve(window.fetchAPI(p)); }

  function initials(s) {
    return (s || '?').split(/\W+/).filter(Boolean).slice(0, 2)
      .map(function (w) { return w[0].toUpperCase(); }).join('') || '?';
  }

  function modelShort(m) {
    if (!m) return '—';
    var idx = m.indexOf('/');
    return idx >= 0 ? m.slice(idx + 1) : m;
  }

  function kvRow(k, v, opts) {
    opts = opts || {};
    var classes = 'kv-value' + (opts.mono ? ' mono' : '');
    var style = opts.style ? ' style="' + opts.style + '"' : '';
    return (
      '<div class="kv-row">' +
        '<div class="kv-key">' + escText(k) + '</div>' +
        '<div class="' + classes + '"' + style + '>' + (opts.html || escText(v)) + '</div>' +
      '</div>'
    );
  }

  function card(title, body, opts) {
    opts = opts || {};
    var headerExtra = opts.headerExtra || '';
    var bodyStyle = opts.bodyPadding === false ? ' style="padding:0;"' : '';
    return (
      '<div class="card mb-16">' +
        '<div class="card-header">' +
          '<span class="card-title">' + escText(title) + '</span>' +
          headerExtra +
        '</div>' +
        '<div class="card-body"' + bodyStyle + '>' + body + '</div>' +
      '</div>'
    );
  }

  function fmtMtime(mtime) {
    if (!mtime) return '—';
    if (typeof mtime !== 'number') return String(mtime);
    var d = new Date(mtime * 1000);
    var now = Date.now();
    var ago = (now - d.getTime()) / 1000;
    if (ago < 60) return Math.floor(ago) + 's ago';
    if (ago < 3600) return Math.floor(ago / 60) + 'm ago';
    if (ago < 86400) return Math.floor(ago / 3600) + 'h ago';
    if (ago < 86400 * 7) return Math.floor(ago / 86400) + 'd ago';
    return d.toISOString().slice(0, 10);
  }

  function fmtBytes(n) {
    if (n == null) return '—';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1024 / 1024).toFixed(1) + ' MB';
  }

  function fmtMtimeIso(s) {
    if (!s) return 'never';
    try {
      var d = new Date(s);
      var ago = (Date.now() - d.getTime()) / 1000;
      if (ago < 60) return Math.floor(ago) + 's ago';
      if (ago < 3600) return Math.floor(ago / 60) + 'm ago';
      if (ago < 86400) return Math.floor(ago / 3600) + 'h ago';
      if (ago < 86400 * 7) return Math.floor(ago / 86400) + 'd ago';
      return d.toISOString().slice(0, 16).replace('T', ' ');
    } catch (e) { return s; }
  }

  function sessionShort(sid) {
    return (sid || '').split('-')[0] || (sid || '').slice(0, 8) || '—';
  }

  function classificationBadge(c) {
    if (!c) return '<span class="tag">—</span>';
    var v = String(c).toLowerCase();
    if (v === 'read_only' || v === 'read-only')
      return '<span class="badge badge-info">read-only</span>';
    if (v === 'state_modifying' || v === 'write')
      return '<span class="badge badge-warning">' + escText(v.replace('_', '-')) + '</span>';
    if (v === 'external_effect' || v === 'execute')
      return '<span class="badge badge-error">' + escText(v.replace('_', '-')) + '</span>';
    if (v === 'inert')
      return '<span class="badge badge-neutral">inert</span>';
    return '<span class="tag">' + escText(v) + '</span>';
  }

  function statusBadgeFor(status) {
    var v = String(status || '').toLowerCase();
    if (v === 'allow' || v === '') return '<span class="badge badge-online">allow</span>';
    if (v === 'deny') return '<span class="badge badge-error">deny</span>';
    if (v === 'inactive') return '<span class="badge badge-neutral">inactive</span>';
    return '<span class="tag">' + escText(v) + '</span>';
  }

  function statusBadge(status) {
    var v = String(status || '').toLowerCase();
    if (v === 'done' || v === 'completed') return '<span class="badge badge-online">' + escText(status) + '</span>';
    if (v === 'failed' || v === 'error') return '<span class="badge badge-error">' + escText(status) + '</span>';
    if (v === 'pending') return '<span class="badge badge-warning">' + escText(status) + '</span>';
    if (v === 'in_progress' || v === 'running') return '<span class="badge badge-info">' + escText(status) + '</span>';
    return '<span class="badge badge-neutral">' + escText(status || '—') + '</span>';
  }

  function tabHeader(activeId) {
    return '<div class="tabs agd-tabs">' + TAB_LABELS.map(function (p) {
      var cls = p[0] === activeId ? 'tab pill-nav-item active' : 'tab pill-nav-item';
      return '<div class="' + cls + '" data-tab="' + p[0] + '">' + p[1] + '</div>';
    }).join('') + '</div>';
  }

  // Wire tool-row click for inline expand. Called after renderToolsTable mounts.
  function wireToolDrillDown(scopeEl) {
    scopeEl.querySelectorAll('.agd-tool-row').forEach(function (row) {
      var detail = row.nextElementSibling;
      if (!detail || !detail.classList.contains('agd-tool-detail')) return;
      row.addEventListener('click', function () {
        var open = detail.style.display !== 'none';
        detail.style.display = open ? 'none' : 'table-row';
        var caret = row.querySelector('td');
        if (caret) caret.textContent = open ? '▸' : '▾';
      });
    });
  }

  window.ARC = window.ARC || {};
  window.ARC.AgentDetail = window.ARC.AgentDetail || {};
  window.ARC.AgentDetail._shared = {
    TAB_LABELS: TAB_LABELS,
    escText: escText,
    escAttr: escAttr,
    api: api,
    initials: initials,
    modelShort: modelShort,
    kvRow: kvRow,
    card: card,
    fmtMtime: fmtMtime,
    fmtBytes: fmtBytes,
    fmtMtimeIso: fmtMtimeIso,
    sessionShort: sessionShort,
    classificationBadge: classificationBadge,
    statusBadgeFor: statusBadgeFor,
    statusBadge: statusBadge,
    tabHeader: tabHeader,
    wireToolDrillDown: wireToolDrillDown,
  };
})();
