"""Sequential measurements and transparent absolute scoring (download weight = 20%)."""
import argparse
from datetime import datetime
import ipaddress
import json
import math
import os
from pathlib import Path
import statistics
import threading
import time
import webbrowser

import requests

from core import RunLock, TestCore, load_nodes, resolve_paths
from iplark import IPLark
from report import write_report

ROOT = Path(__file__).parent
WEIGHTS = {"speed": 20, "iplark": 40, "success": 20,
           "latency": 10, "jitter": 5, "reachability": 5}
SPEED_URL = "https://speed.cloudflare.com/__down"
SITES = [("Google", "https://www.gstatic.com/generate_204", 204),
         ("Microsoft", "http://www.msftconnecttest.com/connecttest.txt", 200)]


def session_for(port: int) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.proxies = {scheme: f"http://127.0.0.1:{port}" for scheme in ("http", "https")}
    session.headers.update({"Cache-Control": "no-cache", "Accept-Encoding": "identity"})
    return session


def check_cancel(cancel):
    if cancel.is_set():
        raise InterruptedError("用户停止检测。")


def probe(port: int, url: str, expected: int, cancel, kind: str) -> dict:
    check_cancel(cancel)
    sample = {"kind": kind, "ok": False, "ms": None}
    start = time.perf_counter()
    try:
        with session_for(port) as session:
            response = session.get(url, timeout=(3, 4), allow_redirects=False)
            sample["http_status"] = response.status_code
            if response.status_code != expected:
                sample["error"] = f"HTTP {response.status_code}"
                return sample
            if kind == "ip":
                sample["ip"] = str(ipaddress.ip_address(response.json()["ip"]))
            if kind == "Microsoft" and response.text.strip() != "Microsoft Connect Test":
                sample["error"] = "响应内容不符合预期"
                return sample
            sample["ok"] = True
            sample["ms"] = round((time.perf_counter() - start) * 1000, 2)
    except (requests.RequestException, ValueError, KeyError) as error:
        sample["error"] = type(error).__name__
    return sample


def download(port: int, size: int, cancel) -> dict:
    check_cancel(cancel)
    sample = {"ok": False, "bytes": 0, "mbps": None}
    deadline = time.perf_counter() + 20
    try:
        with session_for(port) as session:
            with session.get(SPEED_URL, params={"bytes": size, "nonce": time.time_ns()},
                             stream=True, timeout=(3, 4), allow_redirects=False) as response:
                response.raise_for_status()
                if response.headers.get("Content-Encoding", "identity") not in ("identity", ""):
                    raise ValueError("测速响应被压缩，无法可靠计算网速。")
                # Transfer time includes the first body byte, excludes CONNECT/TLS/headers.
                start = time.perf_counter()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    check_cancel(cancel)
                    sample["bytes"] += len(chunk)
                    if sample["bytes"] > size:
                        raise ValueError("测速端点返回的字节数不符合预期。")
                    if time.perf_counter() > deadline:
                        # An intentional sampling cutoff is not a connection failure.
                        if sample["bytes"] >= 256 * 1024:
                            sample["time_limited"] = True
                            break
                        raise TimeoutError("限时内未取得足够测速数据。")
                sample["seconds"] = round(time.perf_counter() - start, 4)
                if sample["bytes"] != size and not sample.get("time_limited"):
                    raise ValueError("测速下载不完整。")
                sample["mbps"] = round(sample["bytes"] * 8 / max(sample["seconds"], 0.001) / 1_000_000, 3)
                sample["ok"] = True
    except (requests.RequestException, ValueError, TimeoutError) as error:
        sample["error"] = type(error).__name__
    return sample


