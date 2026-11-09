"""Run the actual workflow shell against a local server with synthetic credentials."""
import os
from pathlib import Path
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).parents[1]


def run_workflow(secret, status=202):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(parse_qs(urlsplit(self.path).query, keep_blank_values=True))
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b'queued' if status == 202 else b'rejected')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        source = (ROOT / '.github/workflows/taiex-reduction.yml').read_text()
        script = source.split('        run: |\n', 1)[1]
        script = '\n'.join(line[10:] for line in script.splitlines())
        script = script.replace('https://stock-bot-6xct.onrender.com',
                                f'http://127.0.0.1:{server.server_port}')
        env = {**os.environ, 'CRON_SECRET': secret}
        result = subprocess.run(['bash', '-e', '-c', script], env=env,
                                capture_output=True, text=True, timeout=10)
        return result, requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_special_characters_reach_post_endpoint_unchanged_and_are_not_logged():
    secret = 'test + &?#%=/中文字 $(not-a-command)'
    result, requests = run_workflow(secret)
    assert result.returncode == 0, result.stderr
    assert requests == [{'token': [secret]}]
    assert 'queued' in result.stdout
    assert secret not in result.stdout + result.stderr


def test_invalid_credentials_fail_clearly_without_retry_or_secret_output():
    secret = 'synthetic-invalid-secret'
    result, requests = run_workflow(secret, 403)
    assert result.returncode != 0
    assert len(requests) == 1
    assert 'authentication rejected' in result.stdout
    assert 'GitHub Actions and Render' in result.stdout
    assert secret not in result.stdout + result.stderr


def test_missing_secret_does_not_send_a_request():
    result, requests = run_workflow('')
    assert result.returncode != 0
    assert requests == []
    assert 'not configured' in result.stdout


def test_unexpected_success_status_does_not_claim_job_was_queued():
    result, requests = run_workflow('synthetic-secret', 200)
    assert result.returncode != 0
    assert 'HTTP 200 instead of 202' in result.stdout
    assert len(requests) == 1
