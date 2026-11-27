# pgadmin-schemadiff-cli

pgAdmin 4 に付属する **Schema Diff エンジン** をヘッドレスで呼び出し、2 つの DDL セット（Old / New）から PostgreSQL 上にスキーマを構築して差分解消用の移行 SQL（Old → New）を出力する Python 3.13 製 CLI ツールおよびライブラリです。

## 特徴と仕組み

- **pgAdmin 4 Schema Diff 完全互換**: 他の差分ツールは使用せず、インストール済みの pgAdmin 4 内部モジュール（`pgadmin.tools.schema_diff` および各オブジェクトの `SchemaDiffRegistry`）をそのまま利用します。
- **読み取り専用・隔離実行**: pgAdmin 4 のインストールディレクトリは一切変更しません。実行時は一時ディレクトリに独立した `config_distro.py` と一時 SQLite 設定 DB を作成し、終了後に自動削除します。
- **柔軟な DDL 入力**: 単一の `.sql` ファイル、ディレクトリ（直下の `.sql` のみをアルファベット昇順に実行・非再帰）、`.zip` アーカイブ（一時展開後にディレクトリ渡しと同様に実行）に対応しています。
- **入力 SQL の並列ストリーミング復元**: Old と New それぞれの初期化・DDL 投入を並列に実行し、各入力セットは全ファイルを1つの `psql` プロセスに順次送信して1つのトランザクションで実行します。途中でエラーが発生すると、その入力セットの変更はロールバックされます。先行するスキーマのクリーンアップは別処理です。
- **実行前の自動クリーンアップ**: DDL を適用する前に、設定ファイルの `search_path` に指定された対象スキーマ内のリソース（テーブル、パーティションテーブル、インデックス、制約、トリガー、ビュー、マテリアライズドビュー、外部テーブル、シーケンス、関数、プロシージャ、集約関数、ドメイン、型、全文検索オブジェクト、照合順序、拡張統計、演算子、拡張機能）をすべて `DROP ... CASCADE` します。

> [!NOTE]
> PyPI 上の `pgadmin4` パッケージは Web アプリケーション本体であり、Schema Diff 単体のライブラリとしては提供されていません。そのため本ツールでは設定ファイルに pgAdmin 4 のインストールパス（`pgadmin.install_path`）を指定して、同梱の Python ランタイムおよびモジュールを利用する方式を採用しています。

## 前提条件

- **Python 3.13**
- **uv**（`mise` 等で管理）
- **pgAdmin 4**（例: `C:\Program Files\PostgreSQL\17\pgAdmin 4`）
- **PostgreSQL サーバー（2 DB または 2 スキーマ）**
  - pgAdmin 4 Schema Diff の仕様上、Old と New の接続先 PostgreSQL は**同じメジャーバージョン**である必要があります。
  - 設定したデータベース・スキーマは実行のたびにリセット（`DROP`）されるため、必ず差分検証用の専用 DB / スキーマを指定してください。
- **`psql`**（Old / New の SQL 入力を復元する場合）
  - pgAdmin または PostgreSQL のインストール先、`pg_dump_path` と同じディレクトリ、`PATH` から、制限モードの修正を含む `psql` を探します。対応版は 14.24、15.19、16.15、17.11、18.6 以降の各マイナー版と、19 以降です。
  - 入力 SQL の `BEGIN`、`COMMIT`、`ROLLBACK` などのトランザクション制御文はエラーになります。`COPY FROM STDIN` のデータ末尾には `\.` が必要です。

## セットアップ

```powershell
# 依存パッケージおよび開発ツール (pytest, pytest-cov) のインストール
uv sync
```

`uv` が `mise` 経由でインストールされており PATH に見つからない場合:

```powershell
mise exec -- uv sync
```

## 設定ファイル (`config.toml`)

サンプルとして [`config.example.toml`](config.example.toml) を同梱しています。

### `[pgadmin]`

