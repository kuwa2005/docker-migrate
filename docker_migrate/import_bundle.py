"""Import / restore a migration bundle on the destination host."""

from __future__ import annotations

import json
from pathlib import Path

from .conflicts import (
    CloneConfig,
    apply_clone_to_manifest,
    build_clone_config,
    detect_conflicts,
    format_conflict_summary,
    prompt_clone_options,
    resolve_import_mode,
)
from .utils import (
    IMAGE_ARCHIVE_NAME,
    LEGACY_IMAGE_ARCHIVE_NAME,
    MigrateError,
    load_image_archive,
    log,
    read_json,
    resolve_image_archive,
    run_docker,
    sanitize_filename,
)


def import_bundle(
    bundle_dir: Path,
    *,
    container_name: str | None = None,
    start: bool = False,
    force: bool = False,
    mode: str | None = None,
    clone_suffix: str | None = None,
    port_offset: int = 0,
    interactive: bool | None = None,
) -> str:
    """Restore a migration bundle using docker CLI."""
    bundle_dir = bundle_dir.resolve()
    manifest_path = bundle_dir / "manifest.json"
    if not manifest_path.exists():
        raise MigrateError(f"manifest.json が見つかりません: {bundle_dir}")

    manifest = read_json(manifest_path)
    meta = manifest.get("container") or {}
    default_name = meta.get("name") or "restored-container"
    original_name = default_name.lstrip("/")
    requested_name = (container_name or original_name).lstrip("/")

    report = detect_conflicts(bundle_dir, manifest, requested_name)
    resolved_mode = resolve_import_mode(
        report,
        mode=mode,
        force=force,
        interactive=interactive,
    )

    clone_config: CloneConfig | None = None
    working_manifest = manifest
    name = requested_name

    if resolved_mode == "clone":
        if clone_suffix is None and interactive is not False:
            is_tty = interactive if interactive is not None else True
            if is_tty and mode != "clone":
                name, clone_suffix = prompt_clone_options(original_name)
            else:
                name = container_name or f"{original_name}-dev"
                clone_suffix = clone_suffix or "-dev"
        else:
            name = container_name or f"{original_name}{clone_suffix or '-dev'}"
        clone_config = build_clone_config(
            manifest,
            original_name=original_name,
            container_name=name,
            clone_suffix=clone_suffix,
            port_offset=port_offset,
        )
        working_manifest = apply_clone_to_manifest(manifest, clone_config)
        name = clone_config.container_name
        if clone_config.volume_map:
            for orig, cloned in clone_config.volume_map.items():
                log(f"  ボリューム '{orig}' -> '{cloned}' (複製モード)")
        log(f"複製モード: コンテナ '{name}'")
    elif report.has_conflicts:
        log(format_conflict_summary(report))
        if report.port_conflicts:
            owners = ", ".join(f"{port} ({owner})" for port, owner in report.port_conflicts)
            raise MigrateError(
                f"上書きモードでも解決できないポート競合があります: {owners}\n"
                "--mode clone で別ポートに複製してください。"
            )
        log("上書きモードで続行します。")

    _check_existing_container(name, force=(resolved_mode == "overwrite"))

    image_info = manifest.get("image") or {}
    image_tag = image_info.get("tag", "")
    image_tar = resolve_image_archive(bundle_dir, image_info)

    if image_tar is None:
        raise MigrateError(
            f"イメージファイルが見つかりません "
            f"({IMAGE_ARCHIVE_NAME} または {LEGACY_IMAGE_ARCHIVE_NAME})。"
            "バンドルが完全か確認してください。"
        )

    log(f"イメージを読み込み中: {image_tar}")
    load_result = load_image_archive(image_tar)
    loaded_tag = _parse_loaded_tag(load_result.stdout, image_tag)
    _restore_volumes(
        bundle_dir,
        working_manifest,
        overwrite=(resolved_mode == "overwrite"),
    )
    bind_root = _restore_bind_mounts(
        bundle_dir,
        working_manifest,
        bind_root_name=clone_config.bind_root_name if clone_config else "restored-bind-mounts",
    )

    create_args = _build_create_args(working_manifest, bundle_dir, loaded_tag, name, bind_root)
    log(f"コンテナ '{name}' を作成中...")
    run_docker(create_args, capture=True)

    if start:
        log(f"コンテナ '{name}' を起動中...")
        run_docker(["start", name], capture=True)

    log(f"復元完了: コンテナ '{name}'")
    if not start:
        log(f"起動するには: docker start {name}")
    return name


