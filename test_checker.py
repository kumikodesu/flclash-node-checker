# 离线回归测试：覆盖固定评分、测评范围、地区来源、取消、缓存及报告导出。
# 使用虚构节点、文档保留 IP 和 mock，不访问真实代理或 IPLark。
# 通过构造失败和缺失样本，验证程序不会漏节点、夸大性能或伪造完整评分。
import copy
import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from unittest.mock import MagicMock, patch

import bootstrap
import yaml

from checker import WEIGHTS, download, measure_node, parse_args, run, score_node
from core import RunLock, load_nodes, protocol_options
from iplark import IPLark
from report import node_region, ranked, write_report


# 设置独立的 Windows 测试互斥量名称，不与用户的真实 Windows 测评批次争用；Linux 分支仍使用自己的测试容器文件锁。
def setUpModule():
    # Exercise real mutex behavior without competing with an active user batch.
    import core
    global _test_mutex
    _test_mutex = patch.object(core, "RUN_MUTEX_NAME", "Local\\FlClashNodeBenchmark-tests-" + uuid.uuid4().hex)
    _test_mutex.start()


# 撤销模块级 patch，防止测试互斥量名称影响同一解释器后续模块。
def tearDownModule():
    _test_mutex.stop()

# 生成可深拷贝修改的标准成功样本：九次探测、两次 100 Mbps 下载、一个可指定质量分的出口。
# 用它固定无关变量，单独验证评分因素。
def measured(ip_score=50):
    return {"name": "node", "protocol": "ss", "ips": ["203.0.113.1"],
            "probes": [{"kind": kind, "ok": True, "ms": 0} for kind in
                       ["Cloudflare"] * 5 + ["Google", "Microsoft", "ip", "ip"]],
            "downloads": [{"ok": True, "mbps": 100, "bytes": 100} for _ in range(2)],
            "ip_scores": [{"ip": "203.0.113.1", "score": ip_score,
                           "url": "https://iplark.com/203.0.113.1", "source": "live"}],
            "errors": []}


