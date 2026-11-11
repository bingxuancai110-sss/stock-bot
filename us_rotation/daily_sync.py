"""Small daily batch contract, independent of Taiwan research and market scans."""
import csv
from datetime import datetime, timedelta, timezone
from io import StringIO
from zoneinfo import ZoneInfo

from .importer import parse_history_csv, REQUIRED_SYMBOLS

PATH = "/cron/us-rotation-ingest"
AUDIENCE = "https://stock-bot-6xct.onrender.com" + PATH
WORKFLOW = "bingxuancai110-sss/stock-bot/.github/workflows/us-rotation-daily.yml@refs/heads/main"
RISK_SYMBOLS = {"^VIX", "HYG", "IEF", "^GSPC"}
MAX_BYTES = 2 * 1024 * 1024


def encode_closed_history(histories, now=None):
    """Exclude today's US session until after its daily close; preserve real OHLCV."""
    now = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    cutoff = now.date() if now.hour >= 17 else now.date() - timedelta(days=1)
    out = StringIO()
    fields = ["symbol", "trade_date", "open", "high", "low", "close", "adjusted_close", "volume"]
    writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for symbol, rows in histories.items():
        for row in rows:
            if str(row["trade_date"]) <= cutoff.isoformat():
                writer.writerow({**row, "symbol": symbol})
    text = out.getvalue()
    parsed, _ = parse_history_csv(text)
    validate_risk_dates(parsed)
    if len(text.encode()) > MAX_BYTES:
        raise ValueError("每日行情超過匯入上限")
    return text


def validate_risk_dates(histories):
    latest = histories["SPY"][-1]["trade_date"]
    for symbol in REQUIRED_SYMBOLS | RISK_SYMBOLS:
        rows = histories.get(symbol) or []
        if not rows or rows[-1]["trade_date"] != latest:
            raise ValueError("每日行情未齊備或日期未對齊：" + symbol)
    return latest


def freshness_warning(snapshot, now=None):
    if not snapshot:
        return ""
    try:
        stamp = datetime.fromisoformat(str(snapshot.get("fetched_at") or "").replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = ((now or datetime.now(timezone.utc)) - stamp).total_seconds()
        return "更新已逾72小時，以下為歷史快照，並非目前市場狀態。" if age > 72 * 3600 else ""
    except (TypeError, ValueError):
        return "更新時間未確認，請核對資料日期。"
