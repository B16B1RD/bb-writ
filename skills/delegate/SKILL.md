---
name: delegate
description: 子に委任するとき、別系統の CLI（Claude Code・Codex・Grok Build）を bb-writ 専用のログインで起動するとき、非対話実行や再開の呼び出し方が要るときに使う。ホストごとの接続点。
---

# 委任と、ホストごとの接続点

正本は SKILL.md と参照ファイルで、どのホストでも同じものを読む。bb-writ の hook やカスタム subagent 定義に必須の機能を置かない。ここに書くのは、委任・非対話実行・再開・権限の、ホストごとに違う呼び出し方だけ。版ごとに実測し、確かめた日を書く（2026-10-17: Claude Code 2.1.302、Codex CLI 0.163.0、Grok Build 1.0.60）。

## 共通

- **委任に渡すもの:** 目的、判定対象（何を見て何を判断するか）、変更できる範囲、許可された操作、報告の形（成果と証拠の場所、結論、残る不明点）。子の「できました」を読み替えず、実物を見る。
- **モデルと effort:** 課題の難しさで選ぶ。読む・集める・定型の編集は軽い段（Claude Code: `haiku`・`sonnet`、Codex: `gpt-6-luna`・`gpt-6-sol`）、設計の是非・重要な検証・行き詰まりの判断は重い段（`opus`・`fable`、`gpt-6.1-sol`・`gpt-6-astra`）。実効値（実際に使われたモデル・effort）が確認できなければ「不明」と書く。
- **別系統の判断:** 設計の是非や重要な検証では、もう一方の CLI を bb-writ 専用のログインで起動する。同じ系統の名前を変えて呼んだだけで「別モデルで確認した」としない。片方しか使えない場所では、新しい文脈の同系統で進め、独立性の差を記録する。
- **専用ログイン:** Claude Code は `~/.config/bb-writ/.env` の `CLAUDE_CODE_OAUTH_TOKEN`（dotenvx で暗号化。`dotenvx run -f ~/.config/bb-writ/.env -fk ~/.config/bb-writ/.env.keys -- claude ...`）と `CLAUDE_CONFIG_DIR=~/.config/bb-writ/claude-home`。Codex は `CODEX_HOME=~/.config/bb-writ/codex-home`。Grok Build は `~/.config/bb-writ/grok-auth`。オーナーの `~/.claude`・`~/.codex`・`~/.grok` の写しを作らない。トークンを画面・ログ・会話に出さない。
- **権限:** 親・組織・対象リポジトリの制約を委任で広げない。読み取り専用を保証したい経路は、ツール名の列挙でなく実際の書き込み拒否で試す。

## launcher（別系統の子の起動と報告の検査）

`scripts/launch.py`（プラグインの置き場の下。Claude Code では `${CLAUDE_PLUGIN_ROOT}/scripts/launch.py`、Codex ではこのスキルの SKILL.md から 2 つ上の `scripts/launch.py`）。

```bash
python3 <launch.py> --host codex|claude --cwd <対象> --prompt-file <依頼文> [--model M] [--effort E] [--write]
```

- 既定は読み取り専用（Codex は `-s read-only`、Claude Code は `--permission-mode dontAsk` で Read・Grep・Glob と git の読み取りだけを許す）。変更を任せるときだけ `--write`。
- 子に、最後に結論・証拠・不明点の JSON を書かせる。標準出力の 1 行の JSON の `status` が `ok` のときだけ報告を使う。`invalid-report`・`failed`・`quota`・`unavailable` は理由（`problems`）を記録して扱う。JSON が返ったことを成功にしない。
- 実効のモデル・effort を記録する（Claude Code は結果の `modelUsage`、Codex は記録の `turn_context`）。記録は `~/.local/state/bb-writ/delegations/` と `delegations.jsonl`。

## 担当の貸し出しと外部操作の記録

`scripts/work.py`（launcher と同じ置き場）。子に書く仕事を任せるときは、親が担当を持ったままにせず、子に `claim` させる（同じ作業場所に親と子が同時に書かない）。使い方は入口 `bb-writ` の「離席・中断・訂正」。

