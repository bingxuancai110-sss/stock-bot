/* Viewport chrome and navigation feedback only. No fetching or account state. */
(function(){
  'use strict';
  if(window.StockBotExperience)return;
  var nav,inner,cursor,status,pendingPath=null,statusTimer=0,navFrame=0,scrollFrame=0;
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
    if(!status){status=document.createElement('div');status.className='experience-route-status';status.setAttribute('role','status');status.setAttribute('aria-live','polite');status.hidden=true;document.body.appendChild(status);}
    status.textContent=detail.label||'正在開啟頁面…';status.hidden=true;
    statusTimer=setTimeout(function(){if(pendingPath)status.hidden=false;},120);
  }
  function syncScroll(){
    scrollFrame=0;var y=window.scrollY||0,on=y>28,below=document.documentElement.scrollHeight-innerHeight-y>3;
    if(document.body.classList.contains('app-is-scrolled')!==on)document.body.classList.toggle('app-is-scrolled',on);
    if(document.body.classList.contains('app-can-scroll-down')!==below)document.body.classList.toggle('app-can-scroll-down',below);
  }
  function scroll(){if(!scrollFrame)scrollFrame=requestAnimationFrame(syncScroll);}
  function arrive(){
    scheduleNav();scroll();if(motion.matches||document.hidden)return;
    var scope=document.getElementById('app-page-content');if(!scope)return;
    var first=scope.querySelector('.totals,.wb-pulse,.us-hero,.rank-situation,#hv2>.hv2-sec.first,.market-quote-hero');
    if(first&&first.animate)first.animate([{opacity:.75},{opacity:1}],{duration:180,easing:'ease-out'});
  }
  document.addEventListener('stockbot:routepending',startRoute);
  document.addEventListener('stockbot:routecomplete',endRoute);
  document.addEventListener('stockbot:pagecontentready',arrive);
  document.addEventListener('stockbot:page-ready',arrive);
  window.addEventListener('scroll',scroll,{passive:true});window.addEventListener('resize',function(){scheduleNav();scroll();},{passive:true});
  window.addEventListener('pageshow',function(){endRoute();arrive();});
  window.addEventListener('pagehide',function(){endRoute();cancelAnimationFrame(navFrame);cancelAnimationFrame(scrollFrame);navFrame=scrollFrame=0;});
  window.StockBotExperience={sync:scheduleNav};
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',arrive,{once:true});else arrive();
})();
