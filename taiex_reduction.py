"""Close-only TAIEX MA20 reminders. No positions or trading writes."""
from datetime import date, timedelta
import math
import time
import uuid
from html import escape

SNAPSHOT_KEY = "taiex_ma20_reduction_v1"
MA_DAYS = 20
WARNING_DAYS = 2
REDUCTION_DAYS = 3


def render_admin(users, settings, csrf_html, message=""):
    options = "".join(f'<option value="{escape(str(row[0]), quote=True)}">'
                      f'{escape(str(row[1]))}（…{escape(str(row[0])[-5:])}）</option>' for row in users)
    rows = []
    for user in users:
        state = settings.get(str(user[0]), {})
        label = ("已開啟" if state.get("enabled") else "已核准・暫停") if state.get("approved") else "未核准"
        rows.append(f'<li>{escape(str(user[1]))}（…{escape(str(user[0])[-5:])}） — {label}</li>')
    flash = f'<p role="status">{escape(message)}</p>' if message else ""
    return f'''<style>.reduction-admin{{max-width:720px;margin:auto}}.reduction-admin section{{padding:18px 0;border-bottom:1px solid #ddd}}
    .reduction-admin select,.reduction-admin button{{min-height:44px;max-width:100%;margin:6px 0}}
    .reduction-admin select{{display:block;width:100%}}.reduction-admin li{{padding:6px 0}}
    </style><div class="reduction-admin"><h1>月線減碼提醒管理</h1>{flash}
    <p>連續2個交易日收盤低於MA20先預警；第3日確認，下一交易日收盤股票配置降至50%。</p>
    <section><h2>使用者權限</h2><form method="post" action="/web/admin/taiex-reduction">
    {csrf_html}<input type="hidden" name="action" value="settings">
    <label>使用者<select name="target" required>{options}</select></label>
    <label>提醒權限<select name="mode"><option value="on">核准並開啟</option>
    <option value="allowed">核准，但暫停通知（本人可自行開啟）</option>
    <option value="off">停用並撤銷權限（本人不能自行開啟）</option></select></label>
    <button type="submit">儲存這位使用者的設定</button></form><ul>{''.join(rows)}</ul></section>
    <section><h2>今天測試一次</h2><p>只傳給目前登入的管理員本人。訊息會標示「測試」，不代表市場觸發訊號，也不影響正式提醒紀錄。每天最多接受一次。</p>
    <form method="post" action="/web/admin/taiex-reduction">{csrf_html}
    <input type="hidden" name="action" value="test"><button type="submit">發送測試到我的 LINE</button></form></section>
    <p>正式檢查排定台灣時間13:40，14:10及15:10重試；排程可能延遲。資料不足不發交易訊號。</p>
    <a href="/web/admin">返回管理員後台</a></div>'''


def evaluate(history, as_of, is_trading_day):
    """MA includes that session's close; equality breaks a below-MA streak."""
    prices = {}
    for day, raw in history:
        day = day if isinstance(day, date) else date.fromisoformat(str(day))
        if day > as_of:
            continue
        value = float(raw)
        if day in prices or not math.isfinite(value) or value <= 0:
            raise ValueError("重複日期或無效指數收盤")
        prices[day] = value
    days = sorted(prices)
    if len(days) < MA_DAYS + REDUCTION_DAYS - 1 or not days or days[-1] != as_of:
        raise ValueError("不足22個交易日或缺少當日正式收盤")
    # Do not count two non-adjacent observations as consecutive trading days.
    cursor = days[0]
    expected = []
    while cursor <= as_of:
        if is_trading_day(cursor):
            expected.append(cursor)
        cursor += timedelta(days=1)
    if expected != days:
        raise ValueError("指數歷史有交易日缺漏或非交易日資料")
    observations = []
    values = [prices[day] for day in days]
    for i in range(MA_DAYS - 1, len(days)):
        ma = math.fsum(values[i - MA_DAYS + 1:i + 1]) / MA_DAYS
        observations.append({"date": days[i].isoformat(), "close": values[i],
                             "ma20": ma, "below": values[i] < ma})
    streak = 0
    for row in reversed(observations):
        if not row["below"]:
            break
        streak += 1
    return {"date": as_of.isoformat(), "streak": streak,
            "level": min(streak, REDUCTION_DAYS), "rows": observations[-3:]}


