import csv
import json
import re
from pathlib import Path


def node_region(item: dict) -> tuple[str, str]:
    exits = {q.get("ip"): q.get("region") for q in item.get("ip_scores", [])}
    ips = item.get("ips", [])
    regions = sorted({exits[ip] for ip in ips if exits.get(ip)})
    if regions:
        partial = any(not exits.get(ip) for ip in ips)
        return (" / ".join(regions) + (" / 部分出口未知" if partial else ""),
                "出口 IP · IPLark" + ("（部分）" if partial else ""))
    codes = {"US": "美国", "TW": "台湾", "JP": "日本", "SG": "新加坡",
             "HK": "香港", "KR": "韩国", "GB": "英国", "UK": "英国",
             "DE": "德国", "CA": "加拿大", "AU": "澳大利亚"}
    match = re.search(r"(?<![A-Za-z0-9])(US|TW|JP|SG|HK|KR|GB|UK|DE|CA|AU)(?![A-Za-z])",
                      item["name"], re.IGNORECASE)
    if match:
        return codes[match.group().upper()], "名称推断"
    return "未知", "未取得地区信息"


def ranked(results: list[dict]) -> list[dict]:
    def key(item):
        if item.get("status") == "已完成":
            return (0, -(item["score"] or 0), item["name"])
        if item.get("available"):
            return (1, -item.get("performance_score", 0), item["name"])
        return (2 if item.get("status") == "连接失败" else 3, 0, item["name"])
    return sorted(results, key=key)


def safe_csv(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def write_report(report: dict, output: Path):
    data = dict(report)
    data["results"] = []
    for item in ranked(report["results"]):
        region, source = node_region(item)
        data["results"].append(dict(item, region=region, region_source=source))
    if not data.get("iplark_block"):
        data["iplark_block"] = next((q["query_issue"] for item in data["results"]
                                     for q in item.get("ip_scores", []) if q.get("query_issue")), None)
    content = json.dumps(data, ensure_ascii=False, indent=2)
    temporary = output / "results.json.tmp"
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(output / "results.json")
    columns = [("节点", "name"), ("节点地区", "region"), ("地区来源", "region_source"),
               ("协议", "protocol"), ("状态", "status"),
               ("综合分/100", "score"), ("性能部分分/60", "performance_score"),
               ("IPLark分/100", "iplark_score"), ("下载Mbps", "download_mbps"),
               ("下载成功次数/2", "download_success"), ("响应延迟ms", "latency_ms"),
               ("延迟波动ms", "jitter_ms"), ("请求成功率%", "success_pct"),
               ("可达站点/3", "reachable_sites")]
    with (output / "results.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([title for title, _ in columns] + ["IPv4出口", "失败原因", "IPLark来源", "IPLark查询提示"])
        for item in data["results"]:
            values = [safe_csv(item.get(key, "")) for _, key in columns]
            values += [", ".join(item.get("ips", [])), "; ".join(item.get("errors", [])),
                       "; ".join(f"{q['ip']} {q.get('source', 'missing')} {q.get('checked_at', '')}"
                                 for q in item.get("ip_scores", [])),
                       "; ".join(dict.fromkeys(
                           q.get("query_issue", {}).get("message") or q.get("live_error") or q.get("error", "")
                           for q in item.get("ip_scores", []) if
                           q.get("query_issue") or q.get("live_error") or q.get("error")))]
            writer.writerow([safe_csv(value) for value in values])
    # Embedded input is data, including when a subscription uses HTML/script-like names.
    script_data = content.replace("&", "\\u0026").replace("<", "\\u003c").replace(
        ">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    (output / "report.html").write_text(
        (Path(__file__).parent / "report.html").read_text(encoding="utf-8").replace("__REPORT_DATA__", script_data),
        encoding="utf-8")
