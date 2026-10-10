"""IPLark scores are read from its displayed page, never inferred or fabricated."""
# IP 质量查询：先用本批内存缓存，再检查带时区且不超过 24 小时的磁盘缓存。
# 确需实时数据时才启动浏览器桥接；公开页面结果由 iplark_browser.mjs 读取。
# 遇到验证、拒绝访问或限流会记录冷却并停止本批后续实时查询，不自动回答验证码。
import ipaddress
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time

import requests

ROOT = Path(__file__).parent
PROCESS_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# 找到 Node.js 并实际检查内置 WebSocket 是否可用。
# Docker 使用镜像中的 node；保留的 Windows 候选仅是兼容查找路径。
def find_node() -> Path:
    bundled = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
    candidate = shutil.which("node") or (str(bundled) if bundled.is_file() else None)
    if not candidate:
        raise FileNotFoundError("IPLark 浏览器读取需要 Node.js 22 或更新版本。")
    check = subprocess.run([candidate, "-p", "typeof WebSocket"], capture_output=True,
                           text=True, timeout=10, creationflags=PROCESS_FLAGS)
    if check.stdout.strip() != "function":
        raise RuntimeError("Node.js 版本过旧，需要 22 或更新版本。")
    return Path(candidate)


# 浏览器查找函数的历史名称。
# Docker 分支返回 Chromium，Windows 兼容分支查找 Edge；缺少浏览器时不伪造实时评分。
def find_edge() -> Path:
    if os.name != "nt":
        browser = shutil.which("chromium")
        if browser:
            return Path(browser)
        raise FileNotFoundError("容器中未找到 Chromium，无法读取 IPLark 实时评分。")
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            candidate = Path(base) / "Microsoft/Edge/Application/msedge.exe"
            if candidate.is_file():
                return candidate
    raise FileNotFoundError("未找到 Microsoft Edge，无法读取 IPLark 实时评分。")


# 仅在 Windows 兼容分支探测回环 CDP 服务，不启动 Docker、不修改浏览器。
# Docker 未显式设置 IPLARK_CDP_URL 时返回空串，使用自己的 Chromium。
def find_local_cdp() -> str:
    """Reuse the local browser service, without starting or modifying Docker."""
    if os.name != "nt":
        return ""
    endpoint = "http://127.0.0.1:9222"
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(endpoint + "/json/version", timeout=2)
            response.raise_for_status()
            socket_url = response.json().get("webSocketDebuggerUrl", "")
            if isinstance(socket_url, str) and socket_url.startswith(("ws://", "wss://")):
                return endpoint
    except (requests.RequestException, ValueError):
        pass
    return ""


