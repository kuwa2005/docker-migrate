# Changelog

このプロジェクトの重要な変更はすべてこのファイルに記録します。

形式は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/) に基づき、
[Semantic Versioning](https://semver.org/lang/ja/) に準拠します。

## [1.0.0] - 2025-06-22

### Added

- Docker コンテナのエクスポート / インポート（移行バンドル形式）
- 対話型ターミナルメニュー（GUI）— エクスポート・インポート・バンドル情報
- インポート時の競合検出とキーワード解決（`上書き` / `複製`）
- 複製モード（clone）— 別名・別ポート・別ボリュームで同一 PC 上に並行環境を構築
- `docker-backup/` バンドル形式と `image.tar.gz`（gzip 圧縮イメージ）
- `restore.sh` によるスタンドアロン復元スクリプト
- Linux / macOS / WSL2 向け単一ファイル zipapp ビルド（`build.sh`）
- Windows 向け単一ファイル exe ビルド（PyInstaller、`build-windows.ps1`）
- GitHub Actions による Windows exe の自動ビルド（`v*` タグ push 時）
- CLI: `export`, `import`, `info`, `gui` サブコマンド
- 旧形式 `migration-bundle/` / `image.tar` との後方互換

### Notes

- Python 3.10 以上、標準ライブラリのみ（実行時の pip 依存なし）
- Docker CLI が必須

[1.0.0]: https://github.com/kuwa2005/docker-migrate/releases/tag/v1.0.0
