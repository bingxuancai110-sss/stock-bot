(function(){
 'use strict';
 var current=null, sequence=0;
 var names={entry:'進場',add:'加碼',reduce:'減碼',exit:'出場'};
 var colors={entry:'#c5343b',add:'#a75916',reduce:'#94651b',exit:'#13805f'};
 function esc(v){return String(v==null?'—':v).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
 function num(v){return v==null?'—':Number(v).toLocaleString('zh-TW',{maximumFractionDigits:2});}
 function price(v){return v==null?'—':Number(v).toLocaleString('zh-TW',{minimumFractionDigits:2,maximumFractionDigits:2});}
 function api(refresh){var t=new URLSearchParams(location.search).get('t')||'';return '/web/api/workbench/short-term'+(refresh?'?refresh=1':'?read=1')+(t?'&t='+encodeURIComponent(t):'');}
 function alive(s){return current===s&&s.el.isConnected;}
 function header(s){
  var meta=s.data.meta||{};
  s.el.innerHTML='<header class="st-heading"><div><span class="st-eyebrow">PRICE × INSTITUTIONAL FLOW</span><h2>短線交易計畫 <span class="st-beta">試行</span></h2><p>技術找位置，籌碼確認買盤。</p></div><button type="button" class="st-refresh">重新篩選</button></header>'+
   '<div class="st-summary"><span>日 K</span><span>最多 6 檔</span><span>不使用基本面分數</span></div>'+
   '<div class="st-status" role="status">'+esc(meta.building?'背景篩選中 '+(meta.progress||0)+'／'+(meta.pool_limit||24):meta.error?meta.error:meta.date?'行情截至 '+meta.date+' · 檢查 '+meta.scanned+' 檔候選':'尚無符合條件的候選')+(meta.stale&&meta.date?' · 舊結果':'')+'</div>'+
   '<div class="st-cards"></div><details class="st-rules"><summary>篩選與訊號規則</summary><div><p>僅篩選最近五日外資或投信累計買超、至少三日買超的前 24 檔候選；不是全市場完整排名。技術趨勢、量價與法人條件一起確認，不用總分。</p><p>排除二十日均量不足 1,000 張、均成交額不足 1 億、當日漲跌超過 5%、五日漲超過 15%、月線乖離超過 8%、ATR 波動過高的進場候選。無法保證排除所有炒作股；尚未串接注意／處置股名單。</p><p>突破放量或月線回測轉強才進場；加碼需獲利達 1R、再次突破、籌碼支持且合併風險不超初始預算。壓力受阻或跌破十日線搭配法人轉賣時減碼一次；保護價只升不降，次日生效。</p><p>一般訊號收棒確認，法人只使用該棒前交易日已知資料。歷史箭頭為規則重播，非實際交易、非回測績效；價格不保證成交，未計費用與滑價。這個分頁不下單、不修改持股。</p></div></details>';
  s.el.querySelector('.st-refresh').onclick=function(){load(s,true);};
 }
 function render(s){
  if(!alive(s))return;
  header(s);var rows=s.data.rows||[],host=s.el.querySelector('.st-cards');
  if(!rows.length){host.innerHTML=(s.data.meta||{}).building?'<div class="st-skeleton" aria-label="正在篩選候選"></div><div class="st-skeleton"></div>':'<div class="st-empty"><b>目前沒有符合條件的短線候選</b><p>寧可等待，不為了湊名單放寬條件。</p></div>';return;}
  host.innerHTML=rows.map(function(r,i){var chip=r.chip||{},event=r.latest_event,up=r.change_pct>=0;return '<article class="st-card" data-code="'+esc(r.code)+'"><button type="button" class="st-card-toggle" aria-expanded="'+(s.open===r.code)+'" aria-controls="st-detail-'+esc(r.code)+'"><div class="st-stock"><span class="st-state" data-kind="'+esc(event||'wait')+'">'+esc(r.state)+'</span><h3>'+esc(r.name)+' <small>'+esc(r.code)+'</small></h3><span class="st-date">'+esc(r.date)+' 收盤</span></div><div class="st-quote"><strong>'+price(r.price)+'</strong><span class="'+(up?'st-up':'st-down')+'">'+(up?'+':'')+Number(r.change_pct).toFixed(2)+'%</span><span class="st-open-label">'+(s.open===r.code?'收合 K 線 −':'K 線計畫 ＋')+'</span></div></button>'+
   '<div class="st-levels"><div><span>'+(r.active?'策略進場價':'突破觀察價')+'</span><b>'+price(r.entry)+'</b></div><div><span>'+(r.active?'移動保護價':'規劃停損')+'</span><b class="st-down">'+price(r.stop)+'</b></div><div><span>'+esc(r.target_kind||'停利參考')+'</span><b>'+price(r.target)+'</b></div></div>'+
   '<div class="st-evidence"><span>'+esc(r.trend?'趨勢向上':'趨勢待確認')+'</span><span>量比 '+num(r.volume_ratio)+'</span><span>五日法人淨買 '+num(chip.foreign+chip.trust)+' 張</span></div>'+
   '<section class="st-detail" id="st-detail-'+esc(r.code)+'" '+(s.open===r.code?'':'hidden')+'></section></article>';}).join('');
  host.querySelectorAll('.st-card-toggle').forEach(function(btn){btn.onclick=function(){var code=btn.closest('.st-card').dataset.code;s.open=s.open===code?null:code;s.event=null;render(s);};});
  if(s.open){var row=rows.find(function(r){return r.code===s.open;});if(row)detail(s,row);}
 }
 function detail(s,r){
  var el=s.el.querySelector('#st-detail-'+r.code),events=r.signals||[],chip=r.chip||{};
  el.innerHTML='<div class="st-chart-head"><div><b>K 線與策略訊號</b><span>日線 · 歷史規則重播</span></div><div class="st-periods" aria-label="圖表期間">'+[30,60,120].map(function(n){return '<button type="button" data-range="'+n+'" aria-pressed="'+(s.range===n)+'">'+n+' 日</button>';}).join('')+'</div></div>'+
   '<div class="st-chart-readout" aria-live="polite">左右滑動查看日期與價格</div><div class="st-chart" role="group" aria-label="'+esc(r.name)+'日 K 與進出場訊號"></div>'+
   '<div class="st-chart-nav"><button type="button" data-step="-1" aria-label="查看較早K線">← 較早</button><span>▲ 進場／加碼　▼ 減碼／出場</span><button type="button" data-step="1" aria-label="查看較新K線">較新 →</button></div>'+
   '<div class="st-inspector" aria-live="polite"></div><div class="st-chip-detail"><h4>技術 × 籌碼依據</h4><dl><div><dt>支撐參考</dt><dd>'+price(r.support)+'</dd></div><div><dt>上方歷史壓力</dt><dd>'+(r.resistance?price(r.resistance):'區間內無已知壓力')+'</dd></div><div><dt>外資近五日</dt><dd>'+num(chip.foreign)+' 張／'+num(chip.foreign_days)+' 日買超</dd></div><div><dt>投信近五日</dt><dd>'+num(chip.trust)+' 張／'+num(chip.trust_days)+' 日買超</dd></div><div><dt>法人淨買占五日成交量</dt><dd>'+num(r.chip_ratio)+'%</dd></div><div><dt>籌碼資料截至</dt><dd>'+esc(chip.date||'缺資料')+'</dd></div></dl></div>'+
   '<details class="st-history"><summary>訊號紀錄（'+events.length+'）</summary><div>'+events.slice().reverse().map(function(e){return '<button type="button" data-event="'+esc(e.id)+'"><span>'+esc(e.date)+'</span><b style="color:'+colors[e.type]+'">'+names[e.type]+'</b><strong>'+price(e.price)+'</strong></button>';}).join('')+(events.length?'':'<p>這段資料內沒有符合規則的歷史進場，不補畫箭頭。</p>')+'</div></details><p class="st-source">行情：'+esc(r.source)+' · 停損線為觸發價，非保證成交價。</p>';
  el.querySelectorAll('[data-range]').forEach(function(b){b.onclick=function(){s.range=Number(b.dataset.range);s.end=null;detail(s,r);};});
  el.querySelectorAll('[data-step]').forEach(function(b){b.onclick=function(){s.end=Math.max(Math.min(s.range,r.bars.length),Math.min(r.bars.length,(s.end||r.bars.length)+Number(b.dataset.step)*Math.max(5,Math.floor(s.range/2))));draw(s,r,el);};});
  el.querySelectorAll('[data-event]').forEach(function(b){b.onclick=function(){select(s,r,b.dataset.event,el);};});
  inspect(s,r,el);draw(s,r,el);
 }
 function inspect(s,r,el){
  var e=(r.signals||[]).find(function(x){return x.id===s.event;}),box=el.querySelector('.st-inspector');
  box.innerHTML=e?'<span class="st-inspector-label">'+esc(e.date)+' · 歷史規則重播</span><h4 style="color:'+colors[e.type]+'">'+names[e.type]+'訊號 '+price(e.price)+'</h4><p>'+esc(e.reason)+'</p><div><span>當時保護價 <b>'+price(e.stop)+'</b></span><span>當時停利參考 <b>'+price(e.target)+'</b></span></div><button type="button" class="st-current">回到目前計畫</button>':'<span class="st-inspector-label">目前計畫</span><h4>'+esc(r.state)+'</h4><p>'+(r.active?'追蹤的是規則重播中的策略部位，不是你的實際持股。':'等待條件完整成立才畫進場箭頭，觀察價不是立即買進指示。')+'</p><div><span>保護價 <b>'+price(r.stop)+'</b></span><span>停利參考 <b>'+price(r.target)+'</b></span></div>';
  var b=box.querySelector('.st-current');if(b)b.onclick=function(){s.event=null;s.end=null;inspect(s,r,el);draw(s,r,el);};
 }
 function select(s,r,id,el){s.event=id;var event=r.signals.find(function(e){return e.id===id;});var index=r.bars.findIndex(function(b){return b.date===event.date;});if(index>=0)s.end=Math.min(r.bars.length,Math.max(s.range,index+6));inspect(s,r,el);draw(s,r,el);}
 function draw(s,r,el){
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
  [[stop,'保護','#13805f'],[target,event?'當時停利':'停利參考','#a75916']].forEach(function(a){if(a[0]>=min&&a[0]<=max){var yy=y(a[0]);svg+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+yy+'" y2="'+yy+'" stroke="'+a[2]+'" stroke-dasharray="5 5" opacity=".8"/><text x="'+(W-R+5)+'" y="'+(yy-5)+'" fill="'+a[2]+'" font-size="12">'+a[1]+'</text>';}});
  if(!event&&r.support>=min&&r.support<=max){svg+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+y(r.support)+'" y2="'+y(r.support)+'" stroke="#647db0" stroke-dasharray="2 5" opacity=".65"/><text x="'+(L+3)+'" y="'+(y(r.support)-5)+'" fill="#647db0" font-size="11">目前支撐 '+price(r.support)+'</text>';}
  var trail=(r.trailing||[]).filter(function(t){return t.date>=bars[0].date&&t.date<=bars[n-1].date&&t.value>=min&&t.value<=max;});
  var d='';trail.forEach(function(t,i){var ix=bars.findIndex(function(b){return b.date===t.date;});if(ix>=0)d+=(i?'H'+x(ix)+'V'+y(t.value):'M'+x(ix)+','+y(t.value));});
  if(d)svg+='<path d="'+d+'" fill="none" stroke="#13805f" opacity=".7" stroke-width="1.7"/>';
  (r.signals||[]).filter(function(e){return e.date>=bars[0].date&&e.date<=bars[n-1].date;}).slice(-12).forEach(function(e){var i=bars.findIndex(function(b){return b.date===e.date;}),up=e.type==='entry'||e.type==='add',xx=x(i),yy=y(up?bars[i].low:bars[i].high)+(up?14:-14),path=up?'M-7,6L0,-6L7,6Z':'M-7,-6L0,6L7,-6Z';svg+='<g transform="translate('+xx+','+yy+')"><g data-marker="'+esc(e.id)+'" tabindex="0" role="button" aria-label="'+esc(e.date+' '+names[e.type]+' '+price(e.price))+'"><rect x="-22" y="-22" width="44" height="44" fill="transparent"/><path d="'+path+'" fill="'+colors[e.type]+'"/><text y="'+(up?24:-12)+'" text-anchor="middle" font-size="12" fill="'+colors[e.type]+'">'+names[e.type]+'</text></g></g>';});
  [0,Math.floor(n/2),n-1].forEach(function(i){svg+='<text class="st-axis" x="'+x(i)+'" y="328" text-anchor="'+(i===0?'start':i===n-1?'end':'middle')+'">'+esc(bars[i].date.slice(5))+'</text>';});
  svg+='<line class="st-cross" x1="0" x2="0" y1="'+T+'" y2="'+V+'" visibility="hidden"/><text class="st-axis" x="'+L+'" y="18">10 日線　20 日線　綠階梯：移動保護價</text></svg>';
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
  fetch(api(force),{credentials:'same-origin',cache:'no-store',signal:controller.signal}).then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}).then(function(data){if(!data.ok)throw new Error(data.error||'載入失敗');if(!alive(s)||id!==s.request)return;s.data=data;s.fetched=Date.now();render(s);if(data.meta&&data.meta.building&&s.polls++<90)s.timer=setTimeout(function(){s.timer=null;if(!s.el.hidden)load(s,false);},5000);}).catch(function(e){if(alive(s)){s.data.meta=Object.assign({},s.data.meta,{building:false,error:e.name==='AbortError'?'讀取逾時，請重試；原結果保留':'暫時無法載入，請重試'});render(s);}}).finally(function(){clearTimeout(timeout);s.loading=false;if(alive(s)){var b=s.el.querySelector('.st-refresh');if(b){b.disabled=false;b.textContent='重新篩選';}}});
 }
 window.StockBotShortTerm={mount:function(el){if(current&&current.el===el){if(!current.loading&&!current.timer&&((current.data.meta||{}).building||Date.now()-(current.fetched||0)>30000))load(current,false);return;}current={el:el,data:{rows:[],meta:{building:true}},open:null,range:60,end:null,event:null,request:0,loading:false,polls:0,timer:null,id:++sequence};render(current);load(current,false);},refresh:function(el){this.mount(el);current.polls=0;load(current,true);}};
 var panel=document.getElementById('wb-short-term');if(panel&&!panel.hidden)window.StockBotShortTerm.mount(panel);
})();
