---
name: playbook-session-pickup
description: 前のセッション（別の CLI でもよい）の続きを引き受けるときに使う手引き。「続きをやって」「中断した作業を再開して」「この PR の続き」と頼まれたとき、または作業場所に bb-writ の記録や担当の貸し出しが残っていたとき。前の記録と実物から再開点を決め、済んだことをやり直さない。
---

# 手引き: session-pickup（続きを引き受ける）

再開点は自分が決める。前の記録は根拠のある入力で、最初から調べ直すためのものではない。ただし前の「できました」は主張であって証拠ではない。

1. **記録を探す。** Git のタスクは `playbook-parallel` の `task list` で元の checkout から専用の作業場所を探す。新規 worktree を作ってやり直さない。 `work.py status --dir <作業場所>` の `note`（前の担当の記録）と `pending_ops`、作業場所のブランチと commit、PR の本文とコメント。ホストの session ID が書かれていて同じホストなら、`claude -p --resume <id>` や `codex exec resume <id>` で会話ごと続けてもよい（`delegate`）。記録が無ければ、ブランチ・差分・PR から再開点を組み立てる。
2. **担当を引き受ける。** Git のタスクは `playbook-parallel` の `task resume` を使う。そこで担当取得済みなら重ねて取らない。Git 以外は `work.py claim --dir <作業場所> --holder "<ホスト> <session>"`。拒否（終了コード 3）されたら、生きた担当がいる。書かずに、その担当と期限を返す。引き受けたら、出力の `token` を記録に書き、単位ごとに `renew` する。`renew` が拒否されたら、引き継がれている。すぐ書くのをやめる。
3. **結果が分からない外部操作を照合する。** claim の出力の `pending_ops` の 1 つずつについて、外部の状態を見る（PR なら `gh pr list --head <ブランチ> --state all`、push なら `git ls-remote origin <ブランチ>`、コメントなら PR のコメント一覧）。結果が見つかれば `op done --result "<見つけたもの>"` で閉じ、やり直さない。見つからないと確かめられたときだけやり直す。やり直しも `op begin` から（同じ鍵は拒否されるので、`<鍵>:retry` のように別の鍵にする）。
4. **済んだことと残りを分ける。** 目的と確かめ方の述語に照らし、済んだこと（commit・PR・確かめた証拠）と残りを並べ、再開点を 1 つ名指す。前の再現や確かめを、理由なくやり直さない。
5. **続ける。** 残りの仕事に合う手引き（入口 `bb-writ` の表）へ渡す。引き継いだ主張は、最後に元の目的に照らして実物で確かめる（`principle-prove-it-works`）。
6. **外部操作の約束を守る。** これから行う push・PR 作成・コメントなどは元の checkout から実行し、前に `op begin --key <種類:対象> --from <元の checkout>`（push の鍵には出す commit を含める。`push:origin/main@<commit>`。`push`・`pr`・`merge`・`release` は `--verified <検証した commit>` も。前の担当の検証は、その commit が今の HEAD と同じときだけ使える）、後に `op done --result <結果>`。照合（手順 3）の `gh` も元の checkout で実行する（作業場所では別のアカウントになり、PR が見えないことがある）。

`work.py` の場所は `playbook-pause-safely` と同じ。

返答: 前の担当がどこで止まったか、引き継いだものとやり直したもの（できれば無し）、照合した外部操作とその結果、再開点、結果。
