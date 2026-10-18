# bb-writ

Claude Code・Codex 向けのスキルのプラグイン。「直して」「作って」「調べて」といった普通の依頼を受け、リポジトリを読んで実装・修正・調査までやり遂げ、実物で動作を確かめた結果を根拠とともに返す。Issue の整形も、対象のリポジトリの登録も要らない。

## 入れる

下の入れ方は 0.7.0 で実測した（2026-10-18）。

- **Claude Code:** `claude plugin marketplace add B16B1RD/bb-writ` → `claude plugin install bb-writ@bb-writ`。何も入っていない設定からこの手順で入り、スキルが呼べることを確かめた。手元の作業ツリーを試すときは `claude plugin marketplace add <このリポジトリ>`（作業ツリーを直接読むので、編集は次のセッションか `/reload-plugins` で効く）。
- **Codex:** `codex plugin marketplace add B16B1RD/bb-writ` → `codex plugin add bb-writ@bb-writ`。何も入っていない設定から入ることを確かめた（0.3.0）。Codex は版ごとの写しを置くので、版を上げたら `codex plugin remove bb-writ@bb-writ` → `add`。
- **入口の案内（Claude Code）:** 普通の依頼文だけでは入口が使われないので、CLAUDE.md に 1 行「技術的な仕事は、まず bb-writ:bb-writ スキルを使って始める」を置くか、`/bb-writ:bb-writ <依頼>` と明示する。Codex は普通の依頼文で入口を使う。hook は使わない。
- **Grok Build:** root の `plugin.json` を読む形にしてあるが、未確認。
- **別系統の CLI を使う機能:** `delegate` や `interrogate` の別系統の審査役は、`scripts/launch.py` でもう一方の CLI を起動する。両方の CLI を入れておき、起動される側には普段のログインとは別の、bb-writ 専用のログインを用意する（準備は `skills/delegate/SKILL.md`）。この機能を使わなければ、専用ログインは要らない。

## 使う

技術的な依頼をそのまま頼む。入口のスキル `bb-writ` が、依頼を言い直し、最初に手引きを呼び、リポジトリを読み、必要な原則だけを読んで進め、「何を実行し、どの結果を見て、依頼を満たすと判断したか」を返す。

- 手引き: `playbook-investigation`・`playbook-bug`・`playbook-feature`・`playbook-docs-skill`・`playbook-session-pickup`（続きを引き受ける）・`playbook-pause-safely`（安全に止める）・`playbook-eval`（目隠しで比べる）。
- 委任と審査: `delegate`（別系統の CLI の起動。`scripts/launch.py`）、`interrogate`（2 系統の審査役）。
- 検証: `create-verification-skill`（対象のリポジトリに検証スキルを作って 1 回通す）。
- 学び: `reflect`（振り返って行き先へ送る）、`correct`（繰り返す誤りを起こせなくする）。
- 原則は 1 本ずつ別のスキル `principle-*`（18 本）。入口の「原則の索引」から、仕事の局面に当たるものだけを呼ぶ。

## 中身

- `skills/bb-writ/`: 入口（言い直し、手引きの選び方、原則の索引、検証の形、片付け、返答の形）。
- `skills/playbook-*/`: 手引き 7 本。`skills/delegate/`・`skills/interrogate/`・`skills/create-verification-skill/`・`skills/reflect/`・`skills/correct/`。
- `skills/principle-*/`: 原則 18 本。`skills/unknowns/`: 実装前の探索セッション。
- `scripts/launch.py`（別系統の CLI の起動と報告の検査）、`scripts/work.py`（担当の貸し出しと外部操作の記録）。標準ライブラリのみ。
- すべて SKILL.md の本文で届く形にしてある。Claude Code 2.1.302 は、プラグイン内の別ファイルの Read に作業ディレクトリ外として承認を求める（2026-10-17 に実測）。
- 試験: `python3 -B -m unittest discover -s tests -t .`

## 守ること

- トークン・認証情報を画面・ログ・会話に出さない。ワーカーは bb-writ 専用のログインを使う（`skills/delegate/SKILL.md`）。
- 対象を増やすたびに bb-writ 自身を直さない。対象に固有のことは、対象のリポジトリの中で解く。

## 生い立ち

bb-writ は [cc-rite-workflow](https://github.com/B16B1RD/cc-rite-workflow)（rite）の後継。rite は Issue を起点に、仕様・実装・レビュー・PR を固定の工程で回していた。作者は rite を使い続けるうちに、生成 AI で作る時代に旧来の工程をなぞることに疑問を持ち、開発の進め方そのものを見直した。その際、poteto（Lauren Tan）の [pstack](https://github.com/cursor/plugins/tree/main/pstack) の考え方に共感し、rite で得た知見と pstack を参考に、CLI ハーネスで動くスキルのプラグインとして作り直した。

- **rite から:** 子への委任の範囲と実効設定の記録、専用ログイン、根拠に基づく報告、中断・訂正・外部操作（push・PR・マージ）の記録と照合。Issue の書式・固定の工程・対象の登録は捨てた。
- **pstack から:** 原則 18 本のうち 12 本（CLI ハーネス向けに日本語で書き直した。残る 6 本は作者の原則）、入口が依頼を読んで手引きを選ぶ形、`correct`・`create-verification-skill`・`interrogate`・`reflect` の考え方。出典と著作権表示は [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。
- **pstack と違うところ:** Grok Bot と Cursor のクラウドを前提にしない。別系統のモデルによる判断、作業の継続、権限を、Claude Code・Codex の正式な実行・再開・プラグインの機能と薄い補助コードで成り立たせる。

## ライセンス

MIT（[`LICENSE`](LICENSE)）。pstack をもとにした部分の著作権表示とライセンス文は [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。
