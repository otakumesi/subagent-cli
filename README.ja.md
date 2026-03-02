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
- 1つの場所からマルチエージェントワークフローを実行 (`worker start` から `worker continue` まで)。
- 厳密な承認操作と構造化イベントフローで、制御を明示的に維持。
- ランタイム再起動 + セッション再開 (`session/load`) でクリーンに復旧。
- ローカルファーストかつスクリプト可能な単一 CLI。

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

4. ここからマネージャーエージェント（Codex / Claude Code）へ handoff します。  
マネージャー側の最初の手順として `subagent prompt render --target manager` を実行させてください。  
以下のテンプレートを使えます:

```text
Act as the project manager for this repository.
Use subagent-cli as the control plane and progress this task by delegating to worker agents.
If worker startup or turn operations fail due to sandbox limits, request approval for out-of-sandbox execution and retry.

Required workflow:
1) First, run `subagent prompt render --target manager` and follow that output.
2) Check command help before execution (for example `subagent worker --help`, `subagent send --help`, `subagent approve --help`).
3) Break the task into small executable chunks.
4) Start/coordinate workers with subagent-cli.
5) Use `send` as the default turn driver (`send` waits by default).
6) If `matchedEvent.type` is `approval.requested`, run `approve` and continue with `send`.
7) Use `watch` only when detailed event streaming/debugging is needed.
8) Use handoff/continue when context gets large.
9) Verify results (tests or checks) before reporting completion.

Task to execute:
<your task here>
```

handoff 後のマネージャーエージェント標準ライフサイクル:
`worker start` -> `send` -> (`approve` -> `send` を必要に応じて繰り返し) -> `handoff` -> `continue`

単発で送信して、終了イベントまたは承認要求まで待機する例:
```bash
subagent send --worker <worker-id> --text "<instruction>" --json
```

待機を無効化する例:
```bash
subagent send --worker <worker-id> --text "<instruction>" --no-wait --json
```

手動待機モード（高度なカーソル制御）:
```bash
subagent wait --worker <worker-id> --until turn_end --timeout-seconds 60 --json
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
- `worker start` が `BACKEND_UNAVAILABLE` で失敗する場合は、`<workspace>/.subagent/state/runtimes/`（既定）または `$SUBAGENT_STATE_DIR/runtimes/`（上書き時）のログを確認してください。
- バックエンド接続なしの簡易テスト:
```bash
subagent worker start --cwd . --debug-mode
```

## 設定 ⚙️
- 解決順: `--config` > `SUBAGENT_CONFIG` > 最寄りの `<cwd-or-parent>/.subagent/config.yaml` > `~/.config/subagent/config.yaml`
- ユーザー設定の生成: `subagent config init --scope user`
- プロジェクト設定の生成: `subagent config init --scope project --cwd .`
- `config init` の既定値: `codex` -> `npx -y @zed-industries/codex-acp`, `claude-code` -> `npx -y @zed-industries/claude-agent-acp`, `gemini` -> `npx -y @google/gemini-cli --experimental-acp`, `opencode` -> `opencode acp`
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
