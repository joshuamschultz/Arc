/* ============================================================
   ArcUI — Agent Detail Page: Sessions + Telemetry tabs (audit views)

   Sibling of agent-detail.js. Owns the Sessions list (with replay
   drill-down) and the Telemetry tab (stat cards + recent traces).
   IIFE. Exposes ``ARC.AgentDetail._audit.{renderSessions,renderTelemetry}``.
   ============================================================ */

(function () {
  'use strict';

  var _S = window.ARC.AgentDetail._shared;
  var escText = _S.escText;
  var escAttr = _S.escAttr;
  var api = _S.api;
  var modelShort = _S.modelShort;
  var fmtMtime = _S.fmtMtime;
  var fmtBytes = _S.fmtBytes;
  var sessionShort = _S.sessionShort;

  function renderSessions(container, agentId) {
    container.innerHTML = '<div class="loading">Loading sessions…</div>';
    api('/api/agents/' + agentId + '/sessions').then(function (resp) {
      var list = ((resp && resp.sessions) || []).slice().sort(function (a, b) {
        return (b.mtime || 0) - (a.mtime || 0);
      });
      if (!list.length) {
        container.innerHTML = '<div class="card"><div class="card-body">' +
          '<div class="empty-state">No sessions</div></div></div>';
        return;
      }
      container.innerHTML =
        '<div class="card">' +
          '<div class="card-header">' +
            '<span class="card-title">All Sessions (' + list.length + ')</span>' +
            '<span class="text-xs text-dimmed">click a row to expand</span>' +
          '</div>' +
          '<div class="card-body" style="padding:0;">' +
            list.map(function (s) {
              var sid = s.sid || s.session_id || '';
              return (
                '<details class="agd-session-detail" data-sid="' + escAttr(sid) + '" ' +
                'style="border-bottom:1px solid var(--border-primary);">' +
                  '<summary style="display:flex;gap:12px;align-items:center;padding:10px 14px;' +
                    'cursor:pointer;list-style:none;font-size:13px;">' +
                    '<span class="mono text-accent" style="min-width:80px;">' +
                      escText(sessionShort(sid)) + '</span>' +
                    '<span class="text-muted" style="flex:1;">' +
                      escText(fmtMtime(s.mtime)) + '</span>' +
                    '<span class="mono text-muted">' + escText(fmtBytes(s.size)) + '</span>' +
                    '<span class="text-dimmed" style="font-size:10px;">▾</span>' +
                  '</summary>' +
                  '<div class="agd-session-replay" style="padding:0 14px 14px 14px;' +
                    'background:var(--bg-deepest);">' +
                    '<div class="loading">Click to load…</div>' +
                  '</div>' +
                '</details>'
              );
            }).join('') +
          '</div>' +
        '</div>';

      container.querySelectorAll('.agd-session-detail').forEach(function (row) {
        var loaded = false;
        row.addEventListener('toggle', function () {
          if (!row.open || loaded) return;
          loaded = true;
          var sid = row.getAttribute('data-sid');
          var replay = row.querySelector('.agd-session-replay');
          replay.innerHTML = '<div class="loading">Loading replay…</div>';
          api('/api/agents/' + agentId + '/sessions/' +
              encodeURIComponent(sid)).then(function (sr) {
            var msgs = (sr && (sr.messages || sr.events)) || [];
            if (!msgs.length) {
              replay.innerHTML = '<div class="empty-state">Empty session</div>';
              return;
            }
            replay.innerHTML =
              '<div style="font-size:11px;color:var(--text-dimmed);margin:8px 0;">' +
                'Showing ' + Math.min(msgs.length, 50) + ' of ' + msgs.length + ' messages' +
              '</div>' +
              msgs.slice(0, 50).map(function (m) {
                var role = m.role || m.type || '';
                var roleBadge = role === 'user'
                  ? '<span class="badge badge-info">user</span>'
                  : role === 'assistant'
                  ? '<span class="badge badge-accent">assistant</span>'
                  : role === 'tool'
                  ? '<span class="badge badge-warning">tool</span>'
                  : role === 'system'
                  ? '<span class="badge badge-neutral">system</span>'
                  : '<span class="badge badge-neutral">' + escText(role || '?') + '</span>';
                var content = m.content || m.text || JSON.stringify(m);
                if (typeof content !== 'string') content = JSON.stringify(content, null, 2);
                return '<div style="margin-bottom:12px;padding:8px;' +
                  'background:var(--bg-secondary);border-radius:var(--radius-sm);">' +
                  '<div style="margin-bottom:4px;">' + roleBadge + '</div>' +
                  '<div class="mono text-secondary" style="font-size:12px;' +
                    'white-space:pre-wrap;line-height:1.5;">' +
                    escText(content.slice(0, 2000)) +
                    (content.length > 2000 ? '\n\n…(' + (content.length - 2000) + ' more chars)' : '') +
                  '</div></div>';
              }).join('');
          }).catch(function () {
            replay.innerHTML = '<div class="empty-state">Failed to load replay</div>';
          });
        });
      });
    });
  }

  function renderTelemetry(container, agentId) {
    container.innerHTML = '<div class="loading">Loading telemetry…</div>';
    Promise.all([
      api('/api/agents/' + agentId + '/stats'),
      api('/api/agents/' + agentId + '/traces?limit=50'),
    ]).then(function (r) {
      var statsRaw = r[0] || {};
      var stats = (statsRaw && statsRaw.stats) || statsRaw;
      var traces = (r[1] && r[1].traces) || [];

      var fmt = (window.Fmt && window.Fmt.number)
        ? window.Fmt.number.bind(window.Fmt) : String;

      container.innerHTML =
        '<div class="grid-4 mb-20">' +
          '<div class="stat-card"><div class="stat-card-label">Calls</div>' +
            '<div class="stat-card-value">' + escText(fmt(stats.request_count || 0)) + '</div></div>' +
          '<div class="stat-card"><div class="stat-card-label">Tokens</div>' +
            '<div class="stat-card-value">' + escText(fmt(stats.total_tokens || 0)) + '</div></div>' +
          '<div class="stat-card"><div class="stat-card-label">Avg Latency</div>' +
            '<div class="stat-card-value">' + escText(Math.round(stats.latency_avg || 0)) + 'ms</div>' +
            '<div class="stat-card-sub">P95 ' + escText(Math.round(stats.latency_p95 || 0)) + 'ms · P99 ' + escText(Math.round(stats.latency_p99 || 0)) + 'ms</div></div>' +
          '<div class="stat-card"><div class="stat-card-label">Cost</div>' +
            '<div class="stat-card-value">$' + (stats.total_cost || 0).toFixed(4) + '</div></div>' +
        '</div>' +
        '<div class="card">' +
          '<div class="card-header">' +
            '<span class="card-title">Recent Traces (' + traces.length + ')</span>' +
          '</div>' +
          '<div class="card-body" style="padding:0;">' +
            (traces.length
              ? '<table class="agd-trace-table"><thead><tr>' +
                '<th>Trace</th><th>Time</th><th>Model</th><th>Tokens</th><th>Latency</th><th>Status</th>' +
                '</tr></thead><tbody>' +
                traces.map(function (t) {
                  var status = t.status_code && t.status_code >= 400 ? 'error' : 'ok';
                  var badge = status === 'ok'
                    ? '<span class="badge badge-online">ok</span>'
                    : '<span class="badge badge-error">error</span>';
                  var traceId = t.trace_id || '';
                  var traceShort = traceId.slice(0, 10);
                  var totalMs = t.duration_ms || t.latency_ms || 0;
                  return '<tr class="agd-trace-row" data-trace-id="' +
                    escAttr(traceId) + '" style="cursor:pointer;">' +
                    '<td class="mono text-accent">' + escText(traceShort) + '</td>' +
                    '<td class="mono text-muted">' + escText(t.timestamp || '') + '</td>' +
                    '<td class="text-white">' + escText(modelShort(t.model || '')) + '</td>' +
                    '<td class="mono">' + escText(fmt(t.total_tokens || t.tokens || 0)) + '</td>' +
                    '<td class="mono text-muted">' + escText(totalMs ? Math.round(totalMs) + 'ms' : '—') + '</td>' +
                    '<td>' + badge + '</td>' +
                    '</tr>';
                }).join('') +
                '</tbody></table>'
              : '<div class="empty-state">No traces in store</div>') +
          '</div>' +
        '</div>';

      container.querySelectorAll('.agd-trace-row').forEach(function (row) {
        row.addEventListener('click', function () {
          var tid = row.getAttribute('data-trace-id');
          if (tid && window.ARC && window.ARC.openTraceDrawer) {
            window.ARC.openTraceDrawer(tid);
          }
        });
      });
    });
  }

  window.ARC.AgentDetail._audit = {
    renderSessions: renderSessions,
    renderTelemetry: renderTelemetry,
  };
})();
