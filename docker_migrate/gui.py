"""Interactive terminal menu for docker-migrate."""

from __future__ import annotations

import sys
from pathlib import Path

from .export import export_container
from .conflicts import (
    detect_conflicts,
    format_conflict_summary,
    parse_resolution_keyword,
    prompt_clone_options,
)
from .import_bundle import import_bundle, show_bundle_info
from .utils import (
    DEFAULT_BUNDLE_DIR,
    LEGACY_BUNDLE_DIR,
    MigrateError,
    dir_size,
    human_size,
    list_containers,
    log,
    read_json,
    require_docker,
    sanitize_filename,
)


def is_interactive_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _print_header(title: str) -> None:
    width = 60
    print()
    print("=" * width)
    print(f"  {title}")
    print("=" * width)


def _prompt(text: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        try:
            value = input(f"{text}{suffix}: ").strip()
        except EOFError:
            print()
            raise KeyboardInterrupt from None
        if not value and default is not None:
            return default
        if value:
            return value
        print("入力してください。")


def _prompt_conflict_resolution() -> str:
    """Prompt with keyword confirmation (not Y/n) for destructive import choices."""
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


def _prompt_yes_no(text: str, *, default: bool = True) -> bool:
    default_label = "Y/n" if default else "y/N"
    while True:
        try:
            value = input(f"{text} [{default_label}]: ").strip().lower()
        except EOFError:
            print()
            raise KeyboardInterrupt from None
        if not value:
            return default
        if value in ("y", "yes", "はい"):
            return True
        if value in ("n", "no", "いいえ"):
            return False
        print("y または n を入力してください。")


def _prompt_path(
    text: str,
    *,
    must_exist: bool = False,
    must_be_dir: bool = False,
    default: str | None = None,
    create_parents: bool = False,
) -> Path:
    while True:
        raw = _prompt(text, default)
        path = Path(raw).expanduser().resolve()
        if must_exist and not path.exists():
            print(f"パスが見つかりません: {path}")
            continue
        if must_be_dir and path.exists() and not path.is_dir():
            print(f"ディレクトリを指定してください: {path}")
            continue
        if create_parents and not path.exists():
            path.mkdir(parents=True, exist_ok=True)
        return path


def _is_valid_bundle(path: Path) -> bool:
    return path.is_dir() and (path / "manifest.json").is_file()


def _default_export_dir(container_name: str) -> Path:
    safe_name = sanitize_filename(container_name)
    return (Path.cwd() / DEFAULT_BUNDLE_DIR / safe_name).resolve()


def _discover_migration_bundles() -> list[Path]:
    """Scan common locations for migration bundles (newest manifest first)."""
    cwd = Path.cwd().resolve()
    candidates: list[Path] = []

    for root_name in (DEFAULT_BUNDLE_DIR, LEGACY_BUNDLE_DIR):
        bundle_root = cwd / root_name
        if bundle_root.is_dir():
            for child in bundle_root.iterdir():
                if _is_valid_bundle(child):
                    candidates.append(child.resolve())

    for pattern in (f"{DEFAULT_BUNDLE_DIR}-*", f"{LEGACY_BUNDLE_DIR}-*"):
        for child in cwd.glob(pattern):
            if _is_valid_bundle(child):
                candidates.append(child.resolve())

    if _is_valid_bundle(cwd):
        candidates.append(cwd)

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        if path not in seen:
            seen.add(path)
            unique.append(path)

    def manifest_mtime(path: Path) -> float:
        try:
            return (path / "manifest.json").stat().st_mtime
        except OSError:
            return 0.0

    unique.sort(key=manifest_mtime, reverse=True)
    return unique


def _prompt_bundle_dir() -> Path:
    bundles = _discover_migration_bundles()

    if len(bundles) > 1:
        labels = []
        for bundle in bundles:
            manifest = read_json(bundle / "manifest.json")
            meta = manifest.get("container") or {}
            name = meta.get("name") or bundle.name
            labels.append(f"{name}  ({bundle})")
        choice = _select_from_list(
            labels,
            title="検出された移行バンドル",
            allow_cancel=False,
            default_index=0,
        )
        if choice is None:
            raise KeyboardInterrupt
        return bundles[choice]

    default = str(bundles[0]) if bundles else None
    if bundles:
        print(f"検出: {bundles[0]}")
    return _prompt_path(
        "移行バンドルのディレクトリ",
        must_exist=True,
        must_be_dir=True,
        default=default,
    )


def _select_from_list(
    items: list[str],
    *,
    title: str,
    allow_cancel: bool = True,
    default_index: int | None = None,
) -> int | None:
    if not items:
        print("選択肢がありません。")
        return None

    _print_header(title)
    for index, item in enumerate(items, start=1):
        marker = "  ← 推奨" if default_index is not None and index - 1 == default_index else ""
        print(f"  {index}. {item}{marker}")
    if allow_cancel:
        print("  0. 戻る / キャンセル")

    prompt_default = str(default_index + 1) if default_index is not None else None
    while True:
        raw = _prompt("番号を選択", prompt_default)
        if allow_cancel and raw == "0":
            return None
        if raw.isdigit():
            choice = int(raw)
            if 1 <= choice <= len(items):
                return choice - 1
        print(f"1 から {len(items)} の番号を入力してください。")


def _format_bundle_summary(bundle_dir: Path) -> str:
    manifest_path = bundle_dir / "manifest.json"
    manifest = read_json(manifest_path)
    meta = manifest.get("container") or {}
    image = manifest.get("image") or {}
    volumes = manifest.get("volumes") or []
    binds = manifest.get("bind_mounts") or []
    build = manifest.get("build_context") or {}
    build_files = build.get("files") or []

    lines = [
        f"バンドル: {bundle_dir}",
        f"コンテナ名: {meta.get('name') or '(不明)'}",
        f"状態 (エクスポート時): {meta.get('state') or '(不明)'}",
        f"エクスポート日時: {manifest.get('exported_at') or '(不明)'}",
        f"ソースホスト: {manifest.get('source_host') or '(不明)'}",
        f"イメージ: {image.get('tag') or '(不明)'}",
        f"ボリューム数: {len(volumes)}",
        f"バインドマウント数: {len(binds)}",
        f"ビルドコンテキスト: {', '.join(build_files) if build_files else '(なし)'}",
    ]
    if bundle_dir.exists():
        try:
            lines.append(f"バンドルサイズ: {human_size(dir_size(bundle_dir))}")
        except OSError:
            pass
    return "\n".join(lines)


def _run_export_flow() -> None:
    _print_header("エクスポート — コンテナ選択")
    require_docker()

    containers = list_containers()
    if not containers:
        print("コンテナが見つかりません。")
        return

    labels = [
        f"{name}  [{status}]  ({cid[:12]})"
        for cid, name, status in containers
    ]
    choice = _select_from_list(labels, title="エクスポートするコンテナ")
    if choice is None:
        return

    cid, name, _status = containers[choice]
    container_ref = name or cid[:12]

    _print_header("エクスポート — 出力先")
    default_output = str(_default_export_dir(name or cid[:12]))
    output = _prompt_path(
        "出力ディレクトリ（Enter で推奨パス、親フォルダは自動作成）",
        default=default_output,
        create_parents=True,
    )

    if output.exists() and any(output.iterdir()):
        print(f"警告: '{output}' は空ではありません。")
        if not _prompt_yes_no("続行しますか？", default=False):
            return

    _print_header("エクスポート — オプション")
    stop = _prompt_yes_no("実行中なら一時停止してからエクスポート", default=True)
    restart_after = True
    if stop:
        restart_after = _prompt_yes_no("エクスポート後にコンテナを再開", default=True)
    include_image = _prompt_yes_no("イメージ (image.tar.gz) を含める", default=True)
    skip_volumes = not _prompt_yes_no("ボリューム / バインドマウントを含める", default=True)

    _print_header("エクスポート — 確認")
    print(f"  コンテナ: {container_ref}")
    print(f"  出力先:   {output}")
    print(f"  停止:     {'はい' if stop else 'いいえ'}")
    if stop:
        print(f"  再開:     {'はい' if restart_after else 'いいえ'}")
    print(f"  イメージ: {'含める' if include_image else 'スキップ'}")
    print(f"  ボリューム: {'含める' if not skip_volumes else 'スキップ'}")
    print()

    if not _prompt_yes_no("エクスポートを開始", default=True):
        print("キャンセルしました。")
        return

    print()
    log("エクスポートを開始します...")
    export_container(
        container_ref,
        output,
        stop=stop,
        restart_after=restart_after,
        include_image=include_image,
        skip_volumes=skip_volumes,
    )
    print()
    print(f"完了: {output}")


def _run_import_flow() -> None:
    _print_header("インポート — バンドル選択")
    require_docker()

    bundle = _prompt_bundle_dir()
    manifest = bundle / "manifest.json"
    if not manifest.exists():
        print(f"Error: manifest.json が見つかりません: {bundle}")
        return

    _print_header("インポート — バンドル内容")
    print(_format_bundle_summary(bundle))
    print()

    manifest_data = read_json(manifest)
    meta = manifest_data.get("container") or {}
    default_name = (meta.get("name") or "restored-container").lstrip("/")
    container_name = _prompt("復元後のコンテナ名（空欄で元の名前）", "")
    requested_name = (container_name or default_name).lstrip("/")
    start = _prompt_yes_no("復元後にコンテナを起動", default=True)

    report = detect_conflicts(bundle, manifest_data, requested_name)
    mode: str | None = None
    clone_suffix: str | None = None
    port_offset = 0
    resolved_name = container_name or None

    if report.has_conflicts:
        _print_header("インポート — 競合検出")
        print(format_conflict_summary(report))
        print()
        mode = _prompt_conflict_resolution()
        if mode == "clone":
            clone_name, clone_suffix = prompt_clone_options(default_name)
            resolved_name = clone_name
        else:
            resolved_name = container_name or None

    _print_header("インポート — 確認")
    print(f"  バンドル: {bundle}")
    print(f"  コンテナ名: {resolved_name or default_name}")
    print(f"  起動: {'はい' if start else 'いいえ'}")
    if report.has_conflicts:
        print(f"  競合解決: {'上書き' if mode == 'overwrite' else '複製'}")
        if mode == "clone":
            print(f"  複製サフィックス: {clone_suffix}")
    print()

    if not _prompt_yes_no("インポートを開始", default=True):
        print("キャンセルしました。")
        return

    print()
    log("インポートを開始します...")
    name = import_bundle(
        bundle,
        container_name=resolved_name,
        start=start,
        mode=mode,
        clone_suffix=clone_suffix,
        port_offset=port_offset,
        interactive=False,
    )
    print()
    print(f"完了: コンテナ '{name}' を復元しました。")


def _run_info_flow() -> None:
    _print_header("バンドル情報")
    bundle = _prompt_bundle_dir()
    manifest = bundle / "manifest.json"
    if not manifest.exists():
        print(f"Error: manifest.json が見つかりません: {bundle}")
        return

    print()
    print(_format_bundle_summary(bundle))
    print()
    if _prompt_yes_no("manifest.json の全文を表示", default=False):
        print()
        show_bundle_info(bundle)
    else:
        _prompt("Enter でメニューに戻る", "")


def run_gui() -> int:
    """Run the interactive terminal menu. Returns exit code."""
    if not is_interactive_tty():
        print(
            "対話型メニューには TTY が必要です。\n"
            "CLI コマンドを使ってください: docker-migrate export / import / info",
            file=sys.stderr,
        )
        return 1

    try:
        require_docker()
    except MigrateError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    menu_items = [
        ("エクスポート（コンテナ → バンドル）", _run_export_flow),
        ("インポート（バンドル → コンテナ）", _run_import_flow),
        ("バンドル情報を表示", _run_info_flow),
        ("終了", None),
    ]

    while True:
        _print_header("docker-migrate — 移行メニュー")
        labels = [label for label, _ in menu_items]
        choice = _select_from_list(labels, title="操作を選択", allow_cancel=False)
        if choice is None:
            continue

        label, handler = menu_items[choice]
        if handler is None:
            print("終了します。")
            return 0

        try:
            handler()
        except MigrateError as exc:
            print(f"\nError: {exc}", file=sys.stderr)
        except KeyboardInterrupt:
            print("\n操作を中断しました。")

        if not _prompt_yes_no("\nメインメニューに戻る", default=True):
            print("終了します。")
            return 0
