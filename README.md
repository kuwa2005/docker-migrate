# docker-migrate

Docker コンテナを **別 PC へ移行**したり、**同一 PC 上で dev / prod を並行運用**するための CLI / 対話型ツールです。

実行中（または停止中）のコンテナを入力として、イメージ・ボリューム・設定をひとまとめの **移行バンドル** にエクスポートし、移行先で復元できます。

## こんなときに使う

| 用途 | 説明 |
|------|------|
| **別 PC への移行** | 開発機から本番機、旧サーバーから新サーバーへコンテナ環境ごと持ち運ぶ |
| **同一 PC での複製** | 本番コンテナを `-dev` サフィックス付きで複製し、ポート・ボリュームを分離して並行開発 |

## 必要環境

- Linux / macOS / **Windows（ネイティブ）** / WSL2
- **Docker CLI**（デーモンが起動していること）
  - Windows ネイティブ: [Docker Desktop](https://www.docker.com/products/docker-desktop/) をインストールし、`docker` が PATH にあること
  - WSL2: Docker Desktop の WSL 統合を有効にするか、Linux 側に Docker CLI を用意
- **Python 3.10 以上**（標準ライブラリのみ。追加の `pip install` は不要）
  - Windows `.exe` 配布版を使う場合は Python 不要

## インストール

### 開発フォルダ（リポジトリ clone 後）

```bash
cd docker-migrate
chmod +x docker-migrate
./docker-migrate          # TTY なら対話メニュー起動

# 任意: PATH に追加
sudo ln -sf "$(pwd)/docker-migrate" /usr/local/bin/docker-migrate
```

```
docker-migrate          # 開発用ランチャー
docker_migrate/         # Python パッケージ
build.sh                # 単一ファイル zipapp のビルド（Linux / macOS / WSL）
build-windows.ps1       # Windows 向け .exe ビルド（PyInstaller）
docker-migrate.spec     # PyInstaller 設定
requirements-build.txt  # ビルド用依存（PyInstaller）
```

### スタンドアロン配布（単一ファイル）

別 PC へ **1 ファイルだけ** コピーして使えます。

```bash
./build.sh
# → dist/docker-migrate が生成される

chmod +x dist/docker-migrate
./dist/docker-migrate     # GUI / export / import / info すべて利用可
```

配布例:

```bash
scp dist/docker-migrate user@other-host:~/tools/
ssh user@other-host 'chmod +x ~/tools/docker-migrate && ~/tools/docker-migrate'
```

開発フォルダ（`docker-migrate` + `docker_migrate/`）をそのままコピーしても動作します。どちらの方式も **作業ディレクトリに依存しません**。

### Windows 向け配布

Windows では次の 3 通りがあります（推奨順）。

| 方式 | Python 要否 | 備考 |
|------|------------|------|
| **PyInstaller `.exe`（推奨）** | 不要 | 単一ファイル `docker-migrate.exe`。Docker Desktop 必須 |
| **WSL2 + Linux zipapp** | WSL 内不要* | 既存の `dist/docker-migrate` を WSL で実行。Windows ユーザーに馴染みやすい |
| **Python + 開発フォルダ** | 要 | `python docker-migrate` または clone 後のランチャー |

\* WSL2 には通常 Python 3 が入っています。

#### 単一ファイル exe のビルド（Windows 上）

Linux から Windows 向け exe への **クロスコンパイルは非推奨**（PyInstaller はターゲット OS 上でのビルドが前提）です。Windows PC または GitHub Actions の `windows-latest` ランナーでビルドしてください。

**PowerShell（Windows）:**

```powershell
cd docker-migrate
.\build-windows.ps1
# → dist\docker-migrate.exe が生成される

.\dist\docker-migrate.exe              # TTY なら対話メニュー
.\dist\docker-migrate.exe export my-app -o .\docker-backup\my-app
```

手動でビルドする場合:

```powershell
pip install -r requirements-build.txt
pyinstaller --noconfirm --clean docker-migrate.spec
```

**GitHub Actions（タグ push で自動ビルド）:**

`v*` タグ（例: `v1.0.1`）を push すると `.github/workflows/build-windows.yml` が `docker-migrate.exe` をビルドし、Release に添付します。手動実行は Actions タブの **Build Windows executable** → **Run workflow** でも可能です（Artifact としてダウンロード）。

#### Windows での対話メニュー（GUI）

`cmd.exe` / **PowerShell** / Windows Terminal いずれでも、TTY があれば `input()` ベースの対話メニューは動作します。パイプや CI ではヘルプ表示後に終了（終了コード 2）します。

#### Windows の制限・注意

| 項目 | 説明 |
|------|------|
| Docker Desktop | ネイティブ Windows では必須。WSL2 バックエンド利用を推奨 |
| パス | バインドマウントは `C:\...` と WSL の `/mnt/c/...` で挙動が異なる場合あり。移行元・移行先でパスを確認 |
| `restore.sh` | bash スクリプトのため、ネイティブ cmd では不可。WSL / Git Bash で実行するか、`docker-migrate import` を使用 |
| ウイルス対策 | PyInstaller 単一 exe は誤検知されることがあります。社内配布時は署名や社内スキャン例外の検討を |

#### WSL2 で Linux zipapp を使う場合

```bash
# WSL ターミナル内
./build.sh
./dist/docker-migrate
```

Docker Desktop の「WSL 2 based engine」と対象ディストリビューションの統合を有効にしてください。

## クイックスタート

### エクスポート（移行元）

```bash
./docker-migrate export my-app -o ./docker-backup/my-app
./docker-migrate export my-app -o ./bundle --no-stop      # 停止せず commit
./docker-migrate export my-app -o ./bundle --skip-volumes # 設定のみ
```

エクスポートの既定動作:

1. 実行中なら **一時停止**
2. `docker commit` で現在の状態をイメージ化
3. `docker save` → `image.tar.gz`（gzip 圧縮）
4. 名前付きボリューム・バインドマウントを `volumes/` に保存
5. 設定を `manifest.json` に保存
6. Dockerfile / compose が検出できれば `build-context/` にコピー
7. 停止したコンテナは **自動再開**（`--no-restart` で無効化）

### インポート（移行先）

```bash
./docker-migrate import ./docker-backup/my-app --start
./docker-migrate import ./docker-backup/my-app --name my-app-new --start
./docker-migrate info ./docker-backup/my-app
```

バンドル内の `restore.sh` だけでも復元できます:

```bash
cd docker-backup/my-app
./restore.sh --start
```

## 対話型メニュー（GUI）

引数なしで TTY 上から実行すると、番号選択式の対話メニューが起動します。

```bash
./docker-migrate          # TTY ならメニュー起動
./docker-migrate gui      # 明示的にメニュー起動
```

| メニュー | 内容 |
|---------|------|
| エクスポート | コンテナ一覧 → 出力先（`./docker-backup/<名前>` が既定） → オプション → 確認 → 実行 |
| インポート | バンドル選択 → 内容プレビュー → **競合時はキーワード確認（上書き/複製）** → 確認 → 実行 |
| バンドル情報 | manifest 概要の表示（全文表示も可） |

### 入力のコツ（Enter で既定値）

| プロンプト形式 | Enter の動作 |
|---------------|-------------|
| `[Y/n]` / `[y/N]` | 括弧内の大文字側（Yes/No の既定） |
| `[./docker-backup/my-app]` など | 表示中の既定パスを使用 |
| `[1]` 番号選択 | 推奨番号を選択 |
| 空欄可のテキスト | 空欄のまま確定 |
| **競合解決（インポート）** | Enter/Y/n 不可。`上書き` / `複製`（`overwrite` / `clone` も可）を **そのまま入力** |

エクスポート出力先の親ディレクトリ（`docker-backup/` など）は存在しなければ **自動作成** されます。

TTY でない場合（パイプ・CI 等）はヘルプを表示して終了します（終了コード 2）。CLI コマンドは従来どおり利用できます。

## CLI リファレンス

| コマンド | 説明 |
|---------|------|
| `(引数なし)` / `gui` | 対話型メニューを起動（TTY 必須） |
| `export <container> -o <dir>` | コンテナをバンドルにエクスポート |
| `import <bundle> [options]` | バンドルから復元 |
| `info <bundle>` | バンドル内容を表示 |

### export オプション

| オプション | 説明 |
|-----------|------|
| `--no-stop` | コンテナを停止せずにエクスポート |
| `--no-restart` | 停止したコンテナをエクスポート後に再起動しない |
| `--skip-volumes` | ボリューム / バインドマウントのデータをスキップ |
| `--skip-image` | `image.tar.gz` の作成をスキップ（メタデータのみ） |

### import オプション

| オプション | 説明 |
|-----------|------|
| `--name NAME` | 復元後のコンテナ名（省略時は元の名前） |
| `--start` | 復元後にコンテナを起動 |
| `--force` | 競合時に上書き（`--mode overwrite` と同等） |
| `--mode overwrite` | 競合を上書きで解決 |
| `--mode clone` | 別名・別ポート・別ボリュームで複製 |
| `--clone-suffix SUFFIX` | 複製モードのサフィックス（例: `-dev`, `-prod`） |
| `--port-offset N` | 複製時のポートオフセット（0 = 空きポートを自動探索） |

## インポート時の競合解決

インポート前に、次の競合を自動検出します:

- 同名コンテナの存在
- ホストポートの使用中
- 同名ボリュームの存在
- バインドマウント先パスの競合

### 対話モード（TTY）

競合がある場合、**Y/n ではなくキーワード入力** で解決方法を選びます:

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

複製を選ぶと、サフィックス（既定 `-dev`）とコンテナ名を追加で入力できます（Enter で既定値）。

### 非対話モード（CLI / パイプ）

競合がある場合は **`--mode overwrite` または `--mode clone` が必須** です。未指定だとエラーで終了します。

```bash
# 上書き
./docker-migrate import ./docker-backup/my-app --mode overwrite --start

# 複製（サフィックス・ポートオフセット指定可）
./docker-migrate import ./docker-backup/my-app --mode clone --clone-suffix -dev --start
```

`--force` は `--mode overwrite` と同等です。

### GUI メニューからのインポート

インポート前に競合を検出します。競合がある場合は確認画面の前に **キーワード入力**（`上書き` / `複製`）が表示されます。複製を選ぶとサフィックスとコンテナ名を追加で入力できます（Enter で既定値）。

## 複製モード（clone）の動作

同一 PC 上で本番と開発を並行する典型的な使い方です。

| 項目 | 複製モードの動作 |
|------|-----------------|
| **コンテナ名** | 元名 + サフィックス（例: `my-app` → `my-app-dev`） |
| **ホストポート** | 使用中なら空きポートへ自動リマップ（`--port-offset` で加算も可） |
| **名前付きボリューム** | サフィックス付きの新ボリュームを作成（例: `data` → `data_dev`） |
| **バインドマウント** | `restored-bind-mounts-dev/` など別ディレクトリに展開 |

```bash
# 本番 my-app が 8080 で動いている状態で dev 複製
./docker-migrate export my-app -o ./docker-backup/my-app
./docker-migrate import ./docker-backup/my-app --mode clone --clone-suffix -dev --start
# → my-app-dev が別ポート（例: 8081）で起動、ボリュームも分離
```

## バンドル構成

```
docker-backup/my-app/
├── manifest.json           # 移行メタデータ（設定・ボリューム一覧）
├── container.inspect.json  # docker inspect の完全出力
├── container.env           # 環境変数
├── image.tar.gz            # docker save したイメージ（gzip 圧縮）
├── volumes/                # 名前付きボリューム・バインドマウントの tar.gz
├── build-context/          # 検出された Dockerfile / compose（任意）
├── restored-bind-mounts/   # import 時に展開されるバインドマウントデータ
├── restore.sh              # スタンドアロン復元スクリプト
└── RESTORE.md              # 復元手順（日本語）
```

## 後方互換性

| 項目 | 新形式 | 旧形式（互換） |
|------|--------|---------------|
| バンドル検出ディレクトリ | `docker-backup/` | `migration-bundle/` |
| イメージアーカイブ | `image.tar.gz` | `image.tar`（非圧縮） |

GUI のバンドル自動検出は両方のディレクトリ名をスキャンします。`import` は `manifest.json` の `image.archive` を参照し、なければ `image.tar.gz` → `image.tar` の順で探します。

## 制限事項

| 項目 | 説明 |
|------|------|
| カスタムネットワーク | 移行先で `docker network create` が必要な場合があります |
| バインドマウント | ホストパスは環境依存。`restored-bind-mounts/` または手動配置を確認 |
| ポート競合 | 上書きモードでは移行先のポート競合で起動に失敗することがあります（複製モードは自動リマップ） |
| CPU アーキテクチャ | arm64 / amd64 などの差に注意 |
| Swarm / Kubernetes | オーケストレーション設定は対象外 |
| 外部 DB / API 鍵 | 環境変数に含まれる場合、移行先での再設定が必要なことがあります |
| `restore.sh` | 競合検出・複製モード非対応。シンプルな復元用 |

## 使用例

### 別 PC への移行

```bash
# 1. 移行元でエクスポート
./docker-migrate export my-app -o ./docker-backup/my-app

# 2. 転送
tar czf my-app-bundle.tar.gz -C ./docker-backup my-app
scp my-app-bundle.tar.gz user@other-host:/tmp/

# 3. 移行先で復元
tar xzf my-app-bundle.tar.gz -C /tmp
./docker-migrate import /tmp/my-app --start
```

### 同一 PC で dev 環境を複製

```bash
# 本番 my-app が稼働中
./docker-migrate export my-app -o ./docker-backup/my-app
./docker-migrate import ./docker-backup/my-app --mode clone --clone-suffix -dev --start

docker ps   # my-app (8080) と my-app-dev (8081 等) が並行
```

### 対話メニューで操作

```bash
./docker-migrate
# → 1. エクスポート → コンテナ選択 → Enter で既定パス
# → 2. インポート → 検出バンドル選択 → 競合時は「複製」と入力
```

### デモ用 nginx

```bash
docker run -d --name demo-nginx -p 8080:80 -e APP_ENV=prod nginx:alpine
./docker-migrate export demo-nginx -o /tmp/demo-nginx-bundle
./docker-migrate import /tmp/demo-nginx-bundle --start
```

## 移行の流れ

```
[移行元]                          [移行先]
  docker-migrate export    →  USB / scp / rsync  →  docker-migrate import
  (停止→保存→再開)                                  (load→volume復元→create→start)
```

## ライセンス

MIT
