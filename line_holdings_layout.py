"""Presentation-only LINE holdings rows. No quote fetches or accounting."""
from datetime import datetime, timedelta, timezone

NAVY = "#24364B"
MUTED = "#667085"
LIMIT_COLORS = {"漲停": "#BC2936", "跌停": "#10704E"}


def text(value, size="sm", color=NAVY, **options):
    return {"type": "text", "text": str(value), "size": size, "color": color, **options}


def quote_footer(rows):
    stamps, sources = [], []
    unknown = False
    for row in rows:
        note = row[4] if len(row) > 4 else ""
        stamp, _, source = note.partition(" · ")
        try:
            parsed = datetime.fromisoformat(stamp.replace("/", "-").replace("Z", "+00:00"))
            if parsed.tzinfo:
                parsed = parsed.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
            stamps.append(parsed)
        except ValueError:
            unknown = True
        if source and source not in sources:
            sources.append(source)
    if stamps:
        first, last = min(stamps), max(stamps)
        if first.time() == datetime.min.time() and last.time() == datetime.min.time():
            date_text = "最近收盤日 " + first.strftime("%Y-%m-%d")
            if last.date() != first.date():
                date_text += "–" + last.strftime("%Y-%m-%d")
        else:
            date_text = first.strftime("%Y-%m-%d %H:%M:%S")
        if last != first and first.time() != datetime.min.time():
            date_text += "–" + last.strftime("%H:%M:%S" if first.date() == last.date() else "%Y-%m-%d %H:%M:%S")
        timing = (date_text if date_text.startswith("最近收盤日 ") else "行情時間 " + date_text)
        timing += "；部分時間未提供" if unknown else ""
    else:
        timing = "行情時間未提供"
    return [text(timing, "xxs", MUTED, wrap=True),
            text("來源：" + "／".join(sources or ["最近可用行情"]), "xxs", MUTED, wrap=True)]


def render_holdings(rows):
    stocks = []
    for i, row in enumerate(rows):
        name, code, weight, pct, note, limit = row[:6]
        pct_label = row[6] if len(row) > 6 else "今日漲跌"
        pct_date = row[7] if len(row) > 7 else None
        value = f"{pct:+.2f}%" if pct is not None else "待更新"
        color = "#D93025" if pct is not None and pct >= 0 else "#18845B" if pct is not None else MUTED
        weight_text = f"{weight:.1f}%" if weight is not None else "待更新"
        # The caller supplies provider-price-based limit status, never a rounded % inference.
        if limit in LIMIT_COLORS and pct is not None:
            change = {"type": "box", "layout": "horizontal", "spacing": "xs", "flex": 0,
                      "paddingAll": "5px", "cornerRadius": "4px", "backgroundColor": LIMIT_COLORS[limit],
                      "contents": [text(limit, "xs", "#FFFFFF", weight="bold", flex=0),
                                   text(value, "sm", "#FFFFFF", weight="bold", flex=0)]}
        else:
            change = text(value, "sm", color, weight="bold", align="end", flex=0)
        track = {"type": "box", "layout": "horizontal", "height": "6px", "margin": "xs",
                 "backgroundColor": "#E9EEF3", "cornerRadius": "3px", "contents": [
                      {"type": "box", "layout": "vertical", "width": f"{max(0, min(100, weight or 0)):.1f}%",
                      "height": "6px", "backgroundColor": "#2D8B92", "cornerRadius": "3px", "contents": []}]}
        cash = code == "cash"
        if cash:
            track["contents"][0]["backgroundColor"] = "#5E7188"
        content = [{"type": "box", "layout": "horizontal", "spacing": "sm", "alignItems": "center",
                    "contents": [text(name if cash else f"{name}（{code}）", weight="bold", wrap=True, flex=1),
                                 text(weight_text, "md", color=NAVY if weight is not None else MUTED,
                                      weight="bold", align="end", flex=0)]},
                   track]
        if not cash:
            content.append({"type": "box", "layout": "horizontal", "spacing": "sm", "alignItems": "center",
                            "margin": "xs", "contents": [text(pct_label, "xxs", MUTED, flex=1), change]})
        if not cash and pct_date:
            note = f"資料截至 {pct_date} 收盤 · " + (note.partition(" · ")[2] or "最近可用行情")
            content.append(text(note, "xxs", MUTED, wrap=True, margin="xs"))
        elif not cash and pct is None:
            content.append(text("行情待更新 · " + note, "xxs", MUTED, wrap=True, margin="xs"))
        if i < len(rows) - 1:
            content.append({"type": "separator", "margin": "md", "color": "#E9EEF3"})
        stocks.append({"type": "box", "layout": "vertical", "margin": "md", "contents": content})
    items = [text("秉軒的投資組合", "xl", "#203449", weight="bold"),
            text("持股配置比例", "xs", MUTED, margin="sm"),
            {"type": "box", "layout": "vertical", "contents": stocks}]
    stock_rows = [row for row in rows if row[1] != "cash"]
    if stock_rows:
        items.extend([{"type": "separator", "margin": "md", "color": "#E9EEF3"},
                      {"type": "box", "layout": "vertical", "spacing": "xs", "margin": "sm",
                       "contents": quote_footer(stock_rows)}])
    return items
