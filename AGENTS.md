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
- 補助スクリプトは `scripts/launch.py`（別系統の CLI を専用ログインで起動し報告を検査）と `scripts/work.py`（作業場所の確保・担当の貸し出し・統合の排他と照合・外部操作の照合と実行の記録）。標準ライブラリだけ。補助コードに入れるのは、プロセス終了の検出・結果形式の検査・同じ作業への二重書き込みの防止・公開対象と検証対象の一致（`work.py op run --verified`）のような、実際に壊れた場合を試せる機械的な約束だけ。「次は設計」「次はレビュー」という工程の順序を持たせない。例外は 1 つで、push・PR の前に、届ける前の審査を通した commit か要らない理由を宣言させる（`work.py op run --reviewed`。審査をしたかは確かめず、宣言の形と HEAD の一致だけを照らす）。レビュー対応の短い往復で、審査を忘れたまま push したため。
- 必須の機能を bb-writ の hook やカスタム subagent 定義に置かない。hook はホストごとに違い、ハーネス間の互換性を保てないため。入口・原則・手引き・検証スキルの生成・学びは、それらが無くても成り立たせる。hook を使うのは任意の案内や通知だけ。

## 置き場

- 設定 `~/.config/bb-writ/`（dotenvx の `.env`・`.env.keys`、専用ログインの `claude-home`・`codex-home`・`grok-auth`、比較用の `cmp/`）。状態 `~/.local/state/bb-writ/`（`work/` 作業場所ごとの担当・記録・証拠、`delegations/` 委任の記録、`notes/`、`worktrees/`）。
- 専用ログイン: Claude Code は `~/.config/bb-writ/.env` の `CLAUDE_CODE_OAUTH_TOKEN`（dotenvx）と `CLAUDE_CONFIG_DIR=~/.config/bb-writ/claude-home`、Codex は `CODEX_HOME=~/.config/bb-writ/codex-home`、Grok Build は `GROK_HOME=~/.config/bb-writ/grok-auth`。呼び出し方は `skills/delegate/SKILL.md`。
- プラグインとして配る（大前提）。出すときは `.claude-plugin/plugin.json`・`plugin.json`・`.claude-plugin/marketplace.json` の版を一緒に上げる。Codex も同じ 2 つ（marketplace と root の `plugin.json`）を読む（0.163.0 で確認）。ホスト専用のマニフェストは、必要が実物で出るまで足さない。Codex の専用ログインは版ごとの写しなので、版を上げたら `codex plugin remove bb-writ@bb-writ` → `add`。
- 変更履歴は利用者向けの `CHANGELOG.md` 1 つ。版を上げる commit で、先頭に `## <版> - <日付>` の節を足し、前のタグからの変更（版を上げなかった commit の分も）を利用者に何が変わるかで書く。開発者向けの履歴は別に書かず、タグの間の git log を使う。push の後、その commit に `claude plugin tag --push` でタグ（`bb-writ--v<版>`）を付け、Release は `gh release create <タグ> --title "bb-writ <版>" --notes-file <その節を抜き出したファイル>` で作る。Release の本文は手で書かない（同じ文面を 2 か所で持たないため。commit を経ない文は、公開前の検査も通らない）。

## 試験

- 単体: `python3 -B -m unittest discover -s tests -t .`（偽の CLI で launcher と work.py、配布の形）。
- スキルの変更は、新しい CLI セッションで実物を確かめる（最初のツール呼び出しで使われるか、成果物）。比べるときは `skills/playbook-eval`。
- GitHub に触れる確かめは、自分が管理する試験用のリポジトリで行う。子の CLI の起動に、普段渡さない設定（`GH_CONFIG_DIR`・`GH_TOKEN`）を足さない。

## 規則と強制するもの

訂正を受けたら、誤りを直してここに規則を足す。強制するものが無い規則で同じ誤りが起きたら、同じ変更で検査に上げる。起こりえなくなった規則は消す。

