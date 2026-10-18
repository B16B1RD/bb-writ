"""配布の形: マニフェストの名前と版がそろう。スキルの名前は置き場と同じで、使う場面を description に書く。"""

import json
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


if __name__ == "__main__":
    unittest.main()
