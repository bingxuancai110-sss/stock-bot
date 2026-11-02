import ast,json,html,re,math,os
from pathlib import Path
from datetime import date,datetime,timedelta
repo=Path(__file__).resolve().parents[2]
root=Path(os.environ.get('STOCKBOT_UI_ARTIFACTS','/tmp/stockbot-ui-validation'));root.mkdir(parents=True,exist_ok=True)
s=repo.joinpath('main.py').read_text();lines=s.splitlines(True);tree=ast.parse(s)
ns=dict(html=html,re=re,json=json,math=math,date=date,datetime=datetime,timedelta=timedelta,taiwan_today=lambda:date(2026,11,2),taiwan_now=lambda:datetime(2026,11,2,15),current_web_user=lambda:None,get_web_ui_theme=lambda u:'mist',inject_csrf_inputs=lambda b:b,preserve_web_token=lambda b:b)
def compile_node(n,local=ns):
 # Strip Flask decorators: these are isolated renderers, no app/database import.
 if isinstance(n,ast.FunctionDef):n.decorator_list=[]
 exec(compile(ast.Module(body=[n],type_ignores=[]),'<actual-renderer>','exec'),local)
for name in ['_hv2_scope','_hv2_bang','compute_twr','fmt_pct','render_page','render_trend_chart','render_leaderboard_chart','_hv2_group_html','_hv2_impact_card','_render_home_v2','render_workbench_body']:
 n=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name);compile_node(n)
for n in tree.body:
 if isinstance(n,ast.Assign):
  names=[getattr(t,'id','') for t in n.targets]
  if any(name in {'BASE_CSS','GLOBAL_VISUAL_CSS','STOCKBOT_V2_TEMPLATE_CSS','USER_UI_THEME_CSS','VISUAL_ONLY_POLISH_CSS','WEB_UI_THEMES','WORKBENCH_CSS','WORKBENCH_TOUCH_CSS','_HOME_IMPACT_STYLE','_HOME_FEEDBACK_STYLE'} or name.startswith('_HV2_') for name in names):
   if not isinstance(n.value,ast.Call):compile_node(n)
# Actual holdings renderer statements; only portfolio and DB-derived inputs are synthetic.
positions=[dict(code=str(1001+i),name='測試公司'+str(i+1),shares=100,cost=90,bought_on=date(2026,10,1),lots=[]) for i in range(7)]
positions[3].update(code='00981A',name='主動統一台股增長',shares=1063,bought_on=date(2026,4,22))
enriched=[(p,dict(name=p['name'],close=[1355,1510,4705,26.57,1265,1060,637][i],pct=1.2 if i%2==0 else -2.1,source='隔離測試行情'),p['shares']*[1355,1510,4705,26.57,1265,1060,637][i],p['shares']*90) for i,p in enumerate(positions)]
local=dict(ns,positions=positions,enriched=enriched,total_value=sum(r[2] for r in enriched),total_cost=63000,pl_total=20,fee_total=100,total_fee=100,total_day_pl=0,rows_html=[],allocation_items=[],target_weights={},inst={},fee_disc=1,min_fee=20,
 stock_display_name=lambda code,*args:next((p['name'] for p in positions if p['code']==code),code),net_profit=lambda *args:(1000,11.11,20),lots_html=lambda *args:'',csrf_hidden_input=lambda:'<input type="hidden" name="csrf_token" value="fixture-only">',_is_taiwan_intraday_window=lambda:False,msg='',exright_html='')
pts=[]
for i in range(80):
 d=date(2026,7,15)+timedelta(days=i)
 if d.weekday()<5:pts.append(dict(date=d,value=10000+i*50,cost=10000,taiex=22000+i*15))
