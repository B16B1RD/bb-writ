# 保存と片付け

## 下位の機能
保存先の祖先確認、ブランチの一致、未完了操作の拒否、証拠の保持。

## 利用者の経路
変更を別のブランチへ統合してから専用 worktree を片付ける。

## 動かし方
`python3 -B -m unittest tests.test_parallel -v`。実 CLI 試験後は remaining-tasks.json が空、試験用 repo の main に成果物が残ること。

## 注意
squash・rebase で祖先関係が失われた保存先では自動削除しない。作業場所を保持する。
