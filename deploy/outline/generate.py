#!/usr/bin/env python3
import argparse
import ipaddress
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path


CIPHER = "chacha20-ietf-poly1305"
GROUP_NAME = "Keel Outline"
ID_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
HOST_LABEL_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
MAX_INVENTORY_BYTES = 1024 * 1024
MAX_DEVICES = 1000
RESERVED_MIHOMO_LABELS = {
    GROUP_NAME.upper(), "DIRECT", "REJECT", "REJECT-DROP", "PASS", "PASS-RULE", "GLOBAL", "COMPATIBLE",
}


class GenerationError(Exception):
    pass


def _object_without_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise GenerationError("设备清单含有重复 JSON 字段。")
        value[key] = item
    return value


def _server_rule(host):
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        if (
            len(host) > 253
            or any(not HOST_LABEL_PATTERN.fullmatch(label) for label in labels)
            or all(char.isdigit() or char == "." for char in host)
        ):
            raise GenerationError("host 必须是不带协议或路径的有效 IP 地址或 DNS 主机名。")
        return "DOMAIN,{},DIRECT".format(host)
    if address.version == 4:
        return "IP-CIDR,{}/32,DIRECT,no-resolve".format(address)
    return "IP-CIDR6,{}/128,DIRECT,no-resolve".format(address)


def load_inventory(path):
    inventory_path = Path(path)
    try:
        if inventory_path.is_symlink() or not inventory_path.is_file():
            raise GenerationError("设备清单必须是不含符号链接的本地普通文件。")
        if stat.S_IMODE(inventory_path.stat().st_mode) & 0o077:
            raise GenerationError("设备清单权限必须仅允许所有者访问，例如 chmod 600。")
        with inventory_path.open("rb") as source:
            raw = source.read(MAX_INVENTORY_BYTES + 1)
    except GenerationError:
        raise
    except OSError:
        raise GenerationError("无法读取设备清单，请检查路径和权限。")
    if len(raw) > MAX_INVENTORY_BYTES:
        raise GenerationError("设备清单过大。")
    try:
        data = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_object_without_duplicates
        )
    except GenerationError:
        raise
    except (json.JSONDecodeError, UnicodeError):
        raise GenerationError("设备清单必须是有效的 UTF-8 JSON 对象。")
    if not isinstance(data, dict) or set(data) != {"host", "port", "devices"}:
        raise GenerationError("设备清单必须只包含 host、port、devices。")

    host = data["host"]
    port = data["port"]
    devices = data["devices"]
    if not isinstance(host, str) or host != host.strip() or not host.isascii():
        raise GenerationError("host 必须是不带协议或路径的有效 IP 地址或 DNS 主机名。")
    server_rule = _server_rule(host)
    if type(port) is not int or not 1 <= port <= 65535:
        raise GenerationError("port 必须是 1 到 65535 的整数。")
    if not isinstance(devices, list) or not 1 <= len(devices) <= MAX_DEVICES:
        raise GenerationError("devices 必须包含 1 到 1000 个显式设备槽位。")

    normalized = []
    seen_ids = set()
    seen_labels = set()
    seen_secrets = set()
    for index, device in enumerate(devices):
        if not isinstance(device, dict) or set(device) != {"id", "label", "secret"}:
            raise GenerationError("设备槽位 {} 必须只包含 id、label、secret。".format(index + 1))
        device_id = device["id"]
        label = device["label"]
        secret = device["secret"]
        if not isinstance(device_id, str) or not ID_PATTERN.fullmatch(device_id):
            raise GenerationError("设备槽位 {} 的 id 必须是小写 ASCII 字母、数字或连字符。".format(index + 1))
        if device_id in seen_ids:
            raise GenerationError("设备 id 必须唯一。")
        if (
            not isinstance(label, str)
            or not label
            or label != label.strip()
            or len(label) > 80
            or not label.isprintable()
        ):
            raise GenerationError("设备 {} 的 label 必须是可打印的非空短标签。".format(device_id))
        if label.upper() in RESERVED_MIHOMO_LABELS:
            raise GenerationError("设备 {} 的 label 不能使用 Mihomo 保留名称。".format(device_id))
        if label in seen_labels:
            raise GenerationError("设备 label 必须唯一，以便持久保存选择。")
        if (
            not isinstance(secret, str)
            or not secret
            or secret != secret.strip()
            or len(secret) > 1024
            or not secret.isprintable()
        ):
            raise GenerationError("设备 {} 的 secret 必须是非空可打印字符串。".format(device_id))
        if secret in seen_secrets:
            raise GenerationError("每台设备必须使用不同的 secret。")
        seen_ids.add(device_id)
        seen_labels.add(label)
        seen_secrets.add(secret)
        normalized.append({"id": device_id, "label": label, "secret": secret})
    return {"host": host, "port": port, "server_rule": server_rule, "devices": normalized}


