"""LINE-native reminder cards and administrator controls; no market calculations."""
from urllib.parse import parse_qs, urlencode

NAVY = "#214B5B"
MUTED = "#74808A"


def text(value, size="sm", color="#20313C", **extra):
    return {"type": "text", "text": str(value), "size": size, "color": color, "wrap": True, **extra}


def button(label, action, **params):
    return {"type": "button", "height": "sm", "style": "secondary",
            "action": {"type": "postback", "label": label,
                       "data": urlencode({"tr": "1", "action": action, **params})}}


def message(title, body, footer=None, color=NAVY):
    bubble = {"type": "bubble", "size": "mega",
              "header": {"type": "box", "layout": "vertical", "backgroundColor": color,
                         "paddingAll": "18px", "contents": [text(title, "lg", "#FFFFFF", weight="bold")]},
              "body": {"type": "box", "layout": "vertical", "paddingAll": "18px",
                       "spacing": "md", "contents": body}}
    if footer:
        bubble["footer"] = {"type": "box", "layout": "vertical", "spacing": "sm", "contents": footer}
    return {"type": "flex", "altText": title[:400], "contents": bubble}


def alert_card(result=None, next_day=None, test_date=None):
    if result and result.get("kind") == "refill":
        return refill_card(result)
    testing = test_date is not None
    stage = 0 if testing else result["streak"]
    if not testing and stage not in (2, 3):
        raise ValueError("不支援的提醒等級")
    color = NAVY if testing else ("#AD761F" if stage == 2 else "#B6313C")
    title = "月線減碼提醒｜卡片測試" if testing else ("月線減碼預警｜第 2 日" if stage == 2 else "月線減碼確認｜第 3 日")
    day = str(test_date) if testing else result["date"]
    body = [text(day, "xs", MUTED),
            text("測試通知" if testing else ("先留意，尚未減碼" if stage == 2 else "股票配置降至 50%"),
                 "xl", color, weight="bold")]
    steps = []
    for number, label in ((2, "提前預警"), (3, "確認訊號")):
        steps.append({"type": "box", "layout": "vertical", "backgroundColor": "#F3F6F7",
                      "cornerRadius": "10px", "paddingAll": "12px", "flex": 1,
                      "contents": [text(f"第 {number} 日", "lg", color, weight="bold"), text(label, "xs", MUTED)]})
    body.append({"type": "box", "layout": "horizontal", "spacing": "md", "contents": steps})
    if testing:
        body += [text("這是管理員手動測試，並非真實減碼訊號。", color=color, weight="bold"),
                 text("從月線上方跌破後，連續兩日先預警；第三日確認，下一交易日收盤降至50%。"),
                 text("減碼後最低收盤反彈3%補至75%；反彈4.5%補至100%。", color=NAVY)]
    else:
        body.append({"type": "separator", "color": "#E6EBEE"})
        for row in result["rows"][-stage:]:
            body += [text(row["date"], "xs", MUTED),
                     {"type": "box", "layout": "horizontal", "spacing": "sm", "contents": [
                         text(f"收盤 {row['close']:,.2f}", weight="bold", flex=1),
                         text(f"MA20 {row['ma20']:,.2f}", "xs", MUTED, align="end", flex=1)]}]
        body.append(text("若下一交易日仍低於月線，就確認第3日訊號；再於其下一交易日收盤減碼。"
                         if stage == 2 else f"目標執行：{next_day.isoformat()} 收盤\n股票配置降至總資產50%，不重複每天減半。",
                         color=color, weight="bold"))
        body.append(text(result.get("source") or "Yahoo Finance ^TWII 日收盤；當日以TWSE正式收盤覆核。", "xxs", MUTED))
    body.append(text("只發提醒，不會自動交易或更動持股。", "xs", MUTED))
    return message(title, body, color=color)


def refill_card(event):
    threshold = f"{event['threshold']:g}%"
    body = [text(event["date"], "xs", MUTED),
            text(f"股票配置補至 {event['target_pct']}%", "xl", NAVY, weight="bold"),
            text(f"自本輪最低收盤反彈 {event['rebound_pct']:.2f}%", "md", weight="bold"),
            {"type": "separator", "color": "#E6EBEE"},
            text(f"本輪低點 {event['low']:,.2f} · {event['low_date']}", color=MUTED),
            text(f"當日收盤 {event['close']:,.2f}", "lg", weight="bold"),
            text(f"訊號門檻：{threshold}\n目標執行：{event['next_day']} 收盤", color=NAVY, weight="bold")]
    if event.get("crossed_both"):
        body.append(text("本日一次跨過3%與4.5%，合併提醒補至100%。"))
    body += [text(f"低點自策略預定減碼日 {event['tracking_since']} 起追蹤；若再創低會下修。", "xs", MUTED),
             text("這是策略目標，並非你的實際成交或庫存。只發提醒，不會自動買進。", "xs", MUTED),
             text(event.get("source") or "Yahoo Finance ^TWII；當日以TWSE正式收盤覆核。", "xxs", MUTED)]
    return message(f"月線回補提醒｜反彈 {threshold}", body)


