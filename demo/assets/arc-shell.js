/* ============================================================
   ARC Platform — Shared Shell (Sidebar + Topbar + Navigation)
   ============================================================ */

// Manufacturing scenario agent data
const AGENTS = {
  "procurement-01": {
    name: "PROC-Alpha",
    role: "procurement",
    did: "did:arc:cascade:procurement/7f3a9c1e",
    pubkey: "ed25519:Gk3Qx8vNpL2mRjYwS5tFbH6cD9eAz4uXoP1iW7kE0nM",
    status: "online",
    model: "anthropic/claude-sonnet-4-20250514",
    color: "#006fff",
    avatar: "PA"
  },
  "inventory-01": {
    name: "INV-Bravo",
    role: "inventory",
    did: "did:arc:cascade:inventory/2d8b4f6a",
    pubkey: "ed25519:Hm4Ry9wOpM3nSkZxT6uGcI7dE0fBa5vXqQ2jW8lF1oN",
    status: "online",
    model: "anthropic/claude-sonnet-4-20250514",
    color: "#00c853",
    avatar: "IB"
  },
  "quality-01": {
    name: "QC-Charlie",
    role: "quality_control",
    did: "did:arc:cascade:quality/9e1c5d3b",
    pubkey: "ed25519:Jn5Sz0xQqN4oTlAyU7vHdJ8eF1gCb6wYrR3kX9mG2pO",
    status: "online",
    model: "openai/gpt-4o",
    color: "#ff9800",
    avatar: "QC"
  },
  "logistics-01": {
    name: "LOG-Delta",
    role: "logistics",
    did: "did:arc:cascade:logistics/4a6e8f2c",
    pubkey: "ed25519:Kp6Ta1yRrO5pUmBzV8wIeK9fG2hDc7xZsS4lY0nH3qP",
    status: "online",
    model: "anthropic/claude-sonnet-4-20250514",
    color: "#e040fb",
    avatar: "LD"
  },
  "scheduler-01": {
    name: "SCHED-Echo",
    role: "scheduler",
    did: "did:arc:cascade:scheduler/6c0g2h4d",
    pubkey: "ed25519:Lq7Ub2zSsP6qVnCaW9xJfL0gH3iEd8yAtT5mZ1oI4rQ",
    status: "idle",
    model: "ollama/llama3.1:70b",
    color: "#29b6f6",
    avatar: "SE"
  },
  "compliance-01": {
    name: "COMP-Foxtrot",
    role: "compliance",
    did: "did:arc:cascade:compliance/8e2i4j6f",
    pubkey: "ed25519:Mr8Vc3aTtQ7rWoDbX0yKgM1hI4jFe9zBuU6nA2pJ5sR",
    status: "online",
    model: "anthropic/claude-sonnet-4-20250514",
    color: "#f44336",
    avatar: "CF"
  }
};

// SVG icons (inline, no external deps)
const ICONS = {
  dashboard: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>',
  agents: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 4-7 8-7s8 3 8 7"/></svg>',
  agentDetail: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 4-7 8-7s8 3 8 7"/><line x1="18" y1="8" x2="22" y2="8"/><line x1="20" y1="6" x2="20" y2="10"/></svg>',
  messages: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 11.5a8.38 8.38 0 01-.9 3.8 8.5 8.5 0 01-7.6 4.7 8.38 8.38 0 01-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 01-.9-3.8 8.5 8.5 0 014.7-7.6 8.38 8.38 0 013.8-.9h.5a8.48 8.48 0 018 8v.5z"/></svg>',
  tasks: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/></svg>',
  telemetry: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>',
  arcrun: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>',
  security: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
  tools: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z"/></svg>',
  knowledge: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 014 10 15.3 15.3 0 01-4 10 15.3 15.3 0 01-4-10 15.3 15.3 0 014-10z"/></svg>',
  policy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>',
  settings: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-2 2 2 2 0 01-2-2v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83 0 2 2 0 010-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 01-2-2 2 2 0 012-2h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 010-2.83 2 2 0 012.83 0l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 012-2 2 2 0 012 2v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 0 2 2 0 010 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 012 2 2 2 0 01-2 2h-.09a1.65 1.65 0 00-1.51 1z"/></svg>',
  bell: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 01-3.46 0"/></svg>',
  arc: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>',
  search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>',
  chevron: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg>',
  clock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
  key: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 11-7.778 7.778 5.5 5.5 0 017.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/></svg>',
  refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 11-2.12-9.36L23 10"/></svg>',
  filter: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/></svg>',
  download: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>',
  send: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>',
  eye: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>'
};