def build_configs(inventory):
    listeners = [
        {"type": "tcp", "address": ":{}".format(inventory["port"])},
        {"type": "udp", "address": ":{}".format(inventory["port"])},
    ]
    outline = {
        "services": [{
            "listeners": listeners,
            "keys": [
                {"id": device["id"], "cipher": CIPHER, "secret": device["secret"]}
                for device in inventory["devices"]
            ],
        }]
    }
    names = [device["label"] for device in inventory["devices"]]
    clash = {
        "mixed-port": 7890,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "profile": {"store-selected": True},
        "proxies": [
            {
                "name": device["label"],
                "type": "ss",
                "server": inventory["host"],
                "port": inventory["port"],
                "cipher": CIPHER,
                "password": device["secret"],
                "udp": True,
            }
            for device in inventory["devices"]
        ],
        "proxy-groups": [{"name": GROUP_NAME, "type": "select", "proxies": names}],
        "rules": [inventory["server_rule"], "MATCH,{}".format(GROUP_NAME)],
    }
    return outline, clash


def _json_yaml(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _stage_private(directory, content):
    descriptor, temporary_name = tempfile.mkstemp(prefix=".keel-new-", dir=str(directory))
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
            os.fchmod(target.fileno(), 0o600)
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
    except Exception:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return temporary_path


def _replace_outputs(output, contents):
    names = tuple(contents)
    workspace = output / ".keel-transaction"
    try:
        workspace.mkdir(mode=0o700)
    except FileExistsError:
        raise GenerationError("输出目录内已有生成事务，请等待其完成。若上次运行中断，请检查 .keel-transaction 中保留的原文件。")
    backups = {}
    published = set()
    keep_workspace = False
    try:
        staged = {name: _stage_private(workspace, contents[name]) for name in names}
        for name in names:
            destination = output / name
            try:
                destination.lstat()
            except FileNotFoundError:
                continue
            backup = workspace / (name + ".original")
            os.link(destination, backup, follow_symlinks=False)
            backups[name] = backup
        for name in names:
            os.replace(str(staged[name]), str(output / name))
            published.add(name)
    except Exception as publish_error:
        rollback_errors = []
        for name in reversed(names):
            destination = output / name
            if name not in published:
                continue
            try:
                if name in backups:
                    os.replace(str(backups[name]), str(destination))
                else:
                    destination.unlink()
            except Exception as error:
                rollback_errors.append(error)
        if rollback_errors:
            keep_workspace = True
            raise GenerationError("生成失败且无法完整回滚，请检查输出目录。") from publish_error
        raise
    finally:
        if not keep_workspace:
            for path in workspace.iterdir():
                path.unlink()
            workspace.rmdir()


def generate(inventory_path, output_dir):
    inventory_path = Path(inventory_path)
    inventory = load_inventory(inventory_path)
    outline, clash = build_configs(inventory)
    contents = {"outline.yml": _json_yaml(outline), "clash.yaml": _json_yaml(clash)}
    output = Path(output_dir)
    if output.is_symlink() or (output.exists() and not output.is_dir()):
        raise GenerationError("输出路径必须是普通目录，不能是符号链接。")
    try:
        output.mkdir(mode=0o700, parents=True, exist_ok=True)
        if stat.S_IMODE(output.stat().st_mode) & 0o077:
            raise GenerationError("现有输出目录权限必须仅允许所有者访问，例如 chmod 700。")
        for name in ("outline.yml", "clash.yaml"):
            destination = output / name
            if destination.is_dir() and not destination.is_symlink():
                raise GenerationError("生成输出路径不能是目录。")
            if destination.exists() and inventory_path.samefile(destination):
                raise GenerationError("设备清单不能与生成输出文件相同。")
        _replace_outputs(output, contents)
    except OSError:
        raise GenerationError("无法写入生成目录，请检查路径和权限。")


def build_parser():
    parser = argparse.ArgumentParser(
        description="从私有 JSON 清单生成 Outline v1.9+ 和共享 Clash/Mihomo 配置。"
    )
    parser.add_argument("--inventory", required=True, help="权限为 0600 的本地 JSON 清单")
    parser.add_argument("--output", required=True, help="本地生成目录")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        generate(args.inventory, args.output)
    except GenerationError as error:
        print("错误：{}".format(error), file=sys.stderr)
        return 1
    print("已生成 outline.yml 和 clash.yaml；未打印任何设备 secret。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
