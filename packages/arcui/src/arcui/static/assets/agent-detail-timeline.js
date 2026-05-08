/* ============================================================
   ArcUI — Agent Detail Page: Overview tab (timeline / dashboard)

   Sibling of agent-detail.js. Owns the Overview tab renderer and
   the helper builders it composes (renderKvGrid, renderToolsTable,
   renderRecentSessions, renderTasksList, renderSchedulesList).

   IIFE pattern only. Pulls helpers from ``ARC.AgentDetail._shared``;
   exposes the Overview renderer via
   ``ARC.AgentDetail._timeline.render(container, agentId)``.
   ============================================================ */

(function () {
  'use strict';

  var _S = window.ARC.AgentDetail._shared;
  var escText = _S.escText;
  var escAttr = _S.escAttr;
  var api = _S.api;
  var kvRow = _S.kvRow;
  var card = _S.card;
  var fmtMtime = _S.fmtMtime;
  var fmtBytes = _S.fmtBytes;
  var fmtMtimeIso = _S.fmtMtimeIso;
  var sessionShort = _S.sessionShort;
  var classificationBadge = _S.classificationBadge;
  var statusBadgeFor = _S.statusBadgeFor;
  var statusBadge = _S.statusBadge;
  var wireToolDrillDown = _S.wireToolDrillDown;

  function renderOverview(container, agentId) {
    container.innerHTML = '<div class="loading">Loading overview…</div>';
    Promise.all([
      api('/api/agents/' + agentId),
      api('/api/agents/' + agentId + '/config'),
      api('/api/agents/' + agentId + '/stats'),
      api('/api/agents/' + agentId + '/sessions'),
      api('/api/agents/' + agentId + '/tools'),
      api('/api/agents/' + agentId + '/tasks'),
      api('/api/agents/' + agentId + '/schedules'),
    ]).then(function (r) {
      var meta = r[0] || {};
      var cfg  = (r[1] && r[1].config) || {};
      var statsRaw = r[2] || {};
      var stats = (statsRaw && statsRaw.stats) || statsRaw;
      var sessions  = (r[3] && r[3].sessions)  || [];
      var toolsResp = r[4] || {};
      var tools     = toolsResp.tools || [];
      var allowList = toolsResp.allowlist || [];
      var denyList  = toolsResp.denylist || [];
      var tasks     = (r[5] && r[5].tasks)     || [];
      var schedules = (r[6] && r[6].schedules) || [];

      var llm = cfg.llm || {};
      var ctx = cfg.context || {};
      var sessionCfg = cfg.session || {};
      var telCfg = cfg.telemetry || {};

      var fmt = (window.Fmt && window.Fmt.number)
        ? window.Fmt.number.bind(window.Fmt) : String;

      var totalCtx = (ctx && ctx.max_tokens) || (llm && llm.context_window) || 0;
      var pruneThr  = (ctx && ctx.prune_threshold)  || 0.70;
      var compactThr = (ctx && ctx.compact_threshold) || 0.85;
      var emergencyThr = (ctx && ctx.emergency_threshold) || 0.95;
      var ctxBody =
        '<div id="agd-ctx-window">' +
          '<div class="flex justify-between text-xs text-muted mb-4">' +
            '<span>Last prompt size</span>' +
            '<span id="agd-ctx-pct">—</span>' +
          '</div>' +
          '<div class="progress-bar" style="height:8px;margin-bottom:12px;">' +
            '<div class="fill" id="agd-ctx-fill" style="width:0%"></div>' +
          '</div>' +
          '<div class="grid-3" style="gap:12px;">' +
            '<div><div class="text-xs text-dimmed">Used</div>' +
              '<div class="text-sm text-white font-bold" id="agd-ctx-used">—</div></div>' +
            '<div><div class="text-xs text-dimmed">Available</div>' +
              '<div class="text-sm text-white font-bold" id="agd-ctx-avail">—</div></div>' +
            '<div><div class="text-xs text-dimmed">Total</div>' +
              '<div class="text-sm text-white font-bold">' +
                escText(fmt(totalCtx)) + '</div></div>' +
          '</div>' +
          '<div style="margin-top:12px;padding:8px;background:var(--bg-deepest);border-radius:var(--radius-sm);">' +
            '<div class="text-xs text-dimmed mb-4">Thresholds</div>' +
            '<div style="display:flex;gap:12px;flex-wrap:wrap;">' +
              '<span class="text-xs"><span style="color:var(--status-online);">●</span> Prune: ' +
                Math.round(pruneThr * 100) + '%</span>' +
              '<span class="text-xs"><span style="color:var(--status-warning);">●</span> Compact: ' +
                Math.round(compactThr * 100) + '%</span>' +
              '<span class="text-xs"><span style="color:var(--status-error);">●</span> Emergency: ' +
                Math.round(emergencyThr * 100) + '%</span>' +
            '</div>' +
          '</div>' +
        '</div>';

      var leftCol =
        card('Cryptographic Identity', renderKvGrid([
          ['DID', meta.did || '—', { html: '<span class="did">' + escText(meta.did || '—') + '</span>' }],
          ['Organization', meta.org || '—'],
          ['Agent Type', meta.type || '—'],
          ['Display Name', meta.display_name || meta.name || '—'],
          ['Status', meta.online
            ? '<span class="badge badge-online">online</span>'
            : '<span class="badge badge-neutral">offline</span>',
            { html: meta.online
              ? '<span class="badge badge-online">online</span>'
              : '<span class="badge badge-neutral">offline</span>' }],
          ['Workspace', meta.workspace_path || '—', { mono: true }],
        ]), { headerExtra: meta.online
          ? '<span class="badge badge-online">live</span>'
          : '<span class="badge badge-neutral">offline</span>' }) +

        card('Configuration', renderKvGrid([
          ['Model', llm.model || '—'],
          ['Provider', llm.provider || meta.provider || '—'],
          ['Max Tokens', llm.max_tokens != null ? String(llm.max_tokens) : '—'],
          ['Temperature', llm.temperature != null ? String(llm.temperature) : '—'],
          ['Context Window', ctx.max_tokens || llm.context_window || '—'],
          ['Tool Timeout', cfg.tools && cfg.tools.policy && cfg.tools.policy.timeout_seconds
            ? cfg.tools.policy.timeout_seconds + 's' : '—'],
          ['Telemetry', telCfg.enabled ? 'enabled' : '—'],
          ['Service', telCfg.service_name || '—'],
        ])) +

        card('Context Window', ctxBody, {
          headerExtra: '<span class="text-xs text-muted">last prompt</span>',
        }) +

        '';

      var calls = stats.request_count || 0;
      var tokens = stats.total_tokens || 0;
      var latencyAvg = Math.round(stats.latency_avg || 0);
      var latencyP95 = Math.round(stats.latency_p95 || 0);
      var totalCost = stats.total_cost || 0;

      var errorCount = stats.error_count || 0;
      var successPct = calls > 0 ? Math.round(((calls - errorCount) / calls) * 100) : 0;
      var uptimePct = meta.online ? 100 : (calls > 0 ? successPct : 0);
      var responsePct = latencyAvg > 0
        ? Math.max(5, Math.min(100, Math.round(100 - (latencyAvg / 5000) * 100)))
        : 0;
      var responseLabel = latencyAvg > 0
        ? (latencyAvg >= 1000 ? (latencyAvg / 1000).toFixed(1) + 's' : latencyAvg + 'ms')
        : '—';
      var perfBody =
        '<div class="grid-3" style="gap:12px;align-items:center;">' +
          '<div style="text-align:center;">' +
            '<div class="gauge" style="--pct:' + uptimePct + ';margin:0 auto;">' +
              '<span class="gauge-label">' + uptimePct + '%</span>' +
            '</div>' +
            '<div class="text-xs text-dimmed mt-8">Uptime</div>' +
          '</div>' +
          '<div style="text-align:center;">' +
            '<div class="gauge" style="--pct:' + responsePct + ';margin:0 auto;">' +
              '<span class="gauge-label">' + escText(responseLabel) + '</span>' +
            '</div>' +
            '<div class="text-xs text-dimmed mt-8">Avg Response</div>' +
          '</div>' +
          '<div style="text-align:center;">' +
            '<div class="gauge" style="--pct:' + successPct + ';margin:0 auto;">' +
              '<span class="gauge-label">' + successPct + '%</span>' +
            '</div>' +
            '<div class="text-xs text-dimmed mt-8">Tool Success</div>' +
          '</div>' +
        '</div>' +
        '<div style="margin-top:16px;">' +
          '<div class="text-xs text-dimmed mb-8">Token Usage (24h)</div>' +
          '<div id="agd-token-chart" style="height:80px;display:flex;align-items:flex-end;gap:2px;background:var(--bg-deepest);border-radius:var(--radius-sm);padding:8px;">' +
            '<div class="text-xs text-dimmed">loading…</div>' +
          '</div>' +
        '</div>' +
        '<div style="margin-top:12px;font-size:12px;color:var(--text-muted);">' +
          'Total cost: <span class="text-white">$' + totalCost.toFixed(4) + '</span> · ' +
          'P95: <span class="mono">' + escText(latencyP95) + 'ms</span> · ' +
          'Calls: <span class="mono">' + escText(fmt(calls)) + '</span>' +
        '</div>';

      var rightCol =
        card('Performance (24h)', perfBody) +
        card('Recent Sessions',
          renderRecentSessions(sessions),
          { bodyPadding: false }) +
        card('Tasks (' + tasks.length + ')',
          renderTasksList(tasks)) +
        card('Schedules (' + schedules.length + ')',
          renderSchedulesList(schedules));

      container.innerHTML =
        '<div class="grid-2 mb-20">' +
          '<div>' + leftCol + '</div>' +
          '<div>' + rightCol + '</div>' +
        '</div>';
      wireToolDrillDown(container);

      api('/api/agents/' + agentId + '/traces?limit=1').then(function (tr) {
        var traces = (tr && tr.traces) || [];
        if (!traces.length || !totalCtx) return;
        var t = traces[0];
        var used = t.prompt_tokens || t.input_tokens || 0;
        var pct = Math.min(100, Math.round((used / totalCtx) * 100));
        var fillEl = container.querySelector('#agd-ctx-fill');
        var pctEl = container.querySelector('#agd-ctx-pct');
        var usedEl = container.querySelector('#agd-ctx-used');
        var availEl = container.querySelector('#agd-ctx-avail');
        if (fillEl) {
          fillEl.style.width = pct + '%';
          fillEl.classList.remove('warning', 'error', 'success');
          if (pct >= emergencyThr * 100) fillEl.classList.add('error');
          else if (pct >= compactThr * 100) fillEl.classList.add('warning');
          else if (pct >= pruneThr * 100) fillEl.classList.add('success');
        }
        if (pctEl) pctEl.textContent = pct + '%';
        if (usedEl) usedEl.textContent = fmt(used);
        if (availEl) availEl.textContent = fmt(Math.max(0, totalCtx - used));
      });

      api('/api/stats/timeseries?window=24h&agent_id=' + agentId).then(function (data) {
        var chartEl = container.querySelector('#agd-token-chart');
        if (!chartEl) return;
        var buckets = (data && data.buckets) || [];
        if (!buckets.length) {
          chartEl.innerHTML = '<div class="text-xs text-dimmed">No 24h activity</div>';
          return;
        }
        var maxTokens = Math.max.apply(null, buckets.map(function (b) {
          return b.total_tokens || 0;
        }).concat([1]));
        chartEl.innerHTML = '';
        chartEl.style.justifyContent = 'space-between';
        buckets.forEach(function (b) {
          var h = b.total_tokens > 0
            ? Math.max(2, Math.round((b.total_tokens / maxTokens) * 60))
            : 2;
          var bar = document.createElement('div');
          bar.style.flex = '1';
          bar.style.height = h + 'px';
          bar.style.background = b.total_tokens > 0
            ? 'var(--accent)' : 'var(--border-primary)';
          bar.style.borderRadius = '1px';
          bar.style.opacity = b.total_tokens > 0 ? '0.8' : '0.3';
          bar.title = (b.total_tokens || 0) + ' tokens, ' +
            (b.request_count || 0) + ' calls';
          chartEl.appendChild(bar);
        });
      });
    }).catch(function (e) {
      container.innerHTML = '<div class="empty-state">Failed to load overview: ' +
        escText(String(e && e.message || e)) + '</div>';
    });
  }

  function renderKvGrid(pairs) {
    return '<div class="kv-grid">' + pairs.map(function (p) {
      var k = p[0], v = p[1], opts = p[2] || {};
      return kvRow(k, v, opts);
    }).join('') + '</div>';
  }

  function renderToolsTable(tools, allowList, denyList) {
    if (!tools.length && !allowList.length && !denyList.length) {
      return '<div class="empty-state">No tools registered</div>';
    }
    var seen = {};
    var rows = [];
    tools.forEach(function (t) {
      var name = t.name || t;
      if (seen[name]) return;
      seen[name] = true;
      rows.push({
        name: name,
        transport: t.transport || t.kind || '—',
        classification: t.classification || '',
        description: t.description || '',
        status: t.status ||
          (denyList.indexOf(name) >= 0 ? 'deny' : 'allow'),
      });
    });
    allowList.forEach(function (n) { if (!seen[n]) {
      seen[n] = true;
      rows.push({ name: n, transport: 'config', classification: '',
                  description: '', status: 'allow' });
    }});
    denyList.forEach(function (n) { if (!seen[n]) {
      seen[n] = true;
      rows.push({ name: n, transport: 'config', classification: '',
                  description: '', status: 'deny' });
    }});

    return '<table><thead><tr>' +
      '<th></th><th>Tool</th><th>Transport</th><th>Classification</th><th>Status</th>' +
      '</tr></thead><tbody>' +
      rows.map(function (r) {
        var dimmed = r.status === 'inactive' ? ' style="opacity:.5;"' : '';
        var detail = r.description
          ? '<tr class="agd-tool-detail" style="display:none;">' +
              '<td colspan="5" style="padding:8px 14px 12px 36px;' +
              'background:var(--bg-deepest);color:var(--text-secondary);' +
              'font-size:12px;">' + escText(r.description) + '</td>' +
            '</tr>' : '';
        return '<tr class="agd-tool-row" data-name="' + escAttr(r.name) +
          '" style="cursor:pointer;"' + dimmed + '>' +
            '<td class="text-dimmed mono" style="font-size:10px;width:14px;">▸</td>' +
            '<td class="text-white">' + escText(r.name) + '</td>' +
            '<td><span class="tag">' + escText(r.transport) + '</span></td>' +
            '<td>' + classificationBadge(r.classification) + '</td>' +
            '<td>' + statusBadgeFor(r.status) + '</td>' +
          '</tr>' + detail;
      }).join('') +
      '</tbody></table>';
  }

  function renderRecentSessions(sessions) {
    var items = (sessions || []).slice().sort(function (a, b) {
      return (b.mtime || 0) - (a.mtime || 0);
    }).slice(0, 6);
    if (!items.length) return '<div class="empty-state">No sessions</div>';
    return '<table><thead><tr>' +
      '<th>Session</th><th>Modified</th><th>Size</th>' +
      '</tr></thead><tbody>' +
      items.map(function (s) {
        var sid = s.sid || s.session_id || s.id || '';
        return '<tr class="agd-session-row" data-sid="' + escAttr(sid) + '" style="cursor:pointer;">' +
          '<td class="mono text-accent">' + escText(sessionShort(sid)) + '</td>' +
          '<td class="text-muted">' + escText(fmtMtime(s.mtime)) + '</td>' +
          '<td class="mono text-muted">' + escText(fmtBytes(s.size)) + '</td>' +
          '</tr>';
      }).join('') +
      '</tbody></table>';
  }

  function renderTasksList(tasks) {
    if (!tasks.length) return '<div class="empty-state">No tasks</div>';
    return tasks.slice(0, 8).map(function (t) {
      var summary = t.description || t.subject || t.title || t.id || '—';
      var truncated = summary.length > 100 ? summary.slice(0, 100) + '…' : summary;
      return '<details class="agd-detail-row" style="border-bottom:1px solid var(--border-primary);padding:8px 0;">' +
        '<summary style="cursor:pointer;list-style:none;font-size:12px;display:flex;gap:8px;align-items:start;">' +
          '<span style="color:var(--accent);font-size:10px;margin-top:2px;">▸</span>' +
          '<span style="flex:1;min-width:0;">' +
            '<span class="text-white">' + escText(truncated) + '</span> ' +
            statusBadge(t.status) +
          '</span>' +
        '</summary>' +
        '<div style="padding:8px 0 8px 22px;font-size:12px;color:var(--text-muted);">' +
          '<div><span class="text-dimmed">id:</span> <span class="mono">' + escText(t.id || '—') + '</span></div>' +
          (t.result ? '<div style="margin-top:6px;"><span class="text-dimmed">result:</span>' +
            '<div class="text-secondary" style="margin-top:2px;white-space:pre-wrap;">' + escText(t.result) + '</div></div>' : '') +
          (t.description && t.description !== summary
            ? '<div style="margin-top:6px;"><span class="text-dimmed">full:</span>' +
              '<div class="text-secondary" style="margin-top:2px;white-space:pre-wrap;">' + escText(t.description) + '</div></div>'
            : '') +
        '</div>' +
      '</details>';
    }).join('');
  }

  function renderSchedulesList(schedules) {
    if (!schedules.length) return '<div class="empty-state">No schedules</div>';
    return schedules.slice(0, 8).map(function (s) {
      var meta = s.metadata || {};
      var lastRun = meta.last_run ? fmtMtimeIso(meta.last_run) : 'never';
      var runCount = meta.run_count != null ? meta.run_count : 0;
      var promptShort = (s.prompt || '').slice(0, 90) + ((s.prompt || '').length > 90 ? '…' : '');
      var timing = s.expression ? 'cron: ' + s.expression :
                   s.at ? 'once: ' + s.at :
                   s.every_seconds ? 'every ' + s.every_seconds + 's' : '—';
      return '<details class="agd-detail-row" style="border-bottom:1px solid var(--border-primary);padding:8px 0;">' +
        '<summary style="cursor:pointer;list-style:none;font-size:12px;display:flex;gap:8px;align-items:start;">' +
          '<span style="color:var(--accent);font-size:10px;margin-top:2px;">▸</span>' +
          '<span style="flex:1;min-width:0;">' +
            '<div style="display:flex;gap:8px;align-items:center;margin-bottom:2px;">' +
              '<span class="mono text-accent">' + escText(s.id) + '</span> ' +
              (s.enabled ? '<span class="badge badge-online">on</span>'
                         : '<span class="badge badge-neutral">off</span>') +
            '</div>' +
            '<div class="text-secondary">' + escText(promptShort || '(no prompt)') + '</div>' +
          '</span>' +
        '</summary>' +
        '<div style="padding:8px 0 8px 22px;font-size:12px;color:var(--text-muted);">' +
          '<div class="kv-row"><div class="kv-key">Type</div><div class="kv-value mono">' + escText(s.type || '—') + '</div></div>' +
          '<div class="kv-row"><div class="kv-key">Timing</div><div class="kv-value mono">' + escText(timing) + '</div></div>' +
          '<div class="kv-row"><div class="kv-key">Last run</div><div class="kv-value mono">' + escText(lastRun) + '</div></div>' +
          '<div class="kv-row"><div class="kv-key">Runs</div><div class="kv-value mono">' + escText(runCount) + '</div></div>' +
          (meta.last_result ? '<div class="kv-row"><div class="kv-key">Last result</div><div class="kv-value">' +
            statusBadge(meta.last_result) + '</div></div>' : '') +
          (meta.last_duration_seconds != null ? '<div class="kv-row"><div class="kv-key">Duration</div><div class="kv-value mono">' +
            meta.last_duration_seconds + 's</div></div>' : '') +
          (s.prompt ? '<div style="margin-top:6px;"><span class="text-dimmed">Prompt:</span>' +
            '<div class="text-secondary" style="margin-top:2px;white-space:pre-wrap;">' + escText(s.prompt) + '</div></div>' : '') +
        '</div>' +
      '</details>';
    }).join('');
  }

  window.ARC.AgentDetail._timeline = {
    render: renderOverview,
    renderKvGrid: renderKvGrid,
    renderToolsTable: renderToolsTable,
  };
})();
