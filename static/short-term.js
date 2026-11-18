(function(){
 'use strict';
 var current=null, sequence=0;
 var names={entry:'進場',add:'加碼',reduce:'減碼',exit:'出場'};
 var colors={entry:'#c5343b',add:'#a75916',reduce:'#94651b',exit:'#13805f'};
 function esc(v){return String(v==null?'—':v).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
 function num(v){return v==null||!Number.isFinite(Number(v))?'—':Number(v).toLocaleString('zh-TW',{maximumFractionDigits:2});}
 function price(v){return v==null||!Number.isFinite(Number(v))?'—':Number(v).toLocaleString('zh-TW',{minimumFractionDigits:2,maximumFractionDigits:2});}
 function spreadText(v){if(v==null||!Number.isFinite(Number(v)))return '價差未確認';return Number(v)>0?'每股賺 NT$'+price(v):Number(v)<0?'每股賠 NT$'+price(-v):'每股價差 NT$0.00';}
 function spreadClass(v){return Number(v)>0?'st-up':Number(v)<0?'st-down':'';}
 function spreadView(r){var p=r.spreads||{},trades=p.completed||[],last=trades[trades.length-1],out='';if(last){out+='<div class="st-profit"><span>'+(r.provisional&&last.exit_date===r.date?'含盤中暫定出場的每股價差':'已出場每股價差')+(trades.length>1?'（'+trades.length+' 次合計）':'')+'</span><strong class="'+spreadClass(p.total_closed_spread)+'">'+spreadText(p.total_closed_spread)+'</strong><small>上次 '+esc(last.entry_date)+' '+price(last.entry_price)+' → '+esc(last.exit_date)+' '+price(last.exit_price)+(trades.length>1?' · '+spreadText(last.spread):'')+'</small></div>';}if(p.open&&p.open.spread!=null){var o=p.open;out+='<div class="st-profit"><span>尚未出場 · 未實現每股價差</span><strong class="'+spreadClass(o.spread)+'">'+spreadText(o.spread)+'</strong><small>'+esc(o.entry_date)+' '+price(o.entry_price)+' → '+esc(o.date)+' '+price(o.price)+'</small></div>';}return out;}
 function api(refresh){var t=new URLSearchParams(location.search).get('t')||'';return '/web/api/workbench/short-term'+(refresh?'?refresh=1':'?read=1')+(t?'&t='+encodeURIComponent(t):'')+(current&&current.sector?'&sector='+encodeURIComponent(current.sector):'');}
 function alive(s){return current===s&&s.el.isConnected;}
 function notices(s){
  var meta=s.data.meta||{},date=meta.date||'未確認',valid=meta.signals_current===true;
  var alerts=valid?(s.data.alerts||[]):[],entries=alerts.filter(function(e){return e.type==='entry';});
  var title=!valid?'最新訊號尚未確認':entries.length?date+' 新進場 '+entries.length+' 檔':date+' 沒有新的進場訊號';
  if(meta.intraday&&valid)title=entries.length?date+' 盤中暫定進場 '+entries.length+' 檔':date+' 尚無新的盤中進場訊號';
  var body=!valid?'<p>'+esc(meta.building?'正在更新；舊紀錄不作為新的進場提示。':'資料截至 '+date+'，尚未確認 '+(meta.expected_date||'最新交易日')+' 的訊號。')+'</p>':'<p>'+(meta.intraday?'盤中條件會隨價格改變，尚未收棒確認；歷史箭頭不是現在買點。':'僅列本次收盤確認的新訊號；歷史箭頭不是現在的買進提示。')+'</p>';
  if(valid&&alerts.length){var renderAlert=function(e){return '<div class="st-notice-event" data-kind="'+esc(e.type)+'"><b>'+esc(e.name)+' '+esc(e.code)+'｜'+esc((e.provisional?'盤中暫定 · ':'')+(e.type==='entry'?'新進場':names[e.type]))+'</b><span>'+esc(e.date)+' · 訊號價 '+price(e.price)+'</span><span>支撐停損 '+price(e.stop)+'　停利參考 '+price(e.target)+'</span>'+(e.type==='exit'&&e.spread!=null?'<strong class="'+spreadClass(e.spread)+'">'+spreadText(e.spread)+'</strong>':'')+'</div>';};body+=alerts.slice(0,6).map(renderAlert).join('');if(alerts.length>6)body+='<details><summary>其餘 '+(alerts.length-6)+' 筆新訊號</summary>'+alerts.slice(6).map(renderAlert).join('')+'</details>';body+='<p>'+(meta.intraday?'盤中暫定訊號可能消失，不代表已收盤確認或可按該價格成交。':'下一交易日須重新確認價格與停損距離，不代表可按訊號價直接成交。')+'</p>';}
  if(valid&&meta.failed)body+='<p>另有 '+num(meta.failed)+' 檔資料待確認。</p>';
  if(meta.scope&&meta.scope.light_missing)body+='<p>全市場初篩另有 '+num(meta.scope.light_missing)+' 檔行情缺漏，不補造訊號。</p>';
  return '<section class="st-notices" aria-label="最新交易訊號" role="status"><h3>'+esc(title)+'</h3>'+body+'</section>';
 }
 function header(s){
  var meta=s.data.meta||{};
  s.el.innerHTML='<header class="st-heading"><div><span class="st-eyebrow">TECHNICAL FIRST · ALL EQUITIES</span><h2>短線交易計畫 <span class="st-beta">技術 v2</span></h2><p>技術面為主，法人僅供參考。</p></div><button type="button" class="st-refresh">重新篩選</button></header>'+
   notices(s)+'<div class="st-summary"><span>'+(meta.intraday?'盤中日 K（未完成）':'收盤日 K')+'</span><span>最多 6 檔</span><span>不設法人買超門檻</span></div>'+
   '<div class="st-status" role="status">'+esc(meta.building?(meta.phase||'背景篩選中')+' '+(meta.progress||0)+'／'+(meta.progress_total||meta.pool_limit||80):meta.error?meta.error:meta.date?'行情截至 '+meta.date+' · '+(meta.scope&&meta.scope.universe_count?'全市場初篩 '+meta.scope.universe_count+' 檔 · 技術詳查 ':'技術詳查 ')+meta.scanned+' 檔':'尚無符合條件的候選')+(meta.stale&&meta.date?' · 舊結果':'')+'</div>'+
   '<div class="st-refresh-info">'+esc((meta.intraday?'與雷達排程同步，約每 10 分鐘更新':'收盤確認模式')+' · 上次完成 '+(meta.generated_at?meta.generated_at.slice(0,19).replace('T',' '):'尚未完成'))+'<br>'+esc(meta.intraday?'下一輪約 '+(meta.next_refresh_at?meta.next_refresh_at.slice(11,19):'等待雷達排程')+'；行情可能延遲，量比依開盤經過時間估算。':'盤中會切換為暫定訊號；收盤結果分開保存。')+'</div>'+
   '<div class="st-sector" aria-label="短線產業篩選">'+['全部','電子','非電子'].map(function(v){return '<button type="button" data-sector="'+v+'" aria-pressed="'+((s.sector||'全部')===v)+'">'+v+'</button>';}).join('')+'</div><div class="st-cards"></div><details class="st-rules"><summary>篩選與訊號規則</summary><div><p>上市、上櫃股票先做全市場技術初篩，再對最多 80 檔候選做日 K 詳查，最多顯示 6 檔。法人只供參考，不決定入選，也不擋住技術進場。等待中的觀察股不是買進訊號。</p><p>成交量、成交金額不設最低門檻；量比只確認相對量價型態。為降低追高，仍排除當日漲跌超過 5%、五日漲超過 15%、月線乖離超過 8%及 ATR 波動過高的進場候選。無法保證排除所有炒作股；尚未串接注意／處置股名單。</p><p>突破放量或月線回測轉強才進場；加碼需獲利達 1R、再次突破、技術成立且合併風險不超初始預算。壓力受阻、連續兩日跌破十日線，或跌破十日線搭配法人轉賣時減碼一次；支撐停損價只升不降，次日生效。</p><p>盤中日 K 為暫定條件，未收棒前可能消失；盤中量比依開盤時間比例估算，並非實際全日量。收盤後重新確認，法人只使用該棒前交易日已知資料。歷史箭頭為規則重播，非實際交易、非回測績效；價格不保證成交，未計費用與滑價。價差只算首筆進場到出場的每股價格差，不計股數、加減碼均價與費稅。這個分頁不下單、不修改持股。</p></div></details>';
  if(!s.el.hidden){var wbStatus=document.getElementById('wb-status');if(wbStatus)wbStatus.textContent=meta.building?'短線背景篩選中':meta.error?'短線資料待確認':'短線研究已載入';}
  var scope=meta.scope||{};if(scope.universe_count){var markets=scope.markets||{},sectors=scope.sectors||{};s.el.querySelector('.st-status').insertAdjacentHTML('afterend','<div class="st-scope">'+Object.keys(markets).map(function(k){return esc(k)+' '+num(markets[k])+' 檔';}).join(' · ')+'<br>'+Object.keys(sectors).map(function(k){return esc(k)+' '+num(sectors[k])+' 檔';}).join(' · ')+'</div>');}
  s.el.querySelectorAll('[data-sector]').forEach(function(b){b.onclick=function(){if(s.loading)return;s.sector=b.dataset.sector==='全部'?'':b.dataset.sector;s.open=null;load(s,false);};});
  var counts=(scope.qualified_sectors||{}),deep=(scope.deep_sectors||{});if(scope.universe_count)s.el.querySelector('.st-scope').insertAdjacentHTML('beforeend','<br>技術詳查：'+esc(Object.keys(deep).map(function(k){return k+' '+deep[k]+' 檔';}).join(' · '))+'<br>符合觀察條件：'+esc(Object.keys(counts).map(function(k){return k+' '+counts[k]+' 檔';}).join(' · ')||'尚未確認'));
  s.el.querySelector('.st-refresh').onclick=function(){load(s,true);};
 }
 function render(s){
  if(!alive(s))return;
  header(s);var rows=s.data.rows||[],host=s.el.querySelector('.st-cards');
  if(!rows.length){var excluded=(s.data.meta||{}).excluded||{},reasons=Object.keys(excluded).map(function(k){return '<li><span>'+esc(k)+'</span><b>'+num(excluded[k])+' 檔</b></li>';}).join('');host.innerHTML=(s.data.meta||{}).building?'<div class="st-skeleton" aria-label="正在篩選候選"></div><div class="st-skeleton"></div>':'<div class="st-empty"><b>目前沒有符合條件的短線候選</b><p>此分類目前沒有符合觀察條件的候選，可切換其他分類；不為湊名單產生買進訊號。</p>'+(reasons?'<ul class="st-excluded" aria-label="主要排除原因">'+reasons+'</ul>':'')+'</div>';return;}
  host.innerHTML=rows.map(function(r,i){var chip=r.chip||{},event=r.latest_event,up=r.change_pct>=0;return '<article class="st-card" data-code="'+esc(r.code)+'"><button type="button" class="st-card-toggle" aria-expanded="'+(s.open===r.code)+'" aria-controls="st-detail-'+esc(r.code)+'"><div class="st-stock"><span class="st-state" data-kind="'+esc(event||'wait')+'">'+esc((r.latest_event_date?r.latest_event_date.slice(5)+' ':'')+r.state)+'</span><h3>'+esc(r.name)+' <small>'+esc(r.code)+'</small></h3><span class="st-date">'+esc(r.date)+(r.provisional?' 盤中未收棒':' 收盤')+(r.observed_at?' · '+esc(r.observed_at.slice(11,19)):'')+(r.sector?' · '+esc(r.market||'')+' '+esc(r.sector):'')+'</span></div><div class="st-quote"><strong>'+price(r.price)+'</strong><span class="'+(up?'st-up':'st-down')+'">'+(up?'+':'')+Number(r.change_pct).toFixed(2)+'%</span><span class="st-open-label">'+(s.open===r.code?'收合 K 線 −':'K 線計畫 ＋')+'</span></div></button>'+
   '<div class="st-action"><b>'+esc(r.state)+'</b><p>'+esc(r.action_note||'尚未確認新的進場訊號。')+'</p></div>'+
   spreadView(r)+'<div class="st-levels"><div><span>'+(r.active?'原策略進場價':'突破觀察價')+'</span><b>'+price(r.entry)+'</b></div><div><span>'+(r.active?'移動支撐停損':'觀察停損參考')+'</span><b class="st-down">'+price(r.stop)+'</b></div><div><span>'+esc(r.target_kind||'停利參考')+'</span><b>'+price(r.target)+'</b></div></div>'+
   '<div class="st-checks">'+(r.checks||[]).map(function(c){return '<span data-passed="'+!!c.passed+'">'+(c.passed?'✓ ':'— ')+esc(c.label)+(c.passed?'成立':'待確認')+'</span>';}).join('')+'</div>'+
   '<div class="st-evidence"><span>'+esc(r.trend?'趨勢向上':'趨勢待確認')+'</span><span>'+(r.volume_estimated?'時間比例估算量比 ':'量比 ')+num(r.volume_ratio)+'</span><span>'+esc('法人參考：'+(r.chip_support||'資料待確認'))+'</span></div>'+
   '<section class="st-detail" id="st-detail-'+esc(r.code)+'" '+(s.open===r.code?'':'hidden')+'></section></article>';}).join('');
  host.querySelectorAll('.st-card-toggle').forEach(function(btn){btn.onclick=function(){var code=btn.closest('.st-card').dataset.code;s.open=s.open===code?null:code;s.history=false;s.event=null;render(s);};});
  if(s.open){var row=rows.find(function(r){return r.code===s.open;});if(row)detail(s,row);}
 }
 function timeline(r){
  var events=(r.signals||[]).slice().reverse();
  return '<section class="st-history" aria-label="訊號時間軸"><h4>訊號時間軸（'+events.length+'）</h4><p class="st-history-note">依日期由新到舊；歷史規則重播，不是當下的新買點。</p><ol>'+events.map(function(e){return '<li><button type="button" data-event="'+esc(e.id)+'"><span>'+esc(e.date)+'</span><b style="color:'+colors[e.type]+'">'+esc(names[e.type])+'</b><strong>'+price(e.price)+'</strong></button><span class="st-event-status">'+(e.provisional?'盤中暫定 · 未收棒':'歷史收盤規則重播')+'</span><p class="st-event-reason"><b>理由：</b>'+esc(e.reason||'此筆舊紀錄沒有保存理由，不補造原因。')+'</p><small>當時支撐停損 '+price(e.stop)+' · 停利參考 '+price(e.target)+'</small></li>';}).join('')+'</ol>'+(events.length?'':'<p>這段資料尚無進出場紀錄，不補畫訊號。</p>')+'</section>';
 }
 function detail(s,r){
  var el=s.el.querySelector('#st-detail-'+r.code),events=r.signals||[],chip=r.chip||{};
  el.innerHTML='<div class="st-chart-head"><div><b>K 線與策略訊號</b><span>'+(r.provisional?'盤中日線 · 暫定':'日線 · 收盤確認')+'</span></div><div class="st-periods" aria-label="圖表期間">'+[30,60,120].map(function(n){return '<button type="button" data-range="'+n+'" aria-pressed="'+(s.range===n)+'">'+n+' 日</button>';}).join('')+'</div></div>'+
   '<div class="st-chart-mode"><span class="st-chart-context"></span><button type="button" class="st-history-toggle" aria-pressed="false">查看歷史訊號</button></div>'+
   '<div class="st-chart-readout" aria-live="polite">左右滑動查看日期與價格</div><div class="st-chart" role="group" aria-label="'+esc(r.name)+'日 K 與進出場訊號"></div>'+
   '<div class="st-chart-nav"><button type="button" data-step="-1" aria-label="查看較早K線">← 較早</button><span>▲ 進場／加碼　▼ 減碼／出場</span><button type="button" data-step="1" aria-label="查看較新K線">較新 →</button></div>'+
   '<div class="st-inspector" aria-live="polite"></div>'+timeline(r)+'<div class="st-chip-detail"><h4>技術依據與法人參考</h4><dl><div><dt>近 20 日平均成交額（非門檻）</dt><dd>'+num(r.avg_turnover/10000)+' 萬元</dd></div><div><dt>近 20 日平均成交量（非門檻）</dt><dd>'+num(r.avg_volume/1000)+' 張</dd></div><div><dt>支撐參考</dt><dd>'+price(r.support)+'</dd></div><div><dt>上方歷史壓力</dt><dd>'+(r.resistance?price(r.resistance):'區間內無已知壓力')+'</dd></div><div><dt>外資近五日</dt><dd>'+num(chip.foreign)+' 張／'+num(chip.foreign_days)+' 日買超</dd></div><div><dt>投信近五日</dt><dd>'+num(chip.trust)+' 張／'+num(chip.trust_days)+' 日買超</dd></div><div><dt>法人淨買占五日成交量（參考）</dt><dd>'+num(r.chip_ratio)+'%</dd></div><div><dt>籌碼資料截至</dt><dd>'+esc(chip.date||'缺資料')+'</dd></div></dl></div>'+
   '<p class="st-source">行情：'+esc(r.source)+(r.observed_at?' · 來源時間 '+esc(r.observed_at.slice(0,19).replace('T',' ')):'')+' · 停損線為觸發價，非保證成交價。</p>';
  el.querySelector('.st-history-toggle').onclick=function(){s.history=!s.history;s.event=null;s.end=null;inspect(s,r,el);draw(s,r,el);};
  el.querySelectorAll('[data-range]').forEach(function(b){b.onclick=function(){s.range=Number(b.dataset.range);s.end=null;detail(s,r);};});
  el.querySelectorAll('[data-step]').forEach(function(b){b.onclick=function(){s.end=Math.max(Math.min(s.range,r.bars.length),Math.min(r.bars.length,(s.end||r.bars.length)+Number(b.dataset.step)*Math.max(5,Math.floor(s.range/2))));draw(s,r,el);};});
  el.querySelectorAll('[data-event]').forEach(function(b){b.onclick=function(){select(s,r,b.dataset.event,el);};});
  inspect(s,r,el);draw(s,r,el);
 }
 function inspect(s,r,el){
  var e=(r.signals||[]).find(function(x){return x.id===s.event;}),box=el.querySelector('.st-inspector');
  box.innerHTML=e?'<span class="st-inspector-label">'+esc(e.date)+' · 歷史規則重播</span><h4 style="color:'+colors[e.type]+'">'+names[e.type]+'訊號 '+price(e.price)+'</h4><p>'+esc(e.reason)+'</p><div><span>當時支撐停損價 <b>'+price(e.stop)+'</b></span><span>當時停利參考 <b>'+price(e.target)+'</b></span></div><button type="button" class="st-current">回到目前計畫</button>':'<span class="st-inspector-label">目前狀態</span><h4>'+esc(r.state)+'</h4><p>'+esc(r.action_note||'尚未確認新的進場訊號。')+'</p><div><span>支撐停損價 <b>'+price(r.stop)+'</b></span><span>停利參考 <b>'+price(r.target)+'</b></span></div>';
  if(e){var trades=(r.spreads||{}).completed||[],t=trades.find(function(t){return e.date>=t.entry_date&&e.date<=t.exit_date;});if(t)box.insertAdjacentHTML('beforeend','<div class="st-profit"><span>此段已於 '+esc(t.exit_date)+' 出場 · 歷史每股價差</span><strong class="'+spreadClass(t.spread)+'">'+spreadText(t.spread)+'</strong><small>'+price(t.entry_price)+' → '+price(t.exit_price)+'</small></div>');}
  var b=box.querySelector('.st-current');if(b)b.onclick=function(){s.history=false;s.event=null;s.end=null;inspect(s,r,el);draw(s,r,el);};
 }
 function select(s,r,id,el){s.history=true;s.event=id;var event=r.signals.find(function(e){return e.id===id;});var index=r.bars.findIndex(function(b){return b.date===event.date;});if(index>=0)s.end=Math.min(r.bars.length,Math.max(s.range,index+6));inspect(s,r,el);draw(s,r,el);}
 function draw(s,r,el){
  el.querySelector('.st-chart-context').textContent=s.history?'歷史重播，非目前買點':'目前訊號，截至 '+r.date;el.querySelector('.st-history-toggle').textContent=s.history?'隱藏歷史訊號':'查看歷史訊號';el.querySelector('.st-history-toggle').setAttribute('aria-pressed',String(!!s.history));
  var host=el.querySelector('.st-chart'),all=r.bars,end=Math.min(s.end||all.length,all.length),start=Math.max(0,end-s.range),bars=all.slice(start,end),n=bars.length;
  var W=Math.max(280,Math.min(900,host.clientWidth||700)),H=350,L=8,R=64,T=28,B=250,V=292,step=(W-L-R)/Math.max(n,1),cw=Math.max(1,step*.60);
  var event=(r.signals||[]).find(function(e){return e.id===s.event;});
  var low=Math.min.apply(null,bars.map(function(b){return b.low;})),high=Math.max.apply(null,bars.map(function(b){return b.high;}));
  var stop=event?event.stop:r.stop,target=event?event.target:r.target;
  // Distant levels should not squash candles into a flat line; out-of-range
  // values remain readable in the inspector below the chart.
  var span=Math.max(high-low,high*.02),min=low-span*.13,max=high+span*.13;
  function y(v){return T+(max-v)/(max-min)*(B-T);}
  function x(i){return L+step*(i+.5);}
  var svg='<svg viewBox="0 0 '+W+' '+H+'" role="img" aria-label="日 K、成交量與歷史交易訊號"><title>'+esc(r.name)+' K 線策略圖</title>';
  for(var k=0;k<5;k++){var value=min+(max-min)*k/4,yy=y(value);svg+='<line class="st-grid" x1="'+L+'" y1="'+yy+'" x2="'+(W-R)+'" y2="'+yy+'"/><text class="st-axis" x="'+(W-R+8)+'" y="'+(yy+4)+'">'+price(value)+'</text>';}
  var maxVol=Math.max.apply(null,bars.map(function(b){return b.volume;}))||1;
  bars.forEach(function(b,i){var color=b.close>=b.open?'#c5343b':'#13805f',xx=x(i);svg+='<line stroke="'+color+'" x1="'+xx+'" x2="'+xx+'" y1="'+y(b.high)+'" y2="'+y(b.low)+'"/><rect fill="'+color+'" x="'+(xx-cw/2)+'" y="'+Math.min(y(b.open),y(b.close))+'" width="'+cw+'" height="'+Math.max(1,Math.abs(y(b.open)-y(b.close)))+'"/><rect fill="'+color+'" opacity=".35" x="'+(xx-cw/2)+'" y="'+(V-b.volume/maxVol*30)+'" width="'+cw+'" height="'+(b.volume/maxVol*30)+'"/>';});
  function moving(days,color){var points=[];for(var i=start;i<end;i++){if(i+1>=days){var sum=all.slice(i-days+1,i+1).reduce(function(a,b){return a+b.close;},0)/days;if(sum>=min&&sum<=max)points.push(x(i-start)+','+y(sum));}}if(points.length)svg+='<polyline fill="none" stroke="'+color+'" stroke-width="1.6" points="'+points.join(' ')+'"/>';}
  moving(10,'#b58a34');moving(20,'#647db0');
  [[stop,'支撐停損','#13805f'],[target,event?'當時停利':'停利參考','#a75916']].forEach(function(a){if(a[0]>=min&&a[0]<=max){var yy=y(a[0]);svg+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+yy+'" y2="'+yy+'" stroke="'+a[2]+'" stroke-dasharray="5 5" opacity=".8"/><text x="'+(W-R-4)+'" y="'+(yy-5)+'" text-anchor="end" fill="'+a[2]+'" font-size="11">'+a[1]+'</text>';}});
  if(!event&&r.support>=min&&r.support<=max){svg+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+y(r.support)+'" y2="'+y(r.support)+'" stroke="#647db0" stroke-dasharray="2 5" opacity=".65"/><text x="'+(L+3)+'" y="'+(y(r.support)-5)+'" fill="#647db0" font-size="11">目前支撐 '+price(r.support)+'</text>';}
  var trail=(r.trailing||[]).filter(function(t){return (s.history||(r.current_action!=='unconfirmed'&&r.active&&t.date>=(r.last_entry_date||r.date)))&&t.date>=bars[0].date&&t.date<=bars[n-1].date&&t.value>=min&&t.value<=max;});
  var d='';trail.forEach(function(t,i){var ix=bars.findIndex(function(b){return b.date===t.date;});if(ix>=0)d+=(i?'H'+x(ix)+'V'+y(t.value):'M'+x(ix)+','+y(t.value));});
  if(d)svg+='<path d="'+d+'" fill="none" stroke="#13805f" opacity=".7" stroke-width="1.7"/>';
  (r.signals||[]).filter(function(e){return (s.history||(r.current_action!=='unconfirmed'&&e.date===r.date))&&e.date>=bars[0].date&&e.date<=bars[n-1].date;}).slice(-12).forEach(function(e){var i=bars.findIndex(function(b){return b.date===e.date;}),up=e.type==='entry'||e.type==='add',xx=x(i),yy=y(up?bars[i].low:bars[i].high)+(up?14:-14),path=up?'M-7,6L0,-6L7,6Z':'M-7,-6L0,6L7,-6Z';svg+='<g transform="translate('+xx+','+yy+')"><g data-marker="'+esc(e.id)+'" tabindex="0" role="button" aria-label="'+esc(e.date+' '+names[e.type]+' '+price(e.price))+'"><rect x="-22" y="-22" width="44" height="44" fill="transparent"/><path d="'+path+'" fill="'+colors[e.type]+'"/><text y="'+(up?24:-12)+'" text-anchor="middle" font-size="12" fill="'+colors[e.type]+'">'+names[e.type]+'</text></g></g>';});
  [0,Math.floor(n/2),n-1].forEach(function(i){svg+='<text class="st-axis" x="'+x(i)+'" y="328" text-anchor="'+(i===0?'start':i===n-1?'end':'middle')+'">'+esc(bars[i].date.slice(5))+'</text>';});
  svg+='<line class="st-cross" x1="0" x2="0" y1="'+T+'" y2="'+V+'" visibility="hidden"/><text class="st-axis" x="'+L+'" y="18">10 日線　20 日線　綠階梯：移動支撐停損</text></svg>';
  host.innerHTML=svg;var chart=host.querySelector('svg'),cross=host.querySelector('.st-cross');
  function read(e){var rect=chart.getBoundingClientRect(),px=(e.clientX-rect.left)/rect.width*W,i=Math.max(0,Math.min(n-1,Math.floor((px-L)/step))),b=bars[i];cross.setAttribute('x1',x(i));cross.setAttribute('x2',x(i));cross.setAttribute('visibility','visible');el.querySelector('.st-chart-readout').textContent=b.date+'　開 '+price(b.open)+'　高 '+price(b.high)+'　低 '+price(b.low)+'　收 '+price(b.close);}
  chart.addEventListener('pointerdown',read);chart.addEventListener('pointermove',read);
  chart.querySelectorAll('[data-marker]').forEach(function(g){g.onclick=function(){select(s,r,g.dataset.marker,el);};g.onkeydown=function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();select(s,r,g.dataset.marker,el);}};});
  el.querySelector('[data-step="-1"]').disabled=start===0;el.querySelector('[data-step="1"]').disabled=end===all.length;
 }
 function load(s,force){
  if(!alive(s)||s.loading)return;
  clearTimeout(s.timer);s.loading=true;var id=++s.request,controller=new AbortController(),timeout=setTimeout(function(){controller.abort();},12000);
  var btn=s.el.querySelector('.st-refresh');if(btn){btn.disabled=true;btn.textContent='讀取中…';}
  fetch(api(force),{credentials:'same-origin',cache:'no-store',signal:controller.signal}).then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}).then(function(data){if(!data.ok)throw new Error(data.error||'載入失敗');if(!alive(s)||id!==s.request)return;var changed=JSON.stringify(s.data)!==JSON.stringify(data);s.data=data;s.fetched=Date.now();if(changed)render(s);s.timer=setTimeout(function(){s.timer=null;if(!s.el.hidden&&!document.hidden)load(s,false);},data.meta&&data.meta.building?5000:30000);}).catch(function(e){if(alive(s)){s.data.meta=Object.assign({},s.data.meta,{building:false,error:e.name==='AbortError'?'讀取逾時，請重試；原結果保留':'暫時無法載入，請重試'});render(s);}}).finally(function(){clearTimeout(timeout);s.loading=false;if(alive(s)){var b=s.el.querySelector('.st-refresh');if(b){b.disabled=false;b.textContent='重新篩選';}}});
 }
 window.StockBotShortTerm={mount:function(el){if(current&&current.el===el){if(!current.loading&&!current.timer&&((current.data.meta||{}).building||Date.now()-(current.fetched||0)>30000))load(current,false);return;}current={el:el,data:{rows:[],meta:{building:true}},open:null,sector:'',history:false,range:60,end:null,event:null,request:0,loading:false,polls:0,timer:null,id:++sequence};render(current);load(current,false);},refresh:function(el){this.mount(el);current.polls=0;load(current,true);}};
 document.addEventListener('visibilitychange',function(){if(!document.hidden&&current&&!current.el.hidden&&!current.loading)load(current,false);});
 var panel=document.getElementById('wb-short-term');if(panel&&!panel.hidden)window.StockBotShortTerm.mount(panel);
})();
