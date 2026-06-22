"""Export a Docker container into a migration bundle."""

from __future__ import annotations

import json
import os
import platform
import shutil
from pathlib import Path
from typing import Any

from .utils import (
    IMAGE_ARCHIVE_NAME,
    MigrateError,
    copy_tree_if_exists,
    dir_size,
    human_size,
    inspect_container,
    log,
    resolve_container,
    run_docker,
    sanitize_filename,
    save_image_archive,
    tar_directory,
    utc_timestamp,
    write_json,
)


BUNDLE_VERSION = "1.0"


def _extract_metadata(inspect_data: dict[str, Any]) -> dict[str, Any]:
    config = inspect_data.get("Config") or {}
    host = inspect_data.get("HostConfig") or {}
    network = inspect_data.get("NetworkSettings") or {}

    return {
        "container_id": inspect_data.get("Id"),
        "name": (inspect_data.get("Name") or "").lstrip("/"),
        "image": inspect_data.get("Image"),
        "created": inspect_data.get("Created"),
        "platform": inspect_data.get("Platform"),
        "state": (inspect_data.get("State") or {}).get("Status"),
        "config": {
            "hostname": config.get("Hostname"),
            "domainname": config.get("Domainname"),
            "user": config.get("User"),
            "working_dir": config.get("WorkingDir"),
            "entrypoint": config.get("Entrypoint"),
            "cmd": config.get("Cmd"),
            "env": config.get("Env") or [],
            "labels": config.get("Labels") or {},
            "exposed_ports": list((config.get("ExposedPorts") or {}).keys()),
            "stop_signal": config.get("StopSignal"),
            "tty": config.get("Tty"),
            "open_stdin": config.get("OpenStdin"),
        },
        "host_config": {
            "restart_policy": host.get("RestartPolicy"),
            "network_mode": host.get("NetworkMode"),
            "port_bindings": host.get("PortBindings") or {},
            "binds": host.get("Binds") or [],
            "privileged": host.get("Privileged"),
            "cap_add": host.get("CapAdd") or [],
            "cap_drop": host.get("CapDrop") or [],
            "devices": host.get("Devices") or [],
            "extra_hosts": host.get("ExtraHosts") or [],
            "shm_size": host.get("ShmSize"),
            "runtime": host.get("Runtime"),
            "init": host.get("Init"),
            "readonly_rootfs": host.get("ReadonlyRootfs"),
            "security_opt": host.get("SecurityOpt") or [],
            "ulimits": host.get("Ulimits") or [],
        },
        "networks": network.get("Networks") or {},
        "mounts": inspect_data.get("Mounts") or [],
    }


def _discover_build_context(inspect_data: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    """Copy Dockerfile / compose files when discoverable from labels or mounts."""
    build_dir = output_dir / "build-context"
    found: list[str] = []
    notes: list[str] = []

    labels = (inspect_data.get("Config") or {}).get("Labels") or {}
    label_candidates = [
        ("com.docker.compose.project.working_dir", None),
        ("com.docker.compose.project.config_files", "compose"),
        ("org.opencontainers.image.source", None),
    ]

    copied_paths: set[str] = set()

    def copy_build_artifacts(base: Path, prefix: str = "") -> None:
        nonlocal found
        if not base.is_dir():
            return
        key = str(base.resolve())
        if key in copied_paths:
            return
        copied_paths.add(key)

        names = [
            "Dockerfile",
            "docker-compose.yml",
            "docker-compose.yaml",
            "compose.yml",
            "compose.yaml",
            ".dockerignore",
        ]
        for name in names:
            src = base / name
            if src.exists():
                rel = f"{prefix}{name}" if prefix else name
                dst = build_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                found.append(rel)

    # Labels from Docker Compose / OCI
    compose_config = labels.get("com.docker.compose.project.config_files")
    if compose_config:
        for cfg in compose_config.split(","):
            cfg_path = Path(cfg.strip())
            if cfg_path.exists():
                rel = sanitize_filename(cfg_path.name)
                dst = build_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cfg_path, dst)
                found.append(rel)
                copy_build_artifacts(cfg_path.parent, prefix=f"{cfg_path.parent.name}/")

    working_dir = labels.get("com.docker.compose.project.working_dir")
    if working_dir:
        copy_build_artifacts(Path(working_dir))

    # Bind mounts that look like project roots
    for mount in inspect_data.get("Mounts") or []:
        if mount.get("Type") != "bind":
            continue
        source = mount.get("Source")
        if not source:
            continue
        base = Path(source)
        if any((base / n).exists() for n in ("Dockerfile", "docker-compose.yml", "docker-compose.yaml")):
            copy_build_artifacts(base, prefix=f"{sanitize_filename(base.name)}/")

    if not found:
        notes.append(
            "Dockerfile / docker-compose は自動検出できませんでした。"
            "必要なら build-context/ に手動で配置してください。"
        )

    return {"files": sorted(set(found)), "notes": notes}


