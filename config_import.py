"""Validate browser-selected files and save only a self-contained node snapshot."""
import copy
from pathlib import PurePosixPath, PureWindowsPath

import yaml

MAX_IMPORT_BYTES = 24 * 1024 * 1024


def read_yaml(text):
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        raise ValueError("配置或集合缓存不是有效的 YAML 文件。") from None
    if not isinstance(data, dict):
        raise ValueError("配置或集合缓存内容为空或格式不正确。")
    return data


def bundle_config(entries):
    if not isinstance(entries, list) or not 1 <= len(entries) <= 1000:
        raise ValueError("请选择配置文件，或包含配置和集合缓存的数据文件夹（最多 1000 个文件）。")
    files = {}
    size = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("导入文件格式不正确。")
        name, content = entry.get("path"), entry.get("content")
        if not isinstance(name, str) or not isinstance(content, str):
            raise ValueError("导入文件格式不正确。")
        path = PurePosixPath(name)
        if (not name or "\\" in name or path.is_absolute() or ":" in name
                or any(part in ("", ".", "..") for part in name.split("/"))):
            raise ValueError("导入文件路径不正确。")
        if name.casefold() in {key.casefold() for key in files}:
            raise ValueError("导入文件包含重复路径。")
        size += len(content.encode("utf-8"))
        if size > MAX_IMPORT_BYTES:
            raise ValueError("导入内容超过 24 MiB，请只选择配置和相关集合缓存。")
        files[name] = content
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
        for count in range(len(parts), 0, -1):
            suffix = "/".join(parts[-count:]).casefold()
            matches = [name for name in files if name.casefold() == suffix or name.casefold().endswith("/" + suffix)]
            if len(matches) > 1:
                raise ValueError("配置引用的文件存在多个候选，请导入包含完整相对目录的文件夹。")
            if matches:
                return files[matches[0]]
        raise ValueError("配置引用的文件缺失，请选择包含集合缓存或证书的数据文件夹。")

    nodes = copy.deepcopy(original.get("proxies") or [])
    providers = original.get("proxy-providers") or {}
    if not isinstance(nodes, list) or not isinstance(providers, dict):
        raise ValueError("代理节点或集合格式不正确。")
    for provider in providers.values():
        if not isinstance(provider, dict):
            raise ValueError("代理集合格式不正确。")
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
    if len({node["name"] for node in nodes}) != len(nodes):
        raise ValueError("存在同名节点，请先为重复节点设置不同名称。")
    for node in nodes:
        for key in ("ca", "certificate", "private-key"):
            value = node.get(key)
            if key == "private-key" and node["type"].lower() == "wireguard":
                continue  # WireGuard keys are inline values, not certificate files.
            if isinstance(value, str) and value and "\n" not in value:
                node[key] = referenced_file(value)
    config = {key: original[key] for key in ("proxy-groups", "ipv6", "hosts",
              "global-client-fingerprint", "tcp-concurrent") if key in original}
    config["proxies"] = nodes
    return yaml.safe_dump(config, allow_unicode=True), len(nodes)
