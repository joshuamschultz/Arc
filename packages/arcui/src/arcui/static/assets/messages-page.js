/* ============================================================
   ArcUI — Messages page (SPEC-023)
   ============================================================
   Slack-style DM client for served agents. Three columns:
   - left:   roster of teammates with online indicator
   - center: active conversation feed + composer
   - right:  audit / thread details

   Each conversation owns one WebSocket — the gateway adapter fans
   the agent's reply to every browser tab open on the same chat_id,
   so duplicate tabs stay in sync without extra plumbing.
*/

(function () {
  'use strict';

  const PAGE_ID = 'messages';

  /** @type {{
   *   agents: any[],
   *   activeAgentId: string|null,
   *   ws: WebSocket|null,
   *   clientSeq: number,
   *   chatId: string|null,
   *   localMessages: Map<string, any>,
   *   placeholderId: string|null,
   *   bound: boolean,
   *   refreshTimer: number|null,
   * }} */
  const state = {
    agents: [],
    activeAgentId: null,
    ws: null,
    clientSeq: 0,
    chatId: null,
    localMessages: new Map(),
    placeholderId: null,
    bound: false,
    refreshTimer: null,
  };

  function rootEl() {
    return document.querySelector(`[data-page-content="${PAGE_ID}"]`);
  }

  function authToken() {
    try { return window.localStorage.getItem('arcui_viewer_token') || ''; }
    catch (e) { return ''; }
  }

  function authHeaders() {
    const tok = authToken();
    return tok ? { Authorization: `Bearer ${tok}` } : {};
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function avatarInitials(name) {
    return String(name || '?')
      .split(/[-_\s]+/)
      .filter(Boolean)
      .slice(0, 2)
      .map(w => w[0].toUpperCase())
      .join('') || '?';
  }

  // ── Roster ──────────────────────────────────────────────────────────────────

  async function loadAgents() {
    try {
      const resp = await fetch('/api/team/roster', { headers: authHeaders() });
      if (!resp.ok) return [];
      const data = await resp.json();
      const agents = Array.isArray(data) ? data : (data.agents || []);
      // Sort: online first, then by name.
      return agents.slice().sort((a, b) => {
        if (!!a.online !== !!b.online) return a.online ? -1 : 1;
        return (a.display_name || a.name || '').localeCompare(b.display_name || b.name || '');
      });
    } catch (e) {
      return [];
    }
  }

  function renderAgents() {
    const root = rootEl();
    if (!root) return;
    const list = root.querySelector('[data-msg-agents]');
    if (!list) return;
    if (state.agents.length === 0) {
      list.innerHTML = `
        <div class="text-xs text-dimmed" style="padding:14px;line-height:1.5;">
          No teammates yet. Run <code>arc team register</code> or place a
          <code>&lt;name&gt;_agent</code> directory under <code>./team/</code>
          and reload.
        </div>`;
    } else {
      list.innerHTML = state.agents.map(a => {
        const id = a.agent_id || a.name || '';
        const display = a.display_name || a.name || id;
        const role = a.role_label || a.type || '';
        const dotClass = a.online ? 'online' : 'offline';
        const cls = id === state.activeAgentId ? 'channel-item active' : 'channel-item';
        return `<div class="${cls}" data-agent-id="${escapeHtml(id)}"
          style="padding:8px 14px;cursor:pointer;display:flex;align-items:center;gap:10px;border-left:3px solid transparent;">
          <span class="status-dot ${dotClass}" style="margin-right:0;"></span>
          <span style="flex:1;display:flex;flex-direction:column;min-width:0;">
            <span class="text-sm" style="color:inherit;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(display)}</span>
            ${role ? `<span class="text-xs text-dimmed" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(role)}</span>` : ''}
          </span>
        </div>`;
      }).join('');
      list.querySelectorAll('[data-agent-id]').forEach(btn => {
        btn.addEventListener('click', () => openChat(btn.getAttribute('data-agent-id')));
      });
    }
    const counter = root.querySelector('[data-msg-online-count]');
    if (counter) {
      const online = state.agents.filter(a => a.online).length;
      counter.textContent = `${online} online · ${state.agents.length} total`;
    }
  }

  // ── Conversation ────────────────────────────────────────────────────────────

  function activeAgent() {
    return state.agents.find(a => (a.agent_id || a.name) === state.activeAgentId) || null;
  }

  function renderHeader() {
    const root = rootEl();
    if (!root) return;
    const title = root.querySelector('[data-msg-header-title]');
    if (!title) return;
    const a = activeAgent();
    if (!a) {
      title.textContent = 'Select a teammate to start chatting';
      title.className = 'text-dimmed font-bold';
      return;
    }
    const display = a.display_name || a.name || state.activeAgentId;
    const status = a.online ? '<span class="status-dot online"></span>online' : '<span class="status-dot offline"></span>offline';
    title.innerHTML = `
      <span class="text-white" style="font-size:15px;">${escapeHtml(display)}</span>
      <span class="text-xs text-dimmed" style="margin-left:8px;">${status}</span>
      <span class="text-xs text-dimmed mono" style="margin-left:auto;">${escapeHtml(a.did || '')}</span>
    `;
    title.className = '';
    title.style.display = 'flex';
    title.style.alignItems = 'center';
    title.style.gap = '8px';
    title.style.width = '100%';
  }

  function renderMessages() {
    const root = rootEl();
    if (!root) return;
    const feed = root.querySelector('[data-msg-feed]');
    if (!feed) return;
    if (state.localMessages.size === 0) {
      feed.innerHTML = `
        <div class="text-dimmed" style="padding:32px 24px;text-align:center;font-size:13px;">
          ${state.activeAgentId
            ? `Say hi to ${escapeHtml(activeAgent()?.display_name || state.activeAgentId)}.`
            : 'Pick someone from the left panel.'}
        </div>`;
      return;
    }
    const me = '<span class="text-accent font-bold">You</span>';
    feed.innerHTML = Array.from(state.localMessages.values()).map(m => {
      if (m.role === 'tool_call') {
        return `<div class="message">
          <div class="message-avatar" style="background:var(--bg-deepest);color:var(--text-dimmed);">tk</div>
          <div class="message-body">
            <div class="message-header">
              <span class="message-sender text-dimmed">tool</span>
              <span class="message-time mono">${escapeHtml(m.tool || '')}</span>
            </div>
            <div class="message-content text-dimmed mono text-xs">${escapeHtml(m.args || '')}</div>
          </div>
        </div>`;
      }
      if (m.role === 'user') {
        return `<div class="message">
          <div class="message-avatar" style="background:var(--accent);color:white;">YOU</div>
          <div class="message-body">
            <div class="message-header">
              ${me}
              <span class="message-time">${escapeHtml(m.time || '')}</span>
            </div>
            <div class="message-content">${escapeHtml(m.text || '')}</div>
          </div>
        </div>`;
      }
      const a = activeAgent();
      const display = a?.display_name || a?.name || state.activeAgentId || 'agent';
      const color = a?.color || '#006fff';
      const initials = avatarInitials(display);
      const audit = m.audit_hash ? `<div class="text-xs text-dimmed mono" style="margin-top:6px;opacity:.6;">audit: ${escapeHtml(m.audit_hash.slice(0, 26))}…</div>` : '';
      const placeholderClass = m.placeholder ? ' text-dimmed' : '';
      return `<div class="message">
        <div class="message-avatar" style="background:${escapeHtml(color)};">${escapeHtml(initials)}</div>
        <div class="message-body">
          <div class="message-header">
            <span class="message-sender">${escapeHtml(display)}</span>
            <span class="message-time">${escapeHtml(m.time || '')}</span>
          </div>
          <div class="message-content${placeholderClass}">${escapeHtml(m.text || '')}</div>
          ${audit}
        </div>
      </div>`;
    }).join('');
    feed.scrollTop = feed.scrollHeight;
  }

  function renderThread() {
    const root = rootEl();
    if (!root) return;
    const panel = root.querySelector('[data-msg-thread]');
    if (!panel) return;
    if (!state.activeAgentId) {
      panel.innerHTML = '<div class="text-xs text-dimmed">Open a conversation to see audit + thread metadata.</div>';
      return;
    }
    const recent = Array.from(state.localMessages.values()).slice().reverse().find(m => m.audit_hash);
    const a = activeAgent();
    panel.innerHTML = `
      <div style="margin-bottom:14px;">
        <div class="text-xs text-dimmed">Agent</div>
        <div class="mono text-accent text-sm" style="word-break:break-all;">${escapeHtml(a?.did || '—')}</div>
      </div>
      <div style="margin-bottom:14px;">
        <div class="text-xs text-dimmed">Chat ID</div>
        <div class="mono text-sm" style="word-break:break-all;">${escapeHtml(state.chatId || '—')}</div>
      </div>
      <div style="margin-bottom:14px;">
        <div class="text-xs text-dimmed">Last audit_hash</div>
        <div class="mono text-xs" style="word-break:break-all;color:var(--text-secondary);">${escapeHtml(recent?.audit_hash || '—')}</div>
      </div>
      <div style="margin-bottom:14px;">
        <div class="text-xs text-dimmed">Open Knowledge Page</div>
        <a href="?page=knowledge&agent=${encodeURIComponent(state.activeAgentId)}" class="text-accent text-sm">View memory & workspace →</a>
      </div>
    `;
  }

  function setComposerEnabled(enabled) {
    const root = rootEl();
    if (!root) return;
    const input = root.querySelector('[data-msg-input]');
    const btn = root.querySelector('[data-msg-send]');
    if (input) input.disabled = !enabled;
    if (btn) btn.disabled = !enabled;
    if (input && enabled) input.focus();
  }

  function timeNow() {
    const d = new Date();
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  function closeChat() {
    if (state.ws) {
      try { state.ws.close(); } catch (e) { /* ignore */ }
      state.ws = null;
    }
    state.activeAgentId = null;
    state.chatId = null;
    state.clientSeq = 0;
    state.localMessages.clear();
    state.placeholderId = null;
  }

  function openChat(agentId) {
    if (!agentId) return;
    if (agentId === state.activeAgentId) return;
    closeChat();
    state.activeAgentId = agentId;
    renderAgents();
    renderHeader();
    renderMessages();
    renderThread();

    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${proto}//${window.location.host}/ws/chat/${encodeURIComponent(agentId)}`;
    const ws = new WebSocket(url);
    state.ws = ws;

    ws.addEventListener('open', () => {
      ws.send(JSON.stringify({ token: authToken() }));
    });
    ws.addEventListener('message', (ev) => onIncoming(ev.data));
    ws.addEventListener('close', () => {
      setComposerEnabled(false);
      state.ws = null;
    });
    ws.addEventListener('error', () => { /* close handler will fire */ });
  }

  async function loadHistory(agentId, chatId) {
    // The arcagent SessionManager writes every chat turn to
    // workspace/sessions/<chat_id>.jsonl; /api/agents/{id}/sessions/{sid}
    // returns those messages in order. No separate chat-history store
    // exists by design — sessions ARE the chat history.
    if (!agentId || !chatId) return;
    try {
      const resp = await fetch(
        `/api/agents/${encodeURIComponent(agentId)}/sessions/${encodeURIComponent(chatId)}?page_size=200`,
        { headers: authHeaders() },
      );
      if (!resp.ok) return;
      const data = await resp.json();
      // Bail if the user already opened a different conversation while
      // history was in flight.
      if (state.activeAgentId !== agentId || state.chatId !== chatId) return;
      // Bail if live frames already populated — don't clobber them.
      if (state.localMessages.size > 0) return;
      const msgs = Array.isArray(data.messages) ? data.messages : [];
      msgs.forEach((m, i) => {
        const role = m.role === 'user' ? 'user' : 'agent';
        const id = `h${i}`;
        state.localMessages.set(id, {
          role,
          text: typeof m.content === 'string' ? m.content : JSON.stringify(m.content),
          time: m.ts || '',
        });
      });
      renderMessages();
      renderThread();
    } catch (e) { /* ignore — fresh chats start empty */ }
  }

  function onIncoming(raw) {
    let frame;
    try { frame = JSON.parse(raw); } catch (e) { return; }
    if (!frame || typeof frame !== 'object') return;

    if (frame.type === 'ready') {
      state.chatId = frame.chat_id || null;
      setComposerEnabled(true);
      renderThread();
      // Load any prior conversation from the agent's session log so the
      // user sees their history on reconnect (Slack-style).
      loadHistory(state.activeAgentId, state.chatId);
      return;
    }
    if (frame.error) {
      // Auth or upgrade error — surface to feed.
      state.localMessages.set('err-' + Date.now(), {
        role: 'agent', text: 'Error: ' + frame.error, time: timeNow(),
      });
      renderMessages();
      return;
    }
    if (frame.type === 'tool_call') {
      const id = 'tool-' + (frame.turn_id || Date.now());
      state.localMessages.set(id, {
        role: 'tool_call', tool: frame.tool || 'tool',
        args: frame.args || '', time: frame.ts || timeNow(),
      });
      renderMessages();
      return;
    }
    if (frame.type === 'message' && frame.from === 'agent') {
      // Treat the StreamBridge "..." placeholder as a typing indicator we
      // overwrite in place rather than as a separate bubble.
      const isPlaceholder = (frame.text || '').trim() === '...';
      if (isPlaceholder) {
        const pid = 'placeholder-' + (frame.turn_id || Date.now());
        state.placeholderId = pid;
        state.localMessages.set(pid, {
          role: 'agent', text: '…thinking',
          time: frame.ts || timeNow(),
          placeholder: true,
        });
        renderMessages();
        return;
      }
      // Real reply — replace the placeholder with the final text.
      if (state.placeholderId) {
        state.localMessages.delete(state.placeholderId);
        state.placeholderId = null;
      }
      const id = 'm-' + (frame.turn_id || Date.now()) + '-' + state.localMessages.size;
      state.localMessages.set(id, {
        role: 'agent', text: frame.text || '',
        audit_hash: frame.audit_hash,
        time: frame.ts || timeNow(),
      });
      renderMessages();
      renderThread();
    }
  }

  // ── Composer ────────────────────────────────────────────────────────────────

  function sendMessage() {
    const root = rootEl();
    if (!root) return;
    const input = root.querySelector('[data-msg-input]');
    if (!input) return;
    const text = (input.value || '').trim();
    if (!text) return;
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
    state.clientSeq += 1;
    const id = `u${state.clientSeq}`;
    state.localMessages.set(id, { role: 'user', text, time: timeNow() });
    state.ws.send(JSON.stringify({
      type: 'message', text, client_seq: state.clientSeq,
    }));
    input.value = '';
    renderMessages();
  }

  function bindForm() {
    const root = rootEl();
    if (!root || state.bound) return;
    const form = root.querySelector('[data-msg-form]');
    if (!form) return;
    form.addEventListener('submit', (e) => { e.preventDefault(); sendMessage(); });
    state.bound = true;
  }

  // ── Lifecycle ───────────────────────────────────────────────────────────────

  async function refreshRoster() {
    state.agents = await loadAgents();
    renderAgents();
    renderHeader();
  }

  async function mount() {
    bindForm();
    await refreshRoster();
    if (state.refreshTimer == null) {
      state.refreshTimer = window.setInterval(refreshRoster, 5000);
    }
  }

  function unmount() {
    closeChat();
    renderAgents();
    renderHeader();
    renderMessages();
    renderThread();
    if (state.refreshTimer != null) {
      clearInterval(state.refreshTimer);
      state.refreshTimer = null;
    }
  }

  function isMessagesActive() {
    const params = new URLSearchParams(window.location.search);
    return (params.get('page') || 'agents') === PAGE_ID;
  }

  // arc-shell exposes onRouteChange via window.ARC.
  function subscribe() {
    if (window.ARC && typeof window.ARC.onRouteChange === 'function') {
      window.ARC.onRouteChange(route => {
        if (route.page === PAGE_ID) mount();
        else unmount();
      });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      subscribe();
      if (isMessagesActive()) mount();
    });
  } else {
    subscribe();
    if (isMessagesActive()) mount();
  }

  window.MessagesPage = { mount, unmount, openChat };
})();
