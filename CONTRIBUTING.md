# Contributing

Use Python 3.10+ (with Tkinter on Windows) and Node.js 22+. Dependencies are
included under `vendor/`; `bootstrap.py` adds that directory to the import path.

```powershell
python -E -s -X utf8 -m unittest test_checker test_docker test_desktop test_launcher
node --check iplark_browser.mjs
```

The Windows tests cover path discovery and launcher behavior. On Linux use
`python -E -s -m unittest test_docker`; Windows-specific tests need Windows.
Unit tests use synthetic IPs and mocked measurements. Do not run a full network
benchmark in CI: it consumes bandwidth and contacts third-party services.

Describe the problem, the resulting behavior and the focused checks in a pull
request. Preserve the fixed 20% speed weight, all-node coverage, cache timestamps,
and explicit incomplete / blocked statuses. Do not automate CAPTCHA answers.

Before committing, inspect `git diff --cached` and `git status`. Never attach a
real FlClash configuration, subscription URL, proxy credential, report, cache,
launcher setting, or startup log. Use documentation-only reserved IP addresses
and fictional node names for fixtures. Preserve dependency licenses.
