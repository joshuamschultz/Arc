/* ============================================================
   ArcUI — Agent Fleet Page (SPEC-022 §5.1, demo-aligned)

   Mirrors demo/agents.html: breadcrumb, page header, 4-stat
   summary, agent cards with avatar + DID + status dot + tags +
   3-col stat block + sparkline placeholder.
   ============================================================ */

(function () {
  'use strict';

  function escText(s) {
    return String(s == null ? '' : s).replace(/[&<>]/g, function (c) {
      return c === '&' ? '&amp;' : c === '<' ? '&lt;' : '&gt;';
    });
  }

  function statCard(label, value, valueColor) {
    var color = valueColor ? ' style="color:' + valueColor + '"' : '';
    return (
      '<div class="stat-card">' +
        '<div class="stat-card-label">' + escText(label) + '</div>' +
        '<div class="stat-card-value"' + color + '>' + escText(value) + '</div>' +
      '</div>'
    );
  }

  function avatar(a) {
    var initials = (a.display_name || a.name || '?')
      .split(/\W+/).filter(Boolean).slice(0, 2)
      .map(function (w) { return w[0].toUpperCase(); }).join('') || '?';
    return (
      '<div class="message-avatar" ' +
      'style="background:' + escText(a.color || '#006fff') +
      ';width:40px;height:40px;font-size:14px;">' + escText(initials) + '</div>'
    );
  }

  function statusDotClass(a) {
    if (a.online) return 'online';
    return 'idle';
  }

  function modelShort(m) {
    if (!m) return '';
    var idx = m.indexOf('/');
    return idx >= 0 ? m.slice(idx + 1) : m;
  }

  function agentCard(a, metrics) {
    var m = metrics || {};
    return (
      '<div class="card" data-agent="' + escText(a.agent_id) + '" ' +
      'style="cursor:pointer;">' +
        '<div class="card-body">' +
          '<div class="flex items-center gap-12 mb-12">' +
            avatar(a) +
            '<div style="flex:1;min-width:0;">' +
              '<div class="text-white font-bold" style="font-size:15px;">' +
                escText(a.display_name || a.name) +
              '</div>' +
              '<div class="did" style="font-size:10px;" title="' + escText(a.did) + '">' +
                escText(a.did || '—') +
              '</div>' +
            '</div>' +
            '<div style="margin-left:auto;">' +
              '<span class="status-dot ' + statusDotClass(a) + '"></span>' +
            '</div>' +
          '</div>' +
          '<div class="flex gap-8 mb-8">' +
            '<span class="tag">' + escText(a.role_label || a.type || 'agent') + '</span>' +
            (a.model ? '<span class="tag">' + escText(modelShort(a.model)) + '</span>' : '') +
          '</div>' +
          '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:12px;">' +
            kv3('Sessions', m.sessions != null ? m.sessions : '—') +
            kv3('Schedules', m.schedules != null ? m.schedules : '—') +
            kv3('Bullets',   m.bullets   != null ? m.bullets   : '—') +
          '</div>' +
          '<div style="margin-top:12px;">' +
            '<div class="flex justify-between text-xs text-dimmed mb-4">' +
              '<span>Trace activity</span>' +
              '<span>' + escText(m.traces != null ? m.traces : '—') + '</span>' +
            '</div>' +
            '<div class="sparkline" data-spark="' + escText(a.agent_id) + '" ' +
            'style="height:24px;background:var(--bg-deepest);border-radius:var(--radius-sm);"></div>' +
          '</div>' +
        '</div>' +
      '</div>'
    );
  }

  function kv3(label, value) {
    return (
      '<div>' +
        '<div class="text-xs text-dimmed">' + escText(label) + '</div>' +
        '<div class="text-sm text-white font-bold">' + escText(value) + '</div>' +
      '</div>'
    );
  }

  function fetchAgentMetrics(agentId) {
    var get = function (p) { return Promise.resolve(window.fetchAPI(p)); };
    return Promise.all([
      get('/api/agents/' + agentId + '/sessions'),
      get('/api/agents/' + agentId + '/schedules'),
      get('/api/agents/' + agentId + '/policy/bullets'),
      get('/api/agents/' + agentId + '/traces?limit=10'),
    ]).then(function (r) {
      return {
        sessions:  ((r[0] && r[0].sessions)  || []).length,
        schedules: ((r[1] && r[1].schedules) || []).length,
        bullets:   ((r[2] && r[2].bullets)   || []).length,
        traces:    ((r[3] && r[3].traces)    || []).length,
      };
    }).catch(function () { return {}; });
  }

  function render(panelEl, agents, filter, metrics) {
    var visible = agents.filter(function (a) { return !a.hidden; });
    if (filter === 'online')  visible = visible.filter(function (a) { return a.online; });
    if (filter === 'offline') visible = visible.filter(function (a) { return !a.online; });

    var totalAll = agents.filter(function (a) { return !a.hidden; }).length;
    var liveCt   = agents.filter(function (a) { return a.online && !a.hidden; }).length;
    var offlineCt = totalAll - liveCt;

    panelEl.innerHTML =
      '<div class="breadcrumb">' +
        '<span>Dashboard</span><span class="sep">/</span>' +
        '<span>Agent Fleet</span>' +
      '</div>' +
      '<div class="page-header">' +
        '<h1>Agent Fleet</h1>' +
        '<div class="page-header-actions">' +
          '<div class="flex gap-8" id="ap-filters">' +
            '<button class="btn btn-secondary btn-sm ' +
              (filter === 'all' ? 'active' : '') + '" data-filter="all">All</button>' +
            '<button class="btn btn-secondary btn-sm ' +
              (filter === 'online' ? 'active' : '') + '" data-filter="online">Live</button>' +
            '<button class="btn btn-secondary btn-sm ' +
              (filter === 'offline' ? 'active' : '') + '" data-filter="offline">Offline</button>' +
          '</div>' +
        '</div>' +
      '</div>' +
      '<div class="grid-4 mb-20">' +
        statCard('Total', totalAll) +
        statCard('Live', liveCt, 'var(--status-online)') +
        statCard('Offline', offlineCt, 'var(--status-idle)') +
        statCard('Hidden', agents.length - totalAll) +
      '</div>' +
      '<div class="grid-3 mb-20" id="agent-grid">' +
        (visible.length
          ? visible.map(function (a) { return agentCard(a, metrics[a.agent_id]); }).join('')
          : '<div class="card"><div class="card-body"><div class="empty-state">No agents</div></div></div>') +
      '</div>';

    // Render sparkline placeholder bars
    panelEl.querySelectorAll('.sparkline').forEach(function (el) {
      var id = el.getAttribute('data-spark');
      var n = (metrics[id] && metrics[id].traces) || 0;
      var bars = '';
      for (var i = 0; i < 20; i++) {
        var h = n > 0 ? (4 + Math.random() * 16) : 2;
        var op = n > 0 ? '0.6' : '0.15';
        bars += '<div style="display:inline-block;width:3px;margin-right:1px;height:' +
          h.toFixed(1) + 'px;background:var(--accent);opacity:' + op +
          ';vertical-align:bottom;border-radius:1px;"></div>';
      }
      el.innerHTML = '<div style="padding:4px 6px;line-height:0;">' + bars + '</div>';
    });
  }

  function mount(panelEl) {
    var agents = [];
    var filter = 'all';
    var metrics = {};

    function load() {
      panelEl.innerHTML = '<div class="loading">Loading roster…</div>';
      return Promise.resolve(window.fetchAPI('/api/team/roster')).then(function (data) {
        agents = (data && data.agents) || [];
        // Render shell first so the user sees something fast
        render(panelEl, agents, filter, metrics);
        // Fetch per-agent metrics in parallel and re-render once
        return Promise.all(
          agents.filter(function (a) { return !a.hidden; })
                .map(function (a) {
            return fetchAgentMetrics(a.agent_id).then(function (m) {
              metrics[a.agent_id] = m;
            });
          })
        ).then(function () { render(panelEl, agents, filter, metrics); });
      }).catch(function () {
        panelEl.innerHTML = '<div class="empty-state">Failed to load roster</div>';
      });
    }

    function onClick(ev) {
      var btn = ev.target.closest('.btn[data-filter]');
      if (btn) {
        filter = btn.dataset.filter;
        render(panelEl, agents, filter, metrics);
        return;
      }
      var card = ev.target.closest('[data-agent]');
      if (card) {
        ev.preventDefault();
        window.ARC.setRoute({ page: 'agent-detail', agent: card.dataset.agent });
      }
    }

    panelEl.addEventListener('click', onClick);
    load();

    function onArcEvent(ev) {
      var msg = ev && ev.detail;
      if (!msg) return;
      var t = msg.event_type || msg.type;
      if (t === 'agent:online' || t === 'agent:offline' || t === 'roster:changed') {
        load();
      }
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
  window.ARC.AgentsPage = { mount: mount };
})();
