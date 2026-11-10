"""One lightweight reminder worker with durable, correlated completion status."""
from datetime import datetime, timezone
import re
import threading
import uuid


class ReminderJobs:
    def __init__(self, store, run, now=None):
        self.store, self.run = store, run
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.lock = threading.Lock()
        self.active = None

    def write(self, run_id, state, message):
        payload = {"id": run_id, "state": state, "message": message, "updated_at": self.now().isoformat()}
        self.store.save("taiex_reminder_run:" + run_id, payload, data_date=self.now().date())
        self.store.save("taiex_reminder_last_run", payload, data_date=self.now().date())

    def submit(self):
        with self.lock:
            if self.active:
                return self.active
            run_id = str(uuid.uuid4())
            self.write(run_id, "queued", "等待月線檢查")
            self.active = run_id
            try:
                threading.Thread(target=self.execute, args=(run_id,), daemon=True,
                                 name="taiex-close-reminder").start()
            except Exception:
                self.active = None
                self.write(run_id, "error", "背景檢查未啟動")
                raise
            return run_id

    def execute(self, run_id):
        try:
            self.write(run_id, "running", "正在核對官方收盤與提醒狀態")
            message = self.run()
            failed = re.search(r"未確認(\d+)則", message)
            state = ("error" if failed and int(failed[1]) else
                     "busy" if "其他實例" in message else
                     "skipped" if "略過" in message or "尚無訂閱者" in message else "ok")
            self.write(run_id, state, message)
            print("月線檢查完成：" + state + " | " + message, flush=True)
        except Exception as exc:
            message = type(exc).__name__ + (": " + str(exc) if isinstance(exc, ValueError) else "")
            try:
                self.write(run_id, "error", message)
            except Exception:
                pass  # Polling will time out instead of claiming completion.
            print("月線檢查失敗：" + message, flush=True)
        finally:
            with self.lock:
                if self.active == run_id:
                    self.active = None

    def status(self, run_id):
        run_id = str(uuid.UUID(run_id))
        return (self.store.load("taiex_reminder_run:" + run_id, max_age_seconds=86400) or {}).get("payload")
