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
                         read_signal_prices, read_supply_chain, save_snapshot, save_tw_company_snapshots)
from .risk_warning import (HISTORICAL_RISK_PROFILES, calculate_current_risk,
                           build_signal_trends, describe_current_risk, public_stop_profiles)


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
.us-heat-top{display:flex;justify-content:space-between;gap:4px;align-items:center}.us-heat-top b{font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.us-heat-top small{font-size:9px;color:#738394}.us-heat-value{font-size:19px;font-weight:950;margin:8px 0 4px}.us-heat-foot{display:flex;justify-content:space-between;gap:4px;color:#66798A;font-size:9px;font-weight:800}.us-positive{color:#C74343!important}.us-negative{color:#0B8159!important}.us-neutral{color:#708092!important}
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
#us-radar .us-tw-intro{padding:14px 16px;background:#edf6fa;border-radius:12px;margin-bottom:14px;color:#345f75;font-size:13px;line-height:1.8}
#us-radar .us-tw-intro p{margin:5px 0 0;font-size:12px}
#us-radar .us-return-table{overflow:auto}#us-radar .us-return-table table{width:100%;border-collapse:collapse;font-size:12px;line-height:1.7}
#us-radar .us-return-table th,#us-radar .us-return-table td{padding:9px 5px;border-bottom:1px solid #dfe8ee;text-align:left}
#us-radar .us-return-table thead{color:#5b7587;font-size:11px}#us-radar .us-return-table tbody th{font-weight:700;white-space:nowrap}
#us-radar .us-tw-company{padding:14px 0;border-top:1px solid #dbe5eb}#us-radar .us-tw-company:first-of-type{border-top:0}
#us-radar .us-tw-company h4{font-size:16px;margin:0 0 7px;color:#294e65}#us-radar .us-tw-company h4 small{font-size:12px;color:#6e8595;margin-left:4px}
#us-radar .us-tw-company p{font-size:12px;line-height:1.85;margin:7px 0}#us-radar .us-tw-product{color:#326c86}
#us-radar .us-tw-metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px;margin:12px 0}
#us-radar .us-tw-metrics>div{padding:10px 8px;border:1px solid #e0e9ef;background:#fff;border-radius:9px;min-width:0}
#us-radar .us-tw-metrics small,#us-radar .us-tw-metrics span{display:block;font-size:10px;color:#6f8494;line-height:1.6}
#us-radar .us-tw-metrics b{display:block;font-size:16px;margin:5px 0;color:#294f65;overflow-wrap:anywhere}
#us-radar .us-tw-company a{font-size:12px;color:#266d8e;font-weight:800}#us-radar .us-tw-shortcuts{display:flex;flex-wrap:wrap;justify-content:center;gap:6px;margin-top:12px}
#us-radar .us-signal-chart{margin:16px 0 0;padding-top:14px;border-top:1px solid #dce6ec}
#us-radar .us-trend-head{display:flex;justify-content:space-between;gap:8px;align-items:center;font-size:13px;color:#34596f}
#us-radar .us-trend-periods{display:flex;flex:none;gap:4px;padding:3px;background:#eaf2f6;border-radius:9px}
#app-page-content #us-radar .us-trend-periods button{padding:6px 10px!important;margin:0!important;min-height:32px;border:1px solid transparent!important;background:transparent!important;color:#547285!important;border-radius:7px!important;font-size:11px!important;font-weight:800;white-space:nowrap;transform:none!important}
#app-page-content #us-radar .us-trend-periods button[aria-pressed=true]{background:#d5eaf3!important;border-color:#a2c8d9!important;color:#235c77!important}
#app-page-content #us-radar .us-trend-periods button:active{background:#cbe2ed!important}
#us-radar .us-trend-legend{display:flex;flex-wrap:wrap;gap:6px 14px;margin:10px 0 8px;font-size:10px;color:#647e8e}
#us-radar .us-trend-legend span{display:inline-flex;align-items:center;gap:5px}#us-radar .us-trend-legend i{width:18px;border-top:2px solid #297a9d}#us-radar .us-trend-legend i.threshold{border-top:2px dashed #b07a43}
#us-radar .us-signal-svg{width:100%;height:auto;display:block;touch-action:pan-y;overflow:hidden;outline-offset:2px}
#us-radar .us-value-path{fill:none;stroke:#297a9d;stroke-width:2;vector-effect:non-scaling-stroke;stroke-linejoin:round}
#us-radar .us-threshold-path{fill:none;stroke:#b07a43;stroke-width:1.5;stroke-dasharray:5 4;vector-effect:non-scaling-stroke}
#us-radar .us-trend-cursor{stroke:#92adbd;stroke-width:1;stroke-dasharray:3 3}#us-radar .us-trend-dot{fill:#fff;stroke:#297a9d;stroke-width:2;pointer-events:none}
#us-radar .us-trend-readout{font-size:11px;color:#3f677e;line-height:1.8;min-height:22px;font-variant-numeric:tabular-nums}
#us-radar .us-trend-caption{font-size:10px;line-height:1.7;margin:4px 0 0;color:#6e8494}
@media(max-width:700px){#us-radar .us-tw-metrics b{font-size:15px}#us-radar .us-return-table table{font-size:11px}#us-radar .us-signal-svg{min-height:160px}#us-radar .us-trend-readout{font-size:10px}}

#us-radar #us-tw-section{scroll-margin-top:14px}
#us-radar .us-tw-navigation{padding:14px;margin-bottom:14px;border:1px solid #cddfe8;border-radius:12px;background:#f5fafc}
#us-radar .us-tw-navigation label{display:block;margin-bottom:7px;font-size:12px;font-weight:800;color:#3a6278}
#app-page-content #us-radar #us-tw-sector{display:block;box-sizing:border-box;width:100%;min-width:0;min-height:44px;padding:9px 34px 9px 11px;border:1px solid #a9cbdc!important;border-radius:9px!important;background-color:#fff!important;color:#264f68!important;font:inherit;font-size:16px!important;line-height:1.5;box-shadow:none;cursor:pointer}
#app-page-content #us-radar #us-tw-sector:focus-visible{outline:3px solid #63a6c1;outline-offset:2px}
#us-radar .us-tw-nav-actions{display:flex;flex-wrap:wrap;gap:7px;margin-top:10px}
#us-radar .us-tw-close-row{margin-top:12px}
#app-page-content #us-radar .us-tw-nav-actions button,#app-page-content #us-radar [data-close-company]{min-height:44px}
#us-radar .us-tw-company summary{scroll-margin-bottom:calc(96px + env(safe-area-inset-bottom))}
#us-radar .us-tw-metrics small{min-height:30px;display:flex;align-items:flex-start}
#us-radar .us-tw-metrics b{min-height:24px;line-height:1.5}

#us-radar .us-summary-note{grid-column:1/-1;margin:2px 0 0;font-size:11px;line-height:1.7;color:#6b8293}
#us-radar .us-advanced{margin-top:12px;padding:0;border:1px solid #dbe6ed;border-radius:13px;background:#fff;box-shadow:none}
#us-radar .us-advanced>summary{display:list-item;list-style:disclosure-closed;padding:14px 16px;font-size:13px;font-weight:800;color:#44677e;background:transparent;border:0;cursor:pointer}
#us-radar .us-advanced[open]>summary{border-bottom:1px solid #e2eaf0}
#us-radar .us-advanced>.us-section{margin:0;border:0;background:transparent}
#us-radar .us-risk-market-tools{grid-column:1/-1}
#us-radar .us-market-switch{display:flex;gap:4px;width:fit-content;padding:4px;border-radius:12px;background:#edf4f7}
#app-page-content #us-radar .us-market-switch button{min-width:88px;min-height:44px;margin:0!important;padding:9px 18px!important;border:1px solid transparent!important;border-radius:9px!important;background:transparent!important;color:#58758a!important;font-size:13px!important;font-weight:800;transform:none!important;white-space:nowrap}
#app-page-content #us-radar .us-market-switch button[aria-pressed=true]{background:#dbeef6!important;border-color:#a1c9dc!important;color:#1d5c7d!important}
#app-page-content #us-radar .us-market-switch button:active{background:#cce4ee!important}
#us-radar [data-signal][hidden]{display:none!important}
#us-radar .us-signal-intro{font-size:12px;line-height:1.8;color:#6c8291;margin:10px 0 0}
#us-radar .us-signal-chart{margin-top:12px;padding-top:12px}
#us-radar .us-signal-distance{margin:12px 0}
#us-radar #us-market-help{margin:8px 0 0}
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
  <section class="us-section"><div class="us-section-head"><div><h2>產業熱度圖</h2><p>紅色表示上漲，綠色表示下跌。下方另列與美股大盤的漲幅差；半導體單獨觀察。</p></div><div class="us-periods" role="group" aria-label="報酬期間"><button type="button" data-period="5" aria-pressed="false">5 日</button><button type="button" class="on" data-period="20" aria-pressed="true">20 日</button><button type="button" data-period="60" aria-pressed="false">60 日</button></div></div><div class="us-heat" id="us-heat"></div><p class="us-selection-note" id="us-selection-note" aria-live="polite"></p></section>
  <section class="us-section"><div class="us-section-head"><div><h2>產業綜合排名</h2><p>比較各產業近期表現；分數越高，代表目前相對較強。</p></div></div><div class="us-rank-list" id="us-ranking"></div><details class="us-research"><summary>分數怎麼看？</summary><p class="us-note" id="us-score-note"></p></details></section>
  <details class="us-advanced"><summary>進一步看產業強弱位置</summary><section class="us-section"><div class="us-section-head"><div><h2>產業強弱位置</h2><p>右側代表近 20 日比大盤強，上側代表優勢比前一段時間擴大。點選產業可同步查看台灣公司。</p></div></div><div id="us-quadrant"></div></section></details>
  <section class="us-section" id="us-tw-section"><div class="us-section-head"><div><h2>美股產業，對照哪些台灣公司？</h2><p id="us-detail-title">先看美國產業表現，再看台灣公司的相關業務與數據。</p></div></div><div id="us-detail"></div></section>
  <details class="us-advanced"><summary>比較美股產業與台股漲幅</summary><section class="us-section"><div class="us-section-head"><div><h2>美台相對表現落差</h2><p>只看已有業務對照的公司，比較近 20 日股價漲幅；兩地資料日分開列出。</p></div></div><div id="us-gap"></div></section></details>
  <section class="us-section"><div class="us-section-head"><div><h2>風險與止跌觀察</h2><p>先選市場，再看兩件事：壓力是否升高，以及股價是否站回短期高點。</p></div></div><div class="us-alert-grid" id="us-risk-alerts"></div><p class="us-note">指標只供觀察，不代表一定下跌或已經見底。歷史統計可在各卡片展開查看。</p></section>
  <details class="us-advanced"><summary>查看過去的產業排名</summary><section class="us-section"><div class="us-section-head"><div><h2>輪動排名歷史</h2><p>每日快照累積後才形成趨勢；首次建立時歷史不足會明確標示。</p></div></div><div class="us-history" id="us-history"></div></section></details>
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
 let state={snapshot:null,history:[],mapping:[],twCompanies:[],riskProfiles:[],stopProfiles:[],signalExplanations:{},signalTrends:{},signalTrendsUnavailable:false,trendPeriods:{},signalMarket:"us",period:20,selected:null,selectionHistory:[]};
 function empty(el,title,copy){el.innerHTML='<div class="us-empty"><b>'+esc(title)+'</b><p>'+esc(copy)+'</p></div>';}
 function renderSummary(data){
  const rows=data.industries||[],scored=rows.filter(x=>!x.overlay&&x.score!=null).sort((a,b)=>a.rank-b.rank);
  const items=[['目前領先',scored[0]],['轉強觀察',rows.find(x=>x.trend_state==='轉強觀察')],['相對落後',scored[scored.length-1]]];
  $('#us-summary').innerHTML=items.map(([label,row])=>{const value=row&&row.relative_20d_pct;return '<div class="us-summary"><small>'+label+'</small><b>'+esc(row?row.name:'—')+'</b><em class="'+cls(value)+'">'+(value==null?'尚無資料':(value>0?'+':'')+Number(value).toFixed(2))+'</em></div>';}).join('')+'<p class="us-summary-note">數字是近 20 日比美股大盤（SPY）多／少漲的幅度，單位為百分點。</p>';
 }
 function renderHeat(){const rows=state.snapshot&&state.snapshot.industries||[];if(rows.length&&!state.selected)state.selected=rows[0].id;const selected=rows.find(r=>r.id===state.selected);$('#us-selection-note').textContent=selected?'已選取 '+selected.name+'（'+selected.ticker+'） · 下方查看台灣相關業務與公司資料':'';if(!rows.length){empty($('#us-heat'),'尚無美股產業快照','完成第一次批次更新後，這裡會顯示 12 個產業代理 ETF。');return;}
  const key='return_'+state.period+'d_pct';$('#us-heat').innerHTML=rows.map(r=>'<button type="button" aria-pressed="'+String(state.selected===r.id)+'" class="us-heat-card '+(state.selected===r.id?'selected':'')+'" data-industry="'+esc(r.id)+'"><span class="us-heat-top"><b>'+esc(r.name)+'</b><small>'+esc(r.ticker)+(r.overlay?' · 子產業':'')+'</small></span><div class="us-heat-value '+cls(r[key])+'">'+esc(fmt(r[key]))+'</div><span class="us-heat-foot"><span>比美股大盤</span><b class="'+cls(r['relative_'+state.period+'d_pct'])+'">'+esc(r['relative_'+state.period+'d_pct']==null?'資料不足':(r['relative_'+state.period+'d_pct']>=0?'多 ':'少 ')+Math.abs(r['relative_'+state.period+'d_pct']).toFixed(2)+' 個百分點')+'</b></span></button>').join('');root.querySelectorAll('.us-heat-card').forEach(b=>b.addEventListener('click',()=>selectIndustry(b.dataset.industry)));}
 function renderRanking(){const rows=(state.snapshot&&state.snapshot.industries||[]).filter(x=>!x.overlay).sort((a,b)=>(a.rank||99)-(b.rank||99));if(!rows.length){empty($('#us-ranking'),'尚無可排名資料','資料不足時不會補成零分。');return;}
  const max=Math.max(1,...rows.map(x=>Number(x.score)||0));$('#us-ranking').innerHTML=rows.map(r=>'<div class="us-rank-row"><span class="us-rank-no">'+(r.rank||'—')+'</span><span class="us-rank-name"><b>'+esc(r.name)+'</b><small>'+esc(r.ticker)+' · '+esc(r.quadrant||'資料不足')+'</small></span><span class="us-rank-track"><i style="width:'+Math.max(1,(Number(r.score)||0)/max*100)+'%"></i></span><b class="us-rank-score">'+(r.score==null?'—':Number(r.score).toFixed(0)+' 分')+'</b><span class="us-state">'+esc(r.trend_state||'資料不足')+'</span></div>').join('');$('#us-score-note').textContent=(state.snapshot.score_note||'')+' 分數只用來比較產業，不是未來上漲的機率。';}
 function renderQuadrant(){const rows=(state.snapshot&&state.snapshot.industries||[]).filter(r=>!r.overlay&&r.relative_20d_pct!=null&&r.relative_momentum_20d_pct!=null);if(rows.length<2){empty($('#us-quadrant'),'象限資料不足','至少需要 20 日相對報酬及前後兩個 20 日區間。');return;}
  const w=560,h=250,p={l:42,r:16,t:18,b:32},xs=rows.map(r=>Number(r.relative_20d_pct)),ys=rows.map(r=>Number(r.relative_momentum_20d_pct));let xmax=Math.max(1,...xs.map(Math.abs))*1.18,ymax=Math.max(1,...ys.map(Math.abs))*1.18;const X=v=>p.l+(v+xmax)/(2*xmax)*(w-p.l-p.r),Y=v=>p.t+(ymax-v)/(2*ymax)*(h-p.t-p.b);let svg='<svg class="us-chart" viewBox="0 0 '+w+' '+h+'" role="img" aria-label="產業相對強度輪動象限"><line class="us-axis-zero" x1="'+X(0)+'" x2="'+X(0)+'" y1="'+p.t+'" y2="'+(h-p.b)+'"/><line class="us-axis-zero" x1="'+p.l+'" x2="'+(w-p.r)+'" y1="'+Y(0)+'" y2="'+Y(0)+'"/><text class="us-quad-label" x="'+(p.l+6)+'" y="'+(p.t+12)+'">改善</text><text class="us-quad-label" x="'+(w-p.r-42)+'" y="'+(p.t+12)+'">領先</text><text class="us-quad-label" x="'+(p.l+6)+'" y="'+(h-p.b-5)+'">落後</text><text class="us-quad-label" x="'+(w-p.r-42)+'" y="'+(h-p.b-5)+'">轉弱</text><text class="us-chart-label" x="'+(w/2)+'" y="'+(h-3)+'" text-anchor="middle">近 20 日比大盤多／少漲多少</text><text class="us-chart-label" transform="translate(11 '+(h/2)+') rotate(-90)" text-anchor="middle">優勢是否擴大</text>';
  const color=r=>r.quadrant==='領先'?'#C74343':r.quadrant==='改善'?'#B47C37':r.quadrant==='轉弱'?'#638B94':'#0B8159';
  svg+=rows.map(r=>'<circle class="us-dot" cx="'+X(Number(r.relative_20d_pct))+'" cy="'+Y(Number(r.relative_momentum_20d_pct))+'" r="'+(state.selected===r.id?9:6)+'" fill="'+color(r)+'"><title>'+esc(r.name)+' · '+esc(fmt(r.relative_20d_pct))+' · '+esc(r.quadrant)+'</title></circle>').join('')+'</svg>';
  $('#us-quadrant').innerHTML=svg+'<div class="us-quadrant-legend">'+rows.map(r=>'<button type="button" class="us-legend-button" data-quadrant-industry="'+esc(r.id)+'" aria-pressed="'+String(state.selected===r.id)+'"><span class="us-legend-dot" style="background:'+color(r)+'"></span>'+esc(r.name)+' · '+esc(r.quadrant)+'</button>').join('')+'</div>';
  root.querySelectorAll('[data-quadrant-industry]').forEach(b=>b.addEventListener('click',()=>selectIndustry(b.dataset.quadrantIndustry)));}
 function revenueMonth(raw){
  const v=String(raw||'').trim();
  let m=v.match(/^(\d{4})[-/]?(\d{2})$/);if(m&&Number(m[2])>=1&&Number(m[2])<=12)return m[1]+'/'+m[2];
  m=v.match(/^(\d{3})(\d{2})$/);if(m&&Number(m[2])>=1&&Number(m[2])<=12)return (Number(m[1])+1911)+'/'+m[2];
  return v||'月份未提供';
 }
 function renderDetail(){
  const r=(state.snapshot&&state.snapshot.industries||[]).find(x=>x.id===state.selected)||((state.snapshot&&state.snapshot.industries||[])[0]);
  if(!r){empty($('#us-detail'),'先選一個美股產業','資料載入後，可點選上方產業卡片查看台灣業務對照。');return;}
  state.selected=r.id;
  const sectors=state.snapshot.industries||[],previous=sectors.find(x=>x.id===state.selectionHistory[state.selectionHistory.length-1]);
  const controls='<div class="us-tw-navigation"><label for="us-tw-sector">切換產業</label><select id="us-tw-sector" aria-label="切換台灣業務對照產業">'+sectors.map(x=>'<option value="'+esc(x.id)+'"'+(x.id===r.id?' selected':'')+'>'+esc(x.name)+'（'+esc(x.ticker)+'）</option>').join('')+'</select><div class="us-tw-nav-actions">'+(previous?'<button type="button" class="us-legend-button" id="us-tw-back">← 返回'+esc(previous.name)+'</button>':'')+'<button type="button" class="us-legend-button" id="us-tw-overview">↑ 回到熱度圖</button></div></div>';
  const links=state.mapping.filter(x=>x.us_industry===r.id),byCode={};state.twCompanies.forEach(x=>byCode[x.code]=x);
  $('#us-detail-title').textContent='目前選擇：'+r.name+'（'+r.ticker+'） · '+(links.length?links.length+' 家已整理的台灣公司':'台灣對照名單尚未建檔');
  const difference=v=>v==null?'資料不足':v===0?'與大盤相同':(v>0?'多':'少')+' '+Math.abs(v).toFixed(2)+' 個百分點';
  const money=v=>v==null?'—':Number(v).toLocaleString('zh-TW',{maximumFractionDigits:2});
  const metrics=[5,20,60].map(n=>'<tr><th scope="row">'+n+' 個交易日</th><td class="'+cls(r['return_'+n+'d_pct'])+'">'+esc(fmt(r['return_'+n+'d_pct']))+'</td><td>'+esc(difference(r['relative_'+n+'d_pct']))+'</td></tr>').join('');
  const sector='<div class="us-detail-box"><h3>① 美國這個產業最近怎麼走？</h3><p><b>'+esc(r.ticker)+'</b> 是觀察「'+esc(r.name)+'」表現的產業 ETF。下表比較它自己的漲跌，以及比美股大盤 ETF（SPY）多漲或少漲多少。</p><div class="us-return-table"><table><thead><tr><th>期間</th><th>產業漲跌</th><th>與美股大盤比較</th></tr></thead><tbody>'+metrics+'</tbody></table></div><p class="us-note">美股資料日 '+esc(r.as_of||'—')+' · 目前分類：'+esc(r.trend_state||'資料不足')+'</p><details class="us-research"><summary>其他產業指標</summary><p>ETF 成交量是前 20 日均量的 '+(r.relative_volume==null?'—':Number(r.relative_volume).toFixed(2))+' 倍。產業內上漲公司占比：尚無可靠資料。漲跌原因：目前沒有已驗證、附日期的事件資料。</p></details></div>';
  let companies='';
  if(links.length){companies=links.map(x=>{
   const d=byCode[x.code]||{},has=Boolean(d.code);
   const metric=(label,value,note,tone='')=>'<div><small>'+esc(label)+'</small><b class="'+esc(tone)+'">'+esc(value)+'</b><span>'+esc(note)+'</span></div>';
   const numbers=has?'<div class="us-tw-metrics">'+metric('股價收盤',money(d.close),d.price_date||d.as_of_date||'日期未提供')+metric('20 日漲跌',fmt(d.return_20d_pct),'台股自身漲跌',cls(d.return_20d_pct))+metric('營收年增',d.revenue_yoy_pct==null?'資料尚缺':fmt(d.revenue_yoy_pct),revenueMonth(d.revenue_period)+' · 比去年同月',cls(d.revenue_yoy_pct))+'</div>':'<p class="us-note">目前已整理業務關聯，股價與財務資料尚未完成更新。</p>';
   const extra=has?'<p>法人合計：'+esc(d.total_net_lots==null?'資料不足':money(d.total_net_lots)+' 張')+'；近 20 日股價漲跌 '+esc(fmt(d.return_20d_pct))+'。<br>5／20／60 日均價：'+esc([d.ma5,d.ma20,d.ma60].map(money).join(' / '))+'<br>本益比 '+esc(money(d.pe))+' 倍；股價淨值比 '+esc(money(d.pb))+' 倍；殖利率 '+esc(d.dividend_yield_pct==null?'—':money(d.dividend_yield_pct)+'%')+'。</p><p>季 EPS、毛利率與營業利益率：尚無可確認欄位。</p>':'';
   return '<article class="us-tw-company"><h4>'+esc(x.name)+' <small>'+esc(x.code)+'</small></h4><p class="us-tw-product">相關業務：'+esc(x.product)+'</p><p><b>為什麼列在這裡？</b>'+esc(x.reason)+'</p>'+numbers+'<details class="us-research"><summary>更多數據與資料依據</summary>'+extra+'<p>產品分類：'+esc(x.chain)+'<br>公司角色：'+esc(x.role)+'<br>關聯確認程度：'+esc(x.confidence)+'</p><a href="'+esc(x.evidence_url)+'" target="_blank" rel="noopener">查看公司公開資料 ↗</a><div class="us-tw-close-row"><button type="button" class="us-legend-button" data-close-company>收合資料 ↑</button></div></details></article>';
  }).join('');}
  else{
   const available=(state.snapshot.industries||[]).filter(s=>state.mapping.some(m=>m.us_industry===s.id));
   companies='<div class="us-empty"><b>「'+esc(r.name)+'」的台灣對照名單尚未整理</b><p>這代表目前建檔資料不足，不能據此判定台灣沒有相關公司。</p>'+(available.length?'<p>可先查看已整理的產業：</p><div class="us-tw-shortcuts">'+available.map(s=>'<button type="button" class="us-legend-button" data-tw-industry="'+esc(s.id)+'">'+esc(s.name)+'</button>').join('')+'</div>':'')+'</div>';
  }
  $('#us-detail').innerHTML=controls+'<div class="us-tw-intro"><b>看美國產業，對照台灣的相關業務。</b><p>我們依公司公開的產品與業務整理名單，再分開看股價、營收與法人資料。列入名單不代表已確認供貨給某一家美國公司，也不表示股價會跟漲。</p></div><div class="us-detail">'+sector+'<div class="us-detail-box"><h3>② 台灣有哪些公司做相關業務？</h3>'+companies+'</div></div>';
  $('#us-tw-sector').addEventListener('change',e=>selectIndustry(e.target.value,'tw'));
  const back=$('#us-tw-back');if(back)back.addEventListener('click',()=>{const id=state.selectionHistory.pop();if(id)selectIndustry(id,'back');});
  $('#us-tw-overview').addEventListener('click',()=>{const selected=root.querySelector('.us-heat-card.selected');if(selected){selected.scrollIntoView({block:'center',behavior:'auto'});selected.focus({preventScroll:true});}else{$('#us-heat').closest('section').scrollIntoView({block:'start',behavior:'auto'});}});
  root.querySelectorAll('[data-close-company]').forEach(b=>b.addEventListener('click',()=>{const details=b.closest('details');details.open=false;const summary=details.querySelector('summary');requestAnimationFrame(()=>{summary.scrollIntoView({block:'center',behavior:'instant'});summary.focus({preventScroll:true});});}));
  root.querySelectorAll('[data-tw-industry]').forEach(b=>b.addEventListener('click',()=>selectIndustry(b.dataset.twIndustry,'tw')));
 }
 function renderHistory(){const history=state.history||[];if(history.length<2){empty($('#us-history'),'歷史快照尚在累積','至少保存兩個更新日，才顯示產業名次變化；目前不使用今日成分反推過去。');return;}const dates=history.slice(-12);const keys=dates.map(x=>x.as_of);const topIds=(dates[dates.length-1].industries||[]).filter(x=>!x.overlay).sort((a,b)=>(a.rank||99)-(b.rank||99)).slice(0,5).map(x=>x.id);let html='<table><thead><tr><th>產業</th>'+keys.map(x=>'<th>'+esc(x.slice(5))+'</th>').join('')+'</tr></thead><tbody>';topIds.forEach(id=>{const nm=((dates[dates.length-1].industries||[]).find(x=>x.id===id)||{}).name||id;html+='<tr><td>'+esc(nm)+'</td>'+dates.map(day=>{const row=(day.industries||[]).find(x=>x.id===id);return '<td>'+(row&&row.rank?'第 '+row.rank+' 名':'—')+'</td>';}).join('')+'</tr>';});$('#us-history').innerHTML=html+'</tbody></table>';}
 function renderSignalTrends(){
  root.querySelectorAll('[data-signal-chart]').forEach(host=>{
   if(host.closest('[hidden]'))return;
   const key=host.dataset.signalChart,series=(state.signalTrends[key]||{}),all=series.points||[],period=state.trendPeriods[key]||60,points=all.slice(-period);
   if(points.length<2){host.innerHTML='<p class="us-note">'+(state.signalTrendsUnavailable?'近期走勢暫時無法讀取，請稍後重新整理。':'已保存的交易日不足，暫時無法繪製近期走勢。')+'</p>';return;}
   const unit=key==='taiwan'?'個百分點':key.endsWith('_stop')?'點':'',format=v=>Number(v).toLocaleString('zh-TW',{minimumFractionDigits:2,maximumFractionDigits:2});
   const w=Math.max(260,Math.round(host.getBoundingClientRect().width||320)),h=220,p={l:66,r:12,t:18,b:32},values=points.flatMap(x=>[Number(x.value),Number(x.threshold)]);
   let low=Math.min(...values),high=Math.max(...values),pad=Math.max((high-low)*.12,Math.abs(high)*.005,.05);low-=pad;high+=pad;
   const X=i=>p.l+i/(points.length-1)*(w-p.l-p.r),Y=v=>p.t+(high-v)/(high-low)*(h-p.t-p.b);
   const path=field=>points.map((x,i)=>(i?'L':'M')+X(i).toFixed(2)+' '+Y(Number(x[field])).toFixed(2)).join(' ');
   const thresholdLabel=key.endsWith('_stop')?'前五日最高收盤':key==='us'?'VIX 25 門檻':'−3 個百分點門檻';
   let grid='';for(let i=0;i<4;i++){const v=low+(high-low)*i/3,yy=Y(v);grid+='<line x1="'+p.l+'" y1="'+yy+'" x2="'+(w-p.r)+'" y2="'+yy+'" stroke="#e1eaf0"/><text x="'+(p.l-7)+'" y="'+(yy+4)+'" text-anchor="end" fill="#6c8190" font-size="11">'+esc(format(v))+'</text>';}
   const labels=[0,Math.floor((points.length-1)/2),points.length-1].map((i,j)=>'<text x="'+X(i)+'" y="'+(h-10)+'" text-anchor="'+(j===0?'start':j===2?'end':'middle')+'" fill="#6c8190" font-size="12">'+esc(points[i].date.slice(5).replace('-','/'))+'</text>').join('');
   host.innerHTML='<div class="us-trend-head"><b>近期走勢</b><div class="us-trend-periods" role="group" aria-label="'+esc(key)+' 走勢期間">'+[30,60].map(n=>'<button type="button" data-trend-period="'+n+'" aria-pressed="'+String(period===n)+'">'+n+' 日</button>').join('')+'</div></div><div class="us-trend-legend"><span><i></i>'+(key==='taiwan'?'HYG − IEF 20 日報酬差':key==='us'?'VIX 收盤':'指數收盤')+'</span><span><i class="threshold"></i>'+thresholdLabel+'</span></div><div class="us-trend-readout" aria-live="polite"></div><svg class="us-signal-svg" viewBox="0 0 '+w+' '+h+'" tabindex="0" role="group" aria-label="'+esc(key)+' 近 '+points.length+' 個交易日走勢，可點選查看日期與數值"><title>實線為每日數值，虛線為當日門檻</title>'+grid+'<path d="'+path('threshold')+'" class="us-threshold-path"/><path d="'+path('value')+'" class="us-value-path"/><line class="us-trend-cursor" y1="'+p.t+'" y2="'+(h-p.b)+'"/><circle class="us-trend-dot" r="4"/>'+labels+'<rect x="'+p.l+'" y="'+p.t+'" width="'+(w-p.l-p.r)+'" height="'+(h-p.t-p.b)+'" fill="transparent"/></svg><p class="us-trend-caption">'+esc(points[0].date)+' ～ '+esc(points[points.length-1].date)+' · '+points.length+' 個交易日'+(unit?'（'+unit+'）':'')+(series.symbol==='SPY'?' · 使用 SPY 代理':'')+'</p>';
   let index=points.length-1;const svg=host.querySelector('svg');
   const show=i=>{index=Math.max(0,Math.min(points.length-1,i));const q=points[index];host.querySelector('.us-trend-readout').textContent=q.date+' · '+format(q.value)+' · 門檻 '+format(q.threshold);const line=svg.querySelector('.us-trend-cursor');line.setAttribute('x1',X(index));line.setAttribute('x2',X(index));const dot=svg.querySelector('.us-trend-dot');dot.setAttribute('cx',X(index));dot.setAttribute('cy',Y(q.value));};
   const inspect=e=>{const q=svg.createSVGPoint();q.x=e.clientX;q.y=e.clientY;const local=q.matrixTransform(svg.getScreenCTM().inverse());show(Math.round((local.x-p.l)/(w-p.l-p.r)*(points.length-1)));};
   svg.addEventListener('pointerdown',inspect);svg.addEventListener('pointermove',e=>{if(e.pointerType!=='touch')inspect(e);});svg.addEventListener('keydown',e=>{if(e.key==='ArrowLeft'||e.key==='ArrowRight'){e.preventDefault();show(index+(e.key==='ArrowLeft'?-1:1));}});show(index);
   host.querySelectorAll('[data-trend-period]').forEach(b=>b.addEventListener('click',()=>{state.trendPeriods[key]=Number(b.dataset.trendPeriod);renderSignalTrends();}));
  });
 }
 function renderRiskAlerts(){
  const all=state.signalExplanations||{},cur=(state.snapshot&&state.snapshot.current_risk_warning)||{};
  const num=(n,d=2)=>Number(n).toLocaleString('zh-TW',{minimumFractionDigits:d,maximumFractionDigits:d});
  const dateNote=c=>'<span class="us-signal-date">資料日 '+esc(c.as_of||'尚無資料')+' · 採日線收盤判定</span>';
  const stat=(label,value)=>'<div class="us-alert-stat"><span>'+esc(label)+'</span><b>'+esc(value)+'</b></div>';
  const card=(key,title,research)=>{
   const c=all[key]||{},stop=key.endsWith('_stop'),active=c.available&&c.triggered;
   const badge=active?(stop?'已突破':'已達警戒'):(c.available?(stop?'尚未突破':'未達警戒'):'資料不足');
   const unit=c.unit||(stop?'點':key==='us'?'':'個百分點');
   const value=c.available?num(c.value)+(unit?'<small>'+esc(unit)+'</small>':''):'尚無目前資料';
   let condition=stop?(c.available?'收盤須高於 '+num(c.threshold)+' 點，才算突破。':'收盤須高於前五日最高收盤價。'):key==='us'?'警戒線：VIX 收盤達 '+num(c.threshold??25)+'。':'警戒線：差值降到 '+num(c.threshold??-3)+' 個百分點。';
   let distance='資料不足，無法計算目前距離。';
   if(c.available){
    if(stop){
     if(c.equal)distance='目前等於門檻，尚須收盤高於 '+num(c.threshold)+' 點才成立。';
     else distance=active?'已高於門檻 '+num(c.distance)+' 點（'+num(c.distance_pct)+'%）。':'距突破還差 '+num(c.distance)+' 點；需再上漲約 '+num(c.distance_pct)+'%，並收盤高於門檻。';
    }else if(active){distance=c.equal?'目前恰好達到觸發門檻。':'已'+(key==='us'?'高於':'低於')+'門檻 '+num(c.distance)+(unit?' '+unit:'')+'。';}
    else distance=key==='us'?'距警戒線還差 '+num(c.distance)+'。':'再下降 '+num(c.distance)+' 個百分點，就到警戒線。';
   }
   const intro=stop?'收盤站回短期高點，是回穩的觀察線索；不代表已確認見底。':key==='us'?'VIX 可用來觀察美股市場的緊張程度；數字越高，代表市場越緊張。':'比較美國公司債（HYG）與公債（IEF）的 20 日漲跌差。數字越低，表示市場更偏向避險。';
   const proxy=c.proxy_note||((cur[key]||{}).proxy_note)||'';
   return '<article class="us-alert-card '+(active?(stop?'confirm-on':'signal-on'):'')+'" data-signal="'+key+'" data-signal-market="'+(key.startsWith('us')?'us':'taiwan')+'"><div class="us-alert-head"><h3>'+esc(title)+'</h3><span class="us-alert-badge '+(active?(stop?'confirmed':'alert'):'')+'">'+badge+'</span></div><p class="us-signal-intro">'+esc(intro)+'</p><div class="us-alert-value">'+value+'</div><p class="us-signal-condition">'+esc(condition)+'</p><div class="us-signal-distance">'+esc(distance)+'</div>'+dateNote(c)+'<div class="us-signal-chart" data-signal-chart="'+key+'"></div><details class="us-research"><summary>判斷方式與歷史研究（選看）</summary><p class="us-note">'+(stop?'判斷方式：取前五個交易日的最高收盤價，當日收盤必須更高；等於不算突破。':key==='us'?'判斷方式：VIX 收盤大於或等於 25，列為風險警戒。':'判斷方式：HYG 的 20 日報酬減去 IEF 的 20 日報酬，小於或等於 −3 個百分點，列為風險警戒。')+'</p>'+(proxy?'<p class="us-note">'+esc(proxy)+'</p>':'')+research+'</details></article>';
  };
  const cards=state.riskProfiles.map(x=>{
   const key=x.market==='美股'?'us':'taiwan';
   const research=stat('訊號日後 20 交易日急跌比例',num(x.daily_trigger_probability_pct,1)+'%（未觸發樣本 '+num(x.daily_no_trigger_probability_pct,1)+'%）')+stat('歷次警示後真的急跌',x.alert_episode_hits+'/'+x.alert_episodes+' · '+num(x.episode_success_rate_pct,1)+'%')+stat('成功警示提前幾日（中位數）',x.median_lead_sessions+' 個交易日')+stat('急跌前有警示的比例',num(x.event_capture_rate_pct,1)+'%')+stat('發出警示但沒急跌的比例',num(x.episode_no_hit_rate_pct,1)+'%')+'<p class="us-alert-foot">基準急跌比例 '+num(x.base_probability_pct,1)+'%；樣本自 '+esc(x.source_start)+' 起。這是歷史條件統計，不是今天的急跌機率。</p>';
   return card(key,key==='us'?'市場緊張程度 · VIX':'信用市場壓力 · HYG／IEF',research);
  });
  state.stopProfiles.forEach(x=>{
   let research='';['gradual','fast'].forEach(k=>{const p=x[k];research+='<h4 class="us-study-title">'+(k==='fast'?'急跌後止跌樣本':'緩跌後止跌樣本')+'</h4>'+stat('歷史命中率',num(p.success_rate_pct,1)+'%（'+p.hits+'/'+p.signals+'；基準 '+num(p.baseline_pct,1)+'%）')+stat('距之後 20 日低點的天數（中位數）',p.median_days_to_next_20d_low+' 個交易日');});
   research+='<p class="us-alert-foot">命中定義：訊號後十個交易日不破訊號日前十日低點，且第十日收盤高於訊號日。兩組歷史樣本共用目前的突破判定；突破不保證已見底。</p>';
   cards.push(card(x.market==='美股'?'us_stop':'taiwan_stop','有站回短期高點嗎？ · '+x.index,research));
  });
  $('#us-risk-alerts').innerHTML='<div class="us-risk-market-tools"><div class="us-market-switch" role="group" aria-label="風險觀察市場"><button type="button" data-signal-market-select="us">美股</button><button type="button" data-signal-market-select="taiwan">台股</button></div><p class="us-note" id="us-market-help"></p></div>'+cards.join('');
  const setMarket=market=>{state.signalMarket=market;root.querySelectorAll('[data-signal-market-select]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.signalMarketSelect===market)));root.querySelectorAll('[data-signal-market]').forEach(c=>c.hidden=c.dataset.signalMarket!==market);$('#us-market-help').textContent=market==='us'?'先看市場壓力，再看 S&P 500 是否突破短期高點。':'信用指標來自美國債市，作為台股風險參考；加權指數則觀察台股本身。';renderSignalTrends();};
  root.querySelectorAll('[data-signal-market-select]').forEach(b=>b.addEventListener('click',()=>setMarket(b.dataset.signalMarketSelect)));setMarket(state.signalMarket);
 }
 function renderGap(){const mapping=state.mapping||[];const snap=state.snapshot;if(!snap){empty($('#us-gap'),'尚無可比較快照','需先有美股與台股同期報酬資料。');return;}const tw={};(state.twCompanies||[]).forEach(x=>tw[x.code]=x);const industries={};(snap.industries||[]).forEach(x=>industries[x.id]=x);const observations=mapping.map(m=>{const u=industries[m.us_industry],t=tw[m.code];if(!u||!t||u.return_20d_pct==null||t.return_20d_pct==null)return null;return {map:m,us:u,tw:t,gap:Number(u.return_20d_pct)-Number(t.return_20d_pct)};}).filter(x=>x&&x.us.return_20d_pct>0&&x.gap>=5).sort((a,b)=>b.gap-a.gap);if(!observations.length){empty($('#us-gap'),'目前沒有符合條件的比較結果','條件：美股產業近 20 日上漲，且比台股多漲至少 5 個百分點。落後不代表之後一定跟漲。');return;}$('#us-gap').innerHTML=observations.map(x=>'<div class="us-company"><span><b>'+esc(x.map.name)+'（'+esc(x.map.code)+'） · 觀察差距 '+esc(x.gap.toFixed(2))+' 個百分點</b><small>'+esc(x.map.chain)+' · 美股 '+esc(x.map.name)+' 所屬產業代理 '+esc(fmt(x.us.return_20d_pct))+'（截至 '+esc(x.us.as_of||'—')+'），台股 '+esc(fmt(x.tw.return_20d_pct))+'（截至 '+esc(x.tw.price_date||'—')+'）</small><small>月營收年增 '+esc(fmt(x.tw.revenue_yoy_pct))+'；法人合計 '+esc(x.tw.total_net_lots==null?'資料不足':Number(x.tw.total_net_lots).toLocaleString()+' 張')+'。可能原因與估值需另查；此差距不代表補漲。</small><small>供應鏈產品證據：'+esc(x.map.confidence)+'；特定客戶／直接供貨關係未確認。</small></span><a href="'+esc(x.map.evidence_url)+'" target="_blank" rel="noopener">證據 ↗</a></div>').join('');}
 function selectIndustry(id,source){
  if(!(state.snapshot&&state.snapshot.industries||[]).some(r=>r.id===id))return;
  if(id!==state.selected&&state.selected&&source!=='back'){state.selectionHistory.push(state.selected);if(state.selectionHistory.length>30)state.selectionHistory.shift();}
  state.selected=id;renderHeat();renderDetail();renderQuadrant();
  if(source==='tw'||source==='back'){$('#us-tw-sector').focus({preventScroll:true});$('#us-tw-section').scrollIntoView({block:'start',behavior:'auto'});}
 }
 window.addEventListener('resize',()=>{if(root.isConnected)renderSignalTrends();});
 function draw(){renderRiskAlerts();if(!state.snapshot){$('#us-asof').textContent='等待完成快照';$('#us-status').textContent='尚未有完成快照';empty($('#us-summary'),'等待第一筆可信資料','頁面只讀已完成快照，不會在開頁時同步抓行情。');renderHeat();renderRanking();renderQuadrant();renderDetail();renderGap();renderHistory();return;}const s=state.snapshot;$('#us-asof').textContent='美股交易日 '+(s.as_of||'—');let stamp='';try{const d=new Date(String(s.fetched_at||'').replace(' ','T').replace(/(\.\d{3})\d+/,'$1').replace(/([+-]\d{2})$/,'$1:00').replace(/([+-]\d{2})(\d{2})$/,'$1:$2'));if(!Number.isNaN(d.getTime()))stamp=new Intl.DateTimeFormat('zh-TW',{timeZone:'Asia/Taipei',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(d);}catch(e){} const source=String(s.source||'');$('#us-status').textContent='更新 '+(stamp?stamp+'（台北時間）':'時間未提供')+' · '+(source.includes('Yahoo')?'Yahoo Finance 日線':source||'來源未提供');renderSummary(s);renderHeat();renderRanking();renderQuadrant();renderDetail();renderGap();renderHistory();}
 fetch(root.dataset.api+'?t='+encodeURIComponent(new URLSearchParams(location.search).get('t')||''),{credentials:'same-origin',cache:'no-store'}).then(r=>{if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}).then(d=>{state.snapshot=d.snapshot||null;state.history=d.history||[];state.mapping=d.supply_chain||[];state.twCompanies=d.tw_companies||[];state.riskProfiles=d.risk_profiles||[];state.stopProfiles=d.stop_profiles||[];state.signalExplanations=d.signal_explanations||{};state.signalTrends=d.signal_trends||{};state.signalTrendsUnavailable=Boolean(d.signal_trends_unavailable);draw();}).catch(()=>{draw();$('#us-status').textContent='載入失敗，請重新整理頁面';});
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
            trend_error = False
            try:
                risk = (latest or {}).get("current_risk_warning") or {}
                dates = [str(r.get("as_of"))[:10] for r in risk.values()
                         if isinstance(r, dict) and r.get("as_of")]
                end_date = max(dates) if dates else (latest or {}).get("as_of")
                prices = read_signal_prices(get_db_connection, release_db_connection, end_date)
                trends = build_signal_trends(prices, risk)
            except Exception:
                trends, trend_error = {}, True
            return jsonify({"ok": True, "snapshot": latest, "history": history,
                            "signal_trends": trends, "signal_trends_unavailable": trend_error,
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


