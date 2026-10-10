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


class DockerTests(unittest.TestCase):
    def test_valid_cache_skips_browser_entirely(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'IPLARK_CACHE': str(Path(directory)/'cache.json')}):
            client = IPLark(lambda _: None, threading.Event())
            client.saved = {'203.0.113.1': {'score': 30, 'checked_at': datetime.now(timezone.utc).isoformat()}}
            with patch.object(client, 'start') as start:
                self.assertEqual(client.lookup('203.0.113.1')['source'], 'cached')
                start.assert_not_called()

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

    def test_query_spacing_can_be_cancelled_and_date_header_is_supported(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'IPLARK_CACHE': str(Path(directory)/'cache.json')}):
            cancel = threading.Event()
            client = IPLark(lambda _: None, cancel)
            client.record_query({'kind':'rate_limited'}, 'Sat, 10 Oct 2099 10:00:00 GMT')
            self.assertGreater(client.query_state['next_query_at'], time.time()+600)
            cancel.set()
            with self.assertRaises(InterruptedError):
                client.wait_between_queries()

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

    def test_windows_provider_paths_are_mapped_only_inside_mount(self):
        source = Path("/flclash/config.yaml")
        with patch.object(core, "os", SimpleNamespace(name="posix", environ={"FLCLASH_HOST_DIR": r"D:\Data\FlClash"})):
            path = core.config_file(source, r"D:\Data\FlClash\providers\nodes.yaml")
            self.assertEqual(path, source.parent / "providers" / "nodes.yaml")
            self.assertEqual(core.config_file(source, r"providers\nodes.yaml"), path)
            with self.assertRaises(ValueError):
                core.config_file(source, r"C:\Elsewhere\nodes.yaml")

    def test_tcp_rpc_handles_fragmented_frames_and_eof(self):
        with patch.object(core, "os", SimpleNamespace(name="posix")):
            client = core.TestCore(Path("/core"), Path("/config"), {}, [])
        local, remote = socket.socketpair()
        client.connection = local

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

    def test_batch_reloads_protocols_and_does_not_start_stale_protocol(self):
        batch = webui.Batch()
        with patch.object(webui, "resolve_paths", return_value=(Path("config.yaml"), Path("core"))), patch.object(
                webui, "load_nodes", return_value=({}, [{"name": "new", "type": "anytls"}])), patch.object(webui, "run") as runner:
            with self.assertRaises(ValueError):
                batch.start("ss")
            runner.assert_not_called()

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
