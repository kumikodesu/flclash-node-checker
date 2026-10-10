# Security and privacy

Do not disclose proxy credentials, subscription links or unredacted reports in
public issues. For a vulnerability, use the repository's private vulnerability
reporting feature when available; otherwise contact the repository maintainer
through GitHub without publishing secrets.

The Docker web UI is bound to `127.0.0.1`; the optional CDP browser is accessible
only within the Compose network and has no published host port. The web UI
also rejects Host headers for remote domains, to guard against DNS rebinding.
Keep the web port local. CDP provides control of its browser. Configuration is mounted
read-only, but the checker must read node credentials to establish connections.

Reports intentionally include node names, exit IPs, regions and measurement
times and container configuration/core paths. Treat reports, the `checker-data`
Docker volume, exported results, caches, logs and `.env` as private.
Local configuration and exported `results/` are excluded from Git by default.
Share only a separately sanitized copy.

Measurements contact Cloudflare, Google, Microsoft and ipify through each node.
IPLark queries are made by the selected browser over the machine's normal
browser network route and include the measured exit IP in the URL. No proxy
password or subscription URL is submitted to IPLark. This project has no
telemetry service. Review dependency and image updates before deploying them.
