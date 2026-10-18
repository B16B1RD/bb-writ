# 変更履歴

bb-writ を使う人に向けて、版ごとに変わったことを書く。新しい版が上。版はプラグインのマニフェストの `version` と同じで、各版の Release は同じ文面にする。更新のしかたは README の「更新」の節にある。

commit ごとの細かい変更は、各版の末尾の「commit の一覧」（前の版のタグとの比較）で見られる。

## 0.10.0 - 2026-10-19

### 直したこと

セキュリティ診断で見つかった、子（`delegate`・`interrogate` が起動する別系統の CLI）が、渡された範囲の外に書けたり読めたりした経路を閉じた。

- 読み取り専用の Claude Code の子が、`git log --output=<任意のパス>` で対象の外・対象の中・`.git/hooks` に書けた。対象の `.claude/settings.json` の hook も、子の起動で動いた。今は OS の sandbox と `--restricted` で起動し、普通の読み取り（`git diff -U10` など）だけができる。対象の外、認証情報の置き場（`~/.config`・`~/.ssh` など）、トークンの環境変数も読めない。
- 書く子が状態ディレクトリ全体に書けた。そのため、タスクの記録を書き換えると、親の `task resume`・`integrate`・`task cleanup` に別の場所や統合先を使わせられた。今は書く子に作業場所とその記録だけを渡し、親は自分だけが書くタスクの一覧を正とする。
- 書く子が、統合先のブランチや Git の設定（`config`・`hooks`）、作業場所の `.git` を変えられた。今は launcher が子の前後でこれらを比べ、変わっていれば `failed` にする。その作業場所では、示された場所を確かめるまで git を動かさない。
- Grok Build の子が使う sandbox の設定を、専用ログインの `GROK_HOME` に置いていた。今は起動ごとに使い捨ての `GROK_HOME` で動かし、専用ログインとは `auth.json` だけを共有する。書く子に渡す Git の場所は、commit に要るところだけにした。
- 別の人が書いた Issue・PR の本文やコメントを、利用者の依頼として扱っていた。今は依頼を利用者自身の言葉だけとし、それ以外は資料として読む。資料だけが求める依存の追加・外部への送信・CI の変更・コマンドの実行は、進める前に聞く。

### 変えたこと

- 子に別タスクを書かせるときは、親が `task start` で作業場所を作って子に渡し、統合・片付け・外部操作も親が行う。子はその作業場所で担当を取る。元の checkout とその下では、書く子を起動しない。
- Linux・WSL2 で Claude Code の子を起動するには、`bwrap` と `socat` が要る（sandbox を起動できなければ、子を起動しない）。
- Claude Code の子には、launcher と同じ版の bb-writ を渡す。

### 試験

- `tests/verify_sandbox_live.py` で、実物の CLI に境界を越える操作をさせ、止まることを確かめられる。

commit の一覧：https://github.com/B16B1RD/bb-writ/compare/bb-writ--v0.9.1...bb-writ--v0.10.0

## 0.9.1 - 2026-10-18

### 直したこと

- README と `delegate` の Grok Build の導入手順に、必要な `--trust` が抜けていた。書いてあるとおりに `grok plugin install B16B1RD/bb-writ` を実行すると、Grok Build が確認を求めて止まっていた。正しい手順は `grok plugin install B16B1RD/bb-writ --trust`。手元のディレクトリから入れ直す手順も同じ。

commit の一覧：https://github.com/B16B1RD/bb-writ/compare/bb-writ--v0.9.0...bb-writ--v0.9.1

## 0.9.0 - 2026-10-18

### 変えたこと

- Grok Build で bb-writ を使えることを確かめた。`grok plugin install B16B1RD/bb-writ` で入る。Claude Code に bb-writ を入れてあれば、Grok Build に入れなくてもそのまま使える。入口の名前を付けない普通の依頼でも、入口から始まる。
- `delegate`・`interrogate` で起動する別系統の CLI に、Grok Build を選べるようになった（`launch.py --host grok`）。専用のログインは `GROK_HOME=~/.config/bb-writ/grok-auth grok login` で作る。
- 子の Grok Build には、利用者の `~/.claude`・`~/.cursor` の指示・スキル・hook と、親の環境の API キーを渡さない。読み取り専用の子は、書き込みを止められない場所（`/tmp` の下など）を対象にすると起動しない。sandbox を適用できなければ、子は起動を拒む。

