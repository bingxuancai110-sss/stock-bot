(function(){
 'use strict';
 var current=null, sequence=0;
 var names={entry:'進場',add:'加碼',reduce:'減碼',exit:'出場'};
 var strategyNames={reversal:'轉折',breakout:'突破',pullback:'回測',continuation:'延續',repair:'多方修復'};
 var colors={entry:'#c5343b',add:'#a75916',reduce:'#94651b',exit:'#13805f'};
 function esc(v){return String(v==null?'—':v).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
 function num(v){return v==null||!Number.isFinite(Number(v))?'—':Number(v).toLocaleString('zh-TW',{maximumFractionDigits:2});}
 function price(v){return v==null||!Number.isFinite(Number(v))?'—':Number(v).toLocaleString('zh-TW',{minimumFractionDigits:2,maximumFractionDigits:2});}
 function cardQuote(row){
  var quote=row.display_quote||{},official=quote.price!=null;
  var value=official?quote.price:row.price,pct=official?quote.change_pct:row.change_pct;
  var label=official?(quote.final?'正式收盤':'盤中成交'):'策略日 K';
  var date=official?quote.date:row.date;
  var stamp=date?date.slice(5).replace('-','/'):'';
  if(official&&!quote.final&&quote.time)stamp+=' '+quote.time.slice(0,5);
  var change=pct==null||!Number.isFinite(Number(pct))?'—':(Number(pct)>=0?'+':'')+Number(pct).toFixed(2)+'%';
  return '<div class="st-quote"><strong>'+price(value)+'</strong><span class="'+(Number(pct)>=0?'st-up':'st-down')+'">'+change+'</span><span class="st-open-label" title="'+esc(official?quote.source:row.source)+'">'+esc(label+(stamp?' '+stamp:''))+'</span></div>';
 }
 function sizing(e){var a=e&&e.allocation;if(!a)return '';return '<span class="st-sizing"><strong>'+esc(a.label)+'</strong><span>部位 '+num(a.before_pct)+'% → '+num(a.after_pct)+'%（首次股數為100%）</span></span>';}
 function currentSizing(r){if(r.current_action==='unconfirmed'||!r.latest_event)return '';return sizing((r.signals||[]).filter(function(e){return e.date===r.date&&e.type===r.latest_event;}).slice(-1)[0]);}
 function spreadText(v){if(v==null||!Number.isFinite(Number(v)))return '價差未確認';return Number(v)>0?'每股賺 NT$'+price(v):Number(v)<0?'每股賠 NT$'+price(-v):'每股價差 NT$0.00';}
 function spreadClass(v){return Number(v)>0?'st-up':Number(v)<0?'st-down':'';}
 function spreadView(r){var p=r.spreads||{},trades=p.completed||[],last=trades[trades.length-1],out='';if(last){out+='<div class="st-profit"><span>'+(r.provisional&&last.exit_date===r.date?'含盤中暫定出場的每股價差':'已出場每股價差')+(trades.length>1?'（'+trades.length+' 次合計）':'')+'</span><strong class="'+spreadClass(p.total_closed_spread)+'">'+spreadText(p.total_closed_spread)+'</strong><small>上次 '+esc(last.entry_date)+' '+price(last.entry_price)+' → '+esc(last.exit_date)+' '+price(last.exit_price)+(trades.length>1?' · '+spreadText(last.spread):'')+'</small></div>';}if(p.open&&p.open.spread!=null){var o=p.open;out+='<div class="st-profit"><span>尚未出場 · 未實現每股價差</span><strong class="'+spreadClass(o.spread)+'">'+spreadText(o.spread)+'</strong><small>'+esc(o.entry_date)+' '+price(o.entry_price)+' → '+esc(o.date)+' '+price(o.price)+'</small></div>';}return out;}
 function api(refresh){var t=new URLSearchParams(location.search).get('t')||'';return '/web/api/workbench/short-term'+(refresh?'?refresh=1':'?read=1')+(t?'&t='+encodeURIComponent(t):'')+'&price_range='+encodeURIComponent(current&&current.priceRange||'all')+'&sort='+encodeURIComponent(current&&current.sort||'signal')+'&direction='+encodeURIComponent(current&&current.direction||'long')+(current&&current.sector?'&sector='+encodeURIComponent(current.sector):'')+(current&&current.position?'&position='+encodeURIComponent(current.position):'')+(current&&current.shape?'&shape='+encodeURIComponent(current.shape):'')+'&offset='+(current&&current.offset||0)+'&limit=20';}
 function alive(s){return current===s&&s.el.isConnected;}
 function viewKey(s){return [s.priceRange,s.sort,s.direction,s.sector,s.position,s.shape,s.offset].join('|');}
 function previousSnapshot(s,message){
  var cached=s.snapshot;
  if(!cached||cached.view!==viewKey(s)){
   try{cached=JSON.parse(sessionStorage.getItem('st-last-results-'+s.direction)||'null');}catch(ignore){}
  }
  if(!cached||cached.view!==viewKey(s)||!cached.data?.meta?.date)return null;
  var data=Object.assign({},cached.data);
  data.meta=Object.assign({},data.meta,{building:false,signals_current:false,previous_snapshot:true,error:message,position_counts:{unknown:data.meta.total}});
  data.rows=data.rows.map(function(row){return Object.assign({},row,{state:'上次保存',current_action:'unconfirmed',position_group:'unknown',entry_ready:false,action_note:'目前顯示上次保存的研究結果。'});});
  data.notifications=[];
  return data;
 }
 var bannerTimer;
 function dismissActionBanner(){
  clearTimeout(bannerTimer);
  var banner=document.getElementById('st-live-banner');
  if(banner)banner.remove();
 }
 function announceActions(s,data){
  var meta=data.meta||{},events=data.notifications||[];
  if(s.el.hidden||document.hidden||!meta.intraday||!meta.signals_current||!meta.expected_date)return;
  var storageKey='st-live-actions:'+meta.expected_date;
  if(s.liveActionDay!==meta.expected_date){
   s.liveActionDay=meta.expected_date;
   s.seen=new Set();
   try {
    var saved=JSON.parse(sessionStorage.getItem('st-live-actions')||'null');
    if(saved&&saved.key===storageKey)s.seen=new Set(saved.ids);
   }catch(ignore){}
   s.liveActionBaseline=false;
  }
  var fresh=events.filter(function(event){
   if(event.date!==meta.expected_date||!Object.prototype.hasOwnProperty.call(names,event.type))return false;
   var key=[event.direction,event.code,event.date,event.type].join('|');
   if(s.seen.has(key))return false;
   s.seen.add(key);
   var age=Date.now()-Date.parse(event.observed_at);
   return s.liveActionBaseline||(age>=-60000&&age<=180000);
  });
  s.liveActionBaseline=true;
  try{sessionStorage.setItem('st-live-actions',JSON.stringify({key:storageKey,ids:Array.from(s.seen)}));}catch(ignore){}
  if(!fresh.length)return;
  dismissActionBanner();
  var event=fresh[0],short=event.direction==='short';
  var action=short?{entry:'放空',add:'加空',reduce:'部分回補',exit:'回補'}[event.type]:names[event.type];
  var banner=document.createElement('aside');
  banner.id='st-live-banner';
  banner.innerHTML='<button type="button" class="st-live-banner-main"><span role="status"><strong>短線有新動作</strong><span>'+
   esc(event.name)+' '+esc(event.code)+' · '+esc(action)+' · 盤中暫定'+
   (fresh.length>1?' · 另 '+(fresh.length-1)+' 筆':'')+'</span></span></button>'+
   '<button type="button" class="st-live-banner-close" aria-label="關閉短線通知">×</button>';
  banner.querySelector('.st-live-banner-close').onclick=dismissActionBanner;
  banner.querySelector('.st-live-banner-main').onclick=function(){
   dismissActionBanner();
   if(!alive(s)||s.el.hidden)return;
   s.direction=short?'short':'long';
   s.sector='';s.shape='';s.position='';s.priceRange='all';s.offset=0;s.open=null;s.viewChanged=true;
   s.focusLiveActions=true;
   if(s.loading)s.pending=true;
   else load(s,false);
  };
  document.body.appendChild(banner);
  bannerTimer=setTimeout(dismissActionBanner,9000);
 }
 function notices(s){
  var meta=s.data.meta||{},date=meta.date||'未確認',valid=meta.signals_current===true;
  var alerts=valid?(s.data.alerts||[]):[],entries=alerts.filter(function(e){return e.type==='entry';});
  var counts=meta.stage_counts||{watch:(s.data.rows||[]).filter(function(r){return !r.entry_ready;}).length,triggered:0,entry:entries.length};
  var title=!valid?'最新訊號尚未確認':'觀察 '+(counts.watch||0)+' · 型態觸發 '+(counts.triggered||0)+' · '+(meta.intraday?'盤中進場 ':'今日進場 ')+(counts.entry||entries.length||0);
  var body=!valid?'<p>'+esc(meta.building?'正在更新；舊紀錄不作為新的進場提示。':meta.date?'資料截至 '+date+'，尚未確認 '+(meta.expected_date||'最新交易日')+' 的訊號。':'尚未取得可顯示的短線快照。')+'</p>':'';
  if(valid&&alerts.length){var renderAlert=function(e){return '<div class="st-notice-event" data-kind="'+esc(e.type)+'"><b>'+esc(e.name)+' '+esc(e.code)+'｜'+esc((e.provisional?'盤中暫定 · ':'')+(e.type==='entry'?'新'+names[e.type]:names[e.type]))+'</b><span>'+esc(e.date)+' · 訊號價 '+price(e.price)+'</span>'+sizing(e)+'<span>硬停損 '+price(e.stop)+'　停利參考 '+price(e.target)+'</span>'+(e.type==='exit'&&e.spread!=null?'<strong class="'+spreadClass(e.spread)+'">'+spreadText(e.spread)+'</strong>':'')+'</div>';};body+=alerts.slice(0,6).map(renderAlert).join('');if(alerts.length>6)body+='<details><summary>其餘 '+(alerts.length-6)+' 筆新訊號</summary>'+alerts.slice(6).map(renderAlert).join('')+'</details>';body+='<p>'+(meta.intraday?'盤中暫定訊號可能消失，不代表已收盤確認或可按該價格成交。':'下一交易日須重新確認價格與停損距離，不代表可按訊號價直接成交。')+'</p>';}
  var observations=valid?(s.data.observations||[]):[];if(observations.length)body+='<p>今日新增型態觀察 '+observations.length+' 檔 · '+observations.slice(0,3).map(function(r){return esc(r.name);}).join('、')+(observations.length>3?'…':'')+'</p>';
  if(valid&&meta.recent_signals){var recent=meta.recent_signals;body+='<details class="st-recent-signals"><summary>最近30根日K：'+num(recent.entries||0)+' 次進場 · '+num(recent.exits||0)+' 次'+(s.direction==='short'?'回補':'出場')+'</summary><p>範圍：目前篩選的已保存候選，不是全市場績效。策略在場 '+num(recent.active||0)+' 檔；最近進場 '+esc(recent.last_entry_date||'尚無')+'。頂部「今日進場」只計本次資料日。</p></details>';}
  if(valid&&meta.failed)body+='<p>另有 '+num(meta.failed)+' 檔資料待確認。</p>';
  return '<section class="st-notices" aria-label="最新交易訊號" role="status"><h3 aria-label="'+esc(title)+'">'+(!valid?esc(title):'<span><small>觀察 </small><b>'+num(counts.watch||0)+'</b></span><span><small>型態觸發 </small><b>'+num(counts.triggered||0)+'</b></span><span><small>'+(meta.intraday?'盤中進場 ':'今日進場 ')+'</small><b>'+num(counts.entry||entries.length||0)+'</b></span>')+'</h3>'+body+'</section>';
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
  if(r.latest_event)return (r.provisional?'盤中':'今日')+names[r.latest_event]+(r.provisional?'待確認':'');
  var g=group(r),short=r.direction==='short';
  if(g==='held')return (r.last_entry_date||'')+(short?' 放空，尚無加空訊號':' 進場，尚無加碼訊號');
  if(g==='closed')return (r.last_exit_date||'')+(short?' 已回補':' 已出場');
  if(r.signal_stage==='triggered')return '型態已觸發，進場條件尚未全數成立';
  return short?'空方觀察，尚無放空訊號':'多方觀察，尚無進場訊號';
 }
 function compactSpread(r){
  var p=r.spreads||{},open=p.open,last=(p.completed||[]).slice(-1)[0],g=group(r),v=g==='held'&&open?open.spread:g==='closed'&&last?last.spread:null;
  if(v==null)return '';
  return '<div class="st-profit st-compact-profit"><span>'+(g==='held'?'未實現每股價差':'上次每股價差')+'</span><strong class="'+spreadClass(v)+'">'+spreadText(v)+'</strong></div>';
 }
 function statusText(m){return m.building?(m.phase||'更新中')+' '+(m.progress||0)+'／'+(m.progress_total||80):m.error?m.error:(m.intraday?'盤中':'收盤')+' · '+(m.date||'資料待確認')+' · '+(m.generated_at?m.generated_at.slice(11,16)+' 更新':'等待更新');}
 function header(s,target){
  var root=target||s.el,short=s.direction==='short';
  names=short?{entry:'放空',add:'加空',reduce:'回補一半',exit:'全部回補'}:{entry:'進場',add:'加碼',reduce:'減碼',exit:'出場'};
  strategyNames=short?{reversal:'上升線跌破',breakout:'支撐跌破',pullback:'反彈轉弱',continuation:'弱勢續跌'}:{reversal:'轉折',breakout:'突破',pullback:'回測',continuation:'延續',repair:'多方修復'};
  var m=s.data.meta||{},scope=Object.assign({},m.scope||{}),counts=m.position_counts||{},rows=s.data.rows||[];
  if(short){scope.qualified_sectors=scope.short_qualified_sectors||{};var priceCoverage={};Object.keys(scope.price_coverage||{}).forEach(function(k){priceCoverage[k]=Object.assign({},scope.price_coverage[k],{qualified:scope.price_coverage[k].short_qualified||0});});scope.price_coverage=priceCoverage;}
  if(!m.position_counts)Object.keys(groupNames).forEach(function(g){counts[g]=rows.filter(function(r){return group(r)===g;}).length;});
  root.innerHTML='<header class="st-heading"><h2>波段機會</h2><button type="button" class="st-refresh">更新</button></header>'+
   '<div class="st-status" role="status">'+esc(statusText(m))+'</div>'+
   '<div class="st-direction" aria-label="策略方向">'+['long','short'].map(function(d){return '<button type="button" data-direction="'+d+'" aria-pressed="'+((s.direction||'long')===d)+'">'+(d==='short'?'放空機會':'做多機會')+'</button>';}).join('')+'</div>'+notices(s)+'<div class="st-sector" aria-label="短線產業篩選">'+['全部','電子','非電子'].map(function(v){return '<button type="button" data-sector="'+v+'" aria-pressed="'+((s.sector||'全部')===v)+'">'+v+'</button>';}).join('')+'<select class="st-shape-filter" aria-label="型態">'+(short?['','reversal','breakout','pullback','continuation']:['','reversal','breakout','pullback','continuation','repair']).map(function(k){return '<option value="'+k+'" '+((s.shape||'')===k?'selected':'')+'>'+(strategyNames[k]||'全部型態')+'</option>';}).join('')+'</select></div>'+
   '<div class="st-price-tools"><label>股價區間<select class="st-price-filter" aria-label="股價區間">'+[['all','全部股價'],['under100','低於100元'],['100to500','100至未滿500元'],['500to1000','500至未滿1000元'],['1000plus','1000元以上']].map(function(x){return '<option value="'+x[0]+'" '+((s.priceRange||'all')===x[0]?'selected':'')+'>'+x[1]+'</option>';}).join('')+'</select></label><label>排序<select class="st-price-sort" aria-label="排序">'+[['signal','訊號優先'],['price_desc','股價由高到低'],['price_asc','股價由低到高']].map(function(x){return '<option value="'+x[0]+'" '+((s.sort||'signal')===x[0]?'selected':'')+'>'+x[1]+'</option>';}).join('')+'</select></label></div>'+'<div class="st-state-tabs" aria-label="進出場狀態">'+['','held','waiting','closed'].concat(counts.unknown?['unknown']:[]).map(function(g){return '<button type="button" data-position="'+g+'" aria-pressed="'+((s.position||'')===g)+'">'+(g?groupNames[g]:'全部')+(g?' <b>'+num(counts[g]||0)+'</b>':'')+'</button>';}).join('')+'</div><div class="st-list-summary">共 '+num(m.total==null?rows.length:m.total)+' 檔 · '+(s.sort==='price_desc'?'股價由高到低':s.sort==='price_asc'?'股價由低到高':'訊號優先')+'</div><div class="st-cards"></div><nav class="st-pagination" aria-label="候選分頁"><button type="button" data-page="-1" '+((m.offset||0)<=0?'disabled':'')+'>上一頁</button><span>第 '+(Math.floor((m.offset||0)/(m.limit||20))+1)+'／'+Math.max(1,Math.ceil((m.total||rows.length)/(m.limit||20)))+' 頁</span><button type="button" data-page="1" '+(!m.has_more?'disabled':'')+'>下一頁</button></nav>'+
   '<details class="st-data"><summary>更新與掃描範圍</summary><div class="st-refresh-info">'+esc((m.intraday?'獨立排程每 10 分鐘補檢，全市場分批接續':'收盤確認模式')+' · 上次完成 '+(m.generated_at?m.generated_at.slice(0,19).replace('T',' '):'尚未完成'))+'<br>'+esc(m.intraday?'下一輪約 '+(m.next_refresh_at?m.next_refresh_at.slice(11,19):'等待排程')+'；行情可能延遲，量比依開盤時間比例估算。':'盤中暫定訊號與收盤結果分開保存。')+'</div><div class="st-scope">全市場初篩 '+num(scope.universe_count||0)+' 檔 · 已詳查 '+num(scope.deep_done||m.scanned||0)+'／'+num(scope.deep_total||scope.universe_count||m.scanned||0)+' 檔'+(scope.deep_complete?'（本輪已處理完）':'（分批接續）')+'<br>'+esc(Object.keys(scope.sectors||{}).map(function(k){return k+' '+scope.sectors[k]+' 檔';}).join(' · '))+'<br>'+esc(Object.keys(scope.markets||{}).map(function(k){return k+' '+scope.markets[k]+' 檔';}).join(' · '))+'<br>技術詳查：'+esc(Object.keys(scope.deep_sectors||{}).map(function(k){return k+' '+scope.deep_sectors[k]+' 檔';}).join(' · '))+'<br>符合觀察條件：'+esc(Object.keys(scope.qualified_sectors||{}).map(function(k){return k+' '+scope.qualified_sectors[k]+' 檔';}).join(' · '))+'</div><p>行情讀取缺漏 '+num(m.failed||0)+' 檔；缺漏資料不補造訊號。</p><div class="st-price-audit"><h4>價格涵蓋範圍</h4><p>不設股價上限，不按法人買超張數排序。</p><table><thead><tr><th>股價</th><th>取得行情</th><th>技術初篩</th><th>詳查</th><th>符合觀察</th></tr></thead><tbody>'+['低於100','100–499','500–999','1000以上'].map(function(k){var c=(scope.price_coverage||{})[k]||{};return '<tr><th>'+k+'</th><td>'+num(c.quoted||0)+'</td><td>'+num(c.technical||0)+'</td><td>'+num(c.deep||0)+'</td><td>'+num(c.qualified||0)+'</td></tr>';}).join('')+'</tbody></table><details><summary>高價股逐檔檢查</summary>'+((scope.high_price_examples||[]).map(function(r){return '<p>'+esc(r.name)+' '+esc(r.code)+' · '+price(r.price)+'<br>'+esc((r.deep?'完整日K '+r.bars+' 根 · '+(r.date||'')+' · ':'')+r.reason)+'</p>';}).join('')||'<p>本輪診斷尚未完成。</p>')+'</details></div></details>'+
   '<details class="st-rules"><summary>訊號規則</summary><div><p>技術面為主，法人只供參考。成交量、成交金額不設最低門檻；不設股價上限，不按法人買超張數排序。每頁20檔，可翻頁查看全部候選，依型態與趨勢排列；未詳查的股票不宣稱已完成。</p><p>進場：轉折、突破、回測、延續其中一類型態成立，再檢查量價與風險空間。下降線／底部轉折須連續兩日收在同一突破線加緩衝之上，且兩日均站回20日線；不用同時滿足全部型態，也不要求法人買超。部位比例以首次進場股數為100%，不是總資產。一般加碼加首次股數的50%，最多一次，部位由100%變150%；須獲利達1R、再次放量突破且合併風險受控。減碼賣出當時股數的一半：100%剩50%，加碼至150%後則剩75%。連續兩日收盤跌破事前趨勢支撐與20日線，且20日線走平或下彎，才確認反轉出清；觸及硬停損優先退出。收復支撐、再次突破且風險受控時，回補前次減掉的股數，最多回補一次。例：首次1000股→加500股→減750股→回補750股；出場賣出剩餘全部。實際股數依你的首次進場股數換算；法人不影響買賣訊號。</p><p>盤中暫定訊號可能消失，量比依時間比例估算；收盤後重算。單日漲跌超過9.5%先等下一交易日確認；ATR影響風險距離，不單獨否決進場。結構停損距離上限依ATR介於6%至12%；初期轉折須至少1.5R，已確認趨勢的突破、回測與延續則依結構停損追蹤，第一道壓力是減碼關卡，並非整段趨勢的終點。法人同買僅指外資＋投信前五個已完成交易日淨買合計為正。場內是策略重播狀態，不是你的實際帳戶持股；這個分頁不下單。</p></div></details>';
  root.querySelectorAll('[data-direction]').forEach(function(b){b.onclick=function(){if(s.direction===b.dataset.direction)return;s.direction=b.dataset.direction;s.data={rows:[],alerts:[],meta:{building:true,direction:s.direction}};s.offset=0;s.open=null;s.shape='';s.position='';s.rowsSignature=null;s.viewChanged=true;render(s,true);if(s.loading)s.pending=true;else load(s,false);};});
  root.querySelector('.st-rules div').textContent=(short?
   '放空型態分開檢查：上升線跌破、20日支撐跌破、10日或20日線反彈轉弱、弱勢整理再破低。明確跌破可用單日收盤超過0.5ATR且量比至少1.2確認；一般跌破用連續兩個有成交的日K確認。波段趨勢型態以結構停損追蹤，第一道支撐是回補觀察點，不是固定行情終點；初期轉折仍須1.5R空間。加空須已有1R獲利、再破低且初始風險預算受控，最多加首次股數50%；轉強回補當時空單50%，反轉或硬停損回補全部。':
   '進場型態分開檢查：底部／下降線轉折、突破、回測轉強、強勢整理延續。突破可用放量收盤，或連兩個有成交的日K站穩同一壓力與20日線；回測須收復前日高點。轉折初期須1.5R空間；已確認的波段趨勢以結構停損追蹤，第一道壓力是減碼觀察點，不是固定行情終點。多方修復僅觀察，不能單獨產生買點。加碼及減碼沿用首次股數50%、當時部位50%與初始風險預算限制。')+
   ' 不設股價上限、不以成交張數或法人買賣超作門檻。ATR是波動資料，不單獨禁止交易；結構停損距離仍須在ATR計算的6%至12%上限內。單日漲跌逾9.5%不新增部位，等待下一交易日重新確認可交易性。只讀取當時已完成資料；盤中訊號為暫定。零成交日K不計入確認與均線。訊號及價差是規則重播，非券商成交或含費績效；放空資格與券源未核實。不會下單。';
  root.querySelector('.st-shape-filter').onchange=function(e){s.shape=e.target.value;s.position='';s.offset=0;s.open=null;s.viewChanged=true;if(s.loading)s.pending=true;else load(s,false);};
  function priceChoice(e,key){s[key]=e.target.value;s.offset=0;s.open=null;s.viewChanged=true;try{localStorage.setItem('st-price-view',JSON.stringify({priceRange:s.priceRange,sort:s.sort}));}catch(ignore){}if(s.loading)s.pending=true;else load(s,false);}
  root.querySelector('.st-price-filter').onchange=function(e){priceChoice(e,'priceRange');};
  root.querySelector('.st-price-sort').onchange=function(e){priceChoice(e,'sort');};
  root.querySelector('.st-refresh').onclick=function(){load(s,true);};
  root.querySelectorAll('[data-sector]').forEach(function(b){b.onclick=function(){s.sector=b.dataset.sector==='全部'?'':b.dataset.sector;s.position='';s.offset=0;s.open=null;s.viewChanged=true;if(s.loading)s.pending=true;else load(s,false);};});
  root.querySelectorAll('.st-state-tabs button[data-position]').forEach(function(b){b.onclick=function(){s.position=b.dataset.position;s.offset=0;s.open=null;s.viewChanged=true;if(s.loading)s.pending=true;else load(s,false);};});
  root.querySelectorAll('[data-page]').forEach(function(b){b.onclick=function(){s.offset=Math.max(0,(s.offset||0)+Number(b.dataset.page)*20);s.pageJump=true;s.open=null;s.viewChanged=true;if(s.loading)s.pending=true;else load(s,false);};});
  if(!target&&!s.el.hidden){var status=document.getElementById('wb-status');if(status)status.textContent=m.building?'短線更新中':'短線';}
 }
 function render(s,interactive){
  if(!alive(s))return;
  var rows=s.data.rows||[],signature=rows.map(function(r){var copy=Object.assign({},r);delete copy.computed_at;return JSON.stringify(copy);}).join('|');
  var view=[s.priceRange,s.sort,s.direction,s.sector,s.shape,s.position,s.offset,!!((s.data.meta||{}).position_counts||{}).unknown,!rows.length&&!!(s.data.meta||{}).building].join('|');
  if(!interactive&&!s.viewChanged&&(s.pointerActive||Date.now()-(s.lastScroll||0)<180)){s.renderPending=true;clearTimeout(s.renderTimer);s.renderTimer=setTimeout(function(){render(s);},200);return;}
  if(!s.viewChanged&&s.rowsSignature===signature&&s.renderedView===view&&s.el.querySelector('.st-cards')){patchMeta(s);return;}
  var existing=new Map();s.el.querySelectorAll('.st-card').forEach(function(card){existing.set(card.dataset.code,card);});
  var focus=document.activeElement,focusCode=focus&&focus.closest('.st-card'),focusExpand=focus&&focus.matches('.st-expand');
  var anchor=s.open&&s.el.querySelector('.st-card[data-code="'+s.open+'"]'),top=anchor&&anchor.getBoundingClientRect().top;
  header(s);var host=s.el.querySelector('.st-cards');s.rowsSignature=signature;s.renderedView=view;s.renderPending=false;
  if(!rows.length){
   var meta=s.data.meta||{};
   host.innerHTML=meta.building?'<div class="st-skeleton" aria-label="正在篩選候選"></div>':
    !meta.date?'<div class="st-empty"><b>暫無可顯示的短線快照</b></div>':
    '<div class="st-empty"><b>'+(s.position==='held'?'目前沒有場內股票':s.position==='closed'?'目前沒有已出場股票':'目前沒有符合條件的股票')+'</b><p>可切換其他狀態查看。</p></div>';
   return;
  }
  var previous=null;
  host.innerHTML=rows.map(function(r){
   var g=group(r),heading='';
   var es=(r.signals||[]).filter(function(e){return e.type==='entry'&&!e.provisional;}),entry=g==='held'?(r.active?r.entry:(es.slice(-1)[0]||{}).price):r.entry;
   var preview=(r.signals||[]).filter(function(e){return e.provisional;}).slice(-1)[0],stop=preview&&g==='held'?preview.stop:r.stop,target=preview&&g==='held'?preview.target:r.target;
   return heading+'<article class="st-card" data-code="'+esc(r.code)+'" data-position="'+g+'"><button type="button" class="st-card-toggle" aria-expanded="'+(s.open===r.code)+'" aria-controls="st-detail-'+esc(r.code)+'"><div class="st-stock"><span class="st-state" data-position="'+g+'">'+esc(r.latest_event?((r.provisional?'暫定 · ':'')+names[r.latest_event]):g==='waiting'?(r.direction==='short'?'空方觀察':'多方觀察'):g==='held'?(r.direction==='short'?'空單在場':'多單在場'):g==='closed'?(r.direction==='short'?'已回補':'已出場'):groupNames[g])+'</span><h3>'+esc(r.name)+' <small>'+esc(r.code)+'</small></h3>'+ (r.strategy?'<span class="st-pattern-label">'+esc(r.pattern_label||strategyNames[s.shape||r.strategy]||'觀察')+'</span>':'')+(r.institutional_buying?'<span class="st-chip-badge" title="外資＋投信前五個已完成交易日淨買合計為正；僅供參考">法人同買</span>':'')+(r.direction==='short'&&r.institutional_selling?'<span class="st-chip-badge">法人同賣（參考）</span>':'')+'<span class="st-date">'+esc(concise(r))+'</span></div>'+cardQuote(r)+'</button>'+
   (r.provisional&&r.latest_event?'<div class="st-live-action">'+esc('盤中'+names[r.latest_event]+'待確認')+'</div>':'')+
   (g==='held'||(g==='waiting'&&r.entry_ready)?'<div class="st-levels"><div><span>'+(g==='held'?'進場價':'觀察價')+'</span><b>'+price(entry)+'</b></div><div><span>'+(g==='held'?'硬停損':'硬停損參考')+'</span><b class="st-down">'+price(stop)+'</b></div><div><span>'+(g==='held'?'停利':'停利參考')+'</span><b>'+price(target)+'</b></div></div>':g==='waiting'?'<p class="st-wait-reason">'+esc((r.pending||[]).slice(0,2).join(' · ')||'等待進場確認')+'</p>':'')+
   currentSizing(r)+compactSpread(r)+'<button type="button" class="st-expand" data-expand="'+esc(r.code)+'">'+(s.open===r.code?'收合 −':'K 線與進出場理由 ＋')+'</button><section class="st-detail" id="st-detail-'+esc(r.code)+'" '+(s.open===r.code?'':'hidden')+'></section></article>';
  }).join('');
  var reusedOpen=false;
  host.querySelectorAll('.st-card').forEach(function(card){var r=rows.find(function(r){return r.code===card.dataset.code;}),copy=Object.assign({},r);delete copy.computed_at;var key=JSON.stringify(copy),old=existing.get(card.dataset.code),expanded=s.open===card.dataset.code;if(old&&old._stKey===key&&old.querySelector('.st-card-toggle').getAttribute('aria-expanded')===String(expanded)){card.replaceWith(old);if(expanded)reusedOpen=true;}else card._stKey=key;});
  function toggle(code){s.open=s.open===code?null:code;s.history=false;s.event=null;s.end=null;s.readDate=null;s.rowsSignature=null;render(s,true);}
  host.querySelectorAll('.st-card-toggle').forEach(function(b){b.onclick=function(){toggle(b.closest('.st-card').dataset.code);};});
  host.querySelectorAll('[data-expand]').forEach(function(b){b.onclick=function(){toggle(b.dataset.expand);};});
  if(s.open&&!reusedOpen){var row=rows.find(function(r){return r.code===s.open;});if(row)detail(s,row);}
  prepareReveal(s);
  if(focusCode){var replacement=host.querySelector('.st-card[data-code="'+focusCode.dataset.code+'"] '+(focusExpand?'.st-expand':'.st-card-toggle'));if(replacement)replacement.focus({preventScroll:true});}
  var next=s.open&&host.querySelector('.st-card[data-code="'+s.open+'"]');if(next&&top!=null&&!s.pageJump){var delta=next.getBoundingClientRect().top-top;if(Math.abs(delta)>1)window.scrollBy(0,delta);}
  if(s.pageJump){s.pageJump=false;s.el.querySelector('.st-list-summary').scrollIntoView({block:'start'});}
 }
 function patchMeta(s){
  var scratch=document.createElement('div');header(s,scratch);
  ['.st-status','.st-notices','.st-list-summary','.st-pagination','.st-data','.st-rules'].forEach(function(selector){var old=s.el.querySelector(selector),next=scratch.querySelector(selector);if(!old||!next||old.outerHTML===next.outerHTML||old.contains(document.activeElement))return;if(old.tagName==='DETAILS')next.open=old.open;old.replaceWith(next);});
  s.el.querySelectorAll('.st-state-tabs button[data-position]').forEach(function(button){var b=button.querySelector('b'),next=scratch.querySelector('.st-state-tabs button[data-position="'+button.dataset.position+'"] b');if(b&&next)b.textContent=next.textContent;});
 }
 var motion=matchMedia('(prefers-reduced-motion: reduce)'),revealNodes=new Set(),revealFrame=0;
 function finishCard(card){card.classList.remove('st-reveal');card.style.removeProperty('--st-reveal');revealNodes.delete(card);if(current)current.seen.add(card.dataset.code);}
 function paintReveals(){revealFrame=0;var updates=[];revealNodes.forEach(function(card){if(!card.isConnected){revealNodes.delete(card);return;}var p=Math.max(0,Math.min(1,(innerHeight-100-card.getBoundingClientRect().top)/160));updates.push({card:card,progress:p});});updates.forEach(function(u){if(motion.matches||u.progress>=1)finishCard(u.card);else u.card.style.setProperty('--st-reveal',String(.28+.72*u.progress));});}
 function queueReveal(){if(!revealFrame)revealFrame=requestAnimationFrame(paintReveals);}
 function prepareReveal(s){if(motion.matches)return;s.el.querySelectorAll('.st-card').forEach(function(card){if(s.seen.has(card.dataset.code))return;if(card.getBoundingClientRect().top<innerHeight-260){s.seen.add(card.dataset.code);return;}card.classList.add('st-reveal');revealNodes.add(card);});queueReveal();}
 window.addEventListener('scroll',function(){if(current)current.lastScroll=Date.now();queueReveal();},{passive:true});
 window.addEventListener('resize',queueReveal,{passive:true});
 motion.addEventListener('change',function(){if(motion.matches)Array.from(revealNodes).forEach(finishCard);});
 document.addEventListener('pointerdown',function(e){if(!current||!current.el.contains(e.target))return;current.pointerActive=true;var card=e.target.closest('.st-card');if(card)finishCard(card);},{capture:true,passive:true});
 ['pointerup','pointercancel'].forEach(function(type){document.addEventListener(type,function(){if(!current)return;current.pointerActive=false;if(current.renderPending){clearTimeout(current.renderTimer);current.renderTimer=setTimeout(function(){if(current)render(current);},200);}},{passive:true});});
 document.addEventListener('focusin',function(e){var card=e.target.closest('.st-card');if(card)finishCard(card);});
 function timeline(r){
  var events=(r.signals||[]).slice().reverse();
  return '<section class="st-history" aria-label="訊號時間軸"><h4>訊號時間軸（'+events.length+'）</h4><p class="st-history-note">依日期由新到舊；歷史規則重播，不是當下的新買點。</p><ol>'+events.map(function(e){return '<li><button type="button" data-event="'+esc(e.id)+'"><span>'+esc(e.date)+'</span><b class="'+(['entry','add'].indexOf(e.type)>=0?'st-up':'st-down')+'">'+esc(names[e.type])+'</b><strong>'+price(e.price)+'</strong></button><span class="st-event-status">'+(e.provisional?'盤中暫定 · 未收棒':'歷史收盤規則重播')+'</span>'+sizing(e)+'<p class="st-event-reason"><b>理由：</b>'+esc((e.reason||'理由未保存').split('；')[0])+'</p><details><summary>查看當時數值與完整理由</summary><p>'+esc(e.reason||'此筆舊紀錄沒有保存理由，不補造原因。')+'</p></details><small>當時硬停損 '+price(e.stop)+' · 停利參考 '+price(e.target)+'</small></li>';}).join('')+'</ol>'+(events.length?'':'<p>這段資料尚無進出場紀錄，不補畫訊號。</p>')+'</section>';
 }
 function detail(s,r){
  var el=s.el.querySelector('#st-detail-'+r.code),events=r.signals||[],chip=r.chip||{};
  el.innerHTML='<div class="st-chart-head"><div><b>K 線與策略訊號</b><span>'+(r.provisional?'盤中日線 · 暫定':'日線 · 收盤確認')+' · '+esc((r.market||'')+' '+(r.sector||''))+'</span></div><div class="st-periods" aria-label="圖表期間">'+[30,60,120].map(function(n){return '<button type="button" data-range="'+n+'" aria-pressed="'+(s.range===n)+'">'+n+' 日</button>';}).join('')+'</div></div>'+
   '<div class="st-chart-mode"><span class="st-chart-context"></span><button type="button" class="st-history-toggle" aria-pressed="false">查看歷史訊號</button></div>'+
   '<div class="st-chart-readout" aria-live="polite">左右滑動查看日期與價格</div><div class="st-chart" role="group" aria-label="'+esc(r.name)+'日 K 與進出場訊號"></div>'+
   '<div class="st-chart-nav"><button type="button" data-step="-1" aria-label="查看較早K線">← 較早</button><span>'+(r.direction==='short'?'▼ 放空／加空　▲ 回補':'▲ 進場／加碼　▼ 減碼／出場')+'</span><button type="button" data-step="1" aria-label="查看較新K線">較新 →</button></div>'+
   '<div class="st-inspector" aria-live="polite"></div><div class="st-checks">'+(r.checks||[]).map(function(c){return '<span data-passed="'+!!c.passed+'">'+(c.passed?'✓ ':'— ')+esc(c.label)+(c.passed?'成立':'待確認')+'</span>';}).join('')+'</div><div class="st-evidence"><span>'+(r.volume_estimated?'時間比例估算量比 ':'量比 ')+num(r.volume_ratio)+'</span>'+(r.institutional_buying?'<span>法人同買（前五個已完成交易日）</span>':'')+'</div>'+timeline(r)+'<div class="st-chip-detail"><h4>技術依據與法人參考</h4><dl><div><dt>近 20 日平均成交額（非門檻）</dt><dd>'+num(r.avg_turnover/10000)+' 萬元</dd></div><div><dt>近 20 日平均成交量（非門檻）</dt><dd>'+num(r.avg_volume/1000)+' 張</dd></div><div><dt>支撐參考</dt><dd>'+price(r.support)+'</dd></div><div><dt>上方歷史壓力</dt><dd>'+(r.resistance?price(r.resistance):'區間內無已知壓力')+'</dd></div><div><dt>外資近五日</dt><dd>'+num(chip.foreign)+' 張／'+num(chip.foreign_days)+' 日買超</dd></div><div><dt>投信近五日</dt><dd>'+num(chip.trust)+' 張／'+num(chip.trust_days)+' 日買超</dd></div><div><dt>法人淨買占五日成交量（參考）</dt><dd>'+num(r.chip_ratio)+'%</dd></div><div><dt>籌碼資料截至</dt><dd>'+esc(chip.date||'缺資料')+'</dd></div></dl></div>'+
   '<p class="st-source">行情：'+esc(r.source)+(r.observed_at?' · 來源時間 '+esc(r.observed_at.slice(0,19).replace('T',' ')):'')+''+(r.direction==='short'?' · '+esc(r.short_eligibility||'未核實放空資格及券源')+'；價差未扣借券費、利息與交易成本。':' · 趨勢支撐跌破先警戒／減碼；硬停損仍可能直接出場。')+'</p>';
  if((r.family_checks||[]).length){var evidence=document.createElement('details');evidence.className='st-family-evidence';evidence.innerHTML='<summary>逐一型態檢查</summary><div class="st-family-scroll"><table><thead><tr><th>型態</th><th>確認</th><th>停損距離</th><th>首道關卡R</th><th>判斷</th></tr></thead><tbody>'+r.family_checks.map(function(f){return '<tr><th>'+esc(f.pattern||strategyNames[f.strategy])+'</th><td>'+esc(f.confirmation||(f.confirmed?'收盤確認':'待確認'))+'</td><td>'+num(f.risk_pct)+'%</td><td>'+num(f.rr)+'R</td><td>'+esc(f.entry?'符合進場':f.trigger?'風險／確認未齊':'型態未觸發')+'<small>'+esc(f.risk_policy==='trend_following'?'波段追蹤':'轉折空間')+'</small></td></tr>';}).join('')+'</tbody></table></div>';el.querySelector('.st-inspector').after(evidence);}
  el.querySelector('.st-history-toggle').onclick=function(){s.history=!s.history;s.event=null;s.end=null;inspect(s,r,el);draw(s,r,el);};
  el.querySelectorAll('[data-range]').forEach(function(b){b.onclick=function(){s.range=Number(b.dataset.range);s.end=null;detail(s,r);};});
  el.querySelectorAll('[data-step]').forEach(function(b){b.onclick=function(){s.end=Math.max(Math.min(s.range,r.bars.length),Math.min(r.bars.length,(s.end||r.bars.length)+Number(b.dataset.step)*Math.max(5,Math.floor(s.range/2))));draw(s,r,el);};});
  el.querySelectorAll('[data-event]').forEach(function(b){b.onclick=function(){select(s,r,b.dataset.event,el);};});
  inspect(s,r,el);draw(s,r,el);if(!motion.matches){el.classList.add('st-detail-arrive');setTimeout(function(){el.classList.remove('st-detail-arrive');},220);}
 }
 function inspect(s,r,el){
  var e=(r.signals||[]).find(function(x){return x.id===s.event;}),box=el.querySelector('.st-inspector');
  box.innerHTML=e?'<span class="st-inspector-label">'+esc(e.date)+' · 歷史規則重播</span><h4 style="color:'+colors[e.type]+'">'+names[e.type]+'訊號 '+price(e.price)+'</h4>'+sizing(e)+'<p>'+esc(e.reason)+'</p><div><span>當時硬停損價 <b>'+price(e.stop)+'</b></span><span>當時停利參考 <b>'+price(e.target)+'</b></span></div><button type="button" class="st-current">回到目前計畫</button>':'<span class="st-inspector-label">目前狀態</span><h4>'+esc(r.state)+'</h4>'+currentSizing(r)+'<p>'+esc(r.action_note||'尚未確認新的進場訊號。')+'</p><div>'+(r.pattern_line?'<span>'+(r.direction==='short'?'上升趨勢線':'下降壓力線')+' <b>'+price(r.pattern_line.end_price)+'</b></span>':'')+'<span>'+(r.direction==='short'?'趨勢壓力':'趨勢支撐')+' <b>'+price(r.soft_support)+'</b></span>'+(r.observation_price?'<span>'+(r.direction==='short'?'反彈觀察價（非委託）':'轉強觀察價（非委託）')+' <b>'+price(r.observation_price)+'</b></span>':'')+'<span>硬停損價 <b>'+price(r.stop)+'</b></span><span>停利參考 · '+esc(r.target_kind||'')+' <b>'+price(r.target)+'</b></span></div>';
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
  (r.signals||[]).filter(function(e){return (s.history||(r.current_action!=='unconfirmed'&&e.date===r.date))&&e.date>=bars[0].date&&e.date<=bars[n-1].date;}).slice(-12).forEach(function(e){var i=bars.findIndex(function(b){return b.date===e.date;}),up=(e.type==='entry'||e.type==='add')!==(r.direction==='short'),xx=x(i),yy=y(up?bars[i].low:bars[i].high)+(up?14:-14),path=up?'M-7,6L0,-6L7,6Z':'M-7,-6L0,6L7,-6Z';svg+='<g transform="translate('+xx+','+yy+')"><g data-marker="'+esc(e.id)+'" tabindex="0" role="button" aria-label="'+esc(e.date+' '+names[e.type]+' '+price(e.price))+'"><rect x="-22" y="-22" width="44" height="44" fill="transparent"/><path d="'+path+'" fill="'+colors[e.type]+'"/><text y="'+(up?24:-12)+'" text-anchor="middle" font-size="12" fill="'+colors[e.type]+'">'+names[e.type]+'</text></g></g>';});
  [0,Math.floor(n/2),n-1].forEach(function(i){svg+='<text class="st-axis" x="'+x(i)+'" y="328" text-anchor="'+(i===0?'start':i===n-1?'end':'middle')+'">'+esc(bars[i].date.slice(5))+'</text>';});
  svg+='<line class="st-cross" x1="0" x2="0" y1="'+T+'" y2="'+V+'" visibility="hidden"/><text class="st-axis" x="'+L+'" y="18">MA10 / MA20　- - '+(r.direction==='short'?'上升趨勢線':'下降趨勢線')+'</text></svg>';
  host.innerHTML=svg;var chart=host.querySelector('svg'),cross=host.querySelector('.st-cross');
  function read(e){var rect=chart.getBoundingClientRect(),px=(e.clientX-rect.left)/rect.width*W,i=Math.max(0,Math.min(n-1,Math.floor((px-L)/step))),b=bars[i];cross.setAttribute('x1',x(i));cross.setAttribute('x2',x(i));cross.setAttribute('visibility','visible');s.readDate=b.date;el.querySelector('.st-chart-readout').textContent=b.date+'　開 '+price(b.open)+'　高 '+price(b.high)+'　低 '+price(b.low)+'　收 '+price(b.close);}
  chart.setAttribute('tabindex','0');chart.addEventListener('keydown',function(e){if(e.key!=='ArrowLeft'&&e.key!=='ArrowRight')return;e.preventDefault();var i=bars.findIndex(function(b){return b.date===s.readDate;});i=Math.max(0,Math.min(n-1,(i<0?n-1:i)+(e.key==='ArrowLeft'?-1:1)));var rect=chart.getBoundingClientRect();read({clientX:rect.left+x(i)/W*rect.width});});
  if(s.readDate){var keep=bars.findIndex(function(b){return b.date===s.readDate;});if(keep>=0){var rect=chart.getBoundingClientRect();read({clientX:rect.left+x(keep)/W*rect.width});}}
  chart.addEventListener('pointerdown',read);chart.addEventListener('pointermove',read);
  chart.querySelectorAll('[data-marker]').forEach(function(g){g.onclick=function(){select(s,r,g.dataset.marker,el);};g.onkeydown=function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();select(s,r,g.dataset.marker,el);}};});
  el.querySelector('[data-step="-1"]').disabled=start===0;el.querySelector('[data-step="1"]').disabled=end===all.length;
 }
 function load(s,force){
  if(!alive(s))return;if(s.loading){if(force){s.pending=true;s.pendingForce=true;}return;}
  clearTimeout(s.timer);s.loading=true;var viewAtRequest=viewKey(s),id=++s.request,controller=new AbortController(),timeout=setTimeout(function(){controller.abort();},12000);
  var btn=s.el.querySelector('.st-refresh');if(btn){btn.disabled=true;btn.textContent='讀取中…';}
  fetch(api(force),{credentials:'same-origin',cache:'no-store',signal:controller.signal})
   .then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();})
   .then(function(data){
    if(!data.ok)throw new Error(data.error||'載入失敗');
    if(!alive(s)||id!==s.request)return;
    if(viewAtRequest!==viewKey(s)){s.pending=true;return;}
    if(data.meta&&data.meta.offset!=null)s.offset=data.meta.offset;
    if(!data.meta?.date) data=previousSnapshot(s,'顯示上次保存的結果，等待可用快照')||data;
    var changed=s.viewChanged||JSON.stringify(s.data)!==JSON.stringify(data);
    s.data=data;s.fetched=Date.now();
    if(data.meta?.date&&!data.meta.previous_snapshot){
     s.snapshot={view:viewKey(s),data:data};
     try{sessionStorage.setItem('st-last-results-'+s.direction,JSON.stringify(s.snapshot));}catch(ignore){}
    }
    if(changed)render(s,s.focusLiveActions);
    announceActions(s,data);
    if(s.focusLiveActions){
     s.focusLiveActions=false;var actions=s.el.querySelector('.st-notices');
     if(actions){actions.tabIndex=-1;actions.focus({preventScroll:true});actions.scrollIntoView({block:'start'});}
    }
    s.viewChanged=false;
    s.timer=setTimeout(function(){s.timer=null;if(!s.el.hidden&&!document.hidden)load(s,false);},data.meta&&data.meta.building?5000:30000);
   })
   .catch(function(e){
    if(!alive(s))return;
    if(viewAtRequest!==viewKey(s)){s.pending=true;return;}
    var reason=e.name==='AbortError'?'讀取逾時':'暫時無法讀取';
    s.data=previousSnapshot(s,reason+'，顯示上次保存的結果')||
     {rows:[],meta:{building:false,error:reason+'，稍後會自動重試'}};
    render(s);
    s.timer=setTimeout(function(){s.timer=null;if(!s.el.hidden&&!document.hidden)load(s,false);},15000);
   })
   .finally(function(){
    clearTimeout(timeout);s.loading=false;
    if(s.pending&&alive(s)){s.pending=false;var force=s.pendingForce;s.pendingForce=false;load(s,force);return;}
    if(alive(s)){var b=s.el.querySelector('.st-refresh');if(b){b.disabled=false;b.textContent='更新';}}
   });
 }
 window.StockBotShortTerm={mount:function(el){
  document.dispatchEvent(new Event('stockbot:shorttermactive'));
  if(current&&current.el===el){if(!current.loading&&!current.timer&&((current.data.meta||{}).building||Date.now()-(current.fetched||0)>30000))load(current,false);return;}
  current={el:el,data:{rows:[],meta:{building:true}},open:null,offset:0,direction:'long',priceRange:'all',sort:'signal',sector:'',shape:'',position:'',pending:false,history:false,range:60,end:null,event:null,request:0,loading:false,polls:0,timer:null,id:++sequence,seen:new Set()};
  try{var saved=JSON.parse(localStorage.getItem('st-price-view')||'{}');if(['all','under100','100to500','500to1000','1000plus'].includes(saved.priceRange))current.priceRange=saved.priceRange;if(['signal','price_desc','price_asc'].includes(saved.sort))current.sort=saved.sort;}catch(ignore){}
  current.data=previousSnapshot(current,'顯示上次保存的結果，正在確認最新資料')||current.data;
  render(current);load(current,false);
 },refresh:function(el){this.mount(el);current.polls=0;load(current,true);}};
 document.addEventListener('stockbot:pageleaving',dismissActionBanner);
 document.addEventListener('visibilitychange',function(){if(document.hidden)dismissActionBanner();else if(current&&!current.el.hidden&&!current.loading)load(current,false);});
 var panel=document.getElementById('wb-short-term');if(panel&&!panel.hidden)window.StockBotShortTerm.mount(panel);
})();
