# bb-writ の開発の取り決め

bb-writ は、CLI ハーネス（Claude Code・Codex。Grok Build も同じ SKILL.md を読む）向けのスキルのプラグイン。概要・入れ方・生い立ち・ライセンスは `README.md`。オーナー専用の取り決め（手元の環境・試験用のリポジトリ・push の方法）は、gitignore した `CLAUDE.local.md` にある（無ければ、この文書だけに従う）。

## 話し方と名前

- 日本語で話す。
- 旧い作業名（runner・ランナー）を名前に使わない。

## 公開リポジトリとして

- このリポジトリは公開している。個人名・所属先や他人のリポジトリ名・非公開のリポジトリ名・手元の絶対パス・メールアドレスを、ファイルにも commit のメッセージにも書かない。
- 計画の記録と実測の結果は、このリポジトリに置かない（オーナーの非公開の置き場に置く）。ルールの根拠は、別の文書を指さずに、この文書とスキルの本文に書く。

## 形

- 入口は `skills/bb-writ`（進め方の本体 `skills/conduct` を呼ぶ薄い入口。Claude Code では、frontmatter の `context: fork`・`model` で、呼んだセッションとは別の子＝指揮者として動く。Codex・Grok Build は frontmatter のこの 2 行を使わず、入口を読んで本体へ進む）、手引きは `skills/playbook-*`、委任とホストごとの接続点は `skills/delegate`、探索は `skills/unknowns`（本体 `skills/explore` を呼ぶ薄い入口。入口 `bb-writ` と同じく、Claude Code では fork した子として動く）、審査は `skills/interrogate`、検証スキルの生成は `skills/create-verification-skill`、形式検証の検査の生成は `skills/create-formal-verification-skill`、性質の試験の生成は `skills/create-property-test-skill`、学びは `skills/reflect`・`skills/correct`、原則は `skills/principle-*`（1 本 1 スキル）。スキルは目的ごとに分ける（1 つに詰め込まない）。bb-writ 自身を確かめる検証スキル（`verify-parallel`）は配らず、`create-verification-skill` の決めどおり `.agents/skills/` に置く（Claude Code には `.claude/skills/` からの symlink で見せる。置き場と symlink を確かめる検査は無い。配布の検査は `skills/` だけを見る）。
- すべて SKILL.md の本文で届く形にする。Claude Code はプラグイン内の別ファイルの Read に承認を求め、`disable-model-invocation` のスキルは Skill ツールから呼べない（2026-10-17 の実測。`tests/test_distribution.py` が検査する）。
- 補助スクリプトは `scripts/launch.py`（別系統の CLI を専用ログインで起動し報告を検査）と `scripts/work.py`（作業場所の確保・担当の貸し出し・統合の排他と照合・外部操作の照合と実行の記録）。標準ライブラリだけ。補助コードに入れるのは、プロセス終了の検出・結果形式の検査・同じ作業への二重書き込みの防止・公開対象と検証対象の一致（`work.py op run --verified`）のような、実際に壊れた場合を試せる機械的な約束だけ。「次は設計」「次はレビュー」という工程の順序を持たせない。例外は push・PR の前の関門 1 つで、届ける前の審査か要らない理由と変更の危険度の宣言、HEAD の監査役の記録、作業の記録の欄を求める。何を求め何を見ないかと、その理由は「規則と強制するもの」の表の push・PR の行に書く。
- 必須の機能を bb-writ の hook やカスタム subagent 定義に置かない。hook はホストごとに違い、ハーネス間の互換性を保てないため。入口・原則・手引き・検証スキルの生成・学びは、それらが無くても成り立たせる。hook を使うのは任意の案内や通知と、スキルの文がすでに求める関門へ戻すための拒否だけ（`hooks/hooks.json` の、拒否の `scripts/push_guard.py` と、案内の `scripts/conductor_hint.py`。hook が動かないホストでも、同じことを入口の文と description が求める）。

## 置き場

