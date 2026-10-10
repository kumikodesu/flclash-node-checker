"""Validate browser-selected files and save only a self-contained node snapshot."""
# 网页导入边界：输入仅为浏览器提交的相对文件名和文本，不访问原始宿主机路径。
# 将代理集合缓存与证书合并为自包含 YAML，供测评核心使用；省去代理集合的订阅 URL 和控制端口密钥。
# 文件路径、数量、字节数、节点名称和依赖是否完整，都在保存快照之前检查。
import copy
from pathlib import PurePosixPath, PureWindowsPath

import yaml

# 限制按 UTF-8 编码后的文本字节数计算，而非文件名长度或 Python 字符数量。
MAX_IMPORT_BYTES = 24 * 1024 * 1024


# 安全解析文本并要求 YAML 顶层是映射。
# 隐藏解析器的原始报错行，避免错误信息中带出节点密码、订阅地址等内容。
def read_yaml(text):
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        raise ValueError("配置或集合缓存不是有效的 YAML 文件。") from None
    if not isinstance(data, dict):
        raise ValueError("配置或集合缓存内容为空或格式不正确。")
    return data


# 输入为 [{path, content}, ...]，返回 (自包含 YAML 文本, 节点总数)。
# 所有依赖都从这次上传包中查找，验证成功后才允许调用方保存。
def bundle_config(entries):
    if not isinstance(entries, list) or not 1 <= len(entries) <= 1000:
        raise ValueError("请选择配置文件，或包含配置和集合缓存的数据文件夹（最多 1000 个文件）。")
    # 先在内存中检查整个上传包；此函数不写文件，因此失败不会影响已保存的配置。
    files = {}
    size = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("导入文件格式不正确。")
        name, content = entry.get("path"), entry.get("content")
        if not isinstance(name, str) or not isinstance(content, str):
            raise ValueError("导入文件格式不正确。")
        # 浏览器上传统一使用相对 POSIX 路径；拒绝盘符、绝对路径、反斜杠和 ..，避免路径跳转。
        path = PurePosixPath(name)
        if (not name or "\\" in name or path.is_absolute() or ":" in name
                or any(part in ("", ".", "..") for part in name.split("/"))):
            raise ValueError("导入文件路径不正确。")
        # 按大小写不敏感规则检测重复，避免 Windows 目录语义与容器 Linux 语义不一致。
        if name.casefold() in {key.casefold() for key in files}:
            raise ValueError("导入文件包含重复路径。")
        size += len(content.encode("utf-8"))
        if size > MAX_IMPORT_BYTES:
            raise ValueError("导入内容超过 24 MiB，请只选择配置和相关集合缓存。")
        files[name] = content
    # 单文件允许任意 YAML 文件名；文件夹优先根目录 config.yaml，否则必须存在唯一同名候选。
    if len(files) == 1:
        config_name = next(iter(files))
    elif "config.yaml" in files:
        config_name = "config.yaml"
    else:
        candidates = [name for name in files if PurePosixPath(name).name == "config.yaml"]
        if len(candidates) != 1:
            raise ValueError("未找到唯一的生效 config.yaml，请选择正确的数据文件夹。")
        config_name = candidates[0]
    original = read_yaml(files[config_name])

    # 按相对路径或唯一的最长路径后缀匹配上传文件。
    # 浏览器不提供原始绝对目录，因此 Windows / Unix 绝对引用不能直接用于读宿主机文件；存在歧义就拒绝。
    def referenced_file(value):
        # Browser file selection does not disclose the original absolute directory.
        # Resolve absolute references by the longest unambiguous uploaded path suffix.
        normalized = value.replace("\\", "/")
        relative = str(PurePosixPath(config_name).parent / normalized)
        exact = [name for name in files if name.casefold() in (normalized.casefold(), relative.casefold())]
        if len(exact) == 1:
            return files[exact[0]]
        if not (PureWindowsPath(value).is_absolute() or PurePosixPath(normalized).is_absolute()):
            raise ValueError("配置引用的文件缺失，请选择包含集合缓存或证书的数据文件夹。")
        parts = PurePosixPath(normalized).parts
        # 由长到短尝试路径后缀，保留更多目录信息；同一后缀出现多个候选就报错，不擅自选一个。
        for count in range(len(parts), 0, -1):
            suffix = "/".join(parts[-count:]).casefold()
            matches = [name for name in files if name.casefold() == suffix or name.casefold().endswith("/" + suffix)]
            if len(matches) > 1:
                raise ValueError("配置引用的文件存在多个候选，请导入包含完整相对目录的文件夹。")
            if matches:
                return files[matches[0]]
        raise ValueError("配置引用的文件缺失，请选择包含集合缓存或证书的数据文件夹。")

    # 将内联与 provider 节点合并成独立列表，后续证书内联不修改原始解析结果。
    nodes = copy.deepcopy(original.get("proxies") or [])
    providers = original.get("proxy-providers") or {}
    if not isinstance(nodes, list) or not isinstance(providers, dict):
        raise ValueError("代理节点或集合格式不正确。")
    for provider in providers.values():
        if not isinstance(provider, dict):
            raise ValueError("代理集合格式不正确。")
        # inline 直接取 payload；其他集合只用上传的缓存，不向原订阅 URL 发请求。
        if provider.get("type") == "inline":
            extra = provider.get("payload") or []
        else:
            path = provider.get("path")
            if not isinstance(path, str) or not path:
                raise ValueError("代理集合没有缓存路径，请在 FlClash 更新集合后导入数据文件夹。")
            cached = read_yaml(referenced_file(path))
            if not isinstance(cached.get("proxies"), list):
                raise ValueError("代理集合缓存缺少 proxies 列表，请选择正确的数据文件夹。")
            extra = cached["proxies"]
        if not isinstance(extra, list):
            raise ValueError("代理集合缓存格式不正确。")
        nodes.extend(copy.deepcopy(extra))
    if not nodes or any(not isinstance(node, dict) or not isinstance(node.get("name"), str)
                        or not node["name"] or not isinstance(node.get("type"), str)
                        or not node["type"] for node in nodes):
        raise ValueError("配置中没有有效代理节点，或节点缺少名称和协议。")
    # 核心按名称选择代理，同名节点即使协议不同也无法可靠区分，必须显式拒绝。
    if len({node["name"] for node in nodes}) != len(nodes):
        raise ValueError("存在同名节点，请先为重复节点设置不同名称。")
    # 把外部证书正文嵌入快照，使导入后不再依赖宿主机的绝对路径。
    # WireGuard 的 private-key 是内联密钥，与 TLS 证书私钥文件不是同一语义。
    for node in nodes:
        for key in ("ca", "certificate", "private-key"):
            value = node.get(key)
            if key == "private-key" and node["type"].lower() == "wireguard":
                continue  # WireGuard keys are inline values, not certificate files.
            if isinstance(value, str) and value and "\n" not in value:
                node[key] = referenced_file(value)
    # 只保留独立核心实际使用的字段；代理集合已展开，因此省去其 URL 和控制端口 / secret。
    config = {key: original[key] for key in ("proxy-groups", "ipv6", "hosts",
              "global-client-fingerprint", "tcp-concurrent") if key in original}
    config["proxies"] = nodes
    return yaml.safe_dump(config, allow_unicode=True), len(nodes)
