/* Viewport chrome and navigation feedback only. No fetching or account state. */
(function(){
  'use strict';
  if(window.StockBotExperience)return;
  var nav,inner,cursor,pendingPath=null,navFrame=0,scrollFrame=0;
  var arrived=new WeakSet(),homeStatusObserver=null,homeStatusRoot=null;
  var motion=matchMedia('(prefers-reduced-motion: reduce)');
  var phone=matchMedia('(max-width:760px)'), lastScrollY=null, turnY=0, direction=0;
  function syncNav(){
    navFrame=0;nav=document.querySelector('.app-bottom-nav');inner=nav&&nav.querySelector('.bottom-inner');
    if(!inner)return;
    if(!cursor||!cursor.isConnected){cursor=document.createElement('span');cursor.className='app-nav-cursor';cursor.setAttribute('aria-hidden','true');inner.appendChild(cursor);}
    inner.querySelectorAll('a').forEach(function(link){
      var match=!!pendingPath&&new URL(link.href,location.href).pathname===pendingPath;
      link.classList.toggle('experience-nav-target',match);
    });
    var target=inner.querySelector('a.on');if(!target){cursor.hidden=true;return;}
    cursor.hidden=false;var a=target.getBoundingClientRect(),b=inner.getBoundingClientRect();
    cursor.style.width=a.width+'px';cursor.style.height=a.height+'px';cursor.style.transform='translate3d('+(a.left-b.left)+'px,'+(a.top-b.top)+'px,0)';
    requestAnimationFrame(function(){if(nav&&nav.isConnected)nav.classList.add('experience-nav-ready');});
  }
  function scheduleNav(){if(!navFrame)navFrame=requestAnimationFrame(syncNav);}
  function endRoute(){
    pendingPath=null;document.body.classList.remove('app-route-pending');
    resetChrome();scheduleNav();
  }
  function startRoute(e){
    var detail=e.detail||{};pendingPath=detail.path||null;
    document.body.classList.add('app-route-pending');resetChrome();syncNav();

  }
  function syncScroll(){
    scrollFrame=0;
    var maxY=Math.max(0,document.documentElement.scrollHeight-innerHeight);
    var y=Math.max(0,Math.min(maxY,window.scrollY||0)),on=y>28,below=maxY-y>3;
    if(document.body.classList.contains('app-is-scrolled')!==on)document.body.classList.toggle('app-is-scrolled',on);
    if(document.body.classList.contains('app-can-scroll-down')!==below)document.body.classList.toggle('app-can-scroll-down',below);
    if(!phone.matches||motion.matches||y<48||document.body.classList.contains('app-route-pending')){
      lastScrollY=y;turnY=y;direction=0;
      document.body.classList.remove('app-chrome-retracted');
      return;
    }
    if(lastScrollY===null){lastScrollY=y;turnY=y;return;}
    var delta=y-lastScrollY;
    if(Math.abs(delta)<1)return;
    var nextDirection=Math.sign(delta);
    if(direction!==nextDirection){direction=nextDirection;turnY=lastScrollY;}
    lastScrollY=y;
    if(direction>0&&y>78&&y-turnY>=30)document.body.classList.add('app-chrome-retracted');
    if(direction<0&&turnY-y>=12)document.body.classList.remove('app-chrome-retracted');
  }
  function scroll(){if(!scrollFrame)scrollFrame=requestAnimationFrame(syncScroll);}
  function resetChrome(){
    lastScrollY=null;direction=0;document.body.classList.remove('app-chrome-retracted');scroll();
  }
  function prepareHomeStatus(scope){
    var home=scope.querySelector('#hv2');if(!home||homeStatusRoot===home)return;
    var comparison=home.querySelector('.hv2-cmp'),box=comparison&&comparison.closest('.hv2-sec');if(!box)return;
    if(homeStatusObserver)homeStatusObserver.disconnect();
    var parts=Array.from(home.querySelectorAll('.hv2-src,.hv2-sync,[data-home-quote-quality],[data-home-quote-refresh]'));
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
    prepareHomeStatus(scope);if(motion.matches||document.hidden||document.body.classList.contains('app-route-pending'))return;
    var first=scope.querySelector('.totals,.wb-pulse,.us-hero,.rank-situation,#hv2>.hv2-sec.first,.market-quote-hero');
    if(first&&first.animate&&!arrived.has(first)){arrived.add(first);first.animate([{opacity:.75},{opacity:1}],{duration:180,easing:'ease-out'});}
  }
  document.addEventListener('stockbot:pageleaving',releaseHomeStatus);
  document.addEventListener('stockbot:routepending',startRoute);
  document.addEventListener('stockbot:routecomplete',endRoute);
  document.addEventListener('stockbot:pagecontentready',arrive);
  document.addEventListener('stockbot:page-ready',arrive);
  window.addEventListener('scroll',scroll,{passive:true});window.addEventListener('resize',function(){scheduleNav();scroll();},{passive:true});
  if(phone.addEventListener)phone.addEventListener('change',resetChrome);
  if(motion.addEventListener)motion.addEventListener('change',resetChrome);
  window.addEventListener('pageshow',function(){endRoute();arrive();});
  window.addEventListener('pagehide',function(){releaseHomeStatus();endRoute();cancelAnimationFrame(navFrame);cancelAnimationFrame(scrollFrame);navFrame=scrollFrame=0;});
  window.StockBotExperience={sync:scheduleNav};
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',arrive,{once:true});else arrive();
})();
