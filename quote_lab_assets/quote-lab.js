(function(){
  'use strict';
  const root=document.getElementById('quote-lab');
  if(!root||root.dataset.ready==='1')return;
  root.dataset.ready='1';
  const $=s=>root.querySelector(s),keyInput=$('#ql-key'),symbolsInput=$('#ql-symbols'),start=$('#ql-start'),stopButton=$('#ql-stop'),followInput=$('#ql-follow');
  const REST='https://api.fugle.tw/marketdata/v1.0/stock/intraday/quote/';
  const URL='wss://api.fugle.tw/marketdata/v1.0/stock/streaming';
  const names={'2308':'台達電','8996':'高力','2383':'台光電','2368':'金像電','6442':'光聖'};
  const rows=new Map(),models=new Map(),flashes=new Map();
  const clock=new Intl.DateTimeFormat('zh-TW',{timeZone:'Asia/Taipei',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
  let apiKey='',symbols=[],ws=null,running=false,disposed=false,generation=0,retryTimer=null,phaseTimer=null,healthTimer=null,raf=null,releaseLock=null;
  let events=0,retries=0,failures=0,lastMessage=0;
  let following=false,preferred=[],allSymbols=[],authenticated=false,holdTimer=null,restTimer=null,subTimer=null;
  let holdController=null,restController=null,restCursor=0,restPause=0;
  const channels=new Map(),pendingSub=new Set(),pendingUnsub=new Set(),requestTimes=[];
  function text(el,value){if(el&&el.textContent!==value)el.textContent=value;}
  function status(state,label,message){$('#ql-state').dataset.state=state;text($('#ql-state'),label);text($('#ql-message'),message);}
  function controls(){start.disabled=running;stopButton.disabled=!running;keyInput.disabled=running;symbolsInput.disabled=running;followInput.disabled=running;}
  function clearTimer(id){if(id!==null)clearTimeout(id);}
  function dropSocket(){
    clearTimer(phaseTimer);phaseTimer=null;clearTimer(subTimer);subTimer=null;authenticated=false;channels.clear();pendingSub.clear();pendingUnsub.clear();
    if(healthTimer!==null)clearInterval(healthTimer);healthTimer=null;
    const old=ws;ws=null;
    if(old){old.onopen=old.onmessage=old.onclose=old.onerror=null;try{old.close(1000,'Client stopped');}catch(ignore){}}
  }
  function stop(message,state='idle',label='已停止'){
    generation++;running=false;apiKey='';keyInput.value='';clearTimer(retryTimer);retryTimer=null;dropSocket();
    clearTimer(holdTimer);clearTimer(restTimer);holdTimer=restTimer=null;
    if(holdController)holdController.abort();if(restController)restController.abort();holdController=restController=null;
    if(releaseLock){releaseLock();releaseLock=null;}
    controls();status(state,label,message);
  }
  function failed(message){stop(message,'error','連線已停止');}
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
      const url=new window.URL('/web/quote-lab/holdings',location.origin),token=new URLSearchParams(location.search).get('t');
      if(token)url.searchParams.set('t',token);
      const response=await fetch(url.href,{credentials:'same-origin',cache:'no-store',redirect:'error',referrerPolicy:'no-referrer',signal:controller.signal});
      if(!running||mine!==generation)return false;
      if(response.status===401||response.status===403||response.status===404){failed('BOT 登入或測試帳號權限已失效，已清除行情金鑰，請重新登入。');return false;}
      if(!response.ok)throw new Error('holdings unavailable');
      const data=await response.json();
      if(!running||mine!==generation)return false;
      if(data.ok!==true||!Array.isArray(data.holdings)||data.holdings.some(h=>!h||!/^\d{4,6}[A-Z]?$/.test(h.code))||new Set(data.holdings.map(h=>h.code)).size!==data.holdings.length)throw new Error('invalid holdings');
      const nextAll=data.holdings.map(h=>h.code),held=new Set(nextAll);
      if(!nextAll.length){stop(data.unsupported_count?'BOT 沒有可測試的上市櫃持股代碼，已清除金鑰。':'BOT 目前已無持股，已停止行情並清除金鑰。');return false;}
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
  async function pollRest(){
    if(!running||!following||restController)return;
    const codes=extraSymbols();
    if(!codes.length){text($('#ql-rest-status'),'所有持股都在串流名額內，不需額外查詢。');scheduleRest(15000);return;}
    const now=Date.now();while(requestTimes.length&&now-requestTimes[0]>=60000)requestTimes.shift();
    const blockedUntil=Math.max(restPause,requestTimes.length>=40?requestTimes[0]+60000:0);
    if(blockedUntil>now){text($('#ql-rest-status'),'定時查詢暫停約 '+Math.ceil((blockedUntil-now)/1000)+' 秒，等待免費額度恢復；串流照常。');scheduleRest(Math.max(1000,blockedUntil-now));return;}
    const code=codes[restCursor++%codes.length],mine=generation,controller=new AbortController();restController=controller;
    const timeout=setTimeout(()=>controller.abort(),8000);requestTimes.push(now);
    try{
      const response=await fetch(REST+encodeURIComponent(code),{headers:{'X-API-KEY':apiKey},credentials:'omit',cache:'no-store',redirect:'error',referrerPolicy:'no-referrer',signal:controller.signal});
      if(!running||mine!==generation)return;
      if(response.status===401||response.status===403){failed('富果定時查詢金鑰或權限無效，已停止並清除金鑰。');return;}
      if(response.status===429){
        const raw=response.headers.get('Retry-After'),seconds=Number(raw),until=Number.isFinite(seconds)?Date.now()+Math.max(60,seconds)*1000:Date.parse(raw||'');
        restPause=Number.isFinite(until)?Math.max(Date.now()+60000,until):Date.now()+60000;
        text($('#ql-rest-status'),'富果回報查詢額度已滿，暫停定時查詢至少 60 秒；不會重試轟炸，串流照常。');return;
      }
      if(!response.ok)throw new Error('quote unavailable');
      const quote=await response.json();if(!running||mine!==generation||!models.has(code)||symbols.includes(code))return;
      if(String(quote.symbol)!==code||quote.type==='ODDLOT')throw new Error('wrong symbol');
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
      if(running&&mine===generation)scheduleRest(Math.max(1500,15000/Math.max(1,extraSymbols().length)));
    }
  }
  async function launch(){
    if(following){if(!await refreshHoldings(true)||!running)return;}
    else{allSymbols=symbols.slice();buildRows();text($('#ql-holdings-status'),'手動測試模式：不自動讀取或替換 BOT 持股。');}
    connect();if(following)scheduleRest(0);
  }
  function accept(message){
    const d=message.data;if(!d||message.channel!=='trades'||!models.has(String(d.symbol)))return;
    if(d.type==='ODDLOT'||(message.event!=='rest'&&!symbols.includes(String(d.symbol))))return;
    const time=Number(d.time),price=Number(d.price),serial=d.serial==null||!Number.isFinite(Number(d.serial))?null:Number(d.serial);
    if(!Number.isSafeInteger(time)||time<1577836800000000||time>4102444800000000||!Number.isFinite(price)||price<=0)return;
    const model=models.get(String(d.symbol));
    if(model.time!==undefined&&time<model.time)return;
    if(d.isTrial===true){
      if(!model.trialTime||time>=model.trialTime){model.trialTime=time;model.trial='試撮參考 '+number(price)+' · '+timestamp(time)+'（非正式成交）';queueRender();}
      return;
    }
    if(message.event==='rest'&&model.time===time&&model.price===price){model.received=Date.now();model.kind='rest';model.closed=d.isClose===true;queueRender();return;}
    if(model.time!==undefined&&(time<model.time||(time===model.time&&((serial!==null&&model.serial!==null&&serial<=model.serial)||(price===model.price&&serial===model.serial)))))return;
    const previous=model.price;
    model.delta=message.event==='snapshot'||previous===undefined?null:price-previous;
    model.price=price;model.time=time;model.serial=serial;model.received=Date.now();model.kind=message.event;model.closed=d.isClose===true;
    model.trial='';model.trialTime=0;
    if(message.event==='data'){events++;model.flash=model.delta>0?'up':model.delta<0?'down':'';}
    if(message.event!=='rest')failures=0;
    if(message.event!=='rest')status('live','已連線',model.closed?'已收到收盤資料；請在交易時段驗證新成交的更新速度。':message.event==='snapshot'?'已收到最近快照，等待下一筆新成交。':'正在接收新成交，價格只在有新資料時更新。');
    queueRender();
  }
  function reconnect(){
    dropSocket();if(!running||disposed)return;
    if(failures>=6){failed('連線多次失敗，已停止重試並清除金鑰。請確認網路後重新輸入。');return;}
    const delay=Math.min(30000,1000*Math.pow(2,failures));failures++;retries++;
    text($('#ql-retries'),String(retries));status('retry','等待重連','連線中斷，保留最後行情；'+delay/1000+' 秒後重新連線。');
    const mine=generation;retryTimer=setTimeout(()=>{retryTimer=null;if(running&&mine===generation)connect();},delay);
  }
  function connect(){
    if(!running||disposed||document.hidden)return;
    if(ws)return;
    status('connecting','連接中','正在建立富果連線…');
    let socket;try{socket=new WebSocket(URL);}catch(ignore){reconnect();return;}
    ws=socket;const mine=generation;
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
        authenticated=true;reconcile();
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
        failed((explanations[code]||'富果回報無法繼續訂閱。')+' 已停止並清除金鑰。');
      }
    };
    socket.onerror=()=>{if(current())reconnect();};
    socket.onclose=event=>{
      if(!current())return;
      if(event.code===1013||event.code===1008||event.code===1005||(event.code===1001&&String(event.reason).includes('plan'))){failed('連線遭拒或已達方案限制，請檢查金鑰、權限及其他連線。已清除金鑰。');return;}
      reconnect();
    };
  }
  async function begin(){
    if(running||disposed)return;
    if(document.hidden){status('idle','請回到前景','請在可見的測試頁開始連線。');return;}
    const selected=symbolsInput.value.toUpperCase().split(/[\s,，、]+/).filter(Boolean);
    if((selected.length===0&&!followInput.checked)||selected.length>5||new Set(selected).size!==selected.length||selected.some(s=>!/^\d{4,6}[A-Z]?$/.test(s))){status('error','請確認代碼','請輸入 1～5 個不重複的上市櫃股票代碼，以逗號或空格分隔。');return;}
    const supplied=keyInput.value.trim();if(!supplied){status('error','尚未輸入金鑰','請輸入你申請的富果行情 API Key。');return;}
    apiKey=supplied;keyInput.value='';preferred=selected;following=followInput.checked;symbols=following?[]:selected;allSymbols=[];events=retries=failures=0;restPause=0;running=true;generation++;controls();
    rows.clear();models.clear();flashes.forEach(clearTimer);flashes.clear();$('#ql-quotes').replaceChildren();text($('#ql-events'),'0');text($('#ql-retries'),'0');status('connecting','準備中',following?'正在確認你的 BOT 持股…':'正在準備串流…');
    const mine=generation;
    if(navigator.locks&&navigator.locks.request){
      try{
        await navigator.locks.request('stockbot-private-quote-lab',{ifAvailable:true},async lock=>{
          if(!running||mine!==generation)return;
          if(!lock){failed('這個瀏覽器已有另一個測試分頁連線，請先在原分頁停止。');return;}
          await new Promise(resolve=>{releaseLock=resolve;launch();});
        });
      }catch(ignore){if(running&&mine===generation)failed('無法取得測試頁連線鎖，已清除金鑰。請關閉其他分頁後重試。');}
    }else{launch();}
  }
  function leave(){stop('頁面已離開或切到背景，連線已關閉、金鑰已清除；回到頁面後請重新輸入。');}
  function dispose(){if(disposed)return;leave();disposed=true;if(raf!==null)cancelAnimationFrame(raf);raf=null;flashes.forEach(clearTimer);flashes.clear();observer.disconnect();document.removeEventListener('visibilitychange',visibility);document.removeEventListener('stockbot:pageleaving',dispose);window.removeEventListener('pagehide',pagehide);}
  function visibility(){if(document.hidden)leave();}
  function pagehide(){leave();}
  start.addEventListener('click',begin);stopButton.addEventListener('click',()=>stop('連線已停止，金鑰已清除。最後行情保留供查看。'));
  keyInput.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();begin();}});
  document.addEventListener('visibilitychange',visibility);document.addEventListener('stockbot:pageleaving',dispose);window.addEventListener('pagehide',pagehide);
  const observer=new MutationObserver(()=>{if(!root.isConnected)dispose();});observer.observe(document.documentElement,{childList:true,subtree:true});
  controls();
})();
