/* StockBot UI: presentation only; no requests, price calculations, or persistence. */
(function () {
  'use strict';
  if (window.StockBotUI) return;
  var motion = window.matchMedia('(prefers-reduced-motion: reduce)');
  var reduced = function () { return motion.matches || document.hidden; };
  function duration(name,fallback){var raw=getComputedStyle(document.documentElement).getPropertyValue('--ui-'+name).trim(),value=parseFloat(raw);return Number.isFinite(value)?value*(raw.endsWith('ms')?1:1000):fallback;}
  var root = function () { return document.getElementById('app-page-content'); };
  var notification=null, notificationTimer=0;
  var route = null, scanFrame = 0, observer = null;
  var numberRecords = new WeakMap(), activeNumbers = new Map(), detailsAnimations = new Map();
  var loadStates = new Map(), formTimers = new Map(), chartCleanups = new Set();
  var controls = 'button,a[href],summary,[role="button"],label[for]';
  var numberSelector = '[data-ui-number],[data-home-sensitive],[data-home-market-pct],[data-home-portfolio-pct],[data-home-relative],[data-position-price],[data-position-pct],[data-position-day-pl],.stat .num,.stat-value,.totals .total-value,.totals .num,.position-card-primary b,.portfolio-trend-stat strong,.rank-return,[data-allocation-center-value],[data-allocation-center-weight]';
  function el(tag, cls, text) { var n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; }
  function each(scope, sel, fn) { if (!scope) return; scope.querySelectorAll(sel).forEach(fn); }
  function enter(node, cls) {
    if (!node || reduced()) return;
    node.classList.remove(cls); void node.offsetWidth; node.classList.add(cls);
    node.addEventListener('animationend', function end() { node.classList.remove(cls); node.removeEventListener('animationend', end); });
  }
  function skeleton(label, inline) {
    var box = el('div', inline ? 'ui-load-inline' : 'ui-feedback-route');
    box.setAttribute('role', 'status'); box.setAttribute('aria-live', 'polite');
    box.appendChild(el(inline ? 'span' : 'p', '', label || '正在載入…'));
    var grid = el('div', inline ? '' : 'ui-skeleton-grid'); grid.setAttribute('aria-hidden', 'true');
    ['ui-skeleton', 'ui-skeleton', 'ui-skeleton wide'].forEach(function (cls) { grid.appendChild(el('div', cls)); });
    box.appendChild(grid); return box;
  }
  function load(host, label) {
    if (!host || loadStates.has(host)) return;
    var box = skeleton(label, true); host.replaceChildren(box); host.setAttribute('aria-busy', 'true');
    var state = { box: box, timer: 0 }; loadStates.set(host, state);
    state.timer = setTimeout(function () {
      if (!host.isConnected || !host.contains(box)) return endLoad(host);
      box.classList.add('ui-loading-ended'); box.replaceChildren(el('span', '', '讀取時間較長，請稍候；可收合後重新開啟。'));
      host.removeAttribute('aria-busy');
    }, 20000);
  }
  function endLoad(host) { var state = loadStates.get(host); if (state) clearTimeout(state.timer); loadStates.delete(host); if (host) host.removeAttribute('aria-busy'); }
  function routeEnd(ok, message) {
    if (!route) return;
    var old = route; route = null; clearTimeout(old.timer);
    if (old.content) { old.content.classList.remove('ui-route-pending'); old.content.removeAttribute('aria-busy'); }
    each(document, '.ui-nav-pending', function (n) { n.classList.remove('ui-nav-pending'); n.removeAttribute('aria-busy'); });
    if (old.box.isConnected) old.box.remove();
    if (!ok && old.content && old.content.isConnected) {
      var error = el('div', 'ui-feedback-route'); error.dataset.state = 'error'; error.setAttribute('role', 'alert');
      error.appendChild(el('p', '', message || '頁面未能載入，保留原本內容。請再次點選分頁重試。'));
      var dismiss = el('button', '', '繼續查看原頁面'); dismiss.type = 'button'; dismiss.onclick = function () { error.remove(); };
      error.appendChild(dismiss); old.content.before(error);
    }
  }
  function routeStart(label, path, cancel) {
    routeEnd(true); each(document, '.ui-feedback-route[data-state=error]', function (n) { n.remove(); });
    var content = root(); if (!content) return;
    var box = skeleton(label, false); content.before(box); content.classList.add('ui-route-pending');
    each(document, 'a[data-app-nav]', function (a) { if (new URL(a.href, location.href).pathname === path) { a.classList.add('ui-nav-pending'); a.setAttribute('aria-busy', 'true'); } });
    var state = { box: box, content: content, timer: 0 }; route = state;
    state.timer = setTimeout(function () {
      if (route !== state) return;
      box.classList.add('ui-loading-ended'); box.replaceChildren(el('p', '', '載入時間較長，原頁資料仍保留。'));
      var button = el('button', '', cancel ? '取消等待' : '收起等待提示'); button.type = 'button'; button.onclick = function () { if (cancel) { cancel(); routeEnd(false, '已取消等待，原頁面仍可使用。'); } else routeEnd(true); };
      box.appendChild(button);
      // A pending network response never leaves an infinite skeleton.
      content.classList.remove('ui-route-pending');
    }, 18000);
  }
  function notify(message,state){
    if(notification)notification.remove();clearTimeout(notificationTimer);
    notification=el('div','ui-global-status',message);notification.dataset.state=state||'success';notification.setAttribute('role',state==='error'?'alert':'status');
    var close=el('button','','關閉');close.type='button';close.onclick=function(){if(notification)notification.remove();notification=null;clearTimeout(notificationTimer);};notification.appendChild(close);document.body.appendChild(notification);
    notificationTimer=setTimeout(function(){if(notification)notification.remove();notification=null;},8000);
  }
  function formState(form, state, message) {
    if (!form) return;
    if(formTimers.has(form)){clearTimeout(formTimers.get(form));formTimers.delete(form);}
    if(state==='loading')formTimers.set(form,setTimeout(function(){formTimers.delete(form);if(form.isConnected)formState(form,'waiting','尚未收到處理結果，請先確認持股或設定是否已儲存，避免重複送出。');},30000));
    var status = form.querySelector('.ui-form-status');
    if (!status) { status = el('div', 'ui-form-status'); form.appendChild(status); }
    status.dataset.state = state; status.setAttribute('role', state === 'error' ? 'alert' : 'status');
    status.replaceChildren(); if (state === 'loading') { var spin = el('i', 'ui-status-spinner'); spin.setAttribute('aria-hidden', 'true'); status.appendChild(spin); }
    status.appendChild(el('span', '', message || ({ loading: '處理中，請稍候…', success: '已完成', error: '未能完成，請檢查輸入內容。' })[state]));
    form.classList.toggle('ui-form-loading', state === 'loading'); form.dataset.uiState = state;
    if (state === 'loading') form.setAttribute('aria-busy', 'true'); else form.removeAttribute('aria-busy');
  }
  function parseNumber(text) {
    var m = String(text).trim().match(/^([+-]?)(\d[\d,]*(?:\.\d+)?)(\s*(?:%|元|倍|個百分點)?)$/);
    if (!m) return null; var n = Number(m[1] + m[2].replace(/,/g, ''));
    return Number.isFinite(n) ? { value: n, digits: (m[2].split('.')[1] || '').length, plus: m[1] === '+', suffix: m[3], grouping: m[2].includes(',') } : null;
  }
  function stopNumber(target) {
    var a = activeNumbers.get(target); if (!a) return;
    cancelAnimationFrame(a.frame); a.overlay.remove(); target.classList.remove('ui-number-source'); activeNumbers.delete(target);
  }
  function stopNumbers() { Array.from(activeNumbers.keys()).forEach(stopNumber); }
  function tweenNumber(target, from, to) {
    var previousAnimation=activeNumbers.get(target); if(previousAnimation && previousAnimation.current!=null)from={value:previousAnimation.current};
    stopNumber(target); if (reduced() || activeNumbers.size >= 12 || !target.isConnected) return;
    var rect = target.getBoundingClientRect(); if (rect.bottom < 0 || rect.top > innerHeight || rect.width <= 0 || rect.height <= 0) return;
    var style = getComputedStyle(target); if (style.visibility === 'hidden') return;
    var overlay = el('span', 'ui-number-tween'); overlay.setAttribute('aria-hidden', 'true');
    ['fontFamily', 'fontSize', 'fontWeight', 'fontStyle', 'lineHeight', 'letterSpacing', 'color', 'textAlign'].forEach(function (key) { overlay.style[key] = style[key]; });
    overlay.style.left = rect.left + 'px'; overlay.style.top = rect.top + 'px'; overlay.style.width = rect.width + 'px';
    document.body.appendChild(overlay); target.classList.add('ui-number-source');
    var animation = { overlay: overlay, frame: 0 }, started = performance.now(), milliseconds=duration('number',400); activeNumbers.set(target, animation);
    function frame(now) {
      if (!target.isConnected || reduced()) return stopNumber(target);
      var progress = Math.min(1, (now - started) / Math.max(1,milliseconds)), eased = 1 - Math.pow(1 - progress, 3);
      var value = from.value + (to.value - from.value) * eased; animation.current=value;
      overlay.textContent = (to.plus && value >= 0 ? '+' : '') + value.toLocaleString('zh-TW', { minimumFractionDigits: to.digits, maximumFractionDigits: to.digits, useGrouping: to.grouping }) + to.suffix;
      if (progress < 1) animation.frame = requestAnimationFrame(frame); else stopNumber(target);
    }
    frame(started);
  }
  function scanNumbers(scope) {
    each(scope, numberSelector, function (host) {
      if (host.closest('svg,[aria-hidden=true],.ui-chart-readout,.ui-form-status') || host.matches('input')) return;
      var target = host.querySelector('.num') || host;
      if (target.childElementCount) return;
      var next = parseNumber(target.textContent), previous = numberRecords.get(host);
      numberRecords.set(host, next); if (!next) { stopNumber(target); return; }
      if (previous && previous.value !== next.value && previous.suffix === next.suffix) tweenNumber(target, previous, next);
    });
  }
  function selectedControls(scope) {
    each(scope,'.portfolio-trend-tab',function(label){var input=document.getElementById(label.htmlFor);label.classList.toggle('ui-selected-control',!!input&&input.checked);});

    each(scope, 'button[aria-pressed],#rankTabs button,.wb-asset-tabs button,.wb-tabs button,.wb-lab-strategy', function (n) {
      var isOn = n.getAttribute('aria-pressed') === 'true' || n.classList.contains('on') || n.classList.contains('active');
      n.classList.toggle('ui-selected-control', isOn);
    });
    each(document, 'a[data-app-nav]', function (a) { if (a.classList.contains('on')) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); });
  }
  function prepareDetails(scope) { each(scope, 'details', function (d) { var summary = Array.from(d.children).find(function (x) { return x.tagName === 'SUMMARY'; }); if (summary) { d.classList.add('ui-disclosure'); summary.classList.add('ui-disclosure-summary'); summary.setAttribute('aria-expanded', String(d.open)); } }); }
  var disclosureSizes = new WeakMap(), disclosureOpen = new WeakMap(), resizeBound = new WeakSet();
  function animateSize(node, start, end) {
    var old = detailsAnimations.get(node); if (old) { old.cancel(); detailsAnimations.delete(node); }
    if (reduced() || !node.animate || Math.abs(end-start)<1) return;
    var overflow=node.style.overflow;node.style.overflow='hidden';
    var a=node.animate([{height:start+'px'},{height:end+'px'}],{duration:duration('expand',220),easing:'cubic-bezier(.2,.7,.2,1)'});detailsAnimations.set(node,a);
    function finish(){if(detailsAnimations.get(node)===a){node.style.overflow=overflow;detailsAnimations.delete(node);disclosureSizes.set(node,node.getBoundingClientRect().height);}}
    a.onfinish=finish;a.oncancel=finish;
  }
  var resizeObserver = window.ResizeObserver ? new ResizeObserver(function(entries){entries.forEach(function(entry){var d=entry.target,wasOpen=disclosureOpen.get(d);disclosureOpen.set(d,d.open);if(detailsAnimations.has(d))return;var next=d.getBoundingClientRect().height,previous=disclosureSizes.get(d);disclosureSizes.set(d,next);if((d.open||wasOpen)&&previous!=null)animateSize(d,previous,next);});}) : null;
  function toggleDetails(d) {
    var summary=d.querySelector('summary'),start=d.getBoundingClientRect().height;
    d.open=!d.open;summary.setAttribute('aria-expanded',String(d.open));
    animateSize(d,start,d.getBoundingClientRect().height);
  }
  function allocationSelect(code) {
    var scope = root(), allocation = scope && scope.querySelector('.position-allocation'); if (!allocation) return;
    each(scope, '.position-card[data-position-code]', function (card) { card.classList.toggle('ui-allocation-selected', !!code && card.dataset.positionCode === code); });
    each(allocation, '.allocation-slice', function (slice) { slice.setAttribute('aria-pressed', String(slice.dataset.allocationCode === code)); });
    each(scope, '[data-ui-allocation-select]', function (b) { b.setAttribute('aria-pressed', String(b.dataset.uiAllocationSelect === code)); });
  }
  function prepareAllocation(scope) {
    var allocation = scope.querySelector('.position-allocation'); if (!allocation) return;
    each(scope, '.position-card[data-position-code]', function (card) {
      if (card.querySelector('[data-ui-allocation-select]')) return;
      var chip=card.querySelector('.position-card-code');if(!chip)return;
      var button = el('button', 'position-card-code ui-allocation-row-select', chip.textContent); button.type = 'button'; button.dataset.uiAllocationSelect = card.dataset.positionCode; button.setAttribute('aria-pressed', 'false');button.setAttribute('aria-label',card.dataset.positionCode+'，在配置圓環中選取');button.title='在配置圓環中選取';chip.replaceWith(button);
    });
  }
  function chartPoints(svg) {
    if (svg.matches('.portfolio-trend-svg')) return Array.from(svg.querySelectorAll('.trend-hit-point')).map(function (point) { return { x: Number(point.getAttribute('cx')), y: Number(point.getAttribute('cy')), label: point.dataset.date, text: '組合 ' + point.dataset.port + ' · 加權 ' + point.dataset.market + ' · 相對 ' + point.dataset.excess, node: point }; });
    var output = []; each(svg, '[data-ui-points]', function (path) { if (path.style.display === 'none') return; try { JSON.parse(path.dataset.uiPoints).forEach(function (p) { output.push({ x: p.x, y: p.y, label: p.date, text: p.name + ' ' + (p.value >= 0 ? '+' : '') + Number(p.value).toFixed(2) + '%', node: path }); }); } catch (_) {} }); return output;
  }
  function bindChart(svg) {
    if (svg.dataset.uiChartBound) return; svg.dataset.uiChartBound = '1';
    var panel = svg.closest('.portfolio-trend-panel') || svg.parentElement;
    var readout = el('div', 'ui-chart-readout '+(svg.matches('.portfolio-trend-svg')?'ui-portfolio-readout':'ui-rank-readout')), values = el('span', 'ui-chart-values', '滑鼠移入或開啟觸控追蹤，查看資料點');
    var button = el('button', '', '觸控追蹤'); button.type = 'button'; button.setAttribute('aria-pressed', 'false'); readout.append(values, button);
    var host = svg.closest('.portfolio-trend-plot') || svg; host.before(readout);
    var ns = 'http://www.w3.org/2000/svg', guide = document.createElementNS(ns, 'line'), dot = document.createElementNS(ns, 'circle');
    guide.setAttribute('class', 'ui-chart-crosshair'); dot.setAttribute('class', 'ui-chart-dot'); dot.setAttribute('r', '5'); guide.style.display = dot.style.display = 'none'; svg.append(guide, dot);
    svg.setAttribute('tabindex', '0'); svg.setAttribute('aria-label', (svg.getAttribute('aria-label') || '走勢圖') + '，左右方向鍵追蹤資料');
    var tracking = false, frame = 0, lastX = null, selected = 0;
    function clear() { guide.style.display = dot.style.display = 'none'; each(svg, '.ui-chart-point-active', function (n) { n.classList.remove('ui-chart-point-active'); }); }
    function show(index) {
      var points = chartPoints(svg); if (!points.length) return;
      selected = Math.max(0, Math.min(points.length - 1, index)); var p = points[selected];
      var same = points.filter(function (other) { return Math.abs(other.x - p.x) < .1; });
      values.textContent = p.label + ' · ' + same.map(function (v) { return v.text; }).join(' ／ ');
      guide.setAttribute('x1', p.x); guide.setAttribute('x2', p.x); guide.setAttribute('y1', '0'); guide.setAttribute('y2', svg.viewBox.baseVal.height); dot.setAttribute('cx', p.x); dot.setAttribute('cy', p.y); guide.style.display = dot.style.display = '';
      each(svg, '.ui-chart-point-active', function (n) { n.classList.remove('ui-chart-point-active'); }); if (p.node) p.node.classList.add('ui-chart-point-active');
    }
    function nearest(x) { var points = chartPoints(svg), best = 0, distance = Infinity; points.forEach(function (p, i) { var d = Math.abs(p.x - x); if (d < distance) { best = i; distance = d; } }); show(best); }
    function move(e) {
      if (e.pointerType === 'touch' && !tracking) return;
      lastX = e.clientX;
      if (frame) return; frame = requestAnimationFrame(function () { frame = 0; if (!svg.isConnected) return; var r = svg.getBoundingClientRect(); if (r.width) nearest((lastX - r.left) / r.width * svg.viewBox.baseVal.width); });
    }
    button.onclick = function () { tracking = !tracking; button.setAttribute('aria-pressed', String(tracking)); svg.classList.toggle('ui-chart-tracking', tracking); if (!tracking) clear(); };
    function key(e) { if (e.key === 'Escape') { clear(); return; } if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { e.preventDefault(); var points=chartPoints(svg),current=points[selected];if(!current)return;var direction=e.key==='ArrowLeft'?-1:1,next=selected,distance=Infinity;points.forEach(function(p,i){var delta=(p.x-current.x)*direction;if(delta>.1&&delta<distance){distance=delta;next=i;}});show(next); } }
    svg.addEventListener('pointermove', move, { passive: true }); svg.addEventListener('pointerdown', move, { passive: true }); svg.addEventListener('keydown', key);
    var leave = function () { if (!tracking) clear(); }; svg.addEventListener('pointerleave', leave);
    var cleanup = function () { cancelAnimationFrame(frame); svg.removeEventListener('pointermove', move); svg.removeEventListener('pointerdown', move); svg.removeEventListener('keydown', key); svg.removeEventListener('pointerleave', leave); chartCleanups.delete(cleanup); };
    cleanup.svg = svg; chartCleanups.add(cleanup);
  }
  function rankFeedback(button) {
    var board = button.dataset.board; if (!board) return;
    var tabs = button.closest('#rankTabs'); if (!tabs) return;
    var status = tabs.nextElementSibling;
    if (!status || !status.classList.contains('ui-rank-feedback')) { status = el('div', 'ui-rank-feedback'); status.setAttribute('role', 'status'); tabs.after(status); }
    status.textContent = '已切換' + button.textContent.trim() + '，使用已載入快照';
    var shown = document.querySelector('[data-board-panel="' + board + '"]');
    if (shown) { enter(shown, 'ui-chart-period'); each(shown, '.rank-card', function (card, index) { if (index < 10) enter(card, 'ui-rank-enter'); }); each(shown, '.rank-move.up,.rank-move.down', function (badge) { enter(badge, 'ui-rank-moved'); }); }
    each(tabs, 'button[data-board]', function (b) { b.setAttribute('aria-pressed', String(b === button)); });
  }
  function scan() {
    scanFrame = 0; var scope = root(); if (!scope) return;
    ['#us-summary','#us-heat','#us-ranking'].forEach(function(sel){var host=scope.querySelector(sel);if(host&&!host.childElementCount&&!host.textContent.trim())load(host,'讀取已完成快照…');});
    each(scope,'.msg',function(box){if(box.dataset.uiOutcome)return;var text=box.textContent||'';var state=/失敗|錯誤|沒有儲存|不正確|過期|無法|請先|請填|未完成|⚠/.test(text)?'error':/成功|已儲存|已新增|已賣出|✅/.test(text)?'success':null;if(state){box.dataset.uiOutcome=state;box.dataset.state=state;box.classList.add('ui-form-status');box.setAttribute('role',state==='error'?'alert':'status');}});
    scanNumbers(scope); selectedControls(scope); prepareDetails(scope); prepareAllocation(scope);
    if(resizeObserver)each(scope,'details.ui-disclosure',function(d){if(!resizeBound.has(d)){resizeBound.add(d);disclosureOpen.set(d,d.open);disclosureSizes.set(d,d.getBoundingClientRect().height);resizeObserver.observe(d);}});
    each(scope, 'svg.portfolio-trend-svg,svg.ui-interactive-chart', bindChart);
    loadStates.forEach(function (state, host) { if (!host.isConnected || !host.contains(state.box)) endLoad(host); });
    chartCleanups.forEach(function (cleanup) { if (!cleanup.svg.isConnected) cleanup(); });
  }
  function schedule() { if (!scanFrame) scanFrame = requestAnimationFrame(scan); }
  document.addEventListener('pointerdown', function (e) { var target = e.target.closest(controls); if (!target || target.disabled || e.button > 0) return; if(target.matches('[data-home-privacy-toggle]'))stopNumbers(); target.classList.add('ui-pressed'); setTimeout(function () { target.classList.remove('ui-pressed'); }, duration('press',110)); }, { capture: true, passive: true });
  document.addEventListener('click',function(e){if(e.target.closest('[data-home-privacy-toggle]')){stopNumbers();return;}var trigger=e.target.closest('[data-home-chart-row],.hv2-hold-trigger,[data-hv2-more]');if(!trigger)return;var host=trigger.closest('.hv2-hold,.hv2-group')||trigger;var before=host.getBoundingClientRect().height;queueMicrotask(function(){if(host.isConnected)animateSize(host,before,host.getBoundingClientRect().height);});},true);
  document.addEventListener('click', function (e) {
    if(e.target.closest('[data-home-privacy-toggle]'))stopNumbers();
    var summary = e.target.closest('summary');
    if (summary && summary.parentElement.tagName === 'DETAILS' && !e.target.closest('a,input,button,label') && !e.defaultPrevented) { e.preventDefault(); toggleDetails(summary.parentElement); }
    var selected = e.target.closest('[data-allocation-select],[data-allocation-code]');
    if (selected) allocationSelect(selected.dataset.allocationSelect || selected.dataset.allocationCode);
    if (e.target.closest('[data-allocation-reset]')) allocationSelect(null);
    var rowButton = e.target.closest('[data-ui-allocation-select]');
    if (rowButton) { var code = rowButton.dataset.uiAllocationSelect, allocation = document.querySelector('.position-allocation'), legend = allocation && Array.from(allocation.querySelectorAll('[data-allocation-select]')).find(function (b) { return b.dataset.allocationSelect === code; }); if (legend) legend.click(); }
    var board = e.target.closest('#rankTabs button[data-board]'); if (board) rankFeedback(board);
    schedule();
  });
  document.addEventListener('change', function (e) { if (e.target.matches('.trend-period-radio')) { requestAnimationFrame(function () { each(root(), '.portfolio-trend-panel', function (p) { if (p.getClientRects().length) enter(p, 'ui-chart-period'); }); }); } schedule(); });
  document.addEventListener('toggle', function (e) { if (e.target.tagName === 'DETAILS') { var s = e.target.querySelector('summary'); if (s) s.setAttribute('aria-expanded', String(e.target.open)); } }, true);
  function cleanup() { formTimers.forEach(clearTimeout);formTimers.clear(); if(resizeObserver){resizeObserver.disconnect();resizeBound=new WeakSet();} stopNumbers(); detailsAnimations.forEach(function (a) { a.cancel(); }); detailsAnimations.clear(); chartCleanups.forEach(function (fn) { fn(); }); loadStates.forEach(function (_, host) { endLoad(host); }); cancelAnimationFrame(scanFrame); scanFrame = 0; }
  document.addEventListener('stockbot:pageleaving', cleanup);
  document.addEventListener('visibilitychange', function () { if (document.hidden) stopNumbers(); });
  window.addEventListener('scroll', stopNumbers, { capture: true, passive: true }); window.addEventListener('resize', stopNumbers, { passive: true });
  window.addEventListener('pagehide', function () { cleanup(); routeEnd(true); });
  window.addEventListener('pageshow', function () { each(document, 'form[data-submitted="1"]', function (form) { delete form.dataset.submitted; form.classList.remove('tap-loading'); each(form, '[data-old-text]', function (b) { b.disabled = false; if (b.tagName === 'INPUT') b.value = b.dataset.oldText; else b.textContent = b.dataset.oldText; }); formState(form, 'error', '操作狀態已重設，請先確認是否已儲存。'); }); schedule(); });
  window.StockBotUI = { routeStart: routeStart, routeEnd: routeEnd, load: load, endLoad: endLoad, formState: formState, notify: notify, scan: schedule, reducedMotion: reduced, cleanup: cleanup, parseNumber: parseNumber };
  function boot() { var scope = root(); if (!scope) return; observer = new MutationObserver(function(records){ if(records.every(function(r){var n=r.target.nodeType===1?r.target:r.target.parentElement;return n&&n.closest('.ui-chart-readout');}))return; schedule(); }); observer.observe(scope, { childList: true, characterData: true, subtree: true }); scan(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, { once: true }); else boot();
})();