| キー | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- |
| `install_path` (別名: `path`) | 必須 | - | pgAdmin 4 のインストールルートパス（`web/pgAdmin4.py` と同梱 Python が含まれる場所） |
| `timeout_seconds` | 任意 | `300` | pgAdmin 4 サブプロセスのタイムアウト秒数 |
| `pg_dump_path` | 任意 | 自動検出 | `--full-dump` で使用する `pg_dump` 実行ファイル（またはディレクトリ）パス。省略時は `pgAdmin 4` の `runtime/` 等や `PATH` から探索します |

### `[ignore]`（比較時の無視設定）

pgAdmin 4 の Compare ボタン横のメニューに対応します。

| キー | デフォルト | 説明 |
| --- | --- | --- |
| `owner` | `false` | 所有者（Owner）の違いを無視する |
| `whitespace` (別名: `whitespaces`) | `false` | 空白・タブ・改行の違いを無視する |
| `tablespace` | `false` | テーブルスペースの違いを無視する |
| `grants` | `false` | 権限（`GRANT` / `REVOKE`）の違いを無視する |

### `[filter]`（別名: `[output_filter]`、出力フィルタ設定）

pgAdmin 4 の Filter メニューに対応します。pgAdmin 4 の Schema Diff は「Target を Source に合わせる SQL」を生成するため、本ツールでは **Source = New**、**Target = Old** として比較します。

| キー | デフォルト | 説明 |
| --- | --- | --- |
| `source_only` | `true` | New 側にのみ存在するオブジェクト（`CREATE` 系）を含める |
| `target_only` | `true` | Old 側にのみ存在するオブジェクト（`DROP` 系）を含める |
| `different` | `true` | 定義が異なるオブジェクト（`ALTER` / 再作成系）を含める |
| `same` (別名: `identical`) | `false` | 同一のオブジェクトを結果リスト（`--format json` / Python API の `items`）に含める（※SQL 出力には変更クエリが無いため出力されません） |

### `[tweak]`（SQL 後処理設定）

`pglast`（PostgreSQL パーサ / レキサ）を用いて出力 SQL やフルダンプを整形・フィルタします。CLI の `--tweak` オプションで上書き可能です。

| キー | デフォルト | 説明 |
| --- | --- | --- |
| `omit_pg_attver` | `false` | カラムの属性バージョン管理用の `pg_attver` オプション（`ALTER COLUMN ... SET/RESET (pg_attver...)`）を差分クエリから除去する |
| `comment_meta_restrict` | `false` | `--full-dump` の出力に含まれるトップレベルの `\restrict` / `\unrestrict` メタコマンドを `--` でコメントアウトする |

### `[databases.old]` / `[databases.new]`（接続先 DB 設定）

| キー | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- |
| `host` | 必須 | - | PostgreSQL ホスト名 |
| `port` | 任意 | `5432` | ポート番号 |
| `database` (別名: `dbname`) | 任意 | `"postgres"` | データベース名 |
| `user` (別名: `username`) | 必須 | - | 接続ユーザー名 |
| `password` | 任意 | `""` | パスワード（`password_env` と排他） |
| `password_env` | 任意 | - | パスワードを取得する環境変数名（`password` と排他） |
| `search_path` | 任意 | `"public"` | 実行時の `search_path`。`$user` を除く先頭のスキーマがリセットおよび比較対象になります（例: `"app, public"` の場合は `app` が対象、未作成なら自動作成） |
| `sslmode` | 任意 | `"prefer"` | SSL 接続モード |

> [!WARNING]
> `[databases.old]` と `[databases.new]` でホスト・ポート・データベース名・主スキーマ（`search_path` の先頭具体スキーマ）がすべて同一になる設定はエラーとして拒否されます。

## CLI としての使い方