外部操作（push・PR・コメントなど）は元の checkout から実行し、`op begin --from <元の checkout>` で確かめる。成果物を出す操作は `--verified <検証した commit>` も付ける。子に頼むときも同じ。`work.py check --dir <作業場所> --from <実行する場所>` で、そこで使われる GitHub のアカウントと書き込み権限を事前に見られる。子の起動に `GH_CONFIG_DIR` や `GH_TOKEN` を足してアカウントを決めない（利用者の環境の切り替えを上書きし、別の名前で外に出る）。

## Claude Code

- **同じ系統の委任:** `Agent` ツール（新しい文脈。読み取り専用の探索は Explore 型）。
- **非対話実行:** `claude -p "<依頼>" --output-format json --model <m> --effort <low|medium|high|xhigh|max> < /dev/null`。手元の作業ツリーのプラグインを読み込むには `--plugin-dir <ディレクトリ>`。入口の明示は `/bb-writ:bb-writ <依頼>`（普通の依頼文だけでは入口が使われない。実測 2026-10-17）。確認を求める操作を自動で拒否するなら `--permission-prompts none`、許可の方針は `--permission-mode <acceptEdits|auto|bypassPermissions|manual|dontAsk|plan>`。子の起動に `--dangerously-skip-permissions` を付けると、親の auto mode の分類器が起動を拒否した。`--permission-mode auto --permission-prompts none` は通り、実課題を完了した（同日）。
- **再開と背景:** `--resume <session-id>`（結果 JSON の `session_id`）。端末を離れても続けるときは `--bg`（`--resume` と併用可）。記録を残さないなら `--no-session-persistence`。
- **Codex を子にする:** Bash から `CODEX_HOME=~/.config/bb-writ/codex-home codex exec --json -m <model> -c model_reasoning_effort="<effort>" -C <対象> "<依頼>" < /dev/null`。出力の `thread.started` の id が session。

## Codex

- **同じ系統の委任:** 新しい文脈が要るときは `codex exec` を別プロセスで起動する。Codex 自身のサブエージェント機能は版と設定で有効・無効が変わるので、使えるかはその場で確かめ、使えなければそう書く。
- **非対話実行:** `codex exec --json -m <model> -c model_reasoning_effort="<effort>" [-s read-only|workspace-write|danger-full-access] [-C <dir>] [--add-dir <dir>] "<依頼>" < /dev/null`。stdin を閉じないと「Reading additional input from stdin...」で待ち続ける（実測 2026-10-17）。承認を Codex 自身の自動レビューに任せるなら `--approve-for-me`（workspace-write を含むので `-s` と併用できない）。sandbox の中でネットワークが要るなら `-c sandbox_workspace_write.network_access=true`、作業領域の外に書く場所は `--add-dir`。構造化した結果が要るなら `--output-schema <file>`。利用者の設定と AGENTS を切るなら `--ignore-user-config`・`--ignore-rules`。Git 管理外で動かすなら `--skip-git-repo-check`。
- **再開:** `codex exec resume <SESSION_ID>`（`--last` で直前）。
- **プラグイン:** `codex plugin marketplace add <このリポジトリの絶対パス>` → `codex plugin add bb-writ@bb-writ`。`.claude-plugin/marketplace.json` と root の `plugin.json` をそのまま読む。写しが `$CODEX_HOME/plugins/cache/bb-writ/bb-writ/<版>/` に置かれるので、版を上げたら `codex plugin remove bb-writ@bb-writ` → `add`。Git の private リポジトリからの追加は未確認。
- **Claude Code を子にする:** `CLAUDE_CONFIG_DIR=~/.config/bb-writ/claude-home dotenvx run -f ~/.config/bb-writ/.env -fk ~/.config/bb-writ/.env.keys -- claude -p "<依頼>" --output-format json --model <m> --effort <e>`。

## Grok Build

- root の `plugin.json` でプラグインを読む。hook の機構はある。スキルの `allowed-tools` はツールを制限せず、`model`・`effort` は受け取っても適用されない（公式文書 https://docs.x.ai/build/features/skills-plugins-marketplaces）。
- 非対話実行・専用ログイン・サブエージェントを切る引数は v1 の `policy/hosts.toml` の `[grok-build]` に記録がある。plan-v3 の形では未計測。読み込みと最初の仕事を確かめた範囲までを「確認済み」とする。
