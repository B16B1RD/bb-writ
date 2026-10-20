"""配布の形: マニフェストの名前と版がそろう。スキルの名前は置き場と同じで、使う場面を description に書く。"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TestManifests(unittest.TestCase):
    def test_names_and_versions_agree(self):
        claude = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        root = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))  # Codex と Grok Build が読む
        market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
        [entry] = market["plugins"]
        self.assertEqual(entry["source"], ".")
        self.assertEqual({claude["name"], root["name"], entry["name"]}, {"bb-writ"})
        self.assertEqual({claude["version"], root["version"], entry["version"]}, {claude["version"]})  # 出すときは 3 か所を一緒に上げる

    def test_changelog_starts_with_the_current_version(self):
        """版を上げた commit で CHANGELOG.md にも節を足す（Release の本文はその節から作る）。"""
        claude = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        newest = next(line for line in text.splitlines() if line.startswith("## "))
        self.assertRegex(newest, rf"^## {re.escape(claude['version'])} - \d{{4}}-\d{{2}}-\d{{2}}$")


class TestSkills(unittest.TestCase):
    """スキルは目的ごとに分ける（入口・手引き・原則・委任・審査・学び）。"""

    def test_each_skill_is_named_after_its_folder(self):
        dirs = sorted(p.parent.name for p in (ROOT / "skills").glob("*/SKILL.md"))
        self.assertGreater(len(dirs), 1)
        for name in dirs:
            text = (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            head = text.split("---", 2)[1]
            self.assertIn(f"\nname: {name}\n", head)
            self.assertRegex(head, r"\ndescription: .*(使う|適用する)")  # 使う場面（原則は「〜に適用する」）
            self.assertNotIn("disable-model-invocation", head)  # Claude Code の Skill ツールから呼べなくなる（2026-10-17 の実測）

    def test_skills_do_not_point_at_other_plugin_files(self):
        """Claude Code はプラグイン内の別ファイルの Read に承認を求める。スキルは本文だけで届く（scripts の実行は除く）。"""
        for p in (ROOT / "skills").glob("*/SKILL.md"):
            if p.parent.name == "unknowns":
                continue  # 自分の置き場の references を読む既存のスキル
            text = p.read_text(encoding="utf-8")
            self.assertNotIn("references/", text, p)
            self.assertNotIn("playbooks/", text, p)

    def test_entry_keeps_what_to_ask_and_review_rules_within_the_compaction_head(self):
        """Claude Code は要約の後、呼んだスキルを先頭 5,000 トークンまで戻す（合計の枠を超えた古いものは落ちる）。
        入口の聞く範囲・呼び直しの文・返答の形・検証の条件 (1)〜(6) を、戻る先頭に収める。"""
        text = (ROOT / "skills" / "bb-writ" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("\n---", text[3:], "frontmatter の閉じが見つからない")
        body = text[text.index("\n---", 3) + 4:]
        self.assertEqual(body.count("信じてよい条件"), 1, "検証の条件の段落が 1 つでない")
        start = body.index("信じてよい条件")
        self.assertIn("\n\n", body[start:], "検証の条件の段落の終わりが見つからない")
        end = body.index("\n\n", start)
        # 5,650 字は opus の input_tokens の差で約 4,930 トークン（2026-10-20 の実測）。
        # Claude Code が本文の前に付ける前置きの数え方と、切り詰めに使う数え方は確かめていない。
        self.assertLessEqual(end, 5650)
        anchors = (
            "このスキルを呼び直して全文を読み直す",
            "聞くのは製品上の選択",
            "自分で立てた審査の指摘をどこまで直すかも聞かない",
            "## 返答の形",
            "4. 残る範囲",
            "(1) 何が観測",
            "(5) 新しい自動検査",
            "(6) コード",
            "どこまで直すかを利用者に選ばせない",
        )
        for anchor in anchors:
            self.assertEqual(body.count(anchor), 1, anchor)
            self.assertLess(body.index(anchor), end, anchor)

if __name__ == "__main__":
    unittest.main()