def format_alert(result, next_day):
    streak = result["streak"]
    if streak == WARNING_DAYS:
        title = "🟠 月線減碼預警｜連續第2個交易日"
        action = ("尚未觸發減碼。若下一交易日仍收在20日均線下，\n"
                  "將確認第3日訊號，再於其下一交易日將股票配置降至50%。")
    elif streak == REDUCTION_DAYS:
        title = "🔴 月線減碼訊號｜連續第3個交易日"
        action = (f"依策略：下一交易日 {next_day.isoformat()} 收盤，\n"
                  "將股票配置降至總資產的50%。\n不是每天再賣掉剩餘持股的一半。")
    else:
        raise ValueError("只在第2／3日建立通知")
    lines = [title, f"訊號日：{result['date']}", ""]
    for row in result["rows"][-streak:]:
        lines.append(f"{row['date']} 收盤 {row['close']:,.2f}｜MA20 {row['ma20']:,.2f}")
    return "\n".join(lines) + "\n\n" + action + (
        "\n\n資料：Yahoo Finance ^TWII 日收盤；當日以TWSE正式收盤覆核。"
        "\n只發策略提醒，不會自動交易或改動持股。\n關閉：輸入「減碼提醒關閉」。")


def push_text(token, user_id, text, retry_key, post, sleep=time.sleep):
    """Private request headers: never mutate the shared LINE client's headers."""
    body = {"to": user_id, "messages": [{"type": "text", "text": text}]}
    for attempt in range(3):
        try:
            response = post("https://api.line.me/v2/bot/message/push", json=body,
                            headers={"Authorization": "Bearer " + token,
                                     "X-Line-Retry-Key": retry_key}, timeout=(3, 8))
            if response.status_code == 200 or (
                    response.status_code == 409 and response.headers.get("x-line-accepted-request-id")):
                return
            if response.status_code != 429 and response.status_code < 500:
                raise ValueError(f"LINE HTTP {response.status_code}")
            response.raise_for_status()
        except ValueError:
            raise
        except Exception:
            if attempt == 2:
                raise
        sleep(0.8 * (2 ** attempt))


