"""Local browser controls for the Docker edition; measurements reuse checker.run."""
# Docker 网页服务：提供配置导入、协议刷新、测评启动 / 停止和固定报告文件下载。
# 默认优先读取挂载配置，失败时可回退到已有快照；手动导入优先当前运行会话。
# 共享状态由 Batch 的锁保护；后台线程执行耗时测评，HTTP 请求负责控制和查询。
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
# 令牌只存在本次服务内存和本地主页中，重启后重新生成；不保存到配置或公开报告。
TOKEN = secrets.token_urlsafe(32)
IMPORTED = Path(os.environ.get("CHECKER_IMPORTED_CONFIG", "/data/imported-config.yaml"))


# 单个网页服务共享的批次状态。
# 锁保护状态与配置选择；耗时测评在后台线程执行，供 HTTP 轮询持续读取进度。
class Batch:
    # 初始优先使用自动挂载配置，尚未手动选用快照。
    # cancel 用于协作式停止；state 只存网页需要的进度和报告地址。
    def __init__(self):
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.use_imported = False
        self.state = {"running": False, "message": "就绪", "done": 0, "total": 0, "report": None}

    # 在锁内合并少量状态字段，供后台进度 / 提示回调调用。
    # 避免 HTTP 轮询读到正在更新的共享字典。
    def update(self, **values):
        with self.lock:
            self.state.update(values)

    # 在锁内返回状态浅副本；请求处理层不直接持有可修改的内部状态字典。
    def snapshot(self):
        with self.lock:
            return dict(self.state)

    # 解析当前配置并返回来源和协议元数据。
    # 自动配置失败且存在快照时回退，但不永久切换 use_imported，因此后续刷新可再次尝试自动读取。
    def configuration(self):
        # use_imported 表示本会话用户主动导入；临时自动回退只标记 automatic_failed，不改变该选择。
        automatic_failed = False
        try:
            source, _ = resolve_paths(IMPORTED if self.use_imported else None)
            _, nodes = load_nodes(source)
            options, _ = protocol_options(nodes)
        # 文件缺失、权限问题、YAML 错误以及节点字段不完整都可能让自动来源失效。
        # 只有自动模式且快照确实存在才回退；手动选择失败或快照也无效则向接口返回失败。
        except (OSError, ValueError, yaml.YAMLError, KeyError, TypeError, AttributeError):
            if self.use_imported or not IMPORTED.is_file():
                raise
            source, _ = resolve_paths(IMPORTED)
            _, nodes = load_nodes(source)
            options, _ = protocol_options(nodes)
            automatic_failed = True
        mode = "imported" if source == IMPORTED.resolve() else "mounted"
        return source, {"protocols": options, "count": len(nodes), "source_mode": mode,
                        "automatic_failed": automatic_failed}

    # 已有批次运行时拒绝导入。
    # 先完整验证上传包，再用权限受限的临时 YAML 原子替换快照；成功后把当前会话切为手动导入模式。
    def import_config(self, files):
        with self.lock:
            if self.state["running"]:
                raise ValueError("请先停止测评并等待结束，再导入配置。")
            content, count = bundle_config(files)
            IMPORTED.parent.mkdir(parents=True, exist_ok=True)
            temporary = IMPORTED.with_suffix(".yaml.tmp")
            temporary.write_text(content, encoding="utf-8")
            # Linux 快照仅允许运行服务的用户读写；验证完成后替换旧文件，不留下部分配置作为新快照。
            temporary.chmod(0o600)
            temporary.replace(IMPORTED)
            # 成功导入后本会话优先使用用户刚选择的快照；服务重启才重新优先尝试自动挂载来源。
            self.use_imported = True
            self.state["message"] = f"已导入 {count} 个节点，可开始测评。"
            return self.configuration()[1]

    # 在锁内检查批次互斥、重新读取配置、确认协议仍有效，并固定本批配置路径。
    # 释放锁后启动后台线程，防止耗时测速阻塞状态请求。
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

        # 执行测评并把回调转为网页状态。
        # 报告仅暴露固定下载路由；错误消息不直接泄露 YAML 解析行，最终无论成功与否都清除 running。
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

        # 这里已经释放 Batch 锁；后台 run 的进度回调也要获取同一把锁，不能持锁等待测评结束。
        threading.Thread(target=worker, daemon=True).start()


