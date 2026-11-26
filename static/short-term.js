(function(){
 'use strict';
 var current=null, sequence=0;
 var names={entry:'進場',add:'加碼',reduce:'減碼',exit:'出場'};
 var strategyNames={reversal:'轉折',breakout:'突破',pullback:'回測',continuation:'延續',repair:'多方修復'};
 var colors={entry:'#c5343b',add:'#a75916',reduce:'#94651b',exit:'#13805f'};
 function esc(v){return String(v==null?'—':v).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
 function num(v){return v==null||!Number.isFinite(Number(v))?'—':Number(v).toLocaleString('zh-TW',{maximumFractionDigits:2});}
 function price(v){return v==null||!Number.isFinite(Number(v))?'—':Number(v).toLocaleString('zh-TW',{minimumFractionDigits:2,maximumFractionDigits:2});}
 var taipeiTime=new Intl.DateTimeFormat('zh-TW',{timeZone:'Asia/Taipei',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
 function signalTime(value){
  var timestamp=Date.parse(value);
  return Number.isFinite(timestamp)?taipeiTime.format(timestamp):'未記錄';
 }
 function notificationDetails(event){
  var signal=Number(event.price),latest=Number(event.latest_price),delta=(latest/signal-1)*100;
  var movement=signal>0&&latest>0&&Number.isFinite(delta)
   ? ' → 最近掃描 '+price(latest)+'（'+(delta>0?'+':'')+num(delta)+'%）' : '';
  return '<span>首次訊號價 '+price(event.price)+esc(movement)+'</span>'+
   '<span>訊號行情 '+esc(signalTime(event.signal_quote_at))+' · 首次偵測 '+esc(signalTime(event.observed_at))+'</span>'+
   (movement&&event.latest_quote_at?'<span>最近掃描行情 '+esc(signalTime(event.latest_quote_at))+'</span>':'');
 }
 function sizing(e){var a=e&&e.allocation;if(!a)return '';return '<span class="st-sizing"><strong>'+esc(a.label)+'</strong><span>部位 '+num(a.before_pct)+'% → '+num(a.after_pct)+'%（首次股數為100%）</span></span>';}
 function currentSizing(r){if(r.current_action==='unconfirmed'||!r.latest_event)return '';return sizing((r.signals||[]).filter(function(e){return e.date===r.date&&e.type===r.latest_event;}).slice(-1)[0]);}
 function spreadText(v){if(v==null||!Number.isFinite(Number(v)))return '價差未確認';return Number(v)>0?'每股賺 NT$'+price(v):Number(v)<0?'每股賠 NT$'+price(-v):'每股價差 NT$0.00';}
 function spreadClass(v){return Number(v)>0?'st-up':Number(v)<0?'st-down':'';}
 function spreadView(r){var p=r.spreads||{},trades=p.completed||[],last=trades[trades.length-1],out='';if(last){out+='<div class="st-profit"><span>'+(r.provisional&&last.exit_date===r.date?'含盤中暫定出場的每股價差':'已出場每股價差')+(trades.length>1?'（'+trades.length+' 次合計）':'')+'</span><strong class="'+spreadClass(p.total_closed_spread)+'">'+spreadText(p.total_closed_spread)+'</strong><small>上次 '+esc(last.entry_date)+' '+price(last.entry_price)+' → '+esc(last.exit_date)+' '+price(last.exit_price)+(trades.length>1?' · '+spreadText(last.spread):'')+'</small></div>';}if(p.open&&p.open.spread!=null){var o=p.open;out+='<div class="st-profit"><span>尚未出場 · 未實現每股價差</span><strong class="'+spreadClass(o.spread)+'">'+spreadText(o.spread)+'</strong><small>'+esc(o.entry_date)+' '+price(o.entry_price)+' → '+esc(o.date)+' '+price(o.price)+'</small></div>';}return out;}
 var PAGE_SIZE=40;
 function endpoint(values){
  var query=new URLSearchParams(values),token=new URLSearchParams(location.search).get('t');
  if(token)query.set('t',token);
  return '/web/api/workbench/short-term?'+query;
 }
 function api(refresh){
  return endpoint({view:'summary',refresh:refresh?'1':'0',price_range:current.priceRange,sort:current.sort,
   direction:current.direction,sector:current.sector,position:current.position,shape:current.shape,
   offset:current.offset,limit:PAGE_SIZE});
 }
 function alive(s){return current===s&&s.el.isConnected;}
 function viewKey(s){return [s.priceRange,s.sort,s.direction,s.sector,s.position,s.shape,s.offset].join('|');}
 function previousSnapshot(s,message){
  var cached=s.snapshot;
  if(!cached||cached.view!==viewKey(s)){
   try{cached=JSON.parse(sessionStorage.getItem('st-dashboard-results-'+s.direction)||'null');}catch(ignore){}
  }
  if(!cached||cached.view!==viewKey(s)||!cached.data?.meta?.date)return null;
  var data=Object.assign({},cached.data);
  data.meta=Object.assign({},data.meta,{building:false,signals_current:false,previous_snapshot:true,error:message,position_counts:{unknown:data.meta.total}});
  data.rows=data.rows.map(function(row){return Object.assign({},row,{current_action:'unconfirmed',position_group:'unknown',trade:null,dashboard_signal:{stage:'stale'}});});
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
   (fresh.length>1?' · 另 '+(fresh.length-1)+' 筆':'')+'</span>'+notificationDetails(event)+'</span></button>'+
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
  var direction=meta.direction||s.direction, liveByAction=new Map((s.data.notifications||[]).filter(function(event){
   return event.direction===direction;
  }).map(function(event){return [event.code+'|'+event.type,event];}));
  var counts=meta.stage_counts||{watch:(s.data.rows||[]).filter(function(r){return !r.entry_ready;}).length,triggered:0,entry:entries.length};
  var title=!valid?'最新訊號尚未確認':'觀察 '+(counts.watch||0)+' · 型態觸發 '+(counts.triggered||0)+' · '+(meta.intraday?'盤中進場 ':'今日進場 ')+(counts.entry||entries.length||0);
  var body=!valid?'<p>'+esc(meta.building?'正在更新；舊紀錄不作為新的進場提示。':meta.date?'資料截至 '+date+'，尚未確認 '+(meta.expected_date||'最新交易日')+' 的訊號。':'尚未取得可顯示的短線快照。')+'</p>':'';
  if(valid&&alerts.length){var renderAlert=function(e){return '<div class="st-notice-event" data-kind="'+esc(e.type)+'"><b>'+esc(e.name)+' '+esc(e.code)+'｜'+esc((e.provisional?'盤中暫定 · ':'')+(e.type==='entry'?'新'+names[e.type]:names[e.type]))+'</b><span>'+esc(e.date)+' · 訊號價 '+price(e.price)+'</span>'+(e.provisional&&liveByAction.has(e.code+'|'+e.type)?notificationDetails(liveByAction.get(e.code+'|'+e.type)):'')+sizing(e)+'<span>硬停損 '+price(e.stop)+'　停利參考 '+price(e.target)+'</span>'+(e.type==='exit'&&e.spread!=null?'<strong class="'+spreadClass(e.spread)+'">'+spreadText(e.spread)+'</strong>':'')+'</div>';};body+=alerts.slice(0,6).map(renderAlert).join('');if(alerts.length>6)body+='<details><summary>其餘 '+(alerts.length-6)+' 筆新訊號</summary>'+alerts.slice(6).map(renderAlert).join('')+'</details>';body+='<p>'+(meta.intraday?'盤中暫定訊號可能消失，不代表已收盤確認或可按該價格成交。':'下一交易日須重新確認價格與停損距離，不代表可按訊號價直接成交。')+'</p>';}
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
   '<div class="st-direction" aria-label="策略方向">'+['long','short'].map(function(d){return '<button type="button" data-direction="'+d+'" aria-pressed="'+((s.direction||'long')===d)+'">'+(d==='short'?'放空機會':'做多機會')+'</button>';}).join('')+'</div>'+notices(s)+'<details class="st-desk-filters" '+(s.filtersOpen?'open':'')+'><summary>篩選與排序</summary><div class="st-sector" aria-label="短線產業篩選">'+['全部','電子','非電子'].map(function(v){return '<button type="button" data-sector="'+v+'" aria-pressed="'+((s.sector||'全部')===v)+'">'+v+'</button>';}).join('')+'<select class="st-shape-filter" aria-label="型態">'+(short?['','reversal','breakout','pullback','continuation']:['','reversal','breakout','pullback','continuation','repair']).map(function(k){return '<option value="'+k+'" '+((s.shape||'')===k?'selected':'')+'>'+(strategyNames[k]||'全部型態')+'</option>';}).join('')+'</select></div>'+
   '<div class="st-price-tools"><label>股價區間<select class="st-price-filter" aria-label="股價區間">'+[['all','全部股價'],['under100','低於100元'],['100to500','100至未滿500元'],['500to1000','500至未滿1000元'],['1000plus','1000元以上']].map(function(x){return '<option value="'+x[0]+'" '+((s.priceRange||'all')===x[0]?'selected':'')+'>'+x[1]+'</option>';}).join('')+'</select></label><label>排序<select class="st-price-sort" aria-label="排序">'+[['signal','訊號優先'],['price_desc','股價由高到低'],['price_asc','股價由低到高']].map(function(x){return '<option value="'+x[0]+'" '+((s.sort||'signal')===x[0]?'selected':'')+'>'+x[1]+'</option>';}).join('')+'</select></label></div></details>'+'<div class="st-state-tabs" aria-label="進出場狀態">'+['','held','waiting','closed'].concat(counts.unknown?['unknown']:[]).map(function(g){return '<button type="button" data-position="'+g+'" aria-pressed="'+((s.position||'')===g)+'">'+(g?groupNames[g]:'全部')+(g?' <b>'+num(counts[g]||0)+'</b>':'')+'</button>';}).join('')+'</div><div class="st-list-summary">共 '+num(m.total==null?rows.length:m.total)+' 檔 · '+(s.sort==='price_desc'?'股價由高到低':s.sort==='price_asc'?'股價由低到高':'訊號優先')+'</div><div class="st-cards"></div><nav class="st-pagination" aria-label="候選分頁"><button type="button" data-page="-1" '+((m.offset||0)<=0?'disabled':'')+'>上一頁</button><span>第 '+(Math.floor((m.offset||0)/(m.limit||PAGE_SIZE))+1)+'／'+Math.max(1,Math.ceil((m.total||rows.length)/(m.limit||PAGE_SIZE)))+' 頁</span><button type="button" data-page="1" '+(!m.has_more?'disabled':'')+'>下一頁</button></nav>'+
   '<details class="st-data"><summary>更新與掃描範圍</summary><div class="st-refresh-info">'+esc((m.intraday?'獨立排程每 10 分鐘補檢，全市場分批接續':'收盤確認模式')+' · 上次完成 '+(m.generated_at?m.generated_at.slice(0,19).replace('T',' '):'尚未完成'))+'<br>'+esc(m.intraday?'下一輪約 '+(m.next_refresh_at?m.next_refresh_at.slice(11,19):'等待排程')+'；行情可能延遲，量比依開盤時間比例估算。':'盤中暫定訊號與收盤結果分開保存。')+'</div><div class="st-scope">全市場初篩 '+num(scope.universe_count||0)+' 檔 · 已詳查 '+num(scope.deep_done||m.scanned||0)+'／'+num(scope.deep_total||scope.universe_count||m.scanned||0)+' 檔'+(scope.deep_complete?'（本輪已處理完）':'（分批接續）')+'<br>'+esc(Object.keys(scope.sectors||{}).map(function(k){return k+' '+scope.sectors[k]+' 檔';}).join(' · '))+'<br>'+esc(Object.keys(scope.markets||{}).map(function(k){return k+' '+scope.markets[k]+' 檔';}).join(' · '))+'<br>技術詳查：'+esc(Object.keys(scope.deep_sectors||{}).map(function(k){return k+' '+scope.deep_sectors[k]+' 檔';}).join(' · '))+'<br>符合觀察條件：'+esc(Object.keys(scope.qualified_sectors||{}).map(function(k){return k+' '+scope.qualified_sectors[k]+' 檔';}).join(' · '))+'</div><p>行情讀取缺漏 '+num(m.failed||0)+' 檔；缺漏資料不補造訊號。</p><div class="st-price-audit"><h4>價格涵蓋範圍</h4><p>不設股價上限，不按法人買超張數排序。</p><table><thead><tr><th>股價</th><th>取得行情</th><th>技術初篩</th><th>詳查</th><th>符合觀察</th></tr></thead><tbody>'+['低於100','100–499','500–999','1000以上'].map(function(k){var c=(scope.price_coverage||{})[k]||{};return '<tr><th>'+k+'</th><td>'+num(c.quoted||0)+'</td><td>'+num(c.technical||0)+'</td><td>'+num(c.deep||0)+'</td><td>'+num(c.qualified||0)+'</td></tr>';}).join('')+'</tbody></table><details><summary>高價股逐檔檢查</summary>'+((scope.high_price_examples||[]).map(function(r){return '<p>'+esc(r.name)+' '+esc(r.code)+' · '+price(r.price)+'<br>'+esc((r.deep?'完整日K '+r.bars+' 根 · '+(r.date||'')+' · ':'')+r.reason)+'</p>';}).join('')||'<p>本輪診斷尚未完成。</p>')+'</details></div></details>'+
   '<details class="st-rules"><summary>訊號規則</summary><div><p>技術面為主，法人只供參考。成交量、成交金額不設最低門檻；不設股價上限，不按法人買超張數排序。每頁40檔，可翻頁查看全部候選，依型態與趨勢排列；未詳查的股票不宣稱已完成。</p><p>進場：轉折、突破、回測、延續其中一類型態成立，再檢查量價與風險空間。下降線／底部轉折須連續兩日收在同一突破線加緩衝之上，且兩日均站回20日線；不用同時滿足全部型態，也不要求法人買超。部位比例以首次進場股數為100%，不是總資產。一般加碼加首次股數的50%，最多一次，部位由100%變150%；須獲利達1R、再次放量突破且合併風險受控。減碼賣出當時股數的一半：100%剩50%，加碼至150%後則剩75%。連續兩日收盤跌破事前趨勢支撐與20日線，且20日線走平或下彎，才確認反轉出清；觸及硬停損優先退出。收復支撐、再次突破且風險受控時，回補前次減掉的股數，最多回補一次。例：首次1000股→加500股→減750股→回補750股；出場賣出剩餘全部。實際股數依你的首次進場股數換算；法人不影響買賣訊號。</p><p>盤中暫定訊號可能消失，量比依時間比例估算；收盤後重算。單日漲跌超過9.5%先等下一交易日確認；ATR影響風險距離，不單獨否決進場。結構停損距離上限依ATR介於6%至12%；初期轉折須至少1.5R，已確認趨勢的突破、回測與延續則依結構停損追蹤，第一道壓力是減碼關卡，並非整段趨勢的終點。法人同買僅指外資＋投信前五個已完成交易日淨買合計為正。場內是策略重播狀態，不是你的實際帳戶持股；這個分頁不下單。</p></div></details>';
  root.querySelectorAll('[data-direction]').forEach(function(b){b.onclick=function(){if(s.direction===b.dataset.direction)return;s.direction=b.dataset.direction;s.data={rows:[],alerts:[],meta:{building:true,direction:s.direction}};s.offset=0;unmountDetail(s);s.shape='';s.position='';s.rowsSignature=null;s.viewChanged=true;render(s,true);if(s.loading)s.pending=true;else load(s,false);};});
  root.querySelector('.st-rules div').textContent=(short?
   '放空型態分開檢查：上升線跌破、20日支撐跌破、10日或20日線反彈轉弱、弱勢整理再破低。明確跌破可用單日收盤超過0.5ATR且量比至少1.2確認；一般跌破用連續兩個有成交的日K確認。波段趨勢型態以結構停損追蹤，第一道支撐是回補觀察點，不是固定行情終點；初期轉折仍須1.5R空間。加空須已有1R獲利、再破低且初始風險預算受控，最多加首次股數50%；轉強回補當時空單50%，反轉或硬停損回補全部。':
   '進場型態分開檢查：底部／下降線轉折、突破、回測轉強、強勢整理延續。突破可用放量收盤，或連兩個有成交的日K站穩同一壓力與20日線；回測須收復前日高點。轉折初期須1.5R空間；已確認的波段趨勢以結構停損追蹤，第一道壓力是減碼觀察點，不是固定行情終點。多方修復僅觀察，不能單獨產生買點。加碼及減碼沿用首次股數50%、當時部位50%與初始風險預算限制。')+
   ' 不設股價上限、不以成交張數或法人買賣超作門檻。ATR是波動資料，不單獨禁止交易；結構停損距離仍須在ATR計算的6%至12%上限內。單日漲跌逾9.5%不新增部位，等待下一交易日重新確認可交易性。只讀取當時已完成資料；盤中訊號為暫定。零成交日K不計入確認與均線。訊號及價差是規則重播，非券商成交或含費績效；放空資格與券源未核實。不會下單。';
  root.querySelector('.st-desk-filters').ontoggle=function(e){s.filtersOpen=e.target.open;};
  root.querySelector('.st-shape-filter').onchange=function(e){s.shape=e.target.value;s.position='';s.offset=0;unmountDetail(s);s.viewChanged=true;if(s.loading)s.pending=true;else load(s,false);};
  function priceChoice(e,key){s[key]=e.target.value;s.offset=0;unmountDetail(s);s.viewChanged=true;try{localStorage.setItem('st-price-view',JSON.stringify({priceRange:s.priceRange,sort:s.sort}));}catch(ignore){}if(s.loading)s.pending=true;else load(s,false);}
  root.querySelector('.st-price-filter').onchange=function(e){priceChoice(e,'priceRange');};
  root.querySelector('.st-price-sort').onchange=function(e){priceChoice(e,'sort');};
  root.querySelector('.st-refresh').onclick=function(){load(s,true);};
  root.querySelectorAll('[data-sector]').forEach(function(b){b.onclick=function(){s.sector=b.dataset.sector==='全部'?'':b.dataset.sector;s.position='';s.offset=0;unmountDetail(s);s.viewChanged=true;if(s.loading)s.pending=true;else load(s,false);};});
  root.querySelectorAll('.st-state-tabs button[data-position]').forEach(function(b){b.onclick=function(){s.position=b.dataset.position;s.offset=0;unmountDetail(s);s.viewChanged=true;if(s.loading)s.pending=true;else load(s,false);};});
  root.querySelectorAll('[data-page]').forEach(function(b){b.onclick=function(){s.offset=Math.max(0,(s.offset||0)+Number(b.dataset.page)*PAGE_SIZE);s.pageJump=true;unmountDetail(s);s.viewChanged=true;if(s.loading)s.pending=true;else load(s,false);};});
  if(!target&&!s.el.hidden){var status=document.getElementById('wb-status');if(status)status.textContent=m.building?'短線更新中':'短線';}
 }
 function verticalScroller(row){
  for(var parent=row.parentElement;parent&&parent!==document.body;parent=parent.parentElement){
   if(parent.scrollHeight>parent.clientHeight+1&&/^(auto|scroll)$/.test(getComputedStyle(parent).overflowY))return parent;
  }
  return window;
 }
 function keepRowAt(row,top,scroller){
  if(!row||!row.isConnected)return;
  var delta=row.getBoundingClientRect().top-top;
  if(Math.abs(delta)>1)scroller.scrollBy({top:delta,behavior:'instant'});
 }
 function preserveScroll(root){
  var scroller=verticalScroller(root);
  var top=scroller===window?window.scrollY:scroller.scrollTop;
  return function(){
   var currentTop=scroller===window?window.scrollY:scroller.scrollTop;
   if(Math.abs(currentTop-top)>1)scroller.scrollTo({top:top,behavior:'instant'});
  };
 }
 function render(s,interactive){
  if(!alive(s))return;
  var rows=s.data.rows||[],signature=JSON.stringify(rows);
  var view=viewKey(s)+'|'+!!((s.data.meta||{}).position_counts||{}).unknown;
  if(!interactive&&!s.viewChanged&&(s.pointerActive||Date.now()-(s.lastScroll||0)<180)){
   s.renderPending=true;
   clearTimeout(s.renderTimer);
   s.renderTimer=setTimeout(function(){render(s);},200);
   return;
  }
  var restoreScroll=preserveScroll(s.el);
  if(!s.viewChanged&&s.rowsSignature===signature&&s.renderedView===view&&s.el.querySelector('.st-cards')){
   patchMeta(s);
   restoreScroll();
   return;
  }
  var previousDetail=s.el.querySelector('.st-desk-detail');
  var focus=document.activeElement,focusCode=focus&&focus.closest('.st-desk-row');
  var keepFocus=previousDetail&&previousDetail.contains(focus);
  var tableScroll=s.el.querySelector('.st-desk-scroll');
  var scrollLeft=tableScroll?tableScroll.scrollLeft:0;
  var openSections=['.st-data','.st-rules','.st-price-audit details'].filter(function(selector){
   return s.el.querySelector(selector)?.open;
  });
  header(s);
  openSections.forEach(function(selector){var section=s.el.querySelector(selector);if(section)section.open=true;});
  var host=s.el.querySelector('.st-cards');
  s.rowsSignature=signature;s.renderedView=view;s.renderPending=false;
  if(!rows.length){
   var meta=s.data.meta||{};
   host.innerHTML=meta.building?'<div class="st-skeleton" aria-label="正在篩選候選"></div>':
    '<div class="st-empty"><b>'+(!meta.date?'暫無可顯示的短線快照':'目前沒有符合條件的股票')+'</b></div>';
   restoreScroll();
   return;
  }
  var short=s.direction==='short',previous=null;
  host.innerHTML='<div class="st-desk-scroll"><table class="st-desk-table"><caption>策略訊號價 · 每股價差（未扣費）</caption>'+
   '<colgroup><col class="st-desk-symbol"><col class="st-desk-status"><col><col><col><col class="st-desk-pnl"></colgroup>'+
   '<thead><tr><th scope="col">標的</th><th scope="col">狀態</th><th scope="col">現價</th><th scope="col">'+(short?'放空':'進場')+
   '</th><th scope="col">'+(short?'回補':'出場')+'</th><th scope="col">每股價差</th></tr></thead><tbody>'+
   rows.map(function(r){
    var g=group(r),heading='',q=r.display_quote||{},trade=r.trade||{},v=trade.spread,signal=r.dashboard_signal||{};
    var status=g==='waiting'?(r.provisional&&r.latest_event==='entry'?'暫定進場':r.signal_stage==='triggered'?'已觸發':'觀察'):groupNames[g];
    var phase=stageLabels[signal.stage]||'',bias=biasLabels[signal.bias];
    var distance=signal.distance_pct!=null&&signal.distance_pct>=0?'距'+(short?'跌破':'突破')+' '+num(signal.distance_pct)+'%':'';
    if(!s.position&&s.sort==='signal'&&g!==previous){
     heading='<tr class="st-desk-group"><th scope="row" colspan="6">'+esc(g==='waiting'?'未進場／觀察':groupNames[g])+'</th></tr>';
    }
    previous=g;
    return heading+'<tr class="st-desk-row" data-code="'+esc(r.code)+'" data-position="'+esc(g)+'" data-open="'+(s.open===r.code)+'">'+
     '<th scope="row"><button type="button" class="st-desk-open" aria-expanded="'+(s.open===r.code)+'" aria-controls="st-detail-'+esc(r.code)+'"><b>'+
     esc(r.code)+'</b><span title="'+esc(r.name)+'">'+esc(r.name)+'</span>'+(bias?'<small class="st-bias" data-bias="'+esc(signal.bias)+'">'+esc(bias)+'</small>':'')+'</button></th>'+
     '<td><span class="st-desk-state" data-state="'+esc(g)+'">'+esc(status)+'</span>'+
     (phase&&signal.stage!=='stale'?'<small class="st-phase" data-phase="'+esc(signal.stage)+'">'+esc(phase)+'</small>':'')+'</td>'+
     '<td><b>'+price(q.price!=null?q.price:r.price)+'</b>'+(distance?'<small class="st-distance" title="以策略資料日價格計算">'+esc(distance)+'</small>':q.date||r.date?'<small>'+esc((q.date||r.date).slice(5).replace('-','/'))+'</small>':'')+'</td>'+
     '<td>'+price(trade.entry_price)+'</td><td>'+price(trade.exit_price)+'</td>'+
     '<td class="st-desk-spread '+spreadClass(v)+'"><b>'+(v==null?'—':(v>0?'+':v<0?'−':'')+price(Math.abs(v)))+'</b>'+
     (v==null?'':'<small>'+(g==='held'?'未實現':'已出場')+'</small>')+'</td></tr>';
   }).join('')+'</tbody></table></div>';
  var scroller=host.querySelector('.st-desk-scroll');
  scroller.scrollLeft=scrollLeft;
  var active=s.open&&host.querySelector('.st-desk-row[data-code="'+CSS.escape(s.open)+'"]');
  if(active&&previousDetail&&previousDetail.dataset.code===s.open){
   active.after(previousDetail);
   if(keepFocus&&focus.isConnected)focus.focus({preventScroll:true});
  }else if(active){
   mountDetail(s,active);
  }else{
   s.open=null;
  }
  if(focusCode){
   var replacement=host.querySelector('.st-desk-row[data-code="'+CSS.escape(focusCode.dataset.code)+'"] button');
   if(replacement)replacement.focus({preventScroll:true});
  }
  host.addEventListener('click',function(e){
   var row=e.target.closest('.st-desk-row');
   if(!row)return;
   var close=s.open===row.dataset.code;
   var top=row.getBoundingClientRect().top,scroller=verticalScroller(row);
   unmountDetail(s);
   if(!close){
    s.open=row.dataset.code;s.history=true;s.event=null;s.end=null;s.readDate=null;
    row.dataset.open='true';
    row.querySelector('button').setAttribute('aria-expanded','true');
    mountDetail(s,row);
   }
   keepRowAt(row,top,scroller);
  });
  restoreScroll();
  if(s.pageJump){s.pageJump=false;s.el.querySelector('.st-list-summary').scrollIntoView({block:'start'});}
 }
 function unmountDetail(s){
  if(s.detailRequest)s.detailRequest.abort();
  var old=s.el.querySelector('.st-desk-detail'),active=s.el.querySelector('.st-desk-row[data-open="true"]');
  if(old)old.remove();
  if(active){active.dataset.open='false';active.querySelector('button').setAttribute('aria-expanded','false');}
  s.open=null;
 }
 function mountDetail(s,row){
  var code=row.dataset.code,direction=s.direction;
  var panel=document.createElement('tr');
  panel.className='st-desk-detail';
  panel.dataset.code=code;
  panel.innerHTML='<td colspan="6"><section class="st-detail" id="st-detail-'+esc(code)+'"><p role="status">正在載入 '+esc(code)+' 的 K 線…</p></section></td>';
  row.after(panel);
  var key=direction+':'+code;
  var cached=s.detailCache.get(key);
  if(cached&&Date.now()-cached.at<30000){detail(s,cached.row);return;}
  if(s.detailRequest)s.detailRequest.abort();
  var controller=new AbortController();
  s.detailRequest=controller;
  var timeout=setTimeout(function(){controller.abort();},12000);
  fetch(endpoint({code:code,direction:direction}),{credentials:'same-origin',cache:'no-store',signal:controller.signal})
   .then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();})
   .then(function(data){
    if(!data.ok||!data.row)throw new Error('empty detail');
    if(!alive(s)||s.open!==code||s.direction!==direction||!panel.isConnected)return;
    s.detailCache.set(key,{row:data.row,at:Date.now()});
    if(s.detailCache.size>8)s.detailCache.delete(s.detailCache.keys().next().value);
    detail(s,data.row);
   })
   .catch(function(){
    if(!alive(s)||s.open!==code||s.direction!==direction||!panel.isConnected)return;
    var section=panel.querySelector('.st-detail');
    section.innerHTML='<p role="status">K 線暫時無法載入。</p><button type="button" class="st-detail-retry">再試一次</button>';
    section.querySelector('button').onclick=function(){panel.remove();mountDetail(s,s.el.querySelector('.st-desk-row[data-code="'+CSS.escape(code)+'"]'));};
   })
   .finally(function(){clearTimeout(timeout);if(s.detailRequest===controller)s.detailRequest=null;});
 }
 function patchMeta(s){
  var scratch=document.createElement('div');header(s,scratch);
  ['.st-status','.st-notices','.st-list-summary','.st-pagination','.st-data','.st-rules'].forEach(function(selector){var old=s.el.querySelector(selector),next=scratch.querySelector(selector);if(!old||!next||old.outerHTML===next.outerHTML||old.contains(document.activeElement))return;if(old.tagName==='DETAILS')next.open=old.open;old.replaceWith(next);});
  s.el.querySelectorAll('.st-state-tabs button[data-position]').forEach(function(button){var b=button.querySelector('b'),next=scratch.querySelector('.st-state-tabs button[data-position="'+button.dataset.position+'"] b');if(b&&next)b.textContent=next.textContent;});
 }
 var motion=matchMedia('(prefers-reduced-motion: reduce)');
 window.addEventListener('scroll',function(){if(current)current.lastScroll=Date.now();},{passive:true});
 document.addEventListener('pointerdown',function(e){if(current&&current.el.contains(e.target))current.pointerActive=true;},{capture:true,passive:true});
 ['pointerup','pointercancel'].forEach(function(type){
  document.addEventListener(type,function(){
   if(!current)return;
   current.pointerActive=false;
   if(current.renderPending){clearTimeout(current.renderTimer);current.renderTimer=setTimeout(function(){if(current)render(current);},200);}
  },{passive:true});
 });
 var stageLabels={near:'接近觸發',triggered:'待確認',provisional:'盤中暫定',confirmed:'收盤確認',invalidated:'失效',stale:'待更新'};
 var biasLabels={bullish:'整理偏多',bearish:'整理偏空',unknown:'方向未明'};
 function signalContext(r){
  var info=r.dashboard_signal;
  if(!info)return '';
  var stage=stageLabels[info.stage]||(info.stage==='held'?'策略場內':'觀察中');
  var stamp=info.quote_at?info.quote_at.slice(0,16).replace('T',' '):info.data_date;
  var action=names[info.action];
  var level=info.level==null?'':(r.direction==='short'?'跌破參考 ':'突破參考 ')+price(info.level);
  var basis=info.basis_price!=null?' · 策略價 '+price(info.basis_price):'';
  var distance=info.distance_pct!=null&&info.distance_pct>=0?' · 距參考價 '+num(info.distance_pct)+'%':'';
  var reason=info.reason&&info.reason.split('；')[0];
  return '<section class="st-signal-context" aria-label="訊號階段"><div><strong data-phase="'+esc(info.stage)+'">'+esc(stage)+'</strong>'+
   (action?'<b>'+esc(action)+' '+price(info.price)+'</b>':biasLabels[info.bias]?'<b>'+esc(biasLabels[info.bias])+'</b>':'')+
   (stamp?'<time>'+esc(stamp)+'</time>':'')+'</div>'+
   (level?'<p>'+esc(level+distance+basis)+'</p>':'')+(reason?'<p>'+esc(reason)+'</p>':'')+'</section>';
 }
 function timeline(r){
  var events=(r.signals||[]).slice().reverse();
  return '<section class="st-history" aria-label="訊號時間軸"><h4>訊號時間軸（'+events.length+'）</h4><p class="st-history-note">依日期由新到舊；歷史規則重播，不是當下的新買點。</p><ol>'+events.map(function(e){return '<li><button type="button" data-event="'+esc(e.id)+'"><span>'+esc(e.date)+'</span><b class="'+(['entry','add'].indexOf(e.type)>=0?'st-up':'st-down')+'">'+esc(names[e.type])+'</b><strong>'+price(e.price)+'</strong></button><span class="st-event-status">'+(e.provisional?'盤中暫定 · 未收棒':'歷史收盤規則重播')+'</span>'+sizing(e)+'<p class="st-event-reason"><b>理由：</b>'+esc((e.reason||'理由未保存').split('；')[0])+'</p><details><summary>查看當時數值與完整理由</summary><p>'+esc(e.reason||'此筆舊紀錄沒有保存理由，不補造原因。')+'</p></details><small>當時硬停損 '+price(e.stop)+' · 停利參考 '+price(e.target)+'</small></li>';}).join('')+'</ol>'+(events.length?'':'<p>這段資料尚無進出場紀錄，不補畫訊號。</p>')+'</section>';
 }
 function detail(s,r){
  var el=s.el.querySelector('#st-detail-'+CSS.escape(r.code)),chip=r.chip||{};
  r.bars=Array.isArray(r.bars)?r.bars:[];
  el.innerHTML='<div class="st-chart-head"><div><b>K 線與策略訊號</b><span>'+(r.provisional?'盤中日線 · 暫定':'日線 · 收盤確認')+' · '+esc((r.market||'')+' '+(r.sector||''))+'</span></div><div class="st-periods" aria-label="圖表期間">'+[30,60,120].map(function(n){return '<button type="button" data-range="'+n+'" aria-pressed="'+(s.range===n)+'">'+n+' 日</button>';}).join('')+'</div></div>'+
   '<div class="st-chart-mode"><span class="st-chart-context"></span><button type="button" class="st-history-toggle" aria-pressed="false">查看歷史訊號</button></div>'+
   '<div class="st-chart-readout" aria-live="polite">滑動或點選查看日期與價格</div><div class="st-chart" role="group" aria-label="'+esc(r.name)+'日 K、動能與進出場訊號"></div>'+
   '<div class="st-chart-nav"><button type="button" data-step="-1" aria-label="查看較早K線">← 較早</button><span>'+(r.direction==='short'?'▼ 放空／加空　▲ 回補':'▲ 進場／加碼　▼ 減碼／出場')+'</span><button type="button" data-step="1" aria-label="查看較新K線">較新 →</button></div>'+
   signalContext(r)+'<div class="st-inspector" aria-live="polite"></div><div class="st-checks">'+(r.checks||[]).map(function(c){return '<span data-passed="'+!!c.passed+'">'+(c.passed?'✓ ':'— ')+esc(c.label)+(c.passed?'成立':'待確認')+'</span>';}).join('')+'</div><div class="st-evidence"><span>'+(r.volume_estimated?'時間比例估算量比 ':'量比 ')+num(r.volume_ratio)+'</span>'+(r.institutional_buying?'<span>法人同買（前五個已完成交易日）</span>':'')+'</div>'+timeline(r)+'<div class="st-chip-detail"><h4>技術依據與法人參考</h4><dl><div><dt>近 20 日平均成交額（非門檻）</dt><dd>'+num(r.avg_turnover/10000)+' 萬元</dd></div><div><dt>近 20 日平均成交量（非門檻）</dt><dd>'+num(r.avg_volume/1000)+' 張</dd></div><div><dt>支撐參考</dt><dd>'+price(r.support)+'</dd></div><div><dt>上方歷史壓力</dt><dd>'+(r.resistance?price(r.resistance):'區間內無已知壓力')+'</dd></div><div><dt>外資近五日</dt><dd>'+num(chip.foreign)+' 張／'+num(chip.foreign_days)+' 日買超</dd></div><div><dt>投信近五日</dt><dd>'+num(chip.trust)+' 張／'+num(chip.trust_days)+' 日買超</dd></div><div><dt>法人淨買占五日成交量（參考）</dt><dd>'+num(r.chip_ratio)+'%</dd></div><div><dt>籌碼資料截至</dt><dd>'+esc(chip.date||'缺資料')+'</dd></div></dl></div>'+
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
 function macdHistogram(bars){
  var ema12=null,ema26=null,signal=null,total=0,signalSeed=[];
  return bars.map(function(b,i){
   total+=b.close;
   if(i===11)ema12=total/12;
   else if(i>11)ema12+=(b.close-ema12)*2/13;
   if(i===25)ema26=total/26;
   else if(i>25)ema26+=(b.close-ema26)*2/27;
   if(ema26===null)return null;
   var difference=ema12-ema26;
   if(signal===null){
    signalSeed.push(difference);
    if(signalSeed.length<9)return null;
    signal=signalSeed.reduce(function(a,b){return a+b;},0)/9;
   }else signal+=(difference-signal)*.2;
   return difference-signal;
  });
 }
 function draw(s,r,el){
  el.querySelector('.st-chart-context').textContent=s.history?'歷史重播，非目前買點':'目前訊號，截至 '+r.date;el.querySelector('.st-history-toggle').textContent=s.history?'隱藏歷史訊號':'查看歷史訊號';el.querySelector('.st-history-toggle').setAttribute('aria-pressed',String(!!s.history));
  var host=el.querySelector('.st-chart'),all=r.bars,end=Math.min(s.end||all.length,all.length),start=Math.max(0,end-s.range),bars=all.slice(start,end),n=bars.length;
  if(!n){
   host.innerHTML='<p class="st-chart-unavailable">此快照未保存 K 線。</p>';
   el.querySelectorAll('[data-step]').forEach(function(button){button.disabled=true;});
   return;
  }
  var event=(r.signals||[]).find(function(e){return e.id===s.event;});
  var line=event?event.pattern_line:r.pattern_line;
  var leftIndex=line?all.findIndex(function(b){return b.date===line.start_date;}):-1;
  var rightIndex=line?all.findIndex(function(b){return b.date===line.end_date;}):-1;
  var hasLine=leftIndex>=0&&rightIndex>leftIndex&&Number(line.start_price)>0&&Number(line.end_price)>0;
  var extra=hasLine&&rightIndex>=end-14&&rightIndex<end?Math.min(6,Math.max(3,Math.round(n*.13))):0;
  var W=Math.max(260,Math.min(1040,host.clientWidth||700)),H=438,L=10,R=60,T=hasLine&&W<410?42:27,B=248,V=295,M=371,step=(W-L-R)/(n+extra),cw=Math.max(1,step*.66);
  var low=Math.min.apply(null,bars.map(function(b){return b.low;})),high=Math.max.apply(null,bars.map(function(b){return b.high;}));
  var stop=event?event.stop:r.stop,target=event?event.target:r.target;
  // Distant levels should not squash candles into a flat line; out-of-range
  // values remain readable in the inspector below the chart.
  var span=Math.max(high-low,high*.02),min=low-span*.13,max=high+span*.13;
  function y(v){return T+(max-v)/(max-min)*(B-T);}
  function x(i){return L+step*(i+.5);}
  var svg='<svg viewBox="0 0 '+W+' '+H+'" role="img" aria-label="日 K、成交量、MACD 動能柱與交易訊號"><title>'+esc(r.name)+' K 線策略圖</title>';
  svg+='<rect class="st-momentum-bg" x="'+L+'" y="326" width="'+(W-L-R)+'" height="88" rx="7"/>'+
   '<text class="st-axis st-momentum-label" x="'+(L+4)+'" y="321">MACD 動能柱 · 12 / 26 / 9</text>'+
   '<line class="st-momentum-zero" x1="'+L+'" x2="'+(W-R)+'" y1="'+M+'" y2="'+M+'"/>'+
   '<text class="st-axis" x="'+(W-R+8)+'" y="'+(M+4)+'">0</text>';
  for(var k=0;k<5;k++){var value=min+(max-min)*k/4,yy=y(value);svg+='<line class="st-grid" x1="'+L+'" y1="'+yy+'" x2="'+(W-R)+'" y2="'+yy+'"/><text class="st-axis" x="'+(W-R+8)+'" y="'+(yy+4)+'">'+price(value)+'</text>';}
  svg+='<text class="st-axis" x="'+(L+4)+'" y="263">成交量</text>';
  var maxVol=Math.max.apply(null,bars.map(function(b){return b.volume;}))||1;
  bars.forEach(function(b,i){var color=b.close>=b.open?'#c5343b':'#13805f',xx=x(i);svg+='<line stroke="'+color+'" x1="'+xx+'" x2="'+xx+'" y1="'+y(b.high)+'" y2="'+y(b.low)+'"/><rect fill="'+color+'" x="'+(xx-cw/2)+'" y="'+Math.min(y(b.open),y(b.close))+'" width="'+cw+'" height="'+Math.max(1,Math.abs(y(b.open)-y(b.close)))+'"/><rect fill="'+color+'" opacity=".35" x="'+(xx-cw/2)+'" y="'+(V-b.volume/maxVol*30)+'" width="'+cw+'" height="'+(b.volume/maxVol*30)+'"/>';});
  var hist=macdHistogram(all).slice(start,end),momentumScale=Math.max(0.0001,...hist.map(function(v){return Math.abs(v||0);}));
  hist.forEach(function(value,i){
   if(value===null||value===0)return;
   var height=Math.max(1,Math.abs(value)/momentumScale*35);
   svg+='<rect class="st-momentum-bar '+(value>0?'st-momentum-up':'st-momentum-down')+'" x="'+(x(i)-cw/2)+'" y="'+(value>0?M-height:M)+'" width="'+cw+'" height="'+height+'" rx="1"/>';
  });
  function moving(days,color){var points=[];for(var i=start;i<end;i++){if(i+1>=days){var sum=all.slice(i-days+1,i+1).reduce(function(a,b){return a+b.close;},0)/days;if(sum>=min&&sum<=max)points.push(x(i-start)+','+y(sum));}}if(points.length)svg+='<polyline fill="none" stroke="'+color+'" stroke-width="1.6" points="'+points.join(' ')+'"/>';}
  moving(10,'#b58a34');moving(20,'#647db0');
  var trendText='';
  if(hasLine){
   var startPrice=Number(line.start_price);
   var slope=(Number(line.end_price)-startPrice)/(rightIndex-leftIndex);
   var lastVisible=end-1+extra;
   var color=slope>0?'#167eab':'#ad530a';
   function paintTrend(lo,hi,projected){
    if(slope){
     var atMin=leftIndex+(min-startPrice)/slope,atMax=leftIndex+(max-startPrice)/slope;
     lo=Math.max(lo,Math.min(atMin,atMax));
     hi=Math.min(hi,Math.max(atMin,atMax));
    }else if(startPrice<min||startPrice>max)return '';
    if(hi<=lo)return '';
    return '<line class="st-pattern-line'+(projected?' st-pattern-extension':'')+'" x1="'+x(lo-start)+'" x2="'+x(hi-start)+
     '" y1="'+y(startPrice+slope*(lo-leftIndex))+'" y2="'+y(startPrice+slope*(hi-leftIndex))+'" stroke="'+color+
     '" stroke-width="'+(projected?'2':'2.8')+'" stroke-linecap="round"'+(projected?' stroke-dasharray="5 4"':'')+' />';
   }
   var confirmed=paintTrend(Math.max(start,leftIndex),Math.min(lastVisible,rightIndex,end-1),false);
   var projection=paintTrend(Math.max(start,rightIndex),Math.min(lastVisible,rightIndex+14),true);
   svg+=confirmed+projection;
   if(confirmed||projection){
    trendText=(slope>0?'上升趨勢線':'下降趨勢線')+(projection?' · 虛線為延伸參考':'');
    svg+='<text class="st-axis st-trend-label" x="'+(L+2)+'" y="18" style="fill:'+color+'">'+trendText+'</text>';
   }
  }
  [[stop,'硬停損','#13805f'],[target,event?'當時停利':'停利參考','#a75916']].forEach(function(a){if(a[0]>=min&&a[0]<=max){var yy=y(a[0]);svg+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+yy+'" y2="'+yy+'" stroke="'+a[2]+'" stroke-dasharray="5 5" opacity=".8"/><text x="'+(W-R-4)+'" y="'+(yy-5)+'" text-anchor="end" fill="'+a[2]+'" font-size="11">'+a[1]+'</text>';}});
  if(!event&&r.support>=min&&r.support<=max){svg+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+y(r.support)+'" y2="'+y(r.support)+'" stroke="#647db0" stroke-dasharray="2 5" opacity=".65"/><text x="'+(L+3)+'" y="'+(y(r.support)-5)+'" fill="#647db0" font-size="11">目前支撐 '+price(r.support)+'</text>';}
  var trail=(r.trailing||[]).filter(function(t){return (s.history||(r.current_action!=='unconfirmed'&&r.active&&t.date>=(r.last_entry_date||r.date)))&&t.date>=bars[0].date&&t.date<=bars[n-1].date&&t.value>=min&&t.value<=max;});
  var d='';trail.forEach(function(t,i){var ix=bars.findIndex(function(b){return b.date===t.date;});if(ix>=0)d+=(i?'H'+x(ix)+'V'+y(t.value):'M'+x(ix)+','+y(t.value));});
  if(d)svg+='<path d="'+d+'" fill="none" stroke="#13805f" opacity=".7" stroke-width="1.7"/>';
  (r.signals||[]).filter(function(e){return (s.history||(r.current_action!=='unconfirmed'&&e.date===r.date))&&e.date>=bars[0].date&&e.date<=bars[n-1].date;}).slice(-12).forEach(function(e){var i=bars.findIndex(function(b){return b.date===e.date;}),up=(e.type==='entry'||e.type==='add')!==(r.direction==='short'),xx=x(i),yy=y(up?bars[i].low:bars[i].high)+(up?14:-14),path=up?'M-7,6L0,-6L7,6Z':'M-7,-6L0,6L7,-6Z';svg+='<g transform="translate('+xx+','+yy+')"><g data-marker="'+esc(e.id)+'" tabindex="0" role="button" aria-label="'+esc(e.date+' '+names[e.type]+' '+price(e.price))+'"><rect x="-22" y="-22" width="44" height="44" fill="transparent"/><path d="'+path+'" fill="'+colors[e.type]+'"/><text y="'+(up?24:-12)+'" text-anchor="middle" font-size="12" fill="'+colors[e.type]+'">'+names[e.type]+'</text></g></g>';});
  Array.from(new Set([0,Math.floor(n/2),n-1])).forEach(function(i){svg+='<text class="st-axis" x="'+x(i)+'" y="431" text-anchor="'+(i===0?'start':i===n-1?'end':'middle')+'">'+esc(bars[i].date.slice(5))+'</text>';});
  svg+='<line class="st-cross" x1="0" x2="0" y1="'+T+'" y2="414" visibility="hidden"/><text class="st-axis" x="'+(W-R)+'" y="'+(T===42?32:18)+'" text-anchor="end">MA10 / MA20</text></svg>';
  host.innerHTML=svg;var chart=host.querySelector('svg'),cross=host.querySelector('.st-cross');
  function read(e){var rect=chart.getBoundingClientRect(),px=(e.clientX-rect.left)/rect.width*W,i=Math.max(0,Math.min(n-1,Math.floor((px-L)/step))),b=bars[i];cross.setAttribute('x1',x(i));cross.setAttribute('x2',x(i));cross.setAttribute('visibility','visible');if(s.readDate===b.date&&el.querySelector('.st-chart-readout').textContent.startsWith(b.date))return;s.readDate=b.date;el.querySelector('.st-chart-readout').textContent=b.date+'　開 '+price(b.open)+'　高 '+price(b.high)+'　低 '+price(b.low)+'　收 '+price(b.close)+'　動能柱 '+num(hist[i]);}
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
     try{sessionStorage.setItem('st-dashboard-results-'+s.direction,JSON.stringify(s.snapshot));}catch(ignore){}
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
  current={el:el,data:{rows:[],meta:{building:true}},open:null,offset:0,direction:'long',priceRange:'all',sort:'signal',sector:'',shape:'',position:'',pending:false,history:false,range:60,end:null,event:null,request:0,loading:false,polls:0,timer:null,id:++sequence,detailCache:new Map()};
  try{var saved=JSON.parse(localStorage.getItem('st-price-view')||'{}');if(['all','under100','100to500','500to1000','1000plus'].includes(saved.priceRange))current.priceRange=saved.priceRange;if(['signal','price_desc','price_asc'].includes(saved.sort))current.sort=saved.sort;}catch(ignore){}
  current.data=previousSnapshot(current,'顯示上次保存的結果，正在確認最新資料')||current.data;
  render(current);load(current,false);
 },refresh:function(el){this.mount(el);current.polls=0;load(current,true);}};
 document.addEventListener('stockbot:pageleaving',dismissActionBanner);
 document.addEventListener('visibilitychange',function(){if(document.hidden)dismissActionBanner();else if(current&&!current.el.hidden&&!current.loading)load(current,false);});
 var panel=document.getElementById('wb-short-term');if(panel&&!panel.hidden)window.StockBotShortTerm.mount(panel);
})();
