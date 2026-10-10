# FlClash Node Checker

对 FlClash 当前生效配置中的代理节点进行批量测评，生成可排序、筛选和导出的综合评分报告。下载网速固定占 20%，IP 质量参考 [IPLark](https://iplark.com/) 公开网页。

通过Docker Compose启动，在浏览器中操作。可选启用 CloakBrowser；默认使用镜像内的 Chromium，无需另行安装浏览器、Python 或 Node.js。

## 能做什么

- 一键检测当前配置的全部节点，或按配置中的协议筛选，如 `ss`、`anytls`。
- 包含已缓存的 proxy-provider 节点；缓存缺失或节点同名时明确报错，避免漏测。
- 每个节点显示地区、协议、网速、响应延迟、延迟波动、请求成功率、IPLark 分数和综合分。
- 生成 HTML、CSV、JSON；失败、待补全、停止后未检测的节点全部保留。
- 使用独立 FlClashCore 测试进程，配置目录只读挂载；测评不会切换日常 FlClash 的节点或模式。
- IPLark 有效缓存优先，相同出口 IP 合并查询；验证、拒绝访问和限流会显示在报告中。

## 快速开始

需要 Docker、Compose v2 和 Linux 容器，以及 FlClash 已生成的生效配置。镜像使用 Linux amd64 核心；ARM 主机需要支持 amd64 模拟。首次构建需联网下载官方镜像、Debian 软件包和 FlClash 核心，核心包会自动下载并校验 SHA-256。

1. 克隆仓库或完整解压源码，进入项目目录，直接运行：

   ```sh
   docker compose up --build -d
   ```

2. 打开 [测评网页](http://127.0.0.1:8080)，点击 **选择配置文件**，选择 FlClash 当前生效的 YAML 配置。如果配置使用代理集合或外部证书，点击 **选择数据文件夹**，选择包含 `config.yaml` 和依赖文件的整个数据目录。
3. 选择协议，点击一键测评。`all` 表示导入配置中的全部协议、全部节点。

**默认流程无需 `.env`、宿主机路径或手动下载核心。** 没有配置时网页仍会正常启动并提示导入。导入会验证全部节点及集合缓存；文件缺失、同名节点或格式错误会明确提示，并保留上次有效配置。

导入只复制必要的节点数据到 Docker 持久卷，不修改原文件。文件夹内的数据库、日志及其他无关文件不会导入。导入配置会保留到下次启动；它是一次快照，切换 FlClash 配置后需要重新导入。

### 可选：自动读取 FlClash 生效配置

标准数据目录可通过附加 Compose 文件直接读取，网页打开后自动显示协议和节点：

```sh
docker compose -f compose.yaml -f compose.flclash.yaml up --build -d
```

此模式优先使用现有 `.env` 中的 `FLCLASH_DIR`；没有设置时，从 `APPDATA` 自动定位 `com.follow/clash` 标准数据目录。因此使用标准目录的 Windows 安装不需要查找路径或编辑配置。自定义目录或其他系统可在项目 `.env` 设置数据目录绝对路径：

```dotenv
FLCLASH_DIR='/absolute/path/to/FlClashData'
```

Docker 无法直接枚举宿主机进程；此功能自动读取生效配置，不根据文件存在断言 FlClash 正在运行。若找不到目录，改用默认启动命令和网页导入。自动读取模式下切换配置只需刷新协议列表，每批测评重新读取。手动导入会在当前服务运行期间切换为导入快照；重新启动自动读取模式后再次优先读取挂载配置。

要同时使用自动读取和 CloakBrowser：

```sh
docker compose -f compose.yaml -f compose.flclash.yaml -f compose.cloakbrowser.yaml up --build -d
```

自动目录只读挂载，需允许容器用户 UID 10001 读取。移动 FlClash 程序安装位置不影响 Docker 内核心；自定义数据目录移动后更新 `.env`。停止、日志等命令使用与启动时相同的 `-f` 参数。

测速反映运行 Docker 的网络环境。更新、重建或停止容器前，应在网页停止测评并等状态变为结束，避免中断当前批次。

### 可选：启用 CloakBrowser

CloakBrowser相关地址：https://github.com/CloakHQ/CloakBrowser

使用附加 Compose 文件，同时启动官方 CloakBrowser 容器：

```sh
docker compose -f compose.yaml -f compose.cloakbrowser.yaml up --build -d
```

测评服务会等待浏览器健康，再连接 Compose 网络内的 CDP 服务。无需提前安装 CloakBrowser；其控制端口不发布到宿主机。参考[上游 Docker 文档](https://github.com/CloakHQ/CloakBrowser#docker)。

启用此模式后，查看状态、日志或停止时也使用这两个 `-f` 参数，例如：

```sh
docker compose -f compose.yaml -f compose.cloakbrowser.yaml ps
docker compose -f compose.yaml -f compose.cloakbrowser.yaml logs --tail 100
docker compose -f compose.yaml -f compose.cloakbrowser.yaml down
```

切回 Chromium：先用上面的 `down` 停止该组合，再运行 `docker compose up --build -d`。程序仅清理自己的浏览器上下文。CloakBrowser 不能保证网站永远不拦截；程序不自动回答人工验证码。

### 状态、日志与停止

默认 Chromium 模式使用：

```sh
docker compose ps
docker compose logs --tail 100 checker
docker compose down
```

`down` 保留报告和缓存。不要使用 `down -v`，它会删除持久卷中的数据。

### 输出与流量

报告和缓存保存在 Docker 持久卷 `checker-data` 中，不需要手动创建可写目录。每批报告包含：

- `report.html`：搜索、筛选、排序，对比完整评分节点。
- `results.csv`：适合 Excel，含地区、失败原因、IPLark 来源与查询时间。
- `results.json`：完整样本、评分分项、缓存时间、验证或限流状态。

在网页查看报告并下载 CSV / JSON，或将全部报告复制到项目目录：

```sh
docker compose cp checker:/data/results ./results
```

使用 CloakBrowser 时，导出命令也加上相同的两个 `-f` 参数。请在 `down` 前导出；`down` 后再次启动即可继续访问卷内数据。旧版本的 `docker-data/` 不会自动导入新卷，升级前应保留该目录作为备份。

默认每节点两次下载、每次最多 8 MiB，**最大测速下载量约为节点数 × 16 MiB**，另有少量探测和浏览器请求。慢节点达到采样限时后可能用实际下载量计速。运行时可以停止，报告保留全部选中节点。

报告含真实节点名称和出口 IP，请作为私人文件保管，不要提交到公共仓库。

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

## 常见问题

**找不到配置或代理集合缓存**：在 FlClash 中启用目标配置并更新集合，再选择数据文件夹导入。普通订阅文件不一定包含集合缓存。自动读取模式下，检查挂载目录是否包含生效 `config.yaml` 和引用文件。

**Docker 引擎不可用**：先确保 `docker info` 正常，并确认使用 Linux 容器。

**构建下载失败**：确认能访问 GitHub 发布资源、Docker 镜像源和 Debian 软件源；不需要运行下载脚本。

**CloakBrowser 启动失败**：查看上述组合的 Docker 日志，或停止该组合后使用默认 Chromium 命令。

**测评很慢或 IPLark 被拦截**：所有节点串行测试，失败有超时，网站查询有节流和冷却。报告会提示拦截原因；等待冷却或使用有效缓存，不要反复清除状态触发重试。

## 开发与许可

测试和贡献说明见 [CONTRIBUTING.md](CONTRIBUTING.md)。CI 使用合成数据和模拟网络，不访问真实节点。

原创测评程序采用 [MIT](LICENSE) 许可。FlClash 核心及随附依赖保留各自许可，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。本项目为独立工具，与 FlClash、IPLark 和 CloakBrowser 官方无隶属关系。