def score_node(item: dict) -> dict:
    """Missing measurements earn no performance points; missing reputation stays unknown."""
    probes = item["probes"]
    successful = [p for p in probes if p["ok"]]
    cloud = [p["ms"] for p in probes if p["kind"] == "Cloudflare" and p["ok"]]
    latency = statistics.median(cloud) if cloud else None
    # Only adjacent successful probes to the same endpoint contribute to variation.
    changes = []
    previous = None
    for sample in (p for p in probes if p["kind"] == "Cloudflare"):
        if sample["ok"] and previous is not None:
            changes.append(abs(sample["ms"] - previous))
        previous = sample["ms"] if sample["ok"] else None
    jitter = statistics.median(changes) if len(changes) >= 2 else None
    speeds = [sample["mbps"] for sample in item["downloads"] if sample["ok"]]
    speed = statistics.median(speeds) if speeds else None
    # An incomplete second download reduces its 20% component rather than being discarded.
    speed_success = len(speeds) / 2
    reachable = sum(any(p["kind"] == site and p["ok"] for p in probes)
                    for site in ("Cloudflare", "Google", "Microsoft"))
    quality = item.get("ip_scores", [])
    reputation = min(q["score"] for q in quality) if quality and all(
        q.get("score") is not None for q in quality) else None
    components = {
        "speed": min(100, 100 * math.log1p(speed or 0) / math.log1p(100)) * speed_success,
        "iplark": reputation,
        "success": 100 * len(successful) / len(probes) if probes else 0,
        "latency": 100 / (1 + (latency / 400) ** 1.25) if latency is not None else 0,
        "jitter": 100 / (1 + (jitter / 100) ** 1.25) if jitter is not None else 0,
        "reachability": 100 * reachable / 3,
    }
    subtotal = sum(components[key] * weight / 100 for key, weight in WEIGHTS.items()
                   if key != "iplark")
    available = bool(successful or speeds)
    complete = available and reputation is not None
    item.update({"latency_ms": latency, "jitter_ms": jitter, "download_mbps": speed,
                 "download_success": len(speeds), "success_pct": components["success"],
                 "reachable_sites": reachable, "iplark_score": reputation,
                 "components": {key: round(value, 3) if value is not None else None
                                for key, value in components.items()},
                 "performance_score": round(subtotal, 2),
                 "score": round(subtotal + reputation * 0.4, 2) if complete else
                          (0.0 if not available else None),
                 "available": available,
                 "status": "已完成" if complete else ("IP评分待补全" if available else "连接失败")})
    return item


def measure_node(port: int, node: dict, size: int, cancel, notify) -> dict:
    item = {"name": node["name"], "protocol": node["type"], "status": "未检测",
            "score": None, "available": False, "probes": [], "downloads": [],
            "ips": [], "ip_scores": [], "errors": []}
    item["probes"].append(probe(port, "https://api.ipify.org?format=json", 200, cancel, "ip"))
    for index in range(5):
        notify(f"{node['name']} · 响应测试 {index + 1}/5")
        item["probes"].append(probe(port, f"{SPEED_URL}?bytes=0&nonce={time.time_ns()}",
                                    200, cancel, "Cloudflare"))
    for site, url, expected in SITES:
        item["probes"].append(probe(port, url, expected, cancel, site))
    if any(p["ok"] for p in item["probes"]):
        for index in range(2):
            notify(f"{node['name']} · 下载测试 {index + 1}/2")
            item["downloads"].append(download(port, size, cancel))
    item["probes"].append(probe(port, "https://api.ipify.org?format=json", 200, cancel, "ip"))
    item["ips"] = sorted({p["ip"] for p in item["probes"] if p.get("ip")})
    if not item["ips"] and any(p["ok"] for p in item["probes"]):
        item["exit_retries"] = []
        for _ in range(2):
            notify(f"{node['name']} · 重试获取 IPv4 出口")
            extra = probe(port, "https://api.ipify.org?format=json", 200, cancel, "ip")
            extra["checked_at"] = datetime.now().astimezone().isoformat()
            item["exit_retries"].append(extra)
            if extra.get("ip"):
                item["ips"] = [extra["ip"]]
                break
    item["errors"] = sorted({sample["error"] for sample in item["probes"] + item["downloads"]
                             if sample.get("error")})
    item["download_bytes"] = sum(sample["bytes"] for sample in item["downloads"])
    return item


def run(args, cancel: threading.Event, notify=print, progress=lambda *_: None) -> Path:
    with RunLock():
        return _run(args, cancel, notify, progress)