def _export_named_volume(volume_name: str, dest: Path) -> dict[str, Any]:
    archive = dest / f"{sanitize_filename(volume_name)}.tar.gz"
    log(f"  ボリューム '{volume_name}' をエクスポート中...")
    run_docker(
        [
            "run",
            "--rm",
            "-v",
            f"{volume_name}:/source:ro",
            "-v",
            f"{dest.resolve()}:/backup",
            "alpine:3.20",
            "tar",
            "czf",
            f"/backup/{archive.name}",
            "-C",
            "/source",
            ".",
        ],
        capture=True,
    )
    return {
        "name": volume_name,
        "archive": f"volumes/{archive.name}",
        "size_bytes": archive.stat().st_size if archive.exists() else 0,
    }


def _export_bind_mount(mount: dict[str, Any], container_id: str, dest: Path) -> dict[str, Any]:
    source = mount.get("Source") or ""
    destination = mount.get("Destination") or ""
    safe_name = sanitize_filename(destination.strip("/").replace("/", "_") or "root")
    archive = dest / f"bind_{safe_name}.tar.gz"
    meta: dict[str, Any] = {
        "type": "bind",
        "source": source,
        "destination": destination,
        "mode": mount.get("Mode"),
        "propagation": mount.get("Propagation"),
    }

    host_path = Path(source)
    exported = False
    if host_path.exists() and os_accessible(host_path):
        log(f"  バインドマウント '{destination}' (ホスト) をエクスポート中...")
        tar_directory(host_path, archive)
        exported = True
    else:
        log(f"  バインドマウント '{destination}' をコンテナ内からコピー中...")
        tmp = dest / f"_tmp_bind_{safe_name}"
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            run_docker(["cp", f"{container_id}:{destination}/.", str(tmp)], capture=True)
            tar_directory(tmp, archive)
            exported = True
        except MigrateError:
            meta["warning"] = (
                f"バインドマウント '{destination}' をエクスポートできませんでした。"
                f"移行先で手動で {source} を用意してください。"
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    if exported and archive.exists():
        meta["archive"] = f"volumes/{archive.name}"
        meta["size_bytes"] = archive.stat().st_size
    return meta


def os_accessible(path: Path) -> bool:
    try:
        return path.exists() and os.path.isdir(path) and os.access(path, os.R_OK)
    except OSError:
        return False


def export_container(
    container: str,
    output: Path,
    *,
    stop: bool = True,
    restart_after: bool = True,
    include_image: bool = True,
    skip_volumes: bool = False,
) -> Path:
    """Export container to a self-contained migration bundle."""
    container_id = resolve_container(container)
    inspect_data = inspect_container(container_id)
    metadata = _extract_metadata(inspect_data)
    container_name = metadata["name"] or container_id[:12]
    was_running = (inspect_data.get("State") or {}).get("Running", False)

    if output.exists():
        if any(output.iterdir()):
            raise MigrateError(
                f"出力ディレクトリ '{output}' は空ではありません。"
                "空のディレクトリを指定するか、別のパスを使ってください。"
            )
    else:
        output.mkdir(parents=True)

    volumes_dir = output / "volumes"
    volumes_dir.mkdir(exist_ok=True)

    stopped_for_export = False
    if stop and was_running:
        log(f"コンテナ '{container_name}' を一時停止しています...")
        run_docker(["stop", container_id], capture=True)
        stopped_for_export = True
        inspect_data = inspect_container(container_id)

    committed_image = f"migrate-{sanitize_filename(container_name)}:{utc_timestamp()}"
    log(f"コンテナ状態をイメージ '{committed_image}' に保存しています...")
    run_docker(["commit", container_id, committed_image], capture=True)

    image_info: dict[str, Any] = {"tag": committed_image}
    if include_image:
        image_tar = output / IMAGE_ARCHIVE_NAME
        log(f"イメージを {IMAGE_ARCHIVE_NAME} に保存しています...")
        save_image_archive(committed_image, image_tar)
        image_info["archive"] = IMAGE_ARCHIVE_NAME
        image_info["size_bytes"] = image_tar.stat().st_size if image_tar.exists() else 0

    volume_exports: list[dict[str, Any]] = []
    bind_exports: list[dict[str, Any]] = []

    if not skip_volumes:
        seen_volumes: set[str] = set()
        for mount in inspect_data.get("Mounts") or []:
            mtype = mount.get("Type")
            if mtype == "volume":
                vname = mount.get("Name") or ""
                if not vname or vname in seen_volumes:
                    continue
                seen_volumes.add(vname)
                try:
                    volume_exports.append(_export_named_volume(vname, volumes_dir))
                except MigrateError as exc:
                    volume_exports.append(
                        {
                            "name": vname,
                            "error": str(exc),
                            "destination": mount.get("Destination"),
                        }
                    )
            elif mtype == "bind":
                bind_exports.append(_export_bind_mount(mount, container_id, volumes_dir))

    build_info = _discover_build_context(inspect_data, output)

    manifest = {
        "bundle_version": BUNDLE_VERSION,
        "tool_version": "1.0.0",
        "exported_at": utc_timestamp(),
        "source_host": platform.node(),
        "container": metadata,
        "image": image_info,
        "volumes": volume_exports,
        "bind_mounts": bind_exports,
        "build_context": build_info,
        "export_options": {
            "stopped_for_export": stopped_for_export,
            "was_running": was_running,
            "restart_after_export": restart_after and stopped_for_export and was_running,
        },
    }

    write_json(output / "manifest.json", manifest)
    write_json(output / "container.inspect.json", inspect_data)

    env_file = output / "container.env"
    env_lines = metadata["config"].get("env") or []
    env_file.write_text("\n".join(env_lines) + ("\n" if env_lines else ""), encoding="utf-8")

    _write_restore_script(output, manifest)
    _write_restore_readme(output, manifest)

    if restart_after and stopped_for_export and was_running:
        log(f"コンテナ '{container_name}' を再開しています...")
        run_docker(["start", container_id], capture=True)

    # Cleanup committed image locally (optional - keep for now, user may want it)
    try:
        run_docker(["rmi", committed_image], check=False, capture=True)
    except MigrateError:
        pass

    total = human_size(dir_size(output))
    log(f"エクスポート完了: {output} ({total})")
    return output


def _write_restore_script(output: Path, manifest: dict[str, Any]) -> None:
    script = output / "restore.sh"
    script.write_text(
        """#!/usr/bin/env bash
# docker-migrate 自動復元スクリプト
# 使い方: ./restore.sh [--name 新しいコンテナ名] [--start]
set -euo pipefail

BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTAINER_NAME=""
AUTO_START=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) CONTAINER_NAME="$2"; shift 2 ;;
    --start) AUTO_START=true; shift ;;
    -h|--help)
      echo "Usage: $0 [--name CONTAINER_NAME] [--start]"
      exit 0
      ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  echo "Error: docker が見つかりません" >&2
  exit 1
fi

if [[ ! -f "$BUNDLE_DIR/manifest.json" ]]; then
  echo "Error: manifest.json が見つかりません" >&2
  exit 1
fi

python3 - "$BUNDLE_DIR" "$CONTAINER_NAME" "$AUTO_START" <<'PY'
import gzip
import json
import shutil
import subprocess
import sys
from pathlib import Path

bundle = Path(sys.argv[1])
new_name = sys.argv[2]
auto_start = sys.argv[3].lower() == "true"
manifest = json.loads((bundle / "manifest.json").read_text())
meta = manifest["container"]
default_name = meta.get("name") or "restored-container"
container_name = new_name or default_name
image_tag = manifest["image"]["tag"]

def run(args, **kw):
    subprocess.run(["docker", *args], check=True, **kw)

def load_image_archive(path):
    if path.name.endswith(".gz"):
        proc = subprocess.Popen(["docker", "load"], stdin=subprocess.PIPE)
        with gzip.open(path, "rb") as gz_in:
            shutil.copyfileobj(gz_in, proc.stdin)
        proc.stdin.close()
        if proc.wait() != 0:
            raise subprocess.CalledProcessError(proc.returncode, "docker load")
    else:
        run(["load", "-i", str(path)])

archive_name = manifest["image"].get("archive", "image.tar.gz")
candidates = [bundle / archive_name, bundle / "image.tar.gz", bundle / "image.tar"]
image_tar = next((p for p in candidates if p.exists()), None)
if image_tar:
    print(f"Loading image from {image_tar}...")
    load_image_archive(image_tar)
else:
    print("Warning: image archive not found, using tag from manifest only")

# Restore volumes
for vol in manifest.get("volumes", []):
    if "error" in vol:
        print(f"Skipping volume {vol.get('name')}: {vol['error']}")
        continue
    vname = vol["name"]
    archive = bundle / vol["archive"]
    if not archive.exists():
        print(f"Warning: missing archive for volume {vname}")
        continue
    print(f"Restoring volume {vname}...")
    subprocess.run(["docker", "volume", "create", vname], check=False)
    run([
        "run", "--rm",
        "-v", f"{vname}:/dest",
        "-v", f"{archive.parent.resolve()}:/backup:ro",
        "alpine:3.20",
        "sh", "-c", f"cd /dest && tar xzf /backup/{archive.name}",
    ])

# Restore bind mount data to ./restored-bind-mounts/
bind_root = bundle / "restored-bind-mounts"
for bind in manifest.get("bind_mounts", []):
    archive_key = bind.get("archive")
    if not archive_key:
        print(f"Warning: bind mount {bind.get('destination')} has no archive")
        continue
    archive = bundle / archive_key
    dest_hint = bind.get("destination", "unknown").strip("/").replace("/", "_")
    target = bind_root / dest_hint
    target.mkdir(parents=True, exist_ok=True)
    print(f"Extracting bind mount data for {bind.get('destination')} -> {target}")
    run([
        "run", "--rm",
        "-v", f"{target.resolve()}:/dest",
        "-v", f"{archive.parent.resolve()}:/backup:ro",
        "alpine:3.20",
        "sh", "-c", f"cd /dest && tar xzf /backup/{archive.name}",
    ])

# Build docker create args
create_args = ["create", "--name", container_name]
hc = meta.get("host_config", {})
cfg = meta.get("config", {})

if hc.get("restart_policy"):
    rp = hc["restart_policy"]
    policy = rp.get("Name") or "no"
    if policy != "no":
        create_args.extend(["--restart", policy])

for port, bindings in (hc.get("port_bindings") or {}).items():
    for binding in bindings or []:
        host_port = binding.get("HostPort", "")
        host_ip = binding.get("HostIp", "")
        mapping = f"{host_ip}:{host_port}:{port.split('/')[0]}" if host_ip else f"{host_port}:{port.split('/')[0]}"
        create_args.extend(["-p", mapping])

env_file = bundle / "container.env"
if env_file.exists() and env_file.read_text().strip():
    create_args.extend(["--env-file", str(env_file)])

for label, value in (cfg.get("labels") or {}).items():
    create_args.extend(["--label", f"{label}={value}"])

if cfg.get("working_dir"):
    create_args.extend(["-w", cfg["working_dir"]])
if cfg.get("user"):
    create_args.extend(["-u", cfg["user"]])
if cfg.get("hostname"):
    create_args.extend(["--hostname", cfg["hostname"]])
if hc.get("network_mode"):
    create_args.extend(["--network", hc["network_mode"]])
if hc.get("privileged"):
    create_args.append("--privileged")
if hc.get("readonly_rootfs"):
    create_args.append("--read-only")

for cap in hc.get("cap_add") or []:
    create_args.extend(["--cap-add", cap])
for cap in hc.get("cap_drop") or []:
    create_args.extend(["--cap-drop", cap])
for host in hc.get("extra_hosts") or []:
    create_args.extend(["--add-host", host])

# Volume mounts
for vol in manifest.get("volumes", []):
    dest = next(
        (m.get("Destination") for m in meta.get("mounts", []) if m.get("Name") == vol.get("name")),
        None,
    )
    if dest and "name" in vol:
        create_args.extend(["-v", f"{vol['name']}:{dest}"])

for bind in manifest.get("bind_mounts", []):
    dest = bind.get("destination")
    if not dest:
        continue
    hint = dest.strip("/").replace("/", "_")
    local = bind_root / hint
    if local.exists():
        create_args.extend(["-v", f"{local.resolve()}:{dest}"])
    elif bind.get("source"):
        print(f"Note: bind mount {dest} uses original host path {bind['source']} (may need manual fix)")

if cfg.get("entrypoint"):
    create_args.extend(["--entrypoint", cfg["entrypoint"][0] if len(cfg["entrypoint"]) == 1 else json.dumps(cfg["entrypoint"])])

create_args.append(image_tag)
if cfg.get("cmd"):
    create_args.extend(cfg["cmd"])

print("Creating container:", " ".join(create_args))
run(create_args)

if auto_start:
    print(f"Starting {container_name}...")
    run(["start", container_name])

print(f"Done. Container '{container_name}' created.")
print("Start manually: docker start", container_name)
PY
""",
        encoding="utf-8",
    )
    script.chmod(0o755)


def _write_restore_readme(output: Path, manifest: dict[str, Any]) -> None:
    name = manifest["container"].get("name") or "container"
    readme = output / "RESTORE.md"
    readme.write_text(
        f"""# 復元手順 — {name}

このディレクトリは `docker-migrate export` で作成された移行バンドルです。

## バンドル構成

| ファイル / ディレクトリ | 内容 |
|------------------------|------|
| `manifest.json` | コンテナ設定・ボリューム一覧などのメタデータ |
| `container.inspect.json` | 元コンテナの `docker inspect` 出力 |
| `container.env` | 環境変数一覧 |
| `image.tar.gz` | `docker save` したイメージ（gzip 圧縮） |
| `volumes/` | 名前付きボリューム・バインドマウントの tar.gz |
| `build-context/` | 検出された Dockerfile / compose（あれば） |
| `restore.sh` | 自動復元スクリプト |

## 移行先 PC での復元

### 方法 1: 自動スクリプト（推奨）

```bash
# バンドルを移行先にコピー後
cd /path/to/bundle
chmod +x restore.sh
./restore.sh --start
```

別名で復元する場合:

```bash
./restore.sh --name my-restored-app --start
```

### 方法 2: docker-migrate import

```bash
docker-migrate import /path/to/bundle --start
```

競合時（同名コンテナ・ポート・ボリューム）は `docker-migrate import` の `--mode overwrite` / `--mode clone` を使用してください。  
`restore.sh` 単体では競合検出・複製モードには対応していません。

### 方法 3: 手動

```bash
gzip -dc image.tar.gz | docker load
# 旧形式の image.tar がある場合: docker load -i image.tar
docker volume create <volume_name>
# volumes/*.tar.gz を展開してボリュームに投入
docker create ... # manifest.json を参照
docker start <container_name>
```

## 注意事項

- **ネットワーク**: カスタムネットワークは移行先で事前に作成が必要な場合があります。
- **バインドマウント**: ホストパスは環境依存です。`restored-bind-mounts/` または手動配置を確認してください。
- **ポート**: 移行先でポート競合がないか確認してください。
- **プラットフォーム**: ARM/x86 などアーキテクチャ差がある場合はイメージの互換性に注意してください。

## エクスポート情報

- エクスポート日時: {manifest.get('exported_at')}
- ソースホスト: {manifest.get('source_host')}
- 元コンテナ名: {name}
""",
        encoding="utf-8",
    )
