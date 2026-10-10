import bootstrap
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml
import webui
from config_import import bundle_config


NODE = {"name": "US-demo", "type": "ss", "server": "192.0.2.1", "port": 443,
        "cipher": "aes-128-gcm", "password": "fictional-test-password"}


def file(path, data):
    return {"path": path, "content": yaml.safe_dump(data) if isinstance(data, dict) else data}


class ImportTests(unittest.TestCase):
    def test_single_config_preserves_nodes_without_subscription_or_controller_secret(self):
        text, count = bundle_config([file("subscription.yaml", {"proxies": [NODE], "secret": "private",
                                                               "external-controller": "0.0.0.0:9090"})])
        result = yaml.safe_load(text)
        self.assertEqual(count, 1)
        self.assertEqual(result["proxies"], [NODE])
        self.assertNotIn("secret", result)
        self.assertNotIn("external-controller", result)

    def test_folder_includes_extensionless_provider_cache_and_windows_absolute_paths(self):
        text, count = bundle_config([
            file("config.yaml", {"proxies": [NODE], "proxy-providers": {"p": {
                "type": "http", "url": "https://example.invalid/private-subscription",
                "path": r"D:\FlClashData\profiles\providers\p\proxy\123abc"}}}),
            file("profiles/providers/p/proxy/123abc", {"proxies": [{**NODE, "name": "JP-demo", "type": "anytls"}]}),
        ])
        result = yaml.safe_load(text)
        self.assertEqual(count, 2)
        self.assertEqual([n["type"] for n in result["proxies"]], ["ss", "anytls"])
        self.assertNotIn("private-subscription", text)
        self.assertNotIn("proxy-providers", result)

    def test_external_certificate_is_embedded_without_host_path(self):
        pem = "-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n"
        text, _ = bundle_config([file("config.yaml", {"proxies": [{**NODE, "ca": "/original/certs/root.pem"}]}),
                                 file("certs/root.pem", pem)])
        self.assertEqual(yaml.safe_load(text)["proxies"][0]["ca"], pem)

    def test_wireguard_inline_private_key_is_not_treated_as_a_file(self):
        node = {**NODE, "type": "wireguard", "private-key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}
        text, _ = bundle_config([file("config.yaml", {"proxies": [node]})])
        self.assertEqual(yaml.safe_load(text)["proxies"][0]["private-key"], node["private-key"])

    def test_missing_or_ambiguous_provider_file_and_duplicate_nodes_are_rejected(self):
        config = file("config.yaml", {"proxy-providers": {"p": {"path": "/old/nodes.yaml"}}})
        for entries in ([config], [config, file("a/nodes.yaml", {"proxies": [NODE]}),
                                  file("b/nodes.yaml", {"proxies": [NODE]})],
                        [file("config.yaml", {"proxies": [NODE, NODE]})]):
            with self.assertRaises(ValueError):
                bundle_config(entries)

    def test_uploaded_paths_cannot_escape_or_read_host_files(self):
        for name in ("../config.yaml", "/config.yaml", "a/../config.yaml", "C:/config.yaml", "a\\config.yaml"):
            with self.assertRaises(ValueError):
                bundle_config([file(name, {"proxies": [NODE]})])
        with self.assertRaises(ValueError):
            bundle_config([file("config.yaml", {"proxy-providers": {"p": {"path": "/etc/passwd"}}})])

    def test_wrong_provider_cache_cannot_silently_drop_nodes(self):
        with self.assertRaises(ValueError):
            bundle_config([file("config.yaml", {"proxies": [NODE], "proxy-providers": {"p": {"path": "nodes.yaml"}}}),
                           file("nodes.yaml", {"rules": ["DOMAIN,example.invalid"]})])

    def test_yaml_error_never_exposes_credentials(self):
        with self.assertRaises(ValueError) as result:
            bundle_config([file("config.yaml", "password: PRIVATE-MARKER\nproxies: [")])
        self.assertNotIn("PRIVATE-MARKER", str(result.exception))

    def test_failed_import_preserves_previous_snapshot_and_active_batch_rejects_import(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(webui, "IMPORTED", Path(directory)/"config.yaml"):
            batch = webui.Batch()
            with patch.object(batch, "configuration", return_value=(None, {"count": 1})):
                batch.import_config([file("config.yaml", {"proxies": [NODE]})])
            before = webui.IMPORTED.read_bytes()
            with self.assertRaises(ValueError):
                batch.import_config([file("config.yaml", {"proxies": []})])
            self.assertEqual(webui.IMPORTED.read_bytes(), before)
            batch.state["running"] = True
            with self.assertRaises(ValueError):
                batch.import_config([file("config.yaml", {"proxies": [{**NODE, "name": "new"}]})])
            self.assertEqual(webui.IMPORTED.read_bytes(), before)

    def test_protocol_refresh_reports_snapshot_or_mounted_source_and_reloads(self):
        batch = webui.Batch()
        with patch.object(webui, "resolve_paths", return_value=(webui.IMPORTED.resolve(), Path("core"))), patch.object(
                webui, "load_nodes", side_effect=[({}, [NODE]), ({}, [{**NODE, "type": "anytls"}])]):
            first = batch.configuration()[1]
            second = batch.configuration()[1]
            self.assertEqual(first["source_mode"], "imported")
            self.assertEqual(first["protocols"], ["all", "ss"])
            self.assertEqual(second["protocols"], ["all", "anytls"])
        with patch.object(webui, "resolve_paths", return_value=(Path("/flclash/config.yaml"), Path("core"))), patch.object(
                webui, "load_nodes", return_value=({}, [NODE])):
            self.assertEqual(batch.configuration()[1]["source_mode"], "mounted")

    def test_automatic_read_failure_uses_saved_snapshot_and_retries_on_refresh(self):
        batch = webui.Batch()
        mounted = Path("/flclash/config.yaml")
        with patch.object(Path, "is_file", return_value=True), patch.object(
                webui, "resolve_paths", side_effect=[FileNotFoundError("missing"),
                    (webui.IMPORTED.resolve(), Path("core")), (mounted, Path("core"))]), patch.object(
                webui, "load_nodes", side_effect=[({}, [NODE]), ({}, [{**NODE, "type": "anytls"}])]):
            fallback = batch.configuration()[1]
            self.assertEqual(fallback["source_mode"], "imported")
            self.assertTrue(fallback["automatic_failed"])
            self.assertFalse(batch.use_imported)
            recovered = batch.configuration()[1]
            self.assertEqual(recovered["source_mode"], "mounted")
            self.assertFalse(recovered["automatic_failed"])
            self.assertEqual(recovered["protocols"], ["all", "anytls"])

    def test_invalid_automatic_yaml_falls_back_without_disclosing_credentials(self):
        batch = webui.Batch()
        with patch.object(Path, "is_file", return_value=True), patch.object(
                webui, "resolve_paths", side_effect=[(Path("/flclash/config.yaml"), Path("core")),
                    (webui.IMPORTED.resolve(), Path("core"))]), patch.object(
                webui, "load_nodes", side_effect=[yaml.YAMLError("PRIVATE-MARKER"), ({}, [NODE])]):
            result = batch.configuration()[1]
            self.assertTrue(result["automatic_failed"])
            self.assertNotIn("PRIVATE-MARKER", str(result))

    def test_automatic_config_with_missing_node_type_uses_saved_snapshot(self):
        batch = webui.Batch()
        with patch.object(Path, "is_file", return_value=True), patch.object(
                webui, "resolve_paths", side_effect=[(Path("/flclash/config.yaml"), Path("core")),
                    (webui.IMPORTED.resolve(), Path("core"))]), patch.object(
                webui, "load_nodes", side_effect=[({}, [{"name": "invalid"}]), ({}, [NODE])]):
            result = batch.configuration()[1]
            self.assertTrue(result["automatic_failed"])
            self.assertEqual(result["protocols"], ["all", "ss"])


if __name__ == "__main__":
    unittest.main()
