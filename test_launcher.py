"""Windows startup settings: persistence, moved paths and explicit browser opt-out."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == 'nt', 'Windows launcher')
class LauncherTests(unittest.TestCase):
    def invoke(self, script, directory):
        source = str(Path(__file__).with_name('docker-settings.ps1')).replace("'", "''")
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                                 "$ErrorActionPreference='Stop'; . '" + source + "'; " + script],
                                cwd=directory, capture_output=True, text=True, encoding='utf-8',
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_default_does_not_require_cloakbrowser(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.invoke("(Get-LauncherSettings 'missing.json').browserMode", directory), 'chromium')

    def test_settings_roundtrip_chinese_path_and_relocation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'数据 with spaces'
            path.mkdir()
            (path/'config.yaml').write_text('proxies: []', encoding='utf-8')
            # Build Unicode text inside PowerShell to avoid its legacy input codepage.
            self.invoke("$folder=Get-ChildItem -Directory | Select-Object -First 1; "
                        "$s=Get-LauncherSettings 'settings.json'; $s.dataDirectory=$folder.FullName; "
                        "$s.browserMode='cloakbrowser'; Save-LauncherSettings 'settings.json' $s", directory)
            saved = json.loads((Path(directory)/'settings.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['dataDirectory'], str(path.resolve()))
            self.assertEqual(saved['browserMode'], 'cloakbrowser')
            relocated = path.with_name('moved')
            path.rename(relocated)
            self.invoke("$s=Get-LauncherSettings 'settings.json'; $s.dataDirectory=Join-Path (Get-Location) 'moved'; "
                        "Save-LauncherSettings 'settings.json' $s", directory)
            self.assertEqual(json.loads((Path(directory)/'settings.json').read_text())['dataDirectory'], str(relocated.resolve()))
            self.assertFalse((Path(directory)/'settings.json.tmp').exists())

    def test_invalid_directory_cannot_overwrite_saved_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory)/'settings.json'
            file.write_text('{"version":1,"dataDirectory":"old","browserMode":"chromium"}')
            before = file.read_bytes()
            self.invoke("$s=Get-LauncherSettings 'settings.json'; $s.dataDirectory='missing'; "
                        "$rejected=$false; try {Save-LauncherSettings 'settings.json' $s} catch {$rejected=$true}; "
                        "if (-not $rejected) {throw 'invalid path accepted'}", directory)
            self.assertEqual(file.read_bytes(), before)

    def test_opt_out_never_probes_or_uses_inherited_cdp(self):
        with tempfile.TemporaryDirectory() as directory:
            self.invoke("$env:IPLARK_CDP_URL='http://stale.invalid'; "
                        "function Test-LocalCdp {throw 'unexpected CDP probe'}; "
                        "$env:IPLARK_CDP_URL=Get-IPLarkCdpUrl 'chromium'; "
                        "if ($env:IPLARK_CDP_URL) {throw 'opt out failed'}; "
                        "if ((Get-IPLarkCdpUrl 'cloakbrowser') -ne 'http://host.docker.internal:9222') {throw 'opt in failed'}", directory)

    def test_running_batch_is_detected_before_restarting_service(self):
        with tempfile.TemporaryDirectory() as directory:
            self.invoke("function Invoke-RestMethod {return @{running=$true}}; "
                        "if (-not (Test-RunningBatch)) {throw 'active measurement missed'}; "
                        "function Invoke-RestMethod {return @{running=$false}}; "
                        "if (Test-RunningBatch) {throw 'idle service treated as running'}; "
                        "function Invoke-RestMethod {throw 'service offline'}; "
                        "if (Test-RunningBatch) {throw 'offline service treated as running'}", directory)


if __name__ == '__main__':
    unittest.main()
