# bb-writ

bb-writ（読み方：ビービー・リット）は、Claude Code・Codex・Grok Build 向けのスキルのプラグイン。入口のスキルが「直して」「作って」「調べて」といった普通の依頼を受け、リポジトリを読んで実装・修正・調査までやり遂げ、実物で動作を確かめた結果を根拠とともに返す。Issue の書式に整えたり、対象のリポジトリを登録したりする準備は要らない。

紹介動画（35 秒）。中盤は、実際の Claude Code のセッションで和暦の不具合を直す様子を早送りしたもの。

https://github.com/user-attachments/assets/dd9c669e-0d1e-4645-a631-ea70809b345a

## 前提

基本機能に要るもの。基本機能は、手元で調べる・直す・確かめるまでの使い方。push・PR まで頼む場合と、`delegate`・`interrogate`・`reflect`・`playbook-eval` を使う場合は、[任意機能の準備](#任意機能の準備)も要る。

- OS：Linux・macOS・WSL2。Windows のネイティブ環境（PowerShell など）は対象外。入口がファイルに書く前に実行する `scripts/work.py` が、Unix にしか無い `fcntl` を使うので動かない。読むだけの使い方も、ネイティブ環境では確かめていない。Windows では、WSL2 の中に CLI を入れて、その中で起動する。
- CLI：Claude Code・Codex・Grok Build のどれか 1 つ。
- Python：3.9 以上を `python3` で起動できること。標準ライブラリだけを使うので、パッケージは要らない。macOS では、Command Line Tools に付く `python3`（3.9）で足りる。
- git：2.38 以上（`git --version`）。タスクの作業場所の一覧に 2.36、squash・rebase で統合した後の片付けの確認に 2.38 で入った機能を使う。macOS では、Command Line Tools に付く `git` がこれ以上なら足りる（macOS 15 の Command Line Tools は 2.39.5 と報告されているが、手元では確かめていない）。
- GitHub CLI（`gh`、ログイン済み）：Issue・PR を名指しして頼むときと、push・PR などの外部操作の前にアカウントと書き込み権限を確かめるときに使う。gh が無いと、名指しした Issue・PR の本文とコメントを読めず、GitHub への外部操作はこの確かめで止まる。Issue・PR を名指しせずに、リポジトリを読む・手元で直すだけなら要らない。

bb-writ 専用のログイン・dotenvx・もう一方の CLI は、push・PR まで頼むときと任意機能で使う（[任意機能の準備](#任意機能の準備)）。専用ログインが無いと、bb-writ は確かめ役・審査役・返す前の判定の子を立てられない。手元の作業は、その子が無いことを返答に書いて進むが、push・PR は、返す前の判定の子の記録が無いので拒否される。

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

その次に読むのは [スキルの使い方](docs/skills.md)。`interrogate`・`reflect`・`correct` など、自分で呼ぶスキルの頼み方と返ってくるものを 1 本ずつ説明している。

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

CLI を非対話で動かすときは（`claude -p` など）、Claude Code では `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` を付ける。付けないと、背景の処理の途中の文（「終わると通知が届きます」など）が結果として出て、`--output-format json` や stream-json の利用者が完成の報告と取り違える。

```sh
CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 claude -p "/bb-writ:bb-writ <依頼>" --output-format json < /dev/null
```

Claude Code は、普通の依頼文だけでは入口を使わない。入口の名前を付けずに使わせるには、作業するリポジトリの `CLAUDE.md` に「技術的な仕事は、まず bb-writ:bb-writ スキルを使って始める」と書く。Codex と Grok Build は、入口の名前を付けない普通の依頼でも入口を最初に読んだ（試験での観測）。どの CLI でも hook の設定は要らない。

## 仕組みと含まれるスキル

入口のスキル `bb-writ` が呼ばれると、依頼を言い直し、最初に手引きを呼び、リポジトリを読み、必要な原則だけを読んで進め、「何を実行し、どの結果を見て、依頼を満たすと判断したか」を返す。

- 手引き：`playbook-investigation`（読んで答える）・`playbook-bug`（不具合を直す）・`playbook-feature`（振る舞いを足す・変える）・`playbook-docs-skill`（文書・スキルを直す）・`playbook-verify`（確かめて返す。検証の条件の全文）・`playbook-deliver`（担当・記録・push や PR などの外部操作）・`playbook-parallel`（タスクごとの作業場所の確保・再開・統合・片付け）・`playbook-session-pickup`（続きを引き受ける）・`playbook-pause-safely`（安全に止める）・`playbook-eval`（スキルや設定の変種を同じ依頼で走らせ、目隠しで比べる）。
- 委任と審査：`delegate`（別系統の CLI の起動）、`interrogate`（2 系統の審査役）。
- 検証：`create-verification-skill`（対象のリポジトリに検証スキルを作って 1 回通す）、`create-formal-verification-skill`（順序・途中の死・再開の約束を全部の順序で確かめる検査を作る。pstack の同名のスキルの考え方から書き直した）、`create-property-test-skill`（入力の形・文字コード・大きさの約束を、揺れの一覧を組み合わせた多数の入力で確かめる性質の試験を作る）。
- 学び：`reflect`（振り返って行き先へ送る）、`correct`（繰り返す誤りを起こせなくする）。
- 探索：`unknowns`（実装前に、盲点の洗い出しや案の比較をする）。
- 原則：1 本ずつ別のスキル `principle-*`（18 本）。入口の「原則の索引」から、仕事の局面に当たるものだけを呼ぶ。

## 任意機能の準備

push・PR まで頼む場合と、`delegate`・`interrogate`・`reflect`・`playbook-eval` を使う場合は、[前提](#前提)に加えて、ここの準備が要る。bb-writ は、審査役・確かめ役・返す前の判定の子を、CLI の別プロセスとして起動する。その子は、普段のログインでなく、bb-writ だけが使うログイン（専用ログイン）で動く。分けるのは、子の使った量を普段の作業と区別し、普段の設定（個人の指示・hook・プラグイン）を子に混ぜないため。

何を用意するか：

- push・PR だけなら、使っている CLI の専用ログインがあれば足りる。返す前の判定の子を、使っている CLI と同じ CLI で起動するため。Claude Code なら、下の dotenvx と、Linux・WSL2 では `bwrap`・`socat` も要る。
- `interrogate` の別系統の審査役と、`playbook-eval`・`reflect` の別系統の読み手には、もう 1 つの CLI（Claude Code・Codex・Grok Build のどれか）と、その専用ログインが要る。

専用ログインが無いまま push・PR を頼むと、bb-writ は返す前の判定の子を起動できず、push・PR は拒否される。拒否の文と、子を起動できなかったときの返答に、この節が出てくる。用意したら、同じセッションでもう一度頼めばよい（bb-writ は子を起動するたびに専用ログインの置き場を見る）。

### Claude Code の専用ログイン

[dotenvx](https://dotenvx.com/docs/install) を入れてから、次を順に実行する。トークンは暗号化した `~/.config/bb-writ/.env` に置き、復号の鍵は同じ場所の `.env.keys` に置く。bb-writ は子を起動するときだけ、dotenvx でトークンを子の環境に渡す。

```sh
mkdir -p -m 700 ~/.config/bb-writ
claude setup-token
```

`claude setup-token` の案内に従ってログインすると、長期のトークンが表示される（Claude の契約が要る）。表示されたトークンをコピーしておく。

次の 1 行を実行すると、何も表示されない入力待ちになる。そこにトークンを貼って Enter を押す。トークンをコマンド行に書かないので、シェルの履歴に残らない（bash・zsh で動く）。

```sh
IFS= read -rs T
```

続けて、次を実行する。

```sh
dotenvx set CLAUDE_CODE_OAUTH_TOKEN "$T" --no-native -f ~/.config/bb-writ/.env -fk ~/.config/bb-writ/.env.keys; unset T
```

2 つを 1 度に貼らない。`read` が 2 行目を入力として読み、その後に貼ったトークンがコマンドとして実行されて、履歴に残る。

`--no-native` を省かない。dotenvx 2.25 以降は、OS の secret store（macOS のキーチェーンなど）が使えると、鍵を `.env.keys` でなくそこに置くことがある。そこに置かれると、bb-writ は鍵を見つけられず、子を起動しない。

Linux・WSL2 では、子の Claude Code が OS の sandbox（bubblewrap）の中で動き、sandbox を起動できなければ子は起動しない。`command -v bwrap socat` を実行し、どちらかが出なければ入れる（Debian・Ubuntu では `sudo apt install bubblewrap socat`）。macOS では要らない。

できたかを確かめる。まず、鍵のファイルがあるかを見る。

```sh
ls ~/.config/bb-writ/.env.keys
```

見つからなければ、鍵が secret store に置かれている（`--no-native` を付けずに置いた）か、鍵のファイルが消えている。`.env` を残したまま置き直しても鍵のファイルはできないので、`rm ~/.config/bb-writ/.env` で消してから、`IFS= read -rs T` からやり直す（トークンが手元に無ければ、`claude setup-token` から）。

鍵のファイルがあれば、専用ログインで 1 回問い合わせる。

```sh
CLAUDE_CONFIG_DIR=~/.config/bb-writ/claude-home env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN dotenvx run -q -f ~/.config/bb-writ/.env -fk ~/.config/bb-writ/.env.keys -- claude -p "ok とだけ返して" --model haiku < /dev/null
```

`ok` と返れば、専用ログインで動く。先頭の `env -u` は、シェルにある API キーを外す（キーがあると、トークンより先に使われる。launcher も子に渡さない）。`Failed to authenticate. API Error: 401` で終われば、トークンが違うので、`claude setup-token` からやり直す。`claude auth status` は、トークンが無効でも `"loggedIn": true` を返すので、確かめに使わない。`~/.config/bb-writ/claude-home` は、子の Claude Code の設定と会話記録の置き場で、初めて使うときにできる。

### Codex の専用ログイン

```sh
mkdir -p -m 700 ~/.config/bb-writ ~/.config/bb-writ/codex-home
CODEX_HOME=~/.config/bb-writ/codex-home codex login
```

置き場を先に作る。`CODEX_HOME` の指す場所が無いと、Codex は「path does not exist」で止まる。

確かめるには `CODEX_HOME=~/.config/bb-writ/codex-home codex login status` を実行し、`Logged in` で始まる行が出ればよい。

この専用ログインに bb-writ を入れるかは任意。入れると、子のスキルの一覧に bb-writ が出る。入れるときは、[インストール](#codex)の 2 つのコマンドの先頭に `CODEX_HOME=~/.config/bb-writ/codex-home` を付けて実行する。入れた写しは版ごとに入れ直す。写しが起動する側の bb-writ と中身で違うと、Codex の子は起動せず、入れ直すコマンド（`CODEX_HOME` を付けた [Codex の更新](#codex-1)と同じ 3 つ）を示す。

### Grok Build の専用ログイン

```sh
mkdir -p -m 700 ~/.config/bb-writ ~/.config/bb-writ/grok-auth
GROK_HOME=~/.config/bb-writ/grok-auth grok login
```

Grok Build には、ログインの状態を見るコマンドが無い（`grok --help` の一覧で、ログインに関わるのは `login` と `logout` だけ。`grok usage` は、この使い方では「can't show your plan's usage here yet」で終わる）。`~/.config/bb-writ/grok-auth/auth.json` ができていればよい。認証が通らないと、子は「Not signed in」で終わる。そのときは同じコマンドでログインし直す。

呼び出し方の詳細は [`delegate` の手引き](skills/delegate/SKILL.md) にある。

## 開発

この節は、bb-writ 自体を直す人向け。

- 手元の作業ツリーを Claude Code で試すには、配布版の代わりに `claude plugin marketplace add <このリポジトリのパス>` で登録する。作業ツリーへの編集は、次のセッションか `/reload-plugins` で反映する。
- 置き場：スキルは `skills/<名前>/SKILL.md`（上の一覧と同じ名前）。補助スクリプトは `scripts/launch.py`（別系統の CLI の起動と報告の検査）と `scripts/work.py`（担当の貸し出しと外部操作の記録）で、標準ライブラリだけを使う。
- 必要な内容は、すべて SKILL.md の本文に入れる。プラグイン内の別ファイルを読ませると、Claude Code が承認を求めるため。
- 並行開発の仕組み（作業場所の分離・再開・統合・片付け）を変えたときは、このリポジトリの検証スキル `verify-parallel`（`.agents/skills/verify-parallel/`。Claude Code は `.claude/skills/` の symlink から読む）で、実際の CLI を使って確かめる。配布物（`skills/`）には含めない。
- 試験：`python3 -B -m unittest discover -s tests -t .`。入力の形の性質の試験（`tests/test_properties.py`）は Hypothesis を使い、無ければ飛ばす。入れるときは `pip install -r tests/requirements.txt`（試験だけの依存）。
- トークン・認証情報を画面・ログ・会話に出さない。ワーカーは bb-writ 専用のログインを使う（[`delegate` の手引き](skills/delegate/SKILL.md)）。
- 対象のリポジトリを増やすたびに bb-writ 自身を直さない。対象に固有のことは、対象のリポジトリの中で解く。

## 生い立ち

bb-writ は [cc-rite-workflow](https://github.com/B16B1RD/cc-rite-workflow)（rite）の後継。rite は Issue を起点に、仕様・実装・レビュー・PR を固定の工程で回していた。作者は rite を使い続けるうちに、生成 AI で作る時代に旧来の工程をなぞることに疑問を持ち、開発の進め方そのものを見直した。その際、[@poteto](https://x.com/poteto)（Lauren Tan）氏の [pstack](https://github.com/cursor/plugins/tree/main/pstack) の考え方に共感し、rite で得た知見と pstack を参考に、CLI ハーネスで動くスキルのプラグインとして作り直した。

- **rite から**：子への委任の範囲と実効設定の記録、専用ログイン、根拠に基づく報告、中断・訂正・外部操作（push・PR・マージ）の記録と照合。Issue の書式・固定の工程・対象の登録は捨てた。
- **pstack から**：原則のうち 12 本（CLI ハーネス向けに日本語で書き直した。残る 6 本は作者の原則）、入口が依頼を読んで手引きを選ぶ形、`correct`・`create-verification-skill`・`create-formal-verification-skill`・`interrogate`・`reflect` の考え方。出典と著作権表示は [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。
- **pstack と違うところ**：pstack の一部の機能が使う Grok Bot（webhook で起動するボット）と、Cursor 専用の機能（bugbot・automations など）を前提にしない。別系統のモデルによる判断、作業の継続、権限を、Claude Code・Codex・Grok Build の正式な実行・再開・プラグインの機能と薄い補助コードで成り立たせる。

## ライセンス

MIT（[`LICENSE`](LICENSE)）。pstack をもとにした部分の著作権表示とライセンス文は [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。
