/* ============================================================
   ArcUI — Event Drawer (SPEC-022 §4.5)

   Right-side slide-in panel that displays the JSON payload + metadata
   for a single event (audit row, trace, file_change message). Used by
   the audit viewer and traces table to drill into details without
   leaving the current page.

   API:
     ARC.EventDrawer.open(event)   — render + show drawer
     ARC.EventDrawer.close()       — hide drawer
     ARC.EventDrawer.toggle(event)
   ============================================================ */

(function () {
  'use strict';

  var DRAWER_ID = 'arc-event-drawer';

  function escapeText(s) {
    return String(s == null ? '' : s).replace(/[&<>]/g, function (c) {
      return c === '&' ? '&amp;' : c === '<' ? '&lt;' : '&gt;';
    });
  }

  function ensureDrawer() {
    var el = document.getElementById(DRAWER_ID);
    if (el) return el;
    el = document.createElement('aside');
    el.id = DRAWER_ID;
    el.className = 'event-drawer hidden';
    el.setAttribute('aria-hidden', 'true');
    el.innerHTML =
      '<div class="event-drawer-head">' +
        '<span class="event-drawer-title">Event</span>' +
        '<button type="button" class="event-drawer-close" aria-label="Close">×</button>' +
      '</div>' +
      '<div class="event-drawer-body"></div>';
    document.body.appendChild(el);
    el.querySelector('.event-drawer-close').addEventListener('click', close);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') close();
    });
    return el;
  }

  function renderJSON(value) {
    var pretty;
    try {
      pretty = JSON.stringify(value, null, 2);
    } catch (e) {
      pretty = String(value);
    }
    if (window.Prism && window.Prism.languages && window.Prism.languages.json) {
      return (
        '<pre class="event-drawer-json"><code class="language-json">' +
        window.Prism.highlight(pretty, window.Prism.languages.json, 'json') +
        '</code></pre>'
      );
    }
    return '<pre class="event-drawer-json">' + escapeText(pretty) + '</pre>';
  }

  function buildBody(evt) {
    if (evt == null) return '<div class="event-drawer-empty">No event</div>';
    var meta = [];
    ['type', 'event_type', 'agent_id', 'action', 'outcome', 'target', 'timestamp', 'sequence']
      .forEach(function (k) {
        if (evt[k] != null) {
          meta.push(
            '<div class="event-drawer-row">' +
              '<span class="event-drawer-key">' + escapeText(k) + '</span>' +
              '<span class="event-drawer-val">' + escapeText(evt[k]) + '</span>' +
            '</div>'
          );
        }
      });
    return (
      '<div class="event-drawer-meta">' + meta.join('') + '</div>' +
      renderJSON(evt)
    );
  }

  function open(evt) {
    var el = ensureDrawer();
    el.querySelector('.event-drawer-body').innerHTML = buildBody(evt);
    el.classList.remove('hidden');
    el.setAttribute('aria-hidden', 'false');
  }

  function close() {
    var el = document.getElementById(DRAWER_ID);
    if (!el) return;
    el.classList.add('hidden');
    el.setAttribute('aria-hidden', 'true');
  }

  function toggle(evt) {
    var el = document.getElementById(DRAWER_ID);
    if (el && !el.classList.contains('hidden')) close();
    else open(evt);
  }

  window.ARC = window.ARC || {};
  window.ARC.EventDrawer = { open: open, close: close, toggle: toggle };
})();