# 单批 IPLark 查询上下文：管理内存去重、有效磁盘缓存、持久冷却和浏览器生命周期。
# 网站拦截会同时进入节点结果和整批报告。
class IPLark:
    # 初始化回调、停止事件与资源引用，并载入缓存 / 冷却记录。
    # 磁盘冷却尚未到期时设置本批查询错误，使无缓存 IP 保持待补全。
    def __init__(self, notify, cancel):
        self.notify = notify
        self.cancel = cancel
        # cache 是本批查询去重表（包含未知结果），saved 是可持久复用的历史评分；二者用途不同。
        self.cache = {}
        self.browser = None
        self.bridge = None
        self.temp = None
        self.output = queue.Queue()
        self.start_error = None
        self.block_event = None
        self.saved = {}
        self.cdp_url = os.environ.get("IPLARK_CDP_URL", "").strip()
        self.cache_path = Path(os.environ.get("IPLARK_CACHE", ROOT / "iplark-cache.json"))
        # 评分缓存和查询节流分开存储，重启不能靠重新创建客户端绕过网站冷却期。
        self.query_state_path = self.cache_path.with_name("iplark-query-state.json")
        self.query_state = {}
        if self.query_state_path.is_file():
            try:
                state = json.loads(self.query_state_path.read_text(encoding="utf-8"))
                if isinstance(state, dict) and type(state.get("next_query_at")) in (int, float):
                    self.query_state = state
                    # 未到恢复时间且有明确拦截原因时，本批仅允许有效缓存；不自动等待后继续挑战网站验证。
                    if state["next_query_at"] > time.time() and state.get("issue"):
                        self.block_event = state["issue"]
                        self.start_error = "IPLark 仍在冷却期；本次仅使用有效缓存，无缓存则待补全。"
            except (OSError, ValueError, TypeError):
                self.notify("IPLark 查询冷却记录无法读取。")
        if self.cache_path.is_file():
            try:
                self.saved = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if not isinstance(self.saved, dict):
                    self.saved = {}
            except (OSError, json.JSONDecodeError):
                self.notify("IPLark 本地缓存无法读取，将只查询实时页面。")

    # 只接受带时区、非未来且年龄不超过 24 小时的 0–100 数值分数。
    # 返回副本并标记 cached，保留原始查询时间；布尔值不能充当数值评分。
    def cached_score(self, ip: str):
        entry = self.saved.get(ip)
        if not isinstance(entry, dict):
            return None
        try:
            checked = datetime.fromisoformat(entry["checked_at"].replace("Z", "+00:00"))
            age = datetime.now(timezone.utc) - checked
            value = entry.get("score")
            # 要求带时区，并排除未来时间 / 超过 24 小时的条目，避免机器时钟差或旧数据伪装成新查询。
            if checked.tzinfo is None or not timedelta(0) <= age <= timedelta(hours=24):
                return None
            # bool 在 Python 中是 int 的子类，使用精确类型检查，不能把 True 误认作质量分 1。
            if type(value) not in (int, float) or not 0 <= value <= 100:
                return None
            return dict(entry, ip=ip, source="cached", url=f"https://iplark.com/{ip}")
        except (KeyError, ValueError, TypeError):
            return None

    # 按需建立浏览器连接。
    # 显式 CDP 使用外部服务；否则启动本实例的临时配置浏览器，再把对应 WebSocket 地址交给 Node 桥接程序。
    def start(self):
        node = find_node()
        if not self.cdp_url:
            self.cdp_url = find_local_cdp()
        # 连接 Compose 内部 CDP 元数据端点时不使用环境代理；已有浏览器由外部服务管理。
        if self.cdp_url:
            with requests.Session() as session:
                session.trust_env = False
                response = session.get(self.cdp_url.rstrip("/") + "/json/version", timeout=5)
                response.raise_for_status()
                socket_url = response.json()["webSocketDebuggerUrl"]
            self.notify("正在通过 CDP 浏览器查询 IPLark，等待页面自动加载；持续人工验证仍会报告。")
            self.start_bridge(node, socket_url, remote=True)
            return
        edge = find_edge()
        # 使用独立临时浏览器配置，不读取日常浏览器 Cookie 或登录会话；仅这个进程由本实例清理。
        self.temp = tempfile.TemporaryDirectory(prefix="flclash-iplark-browser-")
        self.notify("正在通过独立浏览器查询 IPLark；遇到安全验证则停止自动查询。")
        browser_flags = [] if os.name == "nt" else ["--headless=new", "--disable-dev-shm-usage", "--no-sandbox"]
        self.browser = subprocess.Popen([
            str(edge), "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={self.temp.name}", "--no-first-run", "--no-default-browser-check",
            *browser_flags, "--new-window", "about:blank"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=PROCESS_FLAGS)
        port_file = Path(self.temp.name) / "DevToolsActivePort"
        deadline = time.monotonic() + 15
        while not port_file.is_file():
            if self.cancel.is_set() or time.monotonic() > deadline:
                raise TimeoutError("独立浏览器未启动。")
            time.sleep(0.15)
        port = int(port_file.read_text().splitlines()[0])
        with requests.Session() as session:
            session.trust_env = False
            pages = session.get(f"http://127.0.0.1:{port}/json/list", timeout=5).json()
        page = next(page for page in pages if page.get("type") == "page")
        self.start_bridge(node, page["webSocketDebuggerUrl"])

    # 启动按行 JSON 通信的 Node 子进程。
    # 远程模式由桥接程序建立自己的浏览器上下文；读线程把响应放入队列，主线程等待 ready 后才查询。
    def start_bridge(self, node, socket_url, remote=False):
        command = [str(node), str(ROOT / "iplark_browser.mjs"), socket_url]
        if remote:
            command.append("--remote")
        self.bridge = subprocess.Popen(command,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                       creationflags=PROCESS_FLAGS)

        # 持续读取桥接标准输出，一行对应一条 JSON 响应。
        # 非法 JSON 转为明确错误消息，避免把日志文本当作页面结果。
        def reader():
            for line in self.bridge.stdout:
                try:
                    self.output.put(json.loads(line))
                except json.JSONDecodeError:
                    self.output.put({"error": "浏览器桥接返回了无效数据。"})

        threading.Thread(target=reader, daemon=True).start()
        ready = self.wait(20)
        if not ready.get("ready"):
            raise RuntimeError("IPLark 浏览器读取程序未初始化。")

    # 可取消地等待一条桥接响应，使用单调时钟限时。
    # 每 0.2 秒检查停止事件和子进程状态，避免桥接退出后继续空等。
    def wait(self, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.cancel.is_set():
                raise InterruptedError("用户停止检测。")
            try:
                return self.output.get(timeout=0.2)
            except queue.Empty:
                if self.bridge and self.bridge.poll() is not None:
                    raise RuntimeError("浏览器读取程序已退出。")
        raise TimeoutError("IPLark 页面读取超时。")

    # 通过临时文件替换保存下次查询时间和拦截原因。
    # 写入失败只提示持久化问题，不把已经读取的页面结果改成其他分数。
    def save_query_state(self):
        try:
            temporary = self.query_state_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(self.query_state, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.query_state_path)
        except OSError:
            self.notify("IPLark 查询间隔或冷却时间无法持久保存。")

    # 等待上次记录的查询间隔。
    # Event.wait 能在用户点击停止时立即结束等待，比不可取消的 sleep 更适合网页控制。
    def wait_between_queries(self):
        seconds = max(0, self.query_state.get("next_query_at", 0) - time.time())
        if seconds:
            self.notify(f"IPLark 降低查询频率，等待 {seconds:.0f} 秒。")
            if self.cancel.wait(seconds):
                raise InterruptedError("用户停止检测。")

    # 正常查询间隔至少 10 秒；被拦截时冷却至少 600 秒。
    # Retry-After 支持秒数或 HTTP 日期，取更长等待值，并记录 UTC 恢复时间。
    def record_query(self, issue=None, retry_after=None):
        # 正常节流与拦截冷却都是下次允许查询的最早时间；服务器要求更久时优先遵从 Retry-After。
        seconds = 10
        if issue:
            seconds = 600
            if retry_after:
                try:
                    seconds = max(seconds, int(retry_after))
                except ValueError:
                    try:
                        seconds = max(seconds, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
                    except (ValueError, TypeError, OverflowError):
                        pass
            issue["retry_after"] = retry_after
            issue["resume_after"] = datetime.fromtimestamp(time.time() + seconds, timezone.utc).isoformat()
        # 保存 UTC 时间戳便于跨重启复用；网页报告中的 resume_after 则是可读的带时区时间。
        self.query_state = {"next_query_at": time.time() + seconds, "issue": issue}
        self.save_query_state()

    # 查询一个规范化 IP：本批缓存 → 有效磁盘缓存 → 实时页面。
    # 相同 IP 不重复查询；无分数保持 None，拦截或超时后停止本批后续实时查询。
    def lookup(self, ip: str) -> dict:
        ip = str(ipaddress.ip_address(ip))
        # 同一批同一个出口只查询一次，多节点共享出口时复用相同结果，降低网站请求量。
        if ip in self.cache:
            return self.cache[ip]
        # 有效历史缓存优先于浏览器启动，且保留原查询时间，不能伪装成本次实时结果。
        cached = self.cached_score(ip)
        if cached:
            if self.block_event:
                cached["query_issue"] = dict(self.block_event)
            self.cache[ip] = cached
            return cached
        # 先建立明确的未知结果；查询失败仍返回这一 IP 的状态，不填充推断或随机质量分。
        result = {"ip": ip, "score": None, "source": "missing",
                  "url": f"https://iplark.com/{ip}"}
        try:
            if self.start_error:
                raise RuntimeError(self.start_error)
            self.wait_between_queries()
            if not self.bridge:
                try:
                    self.start()
                except (OSError, RuntimeError, TimeoutError, StopIteration, requests.RequestException) as error:
                    self.start_error = str(error)
                    self.close()
                    raise
            self.bridge.stdin.write(json.dumps({"ip": ip}) + "\n")
            self.bridge.stdin.flush()
            result = self.wait(60)
            # 桥接响应必须属于当前 IP；禁止把延迟返回的前一个查询结果配给后一个节点。
            if result.get("ip") != ip:
                raise RuntimeError("IPLark 返回的 IP 与查询不一致。")
            self.record_query()
            # 只有缺分且存在 403 / 429 / 人工验证信号时才归为拦截；普通解析失败不能武断称为脚本检测。
            if result.get("score") is None and (result.get("http_status") in (403, 429)
                                                or result.get("needs_verification")):
                if result.get("http_status") == 429:
                    kind, message = "rate_limited", "IPLark 返回 HTTP 429，请求被限流。"
                elif result.get("needs_verification"):
                    kind, message = "verification", "IPLark 要求安全验证，自动查询被拦截（可能识别了自动化脚本）。"
                else:
                    kind, message = "access_denied", "IPLark 返回 HTTP 403，自动查询被拒绝。"
                self.block_event = {"kind": kind, "message": message, "ip": ip,
                                    "checked_at": result.get("checked_at") or datetime.now(timezone.utc).isoformat(),
                                    "http_status": result.get("http_status")}
                self.record_query(self.block_event, result.get("retry_after"))
                # 保留拦截信息并关闭本批实时通道，后续 lookup 仍可命中有效缓存；无缓存的 IP 待补全。
                self.start_error = message + " 本次后续自动查询已停止；仅使用有效缓存，无缓存则待补全。"
                self.notify(self.start_error)
                self.close()
        except InterruptedError:
            raise
        except (OSError, RuntimeError, TimeoutError, requests.RequestException) as error:
            result["error"] = str(error)
            # A timed-out response must not be associated with the next IP.
            self.start_error = str(error)
            self.close()
        if result.get("score") is not None:
            # 仅成功读到分数的结果写入历史缓存；临时文件替换减少写到一半损坏缓存的风险。
            self.saved[ip] = result
            try:
                temporary = self.cache_path.with_suffix(".json.tmp")
                temporary.write_text(json.dumps(self.saved, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(self.cache_path)
            except OSError:
                self.notify("实时 IPLark 分数已读取，但本地缓存未能保存。")
        else:
            cached = self.cached_score(ip)
            if cached:
                cached["live_error"] = result.get("error", "实时评分读取失败")
                result = cached
        # 缓存补全了分数也不能隐藏当前批次的网站验证 / 限流事件，报告必须同时呈现。
        if self.block_event:
            # Preserve the warning even when cached data supplies a valid score.
            result["query_issue"] = dict(self.block_event)
        self.cache[ip] = result
        return result

    # 先通知桥接清理其拥有的上下文，再结束本实例启动的临时浏览器。
    # 外部 CDP 浏览器本身不由此实例持有，不能被一起关闭。
    def close(self):
        if self.bridge and self.bridge.poll() is None:
            try:
                self.bridge.stdin.write('{"close":true}\n')
                self.bridge.stdin.flush()
                self.bridge.wait(timeout=4)
            except (OSError, subprocess.TimeoutExpired):
                self.bridge.terminate()
                self.bridge.wait(timeout=5)
        if self.browser and self.browser.poll() is None:
            # Only the dedicated temporary-profile browser is owned by this instance.
            self.browser.terminate()
            self.browser.wait(timeout=5)
        if self.temp:
            try:
                self.temp.cleanup()
            except OSError:
                self.notify("独立浏览器尚未释放缓存，临时目录由系统后续清理。")
            self.temp = None
        self.bridge = None
        self.browser = None

    # 返回查询上下文，但不立即启动浏览器；整批均有有效缓存时不会创建浏览器进程。
    def __enter__(self):
        return self

    # 统一关闭本实例的桥接和临时浏览器，即使查询中途停止或抛出异常。
    def __exit__(self, *_):
        self.close()