const PAGES = [
  { id: 'dashboard', label: 'Dashboard', icon: 'dashboard', file: 'index.html' },
  { id: 'agents', label: 'Agent Fleet', icon: 'agents', file: 'agents.html' },
  { id: 'agent-detail', label: 'Agent Detail', icon: 'agentDetail', file: 'agent-detail.html' },
  { id: 'messages', label: 'Team Comms', icon: 'messages', file: 'messages.html' },
  { id: 'tasks', label: 'Tasks', icon: 'tasks', file: 'tasks.html' },
  { divider: true },
  { id: 'telemetry', label: 'LLM Telemetry', icon: 'telemetry', file: 'telemetry.html' },
  { id: 'arcrun', label: 'ArcRun Monitor', icon: 'arcrun', file: 'arcrun.html' },
  { id: 'security', label: 'Security & Audit', icon: 'security', file: 'security.html' },
  { divider: true },
  { id: 'tools', label: 'Tools & Skills', icon: 'tools', file: 'tools.html' },
  { id: 'knowledge', label: 'Knowledge Base', icon: 'knowledge', file: 'knowledge.html' },
  { id: 'policy', label: 'Policy Engine', icon: 'policy', file: 'policy.html' },
  { id: 'settings', label: 'Settings', icon: 'settings', file: 'settings.html' }
];

function renderShell(activePageId) {
  // Build sidebar
  const sidebarHtml = PAGES.map(p => {
    if (p.divider) return '<div class="sidebar-divider"></div>';
    const isActive = p.id === activePageId ? ' active' : '';
    return `
      <a href="${p.file}" class="sidebar-item${isActive}" title="${p.label}">
        ${ICONS[p.icon]}
        <span class="sidebar-tooltip">${p.label}</span>
      </a>`;
  }).join('');

  // Build topbar
  const topbarHtml = `
    <div class="topbar-logo">
      <svg viewBox="0 0 24 24" fill="none" stroke="#006fff" stroke-width="2">
        <path d="M12 2L2 7l10 5 10-5-10-5z"/>
        <path d="M2 17l10 5 10-5"/>
        <path d="M2 12l10 5 10-5"/>
      </svg>
    </div>
    <div class="topbar-title">ARC Platform</div>
    <div class="topbar-subtitle">Cascade Manufacturing</div>
    <div class="topbar-center">
      <input type="text" class="topbar-search" placeholder="Search agents, tasks, messages..." />
    </div>
    <div class="topbar-right">
      <span class="live-dot">LIVE</span>
      <div class="topbar-badge">
        ${ICONS.bell}
        <span class="badge-count">7</span>
      </div>
      <div class="topbar-user">
        <div class="user-avatar">JS</div>
        <span style="font-size:12px;color:var(--text-secondary)">Operator</span>
      </div>
    </div>`;

  // Inject
  const shell = document.querySelector('.app-shell');
  if (!shell) return;

  const topbar = shell.querySelector('.topbar');
  if (topbar) topbar.innerHTML = topbarHtml;

  const sidebar = shell.querySelector('.sidebar');
  if (sidebar) sidebar.innerHTML = `<div class="sidebar-group">${sidebarHtml}</div>`;
}

// Simulated live clock
function startLiveClock(el) {
  if (!el) return;
  function update() {
    const now = new Date();
    el.textContent = now.toLocaleTimeString('en-US', { hour12: false }) + ' UTC';
  }
  update();
  setInterval(update, 1000);
}

