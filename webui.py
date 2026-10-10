"""Local browser controls for the Docker edition; measurements reuse checker.run."""
import bootstrap  # Load the portable Python dependencies before checker.
import json
import os
from pathlib import Path
import re
import secrets
import threading
import yaml
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from checker import parse_args, run
from core import load_nodes, protocol_options, resolve_paths
from config_import import MAX_IMPORT_BYTES, bundle_config

ROOT = Path(__file__).parent
OUTPUT = Path(os.environ.get("CHECKER_OUTPUT", "/data/results"))
TOKEN = secrets.token_urlsafe(32)
IMPORTED = Path(os.environ.get("CHECKER_IMPORTED_CONFIG", "/data/imported-config.yaml"))


class Batch:
    def __init__(self):
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.use_imported = False
        self.state = {"running": False, "message": "就绪", "done": 0, "total": 0, "report": None}

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def configuration(self):
        source, _ = resolve_paths(IMPORTED if self.use_imported else None)
        _, nodes = load_nodes(source)
        options, _ = protocol_options(nodes)
        mode = "imported" if source == IMPORTED.resolve() else "mounted"
        return source, {"protocols": options, "count": len(nodes), "source_mode": mode}

    def import_config(self, files):
        with self.lock:
            if self.state["running"]:
                raise ValueError("请先停止测评并等待结束，再导入配置。")
            content, count = bundle_config(files)
            IMPORTED.parent.mkdir(parents=True, exist_ok=True)
            temporary = IMPORTED.with_suffix(".yaml.tmp")
            temporary.write_text(content, encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(IMPORTED)
            self.use_imported = True
            self.state["message"] = f"已导入 {count} 个节点，可开始测评。"
            return self.configuration()[1]

    def start(self, protocol):
        with self.lock:
            if self.state["running"]:
                raise ValueError("已有测评正在运行。")
            # Read current configuration for every batch, including after a FlClash switch.
            source, config = self.configuration()
            if protocol not in config["protocols"]:
                raise ValueError("所选协议已不在当前配置中，请刷新协议列表。")
            args = parse_args(["--config", str(source), "--protocol", protocol, "--output", str(OUTPUT)])
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
                with self.batch.lock:
                    data = self.batch.configuration()[1]
                return self.reply(200, data)
            except Exception:
                return self.reply(400, {"error": "尚未读取到有效配置。请选择配置文件；使用代理集合时请选择整个数据文件夹。"})
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
            maximum = MAX_IMPORT_BYTES * 2 if self.path == "/api/import" else 1024
            if not 0 < size <= maximum:
                raise ValueError("请求长度无效。")
            if size > 1024 and (not self.local_host() or self.headers.get("X-Checker-Token") != TOKEN
                                or self.headers.get("Content-Type") != "application/json"):
                return self.reply(403, {"error": "请求校验失败，请刷新页面。"})
            raw = self.rfile.read(size)
            # Drain the bounded body before rejecting, so closing does not reset the response.
            if (not self.local_host() or self.headers.get("X-Checker-Token") != TOKEN
                    or self.headers.get("Content-Type") != "application/json"):
                return self.reply(403, {"error": "请求校验失败，请刷新页面。"})
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("请求格式不正确。")
            if self.path == "/api/start":
                self.batch.start(data.get("protocol", "all"))
            elif self.path == "/api/stop":
                self.batch.cancel.set()
            elif self.path == "/api/import":
                return self.reply(200, self.batch.import_config(data.get("files")))
            else:
                return self.reply(404, {"error": "接口不存在"})
            self.reply(200, self.batch.snapshot())
        except yaml.YAMLError:
            self.reply(400, {"error": "配置或集合缓存不是有效 YAML，请重新导入。"})
        except (ValueError, TypeError, FileNotFoundError, RuntimeError) as error:
            self.reply(400, {"error": str(error)})
        except Exception as error:
            self.reply(400, {"error": f"读取配置失败（{type(error).__name__}），请检查挂载目录。"})


if __name__ == "__main__":
    OUTPUT.mkdir(parents=True, exist_ok=True)
    print("FlClash 节点评分网页服务已启动，端口 8080", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
