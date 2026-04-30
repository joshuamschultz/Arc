/* ============================================================
   ArcUI — Audit Viewer (SPEC-022 §4.5)

   Renders an audit ring as a paginated, filterable table. Each row
   click opens the EventDrawer with the full event payload.

   API:
     ARC.AuditViewer.mount(rootEl, { fetchPage, pageSize? })
     instance.dispose()
     instance.refresh()

   Caller supplies fetchPage({limit, offset, filter}) -> {events: [...], total: N}.
   The component never fetches directly — keeps the data plane in caller-land.
   ============================================================ */

(function () {
  'use strict';

  var DEFAULT_PAGE = 50;

  function escapeText(s) {
    return String(s == null ? '' : s).replace(/[&<>]/g, function (c) {
      return c === '&' ? '&amp;' : c === '<' ? '&lt;' : '&gt;';
    });
  }

  function fmtTimestamp(ts) {
    if (!ts) return '';
    if (typeof ts === 'number') {
      try { return new Date(ts * (ts > 1e12 ? 1 : 1000)).toISOString(); }
      catch (e) { return String(ts); }
    }
    return String(ts);
  }

  function outcomeClass(outcome) {
    var v = String(outcome || '').toLowerCase();
    if (v === 'success' || v === 'allow' || v === 'allowed') return 'av-outcome-ok';
    if (v === 'deny' || v === 'denied' || v === 'fail' || v === 'failure' || v === 'error')
      return 'av-outcome-bad';
    return 'av-outcome-neutral';
  }

  function renderRow(evt, idx) {
    return (
      '<tr class="av-row" data-idx="' + idx + '">' +
        '<td class="av-time">' + escapeText(fmtTimestamp(evt.timestamp || evt.ts)) + '</td>' +
        '<td class="av-action">' + escapeText(evt.action || '') + '</td>' +
        '<td class="av-target">' + escapeText(evt.target || evt.path || '') + '</td>' +
        '<td class="av-agent">' + escapeText(evt.agent_id || '') + '</td>' +
        '<td class="av-outcome ' + outcomeClass(evt.outcome) + '">' +
          escapeText(evt.outcome || '') +
        '</td>' +
      '</tr>'
    );
  }

  function renderTable(events) {
    if (!events || events.length === 0) {
      return '<div class="av-empty">No audit events</div>';
    }
    return (
      '<table class="av-table">' +
        '<thead><tr>' +
          '<th>Timestamp</th><th>Action</th><th>Target</th><th>Agent</th><th>Outcome</th>' +
        '</tr></thead>' +
        '<tbody>' + events.map(renderRow).join('') + '</tbody>' +
      '</table>'
    );
  }

  function mount(rootEl, opts) {
    opts = opts || {};
    var fetchPage = opts.fetchPage;
    var pageSize = opts.pageSize || DEFAULT_PAGE;
    var offset = 0;
    var events = [];
    var total = 0;
    var filterText = '';

    rootEl.innerHTML =
      '<div class="av-toolbar">' +
        '<input type="search" class="av-filter" placeholder="Filter…">' +
        '<span class="av-count"></span>' +
        '<button type="button" class="av-prev" disabled>Prev</button>' +
        '<button type="button" class="av-next" disabled>Next</button>' +
      '</div>' +
      '<div class="av-body"></div>';

    var bodyEl = rootEl.querySelector('.av-body');
    var prevBtn = rootEl.querySelector('.av-prev');
    var nextBtn = rootEl.querySelector('.av-next');
    var countEl = rootEl.querySelector('.av-count');
    var filterEl = rootEl.querySelector('.av-filter');

    function load() {
      bodyEl.innerHTML = '<div class="av-loading">Loading…</div>';
      Promise.resolve(fetchPage({
        limit: pageSize,
        offset: offset,
        filter: filterText,
      })).then(function (resp) {
        events = (resp && resp.events) || [];
        total = (resp && resp.total) || events.length;
        bodyEl.innerHTML = renderTable(events);
        countEl.textContent = (offset + 1) + '–' + (offset + events.length) +
          ' of ' + total;
        prevBtn.disabled = offset <= 0;
        nextBtn.disabled = offset + events.length >= total;
      }).catch(function () {
        bodyEl.innerHTML = '<div class="av-error">Failed to load audit events</div>';
      });
    }

    function onPrev() {
      offset = Math.max(0, offset - pageSize);
      load();
    }
    function onNext() {
      if (offset + events.length < total) {
        offset += pageSize;
        load();
      }
    }
    function onFilter() {
      filterText = filterEl.value;
      offset = 0;
      load();
    }
    function onRowClick(ev) {
      var row = ev.target.closest('.av-row');
      if (!row) return;
      var idx = Number(row.getAttribute('data-idx'));
      var evt = events[idx];
      if (evt && window.ARC && window.ARC.EventDrawer) {
        window.ARC.EventDrawer.open(evt);
      }
    }

    prevBtn.addEventListener('click', onPrev);
    nextBtn.addEventListener('click', onNext);
    filterEl.addEventListener('change', onFilter);
    bodyEl.addEventListener('click', onRowClick);
    load();

    return {
      refresh: load,
      dispose: function () {
        prevBtn.removeEventListener('click', onPrev);
        nextBtn.removeEventListener('click', onNext);
        filterEl.removeEventListener('change', onFilter);
        bodyEl.removeEventListener('click', onRowClick);
        rootEl.innerHTML = '';
      },
    };
  }

  window.ARC = window.ARC || {};
  window.ARC.AuditViewer = { mount: mount };
})();
