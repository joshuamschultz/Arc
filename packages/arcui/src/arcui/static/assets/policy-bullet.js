/* ============================================================
   ArcUI — Policy Bullet (demo-aligned, SPEC-022 §4.4)

   Mirrors demo/policy.html exactly:
     <div class="policy-bullet high-score">
       <div class="flex items-center justify-between">
         <div class="flex items-center gap-8">
           <span class="policy-bullet-id">P01</span>
           <span class="badge badge-accent">agent_id</span>     // optional
         </div>
         <div class="score-bar">
           <div class="score-bar-track">
             <div class="score-bar-fill green" style="width:90%"></div>
           </div>
           <span class="score-value green">9</span>
         </div>
       </div>
       <div class="policy-bullet-text">…</div>
       <div class="policy-bullet-meta">
         <span class="meta-item"><span class="meta-label">uses:</span>12</span>
         …
       </div>
     </div>

   API unchanged from prior version: .render(b), .renderList(arr),
   .scoreTier(score), .sortBy(arr,key,dir), .filterBy(arr,opts).
   ============================================================ */

(function () {
  'use strict';

  function escText(s) {
    return String(s == null ? '' : s).replace(/[&<>]/g, function (c) {
      return c === '&' ? '&amp;' : c === '<' ? '&lt;' : '&gt;';
    });
  }

  function scoreTier(score) {
    if (score == null) return 'mid';
    var n = Number(score);
    if (n <= 2) return 'retired';
    if (n <= 4) return 'low';
    if (n <= 7) return 'mid';
    return 'high';
  }

  // Demo classes: high-score | mid-score | low-score | retired
  function demoClassFor(b) {
    if (b.retired) return 'retired';
    var t = scoreTier(b.score);
    return t === 'high' ? 'high-score'
      : t === 'mid' ? 'mid-score'
      : t === 'low' ? 'low-score'
      : 'retired';
  }

  // Demo color names: green | yellow | red — drives both fill & numeric color
  function colorFor(b) {
    if (b.retired) return 'red';
    var t = scoreTier(b.score);
    if (t === 'high') return 'green';
    if (t === 'mid')  return 'yellow';
    return 'red';
  }

  function metaItem(label, value) {
    if (value == null || value === '') return '';
    return (
      '<span class="meta-item">' +
        '<span class="meta-label">' + escText(label) + ':</span>' +
        escText(value) +
      '</span>'
    );
  }

  function render(b) {
    if (!b || !b.id) return '';
    var cls = demoClassFor(b);
    var color = colorFor(b);
    var score = b.score == null ? 0 : Number(b.score);
    var pct = Math.max(0, Math.min(100, Math.round(score * 10)));
    var agentTag = b.agent_id
      ? '<span class="badge badge-accent" style="font-size:10px;">' +
          escText(b.agent_id) + '</span>'
      : '';

    return (
      '<div class="policy-bullet ' + cls + '" ' +
        'data-bullet-id="' + escText(b.id) + '" ' +
        'data-score="' + escText(score) + '">' +
        '<div class="flex items-center justify-between">' +
          '<div class="flex items-center gap-8">' +
            '<span class="policy-bullet-id">' + escText(b.id) + '</span>' +
            agentTag +
          '</div>' +
          '<div class="score-bar">' +
            '<div class="score-bar-track">' +
              '<div class="score-bar-fill ' + color + '" ' +
                'style="width:' + pct + '%"></div>' +
            '</div>' +
            '<span class="score-value ' + color + '">' + escText(score) + '</span>' +
          '</div>' +
        '</div>' +
        '<div class="policy-bullet-text">' + escText(b.text) + '</div>' +
        '<div class="policy-bullet-meta">' +
          metaItem('uses', b.uses) +
          metaItem('reviewed', b.reviewed) +
          metaItem('created', b.created) +
          metaItem('source', b.source) +
        '</div>' +
      '</div>'
    );
  }

  function renderList(bullets) {
    if (!Array.isArray(bullets) || bullets.length === 0) {
      return '<div class="text-dimmed text-xs" style="padding:24px;text-align:center;">No bullets</div>';
    }
    return bullets.map(render).join('');
  }

  function sortBy(bullets, key, dir) {
    var sign = dir === 'asc' ? 1 : -1;
    var copy = bullets.slice();
    copy.sort(function (a, b) {
      var av = a[key], bv = b[key];
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      if (av < bv) return -1 * sign;
      if (av > bv) return 1 * sign;
      return 0;
    });
    return copy;
  }

  function filterBy(bullets, opts) {
    opts = opts || {};
    return bullets.filter(function (b) {
      if (opts.minScore != null && Number(b.score) < Number(opts.minScore)) return false;
      if (opts.maxScore != null && Number(b.score) > Number(opts.maxScore)) return false;
      if (opts.hideRetired && b.retired) return false;
      if (opts.text) {
        var needle = String(opts.text).toLowerCase();
        if (String(b.text).toLowerCase().indexOf(needle) < 0) return false;
      }
      if (opts.source && b.source !== opts.source) return false;
      return true;
    });
  }

  window.ARC = window.ARC || {};
  window.ARC.PolicyBullet = {
    render: render,
    renderList: renderList,
    scoreTier: scoreTier,
    sortBy: sortBy,
    filterBy: filterBy,
  };
})();
