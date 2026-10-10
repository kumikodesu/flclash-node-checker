# FlClash Node Checker

对 FlClash 当前生效配置中的代理节点进行批量测评，生成可排序、筛选和导出的综合评分报告。**下载网速固定占 20%**，IP 质量参考 [IPLark](https://iplark.com/) 公开网页。

支持本机 Docker 网页版和 Windows 桌面版。Docker 版通过启动设置窗口选择 FlClash 数据目录，可选使用 CloakBrowser；没有 CloakBrowser 也能运行。

## 能做什么

- 一键检测当前配置的全部节点，或按配置中的协议筛选，如 `ss`、`anytls`。
- 包含已缓存的 proxy-provider 节点；缓存缺失或节点同名时明确报错，避免悄悄漏测。
- 每个节点显示地区、协议、网速、响应延迟、延迟波动、请求成功率、IPLark 分数和综合分。
- 生成 HTML、CSV、JSON；失败、待补全、停止后未检测的节点全部保留。
- 使用独立 FlClashCore 测试进程；配置目录只读挂载，测评不会切换日常 FlClash 的节点或模式。
- IPLark 有效缓存优先、相同出口 IP 合并查询；验证、拒绝访问和限流会如实显示在报告中。

## 本机 Docker 快速开始（Windows）

需要 Windows x64、Docker Desktop 的 **Linux containers** 模式和已经生成有效配置的 FlClash。宿主机无需安装 Python、Node.js 或 CloakBrowser。首次构建需联网下载官方镜像、Debian 软件包和 FlClash 核心。

1. 下载本仓库 ZIP 并完整解压，或通过 Git 克隆；不要直接在压缩包内启动。
2. 打开 Docker Desktop，等待引擎就绪。
3. 双击 **`start-docker.cmd`**，在设置窗口选择 **FlClash 数据目录**。该目录必须包含当前生效的 `config.yaml`，默认候选为 `%APPDATA%\com.follow\clash`；不是 FlClash 程序安装目录。
4. 按需勾选 **使用 CloakBrowser**，点击 **保存并启动**。
5. 浏览器打开 <http://127.0.0.1:8080>，刷新协议列表并点击一键检测。`all` 表示当前配置的全部协议、全部节点。

设置保存到**测评程序目录**的 `launcher-settings.json`，下次双击会载入上次选择，仍可修改。取消窗口不会启动或保存。移动 FlClash 安装位置不影响 Docker 自带的核心；数据目录移动后，在此窗口重新选择即可。切换订阅或协议后刷新列表，每次测评重新读取当前配置，运行中的一批使用开始时的快照。

### 浏览器选择

| 选择 | 行为 |
| --- | --- |
| 不勾选（默认） | 使用检测镜像中的普通 Chromium，无需 CloakBrowser，也不会自动连接已有 CDP 服务 |
| 勾选 | 优先复用本机 `127.0.0.1:9222` 的 CDP 浏览器；未提供服务时启动 Compose 可选的官方 CloakBrowser 容器 |

启用时只创建独立浏览器上下文，结束后清理本批上下文。已有 CDP 浏览器可能是其他兼容浏览器，程序不能仅凭端口保证它就是 CloakBrowser。若端口被不兼容程序占用，会明确报错；可以关闭冲突程序或改选 Chromium。取消勾选只改变测评所用浏览器，不会关闭你已有的服务。

CloakBrowser 的部署参考[上游 Docker 文档](https://github.com/CloakHQ/CloakBrowser#docker)，可选镜像由上游维护。它**不能保证网站永远不拦截**。程序不自动回答人工验证码，不反复重试绕过限流。

### 输出与流量

Docker 报告在 `docker-data/results/<时间戳>/`，每批包含：

- `report.html`：搜索、筛选、排序，对比完整评分节点。
- `results.csv`：适合 Excel，含地区、失败原因、IPLark 来源与查询时间。
- `results.json`：完整样本、评分分项、缓存时间、验证或限流状态。

默认每节点两次下载、每次最多 8 MiB，**最大测速下载量约为节点数 × 16 MiB**，另有少量探测和浏览器请求。慢节点达到采样限时后可能用实际下载量计速。运行时可以停止，报告保留全部选中节点。

报告含真实节点名称和出口 IP，请作为本地文件保管，不要直接提交到公共仓库。

### 命令行启动

跳过设置窗口但保存明确提供的目录和浏览器模式：

```powershell
.\start-docker.cmd -ConfigPath 'D:\FlClashData\config.yaml' -BrowserMode chromium -NoSettings
```

可将 `chromium` 改为 `cloakbrowser`。`-PrepareOnly` 仅保存设置并验证 Compose，`-NoOpen` 启动服务但不打开浏览器。

停止检测服务：

```powershell
docker compose --env-file docker.env stop checker
```

仅当可选浏览器由本项目部署时，使用下面命令停止它：

```powershell
docker compose --env-file docker.env --profile cloakbrowser stop cloakbrowser
```

### 其他电脑 / 手动 Compose

每台电脑需要自己的 FlClash 生效配置。在新电脑解压项目并运行 `start-docker.cmd`，重新选择那台电脑的数据目录；无需复制旧电脑的 `docker.env` 或设置文件。测速反映**运行 Docker 的电脑网络经容器后的表现**，远程服务器的结果不能代表本机网速。

Windows 启动器会自动下载并校验缺失的核心资源。手动构建前先运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\prepare-assets.ps1
```

在其他系统，可按 [docker-assets/README.md](docker-assets/README.md) 下载同一官方 `.deb`，验证 SHA-256 并保存为 `docker-assets/flclash-amd64.deb`。仅支持 Linux amd64 核心；ARM 主机需要 Docker 提供 amd64 模拟。

手动创建本地 `docker.env`，路径使用自己的绝对数据目录，空 CDP 表示普通 Chromium：

```dotenv
FLCLASH_DIR='/absolute/path/to/FlClashData'
IPLARK_CDP_URL=''
```

创建 `docker-data` 目录后启动：

```sh
mkdir -p docker-data
docker compose --env-file docker.env up --build -d checker
```

原生 Linux 的绑定目录需要对容器 UID 10001 可写，并让配置对该用户可读；不要给配置开放不必要的写权限。Windows Docker Desktop 通常处理挂载权限。Linux 手动使用可选浏览器时运行 `docker compose --env-file docker.env --profile cloakbrowser up -d --wait cloakbrowser`，设置 `IPLARK_CDP_URL='http://cloakbrowser:9222'` 后重新启动 checker。Windows 启动器为复用宿主机服务使用 `host.docker.internal`。

## 评分规则

固定权重与绝对尺度，便于不同批次比较；不会因为本批只有慢节点而把最快者自动打到满分。

| 维度 | 权重 | 测量方式 |
| --- | ---: | --- |
| 下载网速 | **20%** | 两次 Cloudflare 下载的 Mbps 中位数，100 Mbps 对应单项满分，使用对数尺度；下载失败降低该分项 |
| IP 质量 | 40% | IPLark 页面显示的 0–100 分；同节点多个出口取最低已知分，缺失任何出口分数则综合分待补全 |
| 请求成功率 | 20% | Cloudflare、Google、Microsoft 和出口 IP 探测请求的成功比例 |
| 响应延迟 | 10% | 同一 Cloudflare 端点五次响应耗时的中位数，包含请求连接与响应时间 |
| 延迟波动 | 5% | 相邻成功 Cloudflare 探测的耗时变化中位数，失败会打断样本序列 |
| 站点连通性 | 5% | Cloudflare、Google、Microsoft 三个站点的可达比例 |

具体公式在 `checker.py:score_node`。HTTP 请求失败率不等同于 ICMP 丢包率，下载是当前路线的短时采样，不是运营商带宽承诺。IPLark 质量和线路性能分开评分；这套权重属于本项目，不是 IPLark 官方综合排名。

没有有效 IP 分数时保留“性能部分分 / 60”，**不编造 IP 分数，也不重新归一化权重**。最高综合分只从完整评分节点中选；连接失败、待补全和未检测节点不会被丢弃。地区优先使用 IPLark 出口地区，缺失时只对明确地区代码作名称推断，并标明来源。

## IPLark 缓存和验证

- 仅复用同一 IP、查询时间不超过 24 小时且分数有效的缓存；报告保留原查询时间和 `cached` 来源。
- 同一批按出口 IP 去重，实时查询至少间隔 10 秒。
- 持续人工验证、HTTP 403 或 HTTP 429 会停止本批后续实时查询，保存至少 10 分钟冷却；更长的 `Retry-After` 优先。
- 重启保留冷却记录。报告显示拦截类型、触发 IP、时间和最早恢复时间；无有效缓存的节点标为待补全。
- 页面不提供可核实的分数或地区时保持未知。网站结构可能变化，程序会报告读取失败。

网站公开页面查询包含被测出口 IP，浏览器走测评机器的正常网络路线。详见 [SECURITY.md](SECURITY.md)。

## Windows 桌面版（可选）

需要 Python 3.10+（含 Tkinter）、本机 FlClash / FlClashCore、Microsoft Edge；实时 IPLark 查询还需 Node.js 22+。`vendor/` 已含 requests、PyYAML 等纯 Python 依赖，完整解压即可，不需要全局安装。

双击 `start.cmd` 会打开桌面窗口并默认测评全部节点；首次无法识别路径时在窗口选择当前 `config.yaml` 和 FlClash 程序。移动安装后重新打开 FlClash，或明确选择新路径。

桌面版自动探测已运行的本机 CDP 服务，缺失时使用独立 Edge，**不会启动 Docker 浏览器容器**。Docker 启动设置窗口中的浏览器选择仅作用于 Docker 版。

```powershell
.\start.cmd --headless --no-open --config 'D:\FlClashData\config.yaml' --core 'C:\Program Files\FlClash\FlClashCore.exe' --protocol all
```

`--limit 1 --download-mib 1` 可用于低流量启动验证，正式全量运行不要加 `--limit`。桌面输出在 `results/`。

## 常见问题

**黑色窗口报错或闪退**：Docker 版检查 `docker-startup.log`，桌面版检查 `startup.log`。启动脚本失败时会保留窗口；请先删除分享副本中的个人路径和凭据再反馈。

**找不到配置 / 代理集合缓存**：在 FlClash 中启用目标配置并更新集合，选择包含生效 `config.yaml` 及相关缓存的目录。挂载外的 Windows 绝对引用路径不会被自动扩大访问范围，请把依赖放入数据目录。

**Docker 引擎不可用**：打开 Docker Desktop，确认 Linux containers，先确保 `docker info` 正常。程序不会重置 Docker 数据。

**下载失败**：确认能访问官方 GitHub 发布资源、Docker 镜像源和 Debian 软件源。已有核心包只有通过固定 SHA-256 校验才会被复用，不使用来源不明的替代二进制。

**CloakBrowser 启动失败**：检查端口 9222、Docker 日志及网络，或取消勾选后使用 Chromium。选项启用时失败会明确报错，避免悄悄切换浏览器。

**提示已有测评运行中**：先在网页停止本批或等待完成，再修改启动设置。启动器会保护正在运行的测评，避免重新启动容器中断检测。

**测评很慢 / IPLark 被拦截**：全部节点串行测试，失败有超时，网站查询有节流和冷却；等待冷却或下次使用有效缓存。不要反复清除状态文件触发重试。

## 开发、隐私与许可

测试和贡献说明见 [CONTRIBUTING.md](CONTRIBUTING.md)。CI 使用合成数据和模拟网络，不访问真实节点。配置、设置、报告、缓存、日志、下载的核心二进制和本地工具均默认被 `.gitignore` 排除；提交前仍需检查 `git diff --cached`，不要使用 `git add -f` 添加私人数据。

原创测评程序与启动脚本采用 [MIT](LICENSE) 许可。FlClash 核心及随附依赖保留各自许可，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。本项目为独立工具，与 FlClash、IPLark 和 CloakBrowser 官方无隶属关系。
