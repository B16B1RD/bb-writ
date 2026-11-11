"""配布の形: マニフェストの名前と版がそろう。スキルの名前は置き場と同じで、使う場面を description に書く。"""

import json
import re
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent)); import process_limit  # noqa: E402,F401  試験に上限を掛ける（直に走らせても）

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


def skill_body(case, text, name):
    """frontmatter（1 行目の --- から次の --- の行まで）を除いた本文。"""
    case.assertTrue(text.startswith("---\n"), f"{name}: frontmatter で始まらない")
    case.assertIn("\n---\n", text[3:], f"{name}: frontmatter の閉じが見つからない")
    return text[text.index("\n---\n", 3) + 5:]


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
        入口の聞く範囲・呼び直しの文・返答の形・検証の条件 (1)〜(6) の文が、それぞれ 1 つずつ戻る先頭にあることを確かめる。
        確かめるのは目印の語が 1 回だけあり、条件の段落の終わり（5,600 字以内）より前にあることだけで、
        目印同士の順と文の意味は確かめない（意味は新しい文脈の読者役で確かめる）。目印の文を書き換えたら、ここの目印も直す。"""
        text = (ROOT / "skills" / "bb-writ" / "SKILL.md").read_text(encoding="utf-8")
        body = skill_body(self, text, "bb-writ")
        self.assertEqual(body.count("信じてよい条件"), 1, "検証の条件の段落が 1 つでない")
        start = body.index("信じてよい条件")
        self.assertIn("\n\n", body[start:], "検証の条件の段落の終わりが見つからない")
        end = body.index("\n\n", start)  # 条件 (1)〜(6) は 1 つの段落（分けたら、ここも直す）
        # 境目の 5,589 字は、前置きの 1 行を含めて opus の input_tokens の差で 4,946 トークン（2026-10-20 の実測）。
        # 上限まで約 50 トークンしか余裕が無い。字あたりのトークン数は中身（英数字と日本語の割合）で変わり、
        # 前置きの長さは置き場で変わり、Claude Code の切り詰めの数え方も確かめていないので、字数は近似にとどまる。
        self.assertLessEqual(end, 5600, f"検証の条件の段落の終わりが {end} 字目")
        anchors = (
            "このスキルを呼び直して全文を読み直す",
            "聞くのは製品上の選択",
            "自分で立てた審査の指摘をどこまで直すかも聞かない",
            "## 返答の形",
            "4. 残る範囲",
            "(1) 何が観測",
            "(2) 新しい自動検査は",
            "(3) 意味の判断",
            "(4) 未実行",
            "(5) 新しい自動検査",
            "(6) コード",
            "どこまで直すかを利用者に選ばせない",
        )
        for anchor in anchors:
            self.assertEqual(body.count(anchor), 1, anchor)
            self.assertLess(body.index(anchor), end, anchor)

    def test_skills_invoked_after_the_entry_say_to_reinvoke_it_after_compaction(self):
        """要約の後に入口が合計の枠から落ちたときのため、手引き 8 本・delegate・interrogate の本文の先頭 4,000 字以内に
        呼び直しの文を置く。単独でも呼ばれる delegate・interrogate は、入口から始めた仕事に限る。
        playbook-pause-safely・playbook-eval・reflect・correct・unknowns・原則・検証スキルには置いていない。
        目印の文を書き換えたら、ここの目印も直す。"""
        plain = "会話が要約された後は、入口 `bb-writ` を呼び直して全文を読み直す"
        scoped = "入口 `bb-writ` から始めた仕事で会話が要約された後は、入口を呼び直して全文を読み直す"
        names = {
            "playbook-feature": plain, "playbook-bug": plain, "playbook-docs-skill": plain,
            "playbook-investigation": plain, "playbook-parallel": plain, "playbook-session-pickup": plain,
            "playbook-verify": plain, "playbook-deliver": plain,
            "delegate": scoped, "interrogate": scoped,
        }
        for name, sentence in names.items():
            body = skill_body(self, (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8"), name)  # description は本文として戻らない
            at = body.find(sentence)
            self.assertNotEqual(at, -1, name)
            self.assertLess(at, 4000, name)  # 先頭 5,000 トークンの内側


class TestWorkflows(unittest.TestCase):
    """CI の定義（.github/workflows）。"""

    def test_ci_jobs_have_room_for_the_slow_macos_runners(self):
        """macOS の job は、成功した実行で 8〜12.9 分かかり（2026-11。0.24.0 で試験が増えた後の最長が 12.9 分）、日によって 1〜2 割揺れる。
        下限は、その最長の 3 倍（38.7 分）を切り上げた 39 分。上限 10 分では全部通る試験が 3 回続けて打ち切られた（2026-11）。
        見るのは、ファイルの中の「timeout-minutes:」の後に、空白と、引用符 1 つまでを挟んで、数字が続く形だけ（数字は 10 進の数として読む）。
        守らない範囲: ほかの書き方の上限（式 ${{ }}、符号つき、引用符の中の空白、鍵を引用符で囲む、など）と、上限を書かない job
        （既定は 360 分）は見ない。ファイルの文字を見るだけなので、step ごとの上限や、コメントの中の timeout-minutes でも落ちる
        （job の上限でなくても）。"""
        files = sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
        self.assertTrue(files)
        for p in files:
            for m in re.finditer(r"timeout-minutes:\s*[\"']?(\d+)", p.read_text(encoding="utf-8")):  # 引用符で書いた数も見る
                self.assertGreaterEqual(int(m.group(1)), 39, f"{p.name} の timeout-minutes: {m.group(1)}。最も遅い OS（macOS）の成功した実行の最長を "
                                        "gh run list・gh run view で見て、その 3 倍以上にする。試験を削って合わせない")


if __name__ == "__main__":
    unittest.main()


class TestReadmeSections(unittest.TestCase):
    def test_sections_the_scripts_point_users_to_exist_in_the_readme(self):
        # launcher と work.py は、専用ログインが無いときに README の節の名前を示す。見出しを変えて文だけが古くならないように
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        named = []
        for script in ("launch.py", "work.py"):
            src = (ROOT / "scripts" / script).read_text(encoding="utf-8")
            for top, sub in re.findall(r"README の「([^」]+)」(?:の「([^」]+)」)?", src):
                named.append((script, top, sub))
        self.assertTrue(named, "scripts が README の節を指す文が見つからない（文の形が変わった）")
        for script, top, sub in named:
            self.assertTrue(f"\n## {top}\n" in readme, f"{script} が指す README の節「{top}」が無い")
            if sub:
                body = readme.split(f"\n## {top}\n", 1)[1].split("\n## ", 1)[0]
                self.assertTrue(f"\n### {sub}\n" in body, f"{script} が指す README の「{top}」の「{sub}」が無い")
