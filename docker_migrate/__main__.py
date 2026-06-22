"""CLI entry point for docker-migrate."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from docker_migrate import __version__
from docker_migrate.export import export_container
from docker_migrate.gui import is_interactive_tty, run_gui
from docker_migrate.import_bundle import import_bundle, show_bundle_info
from docker_migrate.utils import MigrateError, require_docker


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docker-migrate",
        description="Docker コンテナを別 PC へ移行するためのエクスポート / インポートツール",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("gui", help="対話型メニューを起動")

    export_p = sub.add_parser("export", help="コンテナを移行バンドルにエクスポート")
    export_p.add_argument("container", help="コンテナ名または ID")
    export_p.add_argument(
        "-o",
        "--output",
        required=True,
        type=Path,
        help="出力ディレクトリ（空のディレクトリを推奨）",
    )
    export_p.add_argument(
        "--no-stop",
        action="store_true",
        help="エクスポート中にコンテナを停止しない（実行中のまま commit）",
    )
    export_p.add_argument(
        "--no-restart",
        action="store_true",
        help="停止したコンテナをエクスポート後に再起動しない",
    )
    export_p.add_argument(
        "--skip-volumes",
        action="store_true",
        help="ボリューム / バインドマウントのデータをエクスポートしない",
    )
    export_p.add_argument(
        "--skip-image",
        action="store_true",
        help="docker save をスキップ（メタデータのみ）",
    )

    import_p = sub.add_parser("import", help="移行バンドルからコンテナを復元")
    import_p.add_argument("bundle", type=Path, help="移行バンドルディレクトリ")
    import_p.add_argument("--name", help="復元後のコンテナ名（省略時は元の名前）")
    import_p.add_argument("--start", action="store_true", help="復元後にコンテナを起動")
    import_p.add_argument(
        "--force",
        action="store_true",
        help="競合時に上書き（--mode overwrite と同等）",
    )
    import_p.add_argument(
        "--mode",
        choices=("overwrite", "clone"),
        help="競合解決: overwrite=上書き, clone=別名・別ポートで複製",
    )
    import_p.add_argument(
        "--clone-suffix",
        help="複製モードのサフィックス（例: -dev, -prod）",
    )
    import_p.add_argument(
        "--port-offset",
        type=int,
        default=0,
        help="複製モードでホストポートに加算するオフセット（0=空きポートを自動探索）",
    )

    info_p = sub.add_parser("info", help="移行バンドルの内容を表示")
    info_p.add_argument("bundle", type=Path, help="移行バンドルディレクトリ")

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv:
        if is_interactive_tty():
            return run_gui()
        build_parser().print_help()
        return 2

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        if is_interactive_tty():
            return run_gui()
        parser.print_help()
        return 2

    if args.command == "gui":
        return run_gui()

    try:
        require_docker()
        if args.command == "export":
            export_container(
                args.container,
                args.output,
                stop=not args.no_stop,
                restart_after=not args.no_restart,
                include_image=not args.skip_image,
                skip_volumes=args.skip_volumes,
            )
        elif args.command == "import":
            import_bundle(
                args.bundle,
                container_name=args.name,
                start=args.start,
                force=args.force,
                mode=args.mode,
                clone_suffix=args.clone_suffix,
                port_offset=args.port_offset,
                interactive=False,
            )
        elif args.command == "info":
            show_bundle_info(args.bundle)
        else:
            parser.error(f"未知のコマンド: {args.command}")
    except MigrateError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n中断しました。", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
