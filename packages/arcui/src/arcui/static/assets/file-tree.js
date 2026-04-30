/* ============================================================
   ArcUI — File Tree Component (SPEC-022 §5.6)

   Two panes side-by-side:
     - Tree (left): folders expand/collapse; state persisted in
       localStorage under `arcui:tree:<agent_id>:<rel_path>`
     - Viewer (right): markdown rendered via renderMarkdown(),
       code highlighted via Prism.highlight().

   API:
     ARC.FileTree.mount(rootEl, { agentId, root, fetchTree, fetchFile })
     instance.dispose()

   The component is purely structural; it does no fetching itself.
   Caller supplies async fetchTree({path}) -> [{path, type, size, mtime}]
   and fetchFile({path}) -> {content, content_type}.
   ============================================================ */

(function () {
  'use strict';

  var LANG_BY_EXT = {
    py: 'python',
    toml: 'toml',
    json: 'json',
    js: 'javascript',
    mjs: 'javascript',
  };

  function storageKey(agentId, path) {
    return 'arcui:tree:' + agentId + ':' + (path || '/');
  }

  function isExpanded(agentId, path) {
    try {
      return window.localStorage.getItem(storageKey(agentId, path)) === '1';
    } catch (e) {
      return false;
    }
  }

  function setExpanded(agentId, path, value) {
    try {
      if (value) {
        window.localStorage.setItem(storageKey(agentId, path), '1');
      } else {
        window.localStorage.removeItem(storageKey(agentId, path));
      }
    } catch (e) { /* localStorage disabled — fall through */ }
  }

  function buildTreeIndex(entries) {
    // entries: [{path: 'a/b/c.md', type: 'file'|'dir', ...}]
    // Produce: { '': [children], 'a': [children], 'a/b': [children] }
    var byParent = { '': [] };
    entries.forEach(function (e) {
      var idx = e.path.lastIndexOf('/');
      var parent = idx >= 0 ? e.path.slice(0, idx) : '';
      if (!byParent[parent]) byParent[parent] = [];
      byParent[parent].push(e);
      if (e.type === 'dir' && !byParent[e.path]) byParent[e.path] = [];
    });
    Object.keys(byParent).forEach(function (k) {
      byParent[k].sort(function (a, b) {
        if (a.type !== b.type) return a.type === 'dir' ? -1 : 1;
        return a.path < b.path ? -1 : a.path > b.path ? 1 : 0;
      });
    });
    return byParent;
  }

  function leaf(path) {
    var idx = path.lastIndexOf('/');
    return idx >= 0 ? path.slice(idx + 1) : path;
  }

  function extOf(path) {
    var idx = path.lastIndexOf('.');
    return idx >= 0 ? path.slice(idx + 1).toLowerCase() : '';
  }

  function renderNode(agentId, entry, byParent, depth) {
    var name = leaf(entry.path);
    var pad = depth * 12;
    if (entry.type === 'dir') {
      var open = isExpanded(agentId, entry.path);
      var children = byParent[entry.path] || [];
      var html =
        '<div class="ft-row ft-dir" data-path="' + escapeAttr(entry.path) +
        '" data-kind="dir" style="padding-left:' + pad + 'px">' +
        '<span class="ft-twisty">' + (open ? '▾' : '▸') + '</span>' +
        '<span class="ft-name">' + escapeText(name) + '</span>' +
        '</div>';
      if (open) {
        children.forEach(function (c) {
          html += renderNode(agentId, c, byParent, depth + 1);
        });
      }
      return html;
    }
    return (
      '<div class="ft-row ft-file" data-path="' + escapeAttr(entry.path) +
      '" data-kind="file" style="padding-left:' + pad + 'px">' +
      '<span class="ft-icon">·</span>' +
      '<span class="ft-name">' + escapeText(name) + '</span>' +
      '</div>'
    );
  }

  function escapeText(s) {
    return String(s).replace(/[&<>]/g, function (c) {
      return c === '&' ? '&amp;' : c === '<' ? '&lt;' : '&gt;';
    });
  }
  function escapeAttr(s) { return escapeText(s).replace(/"/g, '&quot;'); }

  function renderViewer(viewerEl, fileMeta, content) {
    var ext = extOf(fileMeta.path);
    if (ext === 'md') {
      viewerEl.innerHTML =
        '<div class="ft-viewer-path">' + escapeText(fileMeta.path) + '</div>' +
        '<div class="ft-viewer-md">' + window.ARC.renderMarkdown(content) + '</div>';
      return;
    }
    var lang = LANG_BY_EXT[ext] || '';
    var highlighted;
    if (lang && window.Prism && window.Prism.languages && window.Prism.languages[lang]) {
      highlighted = window.Prism.highlight(content, window.Prism.languages[lang], lang);
    } else {
      highlighted = escapeText(content);
    }
    viewerEl.innerHTML =
      '<div class="ft-viewer-path">' + escapeText(fileMeta.path) + '</div>' +
      '<pre class="ft-viewer-code"><code class="language-' + escapeAttr(lang) + '">' +
      highlighted + '</code></pre>';
  }

  function mount(rootEl, opts) {
    var agentId = opts.agentId;
    var fetchTree = opts.fetchTree;
    var fetchFile = opts.fetchFile;
    var entries = [];
    var byParent = { '': [] };

    rootEl.innerHTML =
      '<div class="ft-tree" role="tree"></div>' +
      '<div class="ft-viewer"><div class="ft-empty">Select a file</div></div>';
    var treeEl = rootEl.querySelector('.ft-tree');
    var viewerEl = rootEl.querySelector('.ft-viewer');

    function redraw() {
      var html = '';
      (byParent[''] || []).forEach(function (e) {
        html += renderNode(agentId, e, byParent, 0);
      });
      treeEl.innerHTML = html || '<div class="ft-empty">Empty</div>';
    }

    function load() {
      Promise.resolve(fetchTree({ path: '' })).then(function (resp) {
        entries = (resp && resp.entries) || resp || [];
        byParent = buildTreeIndex(entries);
        redraw();
      }).catch(function () {
        treeEl.innerHTML = '<div class="ft-error">Failed to load tree</div>';
      });
    }

    function onClick(ev) {
      var row = ev.target.closest('.ft-row');
      if (!row) return;
      var path = row.getAttribute('data-path');
      var kind = row.getAttribute('data-kind');
      if (kind === 'dir') {
        setExpanded(agentId, path, !isExpanded(agentId, path));
        redraw();
        return;
      }
      // file: fetch + render
      viewerEl.innerHTML = '<div class="ft-loading">Loading…</div>';
      Promise.resolve(fetchFile({ path: path })).then(function (resp) {
        renderViewer(viewerEl, { path: path }, resp.content || '');
      }).catch(function () {
        viewerEl.innerHTML = '<div class="ft-error">Failed to load file</div>';
      });
    }

    treeEl.addEventListener('click', onClick);
    load();

    return {
      reload: load,
      dispose: function () {
        treeEl.removeEventListener('click', onClick);
        rootEl.innerHTML = '';
      },
    };
  }

  window.ARC = window.ARC || {};
  window.ARC.FileTree = { mount: mount };
})();