// Generate random sparkline bars
function renderSparkline(container, count = 20, maxH = 28) {
  if (!container) return;
  let html = '';
  for (let i = 0; i < count; i++) {
    const h = 4 + Math.random() * (maxH - 4);
    html += `<div class="bar" style="height:${h}px"></div>`;
  }
  container.innerHTML = html;
}

// Simulated typing for messages
function typeMessage(el, text, speed = 30) {
  return new Promise(resolve => {
    let i = 0;
    el.textContent = '';
    const timer = setInterval(() => {
      if (i < text.length) {
        el.textContent += text[i];
        i++;
      } else {
        clearInterval(timer);
        resolve();
      }
    }, speed);
  });
}

// Random chart bars
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

// Simulated trace log entries
function generateTraceEntries(count = 15) {
  const levels = ['info', 'info', 'info', 'audit', 'debug', 'warn', 'info', 'audit'];
  const sources = Object.values(AGENTS).map(a => a.name);
  const msgs = [
    'Tool call: read_file completed (0.23s)',
    'LLM request: anthropic/claude-sonnet-4-20250514 (2847 tokens in, 412 out)',
    'Policy check passed: tool.read_file allowed by default policy',
    'Entity extracted: "Acme Steel Corp" → kb://vendors/acme-steel',
    'Context window at 42% (53,760 / 128,000 tokens)',
    'Tool call: search_inventory completed (0.89s)',
    'Audit: agent:post_tool event emitted, trace_id=t-7f3a9c',
    'Memory consolidation: 3 entities promoted to long-term',
    'NATS publish: channel://procurement (msg_1708789456_003)',
    'Session checkpoint saved: abc123.jsonl (847 messages)',
    'Module bus: bio_memory handler completed in 45ms',
    'Schedule triggered: inventory-check-hourly (cron: 0 * * * *)',
    'Tool sandbox: execute_python validated, workspace boundary OK',
    'mTLS handshake completed with agent://logistics-01',
    'PII scan: 0 sensitive patterns detected in outbound message',
    'Circuit breaker: openai/gpt-4o healthy (5/5 recent calls OK)',
    'Token budget: 2,847 input + 412 output = $0.0089',
    'Task status update: task_042 → in_progress (agent://procurement-01)',
    'KB entry created: decisions/vendor-selection-2026-q1',
    'Signature verified: Ed25519 OK for agent://quality-01'
  ];

  const entries = [];
  const now = Date.now();
  for (let i = 0; i < count; i++) {
    const ts = new Date(now - (count - i) * 3200);
    entries.push({
      time: ts.toLocaleTimeString('en-US', { hour12: false }),
      level: levels[Math.floor(Math.random() * levels.length)],
      source: sources[Math.floor(Math.random() * sources.length)],
      message: msgs[Math.floor(Math.random() * msgs.length)]
    });
  }
  return entries;
}

function renderTraceLog(container, count = 15) {
  const entries = generateTraceEntries(count);
  container.innerHTML = entries.map(e => `
    <div class="trace-line">
      <span class="trace-ts">${e.time}</span>
      <span class="trace-level ${e.level}">${e.level}</span>
      <span class="trace-source">${e.source}</span>
      <span class="trace-msg">${e.message}</span>
    </div>
  `).join('');
}

// Auto-refresh trace log
function startAutoTrace(container, interval = 4000) {
  renderTraceLog(container);
  setInterval(() => {
    const newEntry = generateTraceEntries(1)[0];
    const line = document.createElement('div');
    line.className = 'trace-line animate-slide-up';
    line.innerHTML = `
      <span class="trace-ts">${newEntry.time}</span>
      <span class="trace-level ${newEntry.level}">${newEntry.level}</span>
      <span class="trace-source">${newEntry.source}</span>
      <span class="trace-msg">${newEntry.message}</span>`;
    container.prepend(line);
    // Keep max 30 entries
    while (container.children.length > 30) {
      container.removeChild(container.lastChild);
    }
  }, interval);
}

// Export for use in pages
window.ARC = { AGENTS, ICONS, PAGES, renderShell, startLiveClock, renderSparkline,
  typeMessage, renderChartBars, generateTraceEntries, renderTraceLog, startAutoTrace };
