"""Sequential measurements and transparent absolute scoring (download weight = 20%)."""
# 测评主流程：读取全部节点 → 独立核心逐个测性能 → 按出口 IP 查询 IPLark → 更新报告。
# 网络测评与浏览器查询分两个阶段，避免查询页面的流量干扰下载网速。
# 评分采用固定权重和绝对尺度；缺失 IP 质量分保留为未知，不能冒充完整综合分。
import bootstrap  # Load the dependencies for container command-line execution.
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

import requests

from core import RunLock, TestCore, load_nodes, resolve_paths
from iplark import IPLark
from report import write_report

# 各分项先计算 0–100 分，再乘以百分比权重；这里的数值不随当前批次最快节点变化。
WEIGHTS = {"speed": 20, "iplark": 40, "success": 20,
           "latency": 10, "jitter": 5, "reachability": 5}
SPEED_URL = "https://speed.cloudflare.com/__down"
# Google 使用 204 探测，Microsoft 还核对固定正文，避免重定向或拦截页造成假成功。
SITES = [("Google", "https://www.gstatic.com/generate_204", 204),
         ("Microsoft", "http://www.msftconnecttest.com/connecttest.txt", 200)]


# 创建只走独立核心回环代理端口的 HTTP 会话。
# 禁用环境代理，避免请求绕过当前被测节点；禁用压缩和缓存以便核算真实下载量。
def session_for(port: int) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.proxies = {scheme: f"http://127.0.0.1:{port}" for scheme in ("http", "https")}
    session.headers.update({"Cache-Control": "no-cache", "Accept-Encoding": "identity"})
    return session


# 统一检查共享停止事件；抛出 InterruptedError 让外层结束批次并保存已有结果，而不是把停止误记成节点故障。
def check_cancel(cancel):
    if cancel.is_set():
        raise InterruptedError("用户停止检测。")


# 执行一次响应 / 出口 IP 探测，返回 kind、ok、ms 等结构化样本。
# 先核对状态码，再校验 IP 或 Microsoft 固定正文；网络失败仅记录异常类别。
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


# 流式下载指定字节数，返回实际字节数、传输秒数和 Mbps。
# 单次尝试有采样截止时间；只有取得足够数据时才把主动截止视为有效慢速样本。
def download(port: int, size: int, cancel) -> dict:
    check_cancel(cancel)
    sample = {"ok": False, "bytes": 0, "mbps": None}
    # 整个下载尝试设置 20 秒采样截止；实际 Mbps 的计时从响应体读取前开始。
    deadline = time.perf_counter() + 20
    try:
        with session_for(port) as session:
            with session.get(SPEED_URL, params={"bytes": size, "nonce": time.time_ns()},
                             stream=True, timeout=(3, 4), allow_redirects=False) as response:
                response.raise_for_status()
                if response.headers.get("Content-Encoding", "identity") not in ("identity", ""):
                    raise ValueError("测速响应被压缩，无法可靠计算网速。")
                # Transfer time includes the first body byte, excludes CONNECT/TLS/headers.
                # 请求已建立连接并取得响应头；下面只累计正文传输时间，连接 / TLS 耗时由响应探测反映。
                start = time.perf_counter()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    check_cancel(cancel)
                    sample["bytes"] += len(chunk)
                    if sample["bytes"] > size:
                        raise ValueError("测速端点返回的字节数不符合预期。")
                    if time.perf_counter() > deadline:
                        # An intentional sampling cutoff is not a connection failure.
                        # 截止时至少取得 256 KiB 才认可短时采样；字节太少无法提供可靠速度估计。
                        if sample["bytes"] >= 256 * 1024:
                            sample["time_limited"] = True
                            break
                        raise TimeoutError("限时内未取得足够测速数据。")
                sample["seconds"] = round(time.perf_counter() - start, 4)
                if sample["bytes"] != size and not sample.get("time_limited"):
                    raise ValueError("测速下载不完整。")
                # 字节 × 8 转为 bit，再按十进制 1,000,000 换算 Mbps；MiB 只用于限制下载量。
                sample["mbps"] = round(sample["bytes"] * 8 / max(sample["seconds"], 0.001) / 1_000_000, 3)
                sample["ok"] = True
    except (requests.RequestException, ValueError, TimeoutError) as error:
        sample["error"] = type(error).__name__
    return sample


