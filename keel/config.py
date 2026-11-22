import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .errors import ConfigError


NAME_PATTERN = re.compile(r"[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?")
PROJECT_PATTERN = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
DOMAIN_PROJECT_PATTERN = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?:"
    r"[a-z][a-z0-9-]{4,28}[a-z0-9]"
)


@dataclass(frozen=True)
class Config:
    project: str
    zone: str
    instance: str
    traffic_proxy_port: Optional[int] = None


def default_config_path():
    return Path(__file__).resolve().parent.parent / "vps.local.json"


def load_config(path=None):
    config_path = Path(path) if path is not None else default_config_path()
    try:
        raw = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError("缺少 vps.local.json，请复制 vps.example.json 并填写实例配置。")
    except UnicodeError:
        raise ConfigError("vps.local.json 不是有效的 UTF-8 JSON 文件。")
    except OSError:
        raise ConfigError("无法读取 vps.local.json，请检查文件权限。")

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError):
        raise ConfigError("vps.local.json 不是有效的 UTF-8 JSON 文件。")
    if not isinstance(data, dict):
        raise ConfigError("vps.local.json 必须是 JSON 对象。")

    unknown = set(data) - {"project", "zone", "instance", "trafficProxyPort"}
    if unknown:
        raise ConfigError("vps.local.json 含有不支持的配置项。")

    values = {}
    for field in ("project", "zone", "instance"):
        value = data.get(field)
        if not isinstance(value, str) or not value or value != value.strip():
            raise ConfigError("配置 project、zone、instance 必须是非空且首尾无空白的字符串。")
        if len(value) > 255 or any(ord(char) < 32 for char in value):
            raise ConfigError("配置值过长或含有控制字符。")
        if field == "project":
            valid = PROJECT_PATTERN.fullmatch(value) or DOMAIN_PROJECT_PATTERN.fullmatch(value)
        else:
            valid = NAME_PATTERN.fullmatch(value)
        if not valid:
            raise ConfigError("配置 {} 不是有效的 GCP 标识符。".format(field))
        values[field] = value
    port = data.get("trafficProxyPort")
    if port is not None and (type(port) is not int or not 1 <= port <= 65535):
        raise ConfigError("trafficProxyPort 必须是 1 到 65535 的本机 HTTP 代理端口。")
    return Config(**values, traffic_proxy_port=port)
