# 変更履歴

bb-writ を使う人に向けて、版ごとに変わったことを書く。新しい版が上。版はプラグインのマニフェストの `version` と同じで、各版の Release は同じ文面にする。更新のしかたは README の「更新」の節にある。

commit ごとの細かい変更は、各版の末尾の「commit の一覧」（前の版のタグとの比較）で見られる。

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
