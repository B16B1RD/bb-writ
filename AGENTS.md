# bb-writ の開発の取り決め

bb-writ は、CLI ハーネス（Claude Code・Codex。Grok Build も同じ SKILL.md を読む）向けのスキルのプラグイン。概要・入れ方・生い立ち・ライセンスは `README.md`。オーナー専用の取り決め（手元の環境・試験用のリポジトリ・push の方法）は、gitignore した `CLAUDE.local.md` にある（無ければ、この文書だけに従う）。

## 話し方と名前

- 日本語で話す。
- 旧い作業名（runner・ランナー）を名前に使わない。

## 公開リポジトリとして

- このリポジトリは公開している。個人名・所属先や他人のリポジトリ名・非公開のリポジトリ名・手元の絶対パス・メールアドレスを、ファイルにも commit のメッセージにも書かない。
- 計画の記録と実測の結果は、このリポジトリに置かない（オーナーの非公開の置き場に置く）。ルールの根拠は、別の文書を指さずに、この文書とスキルの本文に書く。

## 形

- 入口は `skills/bb-writ`、手引きは `skills/playbook-*`、委任とホストごとの接続点は `skills/delegate`、審査は `skills/interrogate`、検証スキルの生成は `skills/create-verification-skill`、学びは `skills/reflect`・`skills/correct`、原則は `skills/principle-*`（1 本 1 スキル）。スキルは目的ごとに分ける（1 つに詰め込まない）。
- すべて SKILL.md の本文で届く形にする。Claude Code はプラグイン内の別ファイルの Read に承認を求め、`disable-model-invocation` のスキルは Skill ツールから呼べない（2026-10-17 の実測。`tests/test_distribution.py` が検査する）。
- 補助スクリプトは `scripts/launch.py`（別系統の CLI を専用ログインで起動し報告を検査）と `scripts/work.py`（担当の貸し出しと外部操作の記録）。標準ライブラリだけ。補助コードに入れるのは、プロセス終了の検出・結果形式の検査・同じ作業への二重書き込みの防止・公開対象と検証対象の一致（`work.py op begin --verified`）のような、実際に壊れた場合を試せる機械的な約束だけ。「次は設計」「次はレビュー」という工程の順序を持たせない。
- 必須の機能を bb-writ の hook やカスタム subagent 定義に置かない。hook はホストごとに違い、ハーネス間の互換性を保てないため。入口・原則・手引き・検証スキルの生成・学びは、それらが無くても成り立たせる。hook を使うのは任意の案内や通知だけ。

## 置き場

- 設定 `~/.config/bb-writ/`（dotenvx の `.env`・`.env.keys`、専用ログインの `claude-home`・`codex-home`・`grok-auth`、比較用の `cmp/`）。状態 `~/.local/state/bb-writ/`（`work/` 作業場所ごとの担当・記録・証拠、`delegations/` 委任の記録、`notes/`、`worktrees/`）。
- 専用ログイン: Claude Code は `~/.config/bb-writ/.env` の `CLAUDE_CODE_OAUTH_TOKEN`（dotenvx）と `CLAUDE_CONFIG_DIR=~/.config/bb-writ/claude-home`、Codex は `CODEX_HOME=~/.config/bb-writ/codex-home`、Grok Build は `~/.config/bb-writ/grok-auth`。呼び出し方は `skills/delegate/SKILL.md`。
- プラグインとして配る（大前提）。出すときは `.claude-plugin/plugin.json`・`plugin.json`・`.claude-plugin/marketplace.json` の版を一緒に上げる。Codex も同じ 2 つ（marketplace と root の `plugin.json`）を読む（0.163.0 で確認）。ホスト専用のマニフェストは、必要が実物で出るまで足さない。Codex の専用ログインは版ごとの写しなので、版を上げたら `codex plugin remove bb-writ@bb-writ` → `add`。
- 変更履歴は利用者向けの `CHANGELOG.md` 1 つ。版を上げる commit で、先頭に `## <版> - <日付>` の節を足し、前のタグからの変更（版を上げなかった commit の分も）を利用者に何が変わるかで書く。開発者向けの履歴は別に書かず、タグの間の git log を使う。push の後、その commit に `claude plugin tag --push` でタグ（`bb-writ--v<版>`）を付け、Release は `gh release create <タグ> --title "bb-writ <版>" --notes-file <その節を抜き出したファイル>` で作る。Release の本文は手で書かない（同じ文面を 2 か所で持たないため。commit を経ない文は、公開前の検査も通らない）。

## 試験

- 単体: `python3 -B -m unittest discover -s tests -t .`（偽の CLI で launcher と work.py、配布の形）。
- スキルの変更は、新しい CLI セッションで実物を確かめる（最初のツール呼び出しで使われるか、成果物）。比べるときは `skills/playbook-eval`。
- GitHub に触れる確かめは、自分が管理する試験用のリポジトリで行う。子の CLI の起動に、普段渡さない設定（`GH_CONFIG_DIR`・`GH_TOKEN`）を足さない。

## commit

- main に直接 commit する。Conventional Commits。
- commit のメッセージは、件名も本文も日本語で書く（`feat`・`docs` などの type は英語のまま）。

## 守ること

- トークン・認証情報を画面・ログ・会話に出さない。利用者のログイン（`~/.claude`・`~/.codex`・`~/.grok`）の写しを作らない。ワーカーは bb-writ 専用のログインを使う。
- `.env` を使うコードは dotenvx（`@dotenvx/dotenvx`。Python も dotenvx の形）。
- トランスクリプトの解析はメタデータだけで、本文を引用しない。push・PR をしてよいかは、対象のリポジトリの規則と依頼で決まる（bb-writ の配布物にこの種の取り決めを書かない）。
- 将来に備えた鍵・構造を足さない（実需が出たら作る）。黙って補うより、理由を出して止める。
- 対象のリポジトリを増やすたびに bb-writ 自身（スキル・コード）を直さない。対象に固有のことは対象のリポジトリの中で解く。
