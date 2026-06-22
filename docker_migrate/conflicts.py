"""Conflict detection and clone-mode adjustments for bundle import."""

from __future__ import annotations

import copy
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .utils import MigrateError, log, run_docker, sanitize_filename

PORT_MAPPING_RE = re.compile(r":(\d+)->")

OVERWRITE_KEYWORDS = frozenset({"上書き", "overwrite"})
CLONE_KEYWORDS = frozenset({"複製", "clone"})


@dataclass
class ConflictReport:
    """Structured summary of import-time conflicts."""

    container_name: str
    container_exists: bool = False
    container_id: str | None = None
    port_conflicts: list[tuple[str, str]] = field(default_factory=list)
    existing_volumes: list[str] = field(default_factory=list)
    bind_mount_conflicts: list[str] = field(default_factory=list)

    @property
    def has_conflicts(self) -> bool:
        return (
            self.container_exists
            or bool(self.port_conflicts)
            or bool(self.existing_volumes)
            or bool(self.bind_mount_conflicts)
        )


@dataclass
class CloneConfig:
    """Resolved naming and port adjustments for clone import."""

    container_name: str
    suffix: str
    port_offset: int = 0
    volume_map: dict[str, str] = field(default_factory=dict)
    bind_mount_suffix: str = ""
    bind_root_name: str = "restored-bind-mounts"
    port_map: dict[str, str] = field(default_factory=dict)


def parse_resolution_keyword(text: str) -> str | None:
    normalized = text.strip().lower()
    if normalized in OVERWRITE_KEYWORDS or text.strip() in OVERWRITE_KEYWORDS:
        return "overwrite"
    if normalized in CLONE_KEYWORDS or text.strip() in CLONE_KEYWORDS:
        return "clone"
    return None


def format_conflict_summary(report: ConflictReport) -> str:
    lines = ["競合が検出されました:"]
    if report.container_exists:
        lines.append(f"- コンテナ '{report.container_name}' が存在")
    for host_port, owner in report.port_conflicts:
        lines.append(f"- ポート {host_port} が使用中 ({owner})")
    for vname in report.existing_volumes:
        lines.append(f"- ボリューム '{vname}' が存在")
    for path in report.bind_mount_conflicts:
        lines.append(f"- バインドマウント '{path}' が競合")
    return "\n".join(lines)


def prompt_conflict_resolution(report: ConflictReport) -> str:
    """Ask user to type a keyword (not Y/n) to choose overwrite or clone."""
    print(format_conflict_summary(report))
    print()
    print("上書きする場合は「上書き」、複製する場合は「複製」と入力:")
    while True:
        try:
            value = input("> ").strip()
        except EOFError:
            print()
            raise KeyboardInterrupt from None
        mode = parse_resolution_keyword(value)
        if mode:
            return mode
        print("「上書き」または「複製」（overwrite / clone も可）を入力してください。")


def prompt_clone_options(original_name: str, default_suffix: str = "-dev") -> tuple[str, str]:
    """Return (container_name, suffix) for clone mode."""
    try:
        suffix_raw = input(f"複製サフィックス (Enter で {default_suffix}): ").strip()
    except EOFError:
        print()
        raise KeyboardInterrupt from None
    suffix = suffix_raw or default_suffix
    default_name = f"{original_name}{suffix}"
    try:
        name_raw = input(f"コンテナ名 (Enter で {default_name}): ").strip()
    except EOFError:
        print()
        raise KeyboardInterrupt from None
    return (name_raw or default_name, suffix)


def _container_id_by_name(name: str) -> str | None:
    result = run_docker(
        ["ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.ID}}"],
        capture=True,
        check=False,
    )
    cid = result.stdout.strip().splitlines()
    return cid[0] if cid else None


def _parse_host_ports_from_ps(ports_field: str) -> set[int]:
    return {int(match.group(1)) for match in PORT_MAPPING_RE.finditer(ports_field or "")}


def get_used_host_ports(*, exclude_container_id: str | None = None) -> dict[int, str]:
    """Map host port -> owning container name."""
    result = run_docker(
        ["ps", "-a", "--format", "{{.ID}}\t{{.Names}}\t{{.Ports}}"],
        capture=True,
        check=False,
    )
    used: dict[int, str] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        cid, cname, ports_field = parts
        if exclude_container_id and cid.startswith(exclude_container_id):
            continue
        for port in _parse_host_ports_from_ps(ports_field):
            used.setdefault(port, cname)
    return used


def _volume_exists(name: str) -> bool:
    result = run_docker(["volume", "inspect", name], capture=True, check=False)
    return result.returncode == 0


def _iter_manifest_host_ports(manifest: dict[str, Any]) -> list[tuple[str, str]]:
    hc = (manifest.get("container") or {}).get("host_config") or {}
    ports: list[tuple[str, str]] = []
    for port_key, bindings in (hc.get("port_bindings") or {}).items():
        container_port = port_key.split("/")[0]
        for binding in bindings or []:
            host_port = binding.get("HostPort") or ""
            if host_port:
                ports.append((host_port, container_port))
    return ports


def _bind_mount_conflict_paths(bundle_dir: Path, manifest: dict[str, Any]) -> list[str]:
    conflicts: list[str] = []
    bind_root = bundle_dir / "restored-bind-mounts"
    for bind in manifest.get("bind_mounts") or []:
        dest = bind.get("destination")
        if not dest:
            continue
        hint = sanitize_filename(dest.strip("/").replace("/", "_"))
        target = bind_root / hint
        if target.exists() and any(target.iterdir()):
            conflicts.append(str(target))
        source = bind.get("source")
        if source:
            source_path = Path(source)
            if source_path.exists():
                conflicts.append(source)
    return conflicts


