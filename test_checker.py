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


def setUpModule():
    # Exercise real mutex behavior without competing with an active user batch.
    import core
    global _test_mutex
    _test_mutex = patch.object(core, "RUN_MUTEX_NAME", "Local\\FlClashNodeBenchmark-tests-" + uuid.uuid4().hex)
    _test_mutex.start()


def tearDownModule():
    _test_mutex.stop()

def measured(ip_score=50):
    return {"name": "node", "protocol": "ss", "ips": ["203.0.113.1"],
            "probes": [{"kind": kind, "ok": True, "ms": 0} for kind in
                       ["Cloudflare"] * 5 + ["Google", "Microsoft", "ip", "ip"]],
            "downloads": [{"ok": True, "mbps": 100, "bytes": 100} for _ in range(2)],
            "ip_scores": [{"ip": "203.0.113.1", "score": ip_score,
                           "url": "https://iplark.com/203.0.113.1", "source": "live"}],
            "errors": []}


class ScoringTests(unittest.TestCase):
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

    def test_requested_speed_weight_and_fixed_scale(self):
        self.assertEqual(sum(WEIGHTS.values()), 100)
        self.assertEqual(WEIGHTS["speed"], 20)
        fast = score_node(measured())
        slow = measured()
        slow["downloads"] = [{"ok": False, "mbps": None, "bytes": 0}] * 2
        slow = score_node(slow)
        self.assertAlmostEqual(fast["score"] - slow["score"], 20)
        self.assertEqual(fast["score"], 80)

    def test_missing_ip_score_is_not_zero_or_reweighted(self):
        result = score_node(measured(None))
        self.assertIsNone(result["score"])
        self.assertEqual(result["performance_score"], 60)
        self.assertEqual(result["status"], "IP评分待补全")

    def test_rotating_exits_use_lowest_score_and_missing_blocks_total(self):
        result = measured(85)
        result["ip_scores"].append({"ip": "203.0.113.2", "score": 30})
        self.assertEqual(score_node(result)["iplark_score"], 30)
        result["ip_scores"][1]["score"] = None
        self.assertIsNone(score_node(result)["score"])

    def test_failed_second_download_cannot_inflate_speed_component(self):
        result = measured()
        result["downloads"][1] = {"ok": False, "mbps": None, "bytes": 50}
        self.assertEqual(score_node(result)["components"]["speed"], 50)

    def test_unreachable_node_with_high_reputation_does_not_win(self):
        result = measured(100)
        for sample in result["probes"]:
            sample.update(ok=False, ms=None)
        result["downloads"] = []
        result = score_node(result)
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["status"], "连接失败")

    def test_missing_and_untested_nodes_do_not_disappear(self):
        complete = score_node(measured(10))
        missing = score_node(measured(None))
        missing["name"] = "missing"
        untested = {"name": "untested", "protocol": "anytls", "score": None,
                    "status": "未检测", "available": False}
        rows = ranked([untested, missing, complete])
        self.assertEqual([r["name"] for r in rows], ["node", "missing", "untested"])

    def test_jitter_uses_same_endpoint_and_resets_across_failure(self):
        result = measured()
        for sample, delay in zip(result["probes"], [10, 20, 100, 110, 120, 800, 1200, 10, 10]):
            sample["ms"] = delay
        result["probes"][2].update(ok=False, ms=None)
        self.assertEqual(score_node(result)["jitter_ms"], 10)


class EnvironmentTests(unittest.TestCase):
    def test_protocol_options_refresh_and_accept_core_supported_new_types(self):
        options, selected = protocol_options([{'type':'ss'}, {'type':'anytls'}], 'SS')
        self.assertEqual(selected, 'ss')
        options, selected = protocol_options([{'type':'vless'}, {'type':'hysteria2'}], selected)
        self.assertEqual(options, ['all', 'hysteria2', 'vless'])
        self.assertEqual(selected, 'all')

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


class ScopeAndReportTests(unittest.TestCase):
    def setUp(self):
        for method in ('save_query_state', 'wait_between_queries'):
            mock = patch.object(IPLark, method)
            mock.start()
            self.addCleanup(mock.stop)

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

    def test_region_uses_observed_exits_and_retains_rotating_regions(self):
        item = {"name": "JP-test", "ips": ["a", "b"], "ip_scores": [
            {"ip": "a", "region": "美国"}, {"ip": "b", "region": "台湾"}]}
        region, source = node_region(item)
        self.assertEqual(set(region.split(" / ")), {"美国", "台湾"})
        self.assertEqual(source, "出口 IP · IPLark")
        item["ip_scores"].pop()
        self.assertIn("部分出口未知", node_region(item)[0])

    def test_region_name_fallback_is_explicit_and_ambiguous_names_stay_unknown(self):
        self.assertEqual(node_region({"name": "TW-demo-node"}), ("台湾", "名称推断"))
        self.assertEqual(node_region({"name": "TLS-demo-opaque"})[0], "未知")
        self.assertEqual(node_region({"name": "BUS-X1"})[0], "未知")

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

    def test_concurrent_batch_is_rejected_and_lock_released(self):
        with RunLock():
            with self.assertRaises(RuntimeError):
                with RunLock():
                    self.fail('A concurrent batch was allowed')
        with RunLock():
            pass

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

    def test_missing_provider_cache_fails_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "config.yaml"
            source.write_text("proxy-providers:\n  p:\n    type: http\n    path: missing.yaml\n")
            with self.assertRaises(FileNotFoundError):
                load_nodes(source)

    def test_duplicate_node_names_fail_instead_of_silently_dropping_nodes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "config.yaml"
            source.write_text("proxies:\n- {name: same, type: ss}\n- {name: same, type: anytls}\n")
            with self.assertRaises(ValueError):
                load_nodes(source)

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
