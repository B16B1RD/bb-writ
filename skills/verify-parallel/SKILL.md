---
name: verify-parallel
description: bb-writ の worktree 分離、再開、直列統合、片付けを変更し、実 CLI と中断・競合試験で検証するときに使う。読むだけの調査では実行しない。
---

# 並行開発の検証

## 準備

リポジトリのルートで実行する。Python 3.9 以上、Git、Claude Code、Codex、dotenvx と bb-writ 専用の両 CLI のログインが必要。普段のログインをコピーしない。`tests/verify_parallel_live.py` は専用ログインで Claude Code 2 セッション、続いて Claude Code と Codex を同時起動する。モデルは小さな実装用の sonnet / gpt-6-sol、effort は medium。外部モデルに送るのはこのプラグインと生成した試験用ファイル。GitHub には送らない。

証拠の置き場は担当の `note.evidence_dir` の下で、毎回新しい名前にする。試験用 Git リポジトリもそこに作る。正式な開発 checkout としてその場所でのみ試験用 commit を作り、`/tmp` のコピーでは commit しない。利用者の checkout は編集しない。

## 点検

`python3 -B tests/verify_parallel_live.py --check` を実行する。Python・Git・CLI の有無と、専用ログインのファイルの存在だけを表示する。認証情報は表示しない。この点検は有効期限や接続成功までは保証しない。

## 操作

1. `python3 -B -m unittest discover -s tests -t . -v`。作業分離、古い検証拒否、変更保存、未完了操作、親プロセス停止、動作中の期限切れ、片付けの試験を実行する。
2. `python3 -B tests/verify_parallel_live.py --out <note.evidence_dir の下の新しいディレクトリ>`。数分かかる。両方の組み合わせで `result: passed` が出て、`result.json` の `status` が `passed` になること。
3. `<out>/repo` の main に `0-a.txt`・`0-b.txt`・`2-a.txt`・`2-b.txt` があり、それぞれファイル名から `.txt` を取った一行であること。`remaining-tasks.json` の tasks は空、`git -C <out>/repo status --porcelain` は空であること。

スキルの選択も確かめる。Claude の JSONL は本文を引用せず、最初の `tool_use` の種類と `Skill` のスキル名だけを観測する。入口と feature 手引きが使われ、作業分離は生成された worktree と成果物で判断する。実 CLI 試験は手引きの正本の場所を明示するので、自然言語だけでの暗黙の選択を証明したことにはしない。ドライバーは読むだけの反例も、新しい読み取り専用セッションで `bb-writ で README を調べて説明して。変更しないで` と頼み、investigation の選択と、専用 worktree やタスクが増えないことを確認する。

## 証拠

単体試験のコマンドと終了コード・出力を保存する。実 CLI は `<out>/` にセッション別の JSONL・stderr・実効モデルと session ID・結果 JSON を保存する。`result.json` は成功条件を観測した後だけ作られる。成功報告だけでは通さず、ファイルの内容、Git の差分、タスク一覧を照合する。失敗時は `failure.txt` と `remaining-tasks.json` にたどる。CLI のエラーを別料金の API や別のログインで補わない。

## 壊して確かめる

`scripts/work.py` をリポジトリ外の一時ファイルへコピーし、次の欠陥を 1 個ずつ入れる。`tests.test_parallel` を Python から import し、そのモジュールの `WORK` をコピーの Path に差し替え、該当する unittest を実行する。このコピーでは commit しない。

- `head != a.verified or target != a.expected` の条件を `False` にする → `test_stale_target_requires_merge_and_reverification` が失敗すること。
- cleanup の「タスクのブランチが変更されている」の直前のブランチ照合を無効にする → `test_cleanup_does_not_delete_commits_on_detached_task_branch` が失敗すること。
- integrate の `git(dest, 'merge', '--ff-only', '--no-overwrite-ignore', head, fds=(rf, wf))` から `fds` を除く → `test_integration_holds_repository_lock_until_git_finishes_after_parent_kill` が失敗すること。
- 同じ merge から `--no-overwrite-ignore` を除く → `test_integration_preserves_ignored_files_at_target` が失敗すること。

各失敗の出力と終了コードを証拠として残す。通ってしまったら、その検査を根拠にしない。コピーを削除し、正本で元の試験が通ることも確認する。

## 片付け

実 CLI の子プロセスは試験ドライバーが終了まで待ち、タイムアウト時は自分が作ったプロセスグループだけ停止する。名前で kill しない。成功時はタスクの worktree とブランチを cleanup 済み。失敗時は未統合の変更と担当記録を残し、`task list` で照合してから再開する。証拠と試験用 repo は残す。不要になった repo は、今回作ったものだけと確認してから削除する。
