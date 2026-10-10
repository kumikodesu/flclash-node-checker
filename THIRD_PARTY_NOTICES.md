# Third-party software

The MIT license covers this project's original checker and launchers. It does
not replace the licenses of the following independent dependencies.

| Dependency | Use | License / source |
| --- | --- | --- |
| FlClash v0.8.93 | Unmodified core, run as a separate process over IPC | GPL-3.0; [source at the matching tag](https://github.com/chen08209/FlClash/tree/v0.8.93), [license](https://github.com/chen08209/FlClash/blob/v0.8.93/LICENSE) |
| requests 2.34.2 | HTTP client | Apache-2.0; license retained in `vendor/requests-2.34.2.dist-info/` |
| PyYAML 6.0.3 | Read configuration | MIT; license retained in `vendor/pyyaml-6.0.3.dist-info/` |
| certifi 2026.7.22 | CA certificates | MPL-2.0; license retained in `vendor/certifi-2026.7.22.dist-info/` |
| charset-normalizer 3.5.2 | HTTP text decoding | MIT; license retained in `vendor/charset_normalizer-3.5.2.dist-info/` |
| idna 3.20 | Domain handling | BSD-3-Clause; license retained in `vendor/idna-3.20.dist-info/` |
| urllib3 2.8.0 | HTTP transport | MIT; license retained in `vendor/urllib3-2.8.0.dist-info/` |
| CloakBrowser | Optional external Docker service | [Upstream license and binary terms](https://github.com/CloakHQ/CloakBrowser#license); the image is downloaded from its publisher when selected |
| Chromium, Python, Node.js, Debian | Container runtime | Their respective upstream licenses; installed from official images / Debian packages |

FlClash binaries are excluded from the Git repository. `prepare-assets.ps1`
downloads the official release package and verifies its SHA-256; Docker uses
only its unmodified core. See [docker-assets/README.md](docker-assets/README.md)
for binary and matching source links. Keep upstream notices and provide matching
source when redistributing a container that includes GPL software.

IPLark is an external website, not a dependency bundled in this project. Its
scores are read from public pages and attributed in the local report.