local['portfolio_trend_html']='<section class="portfolio-trend-card">'+ns['render_trend_chart'](pts)+'</section>'
fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='web_positions')
# Use a small function AST for fast statement extraction.
x=ast.parse(''.join(lines[fn.lineno-1:fn.end_lineno])).body[0]
for node in x.body:
 selected=isinstance(node,ast.For) and any(isinstance(a,ast.Call) and isinstance(a.func,ast.Attribute) and isinstance(a.func.value,ast.Name) and a.func.value.id=='rows_html' and a.func.attr=='append' for a in ast.walk(node))
 selected=selected or isinstance(node,ast.Assign) and any(getattr(t,'id','') in {'totals','allocation_html','holding_card_css','body'} for t in node.targets)
 selected=selected or isinstance(node,ast.If) and isinstance(node.test,ast.Name) and node.test.id=='allocation_items'
 selected=selected or isinstance(node,ast.AugAssign) and getattr(node.target,'id','')=='body'
 if selected:compile_node(node,local)
body=local['body'];root.joinpath('positions-fragment.html').write_text(body)
root.joinpath('positions.html').write_text(ns['render_page']('持股',body,'positions'))
# Actual leaderboard chart; fixed test memberships and actual board DOM contract.
series={str(i):dict(nickname='測試會員'+str(i),curve=[(p['date'],i+j*.1) for j,p in enumerate(pts)]) for i in range(1,5)}
ns['safe_html_text']=lambda text:html.escape(str(text))
chart=ns['render_leaderboard_chart'](series,[(p['date'],j*.07) for j,p in enumerate(pts)],list(series))
rank='<div class="tabs rank-tabs" id="rankTabs">'+''.join('<button type="button" data-board="'+k+'" class="'+('on' if k=='short' else '')+'">'+label+'</button>' for k,label in [('short','短線 近30天'),('long','長線 累計'),('season','賽季 本季')])+'</div>'
for k in ['short','long','season']:
 rank+='<section data-board-panel="'+k+'" style="'+('' if k=='short' else 'display:none')+'"><div class="rank-rows">'+''.join('<article class="rank-card"><div class="rank-primary-line"><b class="rank-number">#'+str(i)+'</b><span class="name">測試會員'+str(i)+'</span></div><b class="rank-return up">+'+str(i)+'.20%</b><span class="rank-move '+('up' if i%2 else 'down')+'">'+('↑ 2 名' if i%2 else '↓ 1 名')+'</span><details class="rank-detail"><summary>持股明細</summary><div>合成持股明細</div></details></article>' for i in range(1,5))+'</div>'+chart+'</section>'
# Use the actual existing board-switch listener, not a test replacement.
rank_fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='web_leaderboard');rank_source=''.join(lines[rank_fn.lineno-1:rank_fn.end_lineno]);start=rank_source.index('var tabs = document.getElementById(\'rankTabs\')');end=rank_source.index('</script>',start)
rank+='<script>(function(){'+rank_source[start:end].replace('{{','{').replace('}}','}')+'</script>'
# Include every legacy leaderboard style block so specificity regressions are tested.
rank='<style>'+ '\n'.join(re.findall(r'<style[^>]*>(.*?)</style>',rank_source,re.S)).replace('{{','{').replace('}}','}')+'</style>'+rank
rank+='<section class="leaderboard-history" id="leaderboard-history"><div class="tabs history-tabs" id="historyTabs"><button type="button" class="on" data-history="months">月榜</button><button type="button" data-history="seasons">賽季榜</button></div><div data-history-panel="months">合成月榜資料</div><div data-history-panel="seasons" style="display:none">合成賽季資料</div></section>'
history_start=rank_source.index("var t=document.getElementById('historyTabs')");history_end=rank_source.index('</script>',history_start)
rank+='<script>(function(){'+rank_source[history_start:history_end].replace('{{','{').replace('}}','}')+'</script>'
root.joinpath('rank-fragment.html').write_text(rank);root.joinpath('rank.html').write_text(ns['render_page']('排行榜',rank,'leaderboard'))
# Native settings forms, slow/error lazy pane, and numeric contracts.
settings='<form id="settings-form" method="post" action="/web/settings"><input name="nickname" value="合成帳號" required><button type="submit">儲存設定</button></form><details><summary>展開內容</summary><div style="height:160px">資料內容</div></details><div id="number-test" data-ui-number>1,234.50</div><div id="load-test"></div>'
root.joinpath('settings-fragment.html').write_text(settings);root.joinpath('settings.html').write_text(ns['render_page']('設定',settings,'settings'))
# Actual initial loading shell, without requiring a live Flask app.
ns.update(render_quote_block=lambda:'',request=type('Request',(),{'args':{},'path':'/web/positions'})())
n=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='render_loading_shell');compile_node(n)
root.joinpath('loading.html').write_text(ns['render_loading_shell']('持股','positions',['讀取資料'],staged=False))
# Validate every inline script syntax separately from the module.
for name in ['positions','rank','settings','loading']:
 scripts=re.findall(r'<script(?![^>]*src=)[^>]*>(.*?)</script>',root.joinpath(name+'.html').read_text(),re.S)
 root.joinpath(name+'.js').write_text('\n'.join(scripts))