# HTTP 路由处理器。
# 所有请求共享一个 Batch；只接受回环 Host，写操作还必须携带每次服务启动随机生成的令牌。
class Handler(BaseHTTPRequestHandler):
    # 类属性有意共享：所有 HTTP 处理线程控制同一个批次，不为每次请求创建独立测评。
    batch = Batch()

    # 校验 Host 只能是 localhost / 127.0.0.1，可附端口。
    # 防止恶意域名解析到回环地址后读取令牌、报告或触发测评。
    def local_host(self):
        # Prevent a remote domain rebound to loopback from reading local reports/tokens.
        return re.fullmatch(r"(?:127\.0\.0\.1|localhost)(?::\d{1,5})?",
                            self.headers.get("Host", "").lower()) is not None

    # 统一发送 UTF-8 JSON 或原始字节，显式给出类型与长度。
    # 禁用缓存和 MIME 猜测，避免浏览器缓存旧令牌或误解释报告内容。
    def reply(self, status, payload, kind="application/json; charset=utf-8"):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    # 只读接口：健康检查不要求配置有效；主页注入随机令牌；协议接口读取当前来源；报告下载严格限制批次名和三个文件名。
    def do_GET(self):
        if not self.local_host():
            return self.reply(403, {"error": "请通过 127.0.0.1 或 localhost 访问本机服务。"})
        path = urlsplit(self.path).path
        # 健康检查只代表网页服务可用，配置读取失败不应导致容器被判定为无法启动。
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
                return self.reply(400, {"error": "自动读取未找到有效配置。请选择配置文件或数据文件夹，或在 .env 中设置 FLCLASH_DIR 后重新创建容器。"})
        # 批次目录必须符合生成时的时间格式，文件名必须命中白名单；不提供任意路径下载接口。
        match = re.fullmatch(r"/reports/(\d{8}-\d{6}-\d{6})/(report.html|results.json|results.csv)", path)
        if match:
            file = OUTPUT / match[1] / match[2]
            if file.is_file():
                kind = {"report.html": "text/html; charset=utf-8", "results.json": "application/json; charset=utf-8",
                        "results.csv": "text/csv; charset=utf-8"}[match[2]]
                return self.reply(200, file.read_bytes(), kind)
        self.reply(404, {"error": "页面不存在"})

    # 先约束请求体大小，再检查 Host、令牌和 JSON 类型。
    # 小请求先读完请求体再拒绝，大上传先鉴权，减少未授权的大量内存占用。
    def do_POST(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            # 上传 JSON 中的编码 / 转义会增加字节数，HTTP 层给 48 MiB 空间；实际文件文本仍限制 24 MiB。
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
        # 解析器原始错误可能含密码所在行，因此改为固定提示；不把原始 YAML 异常直接回显给网页。
        except yaml.YAMLError:
            self.reply(400, {"error": "配置或集合缓存不是有效 YAML，请重新导入。"})
        except (ValueError, TypeError, FileNotFoundError, RuntimeError) as error:
            self.reply(400, {"error": str(error)})
        except Exception as error:
            self.reply(400, {"error": f"读取配置失败（{type(error).__name__}），请检查挂载目录。"})


if __name__ == "__main__":
    OUTPUT.mkdir(parents=True, exist_ok=True)
    print("FlClash 节点评分网页服务已启动，端口 8080", flush=True)
    # 容器内监听所有接口以接受端口映射；宿主机暴露范围由 Compose 的 127.0.0.1 绑定限制。
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
