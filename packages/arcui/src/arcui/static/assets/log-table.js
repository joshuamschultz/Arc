/* ============================================================
   ArcUI — LogTable (filterable, sortable trace table)
   ============================================================ */

class LogTable {
  constructor(tbodyEl, opts = {}) {
    this._tbody = tbodyEl;
    this._maxRows = opts.maxRows ?? 500;
    this._container = opts.scrollContainer ?? tbodyEl.closest('.card-body');
    this._resumeBtn = opts.resumeBtn ?? null;
    this._autoScroll = true;

    // Data store — all trace records (newest first)
    this._allData = [];
    this._seenIds = new Set();

    // Filter/sort state
    this._filters = { provider: '', model: '', agent: '', status: '', search: '' };
    this._sortField = null;
    this._sortDir = 'desc'; // 'asc' or 'desc'

    // Callbacks
    this._onCountChange = opts.onCountChange ?? null;

    if (this._container) {
      this._container.addEventListener('scroll', () => this._checkScroll());
    }
    if (this._resumeBtn) {
      this._resumeBtn.addEventListener('click', () => {
        this._autoScroll = true;
        this._resumeBtn.classList.add('hidden');
        this._scrollToTop();
      });
    }
  }

  /** Replace all data and re-render. */
  setRows(records) {
    this._seenIds.clear();
    this._allData = [];
    for (const rec of records) {
      const id = rec.trace_id;
      if (id && !this._seenIds.has(id)) {
        this._seenIds.add(id);
        this._allData.push(rec);
      }
    }
    if (this._allData.length > this._maxRows) this._allData.length = this._maxRows;
    this._updateFilterOptions();
    this._render();
  }

  /** Add a single new trace (prepend). Skip duplicates. */
  addRow(data) {
    const id = data.trace_id;
    if (id && this._seenIds.has(id)) return;
    if (id) this._seenIds.add(id);
    this._allData.unshift(data);
    if (this._allData.length > this._maxRows) {
      this._allData.length = this._maxRows;
    }
    this._updateFilterOptions();
    this._render();
  }

  /** Add a batch of new traces (prepend). Skip duplicates. */
  addBatch(events) {
    if (!events || events.length === 0) return;
    const newEvents = [];
    for (const evt of events) {
      const id = evt.trace_id;
      if (id && this._seenIds.has(id)) continue;
      if (id) this._seenIds.add(id);
      newEvents.push(evt);
    }
    if (newEvents.length === 0) return;
    this._allData = [...newEvents, ...this._allData].slice(0, this._maxRows);
    this._updateFilterOptions();
    this._render();
  }

  /** Set a filter and re-render. */
  setFilter(key, value) {
    this._filters[key] = value;
    this._render();
  }

  /** Set sort column. Toggles direction if same column. */
  setSort(field) {
    if (this._sortField === field) {
      this._sortDir = this._sortDir === 'desc' ? 'asc' : 'desc';
    } else {
      this._sortField = field;
      this._sortDir = 'desc';
    }
    this._render();
    return { field: this._sortField, dir: this._sortDir };
  }

  /** Get current sort state for arrow rendering. */
  getSortState() {
    return { field: this._sortField, dir: this._sortDir };
  }

  // ---- Internal ----

  _getFiltered() {
    const { provider, model, agent, status, search } = this._filters;
    const searchLower = search.toLowerCase();

    return this._allData.filter(d => {
      if (provider && (d.provider || '') !== provider) return false;
      if (model && (d.model || '') !== model) return false;
      if (agent && (d.agent_label || '') !== agent) return false;
      if (status) {
        const s = d.status || 'ok';
        if (s !== status) return false;
      }
      if (searchLower) {
        const haystack = [
          d.trace_id || '',
          d.model || '',
          d.agent_label || '',
          d.provider || '',
        ].join(' ').toLowerCase();
        if (!haystack.includes(searchLower)) return false;
      }
      return true;
    });
  }

  _getSorted(data) {
    if (!this._sortField) return data;
    const field = this._sortField;
    const dir = this._sortDir === 'asc' ? 1 : -1;

    return [...data].sort((a, b) => {
      let va = a[field];
      let vb = b[field];
      // Handle tokens — input_tokens may fallback to total_tokens
      if (field === 'input_tokens') {
        va = a.input_tokens ?? a.total_tokens ?? 0;
        vb = b.input_tokens ?? b.total_tokens ?? 0;
      }
      // Numeric fields
      if (typeof va === 'number' && typeof vb === 'number') {
        return (va - vb) * dir;
      }
      // String/timestamp comparison
      const sa = String(va || '');
      const sb = String(vb || '');
      return sa.localeCompare(sb) * dir;
    });
  }

