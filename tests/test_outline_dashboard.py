import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_PATH = ROOT / "deploy" / "outline" / "grafana-dashboard.json"


class OutlineDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
        cls.panels = cls.dashboard["panels"]
        cls.visual_panels = [panel for panel in cls.panels if panel["type"] != "row"]
        cls.targets = [
            target
            for panel in cls.visual_panels
            for target in panel.get("targets", [])
        ]
        cls.expressions = [target["expr"] for target in cls.targets]
        cls.traffic_targets = [
            target
            for panel in cls.visual_panels
            if panel["title"] in {"设备上下行速率", "所选时段的设备流量"}
            for target in panel.get("targets", [])
        ]

    def test_identity_datasource_and_device_filter(self):
        self.assertEqual(self.dashboard["uid"], "keel-outline")
        variables = self.dashboard["templating"]["list"]
        self.assertEqual(len(variables), 1)
        device = variables[0]
        self.assertEqual(device["name"], "device")
        self.assertEqual(device["type"], "query")
        self.assertTrue(device["multi"])
        self.assertTrue(device["includeAll"])
        self.assertEqual(device["allValue"], ".+")
        self.assertEqual(device["datasource"], {"type": "prometheus", "uid": "keel-prom"})
        query = device["query"]["query"]
        self.assertEqual(device["definition"], query)
        self.assertTrue(query.startswith("query_result("))
        self.assertIn("sum by (access_key) (increase(shadowsocks_data_bytes{", query)
        self.assertIn('access_key!=""', query)
        self.assertIn('dir=~"^(c>p|c<p)$"', query)
        self.assertIn('proto=~"^(tcp|udp)$"', query)
        self.assertIn(" or sum by (access_key) (increase(keel_tcp_download_bytes_total{", query)
        self.assertEqual(query.count("[$__range] @ ${__to:date:seconds}"), 4)
        self.assertTrue(query.endswith("> 0)"))
        self.assertEqual(device["regex"], r'/.*access_key="([^"]+)".*/')
        self.assertEqual(device["refresh"], 2)
        self.assertEqual(self.dashboard["title"], "Keel · 新加坡 VPS")
        self.assertEqual(len(self.dashboard["links"]), 1)
        link = self.dashboard["links"][0]
        self.assertEqual(link["title"], "Node Exporter Full")
        self.assertEqual(link["type"], "link")
        self.assertEqual(link["url"], "/d/rYdddlPWk/node-exporter-full")
        self.assertTrue(link["keepTime"])
        self.assertFalse(link["targetBlank"])
        self.assertTrue({1, 2, 3, 4, 5, 6, 7}.issubset({panel["id"] for panel in self.visual_panels}))
        for panel in self.visual_panels:
            self.assertEqual(panel["datasource"], {"type": "prometheus", "uid": "keel-prom"})
        for target in self.targets:
            self.assertEqual(target["datasource"], {"type": "prometheus", "uid": "keel-prom"})

    def test_client_side_traffic_has_direction_protocol_and_no_double_count(self):
        self.assertEqual(len(self.traffic_targets), 2)
        for target in self.traffic_targets:
            expression = target["expr"]
            self.assertIn('access_key=~"$device"', expression)
            self.assertIn('dir=~"^(c>p|c<p)$"', expression)
            self.assertIn('proto=~"^(tcp|udp)$"', expression)
            self.assertIn("sum by (access_key, dir, proto)", expression)
            self.assertIn("[$__range] @ ${__to:date:seconds}", expression)
            self.assertIn("> 0", expression)
            self.assertNotIn("p<t", expression)
            self.assertNotIn("p>t", expression)
            self.assertNotIn("server", expression)
            for label in ("{{access_key}}", "{{dir}}", "{{proto}}"):
                self.assertIn(label, target["legendFormat"])

        rate_panel = next(panel for panel in self.panels if panel["title"] == "设备上下行速率")
        volume_panel = next(panel for panel in self.panels if panel["title"] == "所选时段的设备流量")
        rate = rate_panel["targets"][0]["expr"]
        volume = volume_panel["targets"][0]["expr"]
        self.assertIn("rate(shadowsocks_data_bytes{", rate)
        self.assertIn("[$__rate_interval]", rate)
        self.assertEqual(rate.count("shadowsocks_data_bytes"), 2)
        self.assertIn("and on(access_key, dir, proto)", rate)
        self.assertEqual(rate.count('access_key=~"$device"'), 2)
        self.assertEqual(volume.count("shadowsocks_data_bytes"), 1)
        self.assertTrue(volume.endswith("> 0"))
        self.assertNotIn("and on(", volume)
        self.assertEqual(rate_panel["fieldConfig"]["defaults"]["unit"], "Bps")
        self.assertEqual(volume_panel["fieldConfig"]["defaults"]["unit"], "bytes")

    def test_device_interval_details_use_only_selected_nonempty_keys(self):
        tunnel_panel = next(
            panel for panel in self.visual_panels
            if any("shadowsocks_tunnel_time_seconds" in target["expr"] for target in panel.get("targets", []))
        )
        self.assertEqual(tunnel_panel["fieldConfig"]["defaults"]["unit"], "s")
        self.assertEqual(len(tunnel_panel["targets"]), 1)
        tunnel = tunnel_panel["targets"][0]
        self.assertIn("increase(shadowsocks_tunnel_time_seconds{", tunnel["expr"])
        self.assertIn('access_key=~"$device"', tunnel["expr"])
        self.assertIn('access_key!=""', tunnel["expr"])
        self.assertIn("[$__range]", tunnel["expr"])
        self.assertIn("sum by (access_key)", tunnel["expr"])
        self.assertIn("来源 IP", tunnel_panel["description"])
        self.assertIn("不是物理设备", tunnel_panel["description"])

        closed_panel = next(
            panel for panel in self.visual_panels
            if any("shadowsocks_tcp_connections_closed" in target["expr"] for target in panel.get("targets", []))
        )
        self.assertEqual(closed_panel["fieldConfig"]["defaults"]["unit"], "short")
        self.assertEqual(len(closed_panel["targets"]), 1)
        closed = closed_panel["targets"][0]
        self.assertIn("increase(shadowsocks_tcp_connections_closed{", closed["expr"])
        self.assertIn('access_key=~"$device"', closed["expr"])
        self.assertIn('access_key!=""', closed["expr"])
        self.assertIn("[$__range]", closed["expr"])
        self.assertIn("sum by (access_key, status)", closed["expr"])
        for target in (closed, tunnel):
            expression = target["expr"]
            self.assertTrue(target["instant"])
            self.assertFalse(target["range"])
            self.assertIn("and on(access_key)", expression)
            self.assertIn("increase(shadowsocks_data_bytes{", expression)
            self.assertIn("or (sum by (access_key) (increase(keel_tcp_download_bytes_total{", expression)
            self.assertIn('dir=~"^(c>p|c<p)$"', expression)
            self.assertIn('proto=~"^(tcp|udp)$"', expression)
            self.assertEqual(expression.count("[$__range] @ ${__to:date:seconds}"), 3)
            self.assertIn("> 0)", expression)
            self.assertEqual(expression.count('access_key=~"$device"'), 3)
        self.assertIn("{{status}}", closed["legendFormat"])
        self.assertIn("{{access_key}}", closed["legendFormat"])

    def test_host_core_inventory_load_pressure_and_oom(self):
        panels = {panel["title"]: panel for panel in self.visual_panels}

        uptime = panels["主机运行时长"]
        self.assertEqual(uptime["fieldConfig"]["defaults"]["unit"], "s")
        self.assertIn("node_time_seconds", uptime["targets"][0]["expr"])
        self.assertIn("node_boot_time_seconds", uptime["targets"][0]["expr"])

        vcpus = panels["可见 vCPU"]
        self.assertEqual(vcpus["fieldConfig"]["defaults"]["unit"], "short")
        self.assertIn("count by (instance)", vcpus["targets"][0]["expr"])
        self.assertIn('node_cpu_seconds_total{mode="idle"}', vcpus["targets"][0]["expr"])

        memory = panels["可见内存"]
        self.assertEqual(memory["fieldConfig"]["defaults"]["unit"], "bytes")
        self.assertIn("node_memory_MemTotal_bytes", memory["targets"][0]["expr"])

        root_available = panels["根分区可用空间"]
        self.assertEqual(root_available["fieldConfig"]["defaults"]["unit"], "bytes")
        self.assertIn('mountpoint="/"', root_available["targets"][0]["expr"])

        load = panels["每 vCPU 负载"]
        self.assertEqual(load["fieldConfig"]["defaults"]["unit"], "short")
        self.assertEqual(len(load["targets"]), 3)
        for number, target in zip((1, 5, 15), load["targets"]):
            self.assertIn(f"node_load{number} / on(instance)", target["expr"])
            self.assertIn("count by (instance)", target["expr"])
            self.assertIn('node_cpu_seconds_total{mode="idle"}', target["expr"])
        self.assertIn("不可中断等待", load["description"])

        cpu_modes = panels["CPU 模式"]
        mode_expression = " ".join(target["expr"] for target in cpu_modes["targets"])
        self.assertEqual(cpu_modes["fieldConfig"]["defaults"]["unit"], "percent")
        self.assertIn("avg by (instance, mode)", mode_expression)
        self.assertIn("iowait", mode_expression)
        self.assertIn("steal", mode_expression)

        available = panels["内存可用量"]
        self.assertEqual(available["fieldConfig"]["defaults"]["unit"], "bytes")
        self.assertIn("node_memory_MemAvailable_bytes", available["targets"][0]["expr"])

        psi = panels["PSI 资源等待"]
        self.assertEqual(psi["fieldConfig"]["defaults"]["unit"], "percent")
        psi_expression = " ".join(target["expr"] for target in psi["targets"])
        for metric in (
            "node_pressure_cpu_waiting_seconds_total",
            "node_pressure_memory_waiting_seconds_total",
            "node_pressure_io_waiting_seconds_total",
        ):
            self.assertIn(f"rate({metric}[$__rate_interval])", psi_expression)

        oom = panels["OOM 终止"]
        self.assertEqual(oom["fieldConfig"]["defaults"]["unit"], "short")
        self.assertIn("increase(node_vmstat_oom_kill[$__rate_interval])", oom["targets"][0]["expr"])

        inodes = panels["根分区 inode 可用"]
        self.assertEqual(inodes["fieldConfig"]["defaults"]["unit"], "percent")
        self.assertIn("node_filesystem_files_free", inodes["targets"][0]["expr"])
        self.assertIn("node_filesystem_files{", inodes["targets"][0]["expr"])

    def test_disk_io_latency_and_generic_device_exclusions(self):
        panels = {panel["title"]: panel for panel in self.visual_panels}

        throughput = panels["磁盘读写吞吐"]
        self.assertEqual(throughput["fieldConfig"]["defaults"]["unit"], "Bps")
        throughput_expression = " ".join(target["expr"] for target in throughput["targets"])
        self.assertIn("rate(node_disk_read_bytes_total{", throughput_expression)
        self.assertIn("rate(node_disk_written_bytes_total{", throughput_expression)

        iops = panels["磁盘 IOPS"]
        self.assertEqual(iops["fieldConfig"]["defaults"]["unit"], "iops")
        iops_expression = " ".join(target["expr"] for target in iops["targets"])
        self.assertIn("rate(node_disk_reads_completed_total{", iops_expression)
        self.assertIn("rate(node_disk_writes_completed_total{", iops_expression)

        latency = panels["磁盘平均读写延迟"]
        self.assertEqual(latency["fieldConfig"]["defaults"]["unit"], "ms")
        self.assertEqual(len(latency["targets"]), 2)
        latency_expression = " ".join(target["expr"] for target in latency["targets"])
        for operation in ("read", "write"):
            time_metric = f"node_disk_{operation}_time_seconds_total"
            operation_metric = f"node_disk_{operation}s_completed_total"
            self.assertIn(f"1000 * rate({time_metric}{{", latency_expression)
            self.assertIn(f"rate({operation_metric}{{", latency_expression)
        for target in latency["targets"]:
            self.assertIn("> 0)", target["expr"])
        self.assertIn("操作为零", latency["description"])

        busy = panels["磁盘繁忙度"]
        self.assertEqual(busy["fieldConfig"]["defaults"]["unit"], "percent")
        self.assertIn("rate(node_disk_io_time_seconds_total{", busy["targets"][0]["expr"])

        for title in ("磁盘读写吞吐", "磁盘 IOPS", "磁盘平均读写延迟", "磁盘繁忙度"):
            for target in panels[title]["targets"]:
                expression = target["expr"]
                self.assertIn('device!~"', expression)
                for excluded in ("loop", "ram", "dm-", "md", "zram", "nvme"):
                    self.assertIn(excluded, expression)
                self.assertNotIn('device="sda"', expression)
                self.assertNotIn('device="ens4"', expression)

    def test_network_errors_tcp_retransmits_and_socket_states(self):
        panels = {panel["title"]: panel for panel in self.visual_panels}
        errors = panels["网卡错误与丢包"]
        self.assertEqual(errors["fieldConfig"]["defaults"]["unit"], "pps")
        self.assertEqual(len(errors["targets"]), 4)
        error_expression = " ".join(target["expr"] for target in errors["targets"])
        for metric in (
            "node_network_receive_errs_total",
            "node_network_transmit_errs_total",
            "node_network_receive_drop_total",
            "node_network_transmit_drop_total",
        ):
            self.assertIn(f"rate({metric}{{", error_expression)
        for title in ("VPS 网卡收发速率", "网卡错误与丢包"):
            for target in panels[title]["targets"]:
                self.assertIn('device!="lo"', target["expr"])

        retransmits = panels["TCP 重传"]
        self.assertEqual(retransmits["fieldConfig"]["defaults"]["unit"], "pps")
        self.assertIn("rate(node_netstat_Tcp_RetransSegs[$__rate_interval])", retransmits["targets"][0]["expr"])

        sockets = panels["TCP 套接字状态"]
        self.assertEqual(sockets["fieldConfig"]["defaults"]["unit"], "short")
        socket_expression = " ".join(target["expr"] for target in sockets["targets"])
        self.assertIn("node_sockstat_TCP_inuse", socket_expression)
        self.assertIn("node_sockstat_TCP_tw", socket_expression)

    def test_protocol_health_panels(self):
        panels = {panel["title"]: panel for panel in self.visual_panels}

        retrans_percent = panels["TCP 重传占比"]
        self.assertEqual(retrans_percent["fieldConfig"]["defaults"]["unit"], "percent")
        self.assertEqual(
            retrans_percent["targets"][0]["expr"],
            "100 * rate(node_netstat_Tcp_RetransSegs[$__rate_interval]) / "
            "(rate(node_netstat_Tcp_OutSegs[$__rate_interval]) > 0)",
        )
        self.assertIn("避免除以零", retrans_percent["description"])

        expected = {
            "TCP SYN 重传与超时": (
                "node_netstat_TcpExt_TCPSynRetrans",
                "node_netstat_TcpExt_TCPTimeouts",
            ),
            "TCP 监听溢出与丢弃": (
                "node_netstat_TcpExt_ListenOverflows",
                "node_netstat_TcpExt_ListenDrops",
            ),
            "UDP 数据报与缓冲错误": (
                "node_netstat_Udp_InDatagrams",
                "node_netstat_Udp_OutDatagrams",
                "node_netstat_Udp_RcvbufErrors",
                "node_netstat_Udp_SndbufErrors",
            ),
            "Outline UDP NAT 增删": (
                "shadowsocks_udp_nat_entries_added",
                "shadowsocks_udp_nat_entries_removed",
            ),
        }
        for title, metrics in expected.items():
            panel = panels[title]
            self.assertEqual(len(panel["targets"]), len(metrics))
            for target, metric in zip(panel["targets"], metrics):
                self.assertEqual(target["expr"], f"rate({metric}[$__rate_interval])")
                self.assertNotIn("$device", target["expr"])

    def test_tcp_leg_diagnostics_use_live_socket_metrics(self):
        row_index = next(
            index for index, panel in enumerate(self.panels)
            if panel["type"] == "row" and panel["title"] == "TCP 链路诊断"
        )
        self.assertGreater(row_index, 0)
        self.assertEqual(self.panels[row_index - 1]["title"], "所选时段 TCP 关闭连接")
        self.assertEqual(self.panels[row_index + 5]["title"], "主机概览")

        panels = self.panels[row_index + 1:row_index + 5]
        self.assertEqual(
            [panel["title"] for panel in panels],
            ["双链路实时 RTT", "双链路下载速率", "设备链路重传字节速率", "设备链路未发送字节"],
        )
        for panel in panels:
            self.assertEqual(panel["type"], "timeseries")
            self.assertEqual(len(panel["targets"]), 1)
            self.assertNotIn("timeFrom", panel)
            self.assertNotIn("timeShift", panel)
            self.assertNotIn("hideTimeOverride", panel)
            custom = panel["fieldConfig"]["defaults"]["custom"]
            self.assertFalse(custom["spanNulls"])
            self.assertFalse(custom["insertNulls"])
            target = panel["targets"][0]
            self.assertTrue(target["range"])
            self.assertFalse(target.get("instant", False))
            expression = target["expr"]
            self.assertIn('access_key=~"$device"', expression)
            self.assertIn('access_key!=""', expression)
            self.assertIn("and on(access_key)", expression)
            self.assertIn("increase(keel_tcp_download_bytes_total{", expression)
            self.assertIn("[$__range] @ ${__to:date:seconds}", expression)
            self.assertIn("max_over_time(keel_tcp_device_notsent_bytes{", expression)
            self.assertIn("increase(keel_tcp_device_retrans_bytes_total{", expression)
            self.assertNotIn("dir=", expression)
            self.assertNotIn("proto=", expression)
            self.assertNotIn("source_ip", expression)
            self.assertNotIn("src_ip", expression)
            self.assertNotIn("client_ip", expression)

        rtt, download, retrans, notsent = panels
        rtt_expression = rtt["targets"][0]["expr"]
        self.assertIn("sum by (access_key, leg) (keel_tcp_rtt_seconds_sum{", rtt_expression)
        self.assertIn(") / (sum by (access_key, leg) (keel_tcp_rtt_samples{", rtt_expression)
        self.assertIn("}) > 0)", rtt_expression)
        self.assertNotIn("rate(keel_tcp_rtt", rtt_expression)
        self.assertEqual(rtt["fieldConfig"]["defaults"]["unit"], "s")
        self.assertIn("当前打开", rtt["description"])
        self.assertIn("平均", rtt["description"])

        download_expression = download["targets"][0]["expr"]
        self.assertIn(
            "sum by (access_key, leg) (rate(keel_tcp_download_bytes_total{",
            download_expression,
        )
        self.assertIn("[$__rate_interval]", download_expression)
        self.assertEqual(download["fieldConfig"]["defaults"]["unit"], "Bps")
        self.assertIn("源站", download["description"])
        self.assertIn("ACK", download["description"])
        self.assertIn("加密", download["description"])

        retrans_expression = retrans["targets"][0]["expr"]
        self.assertIn(
            "sum by (access_key) (rate(keel_tcp_device_retrans_bytes_total{",
            retrans_expression,
        )
        self.assertIn("[$__rate_interval]", retrans_expression)
        self.assertNotIn("leg", retrans_expression)
        self.assertEqual(retrans["fieldConfig"]["defaults"]["unit"], "Bps")
        self.assertIn("发往客户端", retrans["description"])
        self.assertIn("重传", retrans["description"])

        notsent_expression = notsent["targets"][0]["expr"]
        self.assertIn("sum by (access_key) (keel_tcp_device_notsent_bytes{", notsent_expression)
        self.assertNotIn("rate(keel_tcp_device_notsent_bytes", notsent_expression)
        self.assertNotIn("leg", notsent_expression)
        self.assertEqual(notsent["fieldConfig"]["defaults"]["unit"], "bytes")
        self.assertIn("尚未发送", notsent["description"])
        self.assertIn("发往客户端", notsent["description"])

        for panel in (rtt, download):
            expression = panel["targets"][0]["expr"]
            self.assertIn('leg=~"^(source|device)$"', expression)
            self.assertEqual(panel["targets"][0]["legendFormat"], "{{access_key}} · {{leg}}")
        for panel in (retrans, notsent):
            self.assertEqual(panel["targets"][0]["legendFormat"], "{{access_key}}")

    def test_native_rows_and_grid_do_not_overlap(self):
        self.assertEqual(
            [panel["title"] for panel in self.panels if panel["type"] == "row"],
            ["设备与密钥", "TCP 链路诊断", "主机概览", "压力与磁盘", "网络与 TCP", "协议健康"],
        )
        self.assertEqual(len({panel["id"] for panel in self.panels}), len(self.panels))
        for panel in self.visual_panels:
            self.assertNotIn("timeFrom", panel)
            self.assertNotIn("timeShift", panel)
            self.assertNotIn("hideTimeOverride", panel)
        occupied = []
        for panel in self.panels:
            position = panel["gridPos"]
            self.assertGreater(position["h"], 0)
            self.assertGreater(position["w"], 0)
            self.assertGreaterEqual(position["x"], 0)
            self.assertLessEqual(position["x"] + position["w"], 24)
            rectangle = (
                position["x"], position["y"],
                position["x"] + position["w"], position["y"] + position["h"],
            )
            for other in occupied:
                self.assertTrue(
                    rectangle[2] <= other[0] or other[2] <= rectangle[0]
                    or rectangle[3] <= other[1] or other[3] <= rectangle[1],
                    f'{panel["title"]} overlaps another panel',
                )
            occupied.append(rectangle)

    def test_host_queries_are_independent_of_device_selection(self):
        for target in self.targets:
            if "node_" in target["expr"] or target["expr"] == "up":
                self.assertNotIn("$device", target["expr"])
                self.assertNotIn("access_key", target["expr"])

    def test_availability_and_node_exporter_panels_do_not_invent_data(self):
        self.assertIn("up", self.expressions)
        required_metrics = {
            "node_cpu_seconds_total",
            "node_memory_MemAvailable_bytes",
            "node_memory_MemTotal_bytes",
            "node_filesystem_avail_bytes",
            "node_filesystem_size_bytes",
            "node_network_receive_bytes_total",
            "node_network_transmit_bytes_total",
        }
        for metric in required_metrics:
            self.assertTrue(any(metric in expression for expression in self.expressions), metric)
        for expression in self.expressions:
            self.assertNotIn("or vector", expression)
            self.assertNotIn("absent(", expression)
            self.assertNotIn("job=", expression)
        network_panel = next(panel for panel in self.panels if panel["title"] == "VPS 网卡收发速率")
        self.assertEqual(network_panel["fieldConfig"]["defaults"]["unit"], "Bps")
        self.assertEqual(len(network_panel["targets"]), 2)


if __name__ == "__main__":
    unittest.main()
