/* ============================================================
   ArcUI — Tools & Skills Fleet Page (SPEC-022 §6.2)

   Two sections:
     - Tools matrix (rows: tool name; cols: agents that have it registered)
     - Skills directory (rows: parsed-frontmatter cards across all agents)

   API:  ARC.ToolsSkillsPage.mount(panelEl) -> {refresh, dispose}
   Data: GET /api/team/tools-skills
   ============================================================ */

(function () {
  'use strict';

  function escapeText(s) {
    return String(s == null ? '' : s).replace(/[&<>]/g, function (c) {
      return c === '&' ? '&amp;' : c === '<' ? '&lt;' : '&gt;';
    });
  }

  function renderToolsMatrix(tools, agents) {
    if (!tools.length) return '<div class="empty-state">No tools registered</div>';
    return (
      '<table class="data-table tsp-matrix">' +
        '<thead><tr><th>Tool</th>' +
          agents.map(function (a) { return '<th>' + escapeText(a) + '</th>'; }).join('') +
        '</tr></thead>' +
        '<tbody>' +
          tools.map(function (t) {
            return '<tr><td class="mono">' + escapeText(t.name) + '</td>' +
              agents.map(function (a) {
                return '<td>' + (t.agents && t.agents.indexOf(a) >= 0 ? '●' : '·') + '</td>';
              }).join('') +
              '</tr>';
          }).join('') +
        '</tbody>' +
      '</table>'
    );
  }

  function renderSkillsDirectory(skills) {
    if (!skills.length) return '<div class="empty-state">No skills</div>';
    return '<div class="tsp-skill-list">' + skills.map(function (s) {
      var fm = s.frontmatter || {};
      return (
        '<div class="card mb-12">' +
          '<div class="card-header">' +
            '<span class="card-title">' + escapeText(s.name || fm.name || '') + '</span>' +
            (s.agent_id ? '<span class="muted">' + escapeText(s.agent_id) + '</span>' : '') +
          '</div>' +
          '<div class="card-body">' +
            '<div class="muted">' + escapeText(fm.description || s.description || '') + '</div>' +
          '</div>' +
        '</div>'
      );
    }).join('') + '</div>';
  }

  function render(panelEl, data) {
    var tools = (data && data.tools) || [];
    var skills = (data && data.skills) || [];
    var agents = (data && data.agents) || [];
    panelEl.innerHTML =
      '<div class="page-header"><h1>Tools &amp; Skills</h1></div>' +
      '<div class="card mb-20">' +
        '<div class="card-header"><span class="card-title">Tools Matrix</span></div>' +
        '<div class="card-body">' + renderToolsMatrix(tools, agents) + '</div>' +
      '</div>' +
      '<div class="card">' +
        '<div class="card-header"><span class="card-title">Skills Directory</span></div>' +
        '<div class="card-body">' + renderSkillsDirectory(skills) + '</div>' +
      '</div>';
  }

  function mount(panelEl) {
    function load() {
      panelEl.innerHTML = '<div class="loading">Loading tools &amp; skills…</div>';
      return Promise.resolve(window.fetchAPI('/api/team/tools-skills')).then(function (data) {
        render(panelEl, data || {});
      }).catch(function () {
        panelEl.innerHTML = '<div class="empty-state">Failed to load</div>';
      });
    }

    load();

    function onArcEvent(ev) {
      var msg = ev && ev.detail;
      if (msg && (msg.event_type === 'skills:updated' || msg.event_type === 'config:updated')) {
        load();
      }
    }
    window.addEventListener('arc:event', onArcEvent);

    return {
      refresh: load,
      dispose: function () { window.removeEventListener('arc:event', onArcEvent); },
    };
  }

  window.ARC = window.ARC || {};
  window.ARC.ToolsSkillsPage = { mount: mount };
})();
