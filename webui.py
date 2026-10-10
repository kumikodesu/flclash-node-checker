"""Local browser controls for the Docker edition; measurements reuse checker.run."""
import bootstrap  # Load the portable Python dependencies before checker.
import json
import os
from pathlib import Path
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from checker import parse_args, run
from core import load_nodes, protocol_options, resolve_paths

ROOT = Path(__file__).parent
OUTPUT = Path(os.environ.get("CHECKER_OUTPUT", "/data/results"))
TOKEN = secrets.token_urlsafe(32)


class Batch:
    def __init__(self):
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.state = {"running": False, "message": "就绪", "done": 0, "total": 0, "report": None}

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def start(self, protocol):
        # Read current configuration for every batch, including after a FlClash switch.
        source, _ = resolve_paths()
        _, nodes = load_nodes(source)
        options, _ = protocol_options(nodes)
        if protocol not in options:
            raise ValueError("所选协议已不在当前配置中，请刷新协议列表。")
        args = parse_args(["--protocol", protocol, "--output", str(OUTPUT)])
        with self.lock:
            if self.state["running"]:
                raise ValueError("已有测评正在运行。")
            self.cancel = threading.Event()
            self.state = {"running": True, "message": "正在读取当前配置", "done": 0, "total": 0, "report": None}

        def worker():
            try:
                path = run(args, self.cancel,
                           notify=lambda message: self.update(message=message),
                           progress=lambda done, total: self.update(done=done, total=total))
                self.update(report=f"/reports/{path.parent.name}/report.html")
            except Exception as error:
                # YAML parser errors may contain subscription credentials: do not expose them.
                if isinstance(error, (ValueError, FileNotFoundError, RuntimeError, TimeoutError)):
                    self.update(message=str(error))
                else:
                    self.update(message=f"测评失败：{type(error).__name__}。请检查配置文件和容器依赖。")
            finally:
                self.update(running=False)

        threading.Thread(target=worker, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    batch = Batch()

    def local_host(self):
        # Prevent a remote domain rebound to loopback from reading local reports/tokens.
        return re.fullmatch(r"(?:127\.0\.0\.1|localhost)(?::\d{1,5})?",
                            self.headers.get("Host", "").lower()) is not None

    def reply(self, status, payload, kind="application/json; charset=utf-8"):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self.local_host():
            return self.reply(403, {"error": "请通过 127.0.0.1 或 localhost 访问本机服务。"})
        path = urlsplit(self.path).path
        if path == "/health":
            return self.reply(200, {"ok": True})
        if path == "/":
            page = (ROOT / "webui.html").read_text(encoding="utf-8").replace("__TOKEN__", TOKEN)
            return self.reply(200, page.encode(), "text/html; charset=utf-8")
        if path == "/api/status":
            return self.reply(200, self.batch.snapshot())
        if path == "/api/protocols":
            try:
                source, _ = resolve_paths()
                _, nodes = load_nodes(source)
                options, _ = protocol_options(nodes)
                return self.reply(200, {"protocols": options, "count": len(nodes)})
            except Exception as error:
                return self.reply(400, {"error": f"读取配置失败（{type(error).__name__}）。请检查只读挂载的 config.yaml 和集合缓存。"})
        match = re.fullmatch(r"/reports/(\d{8}-\d{6}-\d{6})/(report.html|results.json|results.csv)", path)
        if match:
            file = OUTPUT / match[1] / match[2]
            if file.is_file():
                kind = {"report.html": "text/html; charset=utf-8", "results.json": "application/json; charset=utf-8",
                        "results.csv": "text/csv; charset=utf-8"}[match[2]]
                return self.reply(200, file.read_bytes(), kind)
        self.reply(404, {"error": "页面不存在"})

    def do_POST(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1024:
                raise ValueError("请求长度无效。")
            raw = self.rfile.read(size)
            # Drain the bounded body before rejecting, so closing does not reset the response.
            if (not self.local_host() or self.headers.get("X-Checker-Token") != TOKEN
                    or self.headers.get("Content-Type") != "application/json"):
                return self.reply(403, {"error": "请求校验失败，请刷新页面。"})
            data = json.loads(raw)
            if self.path == "/api/start":
                self.batch.start(data.get("protocol", "all"))
            elif self.path == "/api/stop":
                self.batch.cancel.set()
            else:
                return self.reply(404, {"error": "接口不存在"})
            self.reply(200, self.batch.snapshot())
        except (ValueError, TypeError, FileNotFoundError, RuntimeError) as error:
            self.reply(400, {"error": str(error)})
        except Exception as error:
            self.reply(400, {"error": f"读取配置失败（{type(error).__name__}），请检查挂载目录。"})


if __name__ == "__main__":
    OUTPUT.mkdir(parents=True, exist_ok=True)
    print("FlClash 节点评分网页服务已启动，端口 8080", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