def _check_existing_container(name: str, *, force: bool) -> None:
    result = run_docker(
        ["ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.ID}}"],
        capture=True,
        check=False,
    )
    if result.stdout.strip():
        if not force:
            raise MigrateError(
                f"コンテナ名 '{name}' は既に存在します。"
                "--mode overwrite または --force で上書きするか、"
                "--mode clone / --name で別名を指定してください。"
            )
        cid = result.stdout.strip().splitlines()[0]
        log(f"既存コンテナ '{name}' ({cid}) を削除しています...")
        run_docker(["rm", "-f", cid], capture=True)


def _parse_loaded_tag(load_output: str, fallback_tag: str) -> str:
    for line in load_output.splitlines():
        if "Loaded image:" in line:
            return line.split("Loaded image:", 1)[1].strip()
    return fallback_tag


def _restore_volumes(
    bundle_dir: Path,
    manifest: dict,
    *,
    overwrite: bool = True,
) -> None:
    for vol in manifest.get("volumes") or []:
        if vol.get("error"):
            log(f"  スキップ (エクスポート失敗): {vol.get('name')}")
            continue
        orig_name = vol.get("name")
        archive_rel = vol.get("archive")
        if not orig_name or not archive_rel:
            continue
        vname = orig_name
        archive = bundle_dir / archive_rel
        if not archive.exists():
            log(f"  警告: ボリューム '{vname}' のアーカイブが見つかりません")
            continue
        if not overwrite and _volume_exists(vname):
            log(f"  スキップ: ボリューム '{vname}' は既に存在します")
            continue
        log(f"  ボリューム '{vname}' を復元中...")
        run_docker(["volume", "create", vname], check=False, capture=True)
        run_docker(
            [
                "run",
                "--rm",
                "-v",
                f"{vname}:/dest",
                "-v",
                f"{archive.parent.resolve()}:/backup:ro",
                "alpine:3.20",
                "sh",
                "-c",
                f"cd /dest && tar xzf /backup/{archive.name}",
            ],
            capture=True,
        )


def _volume_exists(name: str) -> bool:
    result = run_docker(["volume", "inspect", name], capture=True, check=False)
    return result.returncode == 0


def _restore_bind_mounts(
    bundle_dir: Path,
    manifest: dict,
    *,
    bind_root_name: str = "restored-bind-mounts",
) -> Path:
    bind_root = bundle_dir / bind_root_name
    bind_root.mkdir(exist_ok=True)
    for bind in manifest.get("bind_mounts") or []:
        archive_rel = bind.get("archive")
        if not archive_rel:
            log(f"  警告: バインドマウント '{bind.get('destination')}' にデータがありません")
            continue
        archive = bundle_dir / archive_rel
        dest_hint = sanitize_filename(
            (bind.get("destination") or "unknown").strip("/").replace("/", "_")
        )
        target = bind_root / dest_hint
        target.mkdir(parents=True, exist_ok=True)
        log(f"  バインドマウント '{bind.get('destination')}' -> {target}")
        run_docker(
            [
                "run",
                "--rm",
                "-v",
                f"{target.resolve()}:/dest",
                "-v",
                f"{archive.parent.resolve()}:/backup:ro",
                "alpine:3.20",
                "sh",
                "-c",
                f"cd /dest && tar xzf /backup/{archive.name}",
            ],
            capture=True,
        )
    return bind_root


