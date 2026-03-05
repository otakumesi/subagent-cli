# subagent-cli

Language: [English](README.md) | **日本語**

[![PyPI version](https://img.shields.io/pypi/v/subagent-cli)](https://pypi.org/project/subagent-cli/)
[![Python versions](https://img.shields.io/pypi/pyversions/subagent-cli)](https://pypi.org/project/subagent-cli/)
[![License](https://img.shields.io/github/license/otakumesi/subagent-cli)](LICENSE)
[![Publish to PyPI](https://github.com/otakumesi/subagent-cli/actions/workflows/publish-pypi.yml/badge.svg)](https://github.com/otakumesi/subagent-cli/actions/workflows/publish-pypi.yml)
[![Status: Alpha](https://img.shields.io/badge/status-alpha-orange)](https://pypi.org/project/subagent-cli/)

別のコーディングエージェントから、安全かつ明示的にコーディングエージェントをオーケストレーションします。  
`subagent-cli` は、マネージャー役のコーディングエージェント（例: Codex / Claude Code）を、worker の起動、ターン送信、承認処理、handoff/continue を行うための実用的なコントロールプレーンにします。🤖

コマンドインターフェースはプロトコル非依存で、現在のランタイムバックエンドは ACP (`acp-stdio`) です。

## subagent-cli を使う理由 🚀
複数のコーディングエージェントを協調させるのは、想像以上に難しい課題です。
そのため多くのツールでは、`/subagent` のような単一コマンドに操作を集約しています。
ただ、ユースケースによっては、役割や目的ごとに使うエージェントを分けたい場面があります。

subagent-cli は、その分離を実現するためのツールです。
責務を分け、プロトコル（ACP）を通して通信させることで、複数エージェントの協調をシンプルかつ明示的に実現します。

たとえば Claude Code を管理役として、コードレビューには Codex、実装には Gemini を割り当てられます。

## 具体的なユースケース 🧪
- flaky test investigation: 再現安定化、原因切り分け、修正案作成を複数 worker に分担させる。
- parallel code review / research workers: レビュー担当と調査担当の worker を並列実行し、manager ターンで結果を統合する。
- parent crash -> handoff -> continue: 親エージェント中断時に handoff コンテキストから再開し、最初からやり直さない。

注: クイックスタートのサンプルプロンプトは project manager 役を前提にしています。上記ユースケースで使う場合は、manager の役割定義と worker 向けプロンプトを用途に合わせて調整してください。

## 現在のスコープ 🧭
- Alpha (`v0.1.x`)
- ローカル単一ホスト向け
- Python 3.11+

## 機能 ✨
- Worker ライフサイクル: `start`, `list`, `show`, `inspect`, `stop`
- ターン操作: `send`, `watch`, `wait`, `approve`, `cancel`
- Handoff ワークフロー: `worker handoff` と `worker continue`
- 構造化イベントに基づく厳密な承認フロー
- ACP ランタイム統合 (`acp-stdio`)

## インストール 📦
`uv` を使う場合（推奨）:
```bash
uv tool install subagent-cli
```

PyPI から:
```bash
pip install subagent-cli
```

ローカルビルド成果物から:
```bash
pip install dist/subagent_cli-*.whl
```

## ターミナルデモ 🎬
録画した asciinema セッション:
- https://asciinema.org/a/Cxfk7i9wOOpZB17F

## クイックスタート ⚡
1. ローカル設定ファイルを初期化します。
```bash
subagent config init --scope user
```

2. `~/.config/subagent/config.yaml` の launcher command/args/env を設定します。

3. ワークスペースで controller を初期化します（ワークスペースごとに1回）。
```bash
subagent controller init --cwd .
```

4. （このリポジトリを GitHub に push 後）推奨の manager スキルをインストールします。
```bash
npx skills add "github/otakumesi/subagent-cli"
```

5. ここからマネージャーエージェント（Codex / Claude Code）へ handoff します。  
以下のような指示文を使えます:

```text
<what to do that you want>

Use subagent commands to delegate tasks to a team of agents and coordinate their progress.
Use the "subagent-manager" skill.
```

handoff 後のマネージャーエージェント標準ライフサイクル:
`worker start` -> `send` -> (`approve` -> `send` を必要に応じて繰り返し) -> `handoff` -> `continue`

単発で送信して、終了イベントまたは承認要求まで待機する例:
```bash
subagent send --worker-id <worker-id> --text "<instruction>" --json
```

待機を無効化する例:
```bash
subagent send --worker-id <worker-id> --text "<instruction>" --no-wait --json
```

複数行やシェル解釈事故を避ける安全な入力方法（推奨）:
```bash
cat > instruction.txt <<'TEXT'
`echo hello` や $(uname) を文字列として扱い、実行しないこと。
TEXT
subagent send --worker-id <worker-id> --text-file ./instruction.txt --json
```

stdin 版:
```bash
cat ./instruction.txt | subagent send --worker-id <worker-id> --text-stdin --json
```

高度な自動化では、構造化 JSON 入力 `--input` も使えます（JSON キーは `workerId`、CLI フラグは `--worker-id`）。

手動待機モード（高度なカーソル制御）:
```bash
subagent wait --worker-id <worker-id> --until turn_end --timeout-seconds 60 --json
```

履歴イベントも対象に含める場合:
```bash
subagent wait --worker-id <worker-id> --include-history --until turn_end --timeout-seconds 60 --json
```

実 launcher なしでローカルシミュレーションする場合:
```bash
subagent worker start --cwd . --debug-mode
```

## トラブルシューティング 🛠️
- ランタイムと manager/worker 双方のサンドボックスが launcher の要件を満たしているか確認してください。
- launcher によっては外向きネットワークが必要ですが、エージェントのサンドボックスポリシーが通信を遮断する場合があります。
- state パス解決に失敗する場合は、ワークスペースルート配下で実行するか `SUBAGENT_STATE_DIR` を明示してください。
- launcher の事前確認:
```bash
subagent launcher probe <launcher-name> --json
```
- `worker start` がバックエンド系エラー（例: `BACKEND_TIMEOUT` / `BACKEND_SOCKET_UNREACHABLE` / `BACKEND_LAUNCHER`）で失敗する場合は、`<workspace>/.subagent/state/runtimes/`（既定）または `$SUBAGENT_STATE_DIR/runtimes/`（上書き時）のログを確認してください。
- バックエンド接続なしの簡易テスト:
```bash
subagent worker start --cwd . --debug-mode
```

## 設定 ⚙️
- 解決順: `--config` > `SUBAGENT_CONFIG` > 最寄りの `<cwd-or-parent>/.subagent/config.yaml` > `~/.config/subagent/config.yaml`
- ユーザー設定の生成: `subagent config init --scope user`
- プロジェクト設定の生成: `subagent config init --scope project --cwd .`
- `config init` の既定値: `codex` -> `npx -y @zed-industries/codex-acp`, `claude-code` -> `npx -y @zed-industries/claude-agent-acp`, `gemini` -> `npx -y @google/gemini-cli --experimental-acp`, `opencode` -> `opencode acp`, `cline` -> `npx -y cline --acp`, `github-copilot` -> `npx -y @github/copilot-language-server --acp`, `kiro` -> `npx -y @kirodotdev/cli acp`
- 設定パス上書き: `SUBAGENT_CONFIG=/path/to/config.yaml`
- 設定例: [config.example.yaml](config.example.yaml)
- launcher は split 形式（`command: npx`, `args: ["-y", "..."]`）と inline 形式（`command: "npx -y ..."`）の両方に対応します。

## State 💾
- 既定 state DB: `<workspace>/.subagent/state/state.db`
- state dir 上書き: `SUBAGENT_STATE_DIR=/path/to/state-dir`
- ワークスペースルートが検出できず、上書きもない場合は `WORKSPACE_ROOT_NOT_FOUND` で失敗します。
- プロジェクトヒントファイル: `<workspace>/.subagent/controller.json`

## ドキュメント 📚
- アーキテクチャ: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 例: [docs/examples](docs/examples)
- 開発/リリース手順: [CONTRIBUTING.md](CONTRIBUTING.md)

## ライセンス
MIT ([LICENSE](LICENSE))
