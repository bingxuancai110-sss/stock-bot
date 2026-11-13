"""Read-only premarket recap of the existing per-user MA20 reminder state."""
from datetime import date, timedelta
from html import escape
import json
import math

COLORS = {"warning":"#9B671B", "reduce":"#B53535", "refill":"#167451",
          "tracking":"#214B5B", "neutral":"#214B5B", "unavailable":"#7A6544"}


def unavailable(detail="最近收盤的月線警示尚未確認，不能判定為無警示。"):
    return {"kind":"unavailable", "title":"月線警示資料待更新", "lines":[detail]}


def summarize(state, today, calendar):
    if not isinstance(state, dict) or not state.get("as_of"):
        return unavailable("提醒已開啟，等待第一筆完成的收盤分析；不補發舊走勢。")
    day = date.fromisoformat(state["as_of"])
    expected = today - timedelta(days=1)
    for _ in range(30):
        if calendar(expected):break
        expected -= timedelta(days=1)
    else:
        return unavailable("交易日資訊不足，尚未確認最近收盤警示。")
    rows = state.get("recent_rows") or []
    row = next((r for r in reversed(rows) if r.get("date") == day.isoformat()), None)
    if day < expected or day > today or not row:
        return unavailable(f"目前僅有 {day.isoformat()} 的紀錄；最近交易日狀態尚待確認。")
    close, ma20 = row.get("close"), row.get("ma20")
    if any(not isinstance(v,(int,float)) or isinstance(v,bool) or not math.isfinite(v) or v<=0 for v in (close,ma20)):
        return unavailable("收盤或月線資料不足，未產生推測訊號。")
    result = {"kind":"neutral", "title":"月線警示｜未有新訊號", "as_of":day.isoformat(),
              "lines":[f"加權收盤 {close:,.2f} · 20日均線 {ma20:,.2f}"]}
    events = [e for e in state.get("events",[]) if e.get("date") == day.isoformat()]
    event = max(events,key=lambda e:int(e.get("level",0)),default={})
    kind = event.get("kind")
    if kind == "warning":
        result.update(kind="warning",title="月線警示｜第2日預警")
        result["lines"].append("尚未確認減碼；下一交易日若仍收在月線下，才是第3日確認。")
    elif kind in {"reduce","refill"}:
        target = 50 if kind == "reduce" else event.get("target_pct")
        if target not in {50,75,100} or not event.get("next_day") or (kind=="refill" and event.get("threshold") not in {3,4.5}):
            return unavailable("策略事件資料不完整，未推測執行比例或日期。")
        result.update(kind=kind,title="月線警示｜第3日確認" if kind=="reduce" else f"月線回補｜反彈{event.get('threshold'):g}%")
        target_day = str(event["next_day"])
        when = "今日收盤" if target_day==today.isoformat() and calendar(today) else target_day+" 收盤"
        result["lines"].append(f"{when}策略目標：股票配置{'降至' if kind=='reduce' else '補至'} {target}%。")
        if event.get("crossed_both"):
            result["lines"].append("同次收盤跨過兩個回補門檻，合併摘要為補至100%。")
    elif (state.get("cycle") or {}).get("pending"):
        pending=state["cycle"]["pending"]
        stage=pending.get("stage");target={0:50,1:75,2:100}.get(stage)
        if target is None or not pending.get("date"):return unavailable()
        result.update(kind="tracking",title="月線策略｜待執行目標")
        result["lines"].append(f"預定 {pending['date']} 收盤股票配置目標 {target}%；不是實際庫存。")
    elif state.get("cycle"):
        result.update(kind="tracking",title="月線策略｜回補追蹤中")
        cycle=state["cycle"];low=cycle.get("low")
        if isinstance(low,(int,float)) and math.isfinite(low) and low>0:
            result["lines"].append(f"本輪最低收盤 {low:,.2f}（{cycle.get('low_date') or '日期未提供'}）。")
            result["lines"].append(f"低點反彈3%補至75%；4.5%補至100%。再破低會下修基準。")
    elif state.get("streak") is None:
        result["lines"].append("開啟時已在月線下，舊走勢不補發；等待下一次有效跌破。")
    elif state.get("streak") == 1:
        result["lines"].append("跌破後第1個交易日，尚未到第2日預警。")
    elif row.get("below"):
        result["lines"].append("目前在月線下；本次沒有新增減碼／回補事件。")
    else:
        result["lines"].append("最近收盤在月線上方，本次沒有新增減碼事件。")
    result["source"] = row.get("source") or "已保存的月線警示快照"
    return result


def read_brief(connect, release, user_id, today, calendar):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout=5000")
            cur.execute("""SELECT s.payload FROM taiex_reduction_subscriptions u
                LEFT JOIN taiex_reduction_user_state s ON s.user_id=u.user_id
                WHERE u.user_id=%s AND u.enabled=TRUE AND u.approved=TRUE""", (user_id,))
            row=cur.fetchone()
        conn.commit()
        if not row:return None
        state=json.loads(row[0]) if isinstance(row[0],str) else row[0]
        return summarize(state,today,calendar)
    except Exception:
        conn.rollback()
        raise
    finally:
        release(conn)


def lines(brief):
    if not brief:return []
    return [brief["title"]] + (["資料日："+brief["as_of"]] if brief.get("as_of") else []) + brief["lines"] + (["來源："+brief["source"]] if brief.get("source") else []) + ["收盤訊號摘要；僅提醒，不會自動交易。"]


def flex(brief):
    if not brief:return None
    color=COLORS[brief["kind"]]
    return {"type":"box","layout":"vertical","margin":"lg","paddingAll":"12px",
            "backgroundColor":"#F3F6F7","cornerRadius":"10px","spacing":"sm",
            "contents":[{"type":"text","text":line,"wrap":True,"size":"sm" if i==0 else "xs",
                         "weight":"bold" if i==0 else "regular","color":color if i==0 else "#40515E"}
                        for i,line in enumerate(lines(brief))]}


def html(brief):
    if not brief:return ""
    content=lines(brief);color=COLORS[brief["kind"]]
    return (f'<section class="card premarket-ma20" aria-label="月線警示摘要" style="border-left:4px solid {color}">'
            f'<h2 style="color:{color}">{escape(content[0])}</h2>'
            +''.join('<p>'+escape(line)+'</p>' for line in content[1:])+'</section>')