```powershell
# 基本的な実行（標準出力に Old -> New の移行 SQL を出力）
uv run pgadmin-schemadiff --config config.toml --old ./ddl/old.sql --new ./ddl/new.sql

# ディレクトリを指定（直下の .sql のみをアルファベット昇順で実行・非再帰）
uv run pgadmin-schemadiff -c config.toml --old ./ddl/v1_dir --new ./ddl/v2_dir

# zip アーカイブを指定（展開して直下の .sql をアルファベット昇順で実行）
uv run pgadmin-schemadiff -c config.toml --old ./v1.zip --new ./v2.zip > migration.sql

# JSON 形式で差分詳細・サマリ付きで出力
uv run pgadmin-schemadiff -c config.toml --old ./old --new ./new --format json

# 差分抽出後に Target (Old DB) へ移行 SQL を適用する
uv run pgadmin-schemadiff -c config.toml --old ./old --new ./new --apply

# 差分抽出後に Source (New DB) のスキーマのみダンプを出力する（出力先省略時は full_dump.sql）
uv run pgadmin-schemadiff -c config.toml --old ./old --new ./new --full-dump
uv run pgadmin-schemadiff -c config.toml --old ./old --new ./new --full-dump ./out/schema.sql

# --apply と --full-dump の併用
uv run pgadmin-schemadiff -c config.toml --old ./old --new ./new --full-dump ./out/schema.sql --apply

# --tweak で pg_attver の除去や \restrict / \unrestrict のコメントアウトを有効化
uv run pgadmin-schemadiff -c config.toml --old ./old --new ./new --full-dump --tweak omit_pg_attver --tweak comment_meta_restrict
uv run pgadmin-schemadiff -c config.toml --old ./old --new ./new --full-dump --tweak omit_pg_attver,comment_meta_restrict

# config.toml の [tweak] 設定を CLI から無効化する場合
uv run pgadmin-schemadiff -c config.toml --old ./old --new ./new --tweak omit_pg_attver=false --tweak no_comment_meta_restrict
```

### 追加オプションの詳細

- `--apply`: スキーマ差分抽出後、生成された移行 SQL を Target（`[databases.old]`）へ適用します。適用に失敗した場合でも、抽出された差分 SQL / JSON は標準出力に出力したうえで標準エラー出力にエラーを表示し、終了コード `1` で終了します。
- `--full-dump [FILE]`: スキーマ差分抽出後、`pg_dump --schema-only` を用いて Source（`[databases.new]`）の比較対象主スキーマのみのダンプファイルを作成します。`[ignore]` 設定（`owner` → `--no-owner`、`tablespace` → `--no-tablespaces`、`grants` → `--no-privileges`）がダンプ出力にも反映されます。`FILE` を省略した場合は `full_dump.sql` に出力します。
- `--tweak TWEAK`: `pglast` を用いた SQL 後処理を有効化または上書きします（複数回指定・カンマ区切り可）。指定可能な値は `omit_pg_attver`・`comment_meta_restrict` で、`<name>=false` や `no_<name>` で設定ファイルの指定を無効化できます。

`python -m pgadmin_schemadiff` としても実行できます。実行中の進捗ステップ（`[1/N] ...` や `Done.`）は標準エラー出力（`stderr`）に出力されるため、標準出力（`stdout`）の SQL / JSON 出力には混ざりません。

終了コード:
- `0`: 正常終了
- `1`: DDL 読み込みエラー、PostgreSQL 接続・実行エラー、pgAdmin 4 実行エラー、`pg_dump` エラー、または `--apply` 適用失敗
- `2`: コマンドライン引数または設定ファイル（`ConfigError`）エラー

## Python ライブラリとしての使い方

```python
from pgadmin_schemadiff import SchemaDiffClient, load_config, run_schema_diff

# 1. 簡易関数で一括実行（必要に応じて apply / full_dump も指定可能）
result = run_schema_diff(
    config="config.toml",
    old_ddl="path/to/old_ddl_dir",
    new_ddl="path/to/new_ddl.zip",
    apply=False,
    full_dump=None,  # True で "full_dump.sql"、パス文字列/Path で出力先指定
)
print(result.script)
for item in result.items:
    print(item.node_type, item.title, item.status)

# 2. Config オブジェクトとクライアントを明示的に使う場合（progress コールバック指定可）
config = load_config("config.toml")
client = SchemaDiffClient(config, progress=print)
result = client.diff_ddls("old.sql", "new.sql", include_identical_ddl=True)
```

## テストとカバレッジ確認

```powershell
mise exec -- uv run pytest --cov=pgadmin_schemadiff --cov-report=term-missing
```

（実 PostgreSQL サーバーへの接続をモックした高速な単体テスト一式が `tests/` 配下に含まれています。）