def detect_conflicts(
    bundle_dir: Path,
    manifest: dict[str, Any],
    container_name: str,
) -> ConflictReport:
    """Detect container, port, volume, and bind-mount conflicts before import."""
    report = ConflictReport(container_name=container_name)
    report.container_id = _container_id_by_name(container_name)
    report.container_exists = report.container_id is not None

    used_ports = get_used_host_ports(exclude_container_id=report.container_id)
    for host_port, _container_port in _iter_manifest_host_ports(manifest):
        try:
            port_num = int(host_port)
        except ValueError:
            continue
        owner = used_ports.get(port_num)
        if owner:
            report.port_conflicts.append((host_port, owner))

    for vol in manifest.get("volumes") or []:
        if vol.get("error"):
            continue
        vname = vol.get("name")
        if vname and _volume_exists(vname):
            report.existing_volumes.append(vname)

    report.bind_mount_conflicts = _bind_mount_conflict_paths(bundle_dir, manifest)
    return report


def _sanitize_suffix(suffix: str) -> str:
    cleaned = suffix.strip()
    if not cleaned:
        return "_clone"
    if cleaned[0] in "-_":
        return cleaned
    return f"_{cleaned}"


def _volume_clone_name(original: str, suffix: str) -> str:
    safe_suffix = _sanitize_suffix(suffix).lstrip("-").replace("-", "_")
    if not safe_suffix.startswith("_"):
        safe_suffix = f"_{safe_suffix}"
    return f"{original}{safe_suffix}"


def _find_free_port(start: int, used: set[int], *, offset: int = 0) -> int:
    if offset:
        candidate = start + offset
    else:
        candidate = start
    while candidate in used or candidate <= 0 or candidate > 65535:
        candidate += 1
    return candidate


def build_clone_config(
    manifest: dict[str, Any],
    *,
    original_name: str,
    container_name: str | None = None,
    clone_suffix: str | None = None,
    port_offset: int = 0,
    used_ports: set[int] | None = None,
) -> CloneConfig:
    suffix = clone_suffix if clone_suffix is not None else "-dev"
    resolved_name = container_name or f"{original_name}{suffix}"
    bind_suffix = _sanitize_suffix(suffix)
    bind_root_name = f"restored-bind-mounts{bind_suffix}"

    volume_map: dict[str, str] = {}
    for vol in manifest.get("volumes") or []:
        if vol.get("error"):
            continue
        vname = vol.get("name")
        if vname:
            volume_map[vname] = _volume_clone_name(vname, suffix)

    if used_ports is None:
        used_ports = set(get_used_host_ports().keys())
    port_map: dict[str, str] = {}
    for host_port, _container_port in _iter_manifest_host_ports(manifest):
        try:
            start = int(host_port)
        except ValueError:
            continue
        new_port = _find_free_port(start, used_ports, offset=port_offset)
        used_ports.add(new_port)
        port_map[host_port] = str(new_port)

    return CloneConfig(
        container_name=resolved_name,
        suffix=suffix,
        port_offset=port_offset,
        volume_map=volume_map,
        bind_mount_suffix=bind_suffix,
        bind_root_name=bind_root_name,
        port_map=port_map,
    )


def apply_clone_to_manifest(manifest: dict[str, Any], clone: CloneConfig) -> dict[str, Any]:
    """Return a deep copy of manifest with clone-specific name/port/volume remapping."""
    adjusted = copy.deepcopy(manifest)

    for vol in adjusted.get("volumes") or []:
        orig = vol.get("name")
        if orig and orig in clone.volume_map:
            vol["name"] = clone.volume_map[orig]

    meta = adjusted.setdefault("container", {})
    mounts = meta.get("mounts") or []
    for mount in mounts:
        if mount.get("Type") == "volume":
            orig = mount.get("Name")
            if orig and orig in clone.volume_map:
                mount["Name"] = clone.volume_map[orig]

    hc = meta.setdefault("host_config", {})
    port_bindings = hc.get("port_bindings") or {}
    new_bindings: dict[str, Any] = {}
    for port_key, bindings in port_bindings.items():
        new_entries = []
        for binding in bindings or []:
            entry = dict(binding)
            host_port = entry.get("HostPort")
            if host_port and host_port in clone.port_map:
                entry["HostPort"] = clone.port_map[host_port]
                log(
                    f"  ポート {host_port} -> {clone.port_map[host_port]} "
                    f"(複製モード)"
                )
            new_entries.append(entry)
        new_bindings[port_key] = new_entries
    hc["port_bindings"] = new_bindings

    return adjusted


def resolve_import_mode(
    report: ConflictReport,
    *,
    mode: str | None,
    force: bool,
    interactive: bool | None = None,
) -> str:
    """Return 'overwrite' or 'clone'. Raises MigrateError when unresolved."""
    if force:
        return "overwrite"
    if mode in ("overwrite", "clone"):
        return mode
    if mode is not None:
        raise MigrateError(f"未知の --mode: {mode}（overwrite または clone を指定）")

    if not report.has_conflicts:
        return "overwrite"

    is_tty = interactive if interactive is not None else (sys.stdin.isatty() and sys.stdout.isatty())
    if is_tty:
        return prompt_conflict_resolution(report)

    summary = format_conflict_summary(report)
    raise MigrateError(
        f"{summary}\n"
        "非対話モードでは --mode overwrite または --mode clone を指定してください。"
    )