class ReductionAlerts:
    def __init__(self, *, connect, release, load, save, history, official,
                 calendar, now, send, quiet, enabled):
        self.connect, self.release = connect, release
        self.load, self.save, self.history, self.official = load, save, history, official
        self.calendar, self.now, self.send = calendar, now, send
        self.quiet, self.enabled = quiet, enabled

    @staticmethod
    def ensure(cur):
        cur.execute("""CREATE TABLE IF NOT EXISTS taiex_reduction_subscriptions (
            user_id TEXT PRIMARY KEY, enabled BOOLEAN NOT NULL,
            approved BOOLEAN NOT NULL DEFAULT FALSE, approved_by TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS taiex_reduction_deliveries (
            signal_date DATE NOT NULL, user_id TEXT NOT NULL, level INTEGER NOT NULL,
            retry_key TEXT NOT NULL, message TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), sent_at TIMESTAMPTZ,
            PRIMARY KEY(signal_date, user_id, level))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS taiex_reduction_job_lock (
            id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL)""")

    def command(self, user_id, command):
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                self.ensure(cur)
                if command in ("減碼提醒開啟", "減碼提醒關閉"):
                    cur.execute("""UPDATE taiex_reduction_subscriptions
                        SET enabled=%s,updated_at=NOW() WHERE user_id=%s AND approved=TRUE""",
                                (command == "減碼提醒開啟", user_id))
                cur.execute("SELECT enabled,approved FROM taiex_reduction_subscriptions WHERE user_id=%s", (user_id,))
                row = cur.fetchone()
            conn.commit()
            if not row or not row[1]:
                return "月線減碼提醒尚未獲管理員核准。請由管理員在「月線減碼提醒管理」開啟你的權限。"
            state = "已開啟" if row and row[0] else "未開啟"
            saved = self.load(SNAPSHOT_KEY, max_age_seconds=7 * 86400)
            result = (saved or {}).get("payload") or {}
            latest = (f"最近檢查：{result['date']}，連續{result['streak']}個交易日低於月線。"
                      if result.get("date") else "尚無完整的收盤檢查結果。")
            return (f"月線減碼提醒：{state}\n\n第2日：預警。\n第3日：確認訊號，下一交易日收盤降至50%。"
                    "\n重新站回月線後，才重新計數。\n\n" + latest +
                    "\n\n開啟：減碼提醒開啟\n關閉：減碼提醒關閉\n僅通知本人，不會自動交易。")
        except Exception:
            conn.rollback()
            raise
        finally:
            self.release(conn)

    def admin_settings(self, admin_id, target=None, mode=None):
        """Caller must validate admin identity and CSRF before invoking a write."""
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                self.ensure(cur)
                if target is not None:
                    if mode not in ("on", "allowed", "off"):
                        raise ValueError("無效提醒權限")
                    cur.execute("""INSERT INTO taiex_reduction_subscriptions
                        (user_id,enabled,approved,approved_by) VALUES (%s,%s,%s,%s)
                        ON CONFLICT(user_id) DO UPDATE SET enabled=EXCLUDED.enabled,
                        approved=EXCLUDED.approved,approved_by=EXCLUDED.approved_by,updated_at=NOW()""",
                                (target, mode == "on", mode != "off", admin_id))
                cur.execute("SELECT user_id,enabled,approved FROM taiex_reduction_subscriptions")
                rows = cur.fetchall()
            conn.commit()
            return {str(row[0]): {"enabled": row[1], "approved": row[2]} for row in rows}
        except Exception:
            conn.rollback()
            raise
        finally:
            self.release(conn)

    def send_test(self, admin_id):
        """Explicit admin click only; once per Taiwan date, never fake a signal."""
        conn = self.connect()
        today = self.now().date()
        try:
            with conn.cursor() as cur:
                self.ensure(cur)
                cur.execute("""INSERT INTO taiex_reduction_deliveries
                    (signal_date,user_id,level,retry_key,message) VALUES (%s,%s,9,%s,%s)
                    ON CONFLICT DO NOTHING""", (today, admin_id, str(uuid.uuid4()),
                    f"🧪 月線減碼提醒｜測試通知\n{today.isoformat()}\n\n"
                    "這是管理員手動測試，並非真實減碼訊號。\n"
                    "正式規則：連續2個交易日低於20日均線先預警；第3日確認，下一交易日收盤配置降至50%。\n"
                    "沒有執行任何交易。"))
                cur.execute("""SELECT retry_key,message,sent_at,
                    created_at > NOW() - INTERVAL '23 hours' FROM taiex_reduction_deliveries
                    WHERE signal_date=%s AND user_id=%s AND level=9""", (today, admin_id))
                key, text, sent_at, safe = cur.fetchone()
            conn.commit()
            if sent_at:
                return "今天的測試已獲LINE接受，為避免重複通知未再次發送。"
            if not safe:
                return "測試結果未確認且已超過安全重試期限，未重送。"
            self.send(admin_id, text, key)
            with conn.cursor() as cur:
                cur.execute("""UPDATE taiex_reduction_deliveries SET sent_at=NOW()
                    WHERE signal_date=%s AND user_id=%s AND level=9""", (today, admin_id))
            conn.commit()
            return "測試訊息已獲LINE接受，請查看你本人的LINE。"
        except Exception:
            conn.rollback()
            raise
        finally:
            self.release(conn)

    def run(self):
        now = self.now()
        today = now.date()
        if (now.hour, now.minute) < (13, 40) or not self.calendar(today):
            return "月線提醒略過：非交易日或未到13:40。"
        if not self.enabled():
            return "月線提醒略過：LINE每日推播已關閉。"
        conn = self.connect()
        locked = False
        owner = str(uuid.uuid4())
        try:
            with conn.cursor() as cur:
                self.ensure(cur)
                # Durable leases also work through Supabase transaction pooling;
                # session-level advisory locks would leak across pooled sessions.
                cur.execute("""INSERT INTO taiex_reduction_job_lock(id,owner,expires_at)
                    VALUES (1,%s,NOW()+INTERVAL '10 minutes')
                    ON CONFLICT(id) DO UPDATE SET owner=EXCLUDED.owner,
                    expires_at=EXCLUDED.expires_at
                    WHERE taiex_reduction_job_lock.expires_at <= NOW()
                    RETURNING owner""", (owner,))
                locked = cur.fetchone() is not None
                if not locked:
                    conn.commit()
                    return "月線提醒已有其他實例執行。"
                cur.execute("SELECT user_id FROM taiex_reduction_subscriptions WHERE enabled=TRUE AND approved=TRUE ORDER BY user_id")
                users = [row[0] for row in cur.fetchall()]
            conn.commit()
            if not users:
                return "月線提醒：尚無訂閱者；請在LINE輸入「減碼提醒開啟」。"
            saved = self.load(SNAPSHOT_KEY, max_age_seconds=86400)
            result = (saved or {}).get("payload") or {}
            if result.get("date") != today.isoformat():
                official = self.official() or {}
                if (not official.get("close_is_final") or
                        str(official.get("date") or "").replace("-", "") != today.strftime("%Y%m%d")):
                    raise ValueError("尚無當日TWSE正式收盤，等待後續排程重試")
                history = [(d, v) for d, v in self.history() if d < today]
                history.append((today, float(str(official["close"]).replace(",", ""))))
                result = evaluate(history, today, self.calendar)
                result["source"] = "Yahoo Finance ^TWII + " + str(official.get("source") or "TWSE")
                if not self.save(SNAPSHOT_KEY, result, data_date=today,
                                 source_meta={"source": result["source"]}):
                    raise RuntimeError("提醒快照未保存，不發送無法追溯的訊號")
            if result["streak"] not in (WARNING_DAYS, REDUCTION_DAYS):
                return f"月線提醒：{today} 低於月線連續{result['streak']}日；無新增第2／3日訊號。"
            next_day = today + timedelta(days=1)
            for _ in range(30):
                if self.calendar(next_day):
                    break
                next_day += timedelta(days=1)
            else:
                raise ValueError("無法確認下一交易日")
            message = format_alert(result, next_day)
            sent = failed = 0
            for uid in users:
                if self.quiet(uid):
                    continue
                with conn.cursor() as cur:
                    cur.execute("""UPDATE taiex_reduction_job_lock
                        SET expires_at=NOW()+INTERVAL '10 minutes'
                        WHERE id=1 AND owner=%s AND expires_at>NOW() RETURNING owner""", (owner,))
                    if cur.fetchone() is None:
                        raise RuntimeError("提醒工作鎖已逾時，停止本次批次")
                    cur.execute("SELECT enabled AND approved FROM taiex_reduction_subscriptions WHERE user_id=%s", (uid,))
                    active = cur.fetchone()
                    if not active or not active[0]:
                        conn.commit()
                        continue
                    cur.execute("""INSERT INTO taiex_reduction_deliveries
                        (signal_date,user_id,level,retry_key,message) VALUES (%s,%s,%s,%s,%s)
                        ON CONFLICT DO NOTHING""", (today, uid, result["level"], str(uuid.uuid4()), message))
                    cur.execute("""SELECT retry_key,message,sent_at,
                        created_at > NOW() - INTERVAL '23 hours'
                        FROM taiex_reduction_deliveries
                        WHERE signal_date=%s AND user_id=%s AND level=%s""", (today, uid, result["level"]))
                    key, original_message, sent_at, retry_safe = cur.fetchone()
                conn.commit()  # Save retry key BEFORE contacting LINE.
                if sent_at or not retry_safe:
                    continue
                try:
                    self.send(uid, original_message, key)
                    with conn.cursor() as cur:
                        cur.execute("""UPDATE taiex_reduction_deliveries SET sent_at=NOW()
                            WHERE signal_date=%s AND user_id=%s AND level=%s""", (today, uid, result["level"]))
                    conn.commit()
                    sent += 1
                except Exception as exc:
                    conn.rollback()
                    failed += 1
                    print("月線提醒推播未確認：" + type(exc).__name__, flush=True)
            return f"月線提醒：{today} 第{result['streak']}日；LINE接受{sent}則、未確認{failed}則。"
        except Exception:
            conn.rollback()
            raise
        finally:
            try:
                if locked:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM taiex_reduction_job_lock WHERE id=1 AND owner=%s", (owner,))
                    conn.commit()
            finally:
                self.release(conn)