# 评分及原始采样回归：固定尺度、未知值、失败样本、轮换出口与波动定义。
class ScoringTests(unittest.TestCase):
    # 构造出口探测失败、重试成功的样本；确认重试另存，原始九次请求的失败率和错误仍保留。
    def test_exit_retry_does_not_erase_original_request_failures(self):
        samples = [{'kind': 'ip', 'ok': False, 'ms': None, 'error': 'ReadTimeout'}]
        samples += [{'kind': 'Cloudflare', 'ok': True, 'ms': 10} for _ in range(5)]
        samples += [{'kind': k, 'ok': True, 'ms': 10} for k in ['Google', 'Microsoft']]
        samples += [{'kind': 'ip', 'ok': False, 'ms': None, 'error': 'ReadTimeout'},
                    {'kind': 'ip', 'ok': True, 'ms': 10, 'ip': '203.0.113.1'}]
        with patch('checker.probe', side_effect=samples), patch('checker.download',
                return_value={'ok': True, 'mbps': 10, 'bytes': 1024}):
            item = measure_node(12345, {'name':'retry', 'type':'ss'}, 1024,
                                threading.Event(), lambda _: None)
        scored = score_node(item)
        self.assertEqual(len(item['probes']), 9)
        self.assertEqual(len(item['exit_retries']), 1)
        self.assertEqual(item['ips'], ['203.0.113.1'])
        self.assertAlmostEqual(scored['success_pct'], 700 / 9)
        self.assertIn('ReadTimeout', item['errors'])

    # 模拟已下载 256 KiB 后触发采样截止；验证慢速但连通的节点仍得到真实非零网速，而非被误记为连接失败。
    def test_planned_time_cutoff_still_measures_a_slow_working_node(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {}
        response.iter_content.return_value = [b'x' * (256 * 1024)]
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.return_value = response
        with patch('checker.session_for', return_value=session), patch(
                'checker.time.perf_counter', side_effect=[0, 0, 21, 21]):
            sample = download(12345, 8 * 1024 * 1024, threading.Event())
        self.assertTrue(sample['ok'])
        self.assertTrue(sample['time_limited'])
        self.assertEqual(sample['bytes'], 256 * 1024)
        self.assertGreater(sample['mbps'], 0)

    # 固定其他样本，只让下载失败；验证总分差恰为 20，权重总和为 100，评分不依赖同批其他节点。
    def test_requested_speed_weight_and_fixed_scale(self):
        self.assertEqual(sum(WEIGHTS.values()), 100)
        self.assertEqual(WEIGHTS["speed"], 20)
        fast = score_node(measured())
        slow = measured()
        slow["downloads"] = [{"ok": False, "mbps": None, "bytes": 0}] * 2
        slow = score_node(slow)
        self.assertAlmostEqual(fast["score"] - slow["score"], 20)
        self.assertEqual(fast["score"], 80)

    # IP 分数缺失时应保留性能部分分 60、综合分 None 及待补全状态，不把未知当零分或重新分配权重。
    def test_missing_ip_score_is_not_zero_or_reweighted(self):
        result = score_node(measured(None))
        self.assertIsNone(result["score"])
        self.assertEqual(result["performance_score"], 60)
        self.assertEqual(result["status"], "IP评分待补全")

    # 构造两个轮换出口；验证取最低已知质量分，任一出口缺分就阻止生成完整综合分。
    def test_rotating_exits_use_lowest_score_and_missing_blocks_total(self):
        result = measured(85)
        result["ip_scores"].append({"ip": "203.0.113.2", "score": 30})
        self.assertEqual(score_node(result)["iplark_score"], 30)
        result["ip_scores"][1]["score"] = None
        self.assertIsNone(score_node(result)["score"])

    # 两次下载只成功一次时，速度分项按成功比例降低，不能因忽略失败样本而抬高评分。
    def test_failed_second_download_cannot_inflate_speed_component(self):
        result = measured()
        result["downloads"][1] = {"ok": False, "mbps": None, "bytes": 50}
        self.assertEqual(score_node(result)["components"]["speed"], 50)

    # 让全部性能请求失败但 IP 质量满分；验证节点仍归为连接失败且总分为零。
    def test_unreachable_node_with_high_reputation_does_not_win(self):
        result = measured(100)
        for sample in result["probes"]:
            sample.update(ok=False, ms=None)
        result["downloads"] = []
        result = score_node(result)
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["status"], "连接失败")

    # 混合完整、缺分和未检测行；验证排序改变顺序但不丢弃任何节点。
    def test_missing_and_untested_nodes_do_not_disappear(self):
        complete = score_node(measured(10))
        missing = score_node(measured(None))
        missing["name"] = "missing"
        untested = {"name": "untested", "protocol": "anytls", "score": None,
                    "status": "未检测", "available": False}
        rows = ranked([untested, missing, complete])
        self.assertEqual([r["name"] for r in rows], ["node", "missing", "untested"])

    # 加入不同站点的长延迟及中途失败；确认波动仅由相邻成功的 Cloudflare 样本计算，失败打断序列。
    def test_jitter_uses_same_endpoint_and_resets_across_failure(self):
        result = measured()
        for sample, delay in zip(result["probes"], [10, 20, 100, 110, 120, 800, 1200, 10, 10]):
            sample["ms"] = delay
        result["probes"][2].update(ok=False, ms=None)
        self.assertEqual(score_node(result)["jitter_ms"], 10)


