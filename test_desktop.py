import bootstrap
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import requests

from gui import completion_summary
from iplark import IPLark, find_local_cdp


class DesktopTests(unittest.TestCase):
    def test_detects_local_cdp_and_falls_back_when_service_is_off(self):
        with patch('iplark.os', SimpleNamespace(name='nt')), patch('iplark.requests.Session') as session:
            connection = session.return_value.__enter__.return_value
            connection.get.return_value.json.return_value = {'webSocketDebuggerUrl': 'ws://127.0.0.1:9222/devtools/browser/test'}
            self.assertEqual(find_local_cdp(), 'http://127.0.0.1:9222')
            connection.get.assert_called_once_with('http://127.0.0.1:9222/json/version', timeout=2)
            self.assertFalse(connection.trust_env)
            connection.get.side_effect = requests.ConnectionError('service stopped')
            self.assertEqual(find_local_cdp(), '')

    def test_start_automatically_reuses_local_browser_without_edge(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {
                'IPLARK_CDP_URL': '', 'IPLARK_CACHE': str(Path(directory)/'cache.json')}):
            client = IPLark(lambda _: None, threading.Event())
        with patch('iplark.find_node', return_value=Path('node')), patch('iplark.find_local_cdp',
                return_value='http://127.0.0.1:9222'), patch('iplark.requests.Session') as session, patch.object(
                client, 'start_bridge') as bridge, patch('iplark.find_edge') as edge:
            session.return_value.__enter__.return_value.get.return_value.json.return_value = {
                'webSocketDebuggerUrl': 'ws://127.0.0.1:9222/devtools/browser/test'}
            client.start()
            bridge.assert_called_once_with(Path('node'), 'ws://127.0.0.1:9222/devtools/browser/test', remote=True)
            edge.assert_not_called()

    def test_completion_summary_preserves_missing_failed_and_unmeasured_nodes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'report.html'
            report = {'results': [
                {'name':'best', 'region':'台湾', 'status':'已完成', 'score':77.96},
                {'name':'missing', 'status':'IP评分待补全', 'score':None},
                {'name':'failed', 'status':'连接失败', 'score':0},
                {'name':'unmeasured', 'status':'未检测', 'score':None}],
                'iplark_block': {'message':'IPLark 返回 HTTP 429，请求被限流。'}}
            path.with_name('results.json').write_text(json.dumps(report), encoding='utf-8')
            summary = completion_summary(path)
            self.assertIn('已测 3/4', summary)
            self.assertIn('完整评分 1 · 连接失败 1 · 待补全 1', summary)
            self.assertIn('best · 台湾 · 77.96/100', summary)
            self.assertIn('HTTP 429', summary)
            report['results'] = [report['results'][1]]
            path.with_name('results.json').write_text(json.dumps(report), encoding='utf-8')
            self.assertNotIn('最高', completion_summary(path))


if __name__ == '__main__':
    unittest.main()
