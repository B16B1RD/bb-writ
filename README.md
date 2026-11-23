# bb-writ

bb-writ（読み方：ビービー・リット）は、Claude Code・Codex・Grok Build 向けのスキルのプラグイン。入口のスキルが「直して」「作って」「調べて」といった普通の依頼を受け、リポジトリを読んで実装・修正・調査までやり遂げ、実物で動作を確かめた結果を根拠とともに返す。Issue の書式に整えたり、対象のリポジトリを登録したりする準備は要らない。

紹介動画（35 秒）。中盤は、実際の Claude Code のセッションで和暦の不具合を直す様子を早送りしたもの。

https://github.com/user-attachments/assets/dd9c669e-0d1e-4645-a631-ea70809b345a

## 前提

基本機能に要るもの。基本機能は、手元で調べる・直す・確かめるまでの使い方。push・PR まで頼む場合と、`delegate`・`interrogate`・`reflect`・`playbook-eval` を使う場合は、[任意機能の準備](#任意機能の準備)も要る。

- OS：Linux・macOS・WSL2。Windows のネイティブ環境（PowerShell など）は対象外。入口がファイルに書く前に実行する `scripts/work.py` が、Unix にしか無い `fcntl` を使うので動かない。読むだけの使い方も、ネイティブ環境では確かめていない。Windows では、WSL2 の中に CLI を入れて、その中で起動する。
- CLI：Claude Code・Codex・Grok Build のどれか 1 つ。Claude Code では、入口は Fable で動く別の子（指揮者）として立つので、Fable を使えるアカウントを前提にする。Fable を使えないときの動きと回避は、この一覧の下の 2 つの段落。入口が別の子として動くことを確かめた版は、2.1.328 と 2.1.329。
- Python：3.9 以上を `python3` で起動できること。標準ライブラリだけを使うので、パッケージは要らない。macOS では、Command Line Tools に付く `python3`（3.9）で足りる。
- git：2.38 以上（`git --version`）。タスクの作業場所の一覧に 2.36、squash・rebase で統合した後の片付けの確認に 2.38 で入った機能を使う。macOS では、Command Line Tools に付く `git` がこれ以上なら足りる（macOS 15 の Command Line Tools は 2.39.5 と報告されているが、手元では確かめていない）。
- GitHub CLI（`gh`、ログイン済み）：Issue・PR を名指しして頼むときと、push・PR などの外部操作の前にアカウントと書き込み権限を確かめるときに使う。gh が無いと、名指しした Issue・PR の本文とコメントを読めず、GitHub への外部操作はこの確かめで止まる。Issue・PR を名指しせずに、リポジトリを読む・手元で直すだけなら要らない。

Fable を使えないアカウントそのものでの入口の動きは、確かめていない。入口のモデルを解決できない名前に書き換えた写しでは、入口の子は `There's an issue with the selected model (<名前>). It may not exist or you may not have access to it.` という文のエラーで止まった。非対話では、この文が結果として返るだけで、仕事は進まなかった。対話では、指揮者がこの文の API エラーで失敗と出た後、親のセッションが、bb-writ の進め方（作業場所の確保・検証・関門）を通さずに、自分で依頼を進めた（読むだけの依頼では、親が自分で読んで答えた。1 行を直す依頼では、親が、作業場所を取らずに元の checkout のファイルを直接書き換え、commit もしなかった。1 回ずつ。2026-11-21）。Fable を使えずに入口がこうなるときは、入口の代わりに、本体を名前で呼ぶ（`/bb-writ:conduct <依頼>`）。本体は、別の子を立てずに、呼んだセッションを指揮者として、そのセッションのモデルで進む。本体の説明（description）は、モデルが依頼を本体で直に受けないように、「依頼は入口 `bb-writ` で受け、ここでは直接受けない」と書いてある。利用者が名前で呼んだときは、この説明のままで進んだ（下の確かめは、どれもこの説明の版）。この回避で確かめたのは、次の 3 つの形（2026-11-20・21、2.1.329）。読むだけの依頼は、Sonnet のセッションで対話と非対話で 1 回ずつ、Opus と Haiku のセッションで非対話で 1 回ずつ進んだ。1 行を直す依頼は、Sonnet のセッションの非対話で 1 回、作業場所の確保・commit・手元の `main` への統合・片付けまで進んだ（このとき、指揮者は、書く子に書かせる本体の決めに従わず、自分で書いた）。同じ形の依頼は、Opus のセッションの対話でも 1 回、同じ所まで進んだ（このとき、指揮者は書く子に書かせた）。入口が失敗した後の同じセッションで本体を呼ぶ形は、Sonnet のセッションで、読むだけの依頼で、対話で 1 回進んだ。bb-writ が立てる子のうち Fable で動かすと決めてある役（Claude Code の `interrogate` の審査役、push・PR の前に必ず通す成果物の検証役、最も難しい依頼の書く子（`--hardest`））は、子が 費用が 0 のまま（作業を始める前に）、利用枠が尽きた（429）か使えないモデル（404）の API のエラーで終わると、launcher が同じ起動の中で Opus `high` と新しい session で 1 回だけ立て直し、記録に `model_fallback` を残す。ほかの API のエラー（過負荷・回線・認証）では立て直さない。途中まで進んでから尽きた子は立て直さず、`failed` か `quota` で返るので、指揮者が `--model opus --effort high` で立て直す。確かめたのは、代わりの段の表のキーを解決できないモデルの名前にした launcher の写しで、その名前を頼んだ `--interrogate` の子が 404 で止まった後に、Opus で立て直されて `ok` で返った 1 回（Fable を使えるログインで。2026-11-23、2.1.330）。Fable を使えない専用ログインそのものと、利用枠が尽きた専用ログインそのものでの立て直しは、確かめていない（利用枠が尽きた専用ログインで、Fable を頼んだ子が 費用が 0 のまま（作業を始める前に） 429 の API のエラーで終わることは、2026-11-22 に launcher で確かめた）。Fable を使えない専用ログインで push・PR まで頼む使い方は、確かめていない。専用ログインの設定の `availableModels` が Fable を外していても、launcher の子は `--restricted` で動くので Fable のまま動いた（2026-11-22）。組織の制限などで頼んだモデルと違うモデルで動いた子は、失敗にせず記録に `model_substituted` を残す（実物では確かめていない）。

Claude Code の設定の `availableModels`（使えるモデルの一覧）が Fable を外しているときは、入口は止まらず、指揮者は代わりのモデルで動く。確かめた場面（一覧が `["sonnet", "opus", "haiku"]`、Sonnet のセッション。2026-11-20、2.1.329）では、指揮者は、呼んだセッションのモデル（`claude-sonnet-5-5`）で動いた。対話では、画面に `bb-writ:bb-writ agent: Model "claude-fable-5-2" is restricted by your organization's settings. Using claude-sonnet-5-5 instead.` の 1 行が出た（1 回）。非対話（`claude -p --output-format json`）では、結果にその行は出なかった（2 回。うち 1 回は標準エラーも見て、空だった）。一覧が塞いだモデルを指定した、別の子として動くスキルを、失敗させずに代わりのモデルで動かすことと、代わりの選び方は、公式の文書の [Restrict model selection](https://code.claude.com/docs/en/model-config#restrict-model-selection) と [When an allowlist blocks a subagent model](https://code.claude.com/docs/en/sub-agents#when-an-allowlist-blocks-a-subagent-model) の節にある。claude.ai の管理画面でモデルを止める組織の制限（`availableModels` とは別の仕組み）での入口の動きは確かめていない。

bb-writ 専用のログイン・dotenvx・もう一方の CLI は、push・PR まで頼むときと任意機能で使う（[任意機能の準備](#任意機能の準備)）。専用ログインが無いと、bb-writ は、実装を任せる書く子（Claude Code では変更を書く依頼のすべて、Codex では最も難しい種類の依頼。Grok Build の入口は、書く子を立てない）と、確かめ役・審査役・返す前の判定の子を立てられない。手元の作業は、入口が自分で書き、子が無いことを返答に書いて進むが、push・PR は、返す前の判定の子の記録が無いので拒否される。

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

`--trust` は、プラグインを信頼して有効にすることの確認で、付けないと Grok Build は導入せずに止まる。bb-writ が有効にするのはスキルと、Claude Code 向けの hook 2 つで、MCP サーバーは含まない。Grok Build は 2 つとも読み込んで動かすが、Grok Build のセッションでは何も止めず、案内も足さない（下の「仕組みと含まれるスキル」）。

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

Claude Code では、入口は呼んだセッションとは別の子（指揮者）として、背景で動く。セッションのモデルは変わらず、指揮者は入口が指定するモデル（`fable`）で動く（Fable を使えないとき・設定が Fable を外しているときは、[前提](#前提)）。指揮者に届くのは、呼んだときの依頼文だけで、それまでの会話は見えない（探索のサマリなどは、ファイルに保存して道筋を渡す）。指揮者が質問や報告で止まると、親のセッションがそれを伝え、返事は指揮者に直接打つよう添える（添えないこともある）。返事や追加の依頼は、指揮者に直接打つ。動いている間は画面下の一覧で ↓ を 2 回押して Enter、終わった後は `/tasks` から指揮者を開いて、入力欄に打つ。親の入力欄に打つと、親は自分では進めず、指揮者にも渡さずに、直接の返事のしかたを伝える（案内の hook。下の「仕組みと含まれるスキル」と、`skills/delegate/SKILL.md` の「指揮者への返事（案内の hook）」）。親に取り次がせないのは、指揮者が、親を通した返事を、本人の選択と確かめられずに断ることがあるため。案内に従うかは親のモデルしだいで、従わずに親が自分でファイルを書き換えると、その分には作業場所の確保も検証も掛からない。非対話の実行では、指揮者が終わるまで待って結果を返し、同じ指揮者への追加の依頼はできない。Codex と Grok Build では、入口は今までどおり、呼んだセッションの中で動く。Codex では、成果物も今までどおり、最も難しい種類の依頼のほかは、入口が自分で書く。Grok Build では、最も難しい種類の依頼も含めて、どの依頼も入口が自分で書く（書く子を立てない）。

Claude Code は、普通の依頼文だけでは入口を使わない。入口の名前を付けずに使わせるには、作業するリポジトリの `CLAUDE.md` に「技術的な仕事は、まず bb-writ:bb-writ スキルを使って始める」と書く。Codex と Grok Build は、入口の名前を付けない普通の依頼でも入口を最初に読んだ（試験での観測）。どの CLI でも hook の設定は要らない。

## 仕組みと含まれるスキル

入口のスキル `bb-writ` が呼ばれると、進め方の本体 `conduct` を読み、依頼を言い直し、最初に手引きを呼び、リポジトリを読み、必要な原則だけを読んで進め、「何を実行し、どの結果を見て、依頼を満たすと判断したか」を返す。Claude Code では、変更を書く依頼では、入口（指揮者）は成果物を自分で書かず、別プロセスの書く子に書かせて、返った実物を確かめる。Codex では、最も難しい種類の依頼だけを書く子に任せ、ほかは入口が自分で書く。Grok Build では、最も難しい種類の依頼も含めて、どの依頼も入口が自分で書く（書く子を立てない）。push・PR の前には、新しい文脈の検証役に成果物を検証させる。

- 入口の本体：`conduct`（進め方の全文。入口が呼ぶ。自分で名前を打って呼ぶのは、Claude Code で、Fable を使えずに入口が進まないときだけ。[前提](#前提)）。

- 手引き：`playbook-investigation`（読んで答える）・`playbook-bug`（不具合を直す）・`playbook-feature`（振る舞いを足す・変える）・`playbook-docs-skill`（文書・スキルを直す）・`playbook-verify`（確かめて返す。検証の条件の全文）・`playbook-deliver`（担当・記録・push や PR などの外部操作）・`playbook-parallel`（タスクごとの作業場所の確保・再開・統合・片付け）・`playbook-session-pickup`（続きを引き受ける）・`playbook-pause-safely`（安全に止める）・`playbook-eval`（スキルや設定の変種を同じ依頼で走らせ、目隠しで比べる）。
- 委任と審査：`delegate`（別系統の CLI の起動）、`interrogate`（2 系統の審査役）。
- 検証：`create-verification-skill`（対象のリポジトリに検証スキルを作って 1 回通す）、`create-formal-verification-skill`（順序・途中の死・再開の約束を全部の順序で確かめる検査を作る。pstack の同名のスキルの考え方から書き直した）、`create-property-test-skill`（入力の形・文字コード・大きさの約束を、揺れの一覧を組み合わせた多数の入力で確かめる性質の試験を作る）。
- 学び：`reflect`（振り返って行き先へ送る）、`correct`（繰り返す誤りを起こせなくする）。
- 探索：`unknowns`（実装前に、盲点の洗い出しや案の比較をする）。
- 原則：1 本ずつ別のスキル `principle-*`（18 本）。本体の「原則の索引」から、仕事の局面に当たるものだけを呼ぶ。
- hook（Claude Code）：2 つある。1 つ目は、入口か本体を呼んだセッション（入口が立てた指揮者とその子孫を含み、子の会話記録で見分ける。指揮者が launcher で立てる子は含まない）で、関門（`work.py op run`）を通さない普段の書き方の `git push`・`gh pr create`・`gh pr merge`・`gh release create` を拒否する（`hooks/hooks.json`）。入口を呼ばないセッションと、`sh -c` などの別の語に包んだ書き方は止めない（範囲は `scripts/push_guard.py` の docstring）。Codex と Grok Build では止めず、同じことを入口の文が求める。2 つ目は、入口を呼んだセッションで、hook が発火するたびに（利用者の入力のほか、指揮者の終わりの知らせ・子からの伝言でも発火する）、指揮者がいるなら、その質問・報告への返事と続きの依頼は、親が自分で進めず、指揮者にも渡さずに、利用者が指揮者に直接打つよう、親のセッションに案内する（`scripts/conductor_hint.py`）。案内だけで、入力は止めない。従うかは親のモデルしだい。Codex と Grok Build では案内は足されない（入口が呼んだセッションの中で動くので、要らない）。

## 任意機能の準備

push・PR まで頼む場合と、`delegate`・`interrogate`・`reflect`・`playbook-eval` を使う場合は、[前提](#前提)に加えて、ここの準備が要る。bb-writ は、実装を任せる書く子（Claude Code。Codex では最も難しい種類の依頼。Grok Build の入口は、書く子を立てない）と、審査役・確かめ役・返す前の判定の子を、CLI の別プロセスとして起動する。その子は、普段のログインでなく、bb-writ だけが使うログイン（専用ログイン）で動く。分けるのは、子の使った量を普段の作業と区別し、普段の設定（個人の指示・hook・プラグイン）を子に混ぜないため。

何を用意するか：

- push・PR だけなら、使っている CLI の専用ログインがあれば足りる。返す前の判定の子を、使っている CLI と同じ CLI で起動するため。Claude Code なら、下の dotenvx と、Linux・WSL2 では `bwrap`・`socat` も要る。
- 手元の作業だけでも、実装を書く子に任せるには、使っている CLI の専用ログインが要る（Claude Code なら、上の項と同じく dotenvx と、Linux・WSL2 では `bwrap`・`socat` も）。無ければ、入口が自分で書く。
- `interrogate` の別系統の審査役と、`playbook-eval`・`reflect` の別系統の読み手には、もう 1 つの CLI（Claude Code・Codex・Grok Build のどれか）と、その専用ログインが要る。

専用ログインが無いまま push・PR を頼むと、bb-writ は返す前の判定の子を起動できず、push・PR は拒否される。拒否の文と、子を起動できなかったときの返答に、この節が出てくる。用意したら、同じセッションでもう一度頼めばよい（bb-writ は子を起動するたびに専用ログインの置き場を見る）。Claude Code では、動いている指揮者に直接打つか、入口を呼び直す。

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

`--no-native` を省かない。dotenvx 2.25 以降は、OS の secret store（macOS のキーチェーンなど）が使えると、鍵を `.env.keys` でなくそこに置くことがある。そこに置かれると、bb-writ は鍵を見つけられず、子を起動しない。`--no-native` は 2.25 より前の版でも通る（1.75.1・2.24.1 で確かめた）。

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

トークンは、`claude setup-token` でログインしたアカウントのもので、ふだんのログインを切り替えても変わらない。Claude のアカウントを切り替えたら、専用ログインのトークンも作り直す。作り直すには、上の `claude setup-token`・`IFS= read -rs T`・`dotenvx set …` の 3 つを、もう一度順に実行する。`.env` の値は置き換わる（dotenvx 2.36.0 で確かめた）。その後、上の確かめの問い合わせをもう一度実行する。

専用ログインだけが利用枠の上限で断られ、同じ時刻のふだんのログインには枠が残っていたことがある。トークンを作り直すと通った（2026-11-22）。前のトークンが別のアカウントのものだったかは、確かめていない。

bb-writ は、Claude Code の子を起動する前に、専用ログインのトークンの組織と、bb-writ を呼んだ環境の Claude Code のログインの組織を比べる。違っても、確かめられなくても、起動は止めない。違えば、子の記録の `claude_login` に案内を出す。問い合わせは終了コード 0 で終わったのに組織を読めないときは、確かめが働いていないことを、同じく記録に案内として出す。入口（指揮者）がその案内を返答で伝えるよう、`delegate` のスキルに書いてある。実物の入口で、別の組織の形の覚えを置いて小さな直しを頼むと、指揮者は返答に案内を 1 回書いた（1 回中 1 回）。組織が同じときは、返答に専用ログインの話は出なかった（1 回中 1 回）。どちらも非対話の `claude -p` で、2026-11-22、Claude Code 2.1.330。伝えるかは指揮者のモデルしだい。確かめた結果は、子の記録（`~/.local/state/bb-writ/delegations.jsonl` の、Claude Code の子を起動した行の `claude_login`）に残る。案内が返答に無いときは、ここで見られる。`org` が `same` なら、トークンとログインは同じ組織。

トークンの組織は、専用ログインの `.env` の中身が変わった後の最初の起動で 1 回だけ（初めての起動と、`claude-login.json` が無い・読めないときも同じ）、専用ログインで Haiku に短い問い合わせをして、問い合わせに `ANTHROPIC_LOG=debug` を付けると出る SDK の debug の出力から、ヘッダ `anthropic-organization-id` の値を探して読む（出力の全体から探す）。同時に起こした子は、それぞれ問い合わせる。Claude Code の文書にこの変数の記述は無く、根拠は実物（Claude Code 2.1.330、WSL2）と SDK の文書。読めなかったときは、次の起動でまた問い合わせる。覚えるのは、`.env` の中身の SHA-256 と、組織の ID の SHA-256 だけで、置き場は `~/.local/state/bb-writ/claude-login.json`。ログインの側は、`claude auth status --json` の `orgId` を使う。

この確かめが守らないこと：

- 比べるのは組織で、同じ組織の中の別の人のトークンは見分けない。
- bb-writ を呼んだ環境の `claude auth status --json` の `orgId` が無いときと、uuid の形でないときは比べない。Claude Code にログインしていないときと、環境に `ANTHROPIC_API_KEY` か `CLAUDE_CODE_OAUTH_TOKEN` が在るとき（`authMethod` が `api_key`・`oauth_token` になる）は、`orgId` が無い（Claude Code 2.1.330 で確かめた。ログインしていないときは空の設定の置き場で、環境の 2 つは偽の値を置いて。2026-11-22）。
- 問い合わせの出力から組織を読めないときは、比べない。問い合わせが終了コード 0 で終わっていれば、確かめが働いていないことを案内し、0 でなければ、記録に理由だけを書く。読み方は Claude Code 2.1.330 で確かめた。SDK の debug の出力の書式は版で変わりうると、公式の SDK の文書にある。
- 読めない状態が続く間は、起動のたびに問い合わせる（結果を覚えない）。
- 利用枠で断られた問い合わせ（429）の応答にヘッダが付くかは、確かめていない（付けば読む）。
- ログインの `orgId` とヘッダの値が同じだったのを見たのは、1 つのアカウントだけ。
- macOS の実物では確かめていない（確かめたのは WSL2）。
- 別のアカウントのトークンで実際に案内が出ることは、偽の CLI の試験で確かめただけで、実物では確かめていない。
- わざと別のアカウントのトークンを使っているときも、案内は出続ける（止める設定は無い）。

### Codex の専用ログイン

```sh
mkdir -p -m 700 ~/.config/bb-writ ~/.config/bb-writ/codex-home
CODEX_HOME=~/.config/bb-writ/codex-home codex login
```

置き場を先に作る。`CODEX_HOME` の指す場所が無いと、Codex は「path does not exist」で止まる。

確かめるには `CODEX_HOME=~/.config/bb-writ/codex-home codex login status` を実行し、`Logged in` で始まる行が出ればよい。

この専用ログインに bb-writ を入れるかは任意。入れると、子のスキルの一覧に bb-writ が出る。入れるときは、[インストール](#codex)の 2 つのコマンドの先頭に `CODEX_HOME=~/.config/bb-writ/codex-home` を付けて実行する。入れた写しは、bb-writ を更新しても、自分では新しくならない。写しが起動する側の bb-writ と中身で違うと、Codex の子は起動しない。手元のディレクトリを marketplace として登録していて、その置き場が、起動する側の bb-writ の置き場と同じなら、bb-writ が自分で入れ直してから起動する。そうでないとき（GitHub から入れたなど）は、起動せず、入れ直すコマンドを示す（`codex plugin add`。GitHub から入れた marketplace なら、その前に `codex plugin marketplace upgrade`。どちらも `CODEX_HOME` を付ける。[Codex の更新](#codex-1)にある `remove` は、挟まなくてよい。Codex CLI 0.171.0 で確かめた）。

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
