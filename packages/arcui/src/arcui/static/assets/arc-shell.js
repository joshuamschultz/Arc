/* ============================================================
   ArcUI — Shell Renderer (Sidebar + Topbar)
   Adapted for standalone telemetry dashboard.
   ============================================================ */

const ICONS = {
  telemetry: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>',
  settings: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-2 2 2 2 0 01-2-2v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83 0 2 2 0 010-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 01-2-2 2 2 0 012-2h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 010-2.83 2 2 0 012.83 0l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 012-2 2 2 0 012 2v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 0 2 2 0 010 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 012 2 2 2 0 01-2 2h-.09a1.65 1.65 0 00-1.51 1z"/></svg>',
  bell: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 01-3.46 0"/></svg>',
  arc: '<svg viewBox="0 0 24 24" fill="none" stroke="#006fff" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>'
};

const PAGES = [
  { id: 'telemetry', label: 'LLM Telemetry', icon: 'telemetry', active: true },
  { divider: true },
  { id: 'settings', label: 'Settings', icon: 'settings', disabled: true },
];

function renderShell() {
  const shell = document.querySelector('.app-shell');
  if (!shell) return;

  // Sidebar
  const sidebar = shell.querySelector('.sidebar');
  if (sidebar) {
    const items = PAGES.map(p => {
      if (p.divider) return '<div class="sidebar-divider"></div>';
      const cls = p.active ? ' active' : (p.disabled ? '' : '');
      const opacity = p.disabled ? ' style="opacity:0.3;pointer-events:none;"' : '';
      return `<a class="sidebar-item${cls}"${opacity} title="${p.label}">${ICONS[p.icon]}<span class="sidebar-tooltip">${p.label}</span></a>`;
    }).join('');
    sidebar.innerHTML = `<div class="sidebar-group">${items}</div>`;
  }

  // Topbar
  const topbar = shell.querySelector('.topbar');
  if (topbar) {
    topbar.innerHTML = `
      <div class="topbar-logo">${ICONS.arc}</div>
      <div class="topbar-title">ARC Platform</div>
      <div class="topbar-subtitle">LLM Telemetry</div>
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

window.ARC = { ICONS, PAGES, renderShell, renderChartBars };
