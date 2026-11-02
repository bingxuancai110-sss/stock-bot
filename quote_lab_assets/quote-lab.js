(function(){
  'use strict';
  const root=document.getElementById('quote-lab');
  if(!root||root.dataset.ready==='1')return;
  root.dataset.ready='1';
  const $=s=>root.querySelector(s),keyInput=$('#ql-key'),symbolsInput=$('#ql-symbols'),start=$('#ql-start'),stopButton=$('#ql-stop');
  const URL='wss://api.fugle.tw/marketdata/v1.0/stock/streaming';
  const names={'2308':'台達電','8996':'高力','2383':'台光電','2368':'金像電','6442':'光聖'};
  const rows=new Map(),models=new Map(),flashes=new Map();
  const clock=new Intl.DateTimeFormat('zh-TW',{timeZone:'Asia/Taipei',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
  let apiKey='',symbols=[],ws=null,running=false,disposed=false,generation=0,retryTimer=null,phaseTimer=null,healthTimer=null,raf=null,releaseLock=null;
  let events=0,retries=0,failures=0,lastMessage=0;
  function text(el,value){if(el&&el.textContent!==value)el.textContent=value;}
  function status(state,label,message){$('#ql-state').dataset.state=state;text($('#ql-state'),label);text($('#ql-message'),message);}
  function controls(){start.disabled=running;stopButton.disabled=!running;keyInput.disabled=running;symbolsInput.disabled=running;}
  function clearTimer(id){if(id!==null)clearTimeout(id);}
  function dropSocket(){
    clearTimer(phaseTimer);phaseTimer=null;
    if(healthTimer!==null)clearInterval(healthTimer);healthTimer=null;
    const old=ws;ws=null;
    if(old){old.onopen=old.onmessage=old.onclose=old.onerror=null;try{old.close(1000,'Client stopped');}catch(ignore){}}
  }
  function stop(message,state='idle',label='已停止'){
    generation++;running=false;apiKey='';keyInput.value='';clearTimer(retryTimer);retryTimer=null;dropSocket();
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
        text(row.querySelector('[data-gap]'),m.kind==='snapshot'?'最近快照，不以快照年齡判斷傳輸速度':gap<0?'裝置時鐘可能不同步，暫不計算':(gap/1000).toFixed(3)+' 秒（觀測值，非延遲保證）');
        text(row.querySelector('.ql-change'),m.delta==null?'第一筆，等待下一筆比較':(m.delta>0?'+':'')+number(m.delta)+' 元 · 比上一筆成交');
        row.dataset.tone=m.delta>0?'up':m.delta<0?'down':'flat';
        text(row.querySelector('.ql-quote-type'),m.closed?'收盤快照／收盤成交':m.kind==='snapshot'?'最近快照':'新成交 · 富果串流');
        if(m.flash){
          row.dataset.flash=m.flash;clearTimer(flashes.get(symbol));
          flashes.set(symbol,setTimeout(()=>{delete row.dataset.flash;flashes.delete(symbol);},300));m.flash='';
        }
      }
      const trial=row.querySelector('.ql-trial');trial.hidden=!m.trial;
      if(m.trial)text(trial,m.trial);
    });
    text($('#ql-events'),String(events));text($('#ql-retries'),String(retries));
  }
  function queueRender(){if(raf===null)raf=requestAnimationFrame(render);}
  function buildRows(){
    rows.clear();models.clear();flashes.forEach(clearTimer);flashes.clear();$('#ql-quotes').replaceChildren();
    symbols.forEach(symbol=>{
      const row=document.createElement('article');row.className='ql-quote';row.dataset.symbol=symbol;
      row.innerHTML='<div class="ql-quote-head"><div><div class="ql-symbol"></div><div class="ql-quote-type">等待資料</div></div><div><div class="ql-price">—</div><div class="ql-change">尚無成交資料</div></div></div><div class="ql-times"><div><small>行情時間 · 台北</small><b data-time>—</b></div><div><small>接收時間 · 台北</small><b data-received>—</b></div><div class="ql-gap"><small>觀測時間差</small><b data-gap>—</b></div></div><p class="ql-trial" hidden></p>';
      text(row.querySelector('.ql-symbol'),(names[symbol]?names[symbol]+' ':'')+symbol);
      rows.set(symbol,row);models.set(symbol,{});$('#ql-quotes').appendChild(row);
    });
    text($('#ql-count'),symbols.length+' / 5');
  }
  function accept(message){
    const d=message.data;if(!d||message.channel!=='trades'||!models.has(String(d.symbol)))return;
    if(d.type==='ODDLOT')return;
    const time=Number(d.time),price=Number(d.price),serial=d.serial==null||!Number.isFinite(Number(d.serial))?null:Number(d.serial);
    if(!Number.isSafeInteger(time)||time<1577836800000000||time>4102444800000000||!Number.isFinite(price)||price<=0)return;
    const model=models.get(String(d.symbol));
    if(model.time!==undefined&&time<model.time)return;
    if(d.isTrial===true){
      if(!model.trialTime||time>=model.trialTime){model.trialTime=time;model.trial='試撮參考 '+number(price)+' · '+timestamp(time)+'（非正式成交）';queueRender();}
      return;
    }
    if(model.time!==undefined&&(time<model.time||(time===model.time&&((serial!==null&&model.serial!==null&&serial<=model.serial)||(price===model.price&&serial===model.serial)))))return;
    const previous=model.price;
    model.delta=message.event==='snapshot'||previous===undefined?null:price-previous;
    model.price=price;model.time=time;model.serial=serial;model.received=Date.now();model.kind=message.event;model.closed=d.isClose===true;
    model.trial='';model.trialTime=0;
    if(message.event==='data'){events++;model.flash=model.delta>0?'up':model.delta<0?'down':'';}
    failures=0;
    status('live','已連線',model.closed?'已收到收盤資料；請在交易時段驗證新成交的更新速度。':message.event==='snapshot'?'已收到最近快照，等待下一筆新成交。':'正在接收新成交，價格只在有新資料時更新。');
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
        try{socket.send(JSON.stringify({event:'subscribe',data:{channel:'trades',symbols:symbols.slice()}}));}catch(ignore){reconnect();return;}
        status('live','已連線','已驗證，等待 5 檔以內的成交資料。收盤後可能只有最近快照。');
        phaseTimer=setTimeout(()=>{if(current()&&events===0)text($('#ql-message'),'連線正常，尚無新成交。收盤後、尚未成交或代碼無效時可能沒有資料，請確認交易時段與代碼。');},15000);
        if(healthTimer!==null)clearInterval(healthTimer);
        healthTimer=setInterval(()=>{if(current()&&Date.now()-lastMessage>65000)reconnect();},10000);
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
    if(selected.length===0||selected.length>5||new Set(selected).size!==selected.length||selected.some(s=>!/^\d{4,6}[A-Z]?$/.test(s))){status('error','請確認代碼','請輸入 1～5 個不重複的上市櫃股票代碼，以逗號或空格分隔。');return;}
    const supplied=keyInput.value.trim();if(!supplied){status('error','尚未輸入金鑰','請輸入你申請的富果行情 API Key。');return;}
    apiKey=supplied;keyInput.value='';symbols=selected;events=retries=failures=0;running=true;generation++;controls();buildRows();queueRender();
    const mine=generation;
    if(navigator.locks&&navigator.locks.request){
      try{
        await navigator.locks.request('stockbot-private-quote-lab',{ifAvailable:true},async lock=>{
          if(!running||mine!==generation)return;
          if(!lock){failed('這個瀏覽器已有另一個測試分頁連線，請先在原分頁停止。');return;}
          await new Promise(resolve=>{releaseLock=resolve;connect();});
        });
      }catch(ignore){if(running&&mine===generation)failed('無法取得測試頁連線鎖，已清除金鑰。請關閉其他分頁後重試。');}
    }else{connect();}
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
