/* ============================================================
   ArcUI — Agent Detail Page (SPEC-022, demo-aligned) — slim entry

   Mirrors demo/agent-detail.html DOM:
     - breadcrumb · message-avatar · h1 · badges · DID strip
     - Pause / Restart / Deploy in page-header-actions
     - <div class="tabs"><div class="tab" data-tab="...">...
     - kv-grid for identity/config; <table> for sessions/tools/modules

   API: ARC.AgentDetail.mount(panelEl, agentId) -> {dispose, refresh, setTab}

   This file is the public entry. The tab renderers were lifted into
   sibling IIFE assets (loaded BEFORE this file in index.html):

     - agent-detail-shared.js     — helpers + TAB_LABELS + tabHeader
     - agent-detail-timeline.js   — Overview tab + tools/sessions builders
     - agent-detail-modules.js    — Skills + Tools tabs
     - agent-detail-files.js      — Memory + Files tabs (FileTree wrappers)
     - agent-detail-audit.js      — Sessions + Telemetry tabs
     - agent-detail-policy.js     — Policy tab + helpers

   Each sibling registers on ``ARC.AgentDetail._<name>``; this entry
   wires the dispatch table.

   Loading mechanism MUST stay IIFE — no `type="module"`, no bundler.
   Match the existing IIFE-script pattern used by sibling assets
   (arc-shell.js, formatters.js, dom-batcher.js, file-tree.js, etc.).
   ============================================================ */

(function () {
  'use strict';

  var _S = window.ARC.AgentDetail._shared;
  var TAB_LABELS = _S.TAB_LABELS;
  var escText = _S.escText;
  var escAttr = _S.escAttr;
  var api = _S.api;
  var initials = _S.initials;
  var card = _S.card;
  var tabHeader = _S.tabHeader;

  // ============================================================
  // Identity tab — small enough to keep in the entry module.
  // ============================================================

  function renderIdentity(container, agentId) {
    var renderKvGrid = window.ARC.AgentDetail._timeline.renderKvGrid;
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

  // ============================================================
  // Dispatch table — each tab maps to one sibling's renderer.
  // ============================================================

  function tabRenderers() {
    var t = window.ARC.AgentDetail._timeline;
    var m = window.ARC.AgentDetail._modules;
    var f = window.ARC.AgentDetail._files;
    var a = window.ARC.AgentDetail._audit;
    var p = window.ARC.AgentDetail._policy;
    return {
      overview:  t.render,
      identity:  renderIdentity,
      sessions:  a.renderSessions,
      skills:    m.renderSkills,
      memory:    f.renderMemory,
      policy:    p.render,
      tools:     m.renderTools,
      telemetry: a.renderTelemetry,
      files:     f.renderFiles,
    };
  }

  function mount(panelEl, agentId) {
    var TAB_RENDERERS = tabRenderers();
    var activeTab = 'overview';
    var currentInstance = null;

    function render() {
      panelEl.innerHTML =
        '<div class="breadcrumb">' +
          '<a href="?page=agents" class="agd-back-link">Dashboard</a>' +
          '<span class="sep">/</span>' +
          '<a href="?page=agents" class="agd-back-link">Agent Fleet</a>' +
          '<span class="sep">/</span>' +
          '<span id="agd-breadcrumb-title">' + escText(agentId) + '</span>' +
        '</div>' +
        '<div class="flex items-center gap-12 mb-20" id="agd-header-block">' +
          '<div class="message-avatar" id="agd-avatar" ' +
            'style="background:#006fff;width:48px;height:48px;font-size:18px;">' +
            escText(initials(agentId)) + '</div>' +
          '<div style="flex:1;min-width:0;">' +
            '<div class="flex items-center gap-12">' +
              '<h1 id="agd-title" style="font-size:22px;font-weight:700;color:var(--text-white);">' +
                escText(agentId) + '</h1>' +
              '<span class="badge badge-neutral" id="agd-status-badge">offline</span>' +
              '<span class="tag" id="agd-type-tag" style="display:none;"></span>' +
            '</div>' +
            '<div class="did" id="agd-did" style="margin-top:4px;display:inline-block;">—</div>' +
          '</div>' +
          '<div class="page-header-actions agd-controls-slot"></div>' +
        '</div>' +
        tabHeader(activeTab) +
        '<div class="tab-panel active agd-body" data-tab="' + escAttr(activeTab) + '"></div>';

      api('/api/agents/' + agentId).then(function (meta) {
        if (!meta) return;
        var titleEl = panelEl.querySelector('#agd-title');
        if (titleEl) titleEl.textContent = meta.display_name || meta.name || agentId;
        var bcTitle = panelEl.querySelector('#agd-breadcrumb-title');
        if (bcTitle) bcTitle.textContent = meta.display_name || meta.name || agentId;
        var avatar = panelEl.querySelector('#agd-avatar');
        if (avatar) {
          avatar.textContent = initials(meta.display_name || meta.name || agentId);
          if (meta.color) avatar.style.background = meta.color;
        }
        var did = panelEl.querySelector('#agd-did');
        if (did) did.textContent = meta.did || '—';
        var statusBadge = panelEl.querySelector('#agd-status-badge');
        if (statusBadge) {
          if (meta.online) {
            statusBadge.className = 'badge badge-online';
            statusBadge.textContent = 'online';
          } else {
            statusBadge.className = 'badge badge-neutral';
            statusBadge.textContent = 'offline';
          }
        }
        var typeTag = panelEl.querySelector('#agd-type-tag');
        if (typeTag && (meta.role_label || meta.type)) {
          typeTag.textContent = meta.role_label || meta.type;
          typeTag.style.display = '';
        }
      });

      var controlsSlot = panelEl.querySelector('.agd-controls-slot');
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

      panelEl.querySelectorAll('.agd-tabs .tab').forEach(function (el) {
        el.classList.toggle('active', el.dataset.tab === tabId);
      });

      var body = panelEl.querySelector('.agd-body');
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
      var tab = ev.target.closest('.agd-tabs .tab');
      if (tab && tab.dataset.tab) {
        activate(tab.dataset.tab);
        return;
      }
      var back = ev.target.closest('.agd-back-link');
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
        var tabsEl = panelEl.querySelector('.agd-tabs');
        var bodyEl = panelEl.querySelector('.agd-body');
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
  window.ARC.AgentDetail = window.ARC.AgentDetail || {};
  window.ARC.AgentDetail.mount = mount;
  window.ARC.AgentDetail.TABS = TAB_LABELS;
})();