# 就地补充测量汇总、六项分数与状态，并返回同一结果字典。
# 网速固定占 20%；IP 分数不完整时，性能部分分仍保留，但不生成可参与完整排名的综合分。
def score_node(item: dict) -> dict:
    """Missing measurements earn no performance points; missing reputation stays unknown."""
    probes = item["probes"]
    successful = [p for p in probes if p["ok"]]
    cloud = [p["ms"] for p in probes if p["kind"] == "Cloudflare" and p["ok"]]
    # 相同端点的五次成功响应取中位数，减少偶发尖峰干扰；全失败时保留 None。
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
    # 计划始终是两次下载；只成功一次会把速度分项减半，不把失败样本从权重中删除。
    speed_success = len(speeds) / 2
    reachable = sum(any(p["kind"] == site and p["ok"] for p in probes)
                    for site in ("Cloudflare", "Google", "Microsoft"))
    quality = item.get("ip_scores", [])
    # 节点可能轮换出口；全部已查询出口都有分才取最差分，否则 IP 质量保持未知。
    reputation = min(q["score"] for q in quality) if quality and all(
        q.get("score") is not None for q in quality) else None
    # 网速按 log(1 + Mbps) 缩放，100 Mbps 达到速度分项满分；延迟 / 波动越大，分项越低。
    # 成功率包含原始探测的失败记录；可达性按三个站点计算，不等同于 ICMP 丢包率。
    components = {
        "speed": min(100, 100 * math.log1p(speed or 0) / math.log1p(100)) * speed_success,
        "iplark": reputation,
        "success": 100 * len(successful) / len(probes) if probes else 0,
        "latency": 100 / (1 + (latency / 400) ** 1.25) if latency is not None else 0,
        "jitter": 100 / (1 + (jitter / 100) ** 1.25) if jitter is not None else 0,
        "reachability": 100 * reachable / 3,
    }
    # 性能六十分单独累加；缺失的四十分 IP 质量不摊给其他分项，便于不同批次比较。
    subtotal = sum(components[key] * weight / 100 for key, weight in WEIGHTS.items()
                   if key != "iplark")
    available = bool(successful or speeds)
    # 必须至少有成功性能样本且 IP 分完整，才标记已完成；高 IP 分不能拯救完全不可达节点。
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


# 收集单节点原始样本：前后出口 IP、五次 Cloudflare 响应、两站连通性、两次下载。
# 额外出口重试单独保存，不改变原始探测失败率。
def measure_node(port: int, node: dict, size: int, cancel, notify) -> dict:
    item = {"name": node["name"], "protocol": node["type"], "status": "未检测",
            "score": None, "available": False, "probes": [], "downloads": [],
            "ips": [], "ip_scores": [], "errors": []}
    # 测评前后都取出口 IP，便于发现节点在同批测量中轮换出口。
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
    # 连通但原始出口探测失败时允许少量重试；重试放到 exit_retries，不抹去 probes 中的失败。
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


# 测评对外入口。
# 先获取同一运行环境中的批次锁，再执行完整流程；上下文退出时释放锁，包括异常或用户停止的情况。
def run(args, cancel: threading.Event, notify=print, progress=lambda *_: None) -> Path:
    with RunLock():
        return _run(args, cancel, notify, progress)


# 执行一次批次并返回 HTML 报告路径。
# 开始即为全部选中节点创建占位行，逐节点落盘，取消或异常时也留下已测结果及未检测节点。
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
    # 先建立完整结果范围，所有选中节点都有未检测占位；用户停止也不会让尚未执行的节点消失。
    report = {"created_at": datetime.now().astimezone().isoformat(),
              "measurement_environment": "Docker 容器网络",
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
                # 总进度分为性能和 IP 质量两个等量阶段；此处只更新第一阶段的节点计数。
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
    # 停止是用户意图，单独记为 cancelled；其他异常记为 interrupted 后继续向调用方抛出。
    except (InterruptedError, KeyboardInterrupt):
        report["cancelled"] = True
        notify("已停止；已完成的数据和未检测节点都保留在报告中。")
    except Exception:
        report["interrupted"] = True
        write_report(report, output)
        raise
    finally:
        # 无论成功、停止或异常都记录结束时间并再次落盘，供网页和下载看到最终批次状态。
        report["finished_at"] = datetime.now().astimezone().isoformat()
        write_report(report, output)
    path = output / "report.html"
    notify(f"报告已生成：{path}")
    return path


# 解析容器命令行参数。
# 默认覆盖全部协议、每次下载 8 MiB；limit 仅用于快速验证，正式全节点测评不设置它。
# 参数范围在执行前检查。
def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="FlClash 全节点综合评分，网速权重固定为 20%。")
    parser.add_argument("--config", default=None, help="生效的 config.yaml；省略时每次自动识别")
    parser.add_argument("--core", default=None, help="容器内 FlClashCore 路径")
    parser.add_argument("--protocol", default="all", help="all、ss、anytls 等配置协议名称")
    parser.add_argument("--output", default=os.environ.get("CHECKER_OUTPUT", "/data/results"))
    parser.add_argument("--download-mib", type=int, default=8)
    parser.add_argument("--limit", type=int, default=None, help="仅用于快速验证，正式使用请省略")
    args = parser.parse_args(argv)
    if not 1 <= args.download_mib <= 64 or (args.limit is not None and args.limit < 1):
        parser.error("--download-mib 必须为 1–64，--limit 必须为正整数。")
    return args


# 命令行入口：创建停止事件并同步运行批次，把进度输出到标准输出。
# Docker 网页服务则直接调用 run，以便共享同一套测评逻辑。
def main():
    args = parse_args()
    run(args, threading.Event(), notify=lambda message: print(message, flush=True))


if __name__ == "__main__":
    main()