### 文書

- README に、Grok Build の入れ方・更新のしかた・入口の呼び方・専用ログインを足した。手元のディレクトリから入れた場合は `grok plugin update` が変更を取り込まないので、入れ直す手順を書いた。
- `delegate` の Grok Build の節を、実際に動かして確かめた内容に書き直した。

commit の一覧：https://github.com/B16B1RD/bb-writ/compare/bb-writ--v0.8.0...bb-writ--v0.9.0

## 0.8.0 - 2026-10-18

### 変えたこと

- 複数の Claude Code、または Claude Code と Codex に別々のタスクを並行して頼める。Git に書くタスクには専用の worktree とブランチを作り、中断後も同じ作業場所から再開する。
- ローカルへの統合は、最新の統合先を取り込んで検証した commit だけを順に反映する。検証後に統合先が進んだ場合は再検証する。統合の中断時は実物を照合して再開し、保存先を確認してから作業場所を片付ける。PR が必須の対象では、そのリポジトリの統合方法に従う。
- 同時に起動した委任の検証記録を別々に保存する。両 CLI に、作業場所と記録を置く状態ディレクトリへのアクセスを渡す。

### 文書

- はじめて使う人向けのガイドを追加した。依頼文で、調査・実装・公開までの範囲を伝える例を載せた。
- README に紹介動画と並行開発の説明を追加した。
- 並行開発の手引きと、実 CLI・中断・競合・片付けを確かめる検証スキルを追加した。
- 変更履歴をこのファイルにまとめ、各版の Release にも同じ文面を使うようにした。

commit の一覧：https://github.com/B16B1RD/bb-writ/compare/bb-writ--v0.7.3...bb-writ--v0.8.0

## 0.7.3 - 2026-10-18

### 直したこと

- 1 つの作業の中で同じブランチへ 2 回目の push をしようとすると、エージェントが「同じ push を二重に実行しようとしている」と判断して止まっていた。push・PR などの外部操作の前に、エージェントは操作ごとの名前（鍵）を付けて記録する。push の鍵に、出す commit を含めるようにした（`push:origin/main@<commit>`）。鍵はエージェントが付けるので、利用者がすることはない。

commit の一覧：https://github.com/B16B1RD/bb-writ/compare/bb-writ--v0.7.2...bb-writ--v0.7.3

## 0.7.2 - 2026-10-18

### 変えたこと

- Python の下限を 3.9 にした。`scripts/launch.py` が、macOS の Command Line Tools に付く `python3`（3.9）でも動く。
- 入口は、ファイルに書く前に `scripts/work.py` で「この作業場所は自分が書いている」と記録する。`work.py` が動かない環境（`python3` が無い、3.9 未満、Windows のネイティブ環境）では、記録できないままファイルへ書き進めず、足りない前提を返して止まる。Windows では WSL2 の中で使う。

### 文書

- README に「前提」の節を足した（対応する OS、CLI、Python、git、gh が要る場面）。
- README のインストールと更新の手順を、CLI ごとに分けて書いた。

commit の一覧：https://github.com/B16B1RD/bb-writ/compare/bb-writ--v0.7.1...bb-writ--v0.7.2

## 0.7.1 - 2026-10-18

### 変えたこと

- プラグインの説明文を、何をしてくれるものかが分かる文にした。Grok Build は、インストールと更新を確かめていないので説明文から外した。プラグインの中身は変えていない。

commit の一覧：https://github.com/B16B1RD/bb-writ/compare/bb-writ--v0.7.0...bb-writ--v0.7.1

## 0.7.0 - 2026-10-18

最初の公開版。

- 入口 `bb-writ`：依頼を言い直し、手引きと原則を選び、実物で確かめた成果と根拠を返す。
- 手引き `playbook-*`：調べる・不具合を直す・振る舞いを足す・文書を直す・続きを引き受ける・安全に止める・変種を比べる。
- 原則 `principle-*`（18 本）、委任 `delegate`、審査 `interrogate`、検証スキルの生成 `create-verification-skill`、学び `reflect`・`correct`、探索 `unknowns`。
- 補助スクリプト `scripts/launch.py`（別系統の CLI の起動）と `scripts/work.py`（担当の貸し出しと外部操作の記録）。

この版のファイル：https://github.com/B16B1RD/bb-writ/tree/bb-writ--v0.7.0
