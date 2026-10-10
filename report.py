# 报告输出：由同一份结果生成 JSON、Excel 可读 CSV 和自包含 HTML。
# 完整评分、待补全、连接失败、未检测节点均保留；排序时明确区分这些状态。
# 节点名称属于外部输入，必须防止 CSV 公式注入及 HTML 中的脚本标签逃逸。
import csv
import json
import re
from pathlib import Path


# 返回 (地区文字, 信息来源)。
# 优先使用已观察到的出口 IP 地区；多个出口可显示多个地区，信息不全时保留未知提示，最后才按明确名称代码推断。
def node_region(item: dict) -> tuple[str, str]:
    exits = {q.get("ip"): q.get("region") for q in item.get("ip_scores", [])}
    ips = item.get("ips", [])
    # 只从本节点实际观察到的 ips 取地区，忽略无关缓存项；排序确保多地区展示稳定。
    regions = sorted({exits[ip] for ip in ips if exits.get(ip)})
    if regions:
        partial = any(not exits.get(ip) for ip in ips)
        return (" / ".join(regions) + (" / 部分出口未知" if partial else ""),
                "出口 IP · IPLark" + ("（部分）" if partial else ""))
    codes = {"US": "美国", "TW": "台湾", "JP": "日本", "SG": "新加坡",
             "HK": "香港", "KR": "韩国", "GB": "英国", "UK": "英国",
             "DE": "德国", "CA": "加拿大", "AU": "澳大利亚"}
    # 国家代码要求有明确字符边界，例如 BUS 里的 US 不能用于推断美国节点。
    match = re.search(r"(?<![A-Za-z0-9])(US|TW|JP|SG|HK|KR|GB|UK|DE|CA|AU)(?![A-Za-z])",
                      item["name"], re.IGNORECASE)
    if match:
        return codes[match.group().upper()], "名称推断"
    return "未知", "未取得地区信息"


# 保留全部节点并分状态排序：完整评分按综合分，待补全按性能部分分，连接失败和未检测置后。
# 名称作为同分时的确定性排序依据。
def ranked(results: list[dict]) -> list[dict]:
    # 为不同完整程度生成排序元组；负分数实现降序，避免连接失败但 IP 质量高的节点成为最高分候选。
    def key(item):
        if item.get("status") == "已完成":
            return (0, -(item["score"] or 0), item["name"])
        if item.get("available"):
            return (1, -item.get("performance_score", 0), item["name"])
        return (2 if item.get("status") == "连接失败" else 3, 0, item["name"])
    return sorted(results, key=key)


# 给可能被 Excel 当作公式的文本加单引号前缀。
# 外部节点名称及错误文本只能作为普通单元格内容，不能执行公式。
def safe_csv(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


# 给结果副本补充地区后生成三种报告，不改动测评原始列表。
# JSON 使用临时文件替换；CSV 保留查询时间 / 拦截提示；HTML 安全嵌入相同数据。
def write_report(report: dict, output: Path):
    # 仅在输出副本中附加排序和地区，不让导出函数改变原测评的节点顺序或样本。
    data = dict(report)
    data["results"] = []
    for item in ranked(report["results"]):
        region, source = node_region(item)
        data["results"].append(dict(item, region=region, region_source=source))
    # 兼容从节点查询结果恢复整批拦截提示，确保缓存结果携带的 warning 不在导出时丢失。
    if not data.get("iplark_block"):
        data["iplark_block"] = next((q["query_issue"] for item in data["results"]
                                     for q in item.get("ip_scores", []) if q.get("query_issue")), None)
    content = json.dumps(data, ensure_ascii=False, indent=2)
    # 先写完整 JSON 再替换目标，减少网页下载撞上半份 JSON 的机会；CSV / HTML 随后重写。
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
    # UTF-8 BOM 方便 Excel 正确识别中文；newline='' 交给 csv 模块处理各平台换行。
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
    # 嵌入 JSON 前转义 <、>、& 及行分隔符，防止节点名中的 </script> 结束数据脚本标签。
    # 转义改变的是 HTML 内嵌表示，JSON 下载文件仍保留原始节点文本。
    script_data = content.replace("&", "\\u0026").replace("<", "\\u003c").replace(
        ">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    (output / "report.html").write_text(
        (Path(__file__).parent / "report.html").read_text(encoding="utf-8").replace("__REPORT_DATA__", script_data),
        encoding="utf-8")
