"""Authenticated web page and bounded cron entry point for US industry rotation."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone

from flask import Blueprint, abort, jsonify, request

from .calculations import calculate_snapshot
from .config import ALGORITHM_VERSION, CHAIN_CATEGORIES, INDUSTRIES, SOURCE_NAME
from .providers import fetch_market_histories
from .repository import (read_history, read_latest, read_latest_tw_company_snapshots,
                         read_supply_chain, save_snapshot, save_tw_company_snapshots)


def _esc(value):
    return (str(value if value is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def create_blueprint(*, web_login_required, render_page, get_db_connection,
                     release_db_connection, run_in_background,
                     fetch_tw_company_snapshot):
    bp = Blueprint("us_rotation", __name__)

    def _page_html():
        return r'''<style>
.us-radar{max-width:1100px;margin:0 auto;padding:4px 0 24px;color:var(--ink,#243647)}
.us-hero{border-radius:20px;padding:20px;background:linear-gradient(128deg,var(--hero,#173F5D),var(--hero-soft,#276987) 58%,var(--ui-blue,#4F8D9D));color:#fff;box-shadow:0 12px 30px rgba(25,63,91,.16)}
.us-kicker{font-size:10px;font-weight:900;letter-spacing:.15em;opacity:.78}.us-hero h1{font-size:24px;line-height:1.3;margin:8px 0}.us-hero p{font-size:13px;line-height:1.75;margin:0;opacity:.9}
.us-meta{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}.us-pill{border:1px solid rgba(255,255,255,.28);background:rgba(255,255,255,.12);border-radius:99px;padding:6px 10px;font-size:10px;font-weight:800}
.us-section{margin-top:16px;padding:16px;border:1px solid var(--ui-line,#DCE3E9);border-radius:16px;background:var(--ui-surface,#fff);box-shadow:0 5px 16px rgba(32,58,79,.045)}
.us-section-head{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;margin-bottom:12px}.us-section h2{font-size:16px;margin:0;font-weight:950}.us-section-head p{font-size:11px;color:var(--ink-faint,#708092);margin:5px 0 0;line-height:1.65}
.us-periods{display:flex;background:var(--ui-page,#EEF3F6);border-radius:10px;padding:3px;gap:2px}.us-periods button{border:0;border-radius:8px;background:transparent;padding:7px 10px;color:var(--ink-soft,#566B7D);font-size:11px;font-weight:900}.us-periods button.on{background:var(--ui-blue,#245779);color:white;box-shadow:0 2px 6px rgba(24,63,93,.2)}
.us-summary-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:9px}.us-summary{padding:11px;border:1px solid var(--ui-line,#E1E8ED);background:var(--ui-page,#F8FAFB);border-radius:12px}.us-summary small{display:block;color:var(--ink-faint,#718294);font-weight:800;font-size:10px}.us-summary b{display:block;font-size:18px;margin-top:5px}.us-summary em{display:block;color:var(--ink-faint,#768493);font-size:10px;font-style:normal;margin-top:3px}
.us-heat{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}.us-heat-card{cursor:pointer;min-width:0;text-align:left;border:1px solid var(--ui-line,#DDE6EB);border-radius:12px;padding:10px;background:var(--ui-surface-soft,#F9FBFC);color:inherit;transition:transform .15s,border-color .15s}.us-heat-card:hover,.us-heat-card.selected{transform:translateY(-1px);border-color:var(--ui-blue,#4C85A5);box-shadow:0 4px 12px rgba(30,82,112,.09)}
.us-heat-top{display:flex;justify-content:space-between;gap:4px;align-items:center}.us-heat-top b{font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.us-heat-top small{font-size:9px;color:#738394}.us-heat-value{font-size:19px;font-weight:950;margin:8px 0 4px}.us-heat-foot{display:flex;justify-content:space-between;gap:4px;color:#66798A;font-size:9px;font-weight:800}.us-positive{color:#0B8159!important}.us-negative{color:#C74343!important}.us-neutral{color:#708092!important}
.us-rank-list{display:grid;gap:7px}.us-rank-row{display:grid;grid-template-columns:30px minmax(94px,1fr) minmax(75px,1.4fr) 66px 78px;align-items:center;gap:8px;padding:9px 8px;border-bottom:1px solid #E7EDF1;font-size:11px}.us-rank-no{font-weight:950;color:#7890A1}.us-rank-name b{display:block;font-size:12px}.us-rank-name small{color:#7A8996;font-size:9px}.us-rank-track{height:6px;background:#E9EFF3;border-radius:99px;overflow:hidden}.us-rank-track i{display:block;height:100%;background:linear-gradient(90deg,#72B7AE,#287DA0);border-radius:99px}.us-rank-score,.us-state{font-weight:900;text-align:right}.us-state{font-size:9px;color:#496779}
.us-chart{width:100%;height:auto;display:block;overflow:visible}.us-grid-line{stroke:#E4EBEF;stroke-width:1}.us-chart-label{fill:#7D8C9A;font-size:10px;font-weight:700}.us-axis-zero{stroke:#A9BAC5;stroke-dasharray:4 4}.us-dot{stroke:#fff;stroke-width:2}.us-quad-label{fill:#728494;font-size:9px;font-weight:800}.us-selected{fill:#1F6284}.us-tooltip{font-size:10px;fill:#233B4D;font-weight:850}
.us-detail{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.1fr);gap:14px}.us-detail-box{border:1px solid #E2E9EE;background:#FAFBFC;border-radius:12px;padding:12px}.us-detail-box h3{font-size:13px;margin:0 0 8px}.us-detail-box p,.us-detail-box li{font-size:11px;line-height:1.8;color:#516779}.us-detail-box ul{padding-left:18px;margin:0}.us-company{display:flex;gap:8px;justify-content:space-between;align-items:flex-start;padding:9px 0;border-top:1px solid #E6EDF1}.us-company:first-of-type{border-top:0}.us-company b{font-size:12px}.us-company small{display:block;color:#718393;font-size:10px;margin-top:3px}.us-company a{font-size:10px;font-weight:900;color:#286D9A;text-decoration:none;white-space:nowrap}
.us-empty{padding:20px 14px;border:1px dashed var(--ui-line,#C8D5DE);border-radius:13px;background:var(--ui-page,#F7FAFC);text-align:center}.us-empty b{font-size:13px}.us-empty p{font-size:11px;line-height:1.8;color:var(--ink-soft,#687C8D);margin:7px 0 0}.us-note{font-size:10px;line-height:1.8;color:var(--ink-faint,#748493);margin:10px 0 0}.us-source{display:flex;align-items:flex-start;gap:8px;padding:10px;border-radius:10px;background:#FFF7E7;border:1px solid #E9D8B4;color:#765E2D;font-size:10px;line-height:1.75}
.us-history{overflow:auto}.us-history table{width:100%;border-collapse:collapse;font-size:10px}.us-history th,.us-history td{padding:7px 5px;border-bottom:1px solid #E6EDF1;text-align:right;white-space:nowrap}.us-history th:first-child,.us-history td:first-child{text-align:left}.us-loading{height:5px;border-radius:99px;background:linear-gradient(90deg,#E4EDF3,#337D9C,#E4EDF3);background-size:200% 100%;animation:us-shimmer 1.2s linear infinite}@keyframes us-shimmer{to{background-position:-200% 0}}
#app-page-content .app-header{display:none!important}
.us-summary-grid>.us-empty,.us-heat>.us-empty{grid-column:1/-1;width:100%;min-width:0;box-sizing:border-box}
@media(max-width:700px){.us-hero{border-radius:17px;padding:17px}.us-hero h1{font-size:21px}.us-section{padding:13px;margin-top:12px;border-radius:14px}.us-summary-grid{grid-template-columns:repeat(3,minmax(0,1fr));gap:6px}.us-summary{padding:9px 7px}.us-summary b{font-size:15px}.us-heat{grid-template-columns:repeat(2,minmax(0,1fr));gap:7px}.us-heat-card{padding:9px}.us-heat-value{font-size:18px}.us-rank-row{grid-template-columns:24px minmax(70px,1fr) 52px 64px;gap:6px;padding:8px 3px}.us-rank-track{display:none}.us-detail{grid-template-columns:1fr}.us-periods button{padding:7px 8px;font-size:10px}.us-chart-label{font-size:9px}}
@media(prefers-reduced-motion:reduce){.us-heat-card,.us-loading{animation:none!important;transition:none!important}}
</style>
<main class="us-radar" id="us-radar" data-api="/web/api/us-rotation">
  <section class="us-hero">
    <div class="us-kicker">US → TW INTELLIGENCE</div><h1>美股輪動雷達</h1>
    <p>追蹤美國產業強弱，接著檢視台灣產品與供應鏈證據。產業走強只代表研究線索，不是台股買進訊號。</p>
    <div class="us-meta"><span class="us-pill" id="us-asof">資料日期載入中</span><span class="us-pill">日線 · 交易日計算</span><span class="us-pill">SPY 為市場基準</span></div>
  </section>
  <section class="us-section">
    <div class="us-section-head"><div><h2>市場輪動概況</h2><p id="us-status">載入最近完成的產業快照</p></div></div>
    <div class="us-summary-grid" id="us-summary"></div>
    <div class="us-source" style="margin-top:11px"><b>資料提醒</b><span>產業 ETF 是代理指標，調整收盤價的股息處理依來源而異；這不是即時行情。資料來源使用條款確認前，不會啟用自動更新。</span></div>
  </section>
  <section class="us-section"><div class="us-section-head"><div><h2>產業熱度圖</h2><p>紅綠採美股習慣：上漲綠、下跌紅；半導體為資訊科技子產業觀察，獨立標示。</p></div><div class="us-periods" role="group" aria-label="報酬期間"><button data-period="5">5 日</button><button class="on" data-period="20">20 日</button><button data-period="60">60 日</button></div></div><div class="us-heat" id="us-heat"></div></section>
  <section class="us-section"><div class="us-section-head"><div><h2>相對強度排行榜</h2><p>相對 SPY 報酬；排名依透明分數，市場廣度缺資料時不當成零分。</p></div></div><div class="us-rank-list" id="us-ranking"></div><p class="us-note" id="us-score-note"></p></section>
  <section class="us-section"><div class="us-section-head"><div><h2>輪動象限</h2><p>橫軸：20 日相對 SPY 報酬；縱軸：最近 20 日相對強度減去前 20 日。</p></div></div><div id="us-quadrant"></div></section>
  <section class="us-section"><div class="us-section-head"><div><h2>產業與台灣供應鏈</h2><p id="us-detail-title">點選上方任一產業查看產品關聯與證據</p></div></div><div id="us-detail"></div></section>
  <section class="us-section"><div class="us-section-head"><div><h2>美台相對表現落差</h2><p>只比較已有產業映射、時間一致的實際報酬；不把落後解讀成必然補漲。</p></div></div><div id="us-gap"></div></section>
  <section class="us-section"><div class="us-section-head"><div><h2>輪動排名歷史</h2><p>每日快照累積後才形成趨勢；首次建立時歷史不足會明確標示。</p></div></div><div class="us-history" id="us-history"></div></section>
  <p class="us-note">研究工具，不構成投資建議。趨勢狀態是規則分類，不是預測；營收、法人與估值須以各自公布日及資料時間為準。</p>
</main>
<script>
(function(){
 const root=document.getElementById('us-radar');if(!root)return;
 const $=s=>root.querySelector(s),esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const fmt=(v,d=2)=>v==null?'—':(Number(v)>0?'+':'')+Number(v).toFixed(d)+'%';
 const cls=v=>v==null?'us-neutral':Number(v)>0?'us-positive':Number(v)<0?'us-negative':'us-neutral';
 let state={snapshot:null,history:[],mapping:[],twCompanies:[],period:20,selected:null};
 function empty(el,title,copy){el.innerHTML='<div class="us-empty"><b>'+esc(title)+'</b><p>'+esc(copy)+'</p></div>';}
 function renderSummary(data){const rows=data.industries||[];const scored=rows.filter(x=>!x.overlay&&x.score!=null).sort((a,b)=>a.rank-b.rank);const top=scored[0],weak=scored[scored.length-1],turn=rows.find(x=>x.trend_state==='轉強觀察');const items=[['目前領先',top?top.name:'—',top?fmt(top.relative_20d_pct):'尚無快照'],['轉強觀察',turn?turn.name:'—',turn?fmt(turn.relative_20d_pct):'等待連續資料'],['相對落後',weak?weak.name:'—',weak?fmt(weak.relative_20d_pct):'尚無快照']];$('#us-summary').innerHTML=items.map(x=>'<div class="us-summary"><small>'+esc(x[0])+'</small><b>'+esc(x[1])+'</b><em>'+esc(x[2])+'</em></div>').join('');}
 function renderHeat(){const rows=state.snapshot&&state.snapshot.industries||[];if(!rows.length){empty($('#us-heat'),'尚無美股產業快照','待資料來源條件確認並完成第一次批次更新後，這裡會顯示 12 個產業代理 ETF。');return;}
  const key='return_'+state.period+'d_pct';$('#us-heat').innerHTML=rows.map(r=>'<button class="us-heat-card '+(state.selected===r.id?'selected':'')+'" data-industry="'+esc(r.id)+'"><span class="us-heat-top"><b>'+esc(r.name)+'</b><small>'+esc(r.ticker)+(r.overlay?' · 子產業':'')+'</small></span><div class="us-heat-value '+cls(r[key])+'">'+esc(fmt(r[key]))+'</div><span class="us-heat-foot"><span>相對 SPY</span><b class="'+cls(r['relative_'+state.period+'d_pct'])+'">'+esc(fmt(r['relative_'+state.period+'d_pct']))+'</b></span></button>').join('');root.querySelectorAll('.us-heat-card').forEach(b=>b.addEventListener('click',()=>selectIndustry(b.dataset.industry)));}
 function renderRanking(){const rows=(state.snapshot&&state.snapshot.industries||[]).filter(x=>!x.overlay).sort((a,b)=>(a.rank||99)-(b.rank||99));if(!rows.length){empty($('#us-ranking'),'尚無可排名資料','資料不足時不會補成零分。');return;}
  const max=Math.max(1,...rows.map(x=>Number(x.score)||0));$('#us-ranking').innerHTML=rows.map(r=>'<div class="us-rank-row"><span class="us-rank-no">'+(r.rank||'—')+'</span><span class="us-rank-name"><b>'+esc(r.name)+'</b><small>'+esc(r.ticker)+' · '+esc(r.quadrant||'資料不足')+'</small></span><span class="us-rank-track"><i style="width:'+Math.max(1,(Number(r.score)||0)/max*100)+'%"></i></span><b class="us-rank-score">'+(r.score==null?'—':Number(r.score).toFixed(0)+' 分')+'</b><span class="us-state">'+esc(r.trend_state||'資料不足')+'</span></div>').join('');$('#us-score-note').textContent=(state.snapshot.score_note||'')+' 權重覆蓋度逐產業顯示；廣度未提供，當前最高分覆蓋 90%。';}
 function renderQuadrant(){const rows=(state.snapshot&&state.snapshot.industries||[]).filter(r=>!r.overlay&&r.relative_20d_pct!=null&&r.relative_momentum_20d_pct!=null);if(rows.length<2){empty($('#us-quadrant'),'象限資料不足','至少需要 20 日相對報酬及前後兩個 20 日區間。');return;}
  const w=560,h=250,p={l:42,r:16,t:18,b:32},xs=rows.map(r=>Number(r.relative_20d_pct)),ys=rows.map(r=>Number(r.relative_momentum_20d_pct));let xmax=Math.max(1,...xs.map(Math.abs))*1.18,ymax=Math.max(1,...ys.map(Math.abs))*1.18;const X=v=>p.l+(v+xmax)/(2*xmax)*(w-p.l-p.r),Y=v=>p.t+(ymax-v)/(2*ymax)*(h-p.t-p.b);let svg='<svg class="us-chart" viewBox="0 0 '+w+' '+h+'" role="img" aria-label="產業相對強度輪動象限"><line class="us-axis-zero" x1="'+X(0)+'" x2="'+X(0)+'" y1="'+p.t+'" y2="'+(h-p.b)+'"/><line class="us-axis-zero" x1="'+p.l+'" x2="'+(w-p.r)+'" y1="'+Y(0)+'" y2="'+Y(0)+'"/><text class="us-quad-label" x="'+(p.l+6)+'" y="'+(p.t+12)+'">改善</text><text class="us-quad-label" x="'+(w-p.r-42)+'" y="'+(p.t+12)+'">領先</text><text class="us-quad-label" x="'+(p.l+6)+'" y="'+(h-p.b-5)+'">落後</text><text class="us-quad-label" x="'+(w-p.r-42)+'" y="'+(h-p.b-5)+'">轉弱</text><text class="us-chart-label" x="'+(w/2)+'" y="'+(h-3)+'" text-anchor="middle">20 日相對 SPY 報酬</text><text class="us-chart-label" transform="translate(11 '+(h/2)+') rotate(-90)" text-anchor="middle">相對強度變化</text>';
  svg+=rows.map(r=>'<circle class="us-dot" cx="'+X(Number(r.relative_20d_pct))+'" cy="'+Y(Number(r.relative_momentum_20d_pct))+'" r="6" fill="'+(r.quadrant==='領先'?'#17856A':r.quadrant==='改善'?'#3D94A2':r.quadrant==='轉弱'?'#D28B46':'#8999A6')+'"><title>'+esc(r.name)+' · '+esc(fmt(r.relative_20d_pct))+' · '+esc(r.quadrant)+'</title></circle><text class="us-tooltip" x="'+(X(Number(r.relative_20d_pct))+8)+'" y="'+(Y(Number(r.relative_momentum_20d_pct))-6)+'">'+esc(r.name)+'</text>').join('')+'</svg>';
  $('#us-quadrant').innerHTML=svg;}
 function renderDetail(){const id=state.selected||((state.snapshot&&state.snapshot.industries||[])[0]||{}).id;const r=(state.snapshot&&state.snapshot.industries||[]).find(x=>x.id===id);if(!r){empty($('#us-detail'),'尚無產業資料','資料載入完成後，點選產業卡片。');return;}state.selected=r.id;$('#us-detail-title').textContent=r.name+' · '+r.ticker+' · '+(r.gics||'');const links=state.mapping.filter(x=>x.us_industry===r.id);const byCode={};(state.twCompanies||[]).forEach(x=>byCode[x.code]=x);const mapHtml=links.length?links.map(x=>{const d=byCode[x.code]||{};const metrics=d.code?'<small>台股資料日 '+esc(d.price_date||d.as_of_date||'—')+' · 收盤 '+esc(d.close==null?'—':Number(d.close).toFixed(2))+' · 20 日 '+esc(fmt(d.return_20d_pct))+'</small><small>月營收年增 '+esc(fmt(d.revenue_yoy_pct))+'（'+esc(d.revenue_period||'資料月未標')+'） · 法人合計 '+esc(d.total_net_lots==null?'—':Number(d.total_net_lots).toLocaleString()+' 張')+'</small><small>MA5 / 20 / 60：'+esc(d.ma5==null?'—':Number(d.ma5).toFixed(2))+' / '+esc(d.ma20==null?'—':Number(d.ma20).toFixed(2))+' / '+esc(d.ma60==null?'—':Number(d.ma60).toFixed(2))+' · PE / PB / 殖利率：'+esc(d.pe==null?'—':Number(d.pe).toFixed(1))+' / '+esc(d.pb==null?'—':Number(d.pb).toFixed(2))+' / '+esc(d.dividend_yield_pct==null?'—':Number(d.dividend_yield_pct).toFixed(2)+'%')+'</small><small>EPS、毛利率、營業利益率：尚無可確認欄位；缺資料不補值。</small>':'<small>尚無台股觀察快照；產品關聯有證據，但行情與基本面尚未同步。</small>';return '<div class="us-company"><span><b>'+esc(x.name)+'（'+esc(x.code)+'）</b><small>'+esc(x.chain)+' · '+esc(x.product)+' · '+esc(x.role)+'</small><small>'+esc(x.confidence)+'；'+esc(x.reason)+'</small>'+metrics+'</span><a href="'+esc(x.evidence_url)+'" target="_blank" rel="noopener">證據 ↗</a></div>';}).join(''):'<p>目前沒有已建檔的台灣公司證據關聯；不會只因題材相同就列為供應商。</p>';
  $('#us-detail').innerHTML='<div class="us-detail"><div class="us-detail-box"><h3>美股產業表現</h3><p>資料日 '+esc(r.as_of||'—')+' · 狀態 '+esc(r.trend_state||'資料不足')+' · '+esc(r.quadrant||'資料不足')+'</p><p>5 日 '+esc(fmt(r.return_5d_pct))+'（相對 '+esc(fmt(r.relative_5d_pct))+'）<br>20 日 '+esc(fmt(r.return_20d_pct))+'（相對 '+esc(fmt(r.relative_20d_pct))+'）<br>60 日 '+esc(fmt(r.return_60d_pct))+'（相對 '+esc(fmt(r.relative_60d_pct))+'）</p><p>ETF 相對 20 日均量：'+(r.relative_volume==null?'—':Number(r.relative_volume).toFixed(2)+' 倍')+'；廣度：資料不足。</p><p>目前沒有與此產業有日期及來源佐證的事件資料；無法確認主要漲跌原因。</p></div><div class="us-detail-box"><h3>台灣產品與供應鏈證據</h3>'+mapHtml+'</div></div>';
 }
 function renderHistory(){const history=state.history||[];if(history.length<2){empty($('#us-history'),'歷史快照尚在累積','至少保存兩個更新日，才顯示產業名次變化；目前不使用今日成分反推過去。');return;}const dates=history.slice(-12);const keys=dates.map(x=>x.as_of);const topIds=(dates[dates.length-1].industries||[]).filter(x=>!x.overlay).sort((a,b)=>(a.rank||99)-(b.rank||99)).slice(0,5).map(x=>x.id);let html='<table><thead><tr><th>產業</th>'+keys.map(x=>'<th>'+esc(x.slice(5))+'</th>').join('')+'</tr></thead><tbody>';topIds.forEach(id=>{const nm=((dates[dates.length-1].industries||[]).find(x=>x.id===id)||{}).name||id;html+='<tr><td>'+esc(nm)+'</td>'+dates.map(day=>{const row=(day.industries||[]).find(x=>x.id===id);return '<td>'+(row&&row.rank?'第 '+row.rank+' 名':'—')+'</td>';}).join('')+'</tr>';});$('#us-history').innerHTML=html+'</tbody></table>';}
 function renderGap(){const mapping=state.mapping||[];const snap=state.snapshot;if(!snap){empty($('#us-gap'),'尚無可比較快照','需先有美股與台股同期報酬資料。');return;}const tw={};(state.twCompanies||[]).forEach(x=>tw[x.code]=x);const industries={};(snap.industries||[]).forEach(x=>industries[x.id]=x);const observations=mapping.map(m=>{const u=industries[m.us_industry],t=tw[m.code];if(!u||!t||u.return_20d_pct==null||t.return_20d_pct==null)return null;return {map:m,us:u,tw:t,gap:Number(u.return_20d_pct)-Number(t.return_20d_pct)};}).filter(x=>x&&x.us.return_20d_pct>0&&x.gap>=5).sort((a,b)=>b.gap-a.gap);if(!observations.length){empty($('#us-gap'),'目前沒有資料完整的落差候選','只在美股產業 20 日上漲、台股價格與營收資料均可對齊時顯示。資料日期可能不同，落後不是補漲訊號。');return;}$('#us-gap').innerHTML=observations.map(x=>'<div class="us-company"><span><b>'+esc(x.map.name)+'（'+esc(x.map.code)+'） · 觀察差距 '+esc(x.gap.toFixed(2))+' 個百分點</b><small>'+esc(x.map.chain)+' · 美股 '+esc(x.map.name)+' 所屬產業代理 '+esc(fmt(x.us.return_20d_pct))+'（截至 '+esc(x.us.as_of||'—')+'），台股 '+esc(fmt(x.tw.return_20d_pct))+'（截至 '+esc(x.tw.price_date||'—')+'）</small><small>月營收年增 '+esc(fmt(x.tw.revenue_yoy_pct))+'；法人合計 '+esc(x.tw.total_net_lots==null?'資料不足':Number(x.tw.total_net_lots).toLocaleString()+' 張')+'。可能原因與估值需另查；此差距不代表補漲。</small><small>供應鏈產品證據：'+esc(x.map.confidence)+'；特定客戶／直接供貨關係未確認。</small></span><a href="'+esc(x.map.evidence_url)+'" target="_blank" rel="noopener">證據 ↗</a></div>').join('');}
 function selectIndustry(id){state.selected=id;renderHeat();renderDetail();}
 function draw(){if(!state.snapshot){$('#us-asof').textContent='資料來源尚未啟用';$('#us-status').textContent='尚未有完成快照';empty($('#us-summary'),'等待第一筆可信資料','頁面只讀已完成快照，不會在開頁時同步抓行情。');renderHeat();renderRanking();renderQuadrant();renderDetail();renderGap();renderHistory();return;}const s=state.snapshot;$('#us-asof').textContent='美股交易日 '+(s.as_of||'—');$('#us-status').textContent='最後更新 '+(s.fetched_at||'—')+' · '+(s.source||'未知來源');renderSummary(s);renderHeat();renderRanking();renderQuadrant();renderDetail();renderGap();renderHistory();}
 fetch(root.dataset.api+'?t='+encodeURIComponent(new URLSearchParams(location.search).get('t')||''),{credentials:'same-origin',cache:'no-store'}).then(r=>{if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}).then(d=>{state.snapshot=d.snapshot||null;state.history=d.history||[];state.mapping=d.supply_chain||[];state.twCompanies=d.tw_companies||[];draw();}).catch(()=>{draw();$('#us-status').textContent='載入失敗，請重新整理頁面';});
 root.querySelectorAll('[data-period]').forEach(b=>b.addEventListener('click',()=>{root.querySelectorAll('[data-period]').forEach(x=>x.classList.toggle('on',x===b));state.period=Number(b.dataset.period);renderHeat();}));
})();
</script>'''

    def _read_status():
        latest = read_latest(get_db_connection, release_db_connection)
        history = read_history(get_db_connection, release_db_connection, 30)
        mapping = read_supply_chain(get_db_connection, release_db_connection)
        tw_companies = read_latest_tw_company_snapshots(get_db_connection, release_db_connection)
        return latest, history, mapping, tw_companies

    def _refresh_job():
        from .providers import ProviderDisabled
        from .config import SOURCE_NAME
        allow = os.environ.get("US_ROTATION_YAHOO_TERMS_ACKNOWLEDGED", "").strip().lower() in {"1", "true", "yes"}
        histories, errors, fetched_at = fetch_market_histories(allow_unofficial=allow)
        if not histories:
            raise ProviderDisabled("行情來源尚未啟用或所有代號抓取失敗")
        snapshot = calculate_snapshot(histories, source=SOURCE_NAME, fetched_at=fetched_at)
        save_snapshot(get_db_connection, release_db_connection, snapshot, histories, errors)
        mappings = read_supply_chain(get_db_connection, release_db_connection)
        codes = sorted({str(item.get("code") or "") for item in mappings if item.get("code")})
        taiwan_snapshot = fetch_tw_company_snapshot(codes)
        if taiwan_snapshot:
            save_tw_company_snapshots(
                get_db_connection, release_db_connection,
                taiwan_snapshot["as_of_date"], taiwan_snapshot["rows"],
                taiwan_snapshot.get("source_meta"))
        return ("美股產業快照完成：%s 個代理 ETF；台股觀察資料 %s 檔；美股未取得：%s" %
                (len(snapshot.get("industries") or []),
                 len((taiwan_snapshot or {}).get("rows") or []),
                 ", ".join(sorted(errors)) or "無"))

    @bp.route("/web/us-rotation")
    @web_login_required
    def page(uid):
        return render_page("美股輪動", _page_html(), "us_rotation")

    @bp.route("/web/api/us-rotation")
    @web_login_required
    def api(uid):
        try:
            latest, history, mapping, tw_companies = _read_status()
            return jsonify({"ok": True, "snapshot": latest, "history": history,
                            "supply_chain": mapping, "tw_companies": tw_companies,
                            "chain_categories": CHAIN_CATEGORIES,
                            "algorithm_version": ALGORITHM_VERSION,
                            "last_updated": datetime.now(timezone.utc).isoformat()})
        except Exception as exc:
            return jsonify({"ok": False, "error": "資料暫時無法讀取", "type": type(exc).__name__}), 503

    @bp.route("/cron/us-rotation-refresh", methods=["GET", "POST"])
    def cron_refresh():
        token = request.args.get("token") or request.headers.get("X-Cron-Token")
        if not token or token != os.environ.get("CRON_SECRET"):
            abort(403)
        return run_in_background("美股產業輪動快照", _refresh_job), 202

    return bp

