"""Run the short-term scan in a separate process, using the existing checkpoints."""

import os
import signal
from threading import Event


def main():
    os.environ["STOCKBOT_PROCESS"] = "short-term-worker"
    from main import _short_term_service as service, connection_pool

    stopping = Event()

    def stop(_signum, _frame):
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if os.environ.get("SHORT_TERM_WORKER_ENABLED") != "1":
        print("短線獨立服務已連線，等候切換掃描", flush=True)
        try:
            stopping.wait()
        finally:
            connection_pool.closeall()
        return
    print("短線獨立掃描服務已啟動", flush=True)
    try:
        while not stopping.is_set():
            try:
                now = service.now()
                clock = now.hour, now.minute
                if service.calendar(now.date()) and ((9, 0) <= clock < (13, 35) or (14, 0) <= clock < (23, 50)):
                    service.trigger = "獨立掃描服務"
                    service.payload(schedule=True, quote_only=True)
            except Exception as exc:
                print("短線獨立掃描服務暫緩：" + type(exc).__name__, flush=True)
            stopping.wait(15)
    finally:
        service.executor.shutdown(wait=True, cancel_futures=True)
        connection_pool.closeall()


if __name__ == "__main__":
    main()
