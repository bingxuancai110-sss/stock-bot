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
    for _, _, _, _, note, _ in rows:
        stamp, _, source = note.partition(" · ")
        try:
            parsed = datetime.fromisoformat(stamp.replace("/", "-").replace("Z", "+00:00"))
            if len(stamp) < 16:
                raise ValueError("missing time")
            if parsed.tzinfo:
                parsed = parsed.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
            stamps.append(parsed)
        except ValueError:
            unknown = True
        if source and source not in sources:
            sources.append(source)
    if stamps:
        first, last = min(stamps), max(stamps)
        date_text = first.strftime("%Y-%m-%d %H:%M:%S")
        if last != first:
            date_text += "–" + last.strftime("%H:%M:%S" if first.date() == last.date() else "%Y-%m-%d %H:%M:%S")
        timing = "行情時間 " + date_text + ("；部分時間未提供" if unknown else "")
    else:
        timing = "行情時間未提供"
    return [text(timing, "xxs", MUTED, wrap=True),
            text("來源：" + "／".join(sources or ["最近可用行情"]), "xxs", MUTED, wrap=True)]


def render_holdings(rows):
    stocks = []
    for i, (name, code, weight, pct, note, limit) in enumerate(rows):
        value = f"{pct:+.2f}%" if pct is not None else "待更新"
        color = "#D93025" if pct is not None and pct >= 0 else "#18845B" if pct is not None else MUTED
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
                     {"type": "box", "layout": "vertical", "width": f"{max(0, min(100, weight)):.1f}%",
                      "height": "6px", "backgroundColor": "#2D8B92", "cornerRadius": "3px", "contents": []}]}
        content = [{"type": "box", "layout": "horizontal", "spacing": "sm", "alignItems": "center",
                    "contents": [text(f"{name}（{code}）", weight="bold", wrap=True, flex=1),
                                 text(f"{weight:.1f}%", "md", weight="bold", align="end", flex=0)]},
                   track,
                   {"type": "box", "layout": "horizontal", "spacing": "sm", "alignItems": "center",
                    "margin": "xs", "contents": [text("今日漲跌", "xxs", MUTED, flex=1), change]}]
        if pct is None:
            content.append(text("行情待更新 · " + note, "xxs", MUTED, wrap=True, margin="xs"))
        if i < len(rows) - 1:
            content.append({"type": "separator", "margin": "md", "color": "#E9EEF3"})
        stocks.append({"type": "box", "layout": "vertical", "margin": "md", "contents": content})
    return [text("秉軒的投資組合", "xl", "#203449", weight="bold"),
            text("持股配置比例", "xs", MUTED, margin="sm"),
            {"type": "box", "layout": "vertical", "contents": stocks},
            {"type": "separator", "margin": "md", "color": "#E9EEF3"},
            {"type": "box", "layout": "vertical", "spacing": "xs", "margin": "sm", "contents": quote_footer(rows)}]