# 配置变化回归：每批重新读取配置，协议列表不依赖上次启动结果。
class EnvironmentTests(unittest.TestCase):
    # 切换到新协议集合，验证协议自动刷新、小写归一化，旧选择失效时返回 all。
    def test_protocol_options_refresh_and_accept_core_supported_new_types(self):
        options, selected = protocol_options([{'type':'ss'}, {'type':'anytls'}], 'SS')
        self.assertEqual(selected, 'ss')
        options, selected = protocol_options([{'type':'vless'}, {'type':'hysteria2'}], selected)
        self.assertEqual(options, ['all', 'hysteria2', 'vless'])
        self.assertEqual(selected, 'all')

    # 连续两批之间更换配置内容，使用已取消批次避免真实测速；确认每批都重新解析路径与节点。
    def test_each_batch_resolves_paths_again_and_reads_switched_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'config.yaml'
            args = parse_args(['--output', str(root/'results')])
            self.assertIsNone(args.config)
            cancel = threading.Event()
            cancel.set()
            with patch('checker.resolve_paths', return_value=(source, root/'core.exe')) as resolve, patch('checker.TestCore'):
                for name, protocol in [('first','ss'), ('second','vless')]:
                    source.write_text(yaml.safe_dump({'proxies':[{'name':name, 'type':protocol}]}))
                    path = run(args, cancel, notify=lambda _: None)
                    saved = json.loads(path.with_name('results.json').read_text(encoding='utf-8'))
                    self.assertEqual([(r['name'], r['protocol']) for r in saved['results']], [(name,protocol)])
                self.assertEqual(resolve.call_count, 2)