- 設定 `~/.config/bb-writ/`（dotenvx の `.env`・`.env.keys`、専用ログインの `claude-home`・`codex-home`・`grok-auth`、比較用の `cmp/`）。状態 `~/.local/state/bb-writ/`（`work/` 作業場所ごとの担当・記録・証拠、`delegations/` と `delegations.jsonl` 委任の記録、`grok-homes/` 起動ごとの Grok の home、`resuming/` 続きの排他、`notes/`、`worktrees/`、`claude-login.json` 専用ログインのトークンの組織の覚え）。状態の置き場に置くものには、消す者と時を決める（消さないと決めるものは、そう書く）。担当・ロック・統合の記録は、`work.py` が操作の終わりに消す。`worktrees/` のタスクの作業場所は `task cleanup` が消し、呼ばれなかったものは、担当を手放した後か期限が切れて 24 時間を過ぎた後に（担当の記録の期限を数として読めなければ、すぐに）、`task start`・`task list` の `unclosed` に出る（記録が JSON として読めなければ、`task list` は並べずに止まる）。読み取り専用の Codex の子の一時ディレクトリ（`delegations/<名前>/tmp`）と `grok-homes/` の home は、launcher が起動のたびに、7 日使われていないものと、もう無いタスクの作業場所だけを見ていた子のものを消す（使っている置き場は、そのディレクトリに掛けた排他で守る）。記録・証拠・報告（`work/`、`delegations/` の中の報告、`delegations.jsonl`、`notes/`）は、消さないと決めてある（消す者を置かない。増え方は守らない）。`resuming/` は空のファイルで、消さない。`claude-login.json` は、launcher が、専用ログインの `.env` の中身の SHA-256 と、トークンの組織の ID の SHA-256 を覚える 1 つのファイルで、launcher が書き換え、増えない。消さない（消しても、launcher が次にトークンの組織を読めたときに書き直す）。`delegations/`・`grok-homes/` の下は launcher だけが書く（読み取り専用の Codex の子と Grok の子は、自分の置き場の中にだけ書けた）。手やほかのプロセスで消さない・名前を付け替えない（launcher は、片付けや起動と同時の付け替えを守らない）。状態の置き場を、子の sandbox が書ける場所（`/tmp` など）の下に置かない。
- 専用ログイン: Claude Code は `~/.config/bb-writ/.env` の `CLAUDE_CODE_OAUTH_TOKEN`（dotenvx）と `CLAUDE_CONFIG_DIR=~/.config/bb-writ/claude-home`、Codex は `CODEX_HOME=~/.config/bb-writ/codex-home`、Grok Build は `GROK_HOME=~/.config/bb-writ/grok-auth`。呼び出し方は `skills/delegate/SKILL.md`。
- プラグインとして配る（大前提）。出すときは `.claude-plugin/plugin.json`・`plugin.json`・`.claude-plugin/marketplace.json` の版を一緒に上げる。Codex も同じ 2 つ（marketplace と root の `plugin.json`）を読む（0.163.0 で確認）。ホスト専用のマニフェストは、必要が実物で出るまで足さない。Codex の専用ログインは版ごとの写しで、写しが launcher と中身で違うと、launcher は Codex の子を起動しない。専用ログインの marketplace が手元のディレクトリで登録してあり、その置き場が launcher の置き場なら、launcher が起動の前に自分で入れ直す。そうでないとき（GitHub から入れた marketplace、作業場所の launcher など）は、launcher が理由に出すコマンド（`CODEX_HOME=<専用ログイン> codex plugin add bb-writ@<marketplace>`。GitHub から入れた marketplace なら、その前に `marketplace upgrade`。`remove` は要らない）で、手で入れ直す（作業場所の launcher では、統合してから）。
- 変更履歴は利用者向けの `CHANGELOG.md` 1 つ。版を上げる commit で、先頭に `## <版> - <日付>` の節を足し、前のタグからの変更（版を上げなかった commit の分も）を利用者に何が変わるかで書く。開発者向けの履歴は別に書かず、タグの間の git log を使う。push の後、その commit に `claude plugin tag --push` でタグ（`bb-writ--v<版>`）を付け、Release は `gh release create <タグ> --title "bb-writ <版>" --notes-file <その節を抜き出したファイル>` で作る。Release の本文は手で書かない（同じ文面を 2 か所で持たないため。commit を経ない文は、公開前の検査も通らない）。

