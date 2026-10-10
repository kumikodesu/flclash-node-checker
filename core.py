"""Read-only FlClash configuration snapshot and isolated core IPC."""
import copy
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path, PureWindowsPath
import socket
import struct
import subprocess
import tempfile
import threading
import time
import uuid
if os.name == "nt":
    import winreg

import yaml

RUN_MUTEX_NAME = "Local\\FlClashNodeBenchmark"

def default_config() -> Path:
    return Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "com.follow/clash/config.yaml"


def running_core_paths() -> list[Path]:
    """Prefer the UI's executable directory; an elevated core may hide its path."""
    script = """[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new();
    @(Get-Process -Name FlClash,FlClashCore -ErrorAction SilentlyContinue |
      Sort-Object ProcessName | Where-Object { $_.Path } |
      Select-Object ProcessName,Path) | ConvertTo-Json -Compress"""
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, text=True, encoding="utf-8", timeout=10,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        entries = json.loads(result.stdout.strip() or "[]")
        if isinstance(entries, dict):
            entries = [entries]
        apps = [entry for entry in entries if entry["ProcessName"].lower() == "flclash"]
        return list(dict.fromkeys(Path(entry["Path"]).parent / "FlClashCore.exe"
                                  for entry in (apps or entries)))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []


def registered_core_paths() -> list[Path]:
    paths = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
                                    0, winreg.KEY_READ | view) as root:
                    for index in range(winreg.QueryInfoKey(root)[0]):
                        try:
                            with winreg.OpenKey(root, winreg.EnumKey(root, index)) as key:
                                name = winreg.QueryValueEx(key, "DisplayName")[0]
                                if str(name).lower().startswith("flclash"):
                                    location = winreg.QueryValueEx(key, "InstallLocation")[0]
                                    if location:
                                        paths.append(Path(os.path.expandvars(location)) / "FlClashCore.exe")
                        except OSError:
                            continue
            except OSError:
                continue
    return paths


def unique_core(candidates: list[Path]) -> Path | None:
    found = list(dict.fromkeys(path.resolve() for path in candidates if path.is_file()))
    if len(found) > 1:
        raise ValueError("发现多份 FlClash 安装，请在窗口选择目标 FlClash.exe / FlClashCore.exe，或用 --core 指定。")
    return found[0] if found else None


def find_core() -> Path:
    active_paths = running_core_paths()
    if active_paths:
        active = unique_core(active_paths)
        if active:
            return active
        raise FileNotFoundError("正在运行的 FlClash 目录缺少 FlClashCore.exe，请重新选择完整安装目录中的程序文件。")
    installed = unique_core(registered_core_paths())
    if installed:
        return installed
    candidates = [Path(value) / "FlClash/FlClashCore.exe" for key in
                  ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")
                  if (value := os.environ.get(key))]
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    candidates += [local / "Programs/FlClash/FlClashCore.exe",
                   Path(__file__).parent / "FlClashCore.exe"]
    found = unique_core(candidates)
    if found:
        return found
    raise FileNotFoundError("未找到 FlClash 核心。请先打开移动后的 FlClash，或在窗口选择其程序文件（命令行用 --core）。")