def state_label(state):
    return ("已開啟" if state.get("enabled") else "已核准・暫停") if state.get("approved") else "未核准"


def directory(users, settings, offset=0):
    offset = max(0, min(int(offset), max(0, len(users) - 1)))
    page = users[offset:offset + 5]
    body = [text("點選使用者，直接在 LINE 編輯月線提醒。", color=MUTED)]
    for row in page:
        uid, name = str(row[0]), str(row[1])
        body += [{"type": "separator", "color": "#E6EBEE"},
                 text(name[:100], "md", weight="bold"),
                 text(f"ID …{uid[-5:]} · 月線提醒：{state_label(settings.get(uid, {}))}", "xs", MUTED),
                 button("編輯月線提醒", "view", target=uid)]
    if not page:
        body.append(text("目前沒有使用者。"))
    footer = []
    if offset:
        footer.append(button("上一頁", "list", offset=max(0, offset - 5)))
    if offset + 5 < len(users):
        footer.append(button("下一頁", "list", offset=offset + 5))
    footer += [button("測試新版卡片到我的 LINE", "test"),
               {"type": "button", "height": "sm", "style": "link", "action": {
                   "type": "message", "label": "查看原本完整名單", "text": "名單詳情"}}]
    return message(f"使用者名單 · {len(users)} 人", body, footer)


def editor(user, settings, notice=""):
    uid, name = str(user[0]), str(user[1])
    body = [text(name[:100], "xl", weight="bold"), text(f"ID …{uid[-5:]}", "xs", MUTED),
            text("月線減碼提醒", color=MUTED), text(state_label(settings.get(uid, {})), "lg", NAVY, weight="bold"),
            text("新跌破第2日預警、3日確認降至50%；低點反彈3%／4.5%分批回補。", "xs", MUTED)]
    if notice:
        body.insert(0, text(notice, color="#187357", weight="bold"))
    return message("編輯使用者提醒", body, [
        button("核准並開啟", "set", target=uid, mode="on"),
        button("核准但暫停通知", "set", target=uid, mode="allowed"),
        button("停用並撤銷權限", "set", target=uid, mode="off"),
        button("返回使用者名單", "list")])


def admin_action(actor, source_type, data, *, is_admin, users, service, queue_check):
    """Every postback rechecks actor, private chat, stable target and allowed action."""
    if not is_admin(actor) or source_type != "user":
        return {"type": "text", "text": "這項操作僅限管理員在與 BOT 的一對一聊天中使用。"}
    if len(data) > 300:
        raise ValueError("無效操作")
    params = parse_qs(data, strict_parsing=True, max_num_fields=6)
    if any(len(value) != 1 for value in params.values()) or params.get("tr") != ["1"]:
        raise ValueError("無效操作")
    action = params.get("action", [""])[0]
    if action == "test":
        result = service.send_test(actor)
        return message("卡片測試結果", [text(result), text("只傳給管理員本人。", "xs", MUTED)], [button("返回名單", "list")])
    rows = users()
    if action == "list":
        offset = int(params.get("offset", ["0"])[0])
        return directory(rows, service.admin_settings(actor), offset)
    if action not in ("view", "set"):
        raise ValueError("無效操作")
    target = params.get("target", [""])[0]
    selected = next((row for row in rows if str(row[0]) == target), None)
    if selected is None:
        raise ValueError("找不到使用者，請重新開啟名單")
    notice = ""
    if action == "set":
        mode = params.get("mode", [""])[0]
        if mode not in ("on", "allowed", "off"):
            raise ValueError("無效設定")
        settings = service.admin_settings(actor, target, mode)
        notice = "設定已儲存"
        if mode == "on":
            try:
                queue_check()
            except Exception:
                notice = "設定已儲存；背景檢查暫未排入，將由既有收盤排程檢查。"
    else:
        settings = service.admin_settings(actor)
    return editor(selected, settings, notice)
