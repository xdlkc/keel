import json
import os
import subprocess

from .errors import AuthError, GcloudError


DESCRIBE_TIMEOUT = 20
TOKEN_TIMEOUT = 10
ACTION_TIMEOUT = 180


class Gcloud:
    def _run(self, argv, timeout, message, auth=False):
        env = os.environ.copy()
        env.update({
            "CLOUDSDK_CORE_SHOULD_PROMPT_TO_ENABLE_API": "false",
            "CLOUDSDK_CORE_DISABLE_PROMPTS": "true",
        })
        try:
            result = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            raise GcloudError("找不到 gcloud，请先安装并初始化 Google Cloud CLI。")
        except subprocess.TimeoutExpired:
            raise GcloudError("gcloud 命令超时，未确认操作完成。")
        except OSError:
            raise GcloudError("无法启动 gcloud 命令。")
        if result.returncode != 0:
            error_type = AuthError if auth else GcloudError
            raise error_type(message)
        return result

    def describe(self, config):
        result = self._run(
            [
                "gcloud", "compute", "instances", "describe", config.instance,
                "--project", config.project,
                "--zone", config.zone,
                "--format", "json",
            ],
            DESCRIBE_TIMEOUT,
            "无法读取实例，请检查登录状态、项目、可用区、实例名和权限。",
        )
        try:
            data = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            raise GcloudError("gcloud 返回了无法识别的实例数据。")
        if not isinstance(data, dict):
            raise GcloudError("gcloud 返回了无法识别的实例数据。")
        return data

    def access_token(self):
        result = self._run(
            ["gcloud", "auth", "print-access-token"],
            TOKEN_TIMEOUT,
            "未登录或访问令牌不可用，请运行 gcloud auth login 后重试。",
            auth=True,
        )
        token = result.stdout.strip()
        if not token or any(char.isspace() for char in token):
            raise AuthError("未获取到有效访问令牌，请运行 gcloud auth login 后重试。")
        return token

    def start(self, config):
        self._run(
            [
                "gcloud", "compute", "instances", "start", config.instance,
                "--project", config.project,
                "--zone", config.zone,
                "--quiet",
            ],
            ACTION_TIMEOUT,
            "gcloud 启动命令失败，未报告成功；请检查实例状态和权限。",
        )

    def stop(self, config):
        self._run(
            [
                "gcloud", "compute", "instances", "stop", config.instance,
                "--project", config.project,
                "--zone", config.zone,
                "--quiet",
            ],
            ACTION_TIMEOUT,
            "gcloud 停止命令失败，未报告成功；请检查实例状态和权限。",
        )


def instance_view(config, data):
    disks = data.get("disks") if isinstance(data.get("disks"), list) else []
    boot_disk = next(
        (disk for disk in disks if isinstance(disk, dict) and disk.get("boot") is True),
        {},
    )
    public_ip = None
    interfaces = data.get("networkInterfaces")
    if isinstance(interfaces, list):
        for interface in interfaces:
            if not isinstance(interface, dict):
                continue
            access_configs = interface.get("accessConfigs")
            if not isinstance(access_configs, list):
                continue
            for access_config in access_configs:
                if isinstance(access_config, dict) and access_config.get("natIP"):
                    public_ip = str(access_config["natIP"])
                    break
            if public_ip:
                break

    machine_type = data.get("machineType")
    if isinstance(machine_type, str):
        machine_type = machine_type.rstrip("/").rsplit("/", 1)[-1]
    else:
        machine_type = None

    disk_size = boot_disk.get("diskSizeGb")
    return {
        "id": str(data["id"]) if data.get("id") is not None else None,
        "name": str(data.get("name") or config.instance),
        "status": str(data.get("status") or "UNKNOWN"),
        "zone": config.zone,
        "machineType": machine_type,
        "bootDiskSizeGb": str(disk_size) if disk_size is not None else None,
        "publicIp": public_ip,
    }