## 試験

- 単体: `python3 -B -m unittest discover -s tests -t .`（偽の CLI で launcher と work.py、配布の形）。`tests/test_properties.py` は Hypothesis（`tests/requirements.txt`、試験だけの依存）で入力の形の性質を見て、無ければ飛ばす。CI は入れて回す。
- スキルの変更は、新しい CLI セッションで実物を確かめる（最初のツール呼び出しで使われるか、成果物）。比べるときは `skills/playbook-eval`。
- GitHub に触れる確かめは、自分が管理する試験用のリポジトリで行う。子の CLI の起動に、普段渡さない設定（`GH_CONFIG_DIR`・`GH_TOKEN`）を足さない。

## 規則と強制するもの

訂正を受けたら、誤りを直してここに規則を足す。強制するものが無い規則で同じ誤りが起きたら、同じ変更で検査に上げる。起こりえなくなった規則は消す。1 行の形は、表の最後の行に従う。規則の根拠は、行の経緯の括弧に短く書く。

| 規則 | 強制するもの |
|---|---|
| 子の起動の後、ホームに空のファイル・ディレクトリを残さない（空の `.bash_profile` でログインシェルが `.bashrc` を読まなくなった）。Claude Code の CLI が自分の置き場として作る `~/.claude` の直下の空のディレクトリと `~/.config/anthropic` だけは数えない | `scripts/launch.py` の `home_entries`。`test_child_leaving_empty_files_in_home_fails`・`test_empty_dirs_the_claude_cli_makes_in_its_own_places_are_not_residue` |
| 書く子の起動の後、対象の直下に、空のファイル（とそれを持つディレクトリ）を残さない。ファイルを持たないディレクトリは数えない | `scripts/launch.py` の `cwd_residue`。`test_empty_places_the_child_leaves_in_the_target_fail` |
| 片付けで、別のプロセスが同時に使いうる空の置き場を消さない（launcher が消した空の `.claude/.cc-writes` で、並行するセッションの設定の書き込みが拒まれた。#65）。ファイルを持たないディレクトリは、片付けを止める理由に数えない | `scripts/work.py` の `clean`・`holds_no_file`。`test_cleanup_does_not_count_directories_that_hold_no_file`・`test_claude_staging_dirs_stay_for_the_sessions_that_share_them` |
| 子の CLI が終わった後（時間切れを含む）、子が起こしたプロセスを残さない | Linux は `scripts/launch.py` が subreaper になって止める（Linux の外はプロセスグループだけ）。`test_processes_the_child_detached_do_not_outlive_it`・`test_a_child_the_launcher_cannot_stop_or_check_fails` |
| 時間切れで止めた子を最初から起動し直さず、`--resume` で同じ session を続ける。続けてよいかは、1 つ前の記録でなく session の履歴で決める | `scripts/launch.py` の `session_history`・`hold_session`。`test_a_timed_out_child_continues_from_where_it_stopped`・`test_a_session_found_unsafe_cannot_be_continued_from_an_earlier_record` |
| 子を止めるときは launcher に TERM を送り、木ごと KILL しない。時間切れでも、TERM の後に片付けの猶予を置く | `scripts/launch.py` の `wait_for_exit`。`test_a_signal_to_the_launcher_stops_the_child_cleanly_and_keeps_the_record`・`test_a_timed_out_child_gets_a_term_and_time_to_clean_up`。親が木ごと KILL しないことは `delegate` の文だけ |
| 統合の記録を、作業場所やブランチが消えたまま残さない | `scripts/work.py` の `settle_integration`。`test_a_stale_integration_record_of_a_gone_task_does_not_stop_others` |
| launcher の終わりを、コマンド行の文字列で探す待ち（`pgrep -f` など）で待たない（待つループが自分に一致して 6 時間止まった） | 無い（`delegate` の文だけ） |
| launcher はプラグインの置き場に `__pycache__` を作らない | `test_launch_leaves_no_bytecode_in_the_plugin` |
| Codex の子を、launcher と中身の違う bb-writ の写しで動かさない。入れ直すのは、launcher 自身の置き場を手元の marketplace として入れた写しだけ | `scripts/launch.py` の `codex_stale_copy`・`codex_refresh`（守らない範囲はその docstring）。`test_a_codex_child_does_not_start_on_a_stale_copy_of_bb_writ`・`test_concurrent_launchers_reinstall_the_codex_copy_once` |
| Claude Code の子には、親の `CLAUDE*`・`ANTHROPIC_*` を渡さず、`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` を渡し、`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` を使わない。子のモデルなど構成の要を、利用者の環境変数や設定（`CLAUDE_CODE_SUBAGENT_MODEL` など）に頼って決めない（利用者の手元に在った設定を前提に、案を出した） | `test_claude_child_gets_no_parent_claude_env`・`test_the_model_comes_from_the_role_unless_the_parent_gives_one`・`test_claude_read_only_in_worktree_denies_the_common_git_dir` |
| 読み取り専用の子に、ホームの認証情報を読ませない。Claude Code の子はホームを丸ごと拒んで道具の置き場と対象を戻し、Codex・Grok の子は一覧で拒む | `scripts/launch.py` の `claude_reads`・`read_only_secrets`・`secret_denies`。`test_claude_read_only_denies_the_whole_home_and_lets_the_tools_back`・`test_grok_read_only_denies_the_credential_places_keeps_what_it_runs_and_gets_a_narrow_env`（渡す規則の形だけ）。守らない範囲は `claude_reads`・`read_only_secrets` の docstring |
| 入口の本体（`conduct`）の聞く範囲・呼び直しの文・返答の形・検証の条件 (1)〜(6) の文を、要約の後に戻る先頭 5,600 字に置く。手引き 8 本・`delegate`・`interrogate` は、先頭 4,000 字に本体を呼び直す文を置く | `test_entry_keeps_what_to_ask_and_review_rules_within_the_compaction_head`・`test_skills_invoked_after_the_entry_say_to_reinvoke_it_after_compaction`（目印の語の位置だけ） |
| 入口のモデルを frontmatter の `model` だけで決めず、`context: fork` と `model` で別の子（指揮者）として動かす（`model` だけでは最初のターンにしか効かなかった） | `test_the_entry_forks_and_hands_over_to_the_body`（探索の入口 `unknowns` と本体 `explore` も同じ形） |
| 指揮者への返事を、親のセッションが取り次ぐ前提で設計・説明しない。利用者が `/tasks` から指揮者に直接返事する | `hooks/hooks.json` の `scripts/conductor_hint.py`（入口か探索の入口 unknowns を呼んだセッションに案内を足す。従うかは親のモデルしだい）。`tests/test_conductor_hint.py` |
| 入口の子（指揮者）が失敗して終わったら、親のセッションは依頼を自分で進めず、失敗と次の手（本体を名前で呼ぶ・入口を呼び直す）を伝えて止まる（入口のモデルを使えずに指揮者が失敗した後、親が関門を通さずにファイルを書き換えた。#57） | `scripts/conductor_hint.py` の `FAILED`（失敗の知らせのターンに案内を足す。従うかは親のモデルしだい）と入口の description。`test_a_failure_notice_of_the_conductor_gets_the_stop_hint` |
| launcher の Claude Code の子に、入口 `bb-writ:bb-writ` を呼ばせない（呼ぶたびに指揮者が fork する） | `scripts/launch.py` が `permissions.deny: Skill(bb-writ:bb-writ)` と依頼文の先頭の 1 行を渡す。`test_claude_children_cannot_invoke_the_entry`・`test_a_prompt_that_starts_with_a_slash_is_not_run_as_a_command` |
| push・PR は、届ける前の審査を通した commit（HEAD か、その後の差分を危険度 低 と宣言した祖先）か、審査が要らない理由と危険度の宣言から出す（危険度も理由も書かずに interrogate を省いて push した）。中・高は `--interrogate` で立てた審査役の記録が要る。子が sandbox で試験を実行できなかったときは `--not-run` の宣言で数える | `scripts/work.py` の `op run`・`op begin` の `--reviewed`・`--delta`・`--risk`・`--not-run`。`TestReviewedCommit`・`TestNotRunDeclaration`。記録の真偽と審査の中身は見ない |
| push・PR は、宣言にかかわらず、HEAD を対象にした遵守の監査役（`--audit`）が `ok` で返り、届けてよい（`deliver: yes`）と判定してから出す。記録・下書き・証拠の整えだけなら `yes` で、立て直さない（`none:文書だけ` で push し、行どうしの食い違いを人のレビューまで見逃した）。監査役は審査と周に数えない | `launch.py` の `AUDIT_CONTRACT`、`work.py` の `reviews_of`。`test_every_push_and_pr_needs_an_audit_of_the_head_even_when_no_review_is_declared`・`test_an_audit_that_sent_the_work_back_does_not_open_the_gate`・`test_an_auditor_must_say_whether_to_deliver_and_the_record_keeps_it`・`test_an_audit_does_not_count_as_a_review`。掛かるのは鍵 `push`・`pr` だけ |
| 入口を呼んだセッションは、`work.py op run` を通さずに外へ出さない（素の `git push`・`gh pr create`・`gh pr merge`・`gh release create`。担当も記録も取らない書き手が素の push で届けた） | Claude Code は `hooks/hooks.json` の `scripts/push_guard.py`（会話記録の入口の印か、入口で始まった子の会話記録で見分ける。守らない範囲と誤って止める例は `scripts/push_guard.py` の docstring）。`test_a_push_by_the_conductor_after_clear_is_denied`・`test_the_hook_denies_a_conductor_started_from_another_version_of_the_plugin`。Codex・Grok Build は入口の文だけ |
| push・PR は、作業の記録の目的・制約・確かめ方・指摘の扱いを書いてから出す（長いセッションで、指摘の扱いの一覧を飛ばしたまま直し始めた） | `scripts/work.py` の `note_gaps`。`TestNoteBeforePublish`・`TestInputShapes`（欄が空かだけを見る） |
| task start の作業場所で審査が 3 周に達したら、作業の記録の「仕組みの要否」を書いてから push・PR を出す（周ごとに仕組みへ直しを足し、約 25 周回した） | `scripts/work.py` の `review_rounds`。`TestPremiseAfterRounds` |
| CI の結果を見ずに完了と報告しない。Release は CI が通った commit にだけ出す。CI の上限は最も遅い OS の実測の 3 倍以上にする | `scripts/work.py` の `ci_not_passed`。`test_a_release_waits_for_the_ci_of_the_commit_to_pass`・`test_ci_jobs_have_room_for_the_slow_macos_runners` |
| 版はマニフェストの間で揃え、`CHANGELOG.md` の先頭は今の版。スキルは SKILL.md の本文だけで届き、名前はフォルダと同じ | `tests/test_distribution.py` |
| 道筋を扱う変更は、一時の置き場が symlink の下でも試験が通るようにする（macOS の一時ディレクトリは `/private/var` への symlink の下） | CI の `unit-symlink-tmp`。`test_ci_also_runs_the_tests_with_the_temporary_place_under_a_symlink` |
| 試験が PATH に置く偽のコマンドから本物を呼ぶときは、PATH を変える前に引いた絶対パスで呼ぶ（偽の git が自分を呼び続け、プロセスの上限を埋めた） | `tests/process_limit.py`。`test_tests_run_under_a_process_limit` |
| 状態の置き場に置いたものの要否を、利用者に聞かない。置くものには、消す者と時を決める | `scripts/launch.py` の `sweep_leftovers`・`hold_place`、`scripts/work.py` の `unclosed_tasks`。`test_the_launcher_removes_its_leftovers_after_the_retention`・`test_a_place_in_use_is_kept_whatever_the_records_say`・`tests/test_sweep_model.py`（関数を変えたら、モデルが本文を写しているかを読み直す） |
| 担当の期限が切れても、誰も取り直していなければ、持ち主の操作を拒否しない | `scripts/work.py` の `held_lease`。`test_an_expired_lease_nobody_took_stays_with_its_holder`・`test_an_expired_lease_nobody_took_still_integrates_and_cleans_up` |
| 期限が切れて 24 時間以内のタスクを、閉じるタスクとして `unclosed` に出さず、`--take-over` の無い `task resume` でも取らない（待っている持ち主の作業場所が消された。#32。その後も古い一覧から取れた） | `scripts/work.py` の `maybe_waiting`・`claim_at`。`test_a_task_whose_owner_may_still_be_waiting_is_not_offered_for_closing`・`test_a_stale_list_does_not_take_the_task_of_an_owner_who_came_back` |
| `scripts/work.py` の op run・監督役・ロック・担当の約束を変えたら、`tests/test_oprun_model.py` のモデルを直して全探索を通し、照合した commit とハッシュを進める | `test_the_modeled_code_keeps_the_invariants_in_every_interleaving`・`test_each_reverted_fix_gives_a_counterexample`・`test_the_model_was_checked_against_the_current_code` |
| この表の行は、短い規則（根拠の経緯は括弧で短く）と、強制するコード・試験の名前だけにする。実測の記録・確かめた回数・守らない範囲の細部は書かず、守らない範囲はそのコードの docstring と試験に置く（1 行が 8,000 字を超え、実装を変えるたびに文の照合の直しが続いた） | `test_rules_table_rows_stay_short`・`test_tests_named_in_the_rules_table_exist`（行の長さと、名指しした試験の実在だけを見る。実測を書いたかは見ない） |

