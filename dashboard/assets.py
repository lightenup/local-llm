"""Static inline CSS and JS constants for the v2 findings dashboard.

All assets are module-level constants (FR-013 determinism, FR-011 no network).
The CSS palette, page layout, focus/motion rules, and the interaction
JavaScript (nav focus, filter/search, sort, tooltip) live here as strings
emitted verbatim into site/index.html by render.py.
"""

from __future__ import annotations

COLORS: dict[str, str] = {
    "bg": "#ffffff",
    "surface": "#f6f7f9",
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

# prefers-color-scheme: dark. Same keys, held to the same WCAG AA ratios
# (tests/test_assets.py checks both palettes).
COLORS_DARK: dict[str, str] = {
    "bg": "#101318",
    "surface": "#191d24",
    "text": "#e6e8eb",
    "secondary": "#a3a9b3",
    "link": "#7ab7ff",
    "badge-shipped-text": "#9be5b0",
    "badge-shipped-bg": "#143322",
    "badge-on-trial-text": "#ffd479",
    "badge-on-trial-bg": "#3a2e0a",
    "badge-retired-text": "#d0d3d8",
    "badge-retired-bg": "#2c2f34",
    "badge-dead-end-text": "#ffb3ba",
    "badge-dead-end-bg": "#3d1418",
    "badge-open-text": "#8fdcec",
    "badge-open-bg": "#0c2f38",
}


def _vars(c: dict[str, str], border: str, chart: str, grid: str) -> str:
    return "".join(f" --{k}: {v};" for k, v in c.items()) + (
        f" --border: {border}; --chart: {chart}; --grid: {grid};")


STYLE = """<style>
:root {""" + _vars(COLORS, "#dde1e6", "#0b5ed7", "#e0e0e0") + """ color-scheme: light dark; }
@media (prefers-color-scheme: dark) {
  :root {""" + _vars(COLORS_DARK, "#2c323c", "#4d9cff", "#2c323c") + """ }
}
* { box-sizing: border-box; }
/* No CSS smooth scrolling: arriving from another page with a #fragment, a
   smooth scroll starts before the long page has laid out and lands short or
   past the target. The nav links scroll smoothly through scrollIntoView. */
[id] { scroll-margin-top: 6.5rem; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       margin: 0; padding: 0; color: var(--text); background: var(--bg);
       line-height: 1.5; }
.skip { position: absolute; left: -9999px; top: 0; z-index: 100;
        padding: .5rem 1rem; background: var(--link); color: var(--bg); }
.skip:focus { left: 0; }
header { max-width: 72rem; margin: 0 auto; padding: 1.5rem 1rem .5rem; }
.head-row { display: flex; flex-wrap: wrap; align-items: baseline;
            justify-content: space-between; gap: .5rem; }
h1 { font-size: 1.5rem; margin: 0 0 .25rem; letter-spacing: -.01em; }
h2 { font-size: 1.25rem; border-bottom: 1px solid var(--border);
     padding-bottom: .3rem; margin-top: 2.5rem; }
h3 { font-size: 1rem; margin-top: 1.5rem; }
h4 { font-size: .95rem; margin: 0 0 .4rem; }
.stamp { color: var(--secondary); font-size: .85rem; font-weight: normal; }
.running-link { font-size: .9rem; font-weight: 600; text-decoration: none;
                border: 1px solid var(--border); border-radius: 999px;
                padding: .15rem .7rem; background: var(--surface); }
.bar { position: sticky; top: 0; background: var(--bg);
       border-bottom: 1px solid var(--border);
       padding: .5rem 1rem; z-index: 10; display: flex; flex-wrap: wrap;
       gap: .5rem; align-items: center; }
.bar nav { display: flex; flex-wrap: wrap; gap: .25rem; }
.bar nav a { color: var(--link); text-decoration: none; font-weight: 500;
             padding: .2rem .6rem; border-radius: .4rem; }
.bar nav a:hover { background: var(--surface); }
.controls { display: flex; flex-wrap: wrap; gap: .4rem; align-items: center;
            margin-left: auto; }
.controls label { font-size: .8rem; color: var(--secondary); display: flex;
                  align-items: center; gap: .2rem; }
.controls select, .controls input[type="search"], .controls button {
    font: inherit; font-size: .8rem; padding: .2rem .4rem; color: var(--text);
    background: var(--surface); border: 1px solid var(--border);
    border-radius: .3rem; }
.controls button { cursor: pointer; }
#f-count { font-size: .8rem; color: var(--secondary); margin: 0; }
main { max-width: 72rem; margin: 0 auto; padding: 0 1rem 3rem; }
section { margin-top: 1.5rem; scroll-margin-top: 4rem; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(20rem, 1fr));
         gap: 1rem; margin: .75rem 0 1rem; }
div.finding, .suite { background: var(--surface); border: 1px solid var(--border);
                      border-radius: .6rem; padding: .9rem 1rem; min-width: 0; }
div.finding table { margin-bottom: 0; }
.tiers { align-items: start; }
.tier-col { display: grid; gap: 1rem; min-width: 0; }
.tier-col h3 { margin: 0; }
.suite { display: flex; flex-direction: column; }
.suite p { margin: .35rem 0; font-size: .9rem; }
.suite .meta { color: var(--secondary); font-size: .8rem; }
.suite .links { display: flex; flex-wrap: wrap; gap: .5rem; font-size: .85rem;
                margin-top: auto; padding-top: .5rem; }
.suite.not-run { opacity: .8; }
.suite:target, .finding:target { outline: 3px solid var(--link); outline-offset: 2px; }
table { border-collapse: collapse; margin: .5rem 0 1rem; width: 100%; }
th, td { border: 1px solid var(--border); padding: .35rem .6rem; text-align: left;
         vertical-align: top; word-wrap: break-word; overflow-wrap: break-word; }
th { background: var(--surface); }
th button { font: inherit; font-weight: 600; background: none; border: none;
            cursor: pointer; padding: 0; color: inherit; }
th button:hover { text-decoration: underline; }
.badge { display: inline-block; padding: .1rem .5rem; border-radius: .3rem;
         font-size: .8rem; font-weight: 600; border: 1px solid; }
.badge-shipped { background: var(--badge-shipped-bg); color: var(--badge-shipped-text);
  border-color: var(--badge-shipped-text); }
.badge-on-trial { background: var(--badge-on-trial-bg); color: var(--badge-on-trial-text);
  border-color: var(--badge-on-trial-text); }
.badge-retired { background: var(--badge-retired-bg); color: var(--badge-retired-text);
  border-color: var(--badge-retired-text); }
.badge-dead-end { background: var(--badge-dead-end-bg); color: var(--badge-dead-end-text);
  border-color: var(--badge-dead-end-text); }
.badge-open { background: var(--badge-open-bg); color: var(--badge-open-text);
  border-color: var(--badge-open-text); }
.marker-stale { background: var(--badge-dead-end-bg); color: var(--badge-dead-end-text);
                padding: .1rem .4rem; border-radius: .3rem; font-size: .8rem;
                font-weight: 600; border: 1px solid; }
.marker-none { background: var(--badge-retired-bg); color: var(--badge-retired-text);
               padding: .1rem .4rem; border-radius: .3rem; font-size: .8rem;
               border: 1px solid; }
.empty-state { color: var(--secondary); font-style: italic; }
a { color: var(--link); }
#board td a { color: var(--text); text-decoration-color: var(--border);
              text-underline-offset: .2em; }
#board td a:hover { text-decoration-color: var(--link); }
.tier { font-size: .75rem; text-transform: uppercase; letter-spacing: .05em;
        color: var(--secondary); font-weight: 700; }
.details { font-size: .8rem; color: var(--secondary); margin: .1rem 0 .4rem; }
.details b { color: var(--text); }
#tooltip { display: none; position: fixed; z-index: 100; max-width: 28rem;
           background: #222; color: #fff; padding: .6rem .8rem;
           border-radius: .4rem; font-size: .85rem; line-height: 1.4;
           white-space: normal; word-wrap: break-word; }
#tooltip .tt-title { font-weight: 600; margin-bottom: .2rem; }
#tooltip a { color: #9ecbff; }
.chart { overflow-x: auto; }
svg text { font-size: 11px; fill: var(--text); }
svg text[fill="#595959"] { fill: var(--secondary); }
svg line[stroke="#e0e0e0"] { stroke: var(--grid); }
svg rect.bar { fill: var(--chart); }
svg rect.bar:hover, svg rect.bar:focus { opacity: .8; }
progress { width: 100%; height: .9rem; accent-color: var(--chart); }
.run { display: grid; gap: .3rem; }
.run .nums { display: flex; flex-wrap: wrap; gap: .25rem 1.25rem; font-size: .9rem; }
.run .nums b { font-variant-numeric: tabular-nums; }
.banner { border: 1px solid; border-radius: .5rem; padding: .5rem .8rem;
          background: var(--badge-on-trial-bg); color: var(--badge-on-trial-text); }
/* ---- text and reading ---- */
.lede, .rationale, .roles-intro, .doc p, .doc li { max-width: 72ch; }
.lede { color: var(--secondary); }
.muted { color: var(--secondary); }
.num, td.num { font-variant-numeric: tabular-nums; }
table { font-variant-numeric: tabular-nums; }
.table-wrap { overflow-x: auto; }
.title-link { color: inherit; text-decoration: none; }
.title-link:hover { text-decoration: underline; color: var(--link); }
abbr[title] { text-decoration: underline dotted; text-underline-offset: .2em; cursor: help; }
.sr { position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0;
      clip: rect(0 0 0 0); white-space: nowrap; border: 0; }
.roleline { margin: -.2rem 0 .5rem; font-size: .85rem; }
details.how, details.more { margin-top: .5rem; font-size: .85rem; }
details.how summary, details.more summary { cursor: pointer; color: var(--link);
                                            font-size: .8rem; }
details.how .details { margin: .4rem 0; }
.details .metric-name { display: block; color: var(--text); }
.details .links { white-space: nowrap; }
.rationale { margin: .3rem 0; }
.finding.dead h3 { font-size: 1rem; margin: 0 0 .4rem; }
.finding.dead .links { font-size: .85rem; margin: .4rem 0 0; }
.chips { margin: 0 0 .3rem; font-size: .85rem; color: var(--secondary); }
.chip { display: inline-block; border: 1px solid var(--border); border-radius: 999px;
        padding: 0 .55rem; margin: .1rem .15rem .1rem 0; font-size: .8rem;
        background: var(--bg); color: var(--text); white-space: nowrap; }
.chip-pass { border-color: var(--badge-shipped-text); color: var(--badge-shipped-text);
             background: var(--badge-shipped-bg); }
.chip-fail { border-color: var(--badge-dead-end-text); color: var(--badge-dead-end-text);
             background: var(--badge-dead-end-bg); }
.chip-old { border-color: var(--badge-on-trial-text); color: var(--badge-on-trial-text);
            background: var(--badge-on-trial-bg); }
figure.chart { margin: 1rem 0; }
figure.chart figcaption { font-weight: 600; font-size: .95rem; margin-bottom: .3rem; }

/* ---- roles strip ---- */
.roles { margin: 1.5rem 0 0; }
.roles-intro { margin: 0 0 .6rem; font-size: .9rem; color: var(--secondary); }
.role-grid { list-style: none; padding: 0; margin: 0; display: grid; gap: .6rem;
             grid-template-columns: repeat(auto-fill, minmax(10.5rem, 1fr)); }
.role { background: var(--surface); border: 1px solid var(--border);
        border-left: 4px solid var(--chart); border-radius: .5rem;
        padding: .5rem .7rem; display: grid; gap: .1rem; min-width: 0; }
.role-name { font-weight: 700; font-size: 1.05rem; }
.role-model { font-size: .85rem; overflow-wrap: anywhere; }
.role-prof, .role-ctx { font-size: .75rem; color: var(--secondary); }

/* ---- pipeline ---- */
.pipeline { list-style: none; padding: 0; margin: .75rem 0 1rem; display: grid;
            grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr)); gap: .6rem;
            counter-reset: step; }
.step { position: relative; background: var(--surface); border: 1px solid var(--border);
        border-top: 4px solid var(--chart); border-radius: .5rem; padding: .6rem .75rem;
        display: grid; gap: .3rem; align-content: start; font-size: .85rem;
        counter-increment: step; }
.step-icon { font-size: 1.3rem; line-height: 1; }
.step-name { font-weight: 700; font-size: 1rem; }
.step-name::before { content: counter(step) ". "; color: var(--secondary); }
.step .cost { justify-self: start; font-size: .75rem; font-weight: 600;
              border: 1px solid var(--border); border-radius: 999px; padding: 0 .5rem;
              background: var(--bg); }
.step .cannot { color: var(--secondary); }
.step-gate { border-top-color: var(--badge-on-trial-text); }
.step-accuracy { border-top-color: var(--link); }
.step-decision { border-top-color: var(--badge-shipped-text); }

/* ---- candidates stepper ---- */
.legend { font-size: .8rem; color: var(--secondary); }
.dot-inline { display: inline-grid; place-items: center; width: 1.2rem; height: 1.2rem;
              border-radius: 50%; font-size: .75rem; margin: 0 .15rem 0 .6rem;
              border: 2px solid; }
table.cands th.model { font-weight: 600; overflow-wrap: anywhere; min-width: 10rem; }
.stepper { list-style: none; margin: 0; padding: 0; display: flex; gap: .2rem;
           align-items: flex-start; }
.stepper .dot { display: grid; justify-items: center; gap: .15rem; width: 3.6rem;
                font-size: .7rem; color: var(--secondary); position: relative; }
.stepper .dot > span[aria-hidden] { display: grid; place-items: center; width: 1.6rem;
                height: 1.6rem; border-radius: 50%; border: 2px solid; font-size: .85rem;
                font-weight: 700; background: var(--bg); }
.stepper .dot + .dot::before { content: ""; position: absolute; top: .8rem;
                right: calc(50% + .9rem); width: 2.2rem; height: 2px; background: var(--border); }
.dot-pass > span[aria-hidden], .dot-inline.dot-pass {
    color: var(--badge-shipped-text); background: var(--badge-shipped-bg); }
.dot-fail > span[aria-hidden], .dot-inline.dot-fail {
    color: var(--badge-dead-end-text); background: var(--badge-dead-end-bg); }
.dot-partial > span[aria-hidden], .dot-inline.dot-partial {
    color: var(--badge-on-trial-text); background: var(--badge-on-trial-bg); }
.dot-none > span[aria-hidden], .dot-inline.dot-none {
    color: var(--secondary); border-style: dashed; }
.notes { margin: 0; padding-left: 1rem; font-size: .85rem; }
.notes .score-row { list-style: none; margin-left: -1rem; display: flex; flex-wrap: wrap;
                    gap: .25rem; margin-bottom: .3rem; }
.chip.score { text-decoration: none; }
.chip.score:hover { border-color: var(--link); }
.chip small { color: var(--secondary); }
.flag { font-size: .7rem; font-weight: 700; border-radius: .25rem; padding: 0 .25rem;
        margin-left: .2rem; background: var(--badge-on-trial-bg); color: var(--badge-on-trial-text);
        border: 1px solid var(--badge-on-trial-text); }
.notes li { margin: .1rem 0; }

/* ---- glossary ---- */
.glossary { display: grid; grid-template-columns: max-content 1fr; gap: .4rem 1.2rem;
            max-width: 60rem; }
.glossary dt { font-weight: 700; }
.glossary dd { margin: 0; max-width: 72ch; }
.glossary dt:target, .glossary dt:target + dd { background: var(--badge-on-trial-bg); }

/* ---- findings document ---- */
.doc-layout { max-width: 84rem; margin: 0 auto; padding: 0 1rem 3rem; display: grid;
              grid-template-columns: 17rem minmax(0, 1fr); gap: 2rem; }
.toc { position: sticky; top: 0; align-self: start; max-height: 100vh; overflow-y: auto;
       padding: 1rem 0; font-size: .8rem; }
.toc-h { font-weight: 700; margin: 0 0 .4rem; }
.toc ol { list-style: none; padding: 0; margin: 0; }
.toc li { margin: .2rem 0; }
.toc a { text-decoration: none; }
.toc a:hover { text-decoration: underline; }
main.doc { max-width: none; padding: 0; margin: 0; }
.docpage header { max-width: 84rem; }
.doc h2 { scroll-margin-top: 1rem; }
.doc h2:target, .doc h3:target { background: var(--badge-on-trial-bg); }
.doc pre { background: var(--surface); border: 1px solid var(--border); border-radius: .4rem;
           padding: .6rem .8rem; overflow-x: auto; font-size: .8rem; }
.doc code { font-size: .85em; }
.doc blockquote { margin: .5rem 0; padding: .1rem 1rem; border-left: 4px solid var(--border);
                  color: var(--secondary); }
.doc table { width: auto; font-size: .85rem; }
.doc hr { border: none; border-top: 1px solid var(--border); margin: 2rem 0; }
@media (max-width: 52rem) {
  .doc-layout { grid-template-columns: 1fr; }
  .toc { position: static; max-height: none; }
  .glossary { grid-template-columns: 1fr; }
}
.plan-steps { list-style: none; padding: 0; margin: .6rem 0 0; display: grid; gap: .35rem; }
.plan-step { display: grid; grid-template-columns: 1.8rem 1fr; gap: .5rem; align-items: start;
             font-size: .9rem; }
.plan-glyph { display: grid; place-items: center; width: 1.6rem; height: 1.6rem;
              border-radius: 50%; border: 2px solid; font-weight: 700; font-size: .8rem; }
.plan-done .plan-glyph { color: var(--badge-shipped-text); background: var(--badge-shipped-bg); }
.plan-running .plan-glyph { color: var(--link); background: var(--bg); }
.plan-pending .plan-glyph { color: var(--secondary); border-style: dashed; }
.plan-missed .plan-glyph { color: var(--badge-dead-end-text); background: var(--badge-dead-end-bg); }
#plan .run, #benchmarks-running .run { max-width: 44rem; }
.plan-steps.sub { margin: .35rem 0 .2rem; }
.plan-steps.sub .plan-step { font-size: .8rem; grid-template-columns: 1.4rem 1fr; }
.plan-steps.sub .plan-glyph { width: 1.2rem; height: 1.2rem; font-size: .65rem; }
:focus-visible { outline: 2px solid var(--link); outline-offset: 2px; }
@media (prefers-reduced-motion: reduce) {
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
        if (b) b.textContent = b.textContent.replace(/\\s*[↑↓]\\s*$/, '');
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
