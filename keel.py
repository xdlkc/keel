#!/usr/bin/env python3
import argparse
import sys

from keel.actions import InstanceActions
from keel.config import load_config
from keel.errors import KeelError
from keel.gcloud import Gcloud, instance_view
from keel.metrics import MonitoringClient


def build_parser():
    parser = argparse.ArgumentParser(
        prog="keel",
        description="Keel：一台 Google Compute Engine 实例的本地控制台。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="读取实例状态")
    subparsers.add_parser("metrics", help="读取最近一小时的 CPU 和出站网络指标")

    serve = subparsers.add_parser("serve", help="启动仅本机访问的 Web 控制台")
    serve.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    serve.add_argument(
        "--traffic-forward-port", type=int, default=None, metavar="PORT",
        help="可选：将 /api/traffic 固定转发到 127.0.0.1:PORT",
    )

    for command in ("start", "stop"):
        action = subparsers.add_parser(command, help="{}实例".format("启动" if command == "start" else "停止"))
        action.add_argument(
            "--confirm",
            required=True,
            metavar="INSTANCE",
            help="必须完整填写已配置的实例名",
        )
    return parser


def _print_status(view):
    print("实例：{}".format(view["name"]))
    print("状态：{}".format(view["status"]))
    print("可用区：{}".format(view["zone"]))
    print("机器类型：{}".format(view["machineType"] or "未知"))
    print("启动磁盘：{}".format(
        "{} GB".format(view["bootDiskSizeGb"]) if view["bootDiskSizeGb"] else "未知"
    ))
    print("公网 IP：{}".format(view["publicIp"] or "无"))


def _print_metrics(result):
    if result["empty"]:
        print("最近一小时没有指标数据。")
        return
    print("时间（UTC）\tCPU (%)\t出站网络 (Mbit/s)")
    cpu = {point["timestamp"]: point["value"] for point in result["cpu"]}
    network = {point["timestamp"]: point["value"] for point in result["network"]}
    for timestamp in sorted(set(cpu) | set(network)):
        cpu_value = "{:.2f}".format(cpu[timestamp]) if timestamp in cpu else "-"
        network_value = (
            "{:.3f}".format(network[timestamp]) if timestamp in network else "-"
        )
        print("{}\t{}\t{}".format(timestamp, cpu_value, network_value))


def main(argv=None, cloud_factory=Gcloud):
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        if not 1 <= args.port <= 65535:
            print("错误：端口必须在 1 到 65535 之间。", file=sys.stderr)
            return 2
        if args.traffic_forward_port is not None and not 1 <= args.traffic_forward_port <= 65535:
            print("错误：设备流量转发端口必须在 1 到 65535 之间。", file=sys.stderr)
            return 2
        from keel.server import serve
        serve(args.port, traffic_forward_port=args.traffic_forward_port)
        return 0

    try:
        config = load_config()
        cloud = cloud_factory()
        if args.command == "status":
            _print_status(instance_view(config, cloud.describe(config)))
        elif args.command == "metrics":
            instance = instance_view(config, cloud.describe(config))
            if not instance["id"] or not instance["id"].isdigit():
                raise KeelError("实例数据缺少有效的数字 ID，无法查询指标。")
            client = MonitoringClient(cloud.access_token)
            _print_metrics(client.collect(config, instance["id"]))
        elif args.command in ("start", "stop"):
            actions = InstanceActions(config, cloud)
            if args.command == "start":
                actions.start(args.confirm)
                print("gcloud 启动命令已成功结束；实际状态请用 python3 keel.py status 确认。")
            else:
                actions.stop(args.confirm)
                print("gcloud 停止命令已成功结束；实际状态请用 python3 keel.py status 确认。")
        return 0
    except KeelError as error:
        print("错误：{}".format(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        sys.exit(130)