## commit

- 通常は main に直接 commit する。並行開発ではタスク専用の worktree・ブランチで commit し、最新の main を取り込んで検証した版を、排他の下で main に統合する。Conventional Commits。
- commit のメッセージは、件名も本文も日本語で書く（`feat`・`docs` などの type は英語のまま）。
- 書く子が、このリポジトリの作業場所（`task start` の worktree）で commit できなかったと報告したら、指揮者が差分を確かめてから commit する。launcher が `--plugin-dir` で渡す bb-writ の置き場を、Claude Code の sandbox が読み込み専用で被せるため、共有の `.git` がその置き場の中に在る（手元の checkout の launcher で、その checkout 自身の作業場所を直す）と、書く子は `index.lock` を作れない。困るのは bb-writ 自身の開発だけなので、launcher は変えない。

## 守ること

- トークン・認証情報を画面・ログ・会話に出さない。利用者のログイン（`~/.claude`・`~/.codex`・`~/.grok`）の写しを作らない。ワーカーは bb-writ 専用のログインを使う。
- `.env` を使うコードは dotenvx（`@dotenvx/dotenvx`。Python も dotenvx の形）。
- トランスクリプトの解析はメタデータだけで、本文を引用しない。push・PR をしてよいかは、対象のリポジトリの規則と依頼で決まる（bb-writ の配布物にこの種の取り決めを書かない）。
- 将来に備えた鍵・構造を足さない（実需が出たら作る）。黙って補うより、理由を出して止める。
- 対象のリポジトリを増やすたびに bb-writ 自身（スキル・コード）を直さない。対象に固有のことは対象のリポジトリの中で解く。
