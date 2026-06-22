"""Tests for import conflict detection and clone adjustments."""

from __future__ import annotations

import unittest

from docker_migrate.conflicts import (
    apply_clone_to_manifest,
    build_clone_config,
    format_conflict_summary,
    parse_resolution_keyword,
)
from docker_migrate.conflicts import _find_free_port, _parse_host_ports_from_ps, _volume_clone_name


SAMPLE_MANIFEST = {
    "container": {
        "host_config": {
            "port_bindings": {
                "80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}],
                "443/tcp": [{"HostIp": "", "HostPort": "8443"}],
            }
        },
        "mounts": [
            {"Type": "volume", "Name": "mydata", "Destination": "/data"},
        ],
    },
    "volumes": [
        {"name": "mydata", "archive": "volumes/mydata.tar.gz"},
    ],
}


class ParseResolutionKeywordTests(unittest.TestCase):
    def test_japanese_keywords(self) -> None:
        self.assertEqual(parse_resolution_keyword("上書き"), "overwrite")
        self.assertEqual(parse_resolution_keyword("複製"), "clone")

    def test_english_keywords(self) -> None:
        self.assertEqual(parse_resolution_keyword("overwrite"), "overwrite")
        self.assertEqual(parse_resolution_keyword("clone"), "clone")

    def test_invalid_keyword(self) -> None:
        self.assertIsNone(parse_resolution_keyword("yes"))
        self.assertIsNone(parse_resolution_keyword("y"))


class PortParsingTests(unittest.TestCase):
    def test_parse_docker_ps_ports(self) -> None:
        field = "0.0.0.0:8080->80/tcp, [::]:8081->80/tcp"
        self.assertEqual(_parse_host_ports_from_ps(field), {8080, 8081})

    def test_find_free_port_with_offset(self) -> None:
        used = {8080, 8081}
        self.assertEqual(_find_free_port(8080, used, offset=1000), 9080)

    def test_find_free_port_auto_increment(self) -> None:
        used = {8080, 8081}
        self.assertEqual(_find_free_port(8080, used), 8082)


class CloneConfigTests(unittest.TestCase):
    def test_volume_rename(self) -> None:
        self.assertEqual(_volume_clone_name("mydata", "-dev"), "mydata_dev")
        self.assertEqual(_volume_clone_name("mydata", "_prod"), "mydata_prod")

    def test_build_clone_config_renames_volumes_and_ports(self) -> None:
        clone = build_clone_config(
            SAMPLE_MANIFEST,
            original_name="my-app",
            container_name="my-app-dev",
            clone_suffix="-dev",
            port_offset=1000,
            used_ports={8080, 8081, 8443},
        )
        self.assertEqual(clone.container_name, "my-app-dev")
        self.assertEqual(clone.volume_map["mydata"], "mydata_dev")
        self.assertEqual(clone.port_map["8080"], "9080")
        self.assertEqual(clone.port_map["8443"], "9443")
        self.assertEqual(clone.bind_root_name, "restored-bind-mounts-dev")

    def test_apply_clone_to_manifest(self) -> None:
        clone = build_clone_config(
            SAMPLE_MANIFEST,
            original_name="my-app",
            container_name="my-app-dev",
            clone_suffix="-dev",
            port_offset=0,
            used_ports={8080, 8443},
        )
        adjusted = apply_clone_to_manifest(SAMPLE_MANIFEST, clone)
        bindings = adjusted["container"]["host_config"]["port_bindings"]["80/tcp"][0]
        self.assertEqual(bindings["HostPort"], clone.port_map["8080"])
        self.assertEqual(adjusted["volumes"][0]["name"], "mydata_dev")
        self.assertEqual(adjusted["container"]["mounts"][0]["Name"], "mydata_dev")


class ConflictSummaryTests(unittest.TestCase):
    def test_format_conflict_summary(self) -> None:
        from docker_migrate.conflicts import ConflictReport

        report = ConflictReport(
            container_name="my-app",
            container_exists=True,
            port_conflicts=[("8080", "other")],
            existing_volumes=["mydata"],
            bind_mount_conflicts=["/tmp/data"],
        )
        text = format_conflict_summary(report)
        self.assertIn("コンテナ 'my-app' が存在", text)
        self.assertIn("ポート 8080", text)
        self.assertIn("ボリューム 'mydata'", text)
        self.assertIn("バインドマウント", text)


if __name__ == "__main__":
    unittest.main()
