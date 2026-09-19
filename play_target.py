"""点击靶 HTTP 服务(仅靶, 不走 PeerJS): 供 WS 通道演示用。

起 127.0.0.1:8799, 用默认浏览器打开 click.html;
页面上的点击/按键通过 POST /click 写进 _clicks.jsonl, 控制端读文件验证。
"""

import http.server
import os
import subprocess
import threading

OUT = os.path.dirname(os.path.abspath(__file__))
PORT = 8799
CLICKS = os.path.join(OUT, "_clicks.jsonl")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=OUT, **kw)

    def do_POST(self):
        if self.path != "/click":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        with open(CLICKS, "a", encoding="utf-8") as fh:
            fh.write(body + "\n")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *a):  # 静音
        pass


def main():
    if os.path.exists(CLICKS):
        os.remove(CLICKS)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"[http] http://127.0.0.1:{PORT}/click.html", flush=True)
    subprocess.Popen(
        ["cmd", "/c", "start", "", f"http://127.0.0.1:{PORT}/click.html"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print("[browser] 已打开靶页", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
