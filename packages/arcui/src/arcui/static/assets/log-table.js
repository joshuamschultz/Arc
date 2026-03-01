/* ============================================================
   ArcUI — LogTable (bounded trace table with auto-scroll)
   ============================================================ */

class LogTable {
  constructor(tbodyEl, opts = {}) {
    this._tbody = tbodyEl;
    this._maxRows = opts.maxRows ?? 300;
    this._autoScroll = true;
    this._container = opts.scrollContainer ?? tbodyEl.closest('.card-body');
    this._resumeBtn = opts.resumeBtn ?? null;

    if (this._container) {
      this._container.addEventListener('scroll', () => this._checkScroll());
    }
    if (this._resumeBtn) {
      this._resumeBtn.addEventListener('click', () => {
        this._autoScroll = true;
        this._resumeBtn.classList.add('hidden');
        this._scrollToBottom();
      });
    }
  }

  addRow(data) {
    domBatcher.schedule(() => {
      const row = this._createRow(data);
      // Prepend (newest first)
      if (this._tbody.firstChild) {
        this._tbody.insertBefore(row, this._tbody.firstChild);
      } else {
        this._tbody.appendChild(row);
      }

      // Trim excess rows
      while (this._tbody.children.length > this._maxRows) {
        this._tbody.removeChild(this._tbody.lastChild);
      }

      if (this._autoScroll && this._container) {
        this._container.scrollTop = 0;
      }
    });
  }

  addBatch(events) {
    if (!events || events.length === 0) return;
    domBatcher.schedule(() => {
      const frag = document.createDocumentFragment();
      for (const evt of events) {
        frag.appendChild(this._createRow(evt));
      }
      if (this._tbody.firstChild) {
        this._tbody.insertBefore(frag, this._tbody.firstChild);
      } else {
        this._tbody.appendChild(frag);
      }

      while (this._tbody.children.length > this._maxRows) {
        this._tbody.removeChild(this._tbody.lastChild);
      }

      if (this._autoScroll && this._container) {
        this._container.scrollTop = 0;
      }
    });
  }

  setRows(records) {
    domBatcher.schedule(() => {
      const frag = document.createDocumentFragment();
      for (const rec of records) {
        frag.appendChild(this._createRow(rec));
      }
      this._tbody.innerHTML = '';
      this._tbody.appendChild(frag);
    });
  }

  _createRow(data) {
    const tr = document.createElement('tr');
    const status = data.status || 'ok';
    const statusBadge = status === 'error' || status === 'timeout'
      ? `<span class="badge badge-error">${Fmt.escapeHTML(status)}</span>`
      : `<span class="badge badge-online">200</span>`;

    tr.innerHTML =
      `<td class="mono text-accent">${Fmt.escapeHTML(Fmt.traceId(data.trace_id || ''))}</td>` +
      `<td><span class="badge badge-accent">${Fmt.escapeHTML(data.agent_label || '-')}</span></td>` +
      `<td class="mono text-muted">${Fmt.escapeHTML(data.model || '-')}</td>` +
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
    // If scrolled away from top (more than 4px)
    this._autoScroll = scrollTop < 4;
    if (this._resumeBtn) {
      this._resumeBtn.classList.toggle('hidden', this._autoScroll);
    }
  }

  _scrollToBottom() {
    if (this._container) {
      this._container.scrollTop = 0;
    }
  }
}

window.LogTable = LogTable;
