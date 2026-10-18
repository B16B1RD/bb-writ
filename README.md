# bb-writ

bb-writ（読み方：ビービー・リット）は、Claude Code・Codex・Grok Build 向けのスキルのプラグイン。入口のスキルが「直して」「作って」「調べて」といった普通の依頼を受け、リポジトリを読んで実装・修正・調査までやり遂げ、実物で動作を確かめた結果を根拠とともに返す。Issue の書式に整えたり、対象のリポジトリを登録したりする準備は要らない。

紹介動画（35 秒）。中盤は、実際の Claude Code のセッションで和暦の不具合を直す様子を早送りしたもの。

https://github.com/user-attachments/assets/dd9c669e-0d1e-4645-a631-ea70809b345a

## 前提

基本機能に要るもの。基本機能は、`delegate`・`interrogate` で別系統の CLI（使っている CLI 以外の Claude Code・Codex・Grok Build）を起動する場合を除く、すべての使い方。

- OS：Linux・macOS・WSL2。Windows のネイティブ環境（PowerShell など）は対象外。入口がファイルに書く前に実行する `scripts/work.py` が、Unix にしか無い `fcntl` を使うので動かない。読むだけの使い方も、ネイティブ環境では確かめていない。Windows では、WSL2 の中に CLI を入れて、その中で起動する。
- CLI：Claude Code・Codex・Grok Build のどれか 1 つ。
- Python：3.9 以上を `python3` で起動できること。標準ライブラリだけを使うので、パッケージは要らない。macOS では、Command Line Tools に付く `python3`（3.9）で足りる。
- git：macOS では、Command Line Tools に付く `git` で足りる。
- GitHub CLI（`gh`、ログイン済み）：Issue・PR を名指しして頼むときと、push・PR などの外部操作の前にアカウントと書き込み権限を確かめるときに使う。gh が無いと、名指しした Issue・PR の本文を読めず、GitHub への外部操作はこの確かめで止まる。Issue・PR を名指しせずに、リポジトリを読む・手元で直すだけなら要らない。