| 規則 | 強制するもの |
|---|---|
| 子の起動の後、ホームに空のファイル・ディレクトリを残さない（子の CLI の sandbox が作る。空の `.bash_profile` でログインシェルが `.bashrc` を読まなくなった） | `scripts/launch.py` が子の前後のホームを `home_entries` で比べて `failed` にする（読めない場所が増えたときも）。Claude Code 2.1.309 の sandbox が設定を組むときに作る空のディレクトリ `~/.claude/ssh`（サーバー側の機能フラグが有効なとき。CLI が `~/.claude` の下に作り、CLI の中にもほかに使う所が見つからない）だけは数えず、消さない。`tests/test_launch.py` の `test_child_leaving_empty_files_in_home_fails`・`test_only_the_empty_ssh_dir_claude_code_makes_in_home_is_not_residue`・`test_a_child_hiding_part_of_home_fails`、`tests/verify_sandbox_live.py` の `no_home_residue` |
| launcher はプラグインの置き場に `__pycache__` を作らない | `tests/test_launch.py` の `test_launch_leaves_no_bytecode_in_the_plugin` |
| Claude Code の子に `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` を使わない（許可の方針を既定に戻し、空のファイルを残した） | 読み取り専用の子は `tests/test_launch.py` の `test_claude_read_only_in_worktree_denies_the_common_git_dir`。書く子は試験が無く、残った空のファイルを上の `home_entries` が捕まえる |
| Claude Code の子に、親の `CLAUDE*`・`ANTHROPIC_*` の環境変数を渡さない | `tests/test_launch.py` の `test_claude_child_gets_no_parent_claude_env` |
| 入口の聞く範囲・呼び直しの文・返答の形・検証の条件 (1)〜(6) の文を、要約の後に戻る先頭 5,000 トークン（字数で近似して 5,600 字）に置く。入口が合計の枠から丸ごと落ちたときのため、手引き 6 本（feature・bug・docs-skill・investigation・parallel・session-pickup）・delegate・interrogate の 8 本だけ、本文の先頭 4,000 字以内に呼び直しの文を置く（単独でも呼ばれる delegate・interrogate は入口から始めた仕事に限る。pause-safely・eval・reflect・correct・unknowns・原則・検証スキルには置いていない） | `tests/test_distribution.py` の `test_entry_keeps_what_to_ask_and_review_rules_within_the_compaction_head`（目印の語の位置だけ。順と意味は確かめず、意味は読者役で確かめる）と `test_skills_invoked_after_the_entry_say_to_reinvoke_it_after_compaction` |
| push・PR は、届ける前の審査を通した commit（HEAD と同じ）か、要らない理由を宣言してから出す（レビュー対応で試験を足した commit を、新しい子の再審査なしに push した） | `scripts/work.py` の `op run`・`op begin` の `--reviewed`。`tests/test_work.py` の `TestReviewedCommit` |
| 版はマニフェストの間で揃え、`CHANGELOG.md` の先頭は今の版。スキルは SKILL.md の本文だけで届き、名前はフォルダと同じ | `tests/test_distribution.py` |
| 試験が PATH に置く偽のコマンドの中から本物を呼ぶときは、PATH を変える前に引いた絶対パスで呼ぶ。偽物が呼ばれたことを記録で確かめる（偽の git が PATH で自分を呼び続け、利用者のプロセスの上限を埋めて、同時に動いていた Claude Code を落とした） | `tests/process_limit.py`（包みと各試験のモジュールの先頭から読む）が、試験のプロセスとその子に、利用者の今のスレッド数と元の上限の中ほどの上限を掛ける（Linux。同じ誤りでも増えるのは試験の分だけ）。`tests/test_launch.py` の `test_tests_run_under_a_process_limit` |

## commit

- 通常は main に直接 commit する。並行開発ではタスク専用の worktree・ブランチで commit し、最新の main を取り込んで検証した版を、排他の下で main に統合する。Conventional Commits。
- commit のメッセージは、件名も本文も日本語で書く（`feat`・`docs` などの type は英語のまま）。

## 守ること

- トークン・認証情報を画面・ログ・会話に出さない。利用者のログイン（`~/.claude`・`~/.codex`・`~/.grok`）の写しを作らない。ワーカーは bb-writ 専用のログインを使う。
- `.env` を使うコードは dotenvx（`@dotenvx/dotenvx`。Python も dotenvx の形）。
- トランスクリプトの解析はメタデータだけで、本文を引用しない。push・PR をしてよいかは、対象のリポジトリの規則と依頼で決まる（bb-writ の配布物にこの種の取り決めを書かない）。
- 将来に備えた鍵・構造を足さない（実需が出たら作る）。黙って補うより、理由を出して止める。
- 対象のリポジトリを増やすたびに bb-writ 自身（スキル・コード）を直さない。対象に固有のことは対象のリポジトリの中で解く。
