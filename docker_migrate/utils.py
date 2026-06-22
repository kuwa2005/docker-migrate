"""Shared utilities for docker-migrate."""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_BUNDLE_DIR = "docker-backup"
LEGACY_BUNDLE_DIR = "migration-bundle"
IMAGE_ARCHIVE_NAME = "image.tar.gz"
LEGACY_IMAGE_ARCHIVE_NAME = "image.tar"


class MigrateError(Exception):
    """Raised when migration fails with a user-facing message."""


def run_docker(
    args: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    input_data: bytes | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = ["docker", *args]
    try:
        return subprocess.run(
            cmd,
            check=check,
            capture_output=capture,
            text=True,
            input=input_data,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else str(exc)
        raise MigrateError(f"Docker コマンドが失敗しました: {' '.join(cmd)}\n{stderr}") from exc
    except FileNotFoundError as exc:
        raise MigrateError(
            "docker コマンドが見つかりません。Docker がインストールされ、PATH に含まれていることを確認してください。"
        ) from exc


def require_docker() -> None:
    run_docker(["version", "--format", "{{.Server.Version}}"], capture=True)


def list_containers() -> list[tuple[str, str, str]]:
    """Return (container_id, name, status) for all containers."""
    result = run_docker(
        ["ps", "-a", "--format", "{{.ID}}\t{{.Names}}\t{{.Status}}"],
        capture=True,
    )
    containers: list[tuple[str, str, str]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) < 2:
            continue
        cid, name = parts[0], parts[1]
        status = parts[2] if len(parts) > 2 else ""
        containers.append((cid, name, status))
    return containers


def resolve_container(identifier: str) -> str:
    """Return container ID for name or partial ID."""
    result = run_docker(["ps", "-a", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}"], capture=True)
    matches: list[tuple[str, str]] = []
    exact: list[tuple[str, str]] = []

    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        container_id, name = parts
        if identifier == name or identifier == container_id:
            exact.append((container_id, name))
        elif container_id.startswith(identifier):
            matches.append((container_id, name))

    if len(exact) == 1:
        return exact[0][0]
    if len(exact) > 1:
        names = ", ".join(name for _, name in exact)
        raise MigrateError(f"コンテナ '{identifier}' が複数一致しました: {names}")

    if len(matches) == 1:
        return matches[0][0]
    if len(matches) > 1:
        names = ", ".join(name for _, name in matches)
        raise MigrateError(
            f"コンテナ ID プレフィックス '{identifier}' が複数一致しました: {names}\n"
            "完全な名前または ID を指定してください。"
        )
    raise MigrateError(f"コンテナ '{identifier}' が見つかりません。")


def inspect_container(container_id: str) -> dict[str, Any]:
    result = run_docker(["inspect", container_id], capture=True)
    data = json.loads(result.stdout)
    if not data:
        raise MigrateError("docker inspect の結果が空です。")
    return data[0]


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w.\-]+", "_", name.strip())
    return cleaned or "unnamed"


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def copy_tree_if_exists(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    if src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return True
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
        return True
    return False


def tar_directory(source: Path, archive: Path) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    run_docker(
        [
            "run",
            "--rm",
            "-v",
            f"{source.resolve()}:/source:ro",
            "-v",
            f"{archive.parent.resolve()}:/backup",
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


def human_size(num_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num_bytes < 1024 or unit == "TB":
            return f"{num_bytes:.1f} {unit}" if unit != "B" else f"{num_bytes} B"
        num_bytes /= 1024
    return f"{num_bytes:.1f} PB"


def dir_size(path: Path) -> int:
    total = 0
    if path.is_file():
        return path.stat().st_size
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def save_image_archive(image_tag: str, dest: Path) -> None:
    """Save a docker image to a gzip-compressed tar archive."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    save_proc = subprocess.Popen(
        ["docker", "save", image_tag],
        stdout=subprocess.PIPE,
    )
    if save_proc.stdout is None:
        raise MigrateError("docker save の stdout を取得できませんでした。")
    try:
        with gzip.open(dest, "wb") as gz_out:
            shutil.copyfileobj(save_proc.stdout, gz_out)
    finally:
        save_proc.stdout.close()
    if save_proc.wait() != 0:
        raise MigrateError(f"docker save が失敗しました: {image_tag}")


def load_image_archive(archive: Path) -> subprocess.CompletedProcess[str]:
    """Load a docker image from tar or gzip-compressed tar."""
    if archive.name.endswith(".gz"):
        load_proc = subprocess.Popen(
            ["docker", "load"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if load_proc.stdin is None:
            raise MigrateError("docker load の stdin を取得できませんでした。")
        try:
            with gzip.open(archive, "rb") as gz_in:
                shutil.copyfileobj(gz_in, load_proc.stdin)
        finally:
            load_proc.stdin.close()
        stdout, stderr = load_proc.communicate()
        if load_proc.returncode != 0:
            stderr_text = stderr.strip() if stderr else "unknown error"
            raise MigrateError(f"docker load が失敗しました: {stderr_text}")
        return subprocess.CompletedProcess(
            ["docker", "load"],
            load_proc.returncode,
            stdout,
            stderr,
        )
    return run_docker(["load", "-i", str(archive)], capture=True)


def resolve_image_archive(bundle_dir: Path, image_info: dict[str, Any]) -> Path | None:
    """Locate an image archive in a bundle, including legacy image.tar."""
    archive_name = image_info.get("archive")
    if archive_name:
        path = bundle_dir / archive_name
        if path.exists():
            return path
    for name in (IMAGE_ARCHIVE_NAME, LEGACY_IMAGE_ARCHIVE_NAME):
        path = bundle_dir / name
        if path.exists():
            return path
    return None