def resolve_paths(config=None, core=None) -> tuple[Path, Path]:
    if os.name != "nt":
        source = Path(config or os.environ.get("FLCLASH_CONFIG", "/flclash/config.yaml")).resolve()
        executable = Path(core or "/opt/flclash/FlClashCore").resolve()
        for path in (source, executable):
            if not path.is_file():
                raise FileNotFoundError(f"文件不存在，请检查配置挂载或核心安装：{path}")
        return source, executable
    executable = Path(core).expanduser().resolve() if core else find_core()
    if executable.name.lower() == "flclash.exe":
        executable = executable.with_name("FlClashCore.exe")
    if executable.name.lower() != "flclashcore.exe":
        raise ValueError("请选择 FlClash.exe 或 FlClashCore.exe，不能使用其他 Clash 核心或程序。")
    if not executable.is_file():
        raise FileNotFoundError(f"核心文件不存在，请重新选择 FlClash：{executable}")
    if config:
        source = Path(config).expanduser().resolve()
    else:
        # Official releases use AppData; portable forks may use config/ beside the app.
        candidates = [default_config(), executable.parent / "config/config.yaml"]
        found = list(dict.fromkeys(path.resolve() for path in candidates if path.is_file()))
        if len(found) > 1:
            raise ValueError("同时发现用户配置和便携配置，请在窗口选择当前生效的 config.yaml（命令行用 --config）。")
        if not found:
            raise FileNotFoundError("未找到当前配置。请先在 FlClash 中启用订阅，或在窗口选择生效的 config.yaml。")
        source = found[0]
    if not source.is_file():
        raise FileNotFoundError(f"配置文件不存在，请重新选择当前配置：{source}")
    return source, executable


def protocol_options(nodes: list[dict], selected="all") -> tuple[list[str], str]:
    options = ["all"] + sorted({node["type"].lower() for node in nodes})
    selected = selected.lower()
    return options, selected if selected in options else "all"


def config_file(source: Path, value: str) -> Path:
    """Translate only Windows paths inside the read-only mounted config directory."""
    if os.name != "nt":
        windows = PureWindowsPath(value)
        if windows.is_absolute():
            root = os.environ.get("FLCLASH_HOST_DIR")
            if not root:
                raise ValueError("配置引用了 Windows 文件路径，请设置 FLCLASH_HOST_DIR 为挂载目录的原路径。")
            try:
                relative = windows.relative_to(PureWindowsPath(root))
            except ValueError:
                raise ValueError("配置引用的文件位于挂载目录之外，请将依赖文件放入配置目录。") from None
            return source.parent.joinpath(*relative.parts)
        value = value.replace("\\", "/")
    path = Path(value)
    return path if path.is_absolute() else source.parent / path


def load_nodes(source: Path) -> tuple[dict, list[dict]]:
    config = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("FlClash 配置为空，请先在 FlClash 中启用一个配置。")
    nodes = copy.deepcopy(config.get("proxies") or [])
    # Use cached provider definitions, never refresh a subscription or write its cache.
    for name, provider in (config.get("proxy-providers") or {}).items():
        if provider.get("type") == "inline":
            nodes.extend(copy.deepcopy(provider.get("payload") or []))
            continue
        if not provider.get("path"):
            raise ValueError(f"代理集合 {name} 没有本地缓存路径，无法保证覆盖全部节点。")
        path = config_file(source, provider["path"])
        if not path.is_file():
            raise FileNotFoundError(f"代理集合 {name} 尚未缓存，请先在 FlClash 中更新该集合。")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("proxies"), list):
            raise ValueError("代理集合缓存缺少 proxies 列表，请先在 FlClash 更新集合。")
        nodes.extend(copy.deepcopy(data["proxies"]))
    names = [node["name"] for node in nodes]
    if not names:
        raise ValueError("当前配置中没有代理节点。")
    if len(names) != len(set(names)):
        raise ValueError("存在同名节点，无法可靠地逐个选择；请先为重复节点设置不同名称。")
    # Relative certificate paths remain relative to the source configuration.
    for node in nodes:
        for key in ("ca", "certificate", "private-key"):
            value = node.get(key)
            if isinstance(value, str) and "\n" not in value:
                path = config_file(source, value)
                if path.is_file():
                    node[key] = str(path.resolve())
    return config, nodes


