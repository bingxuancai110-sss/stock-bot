/* StockBot UI: presentation only; no requests, price calculations, or stored account data. */
(function () {
  'use strict';
  if (window.StockBotUI) return;
  var motion = window.matchMedia('(prefers-reduced-motion: reduce)');
  var reduced = function () { return motion.matches || document.hidden; };
  function duration(name,fallback){var raw=getComputedStyle(document.documentElement).getPropertyValue('--sb-feedback-'+name).trim(),value=parseFloat(raw);return Number.isFinite(value)?value*(raw.endsWith('ms')?1:1000):fallback;}
  var root = function () { return document.getElementById('app-page-content'); };
  var notification=null, notificationTimer=0;
  var route = null, scanFrame = 0, observer = null, fullScanPending = true;
  var pendingNumbers = new Set(), press = null, pressTimer = 0;
  var numberRecords = new WeakMap(), activeNumbers = new Map(), detailsAnimations = new Map();
  var loadStates = new Map(), formTimers = new Map(), chartCleanups = new Set();
  var nearObserver=null, revealObserver=null, nearJobs=new Map(), chartQueued=new WeakSet(), revealSeen=new WeakSet();
  var views=new Map();
  function whenNear(host, work) {
    if (!host || !host.isConnected) return;
    var rect=host.getBoundingClientRect();
    if (!window.IntersectionObserver || (rect.height>0 && rect.top<innerHeight+240 && rect.bottom>-240)) { work(); return; }
    if (!nearObserver) nearObserver=new IntersectionObserver(function(entries){entries.forEach(function(entry){
      if(!entry.isIntersecting)return;
      var jobs=nearJobs.get(entry.target)||[];nearJobs.delete(entry.target);nearObserver.unobserve(entry.target);
      if(entry.target.isConnected)jobs.forEach(function(fn){fn();});
    });},{rootMargin:'240px 0px'});
    if(!nearJobs.has(host))nearJobs.set(host,[]);
    nearJobs.get(host).push(work);nearObserver.observe(host);
  }
  var revealActive=new Set(), revealFrame=0;
  var revealCharts='svg.portfolio-trend-svg,svg.ui-interactive-chart,svg#market-kline,svg.us-signal-svg,svg.us-chart,svg.sa-chart,svg.portfolio-allocation-svg,[data-ui-reveal-chart]';
  function finishReveal(node){
    revealActive.delete(node);if(revealObserver)revealObserver.unobserve(node);
    node.classList.remove('ui-section-arrive');node.style.removeProperty('--ui-reveal-opacity');
  }
  function updateReveals(){
    revealFrame=0;if(document.hidden)return;
    var edge=innerHeight-110, distance=Math.min(180,innerHeight*.24);
    var atEnd=scrollY+innerHeight>=document.documentElement.scrollHeight-4;
    var updates=[];
    revealActive.forEach(function(node){
      if(!node.isConnected||motion.matches){updates.push({node:node,done:true});return;}
      var progress=Math.max(0,Math.min(1,(edge-node.getBoundingClientRect().top)/distance));
      var minimum=node.matches(revealCharts)?0.4:0.08;
      updates.push({node:node,done:progress>=1||atEnd,opacity:String(minimum+(1-minimum)*progress)});
    });
    // Complete every geometry read before writing styles for this frame.
    updates.forEach(function(update){if(update.done)finishReveal(update.node);else if(update.node.style.getPropertyValue('--ui-reveal-opacity')!==update.opacity)update.node.style.setProperty('--ui-reveal-opacity',update.opacity);});
  }
  function scheduleReveals(){if(!revealFrame&&!document.hidden)revealFrame=requestAnimationFrame(updateReveals);}
  function prepareViewport(scope) {
    if(!window.IntersectionObserver || reduced())return;
    if(!revealObserver)revealObserver=new IntersectionObserver(function(entries){entries.forEach(function(entry){
      if(entry.isIntersecting){revealActive.add(entry.target);}
      else if(entry.boundingClientRect.bottom<90){finishReveal(entry.target);}
      else revealActive.delete(entry.target);
    });scheduleReveals();},{rootMargin:'0px 0px 180px 0px'});
    var selector='h2,h3,.more-group-title,.hv2-feature-heading,.portfolio-trend-title,.us-section-head,.wb-d-section-head,.wb-row-main,.hv2-feature-launcher a>span,.more-item>span:not(.more-icon),.rank-primary-line .name,.us-rank-name,.us-study-title,[data-ui-reveal],'+revealCharts;
    function safe(section){
      return !section.closest('form,[role=alert],[role=status],[aria-live="assertive"]') &&
        (section.matches('.wb-row-main')||!section.closest('button,summary')) &&
        (section.matches(revealCharts)||!section.querySelector('svg,canvas')) &&
        !section.matches('[data-home-sensitive],[data-position-price],[data-position-net],[data-position-pct]') &&
        !section.querySelector('form,input,button,[data-home-sensitive],[data-position-price],[data-position-net],[data-position-pct]');
    }
    each(scope,selector,function(section){
      if(revealSeen.has(section))return;
      if(!safe(section))return;
      var parent=section.parentElement&&section.parentElement.closest(selector);
      if(parent&&scope.contains(parent)&&safe(parent))return;
      var rect=section.getBoundingClientRect();if(!rect.height)return;
      revealSeen.add(section);
      // Below-viewport editorial text and plots fade with their scroll position.
      // Quotes, plot controls/readouts and initially visible content stay immediate.
      var start=innerHeight-110-(section.matches(revealCharts)?Math.min(180,innerHeight*.24):0);
      if(rect.top>start){
        section.style.setProperty('--ui-reveal-opacity',section.matches(revealCharts)?'.4':'.08');
        section.classList.add('ui-section-arrive');revealObserver.observe(section);
      }
    });
  }
  function revealForInteraction(e){
    var target=e.target&&e.target.closest?e.target.closest(revealCharts):null;
    if(!target)return;
    // Restore clarity before chart handlers run; never intercept native gestures.
    revealSeen.add(target);finishReveal(target);
  }
  document.addEventListener('pointerdown',revealForInteraction,{capture:true,passive:true});
  document.addEventListener('focusin',revealForInteraction,true);
  document.addEventListener('keydown',revealForInteraction,true);
  function captureView(key) {
    var scope=root();if(!scope)return;
    var radios=[];each(scope,'input.trend-period-radio:checked',function(input){radios.push(input.id);});
    var benchmark=scope.querySelector('#trend-benchmark'),board=scope.querySelector('#rankTabs button.on');
    var state={y:window.scrollY||0,radios:radios,benchmark:benchmark?benchmark.checked:null,board:board?board.dataset.board:null};
    if(scope.querySelector('#hv2')){
      state.details={};
      each(scope,'details[data-home-view]',function(item){state.details[item.dataset.homeView]=item.open;});
      try{sessionStorage.setItem('stockbot-home-view',JSON.stringify(state));}catch(ignore){}
    }
    views.delete(key);views.set(key,state);
    if(views.size>16)views.delete(views.keys().next().value);
  }
  function restoreView(key) {
    var state=views.get(key),scope=root();if(!scope)return 0;
    if(!state&&scope.querySelector('#hv2')){
      try{state=JSON.parse(sessionStorage.getItem('stockbot-home-view')||'null');}catch(ignore){}
    }
    if(!state)return 0;
    if(state.details)each(scope,'details[data-home-view]',function(item){
      var open=state.details[item.dataset.homeView];
      if(typeof open==='boolean')item.open=open;
    });
    state.radios.forEach(function(id){var input=document.getElementById(id);if(input&&scope.contains(input)&&input.matches('input.trend-period-radio')){input.checked=true;input.dispatchEvent(new Event('change',{bubbles:true}));}});
    var benchmark=scope.querySelector('#trend-benchmark');if(benchmark&&state.benchmark!==null)benchmark.checked=state.benchmark;
    if(state.board)each(scope,'#rankTabs button[data-board]',function(button){if(button.dataset.board===state.board&&!button.classList.contains('on'))button.click();});
    schedule();return state.y;
  }
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
  function routeEnd(ok, message, retry) {
    if (!route) return;
    var old = route; route = null; clearTimeout(old.timer);
    if(ok && old.content && old.content.isConnected && !reduced()) {
      old.content.classList.remove('ui-route-arrive');
      requestAnimationFrame(function(){if(!route&&old.content.isConnected){old.content.classList.add('ui-route-arrive');setTimeout(function(){old.content.classList.remove('ui-route-arrive');},260);}});
    }
    if (old.content) { old.content.classList.remove('ui-route-pending'); old.content.removeAttribute('aria-busy'); }
    each(document, '.ui-nav-pending', function (n) { n.classList.remove('ui-nav-pending'); n.removeAttribute('aria-busy'); });
    if (old.box && old.box.isConnected) old.box.remove();
    document.dispatchEvent(new CustomEvent('stockbot:routecomplete'));
    if (ok === false && old.content && old.content.isConnected) {
      var error = el('div', 'ui-feedback-route'); error.dataset.state = 'error'; error.setAttribute('role', 'alert');
      error.appendChild(el('p', '', message || '頁面未能載入，保留原本內容。請再次點選分頁重試。'));
      if (typeof retry === 'function') {
        var retryButton = el('button', '', '重試載入'); retryButton.type = 'button';
        retryButton.onclick = function () { error.remove(); retry(); };
        error.appendChild(retryButton);
      }
      var dismiss = el('button', '', '繼續查看原頁面'); dismiss.type = 'button'; dismiss.onclick = function () { error.remove(); };
      error.appendChild(dismiss); old.content.before(error);
    }
  }
  function routeStart(label, path, cancel, retry) {
    routeEnd(null); each(document, '.ui-feedback-route[data-state=error]', function (n) { n.remove(); });
    var content = root(); if (!content) return;
    content.classList.remove('ui-route-arrive');
    // Keep the current page in place while the next fragment loads. The shared
    // top progress line and pending tab provide feedback without covering the
    // whole screen with a second skeleton card.
    content.classList.add('ui-route-pending'); content.setAttribute('aria-busy', 'true');
    each(document, 'a[data-app-nav]', function (a) { if (new URL(a.href, location.href).pathname === path) { a.classList.add('ui-nav-pending'); a.setAttribute('aria-busy', 'true'); } });
    var state = { box: null, content: content, timer: 0, path:path }; route = state;
    document.dispatchEvent(new CustomEvent('stockbot:routepending',{detail:{label:label,path:path}}));
    state.timer = setTimeout(function () {
      if (route !== state) return;
      var notice = el('div', 'ui-feedback-route'); notice.dataset.state = 'waiting'; notice.setAttribute('role', 'status'); notice.setAttribute('aria-live', 'polite');
      notice.appendChild(el('p', '', '載入比平常久，原畫面仍可操作。'));
      var clock=el('small','ui-route-elapsed',document.getElementById('page-nav-progress-time')?.textContent||'');
      clock.dataset.routeElapsed='';clock.setAttribute('aria-hidden','true');notice.appendChild(clock);
      var button = el('button', '', cancel ? '取消等待' : '收起提示'); button.type = 'button'; button.onclick = function () {
        if (cancel) { cancel(); routeEnd(false, '已取消等待，原畫面仍可使用。', state.retry); }
        else { if (notice.isConnected) notice.remove(); state.box = null; }
      };
      notice.appendChild(button); content.before(notice); state.box = notice;
    }, 7000);
    state.retry = typeof retry === 'function' ? retry : null;
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
  function updateNumber(host) {
      if (!host || !host.isConnected) return;
      if (host.closest('svg,[aria-hidden=true],.ui-chart-readout,.ui-form-status') || host.matches('input')) return;
      var target = host.querySelector('.num') || host;
      if (target.childElementCount) return;
      var next = parseNumber(target.textContent), previous = numberRecords.get(host);
      numberRecords.set(host, next); if (!next) { stopNumber(target); return; }
      if (previous && previous.value !== next.value && previous.suffix === next.suffix) tweenNumber(target, previous, next);
  }
  function scanNumbers(scope) { each(scope, numberSelector, updateNumber); }
  function selectedControls(scope) {
    each(scope,'.portfolio-trend-tab',function(label){var input=document.getElementById(label.htmlFor);label.classList.toggle('ui-selected-control',!!input&&input.checked);});

    each(scope,'button,summary,label[for],[role=button],a[data-app-nav]',function(n){n.classList.add('ui-control');});
    each(scope, 'button[aria-pressed],.tabs button,#rankTabs button,#historyTabs button,.wb-asset-tabs button,.wb-tabs button,.wb-lab-strategy', function (n) {
      var classDriven=n.matches('.tabs button,#rankTabs button,#historyTabs button,.wb-asset-tabs button,.wb-tabs button,.wb-lab-strategy');
      var isOn=classDriven ? n.classList.contains('on')||n.classList.contains('active') : n.getAttribute('aria-pressed')==='true';
      n.classList.toggle('ui-selected-control',isOn);
      if(classDriven)n.setAttribute('aria-pressed',String(isOn));
    });
    each(document, 'a[data-app-nav]', function (a) { if (a.classList.contains('on')) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); });
  }
  function prepareDetails(scope) { each(scope, 'details', function (d) { var summary = Array.from(d.children).find(function (x) { return x.tagName === 'SUMMARY'; }); if (summary) { d.classList.add('ui-disclosure'); summary.classList.add('ui-disclosure-summary'); summary.setAttribute('aria-expanded', String(d.open)); } }); }
  function animateSize(node, start, end) {
    var old = detailsAnimations.get(node); if (old) { old.cancel(); detailsAnimations.delete(node); }
    if (reduced() || !node.animate || Math.abs(end-start)<1) return;
    var overflow=node.style.overflow;node.style.overflow='hidden';
    var a=node.animate([{height:start+'px'},{height:end+'px'}],{duration:duration('expand',220),easing:'cubic-bezier(.2,.7,.2,1)'});detailsAnimations.set(node,a);
    function finish(){if(detailsAnimations.get(node)===a){node.style.overflow=overflow;detailsAnimations.delete(node);}}
    a.onfinish=finish;a.oncancel=finish;
  }
  // Animate explicit disclosure actions only. Async quotes, nested charts and
  // viewport resizing must not start new layout animations while reading.
  function toggleDetails(d) {
    var summary=d.querySelector('summary'),start=d.getBoundingClientRect().height;
    var running=detailsAnimations.get(d);if(running)running.cancel();
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
  function bindPortfolioPinch(svg,host,clear){
    var inner=svg.closest('.trend-chart-inner');
    if(!svg.matches('.portfolio-trend-svg')||!inner||host===svg)return null;
    var originalWidth=inner.style.getPropertyValue('width'),originalMin=inner.style.getPropertyValue('min-width');
    var widthPriority=inner.style.getPropertyPriority('width'),minPriority=inner.style.getPropertyPriority('min-width');
    var originalTouch=svg.style.getPropertyValue('touch-action'),touchPriority=svg.style.getPropertyPriority('touch-action');
    var pinch=null,blocked=false,baseWidth=0,baseScroll=0,frame=0,pending=null;
    var reset=el('button','ui-portfolio-zoom-reset','重設縮放');reset.type='button';reset.hidden=true;reset.setAttribute('aria-label','重設組合走勢圖縮放');host.after(reset);
    // Native one-finger panning remains available. Two fingers belong to the plot.
    svg.style.setProperty('touch-action','pan-x pan-y','important');
    function localX(x){return x-host.getBoundingClientRect().left-host.clientLeft;}
    function limits(){return {min:host.clientWidth,max:Math.max(baseWidth,Math.min(20000,Math.max(baseWidth*4,host.clientWidth*12))) };}
    function apply(){
      frame=0;if(!pending||!svg.isConnected)return;
      var job=pending,range=limits();pending=null;
      var width=Math.max(range.min,Math.min(range.max,job.width));
      inner.style.setProperty('width',width+'px','important');inner.style.setProperty('min-width',width+'px','important');
      host.scrollLeft=Math.max(0,Math.min(width-host.clientWidth,job.anchor*width-localX(job.x)));
      reset.hidden=Math.abs(width-baseWidth)<1;clear();
    }
    function queue(width,anchor,x){pending={width:width,anchor:anchor,x:x};if(!frame)frame=requestAnimationFrame(apply);}
    function points(e){
      var a=e.touches[0],b=e.touches[1];
      return {distance:Math.hypot(a.clientX-b.clientX,a.clientY-b.clientY),x:(a.clientX+b.clientX)/2};
    }
    function begin(e){
      if(e.touches.length!==2)return;
      var p=points(e),width=inner.getBoundingClientRect().width;if(p.distance<8||!width||!host.clientWidth)return;
      if(e.cancelable)e.preventDefault();
      if(!baseWidth){baseWidth=width;baseScroll=host.scrollLeft;}
      pinch={distance:p.distance,width:width,anchor:(host.scrollLeft+localX(p.x))/width};blocked=true;clear();finishReveal(svg);
    }
    function move(e){
      if(!pinch||e.touches.length!==2)return;
      if(e.cancelable)e.preventDefault();var p=points(e);
      queue(pinch.width*p.distance/pinch.distance,pinch.anchor,p.x);
    }
    function end(e){
      if(pinch&&e.touches.length<2){if(frame){cancelAnimationFrame(frame);apply();}pinch=null;}
      if(!e.touches.length)blocked=false;
    }
    function cancel(){cancelAnimationFrame(frame);frame=0;pending=null;pinch=null;blocked=false;clear();}
    function resize(){restore();baseWidth=0;baseScroll=0;}
    function restore(){
      cancelAnimationFrame(frame);frame=0;pending=null;pinch=null;blocked=false;
      if(originalWidth)inner.style.setProperty('width',originalWidth,widthPriority);else inner.style.removeProperty('width');
      if(originalMin)inner.style.setProperty('min-width',originalMin,minPriority);else inner.style.removeProperty('min-width');
      host.scrollLeft=baseScroll;reset.hidden=true;clear();
    }
    function key(e){
      if(!['+','=','-','0'].includes(e.key)||e.ctrlKey||e.metaKey||e.altKey)return false;
      e.preventDefault();if(e.key==='0'){restore();return true;}
      var width=inner.getBoundingClientRect().width;if(!baseWidth){baseWidth=width;baseScroll=host.scrollLeft;}
      var x=host.getBoundingClientRect().left+host.clientLeft+host.clientWidth/2;
      queue(width*(e.key==='-'?1/1.25:1.25),(host.scrollLeft+host.clientWidth/2)/width,x);return true;
    }
    host.addEventListener('touchstart',begin,{passive:false});host.addEventListener('touchmove',move,{passive:false});
    host.addEventListener('touchend',end);host.addEventListener('touchcancel',cancel);reset.addEventListener('click',restore);window.addEventListener('resize',resize,{passive:true});
    return {active:function(){return blocked;},key:key,cleanup:function(){
      restore();host.removeEventListener('touchstart',begin);host.removeEventListener('touchmove',move);host.removeEventListener('touchend',end);host.removeEventListener('touchcancel',cancel);reset.removeEventListener('click',restore);window.removeEventListener('resize',resize);reset.remove();
      if(originalTouch)svg.style.setProperty('touch-action',originalTouch,touchPriority);else svg.style.removeProperty('touch-action');
    }};
  }
  function bindChart(svg) {
    if (svg.dataset.uiChartBound) return; svg.dataset.uiChartBound = '1';
    var originalLabel=svg.getAttribute('aria-label'),originalTabindex=svg.getAttribute('tabindex');
    var panel = svg.closest('.portfolio-trend-panel') || svg.parentElement;
    var readout = el('div', 'ui-chart-readout '+(svg.matches('.portfolio-trend-svg')?'ui-portfolio-readout':'ui-rank-readout')), values = el('span', 'ui-chart-values');
    values.hidden=true;values.setAttribute('role','tooltip');
    readout.append(values);
    panel.classList.add('ui-chart-host');
    var host = svg.closest('.portfolio-trend-plot') || svg;
    var anchor=svg.matches('.portfolio-trend-svg')?host:svg.parentElement.querySelector('.legend')||svg;anchor.after(readout);
    var ns = 'http://www.w3.org/2000/svg', guide = document.createElementNS(ns, 'line'), dot = document.createElementNS(ns, 'circle');
    guide.setAttribute('class', 'ui-chart-crosshair'); dot.setAttribute('class', 'ui-chart-dot'); dot.setAttribute('r', '5'); guide.style.display = dot.style.display = 'none'; svg.append(guide, dot);
    svg.setAttribute('tabindex', '0'); svg.setAttribute('aria-label', (svg.getAttribute('aria-label') || '走勢圖') + '，左右方向鍵追蹤資料');
    svg.classList.add('ui-chart-tracking');if(svg.matches('.portfolio-trend-svg'))svg.setAttribute('aria-label',svg.getAttribute('aria-label')+'，雙指縮放日期範圍，按加減號縮放、0 重設');
    var frame = 0, lastX = null, selected = 0, drag = null, dragged = false;
    var cachedPoints=null,groups=[],groupForIndex=[],activePoint=null,shownGroup=null;
    function data(){
      if(cachedPoints)return cachedPoints;
      cachedPoints=chartPoints(svg);groups=[];groupForIndex=[];
      cachedPoints.map(function(p,i){return {point:p,index:i};}).sort(function(a,b){return a.point.x-b.point.x;}).forEach(function(item){
        var group=groups[groups.length-1];
        if(!group||Math.abs(group.x-item.point.x)>=.1){group={x:item.point.x,index:item.index,points:[],order:groups.length};groups.push(group);}
        group.points.push(item.point);groupForIndex[item.index]=group;
      });
      groups.forEach(function(group){group.text=group.points[0].label+' · '+group.points.map(function(p){return p.text;}).join(' ／ ');});
      return cachedPoints;
    }
    var pointObserver=new MutationObserver(function(records){
      if(records.some(function(r){return r.type==='childList'||(r.target.matches&&r.target.matches('.trend-hit-point,[data-ui-points]'));}))cachedPoints=null;
    });
    pointObserver.observe(svg,{subtree:true,childList:true,attributes:true,attributeFilter:['style','data-ui-points','cx','cy','data-date','data-port','data-market','data-excess']});
    function clear() {
      values.hidden=true;guide.style.display=dot.style.display='none';shownGroup=null;
      if(activePoint){activePoint.classList.remove('ui-chart-point-active');activePoint=null;}
    }
    function show(index,points,svgBox) {
      points=points||data();if(!points.length){clear();return;}
      selected=Math.max(0,Math.min(points.length-1,index));var p=points[selected],group=groupForIndex[selected];
      var panelBox=panel.getBoundingClientRect();svgBox=svgBox||svg.getBoundingClientRect();
      var top=Math.max(8,svgBox.top-panelBox.top+8)+'px',left=Math.max(8,Math.min(panelBox.width-300-8,svgBox.left-panelBox.left+8))+'px';
      if(values.textContent!==group.text)values.textContent=group.text;
      if(values.style.top!==top)values.style.top=top;if(values.style.left!==left)values.style.left=left;values.hidden=false;
      if(shownGroup===group)return;shownGroup=group;
      guide.setAttribute('x1',p.x);guide.setAttribute('x2',p.x);guide.setAttribute('y1','0');guide.setAttribute('y2',svg.viewBox.baseVal.height);dot.setAttribute('cx',p.x);dot.setAttribute('cy',p.y);guide.style.display=dot.style.display='';
      if(activePoint&&activePoint!==p.node)activePoint.classList.remove('ui-chart-point-active');activePoint=p.node;if(activePoint)activePoint.classList.add('ui-chart-point-active');
    }
    function nearest(x,svgBox) {
      var points=data();if(!groups.length){clear();return;}
      var lo=0,hi=groups.length-1;
      while(lo<hi){var mid=Math.floor((lo+hi)/2);if(groups[mid].x<x)lo=mid+1;else hi=mid;}
      var best=lo;if(lo>0&&Math.abs(groups[lo-1].x-x)<=Math.abs(groups[lo].x-x))best=lo-1;
      show(groups[best].index,points,svgBox);
    }
    var pinchZoom=bindPortfolioPinch(svg,host,clear);
    function move(e) {
      if(pinchZoom&&pinchZoom.active()){drag=null;dragged=true;clear();return;}
      if (e.pointerType !== 'touch' && drag && drag.id === e.pointerId && host.scrollWidth > host.clientWidth) {
        var dx = e.clientX - drag.x, dy = e.clientY - drag.y;
        if (Math.abs(dx) > 6 && Math.abs(dx) > Math.abs(dy)) {
          dragged = true;
          host.scrollLeft = drag.scroll - dx;
        }
      }
      lastX = e.clientX;
      if (frame) return; frame = requestAnimationFrame(function () { frame = 0; if (!svg.isConnected||(pinchZoom&&pinchZoom.active())) return; var r = svg.getBoundingClientRect(); if (r.width) nearest((lastX - r.left) / r.width * svg.viewBox.baseVal.width,r); });
    }
    function key(e) {
      if(pinchZoom&&pinchZoom.key(e))return;if(e.key==='Escape'){clear();return;}
      if(e.key==='ArrowLeft'||e.key==='ArrowRight'){
        e.preventDefault();var points=data(),current=groupForIndex[selected];if(!current)return;
        var next=Math.max(0,Math.min(groups.length-1,current.order+(e.key==='ArrowLeft'?-1:1)));show(groups[next].index,points);
      }
    }
    function down(e) { if(pinchZoom&&pinchZoom.active()){dragged=true;return;}dragged = false; drag = {id:e.pointerId,x:e.clientX,y:e.clientY,scroll:host.scrollLeft}; if(e.pointerType!=='touch'&&host.scrollWidth>host.clientWidth&&svg.setPointerCapture){try{svg.setPointerCapture(e.pointerId);}catch(ignore){}} move(e); }
    function up() { drag = null; }
    function click(e) { if ((pinchZoom&&pinchZoom.active())||dragged) { e.preventDefault(); e.stopImmediatePropagation(); dragged = false; } }
    svg.addEventListener('pointermove', move, { passive: true }); svg.addEventListener('pointerdown', down, { passive: true }); svg.addEventListener('pointerup', up); svg.addEventListener('click', click, true); svg.addEventListener('keydown', key);
    var leave = function () { drag = null; clear(); }; svg.addEventListener('pointerleave', leave); svg.addEventListener('pointercancel', leave);
    var cleanup = function () { pointObserver.disconnect();cachedPoints=null;groups=[];groupForIndex=[];if(pinchZoom)pinchZoom.cleanup();cancelAnimationFrame(frame); clear(); svg.removeEventListener('pointermove', move); svg.removeEventListener('pointerdown', down); svg.removeEventListener('pointerup', up); svg.removeEventListener('click', click, true); svg.removeEventListener('keydown', key); svg.removeEventListener('pointerleave', leave); svg.removeEventListener('pointercancel', leave); readout.remove();guide.remove();dot.remove();svg.classList.remove('ui-chart-tracking'); if(originalLabel===null)svg.removeAttribute('aria-label');else svg.setAttribute('aria-label',originalLabel);if(originalTabindex===null)svg.removeAttribute('tabindex');else svg.setAttribute('tabindex',originalTabindex); delete svg.dataset.uiChartBound; chartCleanups.delete(cleanup); };
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
    var full=fullScanPending;fullScanPending=false;
    if(!full){pendingNumbers.forEach(updateNumber);pendingNumbers.clear();return;}
    pendingNumbers.clear();
    ['#us-summary','#us-heat','#us-ranking'].forEach(function(sel){var host=scope.querySelector(sel);if(host&&!host.childElementCount&&!host.textContent.trim())load(host,'讀取已完成快照…');});
    each(scope,'.msg',function(box){if(box.dataset.uiOutcome)return;var text=box.textContent||'';var state=/失敗|錯誤|沒有儲存|不正確|過期|無法|請先|請填|未完成|⚠/.test(text)?'error':/成功|已儲存|已新增|已賣出|✅/.test(text)?'success':null;if(state){box.dataset.uiOutcome=state;box.dataset.state=state;box.classList.add('ui-form-status');box.setAttribute('role',state==='error'?'alert':'status');}});
    scanNumbers(scope); selectedControls(scope); prepareDetails(scope); prepareAllocation(scope);
    prepareViewport(scope);
    each(scope, 'svg.portfolio-trend-svg,svg.ui-interactive-chart', function(svg){if(svg.dataset.uiChartBound||chartQueued.has(svg))return;chartQueued.add(svg);whenNear(svg,function(){chartQueued.delete(svg);bindChart(svg);});});
    nearJobs.forEach(function(_,host){if(!host.isConnected){nearJobs.delete(host);if(nearObserver)nearObserver.unobserve(host);}});
    loadStates.forEach(function (state, host) { if (!host.isConnected || !host.contains(state.box)) endLoad(host); });
    chartCleanups.forEach(function (cleanup) { if (!cleanup.svg.isConnected) cleanup(); });
  }
  function schedule() { fullScanPending=true;if (!scanFrame) scanFrame = requestAnimationFrame(scan); }
  function scheduleNumber(host){pendingNumbers.add(host);if(!scanFrame)scanFrame=requestAnimationFrame(scan);}
  function clearPress(){clearTimeout(pressTimer);pressTimer=0;if(!press)return;press.target.classList.remove('ui-pressed');if(press.card)press.card.classList.remove('ui-pressed');press=null;}
  document.addEventListener('pointerdown', function(e){
    clearPress();var target=e.target.closest(controls);
    if(!target||target.disabled||e.button>0||e.isPrimary===false)return;
    if(target.matches('[data-home-privacy-toggle]'))stopNumbers();
    var card=target.matches('.position-more>summary')?target.closest('.position-card'):null;
    press={id:e.pointerId,x:e.clientX,y:e.clientY,at:performance.now(),target:target,card:card};
    target.classList.add('ui-pressed');if(card)card.classList.add('ui-pressed');
    pressTimer=setTimeout(clearPress,2000);
  },{capture:true,passive:true});
  document.addEventListener('pointermove',function(e){if(press&&press.id===e.pointerId&&(Math.abs(e.clientX-press.x)>8||Math.abs(e.clientY-press.y)>8))clearPress();},{capture:true,passive:true});
  document.addEventListener('pointerup',function(e){if(!press||press.id!==e.pointerId)return;clearTimeout(pressTimer);pressTimer=setTimeout(clearPress,Math.max(0,100-(performance.now()-press.at)));},{capture:true,passive:true});
  document.addEventListener('pointercancel',clearPress,{capture:true,passive:true});
  window.addEventListener('blur',clearPress);
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
  function cleanup() { clearPress();pendingNumbers.clear();fullScanPending=true;if(nearObserver)nearObserver.disconnect();if(revealObserver)revealObserver.disconnect();each(root(),'.ui-section-arrive',finishReveal);revealActive.clear();cancelAnimationFrame(revealFrame);revealFrame=0;nearJobs.clear();chartQueued=new WeakSet(); formTimers.forEach(clearTimeout);formTimers.clear(); stopNumbers(); detailsAnimations.forEach(function (a) { a.cancel(); }); detailsAnimations.clear(); chartCleanups.forEach(function (fn) { fn(); }); loadStates.forEach(function (_, host) { endLoad(host); }); cancelAnimationFrame(scanFrame); scanFrame = 0; }
  function reduceReveal(){if(motion.matches)each(root(),'.ui-section-arrive',finishReveal);}
  if(motion.addEventListener)motion.addEventListener('change',reduceReveal);else if(motion.addListener)motion.addListener(reduceReveal);
  document.addEventListener('stockbot:pageleaving', cleanup);
  document.addEventListener('visibilitychange', function () { if (document.hidden) {stopNumbers();clearPress();}else{schedule();scheduleReveals();} });
  window.addEventListener('scroll',function(){stopNumbers();clearPress();scheduleReveals();}, { capture: true, passive: true }); window.addEventListener('resize',function(){stopNumbers();scheduleReveals();}, { passive: true });
  window.addEventListener('pagehide', function () {
    if(route){try{sessionStorage.setItem('sb-route-handoff',JSON.stringify({path:route.path,at:Date.now()}));}catch(ignore){}}
    cleanup(); routeEnd(true);
  });
  try {
    var handoff=JSON.parse(sessionStorage.getItem('sb-route-handoff')||'null');sessionStorage.removeItem('sb-route-handoff');
    if(handoff&&handoff.path===location.pathname&&Date.now()-handoff.at<60000&&!reduced()){
      var arriving=root();if(arriving){arriving.classList.add('ui-route-arrive');setTimeout(function(){arriving.classList.remove('ui-route-arrive');},260);}
    }
  } catch(ignore){}
  window.addEventListener('pageshow', function () { each(document, 'form[data-submitted="1"]', function (form) { delete form.dataset.submitted; form.classList.remove('tap-loading'); each(form, '[data-old-text]', function (b) { b.disabled = false; if (b.tagName === 'INPUT') b.value = b.dataset.oldText; else b.textContent = b.dataset.oldText; }); formState(form, 'error', '操作狀態已重設，請先確認是否已儲存。'); }); schedule(); });
  window.StockBotUI = { routeStart: routeStart, routeEnd: routeEnd, load: load, endLoad: endLoad, formState: formState, notify: notify, scan: schedule, reducedMotion: reduced, cleanup: cleanup, parseNumber: parseNumber, whenNear:whenNear, captureView:captureView, restoreView:restoreView };
  function boot() { var scope = root(); if (!scope) return; observer = new MutationObserver(function(records){records.forEach(function(r){
    var node=r.target.nodeType===1?r.target:r.target.parentElement;
    if(!node||node.closest('.ui-chart-readout'))return;
    var host=node.closest(numberSelector);if(host){scheduleNumber(host);return;}
    var structural=r.type==='childList'&&Array.from(r.addedNodes).concat(Array.from(r.removedNodes)).some(function(n){return n.nodeType===1;});
    if(structural){schedule();return;}
  });}); observer.observe(scope, { childList: true, characterData: true, subtree: true }); scan(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, { once: true }); else boot();
})();