  _render() {
    const filtered = this._getFiltered();
    const sorted = this._getSorted(filtered);

    domBatcher.schedule(() => {
      const frag = document.createDocumentFragment();
      for (const rec of sorted) {
        frag.appendChild(this._createRow(rec));
      }
      this._tbody.innerHTML = '';
      this._tbody.appendChild(frag);

      if (this._autoScroll && this._container) {
        this._container.scrollTop = 0;
      }

      if (this._onCountChange) {
        this._onCountChange(sorted.length, this._allData.length);
      }
    });
  }

  /** Populate filter dropdowns from current data. */
  _updateFilterOptions() {
    const providers = new Set();
    const models = new Set();
    const agents = new Set();

    for (const d of this._allData) {
      if (d.provider) providers.add(d.provider);
      if (d.model) models.add(d.model);
      if (d.agent_label) agents.add(d.agent_label);
    }

    this._populateSelect('filter-provider', 'All Providers', providers, this._filters.provider);
    this._populateSelect('filter-model', 'All Models', models, this._filters.model);
    this._populateSelect('filter-agent', 'All Agents', agents, this._filters.agent);
  }

  _populateSelect(id, placeholder, values, currentValue) {
    const el = document.getElementById(id);
    if (!el) return;
    const sorted = [...values].sort();
    const html = [`<option value="">${placeholder}</option>`];
    for (const v of sorted) {
      const escaped = Fmt.escapeHTML(v);
      const selected = v === currentValue ? ' selected' : '';
      html.push(`<option value="${escaped}"${selected}>${escaped}</option>`);
    }
    el.innerHTML = html.join('');
  }

  _createRow(data) {
    const tr = document.createElement('tr');
    const status = data.status || 'ok';
    const attempt = data.attempt_number || 0;
    const retryBadge = attempt > 0
      ? ` <span class="badge badge-warning" title="Attempt #${attempt + 1}">R${attempt}</span>`
      : '';
    const statusBadge = status === 'error' || status === 'timeout'
      ? `<span class="badge badge-error">${Fmt.escapeHTML(status)}</span>${retryBadge}`
      : `<span class="badge badge-online">200</span>${retryBadge}`;

    const toolCalls = (data.response_body || {}).tool_calls || [];
    const toolNames = toolCalls.map(tc => Fmt.escapeHTML(tc.name || '?')).join(', ');
    const toolsCell = toolNames
      ? `<span class="mono text-accent" style="font-size:11px;">${toolNames}</span>`
      : '<span class="text-dimmed">-</span>';

    // DID: show short form (last segment after /)
    const did = data.agent_did || '';
    const didShort = did ? did.split('/').pop() || did.split(':').pop() || did : '-';

    tr.innerHTML =
      `<td class="mono text-accent">${Fmt.escapeHTML(Fmt.traceId(data.trace_id || ''))}</td>` +
      `<td><span class="badge badge-accent">${Fmt.escapeHTML(data.agent_label || '-')}</span></td>` +
      `<td class="mono text-dimmed" style="font-size:11px;" title="${Fmt.escapeHTML(did)}">${Fmt.escapeHTML(didShort)}</td>` +
      `<td class="mono text-muted">${Fmt.escapeHTML(data.model || '-')}</td>` +
      `<td>${toolsCell}</td>` +
      `<td class="mono">${Fmt.number(data.input_tokens || data.total_tokens || 0)}</td>` +
      `<td class="mono">${Fmt.number(data.output_tokens || 0)}</td>` +
      `<td class="mono">${Fmt.latency(data.duration_ms || 0)}</td>` +
      `<td class="mono">${Fmt.cost(data.cost_usd || 0)}</td>` +
      `<td>${statusBadge}</td>` +
      `<td class="text-muted">${Fmt.relativeTime(data.timestamp || '')}</td>`;

    tr.dataset.traceId = data.trace_id || '';
    return tr;
  }

  _checkScroll() {
    if (!this._container) return;
    const { scrollTop } = this._container;
    this._autoScroll = scrollTop < 4;
    if (this._resumeBtn) {
      this._resumeBtn.classList.toggle('hidden', this._autoScroll);
    }
  }

  _scrollToTop() {
    if (this._container) {
      this._container.scrollTop = 0;
    }
  }
}

window.LogTable = LogTable;
