/* ============================================================
   ArcUI — Agent Detail Page: Policy tab

   Sibling of agent-detail.js. Owns the Policy tab renderer plus its
   helper builders (config cards, system policy rules table, score
   distribution, top-by-uses).
   IIFE. Exposes ``ARC.AgentDetail._policy.render(container, agentId)``.
   ============================================================ */

(function () {
  'use strict';

  var _S = window.ARC.AgentDetail._shared;
  var escText = _S.escText;
  var escAttr = _S.escAttr;
  var api = _S.api;

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
            '<div class="agd-policy-controls flex gap-8">' +
              '<input type="search" class="agd-policy-filter filter-select" placeholder="Filter…">' +
              '<select class="agd-policy-sort filter-select">' +
                '<option value="score">Sort: score↓</option>' +
                '<option value="uses">Sort: uses↓</option>' +
                '<option value="created">Sort: created↓</option>' +
              '</select>' +
              '<label class="text-xs text-muted">' +
                '<input type="checkbox" class="agd-policy-hide-retired"> hide retired</label>' +
              '<button type="button" class="btn btn-ghost btn-sm agd-policy-toggle-raw">Raw</button>' +
            '</div>' +
          '</div>' +
          '<div class="card-body agd-policy-body"></div>' +
        '</div>' +
        '<div class="card mt-16 agd-policy-raw hidden">' +
          '<div class="card-header"><span class="card-title">Raw policy.md</span></div>' +
          '<div class="card-body" style="padding:0;">' +
            '<pre class="crypto-key" style="margin:0;border:0;border-radius:0;font-size:12px;overflow:auto;max-height:400px;padding:12px;">' +
            escText(raw) + '</pre>' +
          '</div>' +
        '</div>';

      var bodyEl = container.querySelector('.agd-policy-body');
      var filterEl = container.querySelector('.agd-policy-filter');
      var sortEl = container.querySelector('.agd-policy-sort');
      var hideRetiredEl = container.querySelector('.agd-policy-hide-retired');
      var rawEl = container.querySelector('.agd-policy-raw');

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
      container.querySelector('.agd-policy-toggle-raw')
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

  window.ARC.AgentDetail._policy = {
    render: renderPolicy,
  };
})();
