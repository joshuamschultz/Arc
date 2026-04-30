/* ============================================================
   ArcUI — Policy Engine Fleet Page (SPEC-022 §6.4)

   Aggregates ACE policy bullets across all agents. Reuses
   ARC.PolicyBullet (no duplication — one source of truth).

   API:  ARC.PolicyPage.mount(panelEl) -> {refresh, dispose}
   Data: GET /api/team/policy/bullets, GET /api/team/policy/stats
   ============================================================ */

(function () {
  'use strict';

  function escapeText(s) {
    return String(s == null ? '' : s).replace(/[&<>]/g, function (c) {
      return c === '&' ? '&amp;' : c === '<' ? '&lt;' : '&gt;';
    });
  }

  function statBox(label, value) {
    return (
      '<div class="stat-card">' +
        '<div class="stat-card-label">' + escapeText(label) + '</div>' +
        '<div class="stat-card-value">' + escapeText(value) + '</div>' +
      '</div>'
    );
  }

  function renderStats(stats) {
    if (!stats) return '';
    return (
      '<div class="grid-4 mb-20">' +
        statBox('Total Bullets', stats.total || 0) +
        statBox('Active', stats.active || 0) +
        statBox('Retired', stats.retired || 0) +
        statBox('Avg Score', (stats.avg_score || 0).toFixed(1)) +
      '</div>'
    );
  }

  function mount(panelEl) {
    var bullets = [];
    var stats = null;
    var filterText = '';
    var minScore = null;
    var sortKey = 'score';
    var hideRetired = false;

    function paint() {
      var filtered = window.ARC.PolicyBullet.filterBy(bullets, {
        text: filterText,
        minScore: minScore,
        hideRetired: hideRetired,
      });
      var sorted = window.ARC.PolicyBullet.sortBy(filtered, sortKey, 'desc');
      var bodyEl = panelEl.querySelector('.pp-list');
      if (bodyEl) bodyEl.innerHTML = window.ARC.PolicyBullet.renderList(sorted);
    }

    function load() {
      panelEl.innerHTML = '<div class="loading">Loading policy bullets…</div>';
      Promise.all([
        Promise.resolve(window.fetchAPI('/api/team/policy/bullets')),
        Promise.resolve(window.fetchAPI('/api/team/policy/stats')),
      ]).then(function (results) {
        bullets = (results[0] && (results[0].bullets || results[0])) || [];
        stats = results[1] || null;
        panelEl.innerHTML =
          '<div class="page-header"><h1>Policy Engine</h1></div>' +
          renderStats(stats) +
          '<div class="card">' +
            '<div class="card-header">' +
              '<span class="card-title">All Bullets</span>' +
              '<div class="pp-controls">' +
                '<input type="search" class="pp-filter" placeholder="Filter…">' +
                '<select class="pp-sort">' +
                  '<option value="score">score↓</option>' +
                  '<option value="uses">uses↓</option>' +
                  '<option value="created">created↓</option>' +
                '</select>' +
                '<label class="pp-toggle"><input type="checkbox" class="pp-hide-retired"> Hide retired</label>' +
              '</div>' +
            '</div>' +
            '<div class="card-body pp-list"></div>' +
          '</div>';

        panelEl.querySelector('.pp-filter').addEventListener('input', function (e) {
          filterText = e.target.value;
          paint();
        });
        panelEl.querySelector('.pp-sort').addEventListener('change', function (e) {
          sortKey = e.target.value;
          paint();
        });
        panelEl.querySelector('.pp-hide-retired').addEventListener('change', function (e) {
          hideRetired = e.target.checked;
          paint();
        });
        paint();
      }).catch(function () {
        panelEl.innerHTML = '<div class="empty-state">Failed to load policy</div>';
      });
    }

    load();

    function onArcEvent(ev) {
      var msg = ev && ev.detail;
      if (msg && msg.event_type === 'policy:bullets_updated') load();
    }
    window.addEventListener('arc:event', onArcEvent);

    return {
      refresh: load,
      dispose: function () { window.removeEventListener('arc:event', onArcEvent); },
    };
  }

  window.ARC = window.ARC || {};
  window.ARC.PolicyPage = { mount: mount };
})();