class RunLock:
    """Concurrent batches would compete for bandwidth and invalidate comparison."""
    def __enter__(self):
        if os.name != "nt":
            import fcntl
            self.handle = open("/tmp/flclash-node-checker.lock", "a")
            try:
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.handle.close()
                raise RuntimeError("已有一批节点测评正在运行，请等待或先停止该测评。") from None
            return self
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.k.CreateMutexW.restype = wintypes.HANDLE
        self.k.CloseHandle.argtypes = [wintypes.HANDLE]
        self.k.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self.handle = self.k.CreateMutexW(None, True, RUN_MUTEX_NAME)
        error = ctypes.get_last_error()
        if not self.handle:
            raise ctypes.WinError(error)
        if error == 183:
            self.k.CloseHandle(self.handle)
            raise RuntimeError("已有一批节点测评正在运行，请等待或先停止该测评。")
        return self

    def __exit__(self, *_):
        if os.name != "nt":
            self.handle.close()
            return
        self.k.ReleaseMutex(self.handle)
        self.k.CloseHandle(self.handle)


class TestCore:
    """Only owns its own subprocess, loopback listener and temporary configuration."""

    def __init__(self, executable: Path, source: Path, config: dict, nodes: list[dict]):
        self.executable = executable
        self.source = source
        self.original = config
        self.nodes = nodes
        self.process = None
        self.handle = None
        self.temp = None
        self.group = "NODE-CHECK-" + uuid.uuid4().hex[:10]
        self.connection = None
        if os.name != "nt":
            return
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR] + [wintypes.DWORD] * 6 + [ctypes.c_void_p]
        self.k.CreateNamedPipeW.restype = wintypes.HANDLE
        self.k.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        self.k.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        self.k.WriteFile.argtypes = self.k.ReadFile.argtypes
        self.k.PeekNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        self.k.CloseHandle.argtypes = [wintypes.HANDLE]

    def read_exact(self, size: int, deadline: float) -> bytes:
        data = bytearray()
        while len(data) < size:
            if time.monotonic() >= deadline:
                raise TimeoutError("检测核心 IPC 响应超时。")
            if self.connection:
                self.connection.settimeout(max(0.001, deadline - time.monotonic()))
                chunk = self.connection.recv(size - len(data))
                if not chunk:
                    raise EOFError("检测核心断开连接。")
                data.extend(chunk)
                continue
            available = wintypes.DWORD()
            if not self.k.PeekNamedPipe(self.handle, None, 0, None, ctypes.byref(available), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if not available.value:
                if self.process.poll() is not None:
                    raise RuntimeError("独立检测核心已退出。")
                time.sleep(0.015)
                continue
            buffer = ctypes.create_string_buffer(min(size - len(data), available.value))
            count = wintypes.DWORD()
            if not self.k.ReadFile(self.handle, buffer, len(buffer), ctypes.byref(count), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if not count.value:
                raise EOFError("检测核心断开连接。")
            data.extend(buffer.raw[:count.value])
        return bytes(data)

    def rpc(self, method: str, data=None, timeout: float = 20):
        request_id = uuid.uuid4().hex
        raw = json.dumps({"id": request_id, "method": method, "data": data}).encode()
        frame = struct.pack("<I", len(raw)) + raw
        if self.connection:
            self.connection.settimeout(timeout)
            self.connection.sendall(frame)
        else:
            count = wintypes.DWORD()
            if not self.k.WriteFile(self.handle, frame, len(frame), ctypes.byref(count), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if count.value != len(frame):
                raise RuntimeError("检测核心 IPC 写入不完整。")
        deadline = time.monotonic() + timeout
        while True:
            length = struct.unpack("<I", self.read_exact(4, deadline))[0]
            if length > 16 * 1024 * 1024:
                raise ValueError("检测核心返回了异常大小的消息。")
            response = json.loads(self.read_exact(length, deadline))
            if response.get("id") == request_id:
                if response.get("code"):
                    raise RuntimeError(f"检测核心 {method} 调用失败。")
                return response.get("data")

    def __enter__(self):
        try:
            self.temp = tempfile.TemporaryDirectory(prefix="flclash-node-check-")
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                self.port = listener.getsockname()[1]
            if os.name == "nt":
                pipe = rf"\\.\pipe\FlClashNodeCheck-{uuid.uuid4().hex}"
                self.handle = self.k.CreateNamedPipeW(pipe, 3, 0, 1, 65536, 65536, 0, None)
                if self.handle == ctypes.c_void_p(-1).value:
                    self.handle = None
                    raise ctypes.WinError(ctypes.get_last_error())
            groups = copy.deepcopy(self.original.get("proxy-groups") or [])
            for group in groups:
                group.pop("use", None)
                group.pop("include-all", None)
                group.pop("include-all-providers", None)
                group.pop("include-all-proxies", None)
                if not group.get("proxies"):
                    group["proxies"] = [n["name"] for n in self.nodes]
                # Avoid background health checks competing with a speed test.
                if group.get("type") in ("url-test", "fallback", "load-balance"):
                    group["type"] = "select"
                    for key in ("url", "interval", "tolerance", "strategy", "lazy"):
                        group.pop(key, None)
            groups.append({"name": self.group, "type": "select",
                           "proxies": [node["name"] for node in self.nodes]})
            config = {"mixed-port": self.port, "allow-lan": False,
                      "bind-address": "127.0.0.1", "mode": "rule", "log-level": "silent",
                      "ipv6": self.original.get("ipv6", True), "tun": {"enable": False},
                      "dns": {"enable": False}, "hosts": self.original.get("hosts") or {},
                      "profile": {"store-selected": False, "store-fake-ip": False},
                      "proxies": self.nodes, "proxy-groups": groups,
                      "rules": [f"MATCH,{self.group}"]}
            for key in ("global-client-fingerprint", "tcp-concurrent"):
                if self.original.get(key) is not None:
                    config[key] = self.original[key]
            Path(self.temp.name, "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
            if os.name == "nt":
                connected = threading.Event()

                def connect():
                    ok = self.k.ConnectNamedPipe(self.handle, None)
                    if ok or ctypes.get_last_error() == 535:
                        connected.set()

                threading.Thread(target=connect, daemon=True).start()
                self.process = subprocess.Popen([str(self.executable), pipe],
                                                 creationflags=subprocess.CREATE_NO_WINDOW,
                                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if not connected.wait(10):
                    raise RuntimeError("FlClashCore 未连接，请检查版本或 --core 路径。")
            else:
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    listener.listen(1)
                    listener.settimeout(10)
                    self.process = subprocess.Popen([str(self.executable), str(listener.getsockname()[1])],
                                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self.connection, _ = listener.accept()
            if not self.rpc("initClash", json.dumps({"home-dir": self.temp.name, "version": 1})):
                raise RuntimeError("独立核心初始化失败。")
            if self.rpc("setupConfig", json.dumps({"selected-map": {},
                                                   "test-url": "https://speed.cloudflare.com/__down?bytes=0"})):
                raise RuntimeError("独立检测配置被核心拒绝；原 FlClash 配置未更改。")
            available = self.rpc("getProxies")["proxies"]
            missing = [node["name"] for node in self.nodes if node["name"] not in available]
            if missing:
                raise RuntimeError(f"核心没有加载 {len(missing)} 个节点，已停止以避免遗漏。")
            if not self.rpc("startListener"):
                raise RuntimeError("独立检测端口启动失败。")
            return self
        except BaseException:
            self.close()
            raise

    def select(self, name: str):
        error = self.rpc("changeProxy", json.dumps({"group-name": self.group, "proxy-name": name}))
        if error:
            raise RuntimeError("独立检测核心切换节点失败。")

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.connection:
            self.connection.close()
            self.connection = None
        if self.handle:
            self.k.CloseHandle(self.handle)
            self.handle = None
        if self.temp:
            self.temp.cleanup()
            self.temp = None

    def __exit__(self, *_):
        self.close()
