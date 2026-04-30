/* ============================================================
   ArcUI — Shell Renderer (Sidebar + Topbar) + URL Router
   Adapted from SPEC-022 §5.1–5.2 for the multi-page SPA.
   ============================================================ */

const ICONS = {
  agents: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/></svg>',
  agentDetail: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="8" r="4"/><path d="M4 21v-1a8 8 0 0116 0v1"/></svg>',
  telemetry: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>',
  security: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
  tools: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z"/></svg>',
  tasks: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/></svg>',
  policy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/></svg>',
  settings: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-2 2 2 2 0 01-2-2v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83 0 2 2 0 010-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 01-2-2 2 2 0 012-2h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 010-2.83 2 2 0 012.83 0l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 012-2 2 2 0 012 2v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 0 2 2 0 010 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 012 2 2 2 0 01-2 2h-.09a1.65 1.65 0 00-1.51 1z"/></svg>',
  bell: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 01-3.46 0"/></svg>',
  arc: '<svg viewBox="0 0 24 24" fill="none" stroke="#006fff" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>'
};

// SPEC-022 §5.1 PAGES list. `hidden: true` items don't render in the
// sidebar but are valid route targets (navigated into via deep link).
const PAGES = [
  { id: 'agents',         label: 'Agent Fleet',     icon: 'agents' },
  { id: 'agent-detail',   label: 'Agent Detail',    icon: 'agentDetail', hidden: true },
  { divider: true },
  { id: 'telemetry',      label: 'LLM Telemetry',   icon: 'telemetry' },
  { id: 'security',       label: 'Security & Audit', icon: 'security' },
  { divider: true },
  { id: 'tools-skills',   label: 'Tools & Skills',  icon: 'tools' },
  { id: 'tasks',          label: 'Tasks',           icon: 'tasks' },
  { id: 'policy',         label: 'Policy Engine',   icon: 'policy' },
  { id: 'settings',       label: 'Settings',        icon: 'settings' },
];

function renderShell() {
  const shell = document.querySelector('.app-shell');
  if (!shell) return;

  const sidebar = shell.querySelector('.sidebar');
  if (sidebar) {
    const items = PAGES.map(p => {
      if (p.divider) return '<div class="sidebar-divider"></div>';
      if (p.hidden) return '';
      return `<a class="sidebar-item" data-page="${p.id}" title="${p.label}">${ICONS[p.icon] || ''}<span class="sidebar-tooltip">${p.label}</span></a>`;
    }).join('');
    sidebar.innerHTML = `<div class="sidebar-group">${items}</div>`;
  }

  const topbar = shell.querySelector('.topbar');
  if (topbar) {
    topbar.innerHTML = `
      <div class="topbar-logo">${ICONS.arc}</div>
      <div class="topbar-title">ARC Platform</div>
      <div class="topbar-subtitle"></div>
      <div class="topbar-center"></div>
      <div class="topbar-right">
        <span class="live-dot" id="live-indicator">LIVE</span>
        <div class="topbar-badge">${ICONS.bell}<span class="badge-count">0</span></div>
      </div>`;
  }
}

function renderChartBars(container, count = 30, maxH = 180) {
  if (!container) return;
  let html = '';
  for (let i = 0; i < count; i++) {
    const h = 8 + Math.random() * (maxH - 8);
    const cls = Math.random() > 0.7 ? ' secondary' : '';
    html += `<div class="chart-bar${cls}" style="height:${h}px"></div>`;
  }
  container.innerHTML = html;
}

/* ---------- URL Router (SPEC-022 §5.2) ----------
   Single source of truth: ?page=<id>&agent=<id>. history.pushState for
   navigation; popstate hook re-applies on back/forward. The router is
   intentionally tiny: it doesn't own page lifecycle (each page mounts
   itself when applyRoute calls into it).
*/

const _routeListeners = [];

function readRoute() {
  const params = new URLSearchParams(window.location.search);
  const page = params.get('page') || 'agents';
  const agent = params.get('agent') || null;
  return { page, agent };
}

function setRoute({ page, agent } = {}) {
  const params = new URLSearchParams();
  if (page) params.set('page', page);
  if (agent) params.set('agent', agent);
  const qs = params.toString();
  const url = window.location.pathname + (qs ? `?${qs}` : '');
  window.history.pushState(null, '', url);
  applyRoute();
}

function onRouteChange(fn) {
  if (typeof fn === 'function') _routeListeners.push(fn);
}

function applyRoute() {
  const route = readRoute();

  // Toggle page panels.
  document.querySelectorAll('[data-page-content]').forEach(el => {
    el.classList.toggle('hidden', el.dataset.pageContent !== route.page);
  });

  // Sync sidebar active state. Hidden routes (agent-detail) don't have a
  // sidebar item, so we leave the previously-active fleet entry highlighted.
  const sidebar = document.querySelector('.sidebar');
  if (sidebar) {
    sidebar.querySelectorAll('.sidebar-item').forEach(el => {
      const matches = el.dataset.page === route.page ||
        (route.page === 'agent-detail' && el.dataset.page === 'agents');
      el.classList.toggle('active', matches);
    });
  }

  for (const fn of _routeListeners) {
    try { fn(route); } catch (e) { /* listener errors must not break navigation */ }
  }
}

function initSidebarNav() {
  const sidebar = document.querySelector('.sidebar');
  if (!sidebar) return;
  sidebar.addEventListener('click', (e) => {
    const item = e.target.closest('.sidebar-item');
    if (!item || !item.dataset.page) return;
    setRoute({ page: item.dataset.page });
  });
}

function initRouter() {
  window.addEventListener('popstate', applyRoute);
  applyRoute();
}

window.ARC = {
  ICONS, PAGES,
  renderShell, renderChartBars, initSidebarNav,
  readRoute, setRoute, applyRoute, onRouteChange, initRouter,
};