もう一方の CLI・dotenvx・bb-writ 専用のログインは、任意機能（[任意機能の準備](#任意機能の準備)）だけで使う。

## インストール

使う CLI（Claude Code・Codex・Grok Build のどれか）をインストールしてから、その CLI の手順を実行する。

### Claude Code

```sh
claude plugin marketplace add B16B1RD/bb-writ
claude plugin install bb-writ@bb-writ
```

### Codex

```sh
codex plugin marketplace add B16B1RD/bb-writ
codex plugin add bb-writ@bb-writ
```

### Grok Build

```sh
grok plugin install B16B1RD/bb-writ --trust
```

`--trust` は、プラグインを信頼して有効にすることの確認で、付けないと Grok Build は導入せずに止まる。bb-writ が有効にするのはスキルだけで、hook と MCP サーバーは含まない。

Grok Build は Claude Code のプラグインも読む（Claude 互換の既定の設定）。Claude Code に bb-writ を入れてあれば、Grok Build に入れなくても使える。両方に入れた場合は、Grok Build に入れたほうが使われる。

インストール後は、新しい CLI セッションを開いて使う。

## 並行して開発する

Claude Code を複数起動する場合も、Claude Code と Codex を組み合わせる場合も、それぞれに別のタスクを依頼できる。Git に書くタスクは bb-writ が専用の worktree・ブランチを作り、同じ checkout を同時に編集しない。読むだけの調査には作らない。

ローカルに統合するときは、最新の統合先を取り込んで検証し、反映を直列にする。検証後に統合先が進んだら、最新の組み合わせで検証し直す。途中のタスクは作業場所と記録を残して再開できる。PR が必須の対象では、そのリポジトリの統合方法に従う。

手動で作業場所を調べるなら `python3 <プラグインの置き場>/scripts/work.py task list --dir <元の checkout>`。準備・再開・統合・片付けの詳細は [`playbook-parallel`](skills/playbook-parallel/SKILL.md)。worktree で必要な依存の準備と、DB・ポートなどの分離には対象の開発手順を使う。

## 更新

インストールに使った CLI で実行する。marketplace の情報を先に更新し、プラグインを更新する。

版ごとに変わったことは [`CHANGELOG.md`](CHANGELOG.md) にある。新しい版が出たときに知らせを受けるには、GitHub のリポジトリで「Watch」→「Custom」→「Releases」を選ぶ。

### Claude Code

```sh
claude plugin marketplace update bb-writ
claude plugin update bb-writ@bb-writ
```

更新後は Claude Code を再起動する。

### Codex

Codex は版ごとの写しを置くので、marketplace の更新後に削除・再追加する。

```sh
codex plugin marketplace upgrade bb-writ
codex plugin remove bb-writ@bb-writ
codex plugin add bb-writ@bb-writ
```

更新後は、新しい Codex セッションを開く。

### Grok Build

GitHub から入れた場合、`update` は既定のブランチの最新の commit を取り込む（リリースのタグではない）。

```sh
grok plugin update
```

手元のディレクトリから入れた場合は写しが置かれ、`update` は「already live」と表示しても変更を取り込まない。削除して入れ直す（削除はプラグインの名前で指定する）。

```sh
grok plugin uninstall bb-writ
grok plugin install <ディレクトリ> --trust
```

更新後は、新しい Grok Build セッションを開く。

## 使い始める

依頼文の書き方（どこまで任せるか、Issue からドラフト PR・マージまでの進め方、`unknowns` との使い分け）は [はじめての bb-writ](docs/getting-started.md) にある。

作業するリポジトリで CLI を開き、入口のスキルを明示して頼む。たとえば README を調べるだけなら、次のように入力する。

Claude Code:

```text
/bb-writ:bb-writ README を読み、このプロジェクトの使い方を説明して。ファイルは変更しないで。
```

Codex:

```text
$bb-writ:bb-writ README を読み、このプロジェクトの使い方を説明して。ファイルは変更しないで。
```

Grok Build:

```text
/bb-writ README を読み、このプロジェクトの使い方を説明して。ファイルは変更しないで。
```

Claude Code は、普通の依頼文だけでは入口を使わない。入口の名前を付けずに使わせるには、作業するリポジトリの `CLAUDE.md` に「技術的な仕事は、まず bb-writ:bb-writ スキルを使って始める」と書く。Codex と Grok Build は、入口の名前を付けない普通の依頼でも入口を最初に読んだ（試験での観測）。どの CLI でも hook の設定は要らない。

## 仕組みと含まれるスキル

入口のスキル `bb-writ` が呼ばれると、依頼を言い直し、最初に手引きを呼び、リポジトリを読み、必要な原則だけを読んで進め、「何を実行し、どの結果を見て、依頼を満たすと判断したか」を返す。

- 手引き：`playbook-investigation`（読んで答える）・`playbook-bug`（不具合を直す）・`playbook-feature`（振る舞いを足す・変える）・`playbook-docs-skill`（文書・スキルを直す）・`playbook-session-pickup`（続きを引き受ける）・`playbook-pause-safely`（安全に止める）・`playbook-eval`（スキルや設定の変種を同じ依頼で走らせ、目隠しで比べる）。
- 委任と審査：`delegate`（別系統の CLI の起動）、`interrogate`（2 系統の審査役）。
- 検証：`create-verification-skill`（対象のリポジトリに検証スキルを作って 1 回通す）。
- 学び：`reflect`（振り返って行き先へ送る）、`correct`（繰り返す誤りを起こせなくする）。
- 探索：`unknowns`（実装前に、盲点の洗い出しや案の比較をする）。
- 原則：1 本ずつ別のスキル `principle-*`（18 本）。入口の「原則の索引」から、仕事の局面に当たるものだけを呼ぶ。

## 任意機能の準備

`delegate` や `interrogate` で別系統の CLI を起動する場合は、[前提](#前提)に加えて次を用意する。

- 起動する CLI と、起動される CLI（Claude Code・Codex・Grok Build のうち 2 つ以上）。
- 起動される CLI の bb-writ 専用ログイン。Codex で `interrogate` を使うときは、同じ Codex の審査役も別プロセスの Codex として起動するので、Codex のログインも要る。
  - Claude Code：`claude setup-token` で得たトークンを、`~/.config/bb-writ/.env` の `CLAUDE_CODE_OAUTH_TOKEN` に [dotenvx](https://dotenvx.com/) で暗号化して置く（鍵の `.env.keys` も同じ場所）。起動するときも dotenvx がトークンを子の環境にだけ渡すので、dotenvx を入れておく。
  - Codex：`CODEX_HOME=~/.config/bb-writ/codex-home codex login`。
  - Grok Build：`GROK_HOME=~/.config/bb-writ/grok-auth grok login`。

呼び出し方は [`delegate` の手引き](skills/delegate/SKILL.md) にある。

## 開発

この節は、bb-writ 自体を直す人向け。

- 手元の作業ツリーを Claude Code で試すには、配布版の代わりに `claude plugin marketplace add <このリポジトリのパス>` で登録する。作業ツリーへの編集は、次のセッションか `/reload-plugins` で反映する。
- 置き場：スキルは `skills/<名前>/SKILL.md`（上の一覧と同じ名前）。補助スクリプトは `scripts/launch.py`（別系統の CLI の起動と報告の検査）と `scripts/work.py`（担当の貸し出しと外部操作の記録）で、標準ライブラリだけを使う。
- 必要な内容は、すべて SKILL.md の本文に入れる。プラグイン内の別ファイルを読ませると、Claude Code が承認を求めるため。
- 試験：`python3 -B -m unittest discover -s tests -t .`
- トークン・認証情報を画面・ログ・会話に出さない。ワーカーは bb-writ 専用のログインを使う（[`delegate` の手引き](skills/delegate/SKILL.md)）。
- 対象のリポジトリを増やすたびに bb-writ 自身を直さない。対象に固有のことは、対象のリポジトリの中で解く。

## 生い立ち

bb-writ は [cc-rite-workflow](https://github.com/B16B1RD/cc-rite-workflow)（rite）の後継。rite は Issue を起点に、仕様・実装・レビュー・PR を固定の工程で回していた。作者は rite を使い続けるうちに、生成 AI で作る時代に旧来の工程をなぞることに疑問を持ち、開発の進め方そのものを見直した。その際、[@poteto](https://x.com/poteto)（Lauren Tan）氏の [pstack](https://github.com/cursor/plugins/tree/main/pstack) の考え方に共感し、rite で得た知見と pstack を参考に、CLI ハーネスで動くスキルのプラグインとして作り直した。

- **rite から**：子への委任の範囲と実効設定の記録、専用ログイン、根拠に基づく報告、中断・訂正・外部操作（push・PR・マージ）の記録と照合。Issue の書式・固定の工程・対象の登録は捨てた。
- **pstack から**：原則のうち 12 本（CLI ハーネス向けに日本語で書き直した。残る 6 本は作者の原則）、入口が依頼を読んで手引きを選ぶ形、`correct`・`create-verification-skill`・`interrogate`・`reflect` の考え方。出典と著作権表示は [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。
- **pstack と違うところ**：pstack の一部の機能が使う Grok Bot（webhook で起動するボット）と、Cursor 専用の機能（bugbot・automations など）を前提にしない。別系統のモデルによる判断、作業の継続、権限を、Claude Code・Codex の正式な実行・再開・プラグインの機能と薄い補助コードで成り立たせる。

## ライセンス

MIT（[`LICENSE`](LICENSE)）。pstack をもとにした部分の著作権表示とライセンス文は [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。
