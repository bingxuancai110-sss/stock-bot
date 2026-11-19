/* Viewport chrome and navigation feedback only. No fetching or account state. */
(function(){
  'use strict';
  if(window.StockBotExperience)return;
  var nav,inner,cursor,status,pendingPath=null,statusTimer=0,navFrame=0,scrollFrame=0;
  var arrived=new WeakSet(),homeStatusObserver=null,homeStatusRoot=null;
  var motion=matchMedia('(prefers-reduced-motion: reduce)');
  function syncNav(){
    navFrame=0;nav=document.querySelector('.app-bottom-nav');inner=nav&&nav.querySelector('.bottom-inner');
    if(!inner)return;
    if(!cursor||!cursor.isConnected){cursor=document.createElement('span');cursor.className='app-nav-cursor';cursor.setAttribute('aria-hidden','true');inner.appendChild(cursor);}
    var target=null;
    inner.querySelectorAll('a').forEach(function(link){
      var match=!!pendingPath&&new URL(link.href,location.href).pathname===pendingPath;
      link.classList.toggle('experience-nav-target',match);if(match)target=link;
    });
    target=target||inner.querySelector('a.on');if(!target){cursor.hidden=true;return;}
    cursor.hidden=false;var a=target.getBoundingClientRect(),b=inner.getBoundingClientRect();
    cursor.style.width=a.width+'px';cursor.style.height=a.height+'px';cursor.style.transform='translate3d('+(a.left-b.left)+'px,'+(a.top-b.top)+'px,0)';
    requestAnimationFrame(function(){if(nav&&nav.isConnected)nav.classList.add('experience-nav-ready');});
  }
  function scheduleNav(){if(!navFrame)navFrame=requestAnimationFrame(syncNav);}
  function endRoute(){
    clearTimeout(statusTimer);pendingPath=null;document.body.classList.remove('app-route-pending');
    if(status)status.hidden=true;scheduleNav();
  }
  function startRoute(e){
    clearTimeout(statusTimer);var detail=e.detail||{};pendingPath=detail.path||null;
    document.body.classList.add('app-route-pending');syncNav();

  }
  function syncScroll(){
    scrollFrame=0;var y=window.scrollY||0,on=y>28,below=document.documentElement.scrollHeight-innerHeight-y>3;
    if(document.body.classList.contains('app-is-scrolled')!==on)document.body.classList.toggle('app-is-scrolled',on);
    if(document.body.classList.contains('app-can-scroll-down')!==below)document.body.classList.toggle('app-can-scroll-down',below);
  }
  function scroll(){if(!scrollFrame)scrollFrame=requestAnimationFrame(syncScroll);}
  function prepareHomeStatus(scope){
    var home=scope.querySelector('#hv2');if(!home||homeStatusRoot===home)return;
    var comparison=home.querySelector('.hv2-cmp'),box=comparison&&comparison.closest('.hv2-sec');if(!box)return;
    if(homeStatusObserver)homeStatusObserver.disconnect();
    var parts=Array.from(home.querySelectorAll('.hv2-src,.hv2-sync,[data-home-quote-quality]'));
    if(!parts.length)return;
    var details=box.querySelector('.home-market-details'),label,stamp,body;
    if(details){label=details.querySelector('.home-market-status');stamp=details.querySelector('.home-market-stamp');body=details.querySelector('.home-market-detail-body');}
    else{
      details=document.createElement('details');details.className='home-market-details';
      var summary=document.createElement('summary');label=document.createElement('span');stamp=document.createElement('span');body=document.createElement('div');
      label.className='home-market-status';stamp.className='home-market-stamp';body.className='home-market-detail-body';
      summary.append(label,stamp);details.append(summary,body);box.appendChild(details);
    }
    parts.forEach(function(node){if(!body.contains(node))body.appendChild(node);});homeStatusRoot=home;
    var heading=home.querySelector('.home-impact-section .hv2-h');if(heading)heading.textContent='今日組合貢獻';
    function update(){
      var title=body.querySelector('[data-home-live-title]'),quality=body.querySelector('[data-home-quote-quality]');
      var pending=!!quality&&/尚待確認|同步中|無法|暫缺|等待|未完成/.test(quality.textContent);
      var text=pending?'持股行情待確認':(title&&title.textContent.trim())||'行情狀態';
      if(label.textContent!==text)label.textContent=text;
      details.classList.toggle('is-pending',pending);
      var source=(quality&&quality.textContent)||body.textContent,time=source.match(/(?:T|\s)(\d{2}:\d{2})(?::\d{2})?/);
      var clock=time?time[1]+' 更新':'更新詳情';if(stamp.textContent!==clock)stamp.textContent=clock;
    }
    update();homeStatusObserver=new MutationObserver(update);homeStatusObserver.observe(body,{subtree:true,childList:true,characterData:true});
  }
  function releaseHomeStatus(){if(homeStatusObserver)homeStatusObserver.disconnect();homeStatusObserver=null;homeStatusRoot=null;}
  function arrive(){
    scheduleNav();scroll();var scope=document.getElementById('app-page-content');if(!scope)return;
    prepareHomeStatus(scope);if(motion.matches||document.hidden)return;
    var first=scope.querySelector('.totals,.wb-pulse,.us-hero,.rank-situation,#hv2>.hv2-sec.first,.market-quote-hero');
    if(first&&first.animate&&!arrived.has(first)){arrived.add(first);first.animate([{opacity:.75},{opacity:1}],{duration:180,easing:'ease-out'});}
  }
  document.addEventListener('stockbot:pageleaving',releaseHomeStatus);
  document.addEventListener('stockbot:routepending',startRoute);
  document.addEventListener('stockbot:routecomplete',endRoute);
  document.addEventListener('stockbot:pagecontentready',arrive);
  document.addEventListener('stockbot:page-ready',arrive);
  window.addEventListener('scroll',scroll,{passive:true});window.addEventListener('resize',function(){scheduleNav();scroll();},{passive:true});
  window.addEventListener('pageshow',function(){endRoute();arrive();});
  window.addEventListener('pagehide',function(){releaseHomeStatus();endRoute();cancelAnimationFrame(navFrame);cancelAnimationFrame(scrollFrame);navFrame=scrollFrame=0;});
  window.StockBotExperience={sync:scheduleNav};
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',arrive,{once:true});else arrive();
})();
