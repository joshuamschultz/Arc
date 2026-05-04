/* ============================================================
   ArcUI — Knowledge page (SPEC-023)
   ============================================================
   Renders an agent's memory, workspace tree, and graph stats from
   GET /api/knowledge/{agent_id}. Live updates from FileChangeBridge
   (existing /ws subscription) refresh the memory list in place.
*/

(function () {
  'use strict';

  const PAGE_ID = 'knowledge';

  function rootEl() {
    return document.querySelector(`[data-page-content="${PAGE_ID}"]`);
  }

  function authToken() {
    try { return window.localStorage.getItem('arcui_viewer_token') || ''; }
    catch (e) { return ''; }
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  function renderContext(ctx) {
    const root = rootEl();
    if (!root) return;
    const el = root.querySelector('[data-knowledge-context]');
    if (!el) return;
    const used = ctx.memory_used_tokens || 0;
    const total = ctx.input_tokens || 0;
    const pct = total > 0 ? Math.round((used / total) * 100) : 0;
    el.innerHTML = `
      <h3>Context Budget</h3>
      <div>Model: <code>${escapeHtml(ctx.model || '—')}</code></div>
      <div>Memory: ${used.toLocaleString()} / ${total.toLocaleString()} tokens (${pct}%)</div>
    `;
  }

  function renderMemory(mem) {
    const root = rootEl();
    if (!root) return;
    const el = root.querySelector('[data-knowledge-memory]');
    if (!el) return;
    const rows = (mem.entries || []).map(e => `
      <tr>
        <td><code>${escapeHtml(e.filename)}</code></td>
        <td>${e.size_bytes}</td>
        <td>${escapeHtml(e.classification || '')}</td>
        <td><span class="memory-preview">${escapeHtml((e.preview || '').slice(0, 80))}</span></td>
      </tr>
    `).join('');
    el.innerHTML = `
      <h3>Memory (${(mem.entries || []).length} entries, ${mem.total_bytes || 0} B)</h3>
      <table class="memory-table">
        <thead><tr><th>File</th><th>Size</th><th>Class</th><th>Preview</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    `;
  }

  function renderWorkspace(ws) {
    const root = rootEl();
    if (!root) return;
    const el = root.querySelector('[data-knowledge-workspace]');
    if (!el) return;
    const rows = (ws.tree || []).map(node => {
      const icon = node.type === 'dir' ? '📁' : '📄';
      const size = node.type === 'file' ? ` (${node.size_bytes || 0} B)` : '';
      return `<li>${icon} <code>${escapeHtml(node.path)}</code>${size}</li>`;
    }).join('');
    el.innerHTML = `
      <h3>Workspace${ws.truncated ? ' (truncated)' : ''}</h3>
      <ul class="workspace-tree">${rows}</ul>
    `;
  }

  function renderGraph(graph) {
    const root = rootEl();
    if (!root) return;
    const el = root.querySelector('[data-knowledge-graph]');
    if (!el) return;
    if (!graph.available) {
      el.innerHTML = '<h3>Code Graph</h3><div>Graph unavailable.</div>';
      return;
    }
    el.innerHTML = `
      <h3>Code Graph</h3>
      <div>Nodes: ${graph.node_count || 0}</div>
      <div>Edges: ${graph.edge_count || 0}</div>
      <div>Languages: ${(graph.languages || []).join(', ') || '—'}</div>
    `;
  }

  async function loadKnowledge(agentId) {
    if (!agentId) return;
    const token = authToken();
    const headers = token ? { Authorization: `Bearer ${token}` } : {};
    try {
      const resp = await fetch(`/api/knowledge/${encodeURIComponent(agentId)}`, { headers });
      if (!resp.ok) return;
      const data = await resp.json();
      renderContext(data.context || {});
      renderMemory(data.memory || {});
      renderWorkspace(data.workspace || {});
      renderGraph(data.graph || { available: false });
    } catch (e) { /* surface via UI status later */ }
  }

  function subscribe() {
    if (window.ARC && typeof window.ARC.onRouteChange === 'function') {
      window.ARC.onRouteChange(route => {
        if (route.page === PAGE_ID && route.agent) {
          loadKnowledge(route.agent);
        }
      });
    }
  }

  function isKnowledgeActive() {
    const params = new URLSearchParams(window.location.search);
    return params.get('page') === PAGE_ID && params.get('agent');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      subscribe();
      const params = new URLSearchParams(window.location.search);
      if (isKnowledgeActive()) loadKnowledge(params.get('agent'));
    });
  } else {
    subscribe();
    const params = new URLSearchParams(window.location.search);
    if (isKnowledgeActive()) loadKnowledge(params.get('agent'));
  }

  window.KnowledgePage = { loadKnowledge };
})();
