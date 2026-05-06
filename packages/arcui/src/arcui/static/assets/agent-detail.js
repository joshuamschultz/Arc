/* ============================================================
   ArcUI — Agent Detail Page (SPEC-022, demo-aligned)

   Mirrors demo/agent-detail.html DOM:
     - breadcrumb · message-avatar · h1 · badges · DID strip
     - Pause / Restart / Deploy in page-header-actions
     - <div class="tabs"><div class="tab" data-tab="...">...
     - kv-grid for identity/config; <table> for sessions/tools/modules

   API: ARC.AgentDetail.mount(panelEl, agentId) -> {dispose, refresh, setTab}
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

  // ============================================================
  // Tab renderers
  // ============================================================

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

      // Build ctxBody early so leftCol can include it. (var hoists, but
      // assignment doesn't — referencing ctxBody before its line evaluates
      // to undefined and produces the "undefined" placeholder we saw.)
      var totalCtx = (ctx && ctx.max_tokens) || (llm && llm.context_window) || 0;
      var pruneThr  = (ctx && ctx.prune_threshold)  || 0.70;
      var compactThr = (ctx && ctx.compact_threshold) || 0.85;
      var emergencyThr = (ctx && ctx.emergency_threshold) || 0.95;
      var ctxBody =
        '<div id="ad-ctx-window">' +
          '<div class="flex justify-between text-xs text-muted mb-4">' +
            '<span>Last prompt size</span>' +
            '<span id="ad-ctx-pct">—</span>' +
          '</div>' +
          '<div class="progress-bar" style="height:8px;margin-bottom:12px;">' +
            '<div class="fill" id="ad-ctx-fill" style="width:0%"></div>' +
          '</div>' +
          '<div class="grid-3" style="gap:12px;">' +
            '<div><div class="text-xs text-dimmed">Used</div>' +
              '<div class="text-sm text-white font-bold" id="ad-ctx-used">—</div></div>' +
            '<div><div class="text-xs text-dimmed">Available</div>' +
              '<div class="text-sm text-white font-bold" id="ad-ctx-avail">—</div></div>' +
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

      // Right column: performance + recent sessions + tasks/schedules
      var calls = stats.request_count || 0;
      var tokens = stats.total_tokens || 0;
      var latencyAvg = Math.round(stats.latency_avg || 0);
      var latencyP95 = Math.round(stats.latency_p95 || 0);
      var totalCost = stats.total_cost || 0;

      // Performance: three circular gauges + token usage bar chart.
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
          '<div id="ad-token-chart" style="height:80px;display:flex;align-items:flex-end;gap:2px;background:var(--bg-deepest);border-radius:var(--radius-sm);padding:8px;">' +
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

      // -- Live context window: pull most recent trace's prompt_tokens.
      //    Re-runs on policy:bullets_updated / pulse:updated WS events
      //    because the active-tab dispatcher already re-renders Overview.
      api('/api/agents/' + agentId + '/traces?limit=1').then(function (tr) {
        var traces = (tr && tr.traces) || [];
        if (!traces.length || !totalCtx) return;
        var t = traces[0];
        var used = t.prompt_tokens || t.input_tokens || 0;
        var pct = Math.min(100, Math.round((used / totalCtx) * 100));
        var fillEl = container.querySelector('#ad-ctx-fill');
        var pctEl = container.querySelector('#ad-ctx-pct');
        var usedEl = container.querySelector('#ad-ctx-used');
        var availEl = container.querySelector('#ad-ctx-avail');
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

      // -- Token usage bar chart from per-agent timeseries.
      api('/api/stats/timeseries?window=24h&agent_id=' + agentId).then(function (data) {
        var chartEl = container.querySelector('#ad-token-chart');
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
    // Surface allow/deny entries that didn't appear in the tool list (rare —
    // misconfigured policy referencing a tool the agent doesn't have).
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
          ? '<tr class="ad-tool-detail" style="display:none;">' +
              '<td colspan="5" style="padding:8px 14px 12px 36px;' +
              'background:var(--bg-deepest);color:var(--text-secondary);' +
              'font-size:12px;">' + escText(r.description) + '</td>' +
            '</tr>' : '';
        return '<tr class="ad-tool-row" data-name="' + escAttr(r.name) +
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

  // Wire tool-row click for inline expand. Called after renderToolsTable mounts.
  function wireToolDrillDown(scopeEl) {
    scopeEl.querySelectorAll('.ad-tool-row').forEach(function (row) {
      var detail = row.nextElementSibling;
      if (!detail || !detail.classList.contains('ad-tool-detail')) return;
      row.addEventListener('click', function () {
        var open = detail.style.display !== 'none';
        detail.style.display = open ? 'none' : 'table-row';
        var caret = row.querySelector('td');
        if (caret) caret.textContent = open ? '▸' : '▾';
      });
    });
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

  function sessionShort(sid) {
    return (sid || '').split('-')[0] || (sid || '').slice(0, 8) || '—';
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
        return '<tr class="ad-session-row" data-sid="' + escAttr(sid) + '" style="cursor:pointer;">' +
          '<td class="mono text-accent">' + escText(sessionShort(sid)) + '</td>' +
          '<td class="text-muted">' + escText(fmtMtime(s.mtime)) + '</td>' +
          '<td class="mono text-muted">' + escText(fmtBytes(s.size)) + '</td>' +
          '</tr>';
      }).join('') +
      '</tbody></table>';
  }

  function statusBadge(status) {
    var v = String(status || '').toLowerCase();
    if (v === 'done' || v === 'completed') return '<span class="badge badge-online">' + escText(status) + '</span>';
    if (v === 'failed' || v === 'error') return '<span class="badge badge-error">' + escText(status) + '</span>';
    if (v === 'pending') return '<span class="badge badge-warning">' + escText(status) + '</span>';
    if (v === 'in_progress' || v === 'running') return '<span class="badge badge-info">' + escText(status) + '</span>';
    return '<span class="badge badge-neutral">' + escText(status || '—') + '</span>';
  }

  function renderTasksList(tasks) {
    if (!tasks.length) return '<div class="empty-state">No tasks</div>';
    return tasks.slice(0, 8).map(function (t) {
      var summary = t.description || t.subject || t.title || t.id || '—';
      var truncated = summary.length > 100 ? summary.slice(0, 100) + '…' : summary;
      return '<details class="ad-detail-row" style="border-bottom:1px solid var(--border-primary);padding:8px 0;">' +
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
      return '<details class="ad-detail-row" style="border-bottom:1px solid var(--border-primary);padding:8px 0;">' +
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

  function renderIdentity(container, agentId) {
    container.innerHTML = '<div class="loading">Loading identity…</div>';
    Promise.all([
      api('/api/agents/' + agentId),
      api('/api/agents/' + agentId + '/config'),
    ]).then(function (r) {
      var meta = r[0] || {};
      var cfg = (r[1] && r[1].config) || {};
      var ident = cfg.identity || {};
      container.innerHTML =
        '<div class="grid-2 mb-20">' +
          '<div>' +
            card('DID',
              '<div class="crypto-key" style="font-size:13px;">' +
                escText(meta.did || '—') +
              '</div>',
              { headerExtra: '<span class="badge badge-online">parsed</span>' }) +
            card('Identity Config', renderKvGrid([
              ['DID',           ident.did || meta.did || '—'],
              ['Key Directory', ident.key_dir || '—'],
              ['Algorithm',     'Ed25519 (RFC 8032)'],
              ['Curve',         'Curve25519'],
              ['Key Size',      '256-bit (32 bytes)'],
            ])) +
          '</div>' +
          '<div>' +
            card('Tool Policy', renderKvGrid([
              ['Allow', (cfg.tools && cfg.tools.policy && (cfg.tools.policy.allow || []).join(', ')) || '∅ (deny-all)'],
              ['Deny',  (cfg.tools && cfg.tools.policy && (cfg.tools.policy.deny || []).join(', ')) || '∅'],
              ['Timeout', (cfg.tools && cfg.tools.policy && cfg.tools.policy.timeout_seconds + 's') || '—'],
            ])) +
            card('Workspace', renderKvGrid([
              ['Path', meta.workspace_path || '—', { mono: true }],
              ['Color', meta.color || '—',
                { html: '<span style="display:inline-block;width:14px;height:14px;background:' +
                  escText(meta.color || '#888') + ';border-radius:3px;vertical-align:middle;margin-right:6px;"></span>' +
                  '<span class="mono">' + escText(meta.color || '—') + '</span>' }],
              ['Hidden', meta.hidden ? 'yes' : 'no'],
            ])) +
          '</div>' +
        '</div>';
    }).catch(function () {
      container.innerHTML = '<div class="empty-state">Failed to load identity</div>';
    });
  }

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
                '<details class="ad-session-detail" data-sid="' + escAttr(sid) + '" ' +
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
                  '<div class="ad-session-replay" style="padding:0 14px 14px 14px;' +
                    'background:var(--bg-deepest);">' +
                    '<div class="loading">Click to load…</div>' +
                  '</div>' +
                '</details>'
              );
            }).join('') +
          '</div>' +
        '</div>';

      container.querySelectorAll('.ad-session-detail').forEach(function (row) {
        var loaded = false;
        row.addEventListener('toggle', function () {
          if (!row.open || loaded) return;
          loaded = true;
          var sid = row.getAttribute('data-sid');
          var replay = row.querySelector('.ad-session-replay');
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

  function renderSkills(container, agentId) {
    container.innerHTML = '<div class="loading">Loading skills…</div>';
    api('/api/agents/' + agentId + '/skills').then(function (resp) {
      var skills = (resp && (resp.skills || resp)) || [];
      if (!skills.length) {
        container.innerHTML = '<div class="card"><div class="card-body">' +
          '<div class="empty-state">No skills</div></div></div>';
        return;
      }
      container.innerHTML =
        '<div class="card">' +
          '<div class="card-header">' +
            '<span class="card-title">Skills (' + skills.length + ')</span>' +
            '<span class="text-xs text-dimmed">click a skill to view body</span>' +
          '</div>' +
          '<div class="card-body" style="padding:0;">' +
            skills.map(function (s) {
              var fm = s.frontmatter || {};
              var name = s.name || fm.name || '';
              var desc = fm.description || s.description || '';
              var triggers = fm.triggers
                ? (Array.isArray(fm.triggers)
                    ? fm.triggers.join(', ')
                    : JSON.stringify(fm.triggers))
                : '';
              return (
                '<details class="ad-skill-row" data-path="' + escAttr(s.path || '') + '" ' +
                'style="border-bottom:1px solid var(--border-primary);">' +
                  '<summary style="cursor:pointer;list-style:none;padding:12px 14px;display:flex;gap:14px;align-items:start;">' +
                    '<span style="color:var(--accent);font-size:10px;margin-top:4px;">▸</span>' +
                    '<div style="flex:1;min-width:0;">' +
                      '<div style="display:flex;gap:8px;align-items:center;margin-bottom:4px;">' +
                        '<span class="text-white font-bold">' + escText(name) + '</span>' +
                        (fm.version ? '<span class="tag">v' + escText(fm.version) + '</span>' : '') +
                      '</div>' +
                      (desc ? '<div class="text-secondary" style="font-size:12px;">' + escText(desc) + '</div>' : '<div class="text-dimmed text-xs">(loading description…)</div>') +
                      (triggers ? '<div class="text-muted mono" style="font-size:11px;margin-top:4px;">triggers: ' + escText(triggers) + '</div>' : '') +
                    '</div>' +
                    '<span class="text-dimmed mono" style="font-size:10px;">' + escText(fmtMtime(s.mtime)) + '</span>' +
                  '</summary>' +
                  '<div class="ad-skill-body" style="padding:0 14px 14px 36px;background:var(--bg-deepest);">' +
                    '<div class="loading">Click to load…</div>' +
                  '</div>' +
                '</details>'
              );
            }).join('') +
          '</div>' +
        '</div>';

      // The skill body is inlined in the list response. Render markdown
      // on first open, fall back to a description hint pulled from the
      // body's first non-heading paragraph.
      var skillByPath = {};
      skills.forEach(function (s) { skillByPath[s.path || s.name] = s; });

      container.querySelectorAll('.ad-skill-row').forEach(function (row) {
        var path = row.getAttribute('data-path');
        var summary = row.querySelector('.text-dimmed.text-xs');
        var s = skillByPath[path] || {};
        var content = s.body || '';
        var loaded = false;

        // Description hint from body when frontmatter description was empty
        if (summary && content) {
          var bodyOnly = content.replace(/^---[\s\S]*?---\n/, '').trim();
          var para = '';
          var lines = bodyOnly.split('\n');
          for (var i = 0; i < lines.length; i++) {
            var l = lines[i].trim();
            if (!l) continue;
            if (l.startsWith('#')) continue;
            para = l;
            break;
          }
          if (para && summary.parentNode) {
            var d = document.createElement('div');
            d.className = 'text-secondary';
            d.style.fontSize = '12px';
            d.textContent = para.slice(0, 200) + (para.length > 200 ? '…' : '');
            summary.parentNode.replaceChild(d, summary);
          }
        }

        row.addEventListener('toggle', function () {
          if (!row.open || loaded) return;
          loaded = true;
          var bodyEl = row.querySelector('.ad-skill-body');
          if (!content) {
            bodyEl.innerHTML = '<div class="empty-state">Empty skill</div>';
            return;
          }
          var rendered = window.ARC && window.ARC.renderMarkdown
            ? window.ARC.renderMarkdown(content)
            : '<pre>' + escText(content) + '</pre>';
          bodyEl.innerHTML = '<div class="ft-viewer-md">' + rendered + '</div>';
        });
      });
    });
  }

  function renderTools(container, agentId) {
    container.innerHTML = '<div class="loading">Loading tools…</div>';
    api('/api/agents/' + agentId + '/tools').then(function (resp) {
      resp = resp || {};
      var tools = resp.tools || [];
      var allow = resp.allowlist || [];
      var deny = resp.denylist || [];
      container.innerHTML =
        '<div class="grid-2 mb-20">' +
          '<div class="stat-card">' +
            '<div class="stat-card-label">Registered</div>' +
            '<div class="stat-card-value">' + tools.length + '</div>' +
            '<div class="stat-card-sub">live tool registry</div>' +
          '</div>' +
          '<div class="stat-card">' +
            '<div class="stat-card-label">Policy</div>' +
            '<div class="stat-card-value" style="font-size:18px;">' +
              (deny.length === 0 ? 'allow-all' : 'deny ' + deny.length) +
            '</div>' +
            '<div class="stat-card-sub">' + (allow.length ? allow.length + ' allowed' : 'inferred from registry') + '</div>' +
          '</div>' +
        '</div>' +
        '<div class="card">' +
          '<div class="card-header">' +
            '<span class="card-title">Tool Surface</span>' +
            '<span class="text-xs text-dimmed">click a tool for details</span>' +
          '</div>' +
          '<div class="card-body" style="padding:0;">' +
            renderToolsTable(tools, allow, deny) +
          '</div>' +
        '</div>';
      wireToolDrillDown(container);
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
              ? '<table class="ad-trace-table"><thead><tr>' +
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
                  return '<tr class="ad-trace-row" data-trace-id="' +
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

      // Wire click → global trace drawer (same one Telemetry page uses).
      container.querySelectorAll('.ad-trace-row').forEach(function (row) {
        row.addEventListener('click', function () {
          var tid = row.getAttribute('data-trace-id');
          if (tid && window.ARC && window.ARC.openTraceDrawer) {
            window.ARC.openTraceDrawer(tid);
          }
        });
      });
    });
  }

  function renderPolicyConfigCards(cfg) {
    var policyMod = ((cfg.modules && cfg.modules.policy) || {});
    var pcfg = policyMod.config || {};
    var evalCfg = cfg.eval || {};
    var evalInterval = pcfg.eval_interval_turns;
    var maxBullets = pcfg.max_bullets;
    var maxBulletLen = pcfg.max_bullet_text_length;
    var evalModel = evalCfg.model || '—';

    function box(label, body, sub) {
      return (
        '<div class="card">' +
          '<div class="card-body" style="padding:14px 16px;">' +
            '<div class="text-xs text-dimmed" style="margin-bottom:8px;">' +
              escText(label) +
            '</div>' +
            body +
            (sub ? '<div class="text-xs text-dimmed" style="margin-top:8px;">' +
              escText(sub) + '</div>' : '') +
          '</div>' +
        '</div>'
      );
    }

    var scoringHtml =
      '<div class="text-white" style="font-size:13px;line-height:1.7;">' +
        'New bullet = <span class="mono text-accent">score:5</span><br>' +
        '<span style="color:var(--status-online);">Hit (helps goal): +1</span><br>' +
        '<span style="color:var(--status-error);">Miss (hurts goal): −2</span>' +
      '</div>';

    var triggerHtml =
      '<div class="text-white" style="font-size:13px;line-height:1.7;">' +
        'Every <span class="mono text-accent">' + escText(evalInterval != null ? evalInterval : '—') + '</span> agent turns<br>' +
        'On <span class="mono text-accent">agent:shutdown</span><br>' +
        'On <span class="mono text-accent">policy:force_eval</span>' +
      '</div>';

    var securityHtml =
      '<div class="text-white" style="font-size:13px;line-height:1.7;">' +
        'NFKC normalization<br>' +
        'Zero-width char stripping<br>' +
        'Control char removal' +
      '</div>';

    var evaluatorHtml =
      '<div class="text-white" style="font-size:13px;line-height:1.7;">' +
        '<span class="mono text-accent">' + escText(evalModel) + '</span><br>' +
        'fallback: <span class="mono">' + escText(evalCfg.fallback_behavior || '—') + '</span><br>' +
        'concurrency: <span class="mono">' + escText(evalCfg.max_concurrent != null ? evalCfg.max_concurrent : '—') + '</span>' +
      '</div>';

    return (
      '<div class="grid-4 mb-20">' +
        box('Scoring', scoringHtml, 'Range: 1-10, retired at ≤2') +
        box('Eval Triggers', triggerHtml,
          'Max ' + (maxBullets != null ? maxBullets : '?') + ' bullets per agent') +
        box('Security (ASI-06)', securityHtml,
          'Max ' + (maxBulletLen != null ? maxBulletLen : '?') + ' chars per bullet') +
        box('Evaluator', evaluatorHtml,
          'timeout: ' + (evalCfg.timeout_seconds != null ? evalCfg.timeout_seconds + 's' : '—')) +
      '</div>'
    );
  }

  function renderSystemPolicyRules(cfg) {
    var policy = (cfg.tools && cfg.tools.policy) || {};
    var deny = Array.isArray(policy.deny) ? policy.deny : [];
    var allow = Array.isArray(policy.allow) ? policy.allow : [];
    var timeout = policy.timeout_seconds;

    var rows = [];
    deny.forEach(function (tool, i) {
      rows.push({
        id: 'POL-D' + String(i + 1).padStart(3, '0'),
        desc: 'Tool `' + tool + '` is denied — registry skips on load',
        scope: 'all calls',
        action: 'deny',
        actionClass: 'badge-error',
        score: 10,
        evals: '—',
        status: 'active',
      });
    });
    allow.forEach(function (tool, i) {
      rows.push({
        id: 'POL-A' + String(i + 1).padStart(3, '0'),
        desc: 'Tool `' + tool + '` explicitly allowed',
        scope: 'all calls',
        action: 'allow',
        actionClass: 'badge-online',
        score: 10,
        evals: '—',
        status: 'active',
      });
    });
    if (timeout != null) {
      rows.push({
        id: 'POL-T001',
        desc: 'Tool calls aborted after ' + timeout + 's',
        scope: 'all tools',
        action: 'timeout',
        actionClass: 'badge-warning',
        score: 9,
        evals: '—',
        status: 'active',
      });
    }
    if (allow.length === 0 && deny.length === 0) {
      rows.push({
        id: 'POL-DEFAULT',
        desc: 'No tool policy configured — registry default applies',
        scope: 'all calls',
        action: 'allow-all',
        actionClass: 'badge-neutral',
        score: 5,
        evals: '—',
        status: 'inactive',
      });
    }

    if (!rows.length) return '<div class="empty-state">No policy rules</div>';

    return '<table><thead><tr>' +
      '<th>Rule ID</th><th>Description</th><th>Scope</th><th>Action</th>' +
      '<th>Score</th><th>Evaluations</th><th>Status</th>' +
      '</tr></thead><tbody>' +
      rows.map(function (r) {
        var pct = Math.round(r.score * 10);
        var color = r.score >= 8 ? 'green' : r.score >= 5 ? 'yellow' : 'red';
        var statusBadgeCls = r.status === 'active' ? 'badge-online' : 'badge-neutral';
        return '<tr>' +
          '<td class="mono text-accent">' + escText(r.id) + '</td>' +
          '<td class="text-white">' + escText(r.desc) + '</td>' +
          '<td><span class="badge badge-neutral">' + escText(r.scope) + '</span></td>' +
          '<td><span class="badge ' + r.actionClass + '">' + escText(r.action) + '</span></td>' +
          '<td>' +
            '<div class="score-bar">' +
              '<div class="score-bar-track">' +
                '<div class="score-bar-fill ' + color + '" style="width:' + pct + '%"></div>' +
              '</div>' +
              '<span class="score-value ' + color + '">' + r.score + '</span>' +
            '</div>' +
          '</td>' +
          '<td class="mono">' + escText(r.evals) + '</td>' +
          '<td><span class="badge ' + statusBadgeCls + '">' + escText(r.status) + '</span></td>' +
          '</tr>';
      }).join('') +
      '</tbody></table>';
  }

  function renderPolicy(container, agentId) {
    container.innerHTML = '<div class="loading">Loading policy…</div>';
    Promise.all([
      api('/api/agents/' + agentId + '/policy'),
      api('/api/agents/' + agentId + '/policy/stats'),
      api('/api/agents/' + agentId + '/config'),
    ]).then(function (r) {
      var resp = r[0] || {};
      var stats = r[1] || {};
      var cfg = (r[2] && r[2].config) || {};
      var bullets = resp.bullets || [];
      var raw = resp.raw || '';

      // Score distribution buckets (high/mid/low/retired)
      var dist = { high: 0, mid: 0, low: 0, retired: 0 };
      bullets.forEach(function (b) {
        dist[window.ARC.PolicyBullet.scoreTier(b.score)]++;
      });
      var total = bullets.length || 1;

      container.innerHTML =
        '<div class="grid-4 mb-20">' +
          '<div class="stat-card"><div class="stat-card-label">Total Bullets</div>' +
            '<div class="stat-card-value">' + bullets.length + '</div></div>' +
          '<div class="stat-card"><div class="stat-card-label">High Score</div>' +
            '<div class="stat-card-value" style="color:var(--status-online);">' +
              dist.high + '</div>' +
            '<div class="stat-card-sub">8-10</div></div>' +
          '<div class="stat-card"><div class="stat-card-label">Mid Score</div>' +
            '<div class="stat-card-value" style="color:var(--status-info);">' +
              dist.mid + '</div>' +
            '<div class="stat-card-sub">5-7</div></div>' +
          '<div class="stat-card"><div class="stat-card-label">Retired</div>' +
            '<div class="stat-card-value" style="color:var(--text-dimmed);">' +
              dist.retired + '</div>' +
            '<div class="stat-card-sub">&le; 2</div></div>' +
        '</div>' +
        renderPolicyConfigCards(cfg) +
        '<div class="grid-2 mb-20">' +
          '<div class="card">' +
            '<div class="card-header"><span class="card-title">Score Distribution</span></div>' +
            '<div class="card-body">' + renderScoreDist(dist, total) + '</div>' +
          '</div>' +
          '<div class="card">' +
            '<div class="card-header"><span class="card-title">Top Performers (by uses)</span></div>' +
            '<div class="card-body" style="padding:0;">' + renderTopByUses(bullets) + '</div>' +
          '</div>' +
        '</div>' +
        '<div class="card mb-20">' +
          '<div class="card-header">' +
            '<span class="card-title">System Policy Rules</span>' +
            '<span class="text-xs text-dimmed">From [tools.policy] in arcagent.toml — apply to every call</span>' +
          '</div>' +
          '<div class="card-body" style="padding:0;overflow-x:auto;">' +
            renderSystemPolicyRules(cfg) +
          '</div>' +
        '</div>' +
        '<div class="card">' +
          '<div class="card-header">' +
            '<div>' +
              '<span class="card-title">Active Bullets</span>' +
              '<div class="text-xs text-dimmed" style="margin-top:2px;">Discovered by agent through reflection, scored by effectiveness</div>' +
            '</div>' +
            '<div class="ad-policy-controls flex gap-8">' +
              '<input type="search" class="ad-policy-filter filter-select" placeholder="Filter…">' +
              '<select class="ad-policy-sort filter-select">' +
                '<option value="score">Sort: score↓</option>' +
                '<option value="uses">Sort: uses↓</option>' +
                '<option value="created">Sort: created↓</option>' +
              '</select>' +
              '<label class="text-xs text-muted">' +
                '<input type="checkbox" class="ad-policy-hide-retired"> hide retired</label>' +
              '<button type="button" class="btn btn-ghost btn-sm ad-policy-toggle-raw">Raw</button>' +
            '</div>' +
          '</div>' +
          '<div class="card-body ad-policy-body"></div>' +
        '</div>' +
        '<div class="card mt-16 ad-policy-raw hidden">' +
          '<div class="card-header"><span class="card-title">Raw policy.md</span></div>' +
          '<div class="card-body" style="padding:0;">' +
            '<pre class="crypto-key" style="margin:0;border:0;border-radius:0;font-size:12px;overflow:auto;max-height:400px;padding:12px;">' +
            escText(raw) + '</pre>' +
          '</div>' +
        '</div>';

      var bodyEl = container.querySelector('.ad-policy-body');
      var filterEl = container.querySelector('.ad-policy-filter');
      var sortEl = container.querySelector('.ad-policy-sort');
      var hideRetiredEl = container.querySelector('.ad-policy-hide-retired');
      var rawEl = container.querySelector('.ad-policy-raw');

      function paint() {
        var filtered = window.ARC.PolicyBullet.filterBy(bullets, {
          text: filterEl.value, hideRetired: hideRetiredEl.checked,
        });
        var sorted = window.ARC.PolicyBullet.sortBy(filtered, sortEl.value, 'desc');
        bodyEl.innerHTML = window.ARC.PolicyBullet.renderList(sorted);
      }
      filterEl.addEventListener('input', paint);
      sortEl.addEventListener('change', paint);
      hideRetiredEl.addEventListener('change', paint);
      container.querySelector('.ad-policy-toggle-raw')
        .addEventListener('click', function () { rawEl.classList.toggle('hidden'); });
      paint();
    });
  }

  function renderScoreDist(dist, total) {
    var entries = [
      ['High (8-10)', dist.high, 'var(--status-online)'],
      ['Mid (5-7)', dist.mid, 'var(--status-info)'],
      ['Low (3-4)', dist.low, 'var(--status-warning)'],
      ['Retired (<=2)', dist.retired, 'var(--text-dimmed)'],
    ];
    return entries.map(function (e) {
      var pct = Math.round((e[1] / total) * 100);
      return (
        '<div style="margin-bottom:10px;">' +
          '<div class="flex justify-between text-xs text-muted mb-4">' +
            '<span>' + escText(e[0]) + '</span>' +
            '<span>' + e[1] + ' (' + pct + '%)</span>' +
          '</div>' +
          '<div class="progress-bar" style="height:6px;">' +
            '<div class="fill" style="width:' + pct + '%;background:' + e[2] + ';"></div>' +
          '</div>' +
        '</div>'
      );
    }).join('');
  }

  function renderTopByUses(bullets) {
    var top = bullets.slice().sort(function (a, b) {
      return (b.uses || 0) - (a.uses || 0);
    }).slice(0, 6);
    if (!top.length) return '<div class="empty-state">No data</div>';
    return '<table><thead><tr>' +
      '<th>ID</th><th>Uses</th><th>Score</th>' +
      '</tr></thead><tbody>' +
      top.map(function (b) {
        return '<tr>' +
          '<td class="mono text-accent" title="' + escAttr(b.text || '') + '">' +
            escText(b.id) + '</td>' +
          '<td class="mono">' + (b.uses || 0) + '</td>' +
          '<td class="mono">' + (b.score || 0) + '</td>' +
          '</tr>';
      }).join('') +
      '</tbody></table>';
  }

  function renderMemory(container, agentId) {
    container.innerHTML = '<div class="card"><div class="card-body ad-tree-wrap" style="padding:8px;"></div></div>';
    var wrap = container.querySelector('.ad-tree-wrap');
    return window.ARC.FileTree.mount(wrap, {
      agentId: agentId,
      fetchTree: function () {
        return api('/api/agents/' + agentId + '/files/tree?root=workspace');
      },
      fetchFile: function (opts) {
        return api('/api/agents/' + agentId +
          '/files/read?root=workspace&path=' + encodeURIComponent(opts.path));
      },
    });
  }

  function renderFiles(container, agentId) {
    container.innerHTML = '<div class="card"><div class="card-body ad-tree-wrap" style="padding:8px;"></div></div>';
    var wrap = container.querySelector('.ad-tree-wrap');
    return window.ARC.FileTree.mount(wrap, {
      agentId: agentId,
      fetchTree: function () {
        return api('/api/agents/' + agentId + '/files/tree?root=agent');
      },
      fetchFile: function (opts) {
        return api('/api/agents/' + agentId +
          '/files/read?root=agent&path=' + encodeURIComponent(opts.path));
      },
    });
  }

  var TAB_RENDERERS = {
    overview:  renderOverview,
    identity:  renderIdentity,
    sessions:  renderSessions,
    skills:    renderSkills,
    memory:    renderMemory,
    policy:    renderPolicy,
    tools:     renderTools,
    telemetry: renderTelemetry,
    files:     renderFiles,
  };

  function tabHeader(activeId) {
    return '<div class="tabs ad-tabs">' + TAB_LABELS.map(function (p) {
      var cls = p[0] === activeId ? 'tab pill-nav-item active' : 'tab pill-nav-item';
      return '<div class="' + cls + '" data-tab="' + p[0] + '">' + p[1] + '</div>';
    }).join('') + '</div>';
  }

  function mount(panelEl, agentId) {
    var activeTab = 'overview';
    var currentInstance = null;

    function render() {
      panelEl.innerHTML =
        '<div class="breadcrumb">' +
          '<a href="?page=agents" class="ad-back-link">Dashboard</a>' +
          '<span class="sep">/</span>' +
          '<a href="?page=agents" class="ad-back-link">Agent Fleet</a>' +
          '<span class="sep">/</span>' +
          '<span id="ad-breadcrumb-title">' + escText(agentId) + '</span>' +
        '</div>' +
        '<div class="flex items-center gap-12 mb-20" id="ad-header-block">' +
          '<div class="message-avatar" id="ad-avatar" ' +
            'style="background:#006fff;width:48px;height:48px;font-size:18px;">' +
            escText(initials(agentId)) + '</div>' +
          '<div style="flex:1;min-width:0;">' +
            '<div class="flex items-center gap-12">' +
              '<h1 id="ad-title" style="font-size:22px;font-weight:700;color:var(--text-white);">' +
                escText(agentId) + '</h1>' +
              '<span class="badge badge-neutral" id="ad-status-badge">offline</span>' +
              '<span class="tag" id="ad-type-tag" style="display:none;"></span>' +
            '</div>' +
            '<div class="did" id="ad-did" style="margin-top:4px;display:inline-block;">—</div>' +
          '</div>' +
          '<div class="page-header-actions ad-controls-slot"></div>' +
        '</div>' +
        tabHeader(activeTab) +
        '<div class="tab-panel active ad-body" data-tab="' + escAttr(activeTab) + '"></div>';

      // Populate header from /api/agents/{id}
      api('/api/agents/' + agentId).then(function (meta) {
        if (!meta) return;
        var titleEl = panelEl.querySelector('#ad-title');
        if (titleEl) titleEl.textContent = meta.display_name || meta.name || agentId;
        var bcTitle = panelEl.querySelector('#ad-breadcrumb-title');
        if (bcTitle) bcTitle.textContent = meta.display_name || meta.name || agentId;
        var avatar = panelEl.querySelector('#ad-avatar');
        if (avatar) {
          avatar.textContent = initials(meta.display_name || meta.name || agentId);
          if (meta.color) avatar.style.background = meta.color;
        }
        var did = panelEl.querySelector('#ad-did');
        if (did) did.textContent = meta.did || '—';
        var statusBadge = panelEl.querySelector('#ad-status-badge');
        if (statusBadge) {
          if (meta.online) {
            statusBadge.className = 'badge badge-online';
            statusBadge.textContent = 'online';
          } else {
            statusBadge.className = 'badge badge-neutral';
            statusBadge.textContent = 'offline';
          }
        }
        var typeTag = panelEl.querySelector('#ad-type-tag');
        if (typeTag && (meta.role_label || meta.type)) {
          typeTag.textContent = meta.role_label || meta.type;
          typeTag.style.display = '';
        }
      });

      var controlsSlot = panelEl.querySelector('.ad-controls-slot');
      if (controlsSlot && window.ARC && window.ARC.AgentControls) {
        window.ARC.AgentControls.mount(controlsSlot, agentId);
      }

      activate(activeTab);
    }

    function activate(tabId) {
      if (currentInstance && typeof currentInstance.dispose === 'function') {
        try { currentInstance.dispose(); } catch (e) { /* noop */ }
      }
      currentInstance = null;
      activeTab = tabId;

      panelEl.querySelectorAll('.ad-tabs .tab').forEach(function (el) {
        el.classList.toggle('active', el.dataset.tab === tabId);
      });

      var body = panelEl.querySelector('.ad-body');
      body.dataset.tab = tabId;
      var fn = TAB_RENDERERS[tabId];
      if (!fn) {
        body.innerHTML = '<div class="empty-state">Unknown tab</div>';
        return;
      }
      var ret = fn(body, agentId);
      if (ret && typeof ret.dispose === 'function') currentInstance = ret;
    }

    function onClick(ev) {
      var tab = ev.target.closest('.ad-tabs .tab');
      if (tab && tab.dataset.tab) {
        activate(tab.dataset.tab);
        return;
      }
      var back = ev.target.closest('.ad-back-link');
      if (back) {
        ev.preventDefault();
        window.ARC.setRoute({ page: 'agents' });
      }
    }
    panelEl.addEventListener('click', onClick);
    try {
      render();
    } catch (err) {
      console.error('[AgentDetail] render threw:', err);
      panelEl.innerHTML =
        '<div class="empty-state" style="padding:48px;text-align:center;color:var(--text-muted);">' +
          'Detail render failed: <code>' +
          String(err && err.message || err).replace(/[<>&]/g, function (c) {
            return ({'<':'&lt;','>':'&gt;','&':'&amp;'})[c];
          }) +
        '</code><br>See browser console for the full stack.</div>';
    }

    // Optional render-state diagnostic. Toggle on by setting
    // localStorage.arcui_debug_render = '1' in DevTools — useful when
    // chasing down browser-extension or content-script CSS injection
    // that hides the tabs/body without raising any JS error
    // (incident: Dia browser AI sidebar shipped a content script that
    //  set `.tabs { display: none }` to harvest reading content).
    if (window.localStorage && window.localStorage.getItem('arcui_debug_render') === '1') {
      setTimeout(function () {
        var tabsEl = panelEl.querySelector('.ad-tabs');
        var bodyEl = panelEl.querySelector('.ad-body');
        var info = {
          tabs_display: tabsEl ? getComputedStyle(tabsEl).display : 'n/a',
          tabs_count: tabsEl ? tabsEl.children.length : 0,
          body_display: bodyEl ? getComputedStyle(bodyEl).display : 'n/a',
          body_height: bodyEl ? bodyEl.offsetHeight : 0,
          panel_height: panelEl.offsetHeight,
        };
        var bar = document.createElement('div');
        bar.style.cssText =
          'background:#fbbf24;color:#1f2937;padding:8px 12px;' +
          'font-family:ui-monospace,monospace;font-size:11px;' +
          'border-bottom:2px solid #f59e0b;z-index:9999;';
        bar.textContent = '[arcui_debug_render] ' + JSON.stringify(info);
        panelEl.insertBefore(bar, panelEl.firstChild);
      }, 200);
    }

    function onArcEvent(ev) {
      var msg = ev && ev.detail;
      if (!msg || msg.agent_id !== agentId) return;
      var TAB_EVENT_MAP = {
        overview:  ['config:updated', 'pulse:updated', 'tasks:updated',
                    'schedules:updated', 'traces:updated'],
        sessions:  ['session:changed'],
        memory:    ['memory:updated', 'skills:updated'],
        policy:    ['policy:bullets_updated'],
        telemetry: ['traces:updated'],
        files:     ['config:updated', 'memory:updated', 'skills:updated',
                    'policy:bullets_updated', 'tasks:updated', 'schedules:updated',
                    'pulse:updated', 'session:changed', 'traces:updated'],
      };
      var subscribed = TAB_EVENT_MAP[activeTab];
      if (subscribed && subscribed.indexOf(msg.event_type) >= 0) {
        activate(activeTab);
      }
    }
    window.addEventListener('arc:event', onArcEvent);

    return {
      refresh: function () { activate(activeTab); },
      setTab: function (id) { if (TAB_RENDERERS[id]) activate(id); },
      dispose: function () {
        if (currentInstance && typeof currentInstance.dispose === 'function') {
          try { currentInstance.dispose(); } catch (e) { /* noop */ }
        }
        panelEl.removeEventListener('click', onClick);
        window.removeEventListener('arc:event', onArcEvent);
      },
    };
  }

  window.ARC = window.ARC || {};
  window.ARC.AgentDetail = { mount: mount, TABS: TAB_LABELS };
})();
