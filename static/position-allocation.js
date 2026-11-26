(function(){
  'use strict';
  if(window.StockBotAllocation)return;
  function ensureAllocation(items,names){
    let box=document.querySelector('.position-allocation');
    if(!box){box=document.createElement('section');box.className='position-allocation';box.innerHTML='<div class="position-allocation-head"><h2>持股配置</h2><button type="button" data-allocation-reset>顯示全部</button></div><div class="position-allocation-body"><div class="position-allocation-visual"><svg viewBox="0 0 100 100" role="img" aria-label="富果行情持股市值權重"><circle cx="50" cy="50" r="31" fill="#fff"/></svg><div class="position-allocation-center"><b data-allocation-center-title>全部持股</b><strong data-allocation-center-weight data-home-sensitive>100%</strong><small data-allocation-center-value data-home-sensitive></small></div></div><div class="position-allocation-legend"></div></div><div class="allocation-selection" aria-live="polite"><span>組合概覽</span><strong>點選圓環或股票名稱</strong><small></small></div>';const card=document.querySelector('[data-position-code]');card.parentNode.insertBefore(box,card);}
    const held=new Set(items.map(x=>x.code));box.querySelectorAll('[data-allocation-select],[data-allocation-code]').forEach(n=>{if(!held.has(n.dataset.allocationSelect||n.dataset.allocationCode))n.remove();});
    const colors=['#2676A5','#D84B45','#14916C','#8165B5','#D29430','#3A8FA3','#CF6A36','#6E8498'];
    items.forEach((item,index)=>{
      if(box.querySelector('[data-allocation-select="'+item.code+'"]'))return;
      const name=item.code==='cash'?'現金':names[item.code]||item.code;
      const color=item.code==='cash'?'#5E7188':colors[index%colors.length];
      const button=document.createElement('button'),dot=document.createElement('i'),label=document.createElement('span'),value=document.createElement('b');button.type='button';button.className='allocation-item';button.dataset.allocationSelect=item.code;button.dataset.allocationName=name;button.setAttribute('aria-pressed','false');value.setAttribute('data-home-sensitive','');dot.style.background=color;label.textContent=name;button.append(dot,label,value);box.querySelector('.position-allocation-legend').append(button);
      const circle=document.createElementNS('http://www.w3.org/2000/svg','circle');circle.classList.add('allocation-slice');circle.dataset.allocationCode=item.code;Object.entries({cx:50,cy:50,r:40,fill:'none',stroke:color,'stroke-width':14,transform:'rotate(-90 50 50)',tabindex:0,role:'button','aria-label':name}).forEach(([k,v])=>circle.setAttribute(k,v));const svg=box.querySelector('svg');svg.insertBefore(circle,svg.lastElementChild);
    });return box;
  }
  function money(value, digits){
    return Number(value).toLocaleString('zh-TW',{minimumFractionDigits:digits,maximumFractionDigits:digits});
  }
  function assetValue(value){
    return Number(value).toLocaleString('zh-TW',{maximumFractionDigits:2})+' 元';
  }
  function setPrivate(node,text){
    if(!node)return;
    node.dataset.homeOriginal=text;
    node.textContent=window.__stockbotHomePrivacyMasked===true?'******':text;
  }
  function redraw(stocks,names={}){
    const existing=document.querySelector('.position-allocation');
    if(existing?.dataset.allocationCash==='')return;
    const items=stocks.filter(item=>Number(item.value)>0);
    const cash=Number(existing?.dataset.allocationCash||0);
    if(cash>0)items.push({code:'cash',value:cash});
    if(!existing&&!items.length)return;
    items.sort((a,b)=>b.value-a.value);
    const total=items.reduce((value,item)=>value+Number(item.value),0);
    const box=ensureAllocation(items,names),svg=box.querySelector('svg');
    const legend=box.querySelector('.position-allocation-legend'),circumference=2*Math.PI*40;
    let offset=0;
    for(const item of items){
      const selector=CSS.escape(item.code);
      const button=legend.querySelector('[data-allocation-select="'+selector+'"]');
      const slice=svg.querySelector('[data-allocation-code="'+selector+'"]');
      const weight=Number(item.value)/total*100,arc=circumference*weight/100;
      button.dataset.allocationWeight=weight;
      button.dataset.allocationValue=item.value;
      setPrivate(button.querySelector('b'),money(weight,1)+'%');
      legend.append(button);
      slice.setAttribute('aria-label',button.dataset.allocationName+'，權重 '+money(weight,1)+'%');
      slice.setAttribute('stroke-dasharray',arc+' '+circumference);
      slice.setAttribute('stroke-dashoffset',-offset);
      svg.insertBefore(slice,svg.lastElementChild);
      offset+=arc;
    }
    const selected=legend.querySelector('[aria-pressed=true]');
    const title=box.querySelector('[data-allocation-center-title]');
    if(title)title.textContent=selected?.dataset.allocationName||'總資產';
    setPrivate(box.querySelector('[data-allocation-center-weight]'),selected?money(selected.dataset.allocationWeight,1)+'%':'100.0%');
    setPrivate(box.querySelector('[data-allocation-center-value]'),assetValue(selected?selected.dataset.allocationValue:total));
    box.querySelectorAll('.position-allocation-chart,[data-allocation-reset]').forEach(el=>{el.hidden=!total;});
    const pending=box.querySelector('.position-allocation-pending');
    if(pending)pending.hidden=true;
    box.dataset.allocationReady='true';
  }
  window.StockBotAllocation={redraw};

  document.addEventListener('submit',async event=>{
    const form=event.target.closest('form[data-position-cash-form]');
    if(!form)return;
    event.preventDefault();
    if(form.dataset.saving==='true')return;

    const input=form.elements.portfolio_cash;
    const button=form.querySelector('button[type="submit"]');
    const status=form.querySelector('[data-position-cash-status]');
    // The hidden "action" input shadows form.action on HTML forms.
    const url=form.getAttribute('action');
    const body=new FormData(form);
    body.set('response_format','json');
    form.dataset.saving='true';
    button.disabled=true;
    input.readOnly=true;
    status.textContent='儲存中…';
    const controller=new AbortController();
    const timeout=setTimeout(()=>controller.abort(),15000);
    try{
      const response=await fetch(url,{
        method:'POST',credentials:'same-origin',headers:{Accept:'application/json'},body,signal:controller.signal,
      });
      if(response.status===401){
        status.textContent='登入已過期，請重新開啟持股頁。';
        return;
      }
      const data=await response.json();
      if(!response.ok||!data.ok){
        status.textContent=data.message||'暫時無法確認儲存，金額已保留，可以再試一次。';
        return;
      }
      document.dispatchEvent(new CustomEvent('stockbot:positionschanged'));
      if(!form.isConnected)return;
      const box=form.closest('.position-allocation');
      input.value=data.cash;
      box.dataset.allocationCash=data.cash;
      setPrivate(box.querySelector('[data-position-cash-balance]'),assetValue(data.cash));
      if(box.dataset.allocationReady==='true'){
        const stocks=Array.from(box.querySelectorAll('.allocation-item')).filter(item=>item.dataset.allocationSelect!=='cash')
          .map(item=>({code:item.dataset.allocationSelect,value:Number(item.dataset.allocationValue)}));
        redraw(stocks);
        box.querySelector('[data-allocation-reset]')?.click();
      }
      status.textContent='已儲存';
    }catch(error){
      status.textContent='暫時無法確認儲存，金額已保留，可以再試一次。';
    }finally{
      clearTimeout(timeout);
      delete form.dataset.saving;
      button.disabled=false;
      input.readOnly=false;
    }
  });
})();
