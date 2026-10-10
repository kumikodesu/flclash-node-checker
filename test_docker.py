# 容器与接口回归测试：覆盖冷却持久化、CDP、路径映射、IPC 分帧和网页鉴权。
# HTTP 服务和 socketpair 仅在测试环境通信；浏览器和外部 HTTP 查询由 mock 替代。
# 关注正常流程之外的 EOF、非法 Host、无效令牌及旧协议等边界条件。
import bootstrap
import http.client
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from unittest.mock import MagicMock

import core
import webui
from iplark import IPLark


# Docker 运行边界的离线测试集合，不要求连接真实节点或真实浏览器。
class DockerTests(unittest.TestCase):
    # 有效磁盘缓存命中时返回 cached，确认没有调用浏览器启动，避免不必要的网络查询。
    def test_valid_cache_skips_browser_entirely(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'IPLARK_CACHE': str(Path(directory)/'cache.json')}):
            client = IPLark(lambda _: None, threading.Event())
            client.saved = {'203.0.113.1': {'score': 30, 'checked_at': datetime.now(timezone.utc).isoformat()}}
            with patch.object(client, 'start') as start:
                self.assertEqual(client.lookup('203.0.113.1')['source'], 'cached')
                start.assert_not_called()

    # 记录 429 与较长 Retry-After，再创建新客户端；验证冷却跨实例保留且阻止实时浏览器启动。
    def test_cooldown_survives_new_client_and_respects_retry_after(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'IPLARK_CACHE': str(Path(directory)/'cache.json')}):
            client = IPLark(lambda _: None, threading.Event())
            issue = {'kind': 'rate_limited', 'message': 'HTTP 429', 'ip': '203.0.113.1'}
            client.record_query(issue, '1200')
            self.assertGreaterEqual(client.query_state['next_query_at'], time.time()+1199)
            resumed = IPLark(lambda _: None, threading.Event())
            with patch.object(resumed, 'start') as start:
                self.assertEqual(resumed.lookup('203.0.113.2')['query_issue']['kind'], 'rate_limited')
                start.assert_not_called()
            self.assertIn('resume_after', resumed.block_event)

    # 验证 HTTP 日期格式的 Retry-After 能转换等待时间，同时停止事件能取消查询间隔。
    def test_query_spacing_can_be_cancelled_and_date_header_is_supported(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'IPLARK_CACHE': str(Path(directory)/'cache.json')}):
            cancel = threading.Event()
            client = IPLark(lambda _: None, cancel)
            client.record_query({'kind':'rate_limited'}, 'Sat, 10 Oct 2099 10:00:00 GMT')
            self.assertGreater(client.query_state['next_query_at'], time.time()+600)
            cancel.set()
            with self.assertRaises(InterruptedError):
                client.wait_between_queries()

    # 模拟显式 CDP 服务的元数据，验证以远程模式启动桥接，不创建本地 Chromium 或临时配置目录。
    def test_remote_iplark_uses_cdp_without_launching_local_browser(self):
        with patch.dict("os.environ", {"IPLARK_CDP_URL": "http://host.docker.internal:9222"}):
            client = IPLark(lambda _: None, threading.Event())
        response = MagicMock()
        response.json.return_value = {"webSocketDebuggerUrl": "ws://host.docker.internal:9222/devtools/browser/test"}
        with patch("iplark.find_node", return_value=Path("node")), patch("iplark.requests.Session") as session, patch.object(
                client, "start_bridge") as bridge, patch("iplark.find_edge") as edge:
            session.return_value.__enter__.return_value.get.return_value = response
            client.start()
            bridge.assert_called_once_with(Path("node"), response.json.return_value["webSocketDebuggerUrl"], remote=True)
            edge.assert_not_called()
            self.assertIsNone(client.browser)
            self.assertIsNone(client.temp)

    # 在容器语义下映射 Windows 绝对 / 相对集合路径，并确认挂载目录之外的绝对路径会被拒绝。
    def test_windows_provider_paths_are_mapped_only_inside_mount(self):
        source = Path("/flclash/config.yaml")
        with patch.object(core, "os", SimpleNamespace(name="posix", environ={"FLCLASH_HOST_DIR": r"D:\Data\FlClash"})):
            path = core.config_file(source, r"D:\Data\FlClash\providers\nodes.yaml")
            self.assertEqual(path, source.parent / "providers" / "nodes.yaml")
            self.assertEqual(core.config_file(source, r"providers\nodes.yaml"), path)
            with self.assertRaises(ValueError):
                core.config_file(source, r"C:\Elsewhere\nodes.yaml")

    # 通过 socketpair 把一个 RPC 响应拆为数段；验证完整重组并在对端关闭后报告 EOF。
    def test_tcp_rpc_handles_fragmented_frames_and_eof(self):
        with patch.object(core, "os", SimpleNamespace(name="posix")):
            client = core.TestCore(Path("/core"), Path("/config"), {}, [])
        local, remote = socket.socketpair()
        client.connection = local

        # 模拟核心端：完整读取请求帧，沿用请求 ID，再分三段返回响应，主动制造真实 TCP 分帧场景。
        def peer():
            with remote:
                raw = bytearray()
                while len(raw) < 4:
                    raw.extend(remote.recv(4 - len(raw)))
                length = struct.unpack("<I", raw)[0]
                raw = bytearray()
                while len(raw) < length:
                    raw.extend(remote.recv(length - len(raw)))
                request = json.loads(raw)
                payload = json.dumps({"id": request["id"], "code": 0, "data": {"loaded": 56}}).encode()
                frame = struct.pack("<I", len(payload)) + payload
                for chunk in (frame[:2], frame[2:7], frame[7:]):
                    remote.sendall(chunk)

        thread = threading.Thread(target=peer)
        thread.start()
        try:
            self.assertEqual(client.rpc("getProxies", timeout=2), {"loaded": 56})
            thread.join(timeout=2)
            with self.assertRaises(EOFError):
                client.read_exact(4, time.monotonic() + 1)
        finally:
            client.close()

    # 启动临时回环 HTTP 服务；测试健康检查、无令牌拒绝、合法启动请求及目录穿越下载拒绝。
    def test_web_actions_require_token_and_do_not_serve_arbitrary_files(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(webui, "OUTPUT", Path(temporary)), patch.object(
                webui.Handler.batch, "start") as start:
            server = webui.ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                connection.request("GET", "/health")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertTrue(json.loads(response.read())["ok"])
                connection.request("POST", "/api/start", '{}', {"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
                start.assert_not_called()
                connection.request("POST", "/api/start", '{"protocol":"all"}',
                                   {"Content-Type": "application/json", "X-Checker-Token": webui.TOKEN})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                start.assert_called_once_with("all")
                connection.request("GET", "/reports/../iplark-cache.json")
                response = connection.getresponse()
                self.assertEqual(response.status, 404)
                response.read()
            finally:
                connection.close()
                server.shutdown()
                server.server_close()

    # 当前配置已经从 ss 切为 anytls 时，旧协议启动必须失败，并确认真正的测评函数未执行。
    def test_batch_reloads_protocols_and_does_not_start_stale_protocol(self):
        batch = webui.Batch()
        with patch.object(webui, "resolve_paths", return_value=(Path("config.yaml"), Path("core"))), patch.object(
                webui, "load_nodes", return_value=({}, [{"name": "new", "type": "anytls"}])), patch.object(webui, "run") as runner:
            with self.assertRaises(ValueError):
                batch.start("ss")
            runner.assert_not_called()

    # 用外部域名 Host 请求回环服务，即使令牌正确也不能读取主页 / 状态或启动批次，验证 DNS 重绑定防护。
    def test_remote_host_cannot_read_local_token_or_trigger_batch(self):
        server = webui.ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            for path in ("/", "/api/status", "/health"):
                connection.request("GET", path, headers={"Host": "untrusted.example"})
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                self.assertNotIn(webui.TOKEN, response.read().decode())
            with patch.object(webui.Handler.batch, "start") as start:
                connection.request("POST", "/api/start", '{}', {
                    "Host": "untrusted.example", "Content-Type": "application/json",
                    "X-Checker-Token": webui.TOKEN})
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
                start.assert_not_called()
        finally:
            connection.close()
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
