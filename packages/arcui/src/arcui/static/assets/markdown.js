/* ============================================================
   ArcUI — Minimal Markdown Renderer (SPEC-022 §5.5)
   ~80 LOC. Air-gap-friendly. No CDN dependency.

   Supports: # h1..h6, paragraphs, blank lines, ul/ol, code fences,
             inline `code`, **bold**, *em*, [text](url), blockquote.
   Always HTML-escapes input — no raw HTML passthrough.
   ============================================================ */

(function () {
  'use strict';

  function escape(s) {
    return s.replace(/[&<>]/g, function (c) {
      return c === '&' ? '&amp;' : c === '<' ? '&lt;' : '&gt;';
    });
  }

  function inline(s) {
    // Order matters: code first (so we don't bold inside code), then bold/em,
    // then links. We escape first so user content cannot inject markup.
    return escape(s)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/\*([^*]+)\*/g, '<em>$1</em>')
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" rel="noopener noreferrer">$1</a>');
  }

  function renderMarkdown(text) {
    if (typeof text !== 'string') return '';
    var lines = text.split('\n');
    var out = [];
    var inFence = false, fenceLang = '', fenceBuf = [];
    var listType = null; // 'ul' | 'ol' | null

    function closeList() {
      if (listType) {
        out.push('</' + listType + '>');
        listType = null;
      }
    }

    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];

      // Code fences
      if (line.indexOf('```') === 0) {
        if (inFence) {
          out.push(
            '<pre><code class="language-' + escape(fenceLang) + '">' +
            escape(fenceBuf.join('\n')) +
            '</code></pre>'
          );
          inFence = false; fenceBuf = []; fenceLang = '';
        } else {
          closeList();
          inFence = true; fenceLang = line.slice(3).trim();
        }
        continue;
      }
      if (inFence) { fenceBuf.push(line); continue; }

      // Headings
      var hMatch = line.match(/^(#{1,6})\s+(.*)$/);
      if (hMatch) {
        closeList();
        var level = hMatch[1].length;
        out.push('<h' + level + '>' + inline(hMatch[2]) + '</h' + level + '>');
        continue;
      }

      // Unordered list
      if (/^\s*[-*]\s+/.test(line)) {
        if (listType !== 'ul') { closeList(); out.push('<ul>'); listType = 'ul'; }
        out.push('<li>' + inline(line.replace(/^\s*[-*]\s+/, '')) + '</li>');
        continue;
      }

      // Ordered list
      if (/^\s*\d+\.\s+/.test(line)) {
        if (listType !== 'ol') { closeList(); out.push('<ol>'); listType = 'ol'; }
        out.push('<li>' + inline(line.replace(/^\s*\d+\.\s+/, '')) + '</li>');
        continue;
      }

      closeList();

      // Blockquote
      if (line.indexOf('> ') === 0) {
        out.push('<blockquote>' + inline(line.slice(2)) + '</blockquote>');
        continue;
      }

      // Blank line — paragraph break (rendered as empty string; CSS handles spacing)
      if (line.trim() === '') { out.push(''); continue; }

      // Plain paragraph
      out.push('<p>' + inline(line) + '</p>');
    }

    // Final fence — if document ends mid-fence, render what we have so user
    // sees the partial code block instead of swallowed content.
    if (inFence) {
      out.push(
        '<pre><code class="language-' + escape(fenceLang) + '">' +
        escape(fenceBuf.join('\n')) +
        '</code></pre>'
      );
    }
    closeList();
    return out.join('\n');
  }

  window.ARC = window.ARC || {};
  window.ARC.renderMarkdown = renderMarkdown;
  // Convenience global so call sites read like the SDD reference.
  window.renderMarkdown = renderMarkdown;
})();
