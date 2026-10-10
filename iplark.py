"""IPLark scores are read from its displayed page, never inferred or fabricated."""
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


class IPLark:
    def __init__(self, notify, cancel):
        self.notify = notify
        self.cancel = cancel
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
        self.query_state_path = self.cache_path.with_name("iplark-query-state.json")
        self.query_state = {}
        if self.query_state_path.is_file():
            try:
                state = json.loads(self.query_state_path.read_text(encoding="utf-8"))
                if isinstance(state, dict) and type(state.get("next_query_at")) in (int, float):
                    self.query_state = state
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

    def cached_score(self, ip: str):
        entry = self.saved.get(ip)
        if not isinstance(entry, dict):
            return None
        try:
            checked = datetime.fromisoformat(entry["checked_at"].replace("Z", "+00:00"))
            age = datetime.now(timezone.utc) - checked
            value = entry.get("score")
            if checked.tzinfo is None or not timedelta(0) <= age <= timedelta(hours=24):
                return None
            if type(value) not in (int, float) or not 0 <= value <= 100:
                return None
            return dict(entry, ip=ip, source="cached", url=f"https://iplark.com/{ip}")
        except (KeyError, ValueError, TypeError):
            return None

    def start(self):
        node = find_node()
        if not self.cdp_url:
            self.cdp_url = find_local_cdp()
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

    def start_bridge(self, node, socket_url, remote=False):
        command = [str(node), str(ROOT / "iplark_browser.mjs"), socket_url]
        if remote:
            command.append("--remote")
        self.bridge = subprocess.Popen(command,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                       creationflags=PROCESS_FLAGS)

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

    def save_query_state(self):
        try:
            temporary = self.query_state_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(self.query_state, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.query_state_path)
        except OSError:
            self.notify("IPLark 查询间隔或冷却时间无法持久保存。")

    def wait_between_queries(self):
        seconds = max(0, self.query_state.get("next_query_at", 0) - time.time())
        if seconds:
            self.notify(f"IPLark 降低查询频率，等待 {seconds:.0f} 秒。")
            if self.cancel.wait(seconds):
                raise InterruptedError("用户停止检测。")

    def record_query(self, issue=None, retry_after=None):
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
        self.query_state = {"next_query_at": time.time() + seconds, "issue": issue}
        self.save_query_state()

    def lookup(self, ip: str) -> dict:
        ip = str(ipaddress.ip_address(ip))
        if ip in self.cache:
            return self.cache[ip]
        cached = self.cached_score(ip)
        if cached:
            if self.block_event:
                cached["query_issue"] = dict(self.block_event)
            self.cache[ip] = cached
            return cached
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
            if result.get("ip") != ip:
                raise RuntimeError("IPLark 返回的 IP 与查询不一致。")
            self.record_query()
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
        if self.block_event:
            # Preserve the warning even when cached data supplies a valid score.
            result["query_issue"] = dict(self.block_event)
        self.cache[ip] = result
        return result

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

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