# 范围与输出回归：所有节点保留、取消清理、地区来源、缓存和导出安全。
class ScopeAndReportTests(unittest.TestCase):
    # 为每个测试禁用 IPLark 冷却状态的落盘和真实等待；addCleanup 自动撤销 patch，避免测试互相影响。
    def setUp(self):
        for method in ('save_query_state', 'wait_between_queries'):
            mock = patch.object(IPLark, method)
            mock.start()
            self.addCleanup(mock.stop)

    # 模拟实时验证被拦截及已有有效缓存；检查缓存分数可继续使用，验证提示仍存在，后续新 IP 不再查询。
    def test_iplark_verification_is_preserved_with_cache_and_stops_later_queries(self):
        client = IPLark(lambda _: None, threading.Event())
        client.bridge = MagicMock()
        client.saved = {'203.0.113.1': {'score': 60, 'checked_at': datetime.now(timezone.utc).isoformat()}}
        blocked = {'ip': '203.0.113.2', 'score': None, 'needs_verification': True,
                   'source': 'missing', 'http_status': 403}
        with patch.object(client, 'wait', return_value=blocked) as wait, patch.object(client, 'close'):
            pending = client.lookup('203.0.113.2')
            cached = client.lookup('203.0.113.1')
        self.assertEqual(cached['source'], 'cached')
        self.assertEqual(cached['query_issue']['kind'], 'verification')
        self.assertEqual(pending['query_issue'], cached['query_issue'])
        self.assertIsNone(pending['score'])
        self.assertEqual(wait.call_count, 1)
        self.assertEqual(client.block_event['ip'], '203.0.113.2')
        self.assertTrue(client.block_event['checked_at'])

    # 分别模拟 HTTP 403、429 和桥接超时，确认拒绝、限流及普通超时不会混为同一结果。
    def test_iplark_denial_rate_limit_and_timeout_are_distinguished(self):
        for status, kind in [(403, 'access_denied'), (429, 'rate_limited'), (200, None)]:
            client = IPLark(lambda _: None, threading.Event())
            client.bridge = MagicMock()
            client.saved = {}
            with patch.object(client, 'wait', return_value={
                    'ip':'203.0.113.1', 'score':None, 'source':'missing', 'http_status':status}), patch.object(client, 'close'):
                result = client.lookup('203.0.113.1')
            self.assertEqual(result.get('query_issue', {}).get('kind'), kind)
        client = IPLark(lambda _: None, threading.Event())
        client.bridge = MagicMock()
        client.saved = {}
        with patch.object(client, 'wait', side_effect=TimeoutError('timeout')), patch.object(client, 'close'):
            result = client.lookup('203.0.113.1')
        self.assertNotIn('query_issue', result)

    # 用有效缓存补全分数的同时附带验证事件，检查 JSON 与 CSV 都保留拦截提示。
    def test_report_exports_verification_warning_even_if_cache_completes_score(self):
        with tempfile.TemporaryDirectory() as directory:
            item = score_node(measured(60))
            issue = {'kind':'verification', 'message':'IPLark 要求安全验证',
                     'ip':'203.0.113.1', 'checked_at':datetime.now(timezone.utc).isoformat()}
            item['ip_scores'][0].update(source='cached', query_issue=issue)
            report = {'results':[item], 'weights':WEIGHTS}
            write_report(report, Path(directory))
            saved = json.loads(Path(directory,'results.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['iplark_block'], issue)
            self.assertEqual(saved['results'][0]['score'], item['score'])
            with Path(directory,'results.csv').open(encoding='utf-8-sig',newline='') as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(rows[0]['IPLark查询提示'], issue['message'])

    # 名称与出口地区不一致时优先出口；多出口保留多地区，部分地区缺失时标注未知。
    def test_region_uses_observed_exits_and_retains_rotating_regions(self):
        item = {"name": "JP-test", "ips": ["a", "b"], "ip_scores": [
            {"ip": "a", "region": "美国"}, {"ip": "b", "region": "台湾"}]}
        region, source = node_region(item)
        self.assertEqual(set(region.split(" / ")), {"美国", "台湾"})
        self.assertEqual(source, "出口 IP · IPLark")
        item["ip_scores"].pop()
        self.assertIn("部分出口未知", node_region(item)[0])

    # 验证名称代码推断会标明来源，并拒绝把普通单词中偶然出现的国家缩写误判成地区。
    def test_region_name_fallback_is_explicit_and_ambiguous_names_stay_unknown(self):
        self.assertEqual(node_region({"name": "TW-demo-node"}), ("台湾", "名称推断"))
        self.assertEqual(node_region({"name": "TLS-demo-opaque"})[0], "未知")
        self.assertEqual(node_region({"name": "BUS-X1"})[0], "未知")

    # 批次开始即取消，模拟独立核心上下文；确认全部选中节点仍在报告中，资源退出方法仍调用。
    def test_stop_keeps_every_node_and_closes_owned_core(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'config.yaml'
            source.write_text('proxies:\n- {name: a, type: ss}\n- {name: b, type: anytls}\n')
            executable = root/'FlClashCore.exe'
            executable.write_bytes(b'fixture')
            args = parse_args(['--config', str(source), '--core', str(executable),
                               '--output', str(root / 'results')])
            cancel = threading.Event()
            cancel.set()
            with patch('checker.TestCore') as core, patch('checker.IPLark') as reputation:
                path = run(args, cancel, notify=lambda _: None)
            core.return_value.__exit__.assert_called_once()
            reputation.assert_not_called()
            report = json.loads(path.with_name('results.json').read_text(encoding='utf-8'))
            self.assertTrue(report['cancelled'])
            self.assertEqual({n['name'] for n in report['results']}, {'a', 'b'})
            self.assertTrue(all(n['status']=='未检测' and n['score'] is None for n in report['results']))

    # 持有批次锁时尝试重复进入应失败，退出后应能再次进入，验证互斥与清理。
    def test_concurrent_batch_is_rejected_and_lock_released(self):
        with RunLock():
            with self.assertRaises(RuntimeError):
                with RunLock():
                    self.fail('A concurrent batch was allowed')
        with RunLock():
            pass

    # 构造新鲜、过期、未来时间和未知评分缓存；只允许有效分数复用，并标记 cached 来源。
    def test_only_recent_verified_ip_cache_is_used_and_is_marked(self):
        client = IPLark(lambda _: None, threading.Event())
        ip = '203.0.113.1'
        client.saved = {ip: {'score': 85, 'checked_at': datetime.now(timezone.utc).isoformat()}}
        self.assertEqual(client.cached_score(ip)['source'], 'cached')
        self.assertIsNone(client.cached_score('203.0.113.2'))
        client.saved[ip]['checked_at'] = (datetime.now(timezone.utc)-timedelta(hours=25)).isoformat()
        self.assertIsNone(client.cached_score(ip))
        client.saved[ip] = {'score': 101, 'checked_at': datetime.now(timezone.utc).isoformat()}
        self.assertIsNone(client.cached_score(ip))

    # 配置同时含内联节点和集合缓存；验证都进入范围，并逐字节确认原文件未被修改。
    def test_cached_provider_nodes_are_included_without_changing_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "config.yaml"
            provider = root / "provider.yaml"
            source.write_text(yaml.safe_dump({"proxies": [{"name": "inline", "type": "ss"}],
                                             "proxy-providers": {"p": {"type": "http", "path": "provider.yaml"}}}))
            provider.write_text(yaml.safe_dump({"proxies": [{"name": "cached", "type": "anytls"}]}))
            original = source.read_bytes(), provider.read_bytes()
            _, nodes = load_nodes(source)
            self.assertEqual([n["name"] for n in nodes], ["inline", "cached"])
            self.assertEqual(original, (source.read_bytes(), provider.read_bytes()))

    # 缺失集合文件时必须报错，不能继续只测剩余内联节点造成不完整报告。
    def test_missing_provider_cache_fails_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "config.yaml"
            source.write_text("proxy-providers:\n  p:\n    type: http\n    path: missing.yaml\n")
            with self.assertRaises(FileNotFoundError):
                load_nodes(source)

    # 把规则缓存误当代理集合；验证缺少 proxies 列表时明确拒绝，不能悄悄忽略。
    def test_wrong_provider_cache_cannot_silently_drop_nodes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "config.yaml"
            source.write_text("proxies:\n- {name: demo, type: ss}\nproxy-providers:\n  p:\n    path: provider.yaml\n")
            (root / "provider.yaml").write_text("rules: [DOMAIN, example.invalid]\n")
            with self.assertRaises(ValueError):
                load_nodes(source)

    # 同名不同协议节点也会造成选择歧义；验证必须先报错，而不是按名称去重导致漏测。
    def test_duplicate_node_names_fail_instead_of_silently_dropping_nodes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "config.yaml"
            source.write_text("proxies:\n- {name: same, type: ss}\n- {name: same, type: anytls}\n")
            with self.assertRaises(ValueError):
                load_nodes(source)

    # 使用脚本标签和公式形式的虚构节点名称；验证导出保留全部行，并阻止 HTML / CSV 输入被执行。
    def test_html_and_csv_export_untrusted_names_safely_and_keep_all_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unsafe = score_node(measured())
            unsafe["name"] = '</script><img src=x onerror="alert(1)">'
            formula = copy.deepcopy(unsafe)
            formula["name"] = '=HYPERLINK("https://example.com")'
            report = {"created_at": "2026-10-09T12:00:00+08:00", "source": "test",
                      "weights": WEIGHTS, "results": [unsafe, formula]}
            write_report(report, root)
            saved = json.loads((root / "results.json").read_text(encoding="utf-8"))
            self.assertEqual(len(saved["results"]), 2)
            self.assertTrue(all(r["region"] == "未知" for r in saved["results"]))
            html = (root / "report.html").read_text(encoding="utf-8")
            self.assertNotIn(unsafe["name"], html)
            self.assertIn("\\u003c/script", html)
            with (root / "results.csv").open(encoding="utf-8-sig", newline="") as file:
                rows = list(csv.reader(file))
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0][1:3], ["节点地区", "地区来源"])
            self.assertTrue(next(row[0] for row in rows if "HYPERLINK" in row[0]).startswith("'="))


if __name__ == "__main__":
    unittest.main()
