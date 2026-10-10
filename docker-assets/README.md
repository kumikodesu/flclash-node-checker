# FlClash core asset

Git does not contain the binary. On Windows, run `prepare-assets.ps1` (the Docker
launcher does this automatically). It downloads:

- [Official FlClash v0.8.93 Linux amd64 package](https://github.com/chen08209/FlClash/releases/download/v0.8.93/FlClash-0.8.93-linux-amd64.deb)
- Local filename: `flclash-amd64.deb`
- SHA-256: `135da95baaf41f077521b98c0170554da432348b2e3aee73d6f4f050119e7917`
- [Matching GPL-3.0 source](https://github.com/chen08209/FlClash/tree/v0.8.93)
- [Source archive](https://github.com/chen08209/FlClash/archive/refs/tags/v0.8.93.tar.gz)
- [Upstream license](https://github.com/chen08209/FlClash/blob/v0.8.93/LICENSE)

The Dockerfile checks the same digest before extracting `FlClashCore`. An
existing file with a different checksum fails validation; it is never executed.
For a manual download on Linux, save the same official package under the local
filename above before building. Do not substitute a different core version
without verifying the IPC interface and updating the expected checksum.
