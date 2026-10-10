# Contributing

Use Docker with Compose v2 and Linux containers. No host Python or Node.js
installation is required; dependencies are included in the Docker image.

Build and run the checks from the project directory:

```sh
docker build -t flclash-node-checker-test .
docker run --rm --network none --mount "type=bind,src=.,dst=/tests,readonly" -w /tests flclash-node-checker-test python -E -s -m unittest test_checker test_docker
docker run --rm --network none flclash-node-checker-test node --check /app/iplark_browser.mjs
```

The test mount must resolve to the project directory. If your Docker version
does not support relative bind sources, replace `src=.` with its absolute path.
Tests use temporary directories, synthetic IPs and mocked measurements.
Do not run a full network benchmark in CI: it consumes bandwidth and contacts
third-party services. CI also validates both Compose configurations.

Describe the problem, the resulting behavior and the focused checks in a pull
request. Preserve the fixed 20% speed weight, all-node coverage, cache timestamps,
and explicit incomplete / blocked statuses. Do not automate CAPTCHA answers.

Before committing, inspect `git diff --cached` and `git status`. Never attach a
real FlClash configuration, subscription URL, proxy credential, report, cache,
`.env`, or log. Use documentation-only reserved IP addresses and fictional node
names for fixtures. Preserve dependency licenses.