def _run(args, cancel: threading.Event, notify, progress) -> Path:
    source, core_path = resolve_paths(args.config, args.core)
    original, nodes = load_nodes(source)
    notify(f"当前配置：{source}；检测核心：{core_path}")
    selected = [node for node in nodes if args.protocol == "all" or node["type"].lower() == args.protocol.lower()]
    if args.limit:
        selected = selected[:args.limit]
    if not selected:
        raise ValueError("当前配置中没有所选协议的节点。")
    size = args.download_mib * 1024 * 1024
    output = Path(args.output).resolve() / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    output.mkdir(parents=True)
    report = {"created_at": datetime.now().astimezone().isoformat(),
              "measurement_environment": "本机 Docker 容器网络" if os.name != "nt" else "Windows 本机网络",
              "source": str(source), "core": str(core_path), "protocol_filter": args.protocol, "weights": WEIGHTS,
              "scope_count": len(nodes), "selected_count": len(selected), "cancelled": False,
              "iplark_block": None,
              "download_bytes_per_attempt": size, "results": [
                  {"name": n["name"], "protocol": n["type"], "score": None,
                   "status": "未检测", "available": False} for n in selected]}
    write_report(report, output)
    notify(f"发现 {len(nodes)} 个节点，本次检测 {len(selected)} 个；最大下载量约 {len(selected)*2*args.download_mib} MiB。")
    try:
        with TestCore(core_path, source, original, nodes) as core:
            for index, node in enumerate(selected):
                check_cancel(cancel)
                core.select(node["name"])
                notify(f"[{index + 1}/{len(selected)}] {node['name']} ({node['type']})")
                item = measure_node(core.port, node, size, cancel, notify)
                report["results"][index] = score_node(item)
                write_report(report, output)
                progress(index + 1, len(selected) * 2)
        # Network tests finish before browser lookups so browser traffic cannot affect speed.
        notify("网速与连通性检测完成，开始按出口 IP 合并查询 IPLark。")
        with IPLark(notify, cancel) as iplark:
            for index, item in enumerate(report["results"]):
                check_cancel(cancel)
                for ip in item.get("ips", []):
                    notify(f"IPLark · {ip}")
                    item["ip_scores"].append(iplark.lookup(ip))
                    report["iplark_block"] = iplark.block_event
                score_node(item)
                write_report(report, output)
                progress(len(selected) + index + 1, len(selected) * 2)
    except (InterruptedError, KeyboardInterrupt):
        report["cancelled"] = True
        notify("已停止；已完成的数据和未检测节点都保留在报告中。")
    except Exception:
        report["interrupted"] = True
        write_report(report, output)
        raise
    finally:
        report["finished_at"] = datetime.now().astimezone().isoformat()
        write_report(report, output)
    path = output / "report.html"
    notify(f"报告已生成：{path}")
    if not args.no_open:
        webbrowser.open(path.as_uri())
    return path


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="FlClash 全节点综合评分，网速权重固定为 20%。")
    parser.add_argument("--config", default=None, help="生效的 config.yaml；省略时每次自动识别")
    parser.add_argument("--core", default=None, help="FlClash.exe 或 FlClashCore.exe；省略时每次自动识别")
    parser.add_argument("--protocol", default="all", help="all、ss、anytls 等配置协议名称")
    parser.add_argument("--output", default=str(ROOT / "results"))
    parser.add_argument("--download-mib", type=int, default=8)
    parser.add_argument("--limit", type=int, default=None, help="仅用于快速验证，正式使用请省略")
    parser.add_argument("--headless", action="store_true", help="命令行模式，IPLark 浏览器按环境选择")
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--autorun", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.download_mib <= 64 or (args.limit is not None and args.limit < 1):
        parser.error("--download-mib 必须为 1–64，--limit 必须为正整数。")
    return args


def main():
    args = parse_args()
    if args.headless:
        run(args, threading.Event(), notify=lambda message: print(message, flush=True))
    else:
        from gui import show
        show(args)


if __name__ == "__main__":
    main()
