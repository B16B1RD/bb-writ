# bb-writ の開発の取り決め

bb-writ は、CLI ハーネス（Claude Code・Codex。Grok Build も同じ SKILL.md を読む）向けのスキルのプラグイン。概要・入れ方・生い立ち・ライセンスは `README.md`。オーナー専用の取り決め（手元の環境・試験用のリポジトリ・push の方法）は、gitignore した `CLAUDE.local.md` にある（無ければ、この文書だけに従う）。

## 話し方と名前

- 日本語で話す。
- 旧い作業名（runner・ランナー）を名前に使わない。

## 公開リポジトリとして

- このリポジトリは公開している。個人名・所属先や他人のリポジトリ名・非公開のリポジトリ名・手元の絶対パス・メールアドレスを、ファイルにも commit のメッセージにも書かない。
- 計画の記録と実測の結果は、このリポジトリに置かない（オーナーの非公開の置き場に置く）。ルールの根拠は、別の文書を指さずに、この文書とスキルの本文に書く。

## 形

- 入口は `skills/bb-writ`、手引きは `skills/playbook-*`、委任とホストごとの接続点は `skills/delegate`、審査は `skills/interrogate`、検証スキルの生成は `skills/create-verification-skill`、形式検証の検査の生成は `skills/create-formal-verification-skill`、学びは `skills/reflect`・`skills/correct`、原則は `skills/principle-*`（1 本 1 スキル）。スキルは目的ごとに分ける（1 つに詰め込まない）。
- すべて SKILL.md の本文で届く形にする。Claude Code はプラグイン内の別ファイルの Read に承認を求め、`disable-model-invocation` のスキルは Skill ツールから呼べない（2026-10-17 の実測。`tests/test_distribution.py` が検査する）。
- 補助スクリプトは `scripts/launch.py`（別系統の CLI を専用ログインで起動し報告を検査）と `scripts/work.py`（作業場所の確保・担当の貸し出し・統合の排他と照合・外部操作の照合と実行の記録）。標準ライブラリだけ。補助コードに入れるのは、プロセス終了の検出・結果形式の検査・同じ作業への二重書き込みの防止・公開対象と検証対象の一致（`work.py op run --verified`）のような、実際に壊れた場合を試せる機械的な約束だけ。「次は設計」「次はレビュー」という工程の順序を持たせない。例外は 1 つで、push・PR の前に、届ける前の審査を通した commit か要らない理由と、変更の危険度を宣言させる（`work.py op run --reviewed`・`--risk`。危険度が中・高なら none: を受けない。commit を書いたときは、その commit を対象に launcher で起動して `ok` で返った読み取り専用の審査役の記録（`delegations.jsonl` の `head`）があるかを照らし、危険度が中・高なら、そのうち `--interrogate` を付けて立てた審査役の記録を求める。人数・審査の中身・手で書き足した記録（`BB_WRIT_STATE_DIR` の付け替えを含む）と、`none:` の理由の真偽は確かめない）。レビュー対応の短い往復で、審査を忘れたまま push し、宣言だけにした後も、書き手が自分で入れた退行の注入を審査の代わりにして HEAD を書いて push したため。同じ関門で、作業の記録の目的・制約・確かめ方（審査を通した commit なら指摘の扱いも、task start の作業場所で審査役を立てた commit が 3 つに達したら仕組みの要否も）の欄が空なら拒否する（欄が空かどうかだけで、中身・書いた順は見ない）。長いセッションで、言い直し・記録・指摘の扱いの一覧を飛ばしたまま直し始めたため。仕組みの要否は、審査の周ごとに仕組みへ直しを足し、仕組みが目的に要るかを誰も問わないまま周が止まらず、原則の文を足すだけでは長い作業の中で判断が変わらなかったため。
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
| 書く子の起動の後、対象の直下に、中身の空の場所を残さない（時間切れで止めた Claude Code 2.1.310 の書く子が、0 バイトの `.bashrc` などと空のディレクトリだけの `.claude/` を作業場所に残した） | `scripts/launch.py` が書く子の前後で対象の直下を比べ、新しくでき、中身が空のままで index に無いものを `cwd_residue` に並べて `failed` にする（消さない）。`tests/test_launch.py` の `test_empty_places_the_child_leaves_in_the_target_fail` |
| 子の CLI が終わった後（時間切れを含む）、子が起こしたプロセスを残さない（Claude Code の Bash は新しいセッションで動き、プロセスグループを止めても、審査役の試験が子の終了から 49 分以上動いた） | Linux は `scripts/launch.py` が subreaper になって子孫を止め（照合の前に止める順序は試験で見ない）、止めきれなければ `failed`。`tests/test_launch.py` の `test_processes_the_child_detached_do_not_outlive_it`。`test_a_child_the_launcher_cannot_stop_or_check_fails`。Linux の外はプロセスグループを止めるだけ（確かめていない） |
| 時間切れで止めた子を、最初から起動し直さない（審査役 2 人が 30 分で止まり、報告の無いまま 90 分で立て直した。それまでの作業は子の会話記録に残っていたが、記録に session が無く続けられなかった） | `scripts/launch.py` が session を起動の前に決め（Codex は最初の `thread.started`）、時間切れを `timeout` と session つきで記録し、`--resume` が同じ session・同じ権限で続きを起動する。続きの `head` は前の起動と同じ commit のときだけ書く（審査の続きを、間に進めた commit の審査に数えない）。権限の照合に当たった起動は続けさせない（記録の `resumable`）。`tests/test_launch.py` の `test_a_timed_out_child_continues_from_where_it_stopped`・`test_a_timeout_does_not_hide_other_failures`・`test_a_codex_resume_judges_the_earlier_turns_by_their_own_launch`・`test_a_resumed_review_counts_only_for_the_commit_it_started_on`・`test_a_resume_checks_what_it_takes_over_from_the_last_launch`・`test_resume_takes_the_launch_only_from_the_record` |
| 子を途中で止めるときは launcher に TERM を送り、launcher と子を木ごと KILL しない（中断で木ごと止め、Claude Code 2.1.310 の子が対象に空の `.claude/.cc-writes` を残し、記録も残らず、`task cleanup` が止まった） | `scripts/launch.py` が、子を待つ間に受けた TERM・HUP・INT を直下の子に TERM として伝え、子が終わるのを待ってから記録を書く（Linux では猶予の後は KILL）。`tests/test_launch.py` の `test_a_signal_to_the_launcher_stops_the_child_cleanly_and_keeps_the_record`。木ごと KILL しないことは `delegate`・`playbook-pause-safely` の文だけ（強制するものは無い） |
| 続きの判定は、1 つ前の記録だけでなく session の履歴で行う（続けさせないと決めた session が 1 つ前の記録から ok になり、見た commit も忘れた。同じ記録から同時に 2 つ起動でき、片付けていない空の残りが続きの ok で隠れた。形式検証で初めて見つかった） | `scripts/launch.py` の `session_history`・`hold_session`。`tests/test_launch.py` の `test_a_session_found_unsafe_cannot_be_continued_from_an_earlier_record`・`test_a_session_runs_in_one_resume_at_a_time`・`test_a_resume_from_an_older_record_keeps_the_commits_the_session_saw`・`test_a_resume_refuses_while_the_last_launch_left_empty_places`・`test_every_reason_that_bans_a_session_applies_from_an_earlier_record`・`test_a_resume_from_an_older_record_still_sees_later_empty_places` |
| 時間切れでも、いきなり KILL せず、TERM を送って子の片付けを待つ（KILL で止めた Claude Code の書く子が、作業場所に空の置き場を残した） | `scripts/launch.py` の `wait_for_exit`。`tests/test_launch.py` の `test_a_timed_out_child_gets_a_term_and_time_to_clean_up` |
| launcher の終わりを、コマンド行の文字列で探す待ち（`pgrep -f` など）で待たない（審査役の launcher が 3〜4 分で終わった後も、待つループが自分のコマンド行に一致し続け、子が 6 時間止まった） | 無い（`delegate` の文だけ。待つループは親のホストの shell で動き、補助コードからは見えない） |
| launcher はプラグインの置き場に `__pycache__` を作らない | `tests/test_launch.py` の `test_launch_leaves_no_bytecode_in_the_plugin` |
| Claude Code の子に `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` を使わない（許可の方針を既定に戻し、空のファイルを残した） | 読み取り専用の子は `tests/test_launch.py` の `test_claude_read_only_in_worktree_denies_the_common_git_dir`。書く子は試験が無く、残った空のファイルを上の `home_entries` が捕まえる |
| Claude Code の子には背景の処理を使わせない（`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`。審査役が試験を背景で回し、終わりの知らせが次のターンを起こして、`--output-format json` が最後のターンの結果だけを返し、報告が 2 回消えた） | `tests/test_launch.py` の `test_claude_child_gets_no_parent_claude_env`（書く子と読み取り専用の子の両方） |
| Claude Code の子に、親の `CLAUDE*`・`ANTHROPIC_*` の環境変数を渡さない | `tests/test_launch.py` の `test_claude_child_gets_no_parent_claude_env` |
| 入口の聞く範囲・呼び直しの文・返答の形・検証の条件 (1)〜(6) の文を、要約の後に戻る先頭 5,000 トークン（字数で近似して 5,600 字）に置く。入口が合計の枠から丸ごと落ちたときのため、手引き 8 本（feature・bug・docs-skill・investigation・parallel・session-pickup・verify・deliver）・delegate・interrogate の 10 本だけ、本文の先頭 4,000 字以内に呼び直しの文を置く（単独でも呼ばれる delegate・interrogate は入口から始めた仕事に限る。pause-safely・eval・reflect・correct・unknowns・原則・検証スキルには置いていない） | `tests/test_distribution.py` の `test_entry_keeps_what_to_ask_and_review_rules_within_the_compaction_head`（目印の語の位置だけ。順と意味は確かめず、意味は読者役で確かめる）と `test_skills_invoked_after_the_entry_say_to_reinvoke_it_after_compaction` |
| push・PR は、届ける前の審査を通した commit（HEAD と同じで、launcher の審査役の記録がある）か、要らない理由と、変更の危険度を宣言してから出す（危険度も理由も書かずに interrogate を省いて push した。レビュー対応で試験を足した commit を、新しい子の再審査なしに push した。宣言にした後も、子を立てずに HEAD を書いた。危険度を中と書いても、後退の一覧を攻める子の記録だけで push し、interrogate の観点の審査が無かった） | `scripts/work.py` の `op run`・`op begin` の `--reviewed`・`--risk`（中・高は none: と並べられず、`--interrogate` の審査役の記録が要る）と、`scripts/launch.py` が書く委任の記録の `head`・`interrogate`。`tests/test_work.py` の `TestReviewedCommit`、`tests/test_launch.py` の `test_the_record_names_the_commit_the_child_saw_only_when_the_tree_matches_it`・`test_an_interrogate_reviewer_is_recorded_and_its_resume_stays_one` |
| push・PR は、作業の記録の目的・制約・確かめ方と、審査を通した commit なら指摘の扱いを書いてから出す（長いセッションで、言い直し・記録・22 件の指摘の扱いの一覧を飛ばしたまま直し始めた。入口の文は 1 週間で約 2 倍の約 15,500 字に膨らんでいた） | `scripts/work.py` の `note_gaps` と `op_step`。`tests/test_work.py` の `TestNoteBeforePublish` |
| task start の作業場所で審査が 3 周に達したら（その作業場所を --cwd にして launcher で読み取り専用の子（審査役・確かめ役など）を立てて ok で返った commit が 3 つ）、審査を通した commit の push・PR は、作業の記録の「仕組みの要否」（直している仕組みが目的の値に要るかを、仕組みあり・なし・別の形で比べたもの）を書いてから出す（審査の周ごとに仕組みへ直しを足し、約 25 周回した。同じ場面を再現した長い作業で、原則の文だけの版は直しを足し続け、雛形の欄と入口の文を足した版は引き継いだ直後に仕組みを外した。関門そのものが働く場面は確かめていない） | `scripts/work.py` の `review_rounds`・`note_gaps`・`op_step`。`tests/test_work.py` の `TestPremiseAfterRounds` |
| 版はマニフェストの間で揃え、`CHANGELOG.md` の先頭は今の版。スキルは SKILL.md の本文だけで届き、名前はフォルダと同じ | `tests/test_distribution.py` |
| 試験が PATH に置く偽のコマンドの中から本物を呼ぶときは、PATH を変える前に引いた絶対パスで呼ぶ。偽物が呼ばれたことを記録で確かめる（偽の git が PATH で自分を呼び続け、利用者のプロセスの上限を埋めて、同時に動いていた Claude Code を落とした） | `tests/process_limit.py`（包みと各試験のモジュールの先頭から読む）が、試験のプロセスとその子に、利用者の今のスレッド数と元の上限の中ほどの上限を掛ける（Linux。同じ誤りでも増えるのは試験の分だけ）。`tests/test_launch.py` の `test_tests_run_under_a_process_limit` |
| `scripts/work.py` の op run・監督役・操作ごとのロック・担当の約束を変えたら、`tests/test_oprun_model.py` のモデルを直して全探索を通し、照合した commit とハッシュを進める（審査が 1 周に 1 つずつ交互実行と途中の kill -9 を見つけ、そのたびに直していた） | `tests/test_oprun_model.py` の `test_the_modeled_code_keeps_the_invariants_in_every_interleaving`（担当 2・鍵 2・token 3 の範囲の交互実行を全部たどる）・`test_each_reverted_fix_gives_a_counterexample`（過去の修正を戻した版が反例を出す）・`test_the_model_is_the_one_checked_against_tlc`（モデルを変えると落ちる。TLC で照らし直したかは確かめない）・`test_the_model_was_checked_against_the_current_code`（モデルが写す経路の関数の本文と、それが読む定数のハッシュ。変わったことを知らせるだけで、ハッシュを進めたときにモデルと照らしたかは確かめない。コメント・先頭の docstring だけの変更は見ない。写さない経路（task start・resume・cleanup、integrate、pending、main など）の変更は見ない） |

## commit

- 通常は main に直接 commit する。並行開発ではタスク専用の worktree・ブランチで commit し、最新の main を取り込んで検証した版を、排他の下で main に統合する。Conventional Commits。
- commit のメッセージは、件名も本文も日本語で書く（`feat`・`docs` などの type は英語のまま）。

## 守ること

- トークン・認証情報を画面・ログ・会話に出さない。利用者のログイン（`~/.claude`・`~/.codex`・`~/.grok`）の写しを作らない。ワーカーは bb-writ 専用のログインを使う。
- `.env` を使うコードは dotenvx（`@dotenvx/dotenvx`。Python も dotenvx の形）。
- トランスクリプトの解析はメタデータだけで、本文を引用しない。push・PR をしてよいかは、対象のリポジトリの規則と依頼で決まる（bb-writ の配布物にこの種の取り決めを書かない）。
- 将来に備えた鍵・構造を足さない（実需が出たら作る）。黙って補うより、理由を出して止める。
- 対象のリポジトリを増やすたびに bb-writ 自身（スキル・コード）を直さない。対象に固有のことは対象のリポジトリの中で解く。
