# docker-migrate 使い方ガイド

**バージョン 1.0.0**

Docker コンテナを別 PC へ移行したり、同一 PC 上で dev / prod を並行運用するためのツールです。本ドキュメントはインストールから日常運用、トラブルシューティングまでを網羅しています。

---

## 目次

1. [概要](#概要)
2. [必要環境](#必要環境)
3. [インストール](#インストール)
4. [対話型メニュー（GUI）](#対話型メニューgui)
5. [CLI リファレンス](#cli-リファレンス)
6. [同一 PC での複製（clone）](#同一-pc-での複製clone)
7. [別 PC への移行](#別-pc-への移行)
8. [バンドル構成](#バンドル構成)
9. [競合解決](#競合解決)
10. [トラブルシューティング](#トラブルシューティング)
11. [制限事項](#制限事項)

---

## 概要

`docker-migrate` は実行中（または停止中）の Docker コンテナを入力として、以下をひとまとめの **移行バンドル** にエクスポートし、移行先で復元できます。

| 含まれるもの | 説明 |
|-------------|------|
| イメージ | `docker commit` + `docker save` → `image.tar.gz` |
| ボリューム | 名前付きボリューム・バインドマウントのデータ |
| 設定 | `manifest.json`、`container.inspect.json`、環境変数 |
| ビルドコンテキスト | Dockerfile / compose が検出できれば `build-context/` にコピー |
| 復元スクリプト | `restore.sh`（bash 向けスタンドアロン復元） |

### 主な用途

| 用途 | 説明 |
|------|------|
| **別 PC への移行** | 開発機 → 本番機、旧サーバー → 新サーバー |
| **同一 PC での複製** | 本番コンテナを `-dev` サフィックス付きで複製し、ポート・ボリュームを分離 |

---

## 必要環境

| 項目 | 要件 |
|------|------|
| OS | Linux / macOS / Windows（ネイティブ）/ WSL2 |
| Docker | Docker CLI が利用可能で、デーモンが起動していること |
| Python | **3.10 以上**（配布版 zipapp / exe を使う場合は不要） |

### Windows の場合

- **ネイティブ Windows**: [Docker Desktop](https://www.docker.com/products/docker-desktop/) 必須。`docker` が PATH にあること
- **WSL2**: Docker Desktop の WSL 統合を有効にするか、Linux 側に Docker CLI を用意

---

## インストール

### 方法 A: GitHub Release から取得（推奨）

[Releases](https://github.com/kuwa2005/docker-migrate/releases) から環境に合ったファイルをダウンロードします。

| ファイル | 対象環境 | Python 要否 |
|---------|---------|------------|
| `docker-migrate` | Linux / macOS / WSL2 | 不要（zipapp、実行権限が必要） |
| `docker-migrate.exe` | Windows ネイティブ | 不要 |
| `USAGE.md` | ドキュメント（本ファイル） | — |

**Linux / WSL2:**

```bash
chmod +x docker-migrate
./docker-migrate          # TTY なら対話メニュー起動
```

**Windows（PowerShell / cmd）:**

```powershell
.\docker-migrate.exe
.\docker-migrate.exe export my-app -o .\docker-backup\my-app
```

任意で PATH に追加:

```bash
# Linux / macOS
sudo ln -sf "$(pwd)/docker-migrate" /usr/local/bin/docker-migrate
```

### 方法 B: リポジトリ clone（開発モード）

```bash
git clone https://github.com/kuwa2005/docker-migrate.git
cd docker-migrate
chmod +x docker-migrate
./docker-migrate
```

追加の `pip install` は不要です（標準ライブラリのみ使用）。

### 方法 C: 自分でビルド

**Linux / macOS / WSL2（zipapp）:**

```bash
./build.sh
# → dist/docker-migrate が生成される
chmod +x dist/docker-migrate
./dist/docker-migrate
```

**Windows（exe）:**

```powershell
.\build-windows.ps1
# → dist\docker-migrate.exe が生成される
```

手動ビルド:

```powershell
pip install -r requirements-build.txt
pyinstaller --noconfirm --clean docker-migrate.spec
```

> **注意:** PyInstaller はターゲット OS 上でのビルドが前提です。Linux から Windows 向け exe のクロスコンパイルは非推奨です。`v*` タグを push すると GitHub Actions が Windows exe を自動ビルドし Release に添付します。

---

## 対話型メニュー（GUI）

引数なしで TTY 上から実行すると、番号選択式の対話メニューが起動します。

```bash
./docker-migrate          # TTY ならメニュー起動
./docker-migrate gui      # 明示的にメニュー起動
```

### メニュー構成

| 番号 | 機能 | 流れ |
|------|------|------|
| 1 | **エクスポート** | コンテナ一覧 → 出力先 → オプション → 確認 → 実行 |
| 2 | **インポート** | バンドル選択 → 内容プレビュー → 競合解決 → 確認 → 実行 |
| 3 | **バンドル情報** | manifest 概要の表示（全文表示も可） |

### 入力のコツ（Enter で既定値）

| プロンプト形式 | Enter の動作 |
|---------------|-------------|
| `[Y/n]` / `[y/N]` | 括弧内の大文字側（Yes/No の既定） |
| `[./docker-backup/my-app]` など | 表示中の既定パスを使用 |
| `[1]` 番号選択 | 推奨番号を選択 |
| 空欄可のテキスト | 空欄のまま確定 |

### エクスポート（GUI）

1. メニューで **1. エクスポート** を選択
2. 実行中のコンテナ一覧から番号で選択
3. 出力先を入力（既定: `./docker-backup/<コンテナ名>`）。親ディレクトリは自動作成
4. オプションを確認:
   - 停止せずにエクスポート（`--no-stop` 相当）
   - ボリュームをスキップ（`--skip-volumes` 相当）
   - イメージをスキップ（`--skip-image` 相当）
5. 確認後に実行

エクスポートの既定動作:

1. 実行中なら **一時停止**
2. `docker commit` で現在の状態をイメージ化
3. `docker save` → `image.tar.gz`（gzip 圧縮）
4. 名前付きボリューム・バインドマウントを `volumes/` に保存
5. 設定を `manifest.json` に保存
6. Dockerfile / compose が検出できれば `build-context/` にコピー
7. 停止したコンテナは **自動再開**

### インポート（GUI）と競合解決キーワード

1. メニューで **2. インポート** を選択
2. `docker-backup/` または `migration-bundle/` 内のバンドルを選択
3. バンドル内容のプレビューを確認
4. **競合がある場合**、確認画面の前にキーワード入力が表示されます

```
競合が検出されました:
- コンテナ 'my-app' が存在
- ポート 8080 が使用中 (other-container)

上書きする場合は「上書き」、複製する場合は「複製」と入力:
> 複製
```

| 入力 | 動作 |
|------|------|
| `上書き` / `overwrite` | 既存コンテナ・ボリュームを上書き |
| `複製` / `clone` | 別名・別ポート・別ボリュームで複製 |

> **重要:** 競合解決では **Enter / Y/n は使えません**。キーワードをそのまま入力してください。

複製を選ぶと、サフィックス（既定 `-dev`）とコンテナ名を追加で入力できます（Enter で既定値）。

5. 最終確認後にインポート実行

### TTY でない場合

パイプ・CI 等ではヘルプを表示して終了します（終了コード 2）。CLI コマンドは従来どおり利用できます。

---

## CLI リファレンス

### コマンド一覧

| コマンド | 説明 |
|---------|------|
| `(引数なし)` / `gui` | 対話型メニューを起動（TTY 必須） |
| `export <container> -o <dir>` | コンテナをバンドルにエクスポート |
| `import <bundle> [options]` | バンドルから復元 |
| `info <bundle>` | バンドル内容を表示 |
| `--version` | バージョン表示 |

### export

```bash
./docker-migrate export my-app -o ./docker-backup/my-app
./docker-migrate export my-app -o ./bundle --no-stop      # 停止せず commit
./docker-migrate export my-app -o ./bundle --skip-volumes # 設定のみ
./docker-migrate export my-app -o ./bundle --skip-image   # メタデータのみ
./docker-migrate export my-app -o ./bundle --no-restart   # 停止後に再起動しない
```

| オプション | 説明 |
|-----------|------|
| `--no-stop` | コンテナを停止せずにエクスポート |
| `--no-restart` | 停止したコンテナをエクスポート後に再起動しない |
| `--skip-volumes` | ボリューム / バインドマウントのデータをスキップ |
| `--skip-image` | `image.tar.gz` の作成をスキップ |

### import

```bash
./docker-migrate import ./docker-backup/my-app --start
./docker-migrate import ./docker-backup/my-app --name my-app-new --start
./docker-migrate import ./docker-backup/my-app --mode overwrite --start
./docker-migrate import ./docker-backup/my-app --mode clone --clone-suffix -dev --start
```

| オプション | 説明 |
|-----------|------|
| `--name NAME` | 復元後のコンテナ名 |
| `--start` | 復元後にコンテナを起動 |
| `--force` | 競合時に上書き（`--mode overwrite` と同等） |
| `--mode overwrite` | 競合を上書きで解決 |
| `--mode clone` | 別名・別ポート・別ボリュームで複製 |
| `--clone-suffix SUFFIX` | 複製モードのサフィックス（例: `-dev`） |
| `--port-offset N` | ポートオフセット（0 = 空きポートを自動探索） |

### info

```bash
./docker-migrate info ./docker-backup/my-app
```

manifest の概要（コンテナ名、イメージ、ポート、ボリューム等）を表示します。

---

## 同一 PC での複製（clone）

本番コンテナをそのまま残しつつ、開発用の複製環境を作る典型的なワークフローです。

```bash
# 本番 my-app が 8080 で稼働中
./docker-migrate export my-app -o ./docker-backup/my-app
./docker-migrate import ./docker-backup/my-app --mode clone --clone-suffix -dev --start

docker ps   # my-app (8080) と my-app-dev (8081 等) が並行
```

### 複製モードの動作

| 項目 | 動作 |
|------|------|
| **コンテナ名** | 元名 + サフィックス（例: `my-app` → `my-app-dev`） |
| **ホストポート** | 使用中なら空きポートへ自動リマップ |
| **名前付きボリューム** | サフィックス付きの新ボリューム（例: `data` → `data_dev`） |
| **バインドマウント** | `restored-bind-mounts-dev/` など別ディレクトリに展開 |

対話メニューからインポートする場合は、競合検出後に「複製」と入力すれば同じ動作になります。

---

## 別 PC への移行

```bash
# === 移行元 ===
./docker-migrate export my-app -o ./docker-backup/my-app

# バンドルをアーカイブして転送
tar czf my-app-bundle.tar.gz -C ./docker-backup my-app
scp my-app-bundle.tar.gz user@other-host:/tmp/

# === 移行先 ===
tar xzf my-app-bundle.tar.gz -C /tmp
./docker-migrate import /tmp/my-app --start
```

`docker-migrate` 実行ファイル自体も移行先にコピーしてください（Release の zipapp / exe、または開発フォルダ）。

---

## バンドル構成

エクスポート後の `docker-backup/<名前>/` ディレクトリ構成:

```
docker-backup/my-app/
├── manifest.json           # 移行メタデータ（設定・ボリューム一覧）
├── container.inspect.json  # docker inspect の完全出力
├── container.env           # 環境変数
├── image.tar.gz            # docker save したイメージ（gzip 圧縮）
├── volumes/                # 名前付きボリューム・バインドマウントの tar.gz
├── build-context/          # 検出された Dockerfile / compose（任意）
├── restored-bind-mounts/   # import 時に展開されるバインドマウントデータ
├── restore.sh              # スタンドアロン復元スクリプト（bash）
└── RESTORE.md              # 復元手順（日本語）
```

### 後方互換性

| 項目 | 新形式 | 旧形式（互換） |
|------|--------|---------------|
| バンドル検出ディレクトリ | `docker-backup/` | `migration-bundle/` |
| イメージアーカイブ | `image.tar.gz` | `image.tar`（非圧縮） |

### restore.sh による復元

競合検出・複製モードは **非対応** ですが、シンプルな復元には使えます:

```bash
cd docker-backup/my-app
./restore.sh --start
```

Windows ネイティブの cmd では bash がないため、`docker-migrate import` を使用するか、WSL / Git Bash で `restore.sh` を実行してください。

---

## 競合解決

インポート前に以下を自動検出します:

- 同名コンテナの存在
- ホストポートの使用中
- 同名ボリュームの存在
- バインドマウント先パスの競合

### 対話モード（TTY / GUI）

キーワード入力で解決方法を選択（上記「インポート（GUI）」参照）。

### 非対話モード（CLI）

競合がある場合は **`--mode overwrite` または `--mode clone` が必須** です:

```bash
# 上書き
./docker-migrate import ./docker-backup/my-app --mode overwrite --start

# 複製
./docker-migrate import ./docker-backup/my-app --mode clone --clone-suffix -dev --start
```

未指定だとエラーで終了します。`--force` は `--mode overwrite` と同等です。

---

## トラブルシューティング

### `Error: docker コマンドが見つかりません`

- Docker CLI がインストールされ、PATH に含まれているか確認
- Windows: Docker Desktop が起動しているか確認
- WSL2: Docker Desktop の WSL 統合が有効か確認

### エクスポート後にコンテナが起動しない

- `--no-restart` を指定していないか確認
- `docker ps -a` でコンテナの状態を確認し、手動で `docker start <name>` を試す

### インポート時にポート競合で起動失敗

- **上書きモード**: 移行先で同じポートが使用中だと起動に失敗することがあります
- **複製モード**: 空きポートへ自動リマップされます（`--mode clone` を使用）

### バインドマウントのパスが見つからない

- ホストパスは環境依存です。`restored-bind-mounts/` 内のデータを確認
- Windows と Linux でパス形式が異なる場合があります（`C:\...` vs `/mnt/c/...`）

### CPU アーキテクチャの不一致

- arm64 でエクスポートしたイメージを amd64 で実行（またはその逆）は失敗します
- 移行元・移行先のアーキテクチャを `docker info` で確認

### Windows exe がウイルス対策ソフトにブロックされる

- PyInstaller 単一 exe は誤検知されることがあります
- 社内配布時はコード署名やスキャン例外の検討を

### 対話メニューが起動しない（終了コード 2）

- TTY が必要です。パイプや CI では CLI コマンドを直接使用してください
- Windows Terminal / PowerShell / cmd では通常動作します

### `restore.sh` で復元できない

- bash が必要です（WSL / Git Bash）
- 競合がある環境では `docker-migrate import --mode ...` を使用してください

---

## 制限事項

| 項目 | 説明 |
|------|------|
| カスタムネットワーク | 移行先で `docker network create` が必要な場合があります |
| バインドマウント | ホストパスは環境依存。手動配置の確認が必要なことがあります |
| ポート競合 | 上書きモードでは移行先のポート競合で起動失敗の可能性 |
| CPU アーキテクチャ | arm64 / amd64 などの差に注意 |
| Swarm / Kubernetes | オーケストレーション設定は対象外 |
| 外部 DB / API 鍵 | 環境変数に含まれる場合、移行先での再設定が必要なことがあります |
| `restore.sh` | 競合検出・複製モード非対応 |

---

## ライセンス

MIT License — 詳細はリポジトリの README を参照してください。
