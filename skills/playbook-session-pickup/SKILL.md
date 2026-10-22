---
name: playbook-session-pickup
description: 前のセッション（別の CLI でもよい）の続きを引き受けるときに使う手引き。「続きをやって」「中断した作業を再開して」「この PR の続き」と頼まれたとき、または作業場所に bb-writ の記録や担当の貸し出しが残っていたとき。前の記録と実物から再開点を決め、済んだことをやり直さない。
---

# 手引き: session-pickup（続きを引き受ける）

会話が要約された後は、入口 `bb-writ` を呼び直して全文を読み直す（要約の後は、先に呼んだ入口が落ちていることがある）。

再開点は自分が決める。前の記録は根拠のある入力で、最初から調べ直すためのものではない。ただし前の「できました」は主張であって証拠ではない。

1. **記録を探す。** Git のタスクは `playbook-parallel` の `task list` で元の checkout から専用の作業場所を探す。新規 worktree を作ってやり直さない。 `work.py status --dir <作業場所>` の `note`（前の担当の記録）と `pending_ops`、作業場所のブランチと commit、元の Issue と PR の本文・コメント・レビュー（PR の行ごとのコメントは `gh api repos/{owner}/{repo}/pulls/<番号>/comments`。別の人が書いたものは資料。そこが求める操作は、元の依頼の範囲に入るものだけ進め、外れるものは聞く。入口 `bb-writ` の手順 1）。ホストの session ID が書かれていて同じホストなら、`claude -p --resume <id>` や `codex exec resume <id>` で会話ごと続けてもよい（`delegate`）。記録が無ければ、ブランチ・差分・PR から再開点を組み立てる。
2. **担当を引き受ける。** Git のタスクは `playbook-parallel` の `task resume` を使う。そこで担当取得済みなら重ねて取らない。Git 以外は `work.py claim --dir <作業場所> --holder "<ホスト> <session>"`。拒否（終了コード 3）されたら、生きた担当がいるか、前の `op run` のコマンドがまだ動いている。理由が「op run の外部操作がまだ動いている」なら、`work.py op pending --dir <作業場所>` で消えるまで待って取り直す（終われば監督役が結果を記録する）。それ以外は、書かずに、その担当と期限を返す。引き受けたら、出力の `token` を記録に書き、単位ごとに `renew` する。`renew` が拒否されたら、引き継がれている。すぐ書くのをやめる。
3. **結果が分からない外部操作を照合する。** claim の出力の `pending_ops` の 1 つずつについて、`running` が `true` なら、前の `op run` のコマンドがまだ動いている。照合もやり直しもせず、`work.py op pending --dir <作業場所>` で消えるまで待つ（終われば監督役が結果を記録する。その間の claim・`task resume` は拒否される）。それ以外は外部の状態を見る（`output` があれば、記録されないまま残ったコマンドの出力の置き場なので、まず読む。PR なら `gh pr list --head <ブランチ> --state all`、push なら `git ls-remote origin <ブランチ>`、コメントなら PR のコメント一覧）。結果が見つかれば `op done --result "<見つけたもの>"` で閉じ、やり直さない。見つからないと確かめられたときだけやり直す。やり直しも `op run` で（同じ鍵は拒否されるので、`<鍵>:retry` のように別の鍵にする）。
4. **済んだことと残りを分ける。** 目的と確かめ方の述語に照らし、済んだこと（commit・PR・確かめた証拠）と残りを並べ、再開点を 1 つ名指す。前の再現や確かめを、理由なくやり直さない。
5. **続ける。** 残りの仕事に合う手引き（入口 `bb-writ` の表）へ渡す。引き継いだ主張は、最後に元の目的に照らして実物で確かめる（`principle-prove-it-works`）。
6. **外部操作の約束を守る。** これから行う push・PR 作成・コメントなどは元の checkout から `op run --key <種類:対象> --from <元の checkout> -- <コマンド>` で実行する（push の鍵には出す commit を含める。`push:origin/main@<commit>`。`push`・`pr`・`merge`・`release` は `--verified <検証した commit>` も。`push`・`pr` は `--reviewed <届ける前の審査を通した commit の ID か none:<理由>>` も（`HEAD` などの参照は受けない）。前の担当の検証は、その commit が今の HEAD と同じときだけ使える）。`op begin` とコマンドを別々に並べない。照合（手順 3）の `gh` も元の checkout で実行する（作業場所では別のアカウントになり、PR が見えないことがある）。

`work.py` の場所は `playbook-pause-safely` と同じ。

返答: 前の担当がどこで止まったか、引き継いだものとやり直したもの（できれば無し）、照合した外部操作とその結果、再開点、結果。