def _build_create_args(
    manifest: dict,
    bundle_dir: Path,
    image_tag: str,
    container_name: str,
    bind_root: Path,
) -> list[str]:
    meta = manifest.get("container") or {}
    hc = meta.get("host_config") or {}
    cfg = meta.get("config") or {}

    args = ["create", "--name", container_name]

    restart = hc.get("restart_policy") or {}
    policy = restart.get("Name") or "no"
    if policy and policy != "no":
        args.extend(["--restart", policy])

    for port, bindings in (hc.get("port_bindings") or {}).items():
        container_port = port.split("/")[0]
        for binding in bindings or []:
            host_port = binding.get("HostPort") or ""
            host_ip = binding.get("HostIp") or ""
            if host_ip:
                mapping = f"{host_ip}:{host_port}:{container_port}"
            else:
                mapping = f"{host_port}:{container_port}"
            args.extend(["-p", mapping])

    env_file = bundle_dir / "container.env"
    if env_file.exists() and env_file.read_text(encoding="utf-8").strip():
        args.extend(["--env-file", str(env_file)])

    for label, value in (cfg.get("labels") or {}).items():
        args.extend(["--label", f"{label}={value}"])

    if cfg.get("working_dir"):
        args.extend(["-w", cfg["working_dir"]])
    if cfg.get("user"):
        args.extend(["-u", cfg["user"]])
    if cfg.get("hostname"):
        args.extend(["--hostname", cfg["hostname"]])

    network_mode = hc.get("network_mode") or ""
    if network_mode and network_mode not in ("default", "bridge"):
        if network_mode.startswith("container:"):
            log(
                f"  注意: network_mode={network_mode} は移行先で手動調整が必要な場合があります"
            )
        args.extend(["--network", network_mode])

    if hc.get("privileged"):
        args.append("--privileged")
    if hc.get("readonly_rootfs"):
        args.append("--read-only")

    for cap in hc.get("cap_add") or []:
        args.extend(["--cap-add", cap])
    for cap in hc.get("cap_drop") or []:
        args.extend(["--cap-drop", cap])
    for host in hc.get("extra_hosts") or []:
        args.extend(["--add-host", host])

    for vol in manifest.get("volumes") or []:
        vname = vol.get("name")
        if not vname or vol.get("error"):
            continue
        dest = _mount_destination(meta, vname)
        if dest:
            args.extend(["-v", f"{vname}:{dest}"])

    for bind in manifest.get("bind_mounts") or []:
        dest = bind.get("destination")
        if not dest:
            continue
        hint = sanitize_filename(dest.strip("/").replace("/", "_"))
        local = bind_root / hint
        if local.exists() and any(local.iterdir()):
            args.extend(["-v", f"{local.resolve()}:{dest}"])
        elif bind.get("source"):
            log(
                f"  注意: バインドマウント {dest} は元ホストパス {bind['source']} を参照します（要確認）"
            )
            args.extend(["-v", f"{bind['source']}:{dest}"])

    entrypoint = cfg.get("entrypoint")
    if entrypoint:
        if len(entrypoint) == 1:
            args.extend(["--entrypoint", entrypoint[0]])
        else:
            args.extend(["--entrypoint", json.dumps(entrypoint)])

    args.append(image_tag)
    cmd = cfg.get("cmd")
    if cmd:
        args.extend(cmd)
    return args


def _mount_destination(meta: dict, volume_name: str) -> str | None:
    for mount in meta.get("mounts") or []:
        if mount.get("Name") == volume_name:
            return mount.get("Destination")
    return None


def show_bundle_info(bundle_dir: Path) -> None:
    manifest_path = bundle_dir / "manifest.json"
    if not manifest_path.exists():
        raise MigrateError(f"manifest.json が見つかりません: {bundle_dir}")
    manifest = read_json(manifest_path)
    meta = manifest.get("container") or {}
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    print("\n--- 概要 ---")
    print(f"コンテナ名: {meta.get('name')}")
    print(f"エクスポート日時: {manifest.get('exported_at')}")
    print(f"イメージ: {manifest.get('image', {}).get('tag')}")
    vols = manifest.get("volumes") or []
    print(f"ボリューム数: {len(vols)}")
    binds = manifest.get("bind_mounts") or []
    print(f"バインドマウント数: {len(binds)}")
    build = manifest.get("build_context") or {}
    files = build.get("files") or []
    print(f"ビルドコンテキスト: {', '.join(files) if files else '(なし)'}")
