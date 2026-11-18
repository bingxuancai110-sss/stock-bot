(function(){
 'use strict';
 var current=null, sequence=0;
 var names={entry:'進場',add:'加碼',reduce:'減碼',exit:'出場'};
 var strategyNames={reversal:'轉折',breakout:'突破',pullback:'回測',continuation:'延續'};
 var colors={entry:'#c5343b',add:'#a75916',reduce:'#94651b',exit:'#13805f'};
 function esc(v){return String(v==null?'—':v).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
 function num(v){return v==null||!Number.isFinite(Number(v))?'—':Number(v).toLocaleString('zh-TW',{maximumFractionDigits:2});}
 function price(v){return v==null||!Number.isFinite(Number(v))?'—':Number(v).toLocaleString('zh-TW',{minimumFractionDigits:2,maximumFractionDigits:2});}
 function spreadText(v){if(v==null||!Number.isFinite(Number(v)))return '價差未確認';return Number(v)>0?'每股賺 NT$'+price(v):Number(v)<0?'每股賠 NT$'+price(-v):'每股價差 NT$0.00';}
 function spreadClass(v){return Number(v)>0?'st-up':Number(v)<0?'st-down':'';}
 function spreadView(r){var p=r.spreads||{},trades=p.completed||[],last=trades[trades.length-1],out='';if(last){out+='<div class="st-profit"><span>'+(r.provisional&&last.exit_date===r.date?'含盤中暫定出場的每股價差':'已出場每股價差')+(trades.length>1?'（'+trades.length+' 次合計）':'')+'</span><strong class="'+spreadClass(p.total_closed_spread)+'">'+spreadText(p.total_closed_spread)+'</strong><small>上次 '+esc(last.entry_date)+' '+price(last.entry_price)+' → '+esc(last.exit_date)+' '+price(last.exit_price)+(trades.length>1?' · '+spreadText(last.spread):'')+'</small></div>';}if(p.open&&p.open.spread!=null){var o=p.open;out+='<div class="st-profit"><span>尚未出場 · 未實現每股價差</span><strong class="'+spreadClass(o.spread)+'">'+spreadText(o.spread)+'</strong><small>'+esc(o.entry_date)+' '+price(o.entry_price)+' → '+esc(o.date)+' '+price(o.price)+'</small></div>';}return out;}
 function api(refresh){var t=new URLSearchParams(location.search).get('t')||'';return '/web/api/workbench/short-term'+(refresh?'?refresh=1':'?read=1')+(t?'&t='+encodeURIComponent(t):'')+(current&&current.sector?'&sector='+encodeURIComponent(current.sector):'')+(current&&current.position?'&position='+encodeURIComponent(current.position):'')+(current&&current.shape?'&shape='+encodeURIComponent(current.shape):'')+'&offset='+(current&&current.offset||0)+'&limit=20';}
 function alive(s){return current===s&&s.el.isConnected;}
 function notices(s){
  var meta=s.data.meta||{},date=meta.date||'未確認',valid=meta.signals_current===true;
  var alerts=valid?(s.data.alerts||[]):[],entries=alerts.filter(function(e){return e.type==='entry';});
  var counts=meta.stage_counts||{watch:(s.data.rows||[]).filter(function(r){return !r.entry_ready;}).length,triggered:0,entry:entries.length};
  var title=!valid?'最新訊號尚未確認':'觀察 '+(counts.watch||0)+' · 型態觸發 '+(counts.triggered||0)+' · '+(meta.intraday?'暫定進場 ':'進場 ')+(counts.entry||entries.length||0);
  var body=!valid?'<p>'+esc(meta.building?'正在更新；舊紀錄不作為新的進場提示。':'資料截至 '+date+'，尚未確認 '+(meta.expected_date||'最新交易日')+' 的訊號。')+'</p>':'';
  if(valid&&alerts.length){var renderAlert=function(e){return '<div class="st-notice-event" data-kind="'+esc(e.type)+'"><b>'+esc(e.name)+' '+esc(e.code)+'｜'+esc((e.provisional?'盤中暫定 · ':'')+(e.type==='entry'?'新進場':names[e.type]))+'</b><span>'+esc(e.date)+' · 訊號價 '+price(e.price)+'</span><span>硬停損 '+price(e.stop)+'　停利參考 '+price(e.target)+'</span>'+(e.type==='exit'&&e.spread!=null?'<strong class="'+spreadClass(e.spread)+'">'+spreadText(e.spread)+'</strong>':'')+'</div>';};body+=alerts.slice(0,6).map(renderAlert).join('');if(alerts.length>6)body+='<details><summary>其餘 '+(alerts.length-6)+' 筆新訊號</summary>'+alerts.slice(6).map(renderAlert).join('')+'</details>';body+='<p>'+(meta.intraday?'盤中暫定訊號可能消失，不代表已收盤確認或可按該價格成交。':'下一交易日須重新確認價格與停損距離，不代表可按訊號價直接成交。')+'</p>';}
  var observations=valid?(s.data.observations||[]):[];if(observations.length)body+='<p>今日新增型態觀察 '+observations.length+' 檔 · '+observations.slice(0,3).map(function(r){return esc(r.name);}).join('、')+(observations.length>3?'…':'')+'</p>';
  if(valid&&meta.failed)body+='<p>另有 '+num(meta.failed)+' 檔資料待確認。</p>';
  return '<section class="st-notices" aria-label="最新交易訊號" role="status"><h3>'+esc(title)+'</h3>'+body+'</section>';
 }
 function group(r){
  if(r.position_group)return r.position_group;
  if(r.current_action==='unconfirmed')return 'unknown';
  var es=(r.signals||[]).filter(function(e){return !e.provisional&&(e.type==='entry'||e.type==='exit');});
  return es.length?(es[es.length-1].type==='entry'?'held':'closed'):r.active&&!r.provisional?'held':'waiting';
 }
 var groupNames={held:'場內',waiting:'未進場',closed:'已出場',unknown:'待更新'};
 function concise(r){
  if(r.current_action==='unconfirmed')return '報價待更新';
  if(r.signal_stage==='triggered'&&!r.latest_event)return '型態已觸發，等待進場確認';
  if(r.watch_date===r.date&&!r.active&&!r.latest_event)return r.watch_date.slice(5)+' 新觀察';
  if(r.latest_event)return (r.provisional?'盤中':'今日')+names[r.latest_event]+(r.provisional?'待確認':'');
  var g=group(r);return g==='held'?(r.last_entry_date||'')+' 進場':g==='closed'?(r.last_exit_date||'')+' 出場':'尚無進場訊號';
 }
 function compactSpread(r){
  var p=r.spreads||{},open=p.open,last=(p.completed||[]).slice(-1)[0],g=group(r),v=g==='held'&&open?open.spread:g==='closed'&&last?last.spread:null;
  if(v==null)return '';
  return '<div class="st-profit st-compact-profit"><span>'+(g==='held'?'未實現每股價差':'上次每股價差')+'</span><strong class="'+spreadClass(v)+'">'+spreadText(v)+'</strong></div>';
 }
 function header(s){
  var m=s.data.meta||{},scope=m.scope||{},counts=m.position_counts||{},rows=s.data.rows||[];
  if(!m.position_counts)Object.keys(groupNames).forEach(function(g){counts[g]=rows.filter(function(r){return group(r)===g;}).length;});
  s.el.innerHTML='<header class="st-heading"><h2>波段機會</h2><button type="button" class="st-refresh">更新</button></header>'+
   '<div class="st-status" role="status">'+esc(m.building?(m.phase||'更新中')+' '+(m.progress||0)+'／'+(m.progress_total||80):m.error?m.error:(m.intraday?'盤中':'收盤')+' · '+(m.date||'資料待確認')+' · '+(m.generated_at?m.generated_at.slice(11,16)+' 更新':'等待更新'))+'</div>'+
   notices(s)+'<div class="st-sector" aria-label="短線產業篩選">'+['全部','電子','非電子'].map(function(v){return '<button type="button" data-sector="'+v+'" aria-pressed="'+((s.sector||'全部')===v)+'">'+v+'</button>';}).join('')+'<select class="st-shape-filter" aria-label="型態">'+['','reversal','breakout','pullback','continuation'].map(function(k){return '<option value="'+k+'" '+((s.shape||'')===k?'selected':'')+'>'+(strategyNames[k]||'全部型態')+'</option>';}).join('')+'</select></div>'+
   '<div class="st-state-tabs" aria-label="進出場狀態">'+['','held','waiting','closed'].map(function(g){return '<button type="button" data-position="'+g+'" aria-pressed="'+((s.position||'')===g)+'">'+(g?groupNames[g]:'全部')+(g?' <b>'+num(counts[g]||0)+'</b>':'')+'</button>';}).join('')+'</div><div class="st-list-summary">共 '+num(m.total==null?rows.length:m.total)+' 檔 · 趨勢較明確的在前</div><div class="st-cards"></div><nav class="st-pagination" aria-label="候選分頁"><button type="button" data-page="-1" '+((m.offset||0)<=0?'disabled':'')+'>上一頁</button><span>第 '+(Math.floor((m.offset||0)/(m.limit||20))+1)+'／'+Math.max(1,Math.ceil((m.total||rows.length)/(m.limit||20)))+' 頁</span><button type="button" data-page="1" '+(!m.has_more?'disabled':'')+'>下一頁</button></nav>'+
   '<details class="st-data"><summary>更新與掃描範圍</summary><div class="st-refresh-info">'+esc((m.intraday?'獨立排程每 10 分鐘補檢，全市場分批接續':'收盤確認模式')+' · 上次完成 '+(m.generated_at?m.generated_at.slice(0,19).replace('T',' '):'尚未完成'))+'<br>'+esc(m.intraday?'下一輪約 '+(m.next_refresh_at?m.next_refresh_at.slice(11,19):'等待排程')+'；行情可能延遲，量比依開盤時間比例估算。':'盤中暫定訊號與收盤結果分開保存。')+'</div><div class="st-scope">全市場初篩 '+num(scope.universe_count||0)+' 檔 · 已詳查 '+num(scope.deep_done||m.scanned||0)+'／'+num(scope.deep_total||scope.universe_count||m.scanned||0)+' 檔'+(scope.deep_complete?'（本輪已處理完）':'（分批接續）')+'<br>'+esc(Object.keys(scope.sectors||{}).map(function(k){return k+' '+scope.sectors[k]+' 檔';}).join(' · '))+'<br>'+esc(Object.keys(scope.markets||{}).map(function(k){return k+' '+scope.markets[k]+' 檔';}).join(' · '))+'<br>技術詳查：'+esc(Object.keys(scope.deep_sectors||{}).map(function(k){return k+' '+scope.deep_sectors[k]+' 檔';}).join(' · '))+'<br>符合觀察條件：'+esc(Object.keys(scope.qualified_sectors||{}).map(function(k){return k+' '+scope.qualified_sectors[k]+' 檔';}).join(' · '))+'</div><p>行情讀取缺漏 '+num(m.failed||0)+' 檔；缺漏資料不補造訊號。</p><div class="st-price-audit"><h4>價格涵蓋範圍</h4><p>不設股價上限，不按法人買超張數排序。</p><table><thead><tr><th>股價</th><th>取得行情</th><th>技術初篩</th><th>詳查</th><th>符合觀察</th></tr></thead><tbody>'+['低於100','100–499','500–999','1000以上'].map(function(k){var c=(scope.price_coverage||{})[k]||{};return '<tr><th>'+k+'</th><td>'+num(c.quoted||0)+'</td><td>'+num(c.technical||0)+'</td><td>'+num(c.deep||0)+'</td><td>'+num(c.qualified||0)+'</td></tr>';}).join('')+'</tbody></table><details><summary>高價股初篩樣本（最多20檔）</summary>'+((scope.high_price_examples||[]).map(function(r){return '<p>'+esc(r.name)+' '+esc(r.code)+' · '+price(r.price)+'<br>'+esc(r.reason)+'</p>';}).join('')||'<p>本輪診斷尚未完成。</p>')+'</details></div></details>'+
   '<details class="st-rules"><summary>訊號規則</summary><div><p>技術面為主，法人只供參考。成交量、成交金額不設最低門檻；不設股價上限，不按法人買超張數排序。每頁20檔，可翻頁查看全部候選，依型態與趨勢排列；未詳查的股票不宣稱已完成。</p><p>進場：轉折、突破、回測、延續其中一類型態成立，再檢查量價與風險空間。下降線／底部轉折須連續兩日收在同一突破線加緩衝之上，且兩日均站回20日線；不用同時滿足全部型態，也不要求法人買超。加碼：獲利達 1R、再突破且合併風險受控。波段持有：短期支撐轉弱先減碼一半。連續兩日收盤跌破事前趨勢支撐與20日線，且20日線走平或下彎，才確認反轉出清；觸及硬停損優先退出。收復支撐、再次突破且風險受控時可回補；法人不影響買賣訊號。</p><p>盤中暫定訊號可能消失，量比依時間比例估算；收盤後重算。單日漲跌超過9.5%或ATR波動超過6%不進場；一般進場停損距離最多6%，報酬風險比至少1.5R。法人同買僅指外資＋投信前五個已完成交易日淨買合計為正。場內是策略重播狀態，不是你的實際帳戶持股；這個分頁不下單。</p></div></details>';
  s.el.querySelector('.st-shape-filter').onchange=function(e){s.shape=e.target.value;s.position='';s.offset=0;s.open=null;s.viewChanged=true;if(s.loading)s.pending=true;else load(s,false);};
  s.el.querySelector('.st-refresh').onclick=function(){load(s,true);};
  s.el.querySelectorAll('[data-sector]').forEach(function(b){b.onclick=function(){s.sector=b.dataset.sector==='全部'?'':b.dataset.sector;s.position='';s.offset=0;s.open=null;s.viewChanged=true;if(s.loading)s.pending=true;else load(s,false);};});
  s.el.querySelectorAll('[data-position]').forEach(function(b){b.onclick=function(){s.position=b.dataset.position;s.offset=0;s.open=null;s.viewChanged=true;if(s.loading)s.pending=true;else load(s,false);};});
  s.el.querySelectorAll('[data-page]').forEach(function(b){b.onclick=function(){s.offset=Math.max(0,(s.offset||0)+Number(b.dataset.page)*20);s.open=null;s.viewChanged=true;if(s.loading)s.pending=true;else load(s,false);};});
  if(!s.el.hidden){var status=document.getElementById('wb-status');if(status)status.textContent=m.building?'短線更新中':'短線';}
 }
 function render(s){
  if(!alive(s))return;
  header(s);var rows=s.data.rows||[],host=s.el.querySelector('.st-cards');
  if(!rows.length){host.innerHTML=(s.data.meta||{}).building?'<div class="st-skeleton" aria-label="正在篩選候選"></div>':'<div class="st-empty"><b>'+(s.position==='held'?'目前沒有場內股票':s.position==='closed'?'目前沒有已出場股票':'目前沒有符合條件的股票')+'</b><p>可切換其他狀態查看。</p></div>';return;}
  var previous=null;
  host.innerHTML=rows.map(function(r){
   var g=group(r),up=r.change_pct>=0,heading='';
   var es=(r.signals||[]).filter(function(e){return e.type==='entry'&&!e.provisional;}),entry=g==='held'?(r.active?r.entry:(es.slice(-1)[0]||{}).price):r.entry;
   var preview=(r.signals||[]).filter(function(e){return e.provisional;}).slice(-1)[0],stop=preview&&g==='held'?preview.stop:r.stop,target=preview&&g==='held'?preview.target:r.target;
   return heading+'<article class="st-card" data-code="'+esc(r.code)+'" data-position="'+g+'"><button type="button" class="st-card-toggle" aria-expanded="'+(s.open===r.code)+'" aria-controls="st-detail-'+esc(r.code)+'"><div class="st-stock"><span class="st-state" data-position="'+g+'">'+groupNames[g]+'</span><h3>'+esc(r.name)+' <small>'+esc(r.code)+'</small></h3>'+ (r.strategy?'<span class="st-pattern-label">'+esc(strategyNames[s.shape||r.strategy]||'觀察')+'</span>':'')+(r.institutional_buying?'<span class="st-chip-badge" title="外資＋投信前五個已完成交易日淨買合計為正；僅供參考">法人同買</span>':'')+'<span class="st-date">'+esc(concise(r))+'</span></div><div class="st-quote"><strong>'+price(r.price)+'</strong><span class="'+(up?'st-up':'st-down')+'">'+(up?'+':'')+Number(r.change_pct).toFixed(2)+'%</span></div></button>'+
   (r.provisional&&r.latest_event?'<div class="st-live-action">'+esc('盤中'+names[r.latest_event]+'待確認')+'</div>':'')+
   (g==='held'||g==='waiting'?'<div class="st-levels"><div><span>'+(g==='held'?'進場價':'觀察價')+'</span><b>'+price(entry)+'</b></div><div><span>'+(g==='held'?'硬停損':'硬停損參考')+'</span><b class="st-down">'+price(stop)+'</b></div><div><span>'+(g==='held'?'停利':'停利參考')+'</span><b>'+price(target)+'</b></div></div>':'')+
   compactSpread(r)+'<button type="button" class="st-expand" data-expand="'+esc(r.code)+'">'+(s.open===r.code?'收合 −':'K 線與進出場理由 ＋')+'</button><section class="st-detail" id="st-detail-'+esc(r.code)+'" '+(s.open===r.code?'':'hidden')+'></section></article>';
  }).join('');
  function toggle(code){s.open=s.open===code?null:code;s.history=false;s.event=null;render(s);}
  host.querySelectorAll('.st-card-toggle').forEach(function(b){b.onclick=function(){toggle(b.closest('.st-card').dataset.code);};});
  host.querySelectorAll('[data-expand]').forEach(function(b){b.onclick=function(){toggle(b.dataset.expand);};});
  if(s.open){var row=rows.find(function(r){return r.code===s.open;});if(row)detail(s,row);}
 }
 function timeline(r){
  var events=(r.signals||[]).slice().reverse();
  return '<section class="st-history" aria-label="訊號時間軸"><h4>訊號時間軸（'+events.length+'）</h4><p class="st-history-note">依日期由新到舊；歷史規則重播，不是當下的新買點。</p><ol>'+events.map(function(e){return '<li><button type="button" data-event="'+esc(e.id)+'"><span>'+esc(e.date)+'</span><b class="'+(['entry','add'].indexOf(e.type)>=0?'st-up':'st-down')+'">'+esc(names[e.type])+'</b><strong>'+price(e.price)+'</strong></button><span class="st-event-status">'+(e.provisional?'盤中暫定 · 未收棒':'歷史收盤規則重播')+'</span><p class="st-event-reason"><b>理由：</b>'+esc((e.reason||'理由未保存').split('；')[0])+'</p><details><summary>查看當時數值與完整理由</summary><p>'+esc(e.reason||'此筆舊紀錄沒有保存理由，不補造原因。')+'</p></details><small>當時硬停損 '+price(e.stop)+' · 停利參考 '+price(e.target)+'</small></li>';}).join('')+'</ol>'+(events.length?'':'<p>這段資料尚無進出場紀錄，不補畫訊號。</p>')+'</section>';
 }
 function detail(s,r){
  var el=s.el.querySelector('#st-detail-'+r.code),events=r.signals||[],chip=r.chip||{};
  el.innerHTML='<div class="st-chart-head"><div><b>K 線與策略訊號</b><span>'+(r.provisional?'盤中日線 · 暫定':'日線 · 收盤確認')+' · '+esc((r.market||'')+' '+(r.sector||''))+'</span></div><div class="st-periods" aria-label="圖表期間">'+[30,60,120].map(function(n){return '<button type="button" data-range="'+n+'" aria-pressed="'+(s.range===n)+'">'+n+' 日</button>';}).join('')+'</div></div>'+
   '<div class="st-chart-mode"><span class="st-chart-context"></span><button type="button" class="st-history-toggle" aria-pressed="false">查看歷史訊號</button></div>'+
   '<div class="st-chart-readout" aria-live="polite">左右滑動查看日期與價格</div><div class="st-chart" role="group" aria-label="'+esc(r.name)+'日 K 與進出場訊號"></div>'+
   '<div class="st-chart-nav"><button type="button" data-step="-1" aria-label="查看較早K線">← 較早</button><span>▲ 進場／加碼　▼ 減碼／出場</span><button type="button" data-step="1" aria-label="查看較新K線">較新 →</button></div>'+
   '<div class="st-inspector" aria-live="polite"></div><div class="st-checks">'+(r.checks||[]).map(function(c){return '<span data-passed="'+!!c.passed+'">'+(c.passed?'✓ ':'— ')+esc(c.label)+(c.passed?'成立':'待確認')+'</span>';}).join('')+'</div><div class="st-evidence"><span>'+(r.volume_estimated?'時間比例估算量比 ':'量比 ')+num(r.volume_ratio)+'</span>'+(r.institutional_buying?'<span>法人同買（前五個已完成交易日）</span>':'')+'</div>'+timeline(r)+'<div class="st-chip-detail"><h4>技術依據與法人參考</h4><dl><div><dt>近 20 日平均成交額（非門檻）</dt><dd>'+num(r.avg_turnover/10000)+' 萬元</dd></div><div><dt>近 20 日平均成交量（非門檻）</dt><dd>'+num(r.avg_volume/1000)+' 張</dd></div><div><dt>支撐參考</dt><dd>'+price(r.support)+'</dd></div><div><dt>上方歷史壓力</dt><dd>'+(r.resistance?price(r.resistance):'區間內無已知壓力')+'</dd></div><div><dt>外資近五日</dt><dd>'+num(chip.foreign)+' 張／'+num(chip.foreign_days)+' 日買超</dd></div><div><dt>投信近五日</dt><dd>'+num(chip.trust)+' 張／'+num(chip.trust_days)+' 日買超</dd></div><div><dt>法人淨買占五日成交量（參考）</dt><dd>'+num(r.chip_ratio)+'%</dd></div><div><dt>籌碼資料截至</dt><dd>'+esc(chip.date||'缺資料')+'</dd></div></dl></div>'+
   '<p class="st-source">行情：'+esc(r.source)+(r.observed_at?' · 來源時間 '+esc(r.observed_at.slice(0,19).replace('T',' ')):'')+' · 趨勢支撐跌破先警戒／減碼；硬停損仍可能直接出場。</p>';
  el.querySelector('.st-history-toggle').onclick=function(){s.history=!s.history;s.event=null;s.end=null;inspect(s,r,el);draw(s,r,el);};
  el.querySelectorAll('[data-range]').forEach(function(b){b.onclick=function(){s.range=Number(b.dataset.range);s.end=null;detail(s,r);};});
  el.querySelectorAll('[data-step]').forEach(function(b){b.onclick=function(){s.end=Math.max(Math.min(s.range,r.bars.length),Math.min(r.bars.length,(s.end||r.bars.length)+Number(b.dataset.step)*Math.max(5,Math.floor(s.range/2))));draw(s,r,el);};});
  el.querySelectorAll('[data-event]').forEach(function(b){b.onclick=function(){select(s,r,b.dataset.event,el);};});
  inspect(s,r,el);draw(s,r,el);
 }
 function inspect(s,r,el){
  var e=(r.signals||[]).find(function(x){return x.id===s.event;}),box=el.querySelector('.st-inspector');
  box.innerHTML=e?'<span class="st-inspector-label">'+esc(e.date)+' · 歷史規則重播</span><h4 style="color:'+colors[e.type]+'">'+names[e.type]+'訊號 '+price(e.price)+'</h4><p>'+esc(e.reason)+'</p><div><span>當時硬停損價 <b>'+price(e.stop)+'</b></span><span>當時停利參考 <b>'+price(e.target)+'</b></span></div><button type="button" class="st-current">回到目前計畫</button>':'<span class="st-inspector-label">目前狀態</span><h4>'+esc(r.state)+'</h4><p>'+esc(r.action_note||'尚未確認新的進場訊號。')+'</p><div>'+(r.pattern_line?'<span>下降壓力線 <b>'+price(r.pattern_line.end_price)+'</b></span>':'')+'<span>趨勢支撐 <b>'+price(r.soft_support)+'</b></span><span>硬停損價 <b>'+price(r.stop)+'</b></span><span>停利參考 <b>'+price(r.target)+'</b></span></div>';
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
  var line=event?(event.pattern_line||null):r.pattern_line;if(line){var ia=all.findIndex(function(b){return b.date===line.start_date;}),ib=all.findIndex(function(b){return b.date===line.end_date;});if(ia>=0&&ib>ia){var slope=(line.end_price-line.start_price)/(ib-ia),lo=Math.max(start,ia),hi=Math.min(end-1,ib),pa=line.start_price+slope*(lo-ia),pb=line.start_price+slope*(hi-ia);if(hi>lo&&pa>=min&&pa<=max&&pb>=min&&pb<=max)svg+='<line class="st-pattern-line" x1="'+x(lo-start)+'" x2="'+x(hi-start)+'" y1="'+y(pa)+'" y2="'+y(pb)+'" stroke="#b58a34" stroke-width="2" stroke-dasharray="6 3"/>';}}
  [[stop,'硬停損','#13805f'],[target,event?'當時停利':'停利參考','#a75916']].forEach(function(a){if(a[0]>=min&&a[0]<=max){var yy=y(a[0]);svg+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+yy+'" y2="'+yy+'" stroke="'+a[2]+'" stroke-dasharray="5 5" opacity=".8"/><text x="'+(W-R-4)+'" y="'+(yy-5)+'" text-anchor="end" fill="'+a[2]+'" font-size="11">'+a[1]+'</text>';}});
  if(!event&&r.support>=min&&r.support<=max){svg+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+y(r.support)+'" y2="'+y(r.support)+'" stroke="#647db0" stroke-dasharray="2 5" opacity=".65"/><text x="'+(L+3)+'" y="'+(y(r.support)-5)+'" fill="#647db0" font-size="11">目前支撐 '+price(r.support)+'</text>';}
  var trail=(r.trailing||[]).filter(function(t){return (s.history||(r.current_action!=='unconfirmed'&&r.active&&t.date>=(r.last_entry_date||r.date)))&&t.date>=bars[0].date&&t.date<=bars[n-1].date&&t.value>=min&&t.value<=max;});
  var d='';trail.forEach(function(t,i){var ix=bars.findIndex(function(b){return b.date===t.date;});if(ix>=0)d+=(i?'H'+x(ix)+'V'+y(t.value):'M'+x(ix)+','+y(t.value));});
  if(d)svg+='<path d="'+d+'" fill="none" stroke="#13805f" opacity=".7" stroke-width="1.7"/>';
  (r.signals||[]).filter(function(e){return (s.history||(r.current_action!=='unconfirmed'&&e.date===r.date))&&e.date>=bars[0].date&&e.date<=bars[n-1].date;}).slice(-12).forEach(function(e){var i=bars.findIndex(function(b){return b.date===e.date;}),up=e.type==='entry'||e.type==='add',xx=x(i),yy=y(up?bars[i].low:bars[i].high)+(up?14:-14),path=up?'M-7,6L0,-6L7,6Z':'M-7,-6L0,6L7,-6Z';svg+='<g transform="translate('+xx+','+yy+')"><g data-marker="'+esc(e.id)+'" tabindex="0" role="button" aria-label="'+esc(e.date+' '+names[e.type]+' '+price(e.price))+'"><rect x="-22" y="-22" width="44" height="44" fill="transparent"/><path d="'+path+'" fill="'+colors[e.type]+'"/><text y="'+(up?24:-12)+'" text-anchor="middle" font-size="12" fill="'+colors[e.type]+'">'+names[e.type]+'</text></g></g>';});
  [0,Math.floor(n/2),n-1].forEach(function(i){svg+='<text class="st-axis" x="'+x(i)+'" y="328" text-anchor="'+(i===0?'start':i===n-1?'end':'middle')+'">'+esc(bars[i].date.slice(5))+'</text>';});
  svg+='<line class="st-cross" x1="0" x2="0" y1="'+T+'" y2="'+V+'" visibility="hidden"/><text class="st-axis" x="'+L+'" y="18">10／20日線　虛線：下降壓力　綠階梯：硬停損</text></svg>';
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
  fetch(api(force),{credentials:'same-origin',cache:'no-store',signal:controller.signal}).then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}).then(function(data){if(!data.ok)throw new Error(data.error||'載入失敗');if(!alive(s)||id!==s.request)return;var changed=s.viewChanged||JSON.stringify(s.data)!==JSON.stringify(data);s.viewChanged=false;s.data=data;s.fetched=Date.now();if(changed)render(s);s.timer=setTimeout(function(){s.timer=null;if(!s.el.hidden&&!document.hidden)load(s,false);},data.meta&&data.meta.building?5000:30000);}).catch(function(e){if(alive(s)){s.data.meta=Object.assign({},s.data.meta,{building:false,error:e.name==='AbortError'?'讀取逾時，請重試；原結果保留':'暫時無法載入，請重試'});render(s);}}).finally(function(){clearTimeout(timeout);s.loading=false;if(s.pending&&alive(s)){s.pending=false;load(s,false);return;}if(alive(s)){var b=s.el.querySelector('.st-refresh');if(b){b.disabled=false;b.textContent='更新';}}});
 }
 window.StockBotShortTerm={mount:function(el){if(current&&current.el===el){if(!current.loading&&!current.timer&&((current.data.meta||{}).building||Date.now()-(current.fetched||0)>30000))load(current,false);return;}current={el:el,data:{rows:[],meta:{building:true}},open:null,offset:0,sector:'',shape:'',position:'',pending:false,history:false,range:60,end:null,event:null,request:0,loading:false,polls:0,timer:null,id:++sequence};render(current);load(current,false);},refresh:function(el){this.mount(el);current.polls=0;load(current,true);}};
 document.addEventListener('visibilitychange',function(){if(!document.hidden&&current&&!current.el.hidden&&!current.loading)load(current,false);});
 var panel=document.getElementById('wb-short-term');if(panel&&!panel.hidden)window.StockBotShortTerm.mount(panel);
})();
