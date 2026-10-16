"""Static inline CSS and JS constants for the v2 findings dashboard.

All assets are module-level constants (FR-013 determinism, FR-011 no network).
The CSS palette, page layout, focus/motion rules, and the interaction
JavaScript (nav focus, filter/search, sort, tooltip) live here as strings
emitted verbatim into site/index.html by render.py.
"""

from __future__ import annotations

COLORS: dict[str, str] = {
    "bg": "#ffffff",
    "text": "#1a1a1a",
    "secondary": "#595959",
    "link": "#0b5ed7",
    "badge-shipped-text": "#155724",
    "badge-shipped-bg": "#d4edda",
    "badge-on-trial-text": "#856404",
    "badge-on-trial-bg": "#fff3cd",
    "badge-retired-text": "#383d41",
    "badge-retired-bg": "#e2e3e5",
    "badge-dead-end-text": "#721c24",
    "badge-dead-end-bg": "#f8d7da",
    "badge-open-text": "#0c5460",
    "badge-open-bg": "#d1ecf1",
}

STYLE = """<style>
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       margin: 0; padding: 0; color: #1a1a1a; background: #ffffff;
       line-height: 1.5; }
.skip { position: absolute; left: -9999px; top: 0; z-index: 100;
        padding: .5rem 1rem; background: #0b5ed7; color: #fff; }
.skip:focus { left: 0; }
header { max-width: 56rem; margin: 0 auto; padding: 1.5rem 1rem .5rem; }
h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
h2 { font-size: 1.2rem; border-bottom: 1px solid #ccc; padding-bottom: .3rem;
     margin-top: 2rem; }
h3 { font-size: 1rem; margin-top: 1.2rem; }
h4 { font-size: .95rem; margin-top: 1rem; }
.stamp { color: #595959; font-size: .85rem; font-weight: normal; }
.bar { position: sticky; top: 0; background: #ffffff; border-bottom: 1px solid #ddd;
       padding: .5rem 1rem; z-index: 10; display: flex; flex-wrap: wrap;
       gap: .5rem; align-items: center; }
.bar nav { display: flex; gap: .75rem; }
.bar nav a { color: #0b5ed7; text-decoration: none; font-weight: 500; }
.bar nav a:hover { text-decoration: underline; }
.controls { display: flex; flex-wrap: wrap; gap: .4rem; align-items: center;
            margin-left: auto; }
.controls label { font-size: .8rem; color: #595959; display: flex;
                  align-items: center; gap: .2rem; }
.controls select, .controls input[type="search"] {
    font-size: .8rem; padding: .2rem .3rem; border: 1px solid #ccc;
    border-radius: .2rem; }
.controls button { font-size: .8rem; padding: .2rem .5rem; border: 1px solid #ccc;
                   border-radius: .2rem; background: #f5f5f5; cursor: pointer; }
.controls button:hover { background: #e8e8e8; }
#f-count { font-size: .8rem; color: #595959; margin: 0; }
main { max-width: 56rem; margin: 0 auto; padding: 0 1rem 3rem; }
section { margin-top: 1.5rem; }
table { border-collapse: collapse; margin: .5rem 0 1rem; width: 100%; }
th, td { border: 1px solid #ddd; padding: .35rem .6rem; text-align: left;
         vertical-align: top; word-wrap: break-word; overflow-wrap: break-word; }
th { background: #f5f5f5; }
th button { font: inherit; font-weight: 600; background: none; border: none;
            cursor: pointer; padding: 0; color: inherit; }
th button:hover { text-decoration: underline; }
.badge { display: inline-block; padding: .1rem .5rem; border-radius: .3rem;
         font-size: .8rem; font-weight: 600; border: 1px solid; }
.badge-shipped { background: #d4edda; color: #155724; border-color: #155724; }
.badge-on-trial { background: #fff3cd; color: #856404; border-color: #856404; }
.badge-retired { background: #e2e3e5; color: #383d41; border-color: #383d41; }
.badge-dead-end { background: #f8d7da; color: #721c24; border-color: #721c24; }
.badge-open { background: #d1ecf1; color: #0c5460; border-color: #0c5460; }
.marker-stale { background: #f8d7da; color: #721c24; padding: .1rem .4rem;
                border-radius: .3rem; font-size: .8rem; font-weight: 600;
                border: 1px solid #721c24; }
.marker-none { background: #e2e3e5; color: #383d41; padding: .1rem .4rem;
               border-radius: .3rem; font-size: .8rem; border: 1px solid #383d41; }
.empty-state { color: #595959; font-style: italic; }
a { color: #0b5ed7; }
.tier { font-size: .75rem; text-transform: uppercase; letter-spacing: .05em;
        color: #595959; font-weight: 700; }
.details { font-size: .8rem; color: #595959; margin: .1rem 0 .4rem; }
.details b { color: #1a1a1a; }
#tooltip { display: none; position: fixed; z-index: 100; max-width: 28rem;
           background: #222; color: #fff; padding: .6rem .8rem;
           border-radius: .4rem; font-size: .85rem; line-height: 1.4;
           white-space: normal; word-wrap: break-word; }
#tooltip .tt-title { font-weight: 600; margin-bottom: .2rem; }
#tooltip a { color: #9ecbff; }
svg text { font-size: 11px; fill: #1a1a1a; }
.bar-rect { fill: #4dabf7; }
.bar-rect:hover, .bar-rect:focus { fill: #1971c2; }
:focus-visible { outline: 2px solid #0b5ed7; outline-offset: 2px; }
@media (prefers-reduced-motion: reduce) {
  html { scroll-behavior: auto; }
  *, *::before, *::after { transition: none !important; animation: none !important; }
}
</style>"""

