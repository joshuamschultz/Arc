/* ============================================================
   ArcUI — Agent Detail Page: Skills + Tools tabs

   Sibling of agent-detail.js. Owns the Skills and Tools renderers.
   IIFE. Exposes ``ARC.AgentDetail._modules.{renderSkills,renderTools}``.
   ============================================================ */

(function () {
  'use strict';

  var _S = window.ARC.AgentDetail._shared;
  var escText = _S.escText;
  var escAttr = _S.escAttr;
  var api = _S.api;
  var fmtMtime = _S.fmtMtime;
  var wireToolDrillDown = _S.wireToolDrillDown;

  // renderToolsTable lives in the timeline module since the Overview
  // tab also uses it. Pull from there.
  function renderToolsTable(tools, allow, deny) {
    return window.ARC.AgentDetail._timeline.renderToolsTable(tools, allow, deny);
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
                '<details class="agd-skill-row" data-path="' + escAttr(s.path || '') + '" ' +
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
                  '<div class="agd-skill-body" style="padding:0 14px 14px 36px;background:var(--bg-deepest);">' +
                    '<div class="loading">Click to load…</div>' +
                  '</div>' +
                '</details>'
              );
            }).join('') +
          '</div>' +
        '</div>';

      var skillByPath = {};
      skills.forEach(function (s) { skillByPath[s.path || s.name] = s; });

      container.querySelectorAll('.agd-skill-row').forEach(function (row) {
        var path = row.getAttribute('data-path');
        var summary = row.querySelector('.text-dimmed.text-xs');
        var s = skillByPath[path] || {};
        var content = s.body || '';
        var loaded = false;

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
          var bodyEl = row.querySelector('.agd-skill-body');
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

  window.ARC.AgentDetail._modules = {
    renderSkills: renderSkills,
    renderTools: renderTools,
  };
})();
