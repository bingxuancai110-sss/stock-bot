"""Authenticated web page and bounded cron entry point for US industry rotation."""

from __future__ import annotations

import json
import hashlib
import hmac
import os
from datetime import date, datetime, timezone

from flask import Blueprint, Response, abort, jsonify, request

from .calculations import calculate_snapshot
from .config import ALGORITHM_VERSION, CHAIN_CATEGORIES, INDUSTRIES, SOURCE_NAME
from .providers import fetch_market_histories
from .importer import ImportValidationError, csv_template, parse_history_csv
from .repository import (read_history, read_latest, read_latest_tw_company_snapshots,
                         read_supply_chain, save_snapshot, save_tw_company_snapshots)
from .risk_warning import (HISTORICAL_RISK_PROFILES, calculate_current_risk,
                           describe_current_risk, public_stop_profiles)


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
.us-alert-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.us-alert-card{border:1px solid #E4D8CB;background:#FFF9F2;border-radius:13px;padding:13px}.us-alert-card.signal-on{border:2px solid #B84242;background:#FFF0EE}.us-alert-card.confirm-on{border:2px solid #23836C;background:#EFF9F5}.us-alert-head{display:flex;justify-content:space-between;gap:8px;align-items:center}.us-alert-head h3{font-size:13px;margin:0}.us-alert-badge{font-size:10px;font-weight:950;padding:5px 8px;border-radius:99px;background:#EEF1F3;color:#5B6B78}.us-alert-badge.alert{background:#B84242;color:#fff}.us-alert-badge.confirmed{background:#23836C;color:#fff}.us-alert-value{font-size:19px;font-weight:950;margin:10px 0;color:#334B5C}.us-alert-value.alert-text{color:#B84242}.us-alert-value.confirm-text{color:#18745F}.us-alert-stat{display:flex;justify-content:space-between;gap:8px;padding:5px 0;border-top:1px solid rgba(90,110,125,.12);font-size:10px}.us-alert-foot{font-size:9px;line-height:1.7;color:#6E7B85;margin:8px 0 0}
#app-page-content .app-header,#app-page-content .top-nav,#app-page-content footer,#app-page-content .app-bottom-nav{display:none!important}
.us-summary-grid>.us-empty,.us-heat>.us-empty{grid-column:1/-1;width:100%;min-width:0;box-sizing:border-box}
@media(max-width:700px){.us-hero{border-radius:17px;padding:17px}.us-hero h1{font-size:21px}.us-section{padding:13px;margin-top:12px;border-radius:14px}.us-summary-grid{grid-template-columns:repeat(3,minmax(0,1fr));gap:6px}.us-summary{padding:9px 7px}.us-summary b{font-size:15px}.us-heat{grid-template-columns:repeat(2,minmax(0,1fr));gap:7px}.us-heat-card{padding:9px}.us-heat-value{font-size:18px}.us-rank-row{grid-template-columns:24px minmax(70px,1fr) 52px 64px;gap:6px;padding:8px 3px}.us-rank-track{display:none}.us-detail{grid-template-columns:1fr}.us-alert-grid{grid-template-columns:1fr}.us-periods button{padding:7px 8px;font-size:10px}.us-chart-label{font-size:9px}}
@media(prefers-reduced-motion:reduce){.us-heat-card,.us-loading{animation:none!important;transition:none!important}}
/* Scope controls explicitly: the app-wide button theme must not override radar states. */
#app-page-content #us-radar{max-width:1080px;padding-bottom:calc(96px + env(safe-area-inset-bottom));font-variant-numeric:tabular-nums}
#us-radar .us-hero{padding:22px;border-radius:18px;box-shadow:none;background:linear-gradient(120deg,#204d66,#35758a)}
#us-radar .us-hero h1{color:#fff;font-size:24px;letter-spacing:.01em}
#us-radar .us-hero p{max-width:620px;font-size:13px;line-height:1.8}
#us-radar .us-meta{gap:6px;margin-top:16px}#us-radar .us-pill{white-space:nowrap;line-height:1.4}
#us-radar .us-section{padding:20px;border-radius:16px;margin-top:16px;box-shadow:none}
#us-radar .us-section-head{gap:12px;margin-bottom:16px;min-width:0}#us-radar .us-section-head>div:first-child{min-width:0}
#us-radar .us-section-head h2{font-size:18px;line-height:1.5}#us-radar .us-section-head p{font-size:12px;line-height:1.7;overflow-wrap:anywhere}
#us-radar .us-source{display:block;padding:12px 14px;background:#f5f8fa;border-color:#e0e8ed;color:#617586;font-size:11px;line-height:1.8}
#us-radar .us-source b{display:block;white-space:nowrap;color:#435d70;margin-bottom:3px}#us-radar .us-source span{display:block;min-width:0}
#us-radar .us-summary-grid{gap:10px}#us-radar .us-summary{padding:14px 12px;background:#f4f8fa;border:0;border-radius:12px;min-width:0}
#us-radar .us-summary b{font-size:18px;line-height:1.5;margin:6px 0;overflow-wrap:anywhere}#us-radar .us-summary em{font-style:normal;font-size:12px;color:#526b7d}
#app-page-content #us-radar button{font-family:inherit;-webkit-tap-highlight-color:transparent;text-shadow:none;filter:none;opacity:1;box-shadow:none;touch-action:manipulation;cursor:pointer}
#app-page-content #us-radar .us-periods{display:flex;flex:none;width:fit-content;padding:4px;gap:4px;background:#edf3f6;border-radius:12px}
#app-page-content #us-radar .us-periods button{flex:0 0 auto;min-width:62px;min-height:40px;margin:0!important;padding:8px 13px!important;border:0!important;border-radius:9px!important;background:transparent!important;color:#536b7c!important;font-size:12px!important;font-weight:800;white-space:nowrap;word-break:keep-all;line-height:1.2}
#app-page-content #us-radar .us-periods button.on{background:#dcecf3!important;color:#174c69!important;box-shadow:inset 0 0 0 1px #9fc3d5!important}
#app-page-content #us-radar .us-periods button:active{background:#cde4ee!important;transform:none!important}
#us-radar .us-heat{gap:10px}
#app-page-content #us-radar .us-heat-card{position:relative;display:block;width:100%;min-width:0;min-height:124px;margin:0!important;padding:14px!important;text-align:left;border:1px solid #dce6ec!important;border-radius:13px!important;background:#f8fbfc!important;color:#243f52!important;line-height:1.5;transform:none!important;transition:background .15s,border-color .15s}
#app-page-content #us-radar .us-heat-card.selected{background:#edf7fa!important;border:2px solid #3985a0!important;padding:13px!important;box-shadow:0 0 0 2px #e0f0f5!important}
#app-page-content #us-radar .us-heat-card:active,#app-page-content #us-radar .us-heat-card.is-pressed{background:#dfeff5!important;color:#243f52!important}
#app-page-content #us-radar .us-heat-card:hover{border-color:#88b7ca!important;background:#f0f7fa!important}
#app-page-content #us-radar button:focus-visible,#us-radar a:focus-visible,#us-radar summary:focus-visible{outline:3px solid #63a6c1!important;outline-offset:3px}
#us-radar .us-heat-top{display:flex;align-items:flex-start;gap:7px}#us-radar .us-heat-top b{font-size:13px;white-space:normal;overflow:visible;color:#243f52}
#us-radar .us-heat-top small{font-size:10px;white-space:nowrap;color:#6c8190;flex:none}
#us-radar .us-heat-value{font-size:24px;margin:10px 0 8px;letter-spacing:-.02em;line-height:1.2}
#us-radar .us-heat-foot{font-size:10px;gap:4px;flex-wrap:wrap}#us-radar .us-heat-foot>span{white-space:nowrap}
#us-radar .us-selection-note{font-size:12px;color:#326b86;margin:14px 0 0;line-height:1.6}
#us-radar .us-rank-row{grid-template-columns:26px minmax(100px,1fr) minmax(50px,.7fr) 60px 80px;gap:8px;padding:13px 0}
#us-radar .us-rank-name b{font-size:13px}#us-radar .us-rank-name small{display:block;font-size:10px;margin-top:3px}#us-radar .us-rank-score{font-size:13px}
#us-radar .us-state{font-size:11px;white-space:nowrap}#us-radar .us-chart{overflow:hidden;background:#f8fbfc;border-radius:12px}
#us-radar .us-quadrant-legend{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}
#app-page-content #us-radar .us-legend-button{display:inline-flex;align-items:center;gap:6px;margin:0!important;padding:6px 9px!important;min-height:34px;font-size:11px!important;font-weight:700;border:1px solid #dae6ec!important;border-radius:8px!important;background:#f7fafc!important;color:#3b596c!important}
#app-page-content #us-radar .us-legend-button[aria-pressed=true]{background:#e2f0f6!important;border-color:#7baec4!important;color:#174c69!important}
#us-radar .us-legend-dot{width:8px;height:8px;border-radius:50%;flex:none}
#us-radar .us-detail{gap:12px}#us-radar .us-detail-box{padding:16px;background:#f8fbfc}#us-radar .us-detail-box h3{font-size:15px}#us-radar .us-detail-box p{font-size:12px;line-height:1.85}
#us-radar .us-company{padding:14px 0;gap:12px;min-width:0}#us-radar .us-company>span{min-width:0;overflow-wrap:anywhere}#us-radar .us-company small{font-size:11px;line-height:1.8;margin-top:5px}#us-radar .us-company a{flex:none;white-space:nowrap;font-size:11px}
#us-radar .us-alert-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
#us-radar .us-alert-card{background:#fafcfd;border:1px solid #dce6ec;border-radius:14px;padding:18px;min-width:0}
#us-radar .us-alert-card.signal-on{background:#fff6f3;border:1px solid #d99b8b}#us-radar .us-alert-card.confirm-on{background:#f0faf6;border:1px solid #8ac4af}
#us-radar .us-alert-head{align-items:flex-start;gap:10px}#us-radar .us-alert-head h3{font-size:14px;line-height:1.65;color:#304e62;min-width:0}#us-radar .us-alert-badge{flex:none;white-space:nowrap;font-size:10px;line-height:1.4}
#us-radar .us-alert-value{font-size:27px;margin:16px 0 8px;line-height:1.3;letter-spacing:-.02em}#us-radar .us-alert-value small{font-size:12px;font-weight:700;margin-left:5px}
#us-radar .us-signal-condition{font-size:12px;line-height:1.8;color:#5b7181;margin:0}
#us-radar .us-signal-distance{padding:12px;margin:14px 0;background:#eaf3f7;border-radius:10px;font-size:13px;color:#285a73;line-height:1.7;font-weight:800}
#us-radar .signal-on .us-signal-distance{background:#fae7e0;color:#9a4b36}#us-radar .confirm-on .us-signal-distance{background:#e0f2ea;color:#236951}
#us-radar .us-signal-date{display:block;font-size:11px;line-height:1.8;color:#6d8190}
#us-radar .us-research{border:0;border-top:1px solid #dce6ec;border-radius:0;box-shadow:none;margin:14px 0 0;padding:12px 0 0;background:transparent}
#us-radar .us-research>summary{display:list-item;list-style:disclosure-closed;font-size:12px;font-weight:800;color:#49697f;cursor:pointer;padding:2px 0;background:transparent;border:0}
#us-radar .us-alert-stat{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:8px;font-size:11px;padding:9px 0;line-height:1.7}#us-radar .us-alert-stat b{text-align:right;overflow-wrap:anywhere}
#us-radar .us-alert-foot,#us-radar .us-note{font-size:11px;line-height:1.85}#us-radar .us-study-title{font-size:12px;color:#35596f;margin:16px 0 7px}
#us-radar .us-history table{font-size:11px}#us-radar .us-history th,#us-radar .us-history td{padding:10px 7px}
@media(max-width:700px){#us-radar .us-hero{padding:18px}#us-radar .us-hero h1{font-size:22px}#us-radar .us-section{padding:16px;margin-top:12px}#us-radar .us-section-head{flex-direction:column;align-items:stretch;gap:12px}#us-radar .us-section-head h2{font-size:17px}#us-radar .us-summary-grid{gap:7px}#us-radar .us-summary{padding:12px 9px}#us-radar .us-summary b{font-size:15px}#us-radar .us-summary small{font-size:10px}#us-radar .us-summary em{font-size:11px}#us-radar .us-heat{gap:9px}#app-page-content #us-radar .us-heat-card{padding:12px!important;min-height:126px}#app-page-content #us-radar .us-heat-card.selected{padding:11px!important}#us-radar .us-heat-top{flex-direction:column;gap:3px}#us-radar .us-heat-value{font-size:23px}#us-radar .us-heat-foot{flex-direction:column;gap:1px}#us-radar .us-rank-row{grid-template-columns:22px minmax(0,1fr) 48px 66px;gap:6px}#us-radar .us-rank-track{display:none}#us-radar .us-state{font-size:10px}#us-radar .us-alert-grid{grid-template-columns:1fr}#us-radar .us-alert-card{padding:16px}#us-radar .us-company{flex-wrap:wrap}#us-radar .us-company>a{align-self:flex-start}#us-radar .us-detail-box{padding:13px}}
@media print{#us-radar .us-section{break-inside:avoid}#us-radar .us-research{display:block}#us-radar .us-hero{background:#f1f6f9;color:#203d50}#us-radar .us-hero h1{color:#203d50}}
</style>
<main class="us-radar" id="us-radar" data-api="/web/api/us-rotation">
  <section class="us-hero">
    <div class="us-kicker">US → TW INTELLIGENCE</div><h1>美股輪動雷達</h1>
    <p>追蹤美國產業強弱，接著檢視台灣產品與供應鏈證據。產業走強只代表研究線索，不是台股買進訊號。</p>
    <div class="us-meta"><span class="us-pill" id="us-asof">資料日期載入中</span><span class="us-pill">日線 · 交易日計算</span><span class="us-pill">SPY 為市場基準</span><a class="us-pill" id="us-import-link" href="/web/us-rotation/import" style="color:inherit;text-decoration:none">匯入歷史資料</a></div>
  </section>
  <section class="us-section">
    <div class="us-section-head"><div><h2>市場輪動概況</h2><p id="us-status">載入最近完成的產業快照</p></div></div>
    <div class="us-summary-grid" id="us-summary"></div>
    <div class="us-source" style="margin-top:11px"><b>資料提醒</b><span>每日收盤資料，非即時報價。報酬採來源調整收盤價；美股與台股各自標示交易日期。</span></div>
  </section>
  <section class="us-section"><div class="us-section-head"><div><h2>產業熱度圖</h2><p>紅綠採美股習慣：上漲綠、下跌紅；半導體為資訊科技子產業觀察，獨立標示。</p></div><div class="us-periods" role="group" aria-label="報酬期間"><button type="button" data-period="5" aria-pressed="false">5 日</button><button type="button" class="on" data-period="20" aria-pressed="true">20 日</button><button type="button" data-period="60" aria-pressed="false">60 日</button></div></div><div class="us-heat" id="us-heat"></div><p class="us-selection-note" id="us-selection-note" aria-live="polite"></p></section>
  <section class="us-section"><div class="us-section-head"><div><h2>相對強度排行榜</h2><p>相對 SPY 報酬；排名依透明分數，市場廣度缺資料時不當成零分。</p></div></div><div class="us-rank-list" id="us-ranking"></div><p class="us-note" id="us-score-note"></p></section>
  <section class="us-section"><div class="us-section-head"><div><h2>輪動象限</h2><p>橫軸：20 日相對 SPY 報酬；縱軸：最近 20 日相對強度減去前 20 日。</p></div></div><div id="us-quadrant"></div></section>
  <section class="us-section"><div class="us-section-head"><div><h2>產業與台灣供應鏈</h2><p id="us-detail-title">點選上方任一產業查看產品關聯與證據</p></div></div><div id="us-detail"></div></section>
  <section class="us-section"><div class="us-section-head"><div><h2>美台相對表現落差</h2><p>只比較已有產業映射、時間一致的實際報酬；不把落後解讀成必然補漲。</p></div></div><div id="us-gap"></div></section>
  <section class="us-section"><div class="us-section-head"><div><h2>台美起跌風險與止跌確認</h2><p>逐項顯示目前數值、判定門檻與差距。未觸發只表示尚未達標；差距是數值距離，不是觸發機率。</p></div></div><div class="us-alert-grid" id="us-risk-alerts"></div><p class="us-note">歷史命中率是研究樣本中的條件比例，非預測保證；起跌與止跌訊號會明確標示資料日期。HYG/IEF 是美國信用債代理，不能直接等同台股指數。</p></section>
  <section class="us-section"><div class="us-section-head"><div><h2>輪動排名歷史</h2><p>每日快照累積後才形成趨勢；首次建立時歷史不足會明確標示。</p></div></div><div class="us-history" id="us-history"></div></section>
  <p class="us-note">研究工具，不構成投資建議。趨勢狀態是規則分類，不是預測；營收、法人與估值須以各自公布日及資料時間為準。</p>
</main>
<script>
(function(){
 const root=document.getElementById('us-radar');if(!root)return;
 const $=s=>root.querySelector(s),esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 root.addEventListener('pointerdown',e=>{const b=e.target.closest('button');if(b&&root.contains(b))b.classList.add('is-pressed');});
 ['pointerup','pointercancel','pointerleave'].forEach(name=>root.addEventListener(name,()=>root.querySelectorAll('.is-pressed').forEach(b=>b.classList.remove('is-pressed'))));
 const fmt=(v,d=2)=>v==null?'—':(Number(v)>0?'+':'')+Number(v).toFixed(d)+'%';
 const cls=v=>v==null?'us-neutral':Number(v)>0?'us-positive':Number(v)<0?'us-negative':'us-neutral';
 let state={snapshot:null,history:[],mapping:[],twCompanies:[],riskProfiles:[],stopProfiles:[],signalExplanations:{},period:20,selected:null};
 function empty(el,title,copy){el.innerHTML='<div class="us-empty"><b>'+esc(title)+'</b><p>'+esc(copy)+'</p></div>';}
 function renderSummary(data){const rows=data.industries||[];const scored=rows.filter(x=>!x.overlay&&x.score!=null).sort((a,b)=>a.rank-b.rank);const top=scored[0],weak=scored[scored.length-1],turn=rows.find(x=>x.trend_state==='轉強觀察');const items=[['目前領先',top?top.name:'—',top?fmt(top.relative_20d_pct):'尚無快照'],['轉強觀察',turn?turn.name:'—',turn?fmt(turn.relative_20d_pct):'等待連續資料'],['相對落後',weak?weak.name:'—',weak?fmt(weak.relative_20d_pct):'尚無快照']];$('#us-summary').innerHTML=items.map(x=>'<div class="us-summary"><small>'+esc(x[0])+'</small><b>'+esc(x[1])+'</b><em>'+esc(x[2])+'</em></div>').join('');}
 function renderHeat(){const rows=state.snapshot&&state.snapshot.industries||[];if(rows.length&&!state.selected)state.selected=rows[0].id;const selected=rows.find(r=>r.id===state.selected);$('#us-selection-note').textContent=selected?'已選取 '+selected.name+'（'+selected.ticker+'） · 下方查看產業與台灣供應鏈':'';if(!rows.length){empty($('#us-heat'),'尚無美股產業快照','完成第一次批次更新後，這裡會顯示 12 個產業代理 ETF。');return;}
  const key='return_'+state.period+'d_pct';$('#us-heat').innerHTML=rows.map(r=>'<button type="button" aria-pressed="'+String(state.selected===r.id)+'" class="us-heat-card '+(state.selected===r.id?'selected':'')+'" data-industry="'+esc(r.id)+'"><span class="us-heat-top"><b>'+esc(r.name)+'</b><small>'+esc(r.ticker)+(r.overlay?' · 子產業':'')+'</small></span><div class="us-heat-value '+cls(r[key])+'">'+esc(fmt(r[key]))+'</div><span class="us-heat-foot"><span>相對 SPY</span><b class="'+cls(r['relative_'+state.period+'d_pct'])+'">'+esc(fmt(r['relative_'+state.period+'d_pct']))+'</b></span></button>').join('');root.querySelectorAll('.us-heat-card').forEach(b=>b.addEventListener('click',()=>selectIndustry(b.dataset.industry)));}
 function renderRanking(){const rows=(state.snapshot&&state.snapshot.industries||[]).filter(x=>!x.overlay).sort((a,b)=>(a.rank||99)-(b.rank||99));if(!rows.length){empty($('#us-ranking'),'尚無可排名資料','資料不足時不會補成零分。');return;}
  const max=Math.max(1,...rows.map(x=>Number(x.score)||0));$('#us-ranking').innerHTML=rows.map(r=>'<div class="us-rank-row"><span class="us-rank-no">'+(r.rank||'—')+'</span><span class="us-rank-name"><b>'+esc(r.name)+'</b><small>'+esc(r.ticker)+' · '+esc(r.quadrant||'資料不足')+'</small></span><span class="us-rank-track"><i style="width:'+Math.max(1,(Number(r.score)||0)/max*100)+'%"></i></span><b class="us-rank-score">'+(r.score==null?'—':Number(r.score).toFixed(0)+' 分')+'</b><span class="us-state">'+esc(r.trend_state||'資料不足')+'</span></div>').join('');$('#us-score-note').textContent=(state.snapshot.score_note||'')+' 權重覆蓋度逐產業顯示；廣度未提供，當前最高分覆蓋 90%。';}
 function renderQuadrant(){const rows=(state.snapshot&&state.snapshot.industries||[]).filter(r=>!r.overlay&&r.relative_20d_pct!=null&&r.relative_momentum_20d_pct!=null);if(rows.length<2){empty($('#us-quadrant'),'象限資料不足','至少需要 20 日相對報酬及前後兩個 20 日區間。');return;}
  const w=560,h=250,p={l:42,r:16,t:18,b:32},xs=rows.map(r=>Number(r.relative_20d_pct)),ys=rows.map(r=>Number(r.relative_momentum_20d_pct));let xmax=Math.max(1,...xs.map(Math.abs))*1.18,ymax=Math.max(1,...ys.map(Math.abs))*1.18;const X=v=>p.l+(v+xmax)/(2*xmax)*(w-p.l-p.r),Y=v=>p.t+(ymax-v)/(2*ymax)*(h-p.t-p.b);let svg='<svg class="us-chart" viewBox="0 0 '+w+' '+h+'" role="img" aria-label="產業相對強度輪動象限"><line class="us-axis-zero" x1="'+X(0)+'" x2="'+X(0)+'" y1="'+p.t+'" y2="'+(h-p.b)+'"/><line class="us-axis-zero" x1="'+p.l+'" x2="'+(w-p.r)+'" y1="'+Y(0)+'" y2="'+Y(0)+'"/><text class="us-quad-label" x="'+(p.l+6)+'" y="'+(p.t+12)+'">改善</text><text class="us-quad-label" x="'+(w-p.r-42)+'" y="'+(p.t+12)+'">領先</text><text class="us-quad-label" x="'+(p.l+6)+'" y="'+(h-p.b-5)+'">落後</text><text class="us-quad-label" x="'+(w-p.r-42)+'" y="'+(h-p.b-5)+'">轉弱</text><text class="us-chart-label" x="'+(w/2)+'" y="'+(h-3)+'" text-anchor="middle">20 日相對 SPY 報酬</text><text class="us-chart-label" transform="translate(11 '+(h/2)+') rotate(-90)" text-anchor="middle">相對強度變化</text>';
  const color=r=>r.quadrant==='領先'?'#17856A':r.quadrant==='改善'?'#3D94A2':r.quadrant==='轉弱'?'#D28B46':'#8999A6';
  svg+=rows.map(r=>'<circle class="us-dot" cx="'+X(Number(r.relative_20d_pct))+'" cy="'+Y(Number(r.relative_momentum_20d_pct))+'" r="'+(state.selected===r.id?9:6)+'" fill="'+color(r)+'"><title>'+esc(r.name)+' · '+esc(fmt(r.relative_20d_pct))+' · '+esc(r.quadrant)+'</title></circle>').join('')+'</svg>';
  $('#us-quadrant').innerHTML=svg+'<div class="us-quadrant-legend">'+rows.map(r=>'<button type="button" class="us-legend-button" data-quadrant-industry="'+esc(r.id)+'" aria-pressed="'+String(state.selected===r.id)+'"><span class="us-legend-dot" style="background:'+color(r)+'"></span>'+esc(r.name)+' · '+esc(r.quadrant)+'</button>').join('')+'</div>';
  root.querySelectorAll('[data-quadrant-industry]').forEach(b=>b.addEventListener('click',()=>selectIndustry(b.dataset.quadrantIndustry)));}
 function renderDetail(){const id=state.selected||((state.snapshot&&state.snapshot.industries||[])[0]||{}).id;const r=(state.snapshot&&state.snapshot.industries||[]).find(x=>x.id===id);if(!r){empty($('#us-detail'),'尚無產業資料','資料載入完成後，點選產業卡片。');return;}state.selected=r.id;$('#us-detail-title').textContent=r.name+' · '+r.ticker+' · '+(r.gics||'');const links=state.mapping.filter(x=>x.us_industry===r.id);const byCode={};(state.twCompanies||[]).forEach(x=>byCode[x.code]=x);const mapHtml=links.length?links.map(x=>{const d=byCode[x.code]||{};const metrics=d.code?'<small>台股資料日 '+esc(d.price_date||d.as_of_date||'—')+' · 收盤 '+esc(d.close==null?'—':Number(d.close).toFixed(2))+' · 20 日 '+esc(fmt(d.return_20d_pct))+'</small><small>月營收年增 '+esc(fmt(d.revenue_yoy_pct))+'（'+esc(d.revenue_period||'資料月未標')+'） · 法人合計 '+esc(d.total_net_lots==null?'—':Number(d.total_net_lots).toLocaleString()+' 張')+'</small><small>MA5 / 20 / 60：'+esc(d.ma5==null?'—':Number(d.ma5).toFixed(2))+' / '+esc(d.ma20==null?'—':Number(d.ma20).toFixed(2))+' / '+esc(d.ma60==null?'—':Number(d.ma60).toFixed(2))+' · PE / PB / 殖利率：'+esc(d.pe==null?'—':Number(d.pe).toFixed(1))+' / '+esc(d.pb==null?'—':Number(d.pb).toFixed(2))+' / '+esc(d.dividend_yield_pct==null?'—':Number(d.dividend_yield_pct).toFixed(2)+'%')+'</small><small>EPS、毛利率、營業利益率：尚無可確認欄位；缺資料不補值。</small>':'<small>尚無台股觀察快照；產品關聯有證據，但行情與基本面尚未同步。</small>';return '<div class="us-company"><span><b>'+esc(x.name)+'（'+esc(x.code)+'）</b><small>'+esc(x.chain)+' · '+esc(x.product)+' · '+esc(x.role)+'</small><small>'+esc(x.confidence)+'；'+esc(x.reason)+'</small>'+metrics+'</span><a href="'+esc(x.evidence_url)+'" target="_blank" rel="noopener">證據 ↗</a></div>';}).join(''):'<p>目前沒有已建檔的台灣公司證據關聯；不會只因題材相同就列為供應商。</p>';
  $('#us-detail').innerHTML='<div class="us-detail"><div class="us-detail-box"><h3>美股產業表現</h3><p>資料日 '+esc(r.as_of||'—')+' · 狀態 '+esc(r.trend_state||'資料不足')+' · '+esc(r.quadrant||'資料不足')+'</p><p>5 日 '+esc(fmt(r.return_5d_pct))+'（相對 '+esc(fmt(r.relative_5d_pct))+'）<br>20 日 '+esc(fmt(r.return_20d_pct))+'（相對 '+esc(fmt(r.relative_20d_pct))+'）<br>60 日 '+esc(fmt(r.return_60d_pct))+'（相對 '+esc(fmt(r.relative_60d_pct))+'）</p><p>ETF 相對 20 日均量：'+(r.relative_volume==null?'—':Number(r.relative_volume).toFixed(2)+' 倍')+'；廣度：資料不足。</p><p>目前沒有與此產業有日期及來源佐證的事件資料；無法確認主要漲跌原因。</p></div><div class="us-detail-box"><h3>台灣產品與供應鏈證據</h3>'+mapHtml+'</div></div>';
 }
 function renderHistory(){const history=state.history||[];if(history.length<2){empty($('#us-history'),'歷史快照尚在累積','至少保存兩個更新日，才顯示產業名次變化；目前不使用今日成分反推過去。');return;}const dates=history.slice(-12);const keys=dates.map(x=>x.as_of);const topIds=(dates[dates.length-1].industries||[]).filter(x=>!x.overlay).sort((a,b)=>(a.rank||99)-(b.rank||99)).slice(0,5).map(x=>x.id);let html='<table><thead><tr><th>產業</th>'+keys.map(x=>'<th>'+esc(x.slice(5))+'</th>').join('')+'</tr></thead><tbody>';topIds.forEach(id=>{const nm=((dates[dates.length-1].industries||[]).find(x=>x.id===id)||{}).name||id;html+='<tr><td>'+esc(nm)+'</td>'+dates.map(day=>{const row=(day.industries||[]).find(x=>x.id===id);return '<td>'+(row&&row.rank?'第 '+row.rank+' 名':'—')+'</td>';}).join('')+'</tr>';});$('#us-history').innerHTML=html+'</tbody></table>';}
 function renderRiskAlerts(){
  const all=state.signalExplanations||{},cur=(state.snapshot&&state.snapshot.current_risk_warning)||{};
  const num=(n,d=2)=>Number(n).toLocaleString('zh-TW',{minimumFractionDigits:d,maximumFractionDigits:d});
  const dateNote=c=>'<span class="us-signal-date">資料日 '+esc(c.as_of||'尚無資料')+' · 採日線收盤判定</span>';
  const stat=(label,value)=>'<div class="us-alert-stat"><span>'+esc(label)+'</span><b>'+esc(value)+'</b></div>';
  const card=(key,title,research)=>{
   const c=all[key]||{},stop=key.endsWith('_stop'),active=c.available&&c.triggered;
   const badge=active?(stop?'突破成立':'已達門檻'):(c.available?'未觸發':'資料不足');
   const unit=c.unit||(stop?'點':key==='us'?'':'個百分點');
   const value=c.available?num(c.value)+(unit?'<small>'+esc(unit)+'</small>':''):'尚無目前資料';
   let condition=stop?'觸發條件：當日收盤 > 前五個交易日最高收盤':key==='us'?'觸發條件：VIX 收盤 ≥ '+num(c.threshold??25):'觸發條件：HYG − IEF 的 20 日報酬差 ≤ '+num(c.threshold??-3)+' 個百分點';
   if(stop&&c.available)condition+=' '+num(c.threshold)+' 點';
   let distance='資料不足，無法計算目前距離。';
   if(c.available){
    if(stop){
     if(c.equal)distance='目前等於門檻，尚須收盤高於 '+num(c.threshold)+' 點才成立。';
     else distance=active?'已高於門檻 '+num(c.distance)+' 點（'+num(c.distance_pct)+'%）。':'距突破還差 '+num(c.distance)+' 點；需再上漲約 '+num(c.distance_pct)+'%，並收盤高於門檻。';
    }else if(active){distance=c.equal?'目前恰好達到觸發門檻。':'已'+(key==='us'?'高於':'低於')+'門檻 '+num(c.distance)+(unit?' '+unit:'')+'。';}
    else distance=key==='us'?'距觸發還需上升 '+num(c.distance)+'。':'距觸發還需下降 '+num(c.distance)+' 個百分點。';
   }
   const proxy=c.proxy_note||((cur[key]||{}).proxy_note)||'';
   return '<article class="us-alert-card '+(active?(stop?'confirm-on':'signal-on'):'')+'" data-signal="'+key+'"><div class="us-alert-head"><h3>'+esc(title)+'</h3><span class="us-alert-badge '+(active?(stop?'confirmed':'alert'):'')+'">'+badge+'</span></div><div class="us-alert-value">'+value+'</div><p class="us-signal-condition">'+esc(condition)+'</p><div class="us-signal-distance">'+esc(distance)+'</div>'+dateNote(c)+(proxy?'<p class="us-note">'+esc(proxy)+'</p>':'')+'<details class="us-research"><summary>歷史研究統計</summary>'+research+'</details></article>';
  };
  const cards=state.riskProfiles.map(x=>{
   const key=x.market==='美股'?'us':'taiwan';
   const research=stat('訊號日後 20 交易日急跌比例',num(x.daily_trigger_probability_pct,1)+'%（未觸發樣本 '+num(x.daily_no_trigger_probability_pct,1)+'%）')+stat('警示期命中',x.alert_episode_hits+'/'+x.alert_episodes+' · '+num(x.episode_success_rate_pct,1)+'%')+stat('成功訊號提前中位數',x.median_lead_sessions+' 個交易日')+stat('事件捕捉率／未命中警示',num(x.event_capture_rate_pct,1)+'% / '+num(x.episode_no_hit_rate_pct,1)+'%')+'<p class="us-alert-foot">基準急跌比例 '+num(x.base_probability_pct,1)+'%；樣本自 '+esc(x.source_start)+' 起。這是歷史條件統計，不是今天的急跌機率。</p>';
   return card(key,key==='us'?'美股起跌風險 · VIX':'台股風險代理 · HYG／IEF',research);
  });
  state.stopProfiles.forEach(x=>{
   let research='';['gradual','fast'].forEach(k=>{const p=x[k];research+='<h4 class="us-study-title">'+(k==='fast'?'急跌後止跌樣本':'緩跌後止跌樣本')+'</h4>'+stat('歷史命中率',num(p.success_rate_pct,1)+'%（'+p.hits+'/'+p.signals+'；基準 '+num(p.baseline_pct,1)+'%）')+stat('後續 20 日低點中位距離',p.median_days_to_next_20d_low+' 個交易日');});
   research+='<p class="us-alert-foot">命中定義：訊號後十個交易日不破訊號日前十日低點，且第十日收盤高於訊號日。兩組歷史樣本共用目前的突破判定；突破不保證已見底。</p>';
   cards.push(card(x.market==='美股'?'us_stop':'taiwan_stop',x.market+'止跌確認 · '+x.index,research));
  });
  $('#us-risk-alerts').innerHTML=cards.join('');
 }
 function renderGap(){const mapping=state.mapping||[];const snap=state.snapshot;if(!snap){empty($('#us-gap'),'尚無可比較快照','需先有美股與台股同期報酬資料。');return;}const tw={};(state.twCompanies||[]).forEach(x=>tw[x.code]=x);const industries={};(snap.industries||[]).forEach(x=>industries[x.id]=x);const observations=mapping.map(m=>{const u=industries[m.us_industry],t=tw[m.code];if(!u||!t||u.return_20d_pct==null||t.return_20d_pct==null)return null;return {map:m,us:u,tw:t,gap:Number(u.return_20d_pct)-Number(t.return_20d_pct)};}).filter(x=>x&&x.us.return_20d_pct>0&&x.gap>=5).sort((a,b)=>b.gap-a.gap);if(!observations.length){empty($('#us-gap'),'目前沒有資料完整的落差候選','只在美股產業 20 日上漲、台股價格與營收資料均可對齊時顯示。資料日期可能不同，落後不是補漲訊號。');return;}$('#us-gap').innerHTML=observations.map(x=>'<div class="us-company"><span><b>'+esc(x.map.name)+'（'+esc(x.map.code)+'） · 觀察差距 '+esc(x.gap.toFixed(2))+' 個百分點</b><small>'+esc(x.map.chain)+' · 美股 '+esc(x.map.name)+' 所屬產業代理 '+esc(fmt(x.us.return_20d_pct))+'（截至 '+esc(x.us.as_of||'—')+'），台股 '+esc(fmt(x.tw.return_20d_pct))+'（截至 '+esc(x.tw.price_date||'—')+'）</small><small>月營收年增 '+esc(fmt(x.tw.revenue_yoy_pct))+'；法人合計 '+esc(x.tw.total_net_lots==null?'資料不足':Number(x.tw.total_net_lots).toLocaleString()+' 張')+'。可能原因與估值需另查；此差距不代表補漲。</small><small>供應鏈產品證據：'+esc(x.map.confidence)+'；特定客戶／直接供貨關係未確認。</small></span><a href="'+esc(x.map.evidence_url)+'" target="_blank" rel="noopener">證據 ↗</a></div>').join('');}
 function selectIndustry(id){state.selected=id;renderHeat();renderDetail();renderQuadrant();}
 function draw(){renderRiskAlerts();if(!state.snapshot){$('#us-asof').textContent='等待完成快照';$('#us-status').textContent='尚未有完成快照';empty($('#us-summary'),'等待第一筆可信資料','頁面只讀已完成快照，不會在開頁時同步抓行情。');renderHeat();renderRanking();renderQuadrant();renderDetail();renderGap();renderHistory();return;}const s=state.snapshot;$('#us-asof').textContent='美股交易日 '+(s.as_of||'—');let stamp='';try{const d=new Date(String(s.fetched_at||'').replace(' ','T').replace(/(\.\d{3})\d+/,'$1'));if(!Number.isNaN(d.getTime()))stamp=new Intl.DateTimeFormat('zh-TW',{timeZone:'Asia/Taipei',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(d);}catch(e){} const source=String(s.source||'');$('#us-status').textContent='更新 '+(stamp?stamp+'（台北時間）':'時間未提供')+' · '+(source.includes('Yahoo')?'Yahoo Finance 日線':source||'來源未提供');renderSummary(s);renderHeat();renderRanking();renderQuadrant();renderDetail();renderGap();renderHistory();}
 fetch(root.dataset.api+'?t='+encodeURIComponent(new URLSearchParams(location.search).get('t')||''),{credentials:'same-origin',cache:'no-store'}).then(r=>{if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}).then(d=>{state.snapshot=d.snapshot||null;state.history=d.history||[];state.mapping=d.supply_chain||[];state.twCompanies=d.tw_companies||[];state.riskProfiles=d.risk_profiles||[];state.stopProfiles=d.stop_profiles||[];state.signalExplanations=d.signal_explanations||{};draw();}).catch(()=>{draw();$('#us-status').textContent='載入失敗，請重新整理頁面';});
 root.querySelectorAll('[data-period]').forEach(b=>b.addEventListener('click',()=>{root.querySelectorAll('[data-period]').forEach(x=>{x.classList.toggle('on',x===b);x.setAttribute('aria-pressed',String(x===b));});state.period=Number(b.dataset.period);renderHeat();}));
})();
</script>'''

    def _read_status():
        latest = read_latest(get_db_connection, release_db_connection)
        history = read_history(get_db_connection, release_db_connection, 30)
        mapping = read_supply_chain(get_db_connection, release_db_connection)
        tw_companies = read_latest_tw_company_snapshots(get_db_connection, release_db_connection)
        return latest, history, mapping, tw_companies

    def _import_page(message="", error=False):
        symbols = ", ".join(item["ticker"] for item in INDUSTRIES) + ", SPY"
        risk_symbols = "^VIX, HYG, IEF, ^GSPC, ^TWII（選填）"
        token = request.args.get("t") or ""
        suffix = "?t=" + _esc(token) if token else ""
        result = ""
        if message:
            bg, color = ("#FCECEC", "#B74747") if error else ("#EAF5EF", "#276B55")
            result = (f'<div style="margin:12px 0;padding:11px;border-radius:9px;'
                      f'background:{bg};color:{color};font-weight:800">{_esc(message)}</div>')
        page = '''<main style="max-width:760px;margin:16px auto;padding:16px;color:var(--ink,#243647)">
<section style="padding:20px;border:1px solid var(--ui-line,#DCE3E9);border-radius:16px;background:var(--ui-surface,#fff)">
<a href="/web/us-rotation__SUFFIX__" style="color:#286D9A;text-decoration:none;font-weight:800">← 回美股輪動</a>
<h1 style="font-size:22px;margin:18px 0 8px">匯入美股歷史日線</h1>
<p style="line-height:1.8;color:#617487">上傳 CSV 後會先完整檢查，通過才寫入資料庫。每個代號至少 61 個交易日，且最後交易日需一致；不會用不完整資料補成零。</p>
<p style="line-height:1.8"><b>必須包含的代號：</b>__SYMBOLS__</p>
<p style="line-height:1.8"><b>起跌／止跌警示選填代號：</b>__RISK_SYMBOLS__。^VIX、^GSPC、^TWII 可只提供 close；HYG、IEF 請提供 adjusted_close。缺少選填代號不影響產業排行榜，但對應警示會顯示資料不足。</p>
<p style="line-height:1.8"><b>CSV 欄位：</b><code>symbol,trade_date,open,high,low,close,adjusted_close,volume</code><br>
每個代號每天一列，日期格式 YYYY-MM-DD。若沒有股息還原收盤價，可省略整個 adjusted_close 欄位，系統會標示為未還原價格報酬；不可只填部分列。</p>
<p><a href="/web/us-rotation/import/template.csv__SUFFIX__" style="color:#286D9A;font-weight:800">下載 CSV 欄位範本</a></p>
<p style="font-size:12px;color:#617487">請確認你提供的資料來源允許匯入及在此網站使用；來源名稱會與快照一起保存。建議匯入至最近已完成的美股交易日。</p>
__RESULT__
<form method="post" enctype="multipart/form-data" action="/web/us-rotation/import__SUFFIX__" style="display:grid;gap:12px;margin-top:16px">
<label style="font-weight:800">資料來源名稱<input name="source_name" required maxlength="120" placeholder="請填行情來源名稱" style="display:block;width:100%;box-sizing:border-box;padding:11px;margin-top:6px;border:1px solid #CAD5DE;border-radius:9px"></label>
<label style="font-weight:800">CSV 檔案<input name="file" type="file" accept=".csv,text/csv" required style="display:block;width:100%;margin-top:8px"></label>
<button type="submit" style="border:0;border-radius:10px;background:#245779;color:white;padding:12px;font-weight:900;font-size:15px">檢查並匯入</button>
</form></section></main>'''
        return (page.replace("__SUFFIX__", suffix)
                    .replace("__SYMBOLS__", _esc(symbols))
                    .replace("__RISK_SYMBOLS__", _esc(risk_symbols))
                    .replace("__RESULT__", result))

    def _valid_import_csrf(uid):
        supplied = str(request.form.get("csrf_token") or "").strip()
        secret = (os.environ.get("WEB_CSRF_SECRET") or os.environ.get("CRON_SECRET")
                  or "change-this-web-csrf-secret")
        message = ("web-csrf-user-v2:" + str(uid or "").strip()).encode("utf-8")
        expected = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
        return bool(supplied and uid and hmac.compare_digest(expected, supplied))

    def _refresh_job(allow_unofficial_override=False):
        from .providers import ProviderDisabled
        from .config import SOURCE_NAME
        # The existing morning warm-up already refreshes US quotes through the
        # project's Yahoo adapter. Reuse that explicit operational path for the
        # rotation snapshot; direct/manual refresh remains behind the source gate.
        allow = bool(allow_unofficial_override) or os.environ.get(
            "US_ROTATION_YAHOO_TERMS_ACKNOWLEDGED", "").strip().lower() in {"1", "true", "yes"}
        histories, errors, fetched_at = fetch_market_histories(allow_unofficial=allow)
        if not histories:
            raise ProviderDisabled("行情來源尚未啟用或所有代號抓取失敗")
        snapshot = calculate_snapshot(histories, source=SOURCE_NAME, fetched_at=fetched_at)
        snapshot["current_risk_warning"] = calculate_current_risk(histories)
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

    # Allow the existing authenticated daily warm-up to invoke this same job
    # without exposing a second public refresh path.
    bp.us_rotation_refresh_job = _refresh_job

    @bp.route("/web/us-rotation")
    @web_login_required
    def page(uid):
        return render_page("美股輪動", _page_html(), "us_rotation")

    @bp.route("/web/us-rotation/import", methods=["GET", "POST"])
    @web_login_required
    def import_page(uid):
        if request.method == "GET":
            return render_page("匯入美股資料", _import_page(), "us_rotation")
        if not _valid_import_csrf(uid):
            abort(403)
        source_name = (request.form.get("source_name") or "").strip()[:120]
        uploaded = request.files.get("file")
        if not source_name or not uploaded or not uploaded.filename:
            return render_page("匯入美股資料", _import_page("請填寫資料來源並選擇 CSV 檔案。", True), "us_rotation")
        raw = uploaded.stream.read(5 * 1024 * 1024 + 1)
        if len(raw) > 5 * 1024 * 1024:
            return render_page("匯入美股資料", _import_page("檔案超過 5 MB，請縮小後再上傳。", True), "us_rotation")
        try:
            text = raw.decode("utf-8-sig")
            histories, has_adjusted = parse_history_csv(text)
            fetched_at = datetime.now(timezone.utc).isoformat()
            snapshot = calculate_snapshot(histories, source=source_name, fetched_at=fetched_at)
            snapshot["current_risk_warning"] = calculate_current_risk(histories)
            snapshot["price_basis"] = ("股息還原收盤價" if has_adjusted
                                       else "未還原收盤價（價格報酬，不含股息）")
            save_snapshot(
                get_db_connection, release_db_connection, snapshot, histories,
                source_meta={"imported": True, "unofficial_source": False,
                             "price_basis": snapshot["price_basis"],
                             "original_filename": uploaded.filename[:200]},
            )
            message = (f"匯入完成：{sum(len(rows) for rows in histories.values()):,} 筆日線，"
                       f"最新交易日 {snapshot.get('as_of')}；{snapshot['price_basis']}。")
            return render_page("匯入美股資料", _import_page(message), "us_rotation")
        except (UnicodeDecodeError, ImportValidationError) as exc:
            return render_page("匯入美股資料", _import_page(str(exc), True), "us_rotation")
        except Exception:
            return render_page("匯入美股資料", _import_page("資料庫匯入失敗，請稍後重新載入輪動頁確認。", True), "us_rotation")

    @bp.route("/web/us-rotation/import/template.csv")
    @web_login_required
    def import_template(uid):
        return Response(csv_template(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=us_rotation_template.csv"})

    @bp.route("/web/api/us-rotation")
    @web_login_required
    def api(uid):
        try:
            latest, history, mapping, tw_companies = _read_status()
            return jsonify({"ok": True, "snapshot": latest, "history": history,
                            "signal_explanations": describe_current_risk(
                                (latest or {}).get("current_risk_warning")),
                            "supply_chain": mapping, "tw_companies": tw_companies,
                            "risk_profiles": HISTORICAL_RISK_PROFILES,
                            "stop_profiles": public_stop_profiles(),
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