SCRIPTS = """<script>
(function () {
  var tip = document.getElementById('tooltip');
  var current = null;
  function show(el) {
    if (current === el) return;
    hide();
    current = el;
    tip.innerHTML = el.getAttribute('data-tooltip') || '';
    tip.style.display = 'block';
  }
  function hide() {
    if (current) current.blur && current.blur();
    current = null;
    tip.style.display = 'none';
  }
  document.addEventListener('mouseover', function (e) {
    var el = e.target.closest('[data-tooltip]');
    if (el) show(el);
  });
  document.addEventListener('mouseout', function (e) {
    var el = e.target.closest('[data-tooltip]');
    if (el && current === el) hide();
  });
  document.addEventListener('focusin', function (e) {
    var el = e.target.closest('[data-tooltip]');
    if (el) show(el);
  });
  document.addEventListener('focusout', function (e) {
    var el = e.target.closest('[data-tooltip]');
    if (el && current === el) hide();
  });
  document.addEventListener('click', function (e) {
    var el = e.target.closest('[data-tooltip]');
    if (el && current === el && !e.target.closest('a')) hide();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') hide();
  });

  var navLinks = document.querySelectorAll('nav[aria-label="Views"] a');
  navLinks.forEach(function (link) {
    link.addEventListener('click', function (e) {
      var target = document.querySelector(link.getAttribute('href'));
      if (target) {
        var heading = target.querySelector('h2');
        if (heading) {
          e.preventDefault();
          target.scrollIntoView({ behavior: 'smooth' });
          heading.focus();
        }
      }
    });
  });

  var fModel = document.getElementById('f-model');
  var fTier = document.getElementById('f-tier');
  var fStatus = document.getElementById('f-status');
  var fText = document.getElementById('f-text');
  var fClear = document.getElementById('f-clear');
  var fCount = document.getElementById('f-count');
  if (fModel && fTier && fStatus && fText && fClear && fCount) {
    var allFindings = Array.prototype.slice.call(
      document.querySelectorAll('.finding'));
    var noMatchLineup = document.getElementById('no-match-lineup');
    var noMatchBoard = document.getElementById('no-match-board');
    var noMatchDeadends = document.getElementById('no-match-dead-ends');
    var noMatchAll = [noMatchLineup, noMatchBoard, noMatchDeadends].filter(Boolean);
    function applyFilter() {
      var mv = fModel.value;
      var tv = fTier.value;
      var sv = fStatus.value;
      var qv = (fText.value || '').toLowerCase().trim();
      var visible = 0;
      allFindings.forEach(function (el) {
        var dm = (el.getAttribute('data-model') || '').split(' ');
        var dt = el.getAttribute('data-tier') || '';
        var ds = el.getAttribute('data-status') || '';
        var text = ((el.getAttribute('data-title') || '') + ' ' +
                    (el.getAttribute('data-rationale') || '')).toLowerCase();
        var ok = true;
        if (mv !== 'all' && dm.indexOf(mv) === -1) ok = false;
        if (tv !== 'all' && dt !== tv) ok = false;
        if (sv !== 'all' && ds !== sv) ok = false;
        if (ok && qv && text.indexOf(qv) === -1) ok = false;
        el.hidden = !ok;
        if (ok) visible++;
      });
      fCount.textContent = visible + ' of ' + allFindings.length + ' findings';
      var views = ['lineup', 'board', 'dead-ends'];
      views.forEach(function (vid) {
        var section = document.getElementById(vid);
        var nm = document.getElementById('no-match-' + vid);
        if (!section || !nm) return;
        var anyVisible = false;
        section.querySelectorAll('.finding:not([hidden])').forEach(function () {
          anyVisible = true;
        });
        if (!anyVisible) {
          var empty = section.querySelector('.empty-state');
          if (!empty || empty.hidden) {
            nm.hidden = false;
          }
        } else {
          nm.hidden = true;
        }
      });
    }
    function clearFilters() {
      fModel.value = 'all';
      fTier.value = 'all';
      fStatus.value = 'all';
      fText.value = '';
      applyFilter();
    }
    fModel.addEventListener('change', applyFilter);
    fTier.addEventListener('change', applyFilter);
    fStatus.addEventListener('change', applyFilter);
    fText.addEventListener('input', applyFilter);
    fClear.addEventListener('click', clearFilters);
    noMatchAll.forEach(function (nm) {
      var btn = nm.querySelector('.no-match-clear');
      if (btn) btn.addEventListener('click', clearFilters);
    });
  }

  var sortBtns = document.querySelectorAll('th button');
  sortBtns.forEach(function (btn) {
    btn.addEventListener('click', function () {
      var th = btn.parentElement;
      var table = th.closest('table');
      if (!table) return;
      var colIdx = Array.prototype.indexOf.call(
        th.parentElement.children, th);
      var rows = Array.prototype.slice.call(
        table.querySelectorAll('tr')).filter(function (r) {
        return r.children.length > colIdx &&
               !r.querySelector('th');
      });
      if (rows.length <= 1) return;
      var ths = table.querySelectorAll('tr th');
      var isNumeric = th.hasAttribute('data-sort');
      var currentSort = th.getAttribute('aria-sort');
      var dir;
      if (currentSort === 'none') {
        if (isNumeric) {
          dir = th.getAttribute('data-sort') === 'higher' ? 'descending' : 'ascending';
        } else {
          dir = 'ascending';
        }
      } else if (currentSort === 'ascending') {
        dir = 'descending';
      } else {
        dir = 'ascending';
      }
      ths.forEach(function (t) {
        t.setAttribute('aria-sort', 'none');
        var b = t.querySelector('button');
        if (b) b.textContent = b.textContent.replace(/\s*[↑↓]\s*$/, '');
      });
      th.setAttribute('aria-sort', dir);
      btn.textContent = btn.textContent +
        (dir === 'ascending' ? ' \u2191' : ' \u2193');
      var tbody = table;
      rows.sort(function (a, b) {
        var cellA = a.children[colIdx];
        var cellB = b.children[colIdx];
        var vA = cellA.getAttribute('data-value');
        var vB = cellB.getAttribute('data-value');
        if (vA === null && vB === null) return 0;
        if (vA === null) return 1;
        if (vB === null) return -1;
        if (isNumeric) {
          var nA = parseFloat(vA);
          var nB = parseFloat(vB);
          return dir === 'ascending' ? nA - nB : nB - nA;
        }
        var sA = cellA.textContent || '';
        var sB = cellB.textContent || '';
        return dir === 'ascending'
          ? sA.localeCompare(sB)
          : sB.localeCompare(sA);
      });
      rows.forEach(function (r) { tbody.appendChild(r); });
    });
  });
})();
</script>"""