print('Actual shell, holdings cards/allocations/scripts, portfolio chart, leaderboard chart/switch listener; explicit synthetic inputs.')

# Homepage: real contribution/holdings renderers with synthetic data.
home_fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='render_daily_home_top')
home_source=''.join(lines[home_fn.lineno-1:home_fn.end_lineno]);home_ast=ast.parse(home_source).body[0]
home_ns=dict(ns,contribution_basis_text=lambda h,**kw:'佔組合 10%',realized_today_text=lambda h:'')
row_fn=next(n for n in home_ast.body if isinstance(n,ast.FunctionDef) and n.name=='contribution_detail_rows');compile_node(row_fn,home_ns)
home_ns.update(holdings=positions,_home_preview_rows=[dict(**p,price=100,value=10000) for p in positions],_home_preview_total=70000,price_map={p['code']:dict(close=100,pct=1.2) for p in positions})
active=False
for n in home_ast.body:
 if isinstance(n,ast.Assign) and any(getattr(t,'id','')=='_home_holding_rows' for t in n.targets):active=True
 if active:compile_node(n,home_ns)
 if isinstance(n,ast.Assign) and any(getattr(t,'id','')=='_home_holdings_preview_html' for t in n.targets):break
pos=[(p,1.2,.1+i*.1) for i,p in enumerate(positions[:4])];neg=[(p,-1.5,-.2-i*.1) for i,p in enumerate(positions[4:])]
groups=ns['_hv2_group_html']('up','推升組合',len(pos),home_ns['contribution_detail_rows'](pos,'up'))+ns['_hv2_group_html']('down','拖累組合',len(neg),home_ns['contribution_detail_rows'](neg,'down'))
data=dict(total_value=70000,total_cost=63000,holdings_count=7,pl_html=ns['fmt_pct'](11.1),market_html=ns['fmt_pct'](-1),portfolio_label='你的組合',portfolio_html=ns['fmt_pct'](1),relative_html=ns['fmt_pct'](2),market_status='合成測試行情',sync_title='測試資料',sync_detail='畫面測試用',contribution_day_label='今日',stale_notice='',impact_positive=dict(name=pos[0][0]['name'],pct=1.2,contribution=.1),impact_negative=dict(name=neg[0][0]['name'],pct=-1.5,contribution=-.2),contribution_groups=groups,holdings_html=home_ns['_home_holdings_preview_html'],journal_html='',judgement_html='<p>合成組合判讀</p>',live_script='')
home=ns['_render_home_v2'](data);root.joinpath('home-fragment.html').write_text(home);root.joinpath('home.html').write_text(ns['render_page']('今日',home,'portfolio'))
workbench=ns['render_workbench_body']();root.joinpath('workbench.html').write_text(ns['render_page']('選股工作台',workbench,'screener'))
