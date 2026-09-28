"""Local-only QA fixtures, shared by direct and real stdio tests."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time

HTML = b"""<!doctype html><title>Gateway QA fixture</title>
<h1>Local QA</h1><label>Name <input id="name"></label>
<button id="go" onclick="document.querySelector('#result').textContent=document.querySelector('#name').value; console.error('qa console error'); setTimeout(()=>{throw new Error('qa page error')},0)">Apply</button>
<p id="result">waiting</p>"""


@contextmanager
def local_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(503 if self.path == "/unavailable" else 200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            try:
                if self.path == "/slow":
                    for _ in range(50):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(.1)
                else:
                    self.wfile.write(HTML)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
