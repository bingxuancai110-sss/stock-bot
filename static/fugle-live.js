(async function startFugle(){
  'use strict';
  if(window.StockBotFugle||window.StockBotFugleInitializing)return;
  if(!window.StockBotFugleBootstrapBound){
    window.StockBotFugleBootstrapBound=true;
    const resume=()=>{if(document.hidden)return;if(window.StockBotFugle)window.StockBotFugle.resume();else startFugle();};
    window.addEventListener('pageshow',resume);window.addEventListener('online',resume);
    document.addEventListener('DOMContentLoaded',resume);document.addEventListener('stockbot:pagecontentready',resume);
    document.addEventListener('visibilitychange',resume);
  }
  window.StockBotFugleInitializing=true;
  const relevant=()=>/^\/web\/(portfolio|positions|more|quote-lab)\/?$/.test(location.pathname);
  const localURL=path=>{const url=new window.URL(path,location.origin);let token='';try{token=new URLSearchParams(location.search).get('t')||localStorage.getItem('stockbot_web_token')||'';}catch(ignore){}if(token)url.searchParams.set('t',token);return url.href;};
  let config;
  try{
    for(let attempt=0;attempt<4;attempt++){
      const controller=new AbortController(),deadline=setTimeout(()=>controller.abort(),5000);
      try{
        const response=await fetch(localURL('/web/quote-lab/app-config'),{credentials:'same-origin',cache:'no-store',redirect:'error',signal:controller.signal});
        if([401,403,404].includes(response.status))return;
        if(!response.ok)throw new Error('configuration unavailable');
        const value=await response.json();if(!value.ok||!value.csrf_token)throw new Error('invalid configuration');
        config=value;break;
      }catch(ignore){}finally{clearTimeout(deadline);}
      if(attempt<3)await new Promise(resolve=>setTimeout(resolve,[300,1000,2000][attempt]));
    }
  }finally{window.StockBotFugleInitializing=false;}
  if(!config)return;
  const doc=new DOMParser().parseFromString(config.panel_html,'text/html');
  if(!document.getElementById('fugle-panel-style')){const style=document.createElement('style');style.id='fugle-panel-style';style.textContent=Array.from(doc.querySelectorAll('style')).map(x=>x.textContent).join('\n')+'\n#stockbot-fugle-panel{margin:10px 0;border:1px solid #cbded5;border-radius:12px;background:#f6faf8;padding:10px 14px}#stockbot-fugle-panel>summary{min-height:38px;cursor:pointer;font-weight:750;font-size:13px;color:#315d49}#stockbot-fugle-panel #quote-lab{padding-bottom:8px}#stockbot-fugle-panel.is-compact .ql-back,#stockbot-fugle-panel.is-compact .ql-hero,#stockbot-fugle-panel.is-compact .ql-note{display:none}#stockbot-fugle-panel.is-compact .ql-panel{padding:12px;margin-top:10px}#stockbot-fugle-panel.is-compact .ql-stats{display:none}';document.head.appendChild(style);}
  const root=document.getElementById('quote-lab')||doc.getElementById('quote-lab');
  if(!root||root.dataset.ready==='1')return;
  const panel=document.createElement('details'),panelLabel=document.createElement('summary');panel.id='stockbot-fugle-panel';panelLabel.textContent='富果行情 · 尚未啟用';panel.append(panelLabel,root);
  let mountFrame=0;
  function mount(){mountFrame=0;const host=document.getElementById('app-page-content');if(!host)return;document.querySelectorAll('#quote-lab').forEach(x=>{if(x!==root)x.remove();});panel.hidden=!/^\/web\/(more|quote-lab)\/?$/.test(location.pathname);panel.classList.toggle('is-compact',location.pathname!=='/web/quote-lab');const parent=panel.hidden?document.body:host;if(panel.parentNode!==parent){if(panel.hidden)parent.appendChild(panel);else parent.prepend(panel);}if(location.pathname==='/web/quote-lab')panel.open=true;}
  function queueMount(){if(!mountFrame)mountFrame=requestAnimationFrame(mount);}
  mount();
  root.dataset.ready='1';
  const $=s=>root.querySelector(s),keyInput=$('#ql-key'),symbolsInput=$('#ql-symbols'),start=$('#ql-start'),stopButton=$('#ql-stop'),followInput=$('#ql-follow'),rememberInput=$('#ql-remember'),rememberStatus=$('#ql-remember-status'),changeButton=$('#ql-change-key'),forgetButton=$('#ql-forget');
  // Account-scoped and owner-authorized before reading any local credential.
  const storageKey=typeof config.storage_scope==='string'&&/^[a-f0-9]{64}$/.test(config.storage_scope)?'stockbot_fugle_device_v1:'+config.storage_scope:null;
  let saved=null,storageFailed=false,restoreBlocked=false;
  function readSaved(){
    if(!storageKey)return null;
    try{
      const value=JSON.parse(localStorage.getItem(storageKey)||'null');
      if(value&&value.version===1&&typeof value.key==='string'&&value.key.trim()&&value.key.length<=4096&&typeof value.symbols==='string'&&typeof value.follow==='boolean'&&typeof value.autoStart==='boolean')return value;
      localStorage.removeItem(storageKey);
    }catch(ignore){storageFailed=true;}
    return null;
  }
  function showRemember(){
    text(rememberStatus,storageFailed?'這個瀏覽器無法保存或清除裝置記錄，變更未儲存；本次仍可連線，請檢查瀏覽器設定。':saved?(saved.autoStart?'已記住此裝置，下次開網站會自動連線。':'已記住金鑰；自動連線已停止，按「啟用」可恢復。'):'尚未儲存金鑰；未勾選時僅在本次開啟期間使用。');
    forgetButton.disabled=!saved&&!storageFailed&&!lastCompleteView&&!readView();changeButton.disabled=!running&&!saved;
  }
  function removeSaved(){
    saved=null;
    try{if(storageKey)localStorage.removeItem(storageKey);storageFailed=false;}catch(ignore){storageFailed=true;}
    showRemember();return !storageFailed;
  }
  function writeSaved(value){
    if(!storageKey){storageFailed=true;showRemember();return;}
    try{localStorage.setItem(storageKey,JSON.stringify(value));saved=value;storageFailed=false;}catch(ignore){storageFailed=true;}
    showRemember();
  }
  function rememberVerified(){
    if(rememberInput.checked&&authenticated&&apiKey)writeSaved({version:1,key:apiKey,symbols:symbolsInput.value,follow:following,autoStart:true});
  }
  function disableAutoStart(){restoreBlocked=true;if(saved)writeSaved({...saved,autoStart:false});}
  const REST='https://api.fugle.tw/marketdata/v1.0/stock/intraday/quote/';
  const URL='wss://api.fugle.tw/marketdata/v1.0/stock/streaming';
  const names={'2308':'台達電','8996':'高力','2383':'台光電','2368':'金像電','6442':'光聖'};
  const rows=new Map(),models=new Map(),flashes=new Map();
  const clock=new Intl.DateTimeFormat('zh-TW',{timeZone:'Asia/Taipei',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
  let apiKey='',symbols=[],ws=null,running=false,disposed=false,generation=0,retryTimer=null,phaseTimer=null,healthTimer=null,raf=null,releaseLock=null;
  let events=0,retries=0,failures=0,lastMessage=0;
  let following=false,preferred=[],allSymbols=[],authenticated=false,holdTimer=null,restTimer=null,subTimer=null;
  let holdController=null,restController=null,restCursor=0,restPause=0,indexQuote=null,indexAttempt=0;
  let applyTimer=null,applyController=null,lastApply=0,paused=false,lockGeneration=null;
  const today=()=>new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Taipei',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
  const channels=new Map(),pendingSub=new Set(),pendingUnsub=new Set(),requestTimes=[];
  function text(el,value){if(el&&el.textContent!==value)el.textContent=value;}
  function status(state,label,message){$('#ql-state').dataset.state=state;text($('#ql-state'),label);text($('#ql-message'),message);text(panelLabel,'富果行情 · '+label);}
  function controls(){start.disabled=running;stopButton.disabled=!running;keyInput.disabled=running;symbolsInput.disabled=running;followInput.disabled=running;showRemember();}
  function clearTimer(id){if(id!==null)clearTimeout(id);}
  function dropSocket(){
    clearTimer(phaseTimer);phaseTimer=null;clearTimer(subTimer);subTimer=null;authenticated=false;channels.clear();pendingSub.clear();pendingUnsub.clear();
    if(healthTimer!==null)clearInterval(healthTimer);healthTimer=null;
    const old=ws;ws=null;
    if(old){old.onopen=old.onmessage=old.onclose=old.onerror=null;try{old.close(1000,'Client stopped');}catch(ignore){}}
  }
  function stop(message,state='idle',label='已停止'){
    generation++;running=false;paused=false;apiKey='';clearTimer(applyTimer);applyTimer=null;if(applyController)applyController.abort();applyController=null;keyInput.value='';clearTimer(retryTimer);retryTimer=null;dropSocket();
    clearTimer(holdTimer);clearTimer(restTimer);holdTimer=restTimer=null;
    if(holdController)holdController.abort();if(restController)restController.abort();holdController=restController=null;
    if(releaseLock){releaseLock();releaseLock=null;}
    controls();status(state,label,message);
  }
  function failed(message,invalidKey=false){restoreBlocked=true;if(invalidKey){removeSaved();rememberInput.checked=false;}stop(message,'error','連線已停止');}
  function timestamp(micros){const ms=micros/1000;return clock.format(new Date(ms))+'.'+String(Math.floor(ms)%1000).padStart(3,'0');}
  function number(value){return Number(value).toLocaleString('zh-TW',{minimumFractionDigits:2,maximumFractionDigits:2});}
  function render(){
    raf=null;if(disposed)return;
    models.forEach((m,symbol)=>{
      const row=rows.get(symbol);if(!row)return;
      if(m.price!==undefined){
        text(row.querySelector('.ql-price'),number(m.price));
        text(row.querySelector('[data-time]'),timestamp(m.time));
        text(row.querySelector('[data-received]'),timestamp(m.received*1000));
        const gap=m.received-m.time/1000;
        text(row.querySelector('[data-gap]'),m.kind==='snapshot'?'最近快照，不以快照年齡判斷傳輸速度':gap<0?'裝置時鐘可能不同步，暫不計算':(gap/1000).toFixed(3)+(m.kind==='rest'?' 秒（成交距接收，包含等待成交，非傳輸延遲）':' 秒（觀測值，非延遲保證）'));
        text(row.querySelector('.ql-change'),m.delta==null?'第一筆，等待下一筆比較':(m.delta>0?'+':'')+number(m.delta)+' 元 · 比上次接收報價');
        row.dataset.tone=m.delta>0?'up':m.delta<0?'down':'flat';
        text(row.querySelector('.ql-quote-type'),m.closed?'收盤快照／收盤成交':m.kind==='snapshot'?'最近快照':m.kind==='rest'?'最後成交 · 富果定時查詢':'新成交 · 富果串流');
        if(m.flash){
          row.dataset.flash=m.flash;clearTimer(flashes.get(symbol));
          flashes.set(symbol,setTimeout(()=>{delete row.dataset.flash;flashes.delete(symbol);},300));m.flash='';
        }
      }
      text(row.querySelector('.ql-fetch-note'),m.fetchNote||'');
      const trial=row.querySelector('.ql-trial');trial.hidden=!m.trial;
      if(m.trial)text(trial,m.trial);
    });
    text($('#ql-events'),String(events));text($('#ql-retries'),String(retries));
  }
  function queueRender(){if(raf===null)raf=requestAnimationFrame(render);}
  function buildRows(){
    const host=$('#ql-quotes');host.querySelectorAll('.ql-empty').forEach(el=>el.remove());
    rows.forEach((row,code)=>{if(!allSymbols.includes(code)){clearTimer(flashes.get(code));flashes.delete(code);row.remove();rows.delete(code);models.delete(code);}});
    allSymbols.forEach((symbol,index)=>{
      let row=rows.get(symbol);
      if(!row){
        row=document.createElement('article');row.className='ql-quote';row.dataset.symbol=symbol;
        row.innerHTML='<div class="ql-quote-head"><div><div class="ql-symbol"></div><span class="ql-feed"></span><div class="ql-quote-type">等待資料</div></div><div><div class="ql-price">—</div><div class="ql-change">尚無成交資料</div></div></div><div class="ql-times"><div><small>行情時間 · 台北</small><b data-time>—</b></div><div><small>接收時間 · 台北</small><b data-received>—</b></div><div class="ql-gap"><small>觀測時間差</small><b data-gap>—</b></div></div><p class="ql-trial" hidden></p><p class="ql-fetch-note"></p>';
        rows.set(symbol,row);models.set(symbol,{});
      }
      text(row.querySelector('.ql-symbol'),(names[symbol]&&names[symbol]!==symbol?names[symbol]+' ':'')+symbol);
      const feed=row.querySelector('.ql-feed'),stream=symbols.includes(symbol);feed.dataset.mode=stream?'stream':'poll';
      text(feed,stream?'串流 · 新成交推送':'定時查詢 · 約 15 秒起');
      if(host.children[index]!==row)host.insertBefore(row,host.children[index]||null);
    });
    text($('#ql-count'),symbols.length+' / 5');queueRender();
  }
  function reconcile(){
    if(!running||!authenticated||!ws||ws.readyState!==1)return;
    const removing=[];
    channels.forEach((id,code)=>{if(!symbols.includes(code)&&!pendingUnsub.has(id)){removing.push(id);pendingUnsub.add(id);}});
    try{
      if(removing.length)ws.send(JSON.stringify({event:'unsubscribe',data:{ids:removing}}));
      // Wait for unsubscribe acknowledgements before allocating replacement slots.
      if(!pendingUnsub.size){
        const room=5-channels.size-pendingSub.size;
        const adding=symbols.filter(code=>!channels.has(code)&&!pendingSub.has(code)).slice(0,Math.max(0,room));
        if(adding.length){adding.forEach(code=>pendingSub.add(code));ws.send(JSON.stringify({event:'subscribe',data:{channel:'trades',symbols:adding}}));}
      }
      if((pendingSub.size||pendingUnsub.size)&&subTimer===null)subTimer=setTimeout(()=>{subTimer=null;if(running&&(pendingSub.size||pendingUnsub.size))reconnect();},12000);
      if(!pendingSub.size&&!pendingUnsub.size){clearTimer(subTimer);subTimer=null;}
    }catch(ignore){reconnect();}
  }
  function subscriptionAck(message){
    const entries=Array.isArray(message.data)?message.data:[message.data];
    entries.forEach(d=>{
      if(!d||d.channel!=='trades'||!d.id)return;
      if(message.event==='subscribed'){
        const code=String(d.symbol);if(!pendingSub.has(code)&&!channels.has(code))return;
        pendingSub.delete(code);channels.set(code,String(d.id));
      }else{
        pendingUnsub.delete(String(d.id));channels.forEach((id,code)=>{if(id===String(d.id))channels.delete(code);});
      }
    });
    clearTimer(subTimer);subTimer=null;reconcile();
  }
  function extraSymbols(){return allSymbols.filter(code=>!symbols.includes(code));}
  async function refreshHoldings(initial=false){
    if(!running||!following||holdController)return false;
    const mine=generation,controller=new AbortController();holdController=controller;
    const timeout=setTimeout(()=>controller.abort(),8000);
    try{
      const url=new window.URL(localURL('/web/quote-lab/holdings')),token='';
      if(token)url.searchParams.set('t',token);
      const response=await fetch(url.href,{credentials:'same-origin',cache:'no-store',redirect:'error',referrerPolicy:'no-referrer',signal:controller.signal});
      if(!running||mine!==generation)return false;
      if(response.status===401||response.status===403||response.status===404){failed('BOT 登入或帳號權限已失效，已停止行情，請重新登入。');return false;}
      if(!response.ok)throw new Error('holdings unavailable');
      const data=await response.json();
      if(!running||mine!==generation)return false;
      if(data.ok!==true||!Array.isArray(data.holdings)||data.holdings.some(h=>!h||!/^\d{4,6}[A-Z]?$/.test(h.code))||new Set(data.holdings.map(h=>h.code)).size!==data.holdings.length)throw new Error('invalid holdings');
      const nextAll=data.holdings.map(h=>h.code),held=new Set(nextAll);
      if(!nextAll.length){stop(data.unsupported_count?'BOT 沒有可查詢的上市櫃持股代碼，已停止行情。':'BOT 目前已無持股，已停止行情。');return false;}
      data.holdings.forEach(h=>{if(h.name)names[h.code]=String(h.name);});
      const next=[...new Set([...symbols.filter(c=>held.has(c)),...preferred.filter(c=>held.has(c)),...nextAll])].slice(0,5);
      const changed=JSON.stringify(allSymbols)!==JSON.stringify(nextAll)||JSON.stringify(symbols)!==JSON.stringify(next);
      allSymbols=nextAll;symbols=next;
      if(changed){buildRows();reconcile();}
      text($('#ql-holdings-status'),'BOT 持股 '+allSymbols.length+' 檔 · 串流 '+symbols.length+' 檔 · 定時查詢 '+extraSymbols().length+' 檔。'+(changed&&!initial?'已自動同步賣出／新增持股與串流名額。':'每 15 秒確認部位。'));
      return true;
    }catch(ignore){
      if(running&&mine===generation){
        if(initial)failed('無法確認 BOT 持股，未開始行情。請稍後重新輸入金鑰。');
        else text($('#ql-holdings-status'),'BOT 持股同步暫時失敗，保留上次清單，不會把失敗當成全部賣出。');
      }
      return false;
    }finally{
      clearTimeout(timeout);if(holdController===controller)holdController=null;
      if(running&&following&&mine===generation)holdTimer=setTimeout(()=>{holdTimer=null;refreshHoldings();},15000);
    }
  }
  function scheduleRest(delay){clearTimer(restTimer);restTimer=null;if(running&&following)restTimer=setTimeout(()=>{restTimer=null;pollRest();},delay);}
  const afterClose=()=>Number(new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Taipei',hour:'2-digit',hourCycle:'h23'}).format(new Date()))*60+Number(new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Taipei',minute:'2-digit'}).format(new Date()))>=810;
  function restSymbols(){return [...new Set([...symbols.filter(c=>!models.get(c)?.previousClose||models.get(c)?.previousCloseDay!==today()||(afterClose()&&!models.get(c)?.closed)),...extraSymbols()])];}
  async function pollRest(){
    if(!running||!following||restController)return;
    const codes=restSymbols();
    if(!codes.length&&Date.now()-indexAttempt<15000){scheduleRest(Math.max(1500,15000-(Date.now()-indexAttempt)));return;}
    const now=Date.now();while(requestTimes.length&&now-requestTimes[0]>=60000)requestTimes.shift();
    const blockedUntil=Math.max(restPause,requestTimes.length>=40?requestTimes[0]+60000:0);
    if(blockedUntil>now){text($('#ql-rest-status'),'定時查詢暫停約 '+Math.ceil((blockedUntil-now)/1000)+' 秒，等待免費額度恢復；串流照常。');scheduleRest(Math.max(1000,blockedUntil-now));return;}
    const unseeded=allSymbols.filter(c=>!models.get(c)?.previousClose||models.get(c)?.previousCloseDay!==today()||(afterClose()&&!models.get(c)?.closed));
    const indexDue=Date.now()-indexAttempt>=15000;
    const code=indexDue?'IX0001':unseeded.sort((a,b)=>(models.get(a)?.baselineAttempt||0)-(models.get(b)?.baselineAttempt||0))[0]||codes[restCursor++%codes.length],mine=generation,controller=new AbortController();restController=controller;
    if(code==='IX0001')indexAttempt=Date.now();else models.get(code).baselineAttempt=Date.now();
    const timeout=setTimeout(()=>controller.abort(),8000);requestTimes.push(now);
    try{
      const response=await fetch(REST+encodeURIComponent(code),{headers:{'X-API-KEY':apiKey},credentials:'omit',cache:'no-store',redirect:'error',referrerPolicy:'no-referrer',signal:controller.signal});
      if(!running||mine!==generation)return;
      if(response.status===403&&code==='IX0001'){text($('#ql-rest-status'),'富果暫未提供加權指數權限，持股行情照常更新。');return;}
      if(response.status===401||response.status===403){failed(response.status===401?'富果行情金鑰已失效，已清除記錄，請重新輸入。':'富果查詢權限不足，已停止連線；已記住的金鑰保留。',response.status===401);return;}
      if(response.status===429){
        const raw=response.headers.get('Retry-After'),seconds=Number(raw),until=Number.isFinite(seconds)?Date.now()+Math.max(60,seconds)*1000:Date.parse(raw||'');
        restPause=Number.isFinite(until)?Math.max(Date.now()+60000,until):Date.now()+60000;
        text($('#ql-rest-status'),'富果回報查詢額度已滿，暫停定時查詢至少 60 秒；不會重試轟炸，串流照常。');return;
      }
      if(!response.ok)throw new Error('quote unavailable');
      const quote=await response.json();if(!running||mine!==generation)return;
      if(code==='IX0001'){
        if(quote.symbol!=='IX0001'||quote.type!=='INDEX'||quote.date!==today()||quote.isTrial===true)throw new Error('invalid index');
        indexQuote={code:'IX0001',type:'INDEX',price:quote.closePrice,previous_close:quote.previousClose??quote.referencePrice,time:quote.closeTime,observed_at:Date.now(),closed:quote.isClose===true,mode:'rest'};
        requestApply();return;
      }
      if(!models.has(code))return;
      if(String(quote.symbol)!==code||quote.type==='ODDLOT')throw new Error('wrong symbol');
      if(quote.date!==today())throw new Error('old trading day');
      const prior=Number(quote.previousClose),reference=Number(quote.referencePrice);
      const baseline=Number.isFinite(prior)&&prior>0?prior:reference;
      if(Number.isFinite(baseline)&&baseline>0){models.get(code).previousClose=baseline;models.get(code).referencePrice=Number.isFinite(reference)&&reference>0?reference:baseline;models.get(code).previousCloseDay=quote.date;}
      if(quote.name){names[code]=String(quote.name);text(rows.get(code).querySelector('.ql-symbol'),names[code]+' '+code);}
      const trade=quote.lastTrade&&quote.lastTrade.price!=null&&quote.lastTrade.time!=null?quote.lastTrade:{price:quote.closePrice,time:quote.closeTime,serial:null};
      const parsedPrice=Number(trade.price),parsedTime=Number(trade.time);
      if(!Number.isFinite(parsedPrice)||parsedPrice<=0||!Number.isSafeInteger(parsedTime)){
        if(quote.isTrial===true&&quote.lastTrial)accept({event:'rest',channel:'trades',data:{...quote.lastTrial,symbol:code,isTrial:true}});
        models.get(code).fetchNote='目前尚無正式成交，試撮參考另行顯示。';queueRender();return;
      }
      accept({event:'rest',channel:'trades',data:{symbol:code,type:quote.type,price:trade.price,time:trade.time,serial:trade.serial,isClose:quote.isClose===true}});
      if(quote.isTrial===true&&quote.lastTrial)accept({event:'rest',channel:'trades',data:{...quote.lastTrial,symbol:code,isTrial:true}});
      const model=models.get(code);if(model)model.fetchNote='';queueRender();
      text($('#ql-rest-status'),'其他 '+codes.length+' 檔依序查詢，約 '+Math.ceil(Math.max(15,codes.length*1.5))+' 秒／輪，視回應速度調整。本頁近 60 秒 '+requestTimes.length+'／40 次。');
    }catch(ignore){
      if(running&&mine===generation&&models.has(code)){models.get(code).fetchNote='查詢暫時失敗，保留最後行情；不以本次讀取時間冒充成交時間。';queueRender();text($('#ql-rest-status'),'部分定時查詢暫時失敗，會在下一輪再試；串流照常。');}
    }finally{
      clearTimeout(timeout);if(restController===controller)restController=null;
      if(running&&mine===generation)scheduleRest(Math.max(1500,15000/Math.max(1,restSymbols().length)));
    }
  }
  async function launch(){
    if(following){if(!await refreshHoldings(true)||!running)return;}
    else{allSymbols=symbols.slice();buildRows();text($('#ql-holdings-status'),'手動測試模式：不自動讀取或替換 BOT 持股。');}
    connect();if(following){scheduleRest(0);requestApply();}
  }
  function accept(message){
    const d=message.data;if(!d||message.channel!=='trades'||!models.has(String(d.symbol)))return;
    if(d.type==='ODDLOT'||(message.event!=='rest'&&!symbols.includes(String(d.symbol))))return;
    const time=Number(d.time),price=Number(d.price),serial=d.serial==null||!Number.isFinite(Number(d.serial))?null:Number(d.serial);
    if(!Number.isSafeInteger(time)||time<1577836800000000||time>4102444800000000||!Number.isFinite(price)||price<=0)return;
    if(new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Taipei',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(time/1000))!==today())return;
    const model=models.get(String(d.symbol));
    if(model.time!==undefined&&time<model.time)return;
    if(d.isTrial===true){
      if(!model.trialTime||time>=model.trialTime){model.trialTime=time;model.trial='試撮參考 '+number(price)+' · '+timestamp(time)+'（非正式成交）';queueRender();}
      return;
    }
    if(message.event==='rest'&&model.time===time&&model.price===price){model.received=Date.now();model.kind='rest';model.closed=d.isClose===true;queueRender();requestApply();return;}
    if(model.time!==undefined&&(time<model.time||(time===model.time&&((serial!==null&&model.serial!==null&&serial<=model.serial)||(price===model.price&&serial===model.serial)))))return;
    const previous=model.price;
    model.delta=message.event==='snapshot'||previous===undefined?null:price-previous;
    model.price=price;model.time=time;model.serial=serial;model.received=Date.now();model.kind=message.event;model.closed=d.isClose===true;
    model.trial='';model.trialTime=0;
    if(message.event==='data'){events++;model.flash=model.delta>0?'up':model.delta<0?'down':'';}
    if(message.event!=='rest')failures=0;
    if(message.event!=='rest')status('live','已連線',model.closed?'已收到收盤資料；請在交易時段驗證新成交的更新速度。':message.event==='snapshot'?'已收到最近快照，等待下一筆新成交。':'正在接收新成交，價格只在有新資料時更新。');
    queueRender();requestApply();
  }
  function reconnect(){
    dropSocket();if(!running||disposed)return;
    if(failures>=6){failed('連線多次失敗，已停止重試。請確認網路後按「啟用」，已記住的金鑰可直接沿用。');return;}
    const delay=Math.min(30000,1000*Math.pow(2,failures));failures++;retries++;
    text($('#ql-retries'),String(retries));status('retry','等待重連','連線中斷，保留最後行情；'+delay/1000+' 秒後重新連線。');
    const mine=generation;retryTimer=setTimeout(()=>{retryTimer=null;if(running&&mine===generation)connect();},delay);
  }
  function connect(){
    if(!running||disposed||document.hidden)return;
    if(ws)return;
    status('connecting','連接中','正在建立富果連線…');
    let socket;try{socket=new WebSocket(URL);}catch(ignore){reconnect();return;}
    ws=socket;lastMessage=Date.now();const mine=generation;
    const current=()=>running&&!disposed&&ws===socket&&mine===generation;
    phaseTimer=setTimeout(()=>{if(current())reconnect();},15000);
    socket.onopen=()=>{
      if(!current())return;
      status('connecting','驗證中','正在驗證行情金鑰…');
      try{socket.send(JSON.stringify({event:'auth',data:{apikey:apiKey}}));}catch(ignore){reconnect();}
    };
    socket.onmessage=event=>{
      if(!current())return;
      lastMessage=Date.now();let message;
      try{message=JSON.parse(event.data);}catch(ignore){return;}
      if(!message||typeof message!=='object')return;
      if(message.event==='authenticated'){
        clearTimer(phaseTimer);phaseTimer=null;
        authenticated=true;rememberVerified();reconcile();
        status('live','已連線','已驗證，等待 5 檔以內的成交資料。收盤後可能只有最近快照。');
        phaseTimer=setTimeout(()=>{if(current()&&events===0)text($('#ql-message'),'連線正常，尚無新成交。收盤後、尚未成交或代碼無效時可能沒有資料，請確認交易時段與代碼。');},15000);
        if(healthTimer!==null)clearInterval(healthTimer);
        healthTimer=setInterval(()=>{if(current()&&Date.now()-lastMessage>65000)reconnect();},10000);
      }else if(message.event==='subscribed'||message.event==='unsubscribed'){
        subscriptionAck(message);
      }else if(message.event==='snapshot'||message.event==='data'){
        accept(message);
      }else if(message.event==='error'){
        const code=Number(message.code);
        if(code===1011){reconnect();return;}
        const explanations={1000:'金鑰無效或目前方案不支援，請確認金鑰與免費方案權限。',1001:'超出訂閱上限，請確認此金鑰是否也在其他程式使用。',1002:'富果拒絕此訂閱，請確認帳號權限。',1003:'連線數已滿或股票訂閱不受支援，請關閉其他測試連線並確認代碼。'};
        failed((explanations[code]||'富果回報無法繼續訂閱。')+(code===1000?' 已清除記錄，請重新輸入。':' 已停止連線，已記住的金鑰仍保留。'),code===1000);
      }
    };
    socket.onerror=()=>{if(current())reconnect();};
    socket.onclose=event=>{
      if(!current())return;
      if(event.code===1013||event.code===1008||event.code===1005||(event.code===1001&&String(event.reason).includes('plan'))){failed('連線遭拒或已達方案限制，請檢查金鑰、權限及其他連線。已停止本次連線；已記住的金鑰保留。');return;}
      reconnect();
    };
  }
  async function begin(){
    if(running||disposed)return;
    if(document.hidden){status('idle','請回到前景','請在可見的測試頁開始連線。');return;}
    const selected=symbolsInput.value.toUpperCase().split(/[\s,，、]+/).filter(Boolean);
    if((selected.length===0&&!followInput.checked)||selected.length>5||new Set(selected).size!==selected.length||selected.some(s=>!/^\d{4,6}[A-Z]?$/.test(s))){status('error','請確認代碼','請輸入 1～5 個不重複的上市櫃股票代碼，以逗號或空格分隔。');return;}
    const supplied=keyInput.value.trim()||(rememberInput.checked&&saved?saved.key:'');if(!supplied){status('error','尚未輸入金鑰','請輸入你申請的富果行情 API Key。');return;}
    restoreBlocked=false;apiKey=supplied;keyInput.value='';if(location.pathname!=='/web/quote-lab')panel.open=false;preferred=selected;following=followInput.checked;symbols=following?[]:selected;allSymbols=[];events=retries=failures=0;restPause=0;running=true;generation++;controls();
    indexQuote=null;indexAttempt=0;rows.clear();models.clear();flashes.forEach(clearTimer);flashes.clear();$('#ql-quotes').replaceChildren();text($('#ql-events'),'0');text($('#ql-retries'),'0');status('connecting','準備中',following?'正在確認你的 BOT 持股…':'正在準備串流…');
    acquireConnection();
  }
  async function acquireConnection(){
    if(!running||disposed||paused||document.hidden||lockGeneration===generation)return;
    const mine=generation;lockGeneration=mine;
    try{
      if(navigator.locks&&navigator.locks.request){
        await navigator.locks.request('stockbot-private-quote-lab',{ifAvailable:true},async lock=>{
          if(!running||mine!==generation)return;
          if(!lock){
            status('retry','等待連線','另一個分頁正在使用富果行情，名額釋放後會自動連線。');
            clearTimer(retryTimer);retryTimer=setTimeout(()=>{retryTimer=null;if(running&&mine===generation)acquireConnection();},2000);
            return;
          }
          await new Promise(resolve=>{releaseLock=resolve;launch();});
        });
      }else{launch();}
    }catch(ignore){if(running&&mine===generation)failed('無法取得行情連線，已停止本次連線；已記住的金鑰保留。請稍後按啟用重試。');}
    finally{if(lockGeneration===mine)lockGeneration=null;}
  }
  function ensureAllocation(items){
    let box=document.querySelector('.position-allocation');
    if(!box){box=document.createElement('section');box.className='position-allocation';box.innerHTML='<div class="position-allocation-head"><h2>持股配置</h2><button type="button" data-allocation-reset>顯示全部</button></div><div class="position-allocation-body"><div class="position-allocation-visual"><svg viewBox="0 0 100 100" role="img" aria-label="富果行情持股市值權重"><circle cx="50" cy="50" r="31" fill="#fff"/></svg><div class="position-allocation-center"><b data-allocation-center-title>全部持股</b><strong data-allocation-center-weight data-home-sensitive>100%</strong><small data-allocation-center-value data-home-sensitive></small></div></div><div class="position-allocation-legend"></div></div><div class="allocation-selection" aria-live="polite"><span>組合概覽</span><strong>點選圓環或股票名稱</strong><small></small></div>';const card=document.querySelector('[data-position-code]');card.parentNode.insertBefore(box,card);}
    const held=new Set(items.map(x=>x.code));box.querySelectorAll('[data-allocation-select],[data-allocation-code]').forEach(n=>{if(!held.has(n.dataset.allocationSelect||n.dataset.allocationCode))n.remove();});
    const colors=['#2676A5','#D84B45','#14916C','#8165B5','#D29430','#3A8FA3','#CF6A36','#6E8498'];
    items.forEach((item,index)=>{
      if(box.querySelector('[data-allocation-select="'+item.code+'"]'))return;
      const button=document.createElement('button'),dot=document.createElement('i'),label=document.createElement('span'),value=document.createElement('b');button.type='button';button.className='allocation-item';button.dataset.allocationSelect=item.code;button.dataset.allocationName=names[item.code]||item.code;button.setAttribute('aria-pressed','false');value.setAttribute('data-home-sensitive','');dot.style.background=colors[index%colors.length];label.textContent=names[item.code]||item.code;button.append(dot,label,value);box.querySelector('.position-allocation-legend').append(button);
      const circle=document.createElementNS('http://www.w3.org/2000/svg','circle');circle.classList.add('allocation-slice');circle.dataset.allocationCode=item.code;Object.entries({cx:50,cy:50,r:40,fill:'none',stroke:colors[index%colors.length],'stroke-width':12,transform:'rotate(-90 50 50)',tabindex:0,role:'button','aria-label':names[item.code]||item.code}).forEach(([k,v])=>circle.setAttribute(k,v));const svg=box.querySelector('svg');svg.insertBefore(circle,svg.lastElementChild);
    });return box;
  }
  function applyValuations(data){
    const positions=data.positions||{},totals=positions.totals||{},complete=totals.complete===true;
    const money=(n,d=0)=>Number(n).toLocaleString('zh-TW',{minimumFractionDigits:d,maximumFractionDigits:d});
    const pct=n=>(Number(n)>=0?'+':'')+money(n,2)+'%';
    const setPrivate=(el,shown)=>{if(!el)return;el.setAttribute('data-home-original',shown);el.textContent=window.__stockbotHomePrivacyMasked===true?'******':shown;};
    if(complete)document.querySelectorAll('[data-fugle-total]').forEach(el=>{const field=el.dataset.fugleTotal,value=totals[field],shown=value==null?'資料不足':field==='net_pct'?pct(value):money(value);if(el.hasAttribute('data-home-sensitive')){el.dataset.homeOriginal=shown;if(!el.closest('.is-privacy-hidden'))el.textContent=shown;}else el.textContent=shown;if(['net_pl','net_pct','day_pl'].includes(field)){el.classList.toggle('up',value>=0);el.classList.toggle('down',value<0);}});
    const hero=document.querySelector('[data-home-privacy-card] .hv2-px[data-home-sensitive]');
    if(hero&&complete){const value=totals.value==null?'資料不足':money(totals.value);hero.dataset.homeOriginal=value;if(!hero.closest('.is-privacy-hidden'))hero.textContent=value;const gain=document.querySelector('#hv2 .hv2-head-r>b');if(gain){gain.textContent=totals.net_pct==null?'資料不足':pct(totals.net_pct);gain.classList.toggle('up',totals.net_pct>=0);gain.classList.toggle('down',totals.net_pct<0);}}
    (positions.updates||[]).forEach(item=>{
      const card=document.querySelector('[data-position-code="'+item.code+'"]');
      if(card){
        const target=card.querySelector('.position-target-summary em');if(target&&item.target_pct!=null&&item.weight!=null){const diff=item.weight-item.target_pct;setPrivate(target,Math.abs(diff)<=0.05?'符合目標':(diff>0?'高於目標 ':'低於目標 ')+money(Math.abs(diff),1)+' 個百分點');}
        const weightBar=card.querySelector('.position-card-weight i');if(weightBar&&item.weight!=null)weightBar.style.width=(item.weight==null?0:Math.min(100,item.weight/30*100))+'%';
        const summary=card.querySelector('.position-card-weight>b');if(summary&&item.weight!=null)setPrivate(summary,money(item.weight,1)+'%');
        if(card.classList.contains('position-card-noquote')){
          card.classList.remove('position-card-noquote');const primary=card.querySelector('.position-card-primary');if(primary){primary.replaceChildren();const brokerage=!!card.closest('#portfolio-v2');const metrics=brokerage?[['今日損益',item.day_pl,'data-position-day-pl'],['市值',item.value,'data-position-value'],['未實現損益',item.net_pl,'data-position-net']]:[['今日損益',item.day_pl,'data-position-day-pl'],['市值',item.value,'data-position-value'],['持有報酬',item.gross_pct,'data-position-gross']];metrics.forEach(([label,value,attr])=>{const col=document.createElement('div'),small=document.createElement('small'),b=document.createElement('b');small.textContent=label;b.setAttribute(attr,'');b.setAttribute('data-home-sensitive','');const shown=value==null?'—':attr==='data-position-gross'?pct(value):money(value);b.setAttribute('data-home-original',shown);b.textContent=window.__stockbotHomePrivacyMasked===true?'******':shown;if(attr!=='data-position-value'){b.classList.toggle('up',value>=0);b.classList.toggle('down',value<0);}col.append(small,b);if(brokerage&&attr==='data-position-net'){const sub=document.createElement('em');sub.dataset.positionNetPct='';sub.setAttribute('data-home-sensitive','');const pctShown=item.net_pct==null?'':pct(item.net_pct);sub.setAttribute('data-home-original',pctShown);sub.textContent=window.__stockbotHomePrivacyMasked===true?'******':pctShown;col.append(sub);}primary.append(col);});}
          if(card.closest('#portfolio-v2')){const weight=Number(item.weight)||0,track=card.querySelector('.p2-weight-track i');if(track)track.style.width=Math.max(0,Math.min(100,weight))+'%';card.dataset.sortValue=String(item.value||0);card.dataset.sortPl=String(item.net_pl||0);card.dataset.sortPct=String(item.net_pct||0);card.dataset.sortWeight=String(weight);}
        }
      }
      const legend=document.querySelector('[data-allocation-select="'+item.code+'"]');
      if(legend&&complete&&item.weight!=null){legend.dataset.allocationWeight=item.weight;legend.dataset.allocationValue=item.value;const text=legend.querySelector('b');if(text)setPrivate(text,money(item.weight,1)+'%');}
    });
    const allocation=complete&&document.querySelector('[data-position-code]')?ensureAllocation(positions.updates||[]):document.querySelector('.position-allocation');
    if(allocation){
      allocation.setAttribute('aria-label',complete?'富果行情持股配置':'等待完整富果行情，配置保留前次資料');
      if(complete){let offset=0;allocation.querySelectorAll('.allocation-slice').forEach(slice=>{const item=(positions.updates||[]).find(x=>x.code===slice.dataset.allocationCode);if(!item)return;const legend=allocation.querySelector('[data-allocation-select="'+item.code+'"]');if(legend){legend.dataset.allocationWeight=item.weight;legend.dataset.allocationValue=item.value;setPrivate(legend.querySelector('b'),money(item.weight,1)+'%');}const arc=2*Math.PI*40*item.weight/100;slice.setAttribute('stroke-dasharray',arc+' '+(2*Math.PI*40-arc));slice.setAttribute('stroke-dashoffset',-offset);offset+=arc;});const selected=allocation.querySelector('[data-allocation-select][aria-pressed=true]');const title=allocation.querySelector('[data-allocation-center-title]'),weight=allocation.querySelector('[data-allocation-center-weight]'),value=allocation.querySelector('[data-allocation-center-value]');if(weight)setPrivate(weight,selected?money(selected.dataset.allocationWeight,1)+'%':'100%');if(value)setPrivate(value,money(selected?selected.dataset.allocationValue:totals.value)+' 元');}
    }
  }
  // Keep complete valuation snapshots intact while a new quote batch is incomplete.
  // A server-generated context prevents restoring valuations after position/settings changes.
  const viewStorageKey=storageKey?storageKey.replace('fugle_device_v1:','fugle_view_v1:'):null;
  let lastCompleteView=null,lastViewContext=null,lastViewWrite=0,latestDisplayedAt=0;
  function validView(record){
    const data=record&&record.data,updates=data?.positions?.updates,totals=data?.positions?.totals;
    return record?.version===1&&Number.isFinite(record.savedAt)&&Date.now()-record.savedAt>=-300000&&Date.now()-record.savedAt<7*86400000&&
      /^[a-f0-9]{64}$/.test(data?.view_context||'')&&/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(data?.view_as_of||'')&&totals?.complete===true&&Number.isFinite(totals.value)&&
      Number.isFinite(data?.home?.portfolio?.pct)&&Array.isArray(updates)&&updates.length>0&&updates.length<=60&&
      updates.every(row=>row&&typeof row.code==='string'&&/^\d{4,6}[A-Z]?$/.test(row.code)&&Number.isFinite(row.price)&&Number.isFinite(row.value));
  }
  function clearView(){lastCompleteView=null;lastViewWrite=0;try{if(viewStorageKey)localStorage.removeItem(viewStorageKey);}catch(ignore){}}
  function readView(){
    try{const raw=viewStorageKey&&localStorage.getItem(viewStorageKey);if(raw&&raw.length<=131072){const record=JSON.parse(raw);if(validView(record))return record;}}catch(ignore){}
    return null;
  }
  function clearOldAggregates(){
    document.querySelectorAll('[data-fugle-total],[data-home-portfolio-pct],[data-home-relative],[data-home-judgement-relative]').forEach(el=>{if(el.hasAttribute('data-home-sensitive')){el.dataset.homeOriginal='更新中';if(!el.closest('.is-privacy-hidden'))el.textContent='更新中';}else el.textContent='更新中';el.classList.remove('up','down');});
    const hero=document.querySelector('[data-home-privacy-card] .hv2-px[data-home-sensitive]');
    if(hero){hero.dataset.homeOriginal='更新中';if(!hero.closest('.is-privacy-hidden'))hero.textContent='更新中';}
    const gain=document.querySelector('#hv2 .hv2-head-r>b');if(gain){gain.textContent='更新中';gain.classList.remove('up','down');}
  }
  function viewStamp(value){
    const match=String(value||'').match(/\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?/);
    return match?Date.parse(match[0].replace(' ','T')+'+08:00')||0:0;
  }
  function visibleViewStamp(){
    const home=document.getElementById('hv2'),portfolio=document.querySelector('[data-home-portfolio-pct]');
    const visible=portfolio&&/%/.test(portfolio.textContent)?viewStamp(document.querySelector('[data-home-market-freshness]')?.textContent):0;
    return Math.max(latestDisplayedAt,Number(home?.dataset.quoteViewAt||0),visible);
  }
  function viewFor(data){
    if(lastCompleteView&&!validView(lastCompleteView))clearView();
    if(!lastCompleteView)lastCompleteView=readView();
    const context=data.view_context,changed=!!context&&((lastViewContext&&lastViewContext!==context)||(lastCompleteView&&lastCompleteView.data.view_context!==context));
    lastViewContext=context||lastViewContext;
    if(changed){clearView();clearOldAggregates();latestDisplayedAt=0;const home=document.getElementById('hv2');if(home)delete home.dataset.quoteViewAt;}
    const complete=data.positions?.totals?.complete===true&&Number.isFinite(data.home?.portfolio?.pct);
    const floor=visibleViewStamp(),incomingAt=viewStamp(data.view_as_of);
    if(complete&&incomingAt>=floor){
      latestDisplayedAt=Math.max(floor,incomingAt);
      const record={version:1,savedAt:Date.now(),data};
      if(validView(record)){
        lastCompleteView=record;
        if(Date.now()-lastViewWrite>=15000){try{if(viewStorageKey)localStorage.setItem(viewStorageKey,JSON.stringify(record));lastViewWrite=Date.now();}catch(ignore){}}
      }
      return data;
    }
    const count=data.positions?.updates?.length||0,progress='富果行情更新中 '+count+'／'+allSymbols.length+' 檔';
    const previous=lastCompleteView?.data;
    if(previous&&previous.view_context===context&&viewStamp(previous.view_as_of)>=floor){
      const message=progress+'；沿用上次完整資料 '+previous.view_as_of+'，新行情完整後更新。';
      return {...previous,home:{...previous.home,market:previous.home.market?{...previous.home.market,stale:true}:null,note:message,quote_freshness:{text:message},view_retained:true},positions:{...previous.positions,note:message,view_retained:true}};
    }
    const message=progress+(changed?'；持股或設定已變更，正在重新計算。':'；保留頁面原有數值，等待完整行情。');
    return {...data,home:{...data.home,portfolio:{...data.home.portfolio,ready:false},note:message,quote_freshness:{text:message}},positions:{...data.positions,totals:{...data.positions?.totals,complete:false}}};
  }
  function quoteRows(){
    const now=Date.now(),healthy=authenticated&&ws&&ws.readyState===1&&now-lastMessage<45000;
    return allSymbols.flatMap(code=>{const m=models.get(code);if(!m||m.price===undefined||!m.previousClose||m.previousCloseDay!==today())return [];const stream=symbols.includes(code)&&channels.has(code);return [{code,price:m.price,previous_close:m.previousClose,reference_price:m.referencePrice,time:m.time,observed_at:stream&&healthy?now:m.received,closed:m.closed===true,mode:stream?'stream':'rest'}];});
  }
  function requestApply(){
    if(!running||!following||paused||disposed||document.hidden||!relevant()||applyController||applyTimer!==null)return;
    applyTimer=setTimeout(()=>{applyTimer=null;apply();},Math.max(30,2000-(Date.now()-lastApply)));
  }
  async function apply(){
    if(!running||!following||paused||document.hidden||applyController)return;
    const rows=quoteRows();
    const controller=new AbortController(),mine=generation;applyController=controller;lastApply=Date.now();
    const deadline=setTimeout(()=>controller.abort(),10000);
    try{
      const body=new URLSearchParams({csrf_token:config.csrf_token,quotes:JSON.stringify(rows)});
      if(indexQuote)body.set('market',JSON.stringify(indexQuote));
      const response=await fetch(localURL('/web/quote-lab/apply'),{method:'POST',credentials:'same-origin',cache:'no-store',redirect:'error',body,signal:controller.signal});
      if(!running||mine!==generation)return;
      if(response.status===401||response.status===403){failed('BOT 登入已失效，已停止富果行情，請重新登入 BOT。');return;}
      if(!response.ok)throw new Error('calculation unavailable');
      const data=await response.json();if(!running||mine!==generation||!data.ok)return;
      const view=viewFor(data);document.dispatchEvent(new CustomEvent('stockbot:fuglequotes',{detail:view}));applyValuations(view);text(panelLabel,'富果行情 · '+(data.positions?.updates?.length||0)+'／'+allSymbols.length+(data.positions?.totals?.complete?' 檔已更新':' 檔 · 更新中'));
    }catch(ignore){if(running&&mine===generation){
      const message='行情更新暫時失敗；'+(lastCompleteView?'沿用上次完整資料 '+lastCompleteView.data.view_as_of:'保留畫面現有資料')+'，稍後自動重試。';
      text($('#ql-message'),message);document.querySelectorAll('[data-home-quote-quality],[data-home-live-note]').forEach(el=>text(el,message));
      const title=document.querySelector('[data-home-live-title]');if(title)text(title,'行情暫時無法更新');
    }}
    finally{clearTimeout(deadline);if(applyController===controller)applyController=null;if(running&&mine===generation&&!paused)applyTimer=setTimeout(()=>{applyTimer=null;requestApply();},5000);}
  }
  function detach(){panel.hidden=true;document.body.appendChild(panel);queueMount();}
  function visibility(){
    if(document.hidden){
      if(!running)return;paused=true;generation++;dropSocket();clearTimer(retryTimer);retryTimer=null;if(releaseLock){releaseLock();releaseLock=null;}clearTimer(holdTimer);clearTimer(restTimer);clearTimer(applyTimer);holdTimer=restTimer=applyTimer=null;
      if(holdController)holdController.abort();if(restController)restController.abort();if(applyController)applyController.abort();holdController=restController=applyController=null;
      status('idle','背景暫停','回到前景後會重新確認持股與行情；未儲存的金鑰僅保留在本分頁記憶體。');
    }else if(running&&paused){paused=false;acquireConnection();requestApply();}
    else if(!running)restoreSaved();
  }
  function pagehide(){stop('頁面已關閉；已記住的裝置記錄會保留。');}
  start.addEventListener('click',begin);stopButton.addEventListener('click',()=>{disableAutoStart();stop('連線已停止；已記住的金鑰保留，按啟用可恢復。');});
  function forget(message){clearView();const cleared=removeSaved();restoreBlocked=true;rememberInput.checked=false;stop(cleared?message:'本次連線已停止，但裝置記錄無法清除；請至瀏覽器設定清除這個網站的資料。');}
  forgetButton.addEventListener('click',()=>forget('已清除這個裝置的金鑰與自動連線記錄。'));
  changeButton.addEventListener('click',()=>{forget('請輸入新的富果 API Key。');panel.open=true;keyInput.focus();});
  rememberInput.addEventListener('change',()=>{if(!rememberInput.checked)removeSaved();else rememberVerified();});
  keyInput.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();begin();}});
  document.addEventListener('visibilitychange',visibility);document.addEventListener('stockbot:pageleaving',detach);window.addEventListener('pagehide',pagehide);window.addEventListener('popstate',queueMount);
  const observer=new MutationObserver(queueMount);observer.observe(document.documentElement,{childList:true,subtree:true});
  window.StockBotFugle={active:()=>running&&following,request:requestApply,resume:()=>{restoreSaved();requestApply();}};
  function restoreSaved(){
    if(config.background_enabled)return;
    if(running||disposed||document.hidden||restoreBlocked)return;
    saved=readSaved();rememberInput.checked=!!saved;showRemember();
    if(!saved)return;
    symbolsInput.value=saved.symbols;followInput.checked=saved.follow;
    if(saved.autoStart)begin();
  }
  window.addEventListener('storage',event=>{
    if(event.key!==storageKey&&event.key!==null)return;
    const previous=saved;saved=readSaved();rememberInput.checked=!!saved;
    if(running&&(!saved||!saved.autoStart||(previous&&saved.key!==previous.key)))stop('其他分頁已變更裝置記錄，這個分頁已停止。');
    showRemember();
  });
  controls();
  if(config.background_enabled){
    text($('#ql-mode-status'),'伺服器背景更新已啟用，盤中排程不依賴 App 是否開啟。');
    // Server owns provider access. Never open a second browser connection or
    // read/migrate its saved key when background mode is enabled.
    start.disabled=true;keyInput.disabled=true;stopButton.disabled=true;
    changeButton.disabled=true;rememberInput.disabled=true;
    text(panelLabel,'富果行情 · 伺服器背景更新');
    text($('#ql-message'),'關閉 App 後仍由伺服器定時更新；本頁只讀取完成的行情。');
    let backgroundTimer=null,backgroundController=null;
    async function readBackground(){
      clearTimeout(backgroundTimer);backgroundTimer=null;
      if(document.hidden||backgroundController||!relevant())return;
      const controller=new AbortController();backgroundController=controller;
      const deadline=setTimeout(()=>controller.abort(),8000);
      try{
        const response=await fetch(localURL('/web/quote-lab/background'),{credentials:'same-origin',cache:'no-store',redirect:'error',signal:controller.signal});
        if(!response.ok)throw new Error('background snapshot unavailable');
        const data=await response.json();if(!data.ok||!data.view)return;
        allSymbols=data.codes||[];
        const view=viewFor(data.view);
        document.dispatchEvent(new CustomEvent('stockbot:fuglequotes',{detail:view}));
        applyValuations(view);
        text(panelLabel,'富果行情 · 背景資料 '+(data.view.positions?.updates?.length||0)+'／'+allSymbols.length+' 檔');
        text($('#ql-message'),['credential_or_permission_error','rate_limited','partial','storage_unavailable'].includes(data.status)?'背景更新尚未完整（'+data.status+'），保留最後有效行情。':data.view.positions?.note||'等待首份背景行情。');
      }catch(ignore){const message='背景行情暫時無法讀取，保留畫面最後有效資料；稍後自動重試。';text($('#ql-message'),message);document.querySelectorAll('[data-home-quote-quality],[data-home-live-note]').forEach(el=>text(el,message));}
      finally{clearTimeout(deadline);backgroundController=null;if(!document.hidden)backgroundTimer=setTimeout(readBackground,15000);}
    }
    const resumeBackground=()=>{if(!document.hidden)readBackground();else{clearTimeout(backgroundTimer);backgroundTimer=null;if(backgroundController)backgroundController.abort();}};
    document.addEventListener('visibilitychange',resumeBackground);
    document.addEventListener('stockbot:pagecontentready',resumeBackground);
    window.addEventListener('pagehide',()=>{clearTimeout(backgroundTimer);if(backgroundController)backgroundController.abort();});
    window.StockBotFugle={active:()=>true,request:readBackground,resume:resumeBackground};
    readBackground();
  }else{
    text($('#ql-mode-status'),'目前為裝置直連，關閉 App 會暫停；伺服器背景更新尚待啟用。');
    restoreSaved();
  }
})();
