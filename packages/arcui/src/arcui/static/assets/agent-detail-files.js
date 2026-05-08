/* ============================================================
   ArcUI — Agent Detail Page: Memory + Files tabs

   Sibling of agent-detail.js. Both renderers delegate to
   ARC.FileTree (a separate IIFE asset); this module just wires the
   right fetchTree/fetchFile pair (workspace root vs agent root).
   IIFE. Exposes ``ARC.AgentDetail._files.{renderMemory,renderFiles}``.
   ============================================================ */

(function () {
  'use strict';

  var _S = window.ARC.AgentDetail._shared;
  var api = _S.api;

  function renderMemory(container, agentId) {
    container.innerHTML = '<div class="card"><div class="card-body agd-tree-wrap" style="padding:8px;"></div></div>';
    var wrap = container.querySelector('.agd-tree-wrap');
    return window.ARC.FileTree.mount(wrap, {
      agentId: agentId,
      fetchTree: function () {
        return api('/api/agents/' + agentId + '/files/tree?root=workspace');
      },
      fetchFile: function (opts) {
        return api('/api/agents/' + agentId +
          '/files/read?root=workspace&path=' + encodeURIComponent(opts.path));
      },
    });
  }

  function renderFiles(container, agentId) {
    container.innerHTML = '<div class="card"><div class="card-body agd-tree-wrap" style="padding:8px;"></div></div>';
    var wrap = container.querySelector('.agd-tree-wrap');
    return window.ARC.FileTree.mount(wrap, {
      agentId: agentId,
      fetchTree: function () {
        return api('/api/agents/' + agentId + '/files/tree?root=agent');
      },
      fetchFile: function (opts) {
        return api('/api/agents/' + agentId +
          '/files/read?root=agent&path=' + encodeURIComponent(opts.path));
      },
    });
  }

  window.ARC.AgentDetail._files = {
    renderMemory: renderMemory,
    renderFiles: renderFiles,
  };
})();
