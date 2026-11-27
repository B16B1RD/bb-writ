"""配布の形: マニフェストの名前とバージョンがそろう。スキルの名前は置き場と同じで、使う場面を description に書く。"""

import json
import re
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent)); import process_limit  # noqa: E402,F401  テストに上限を掛ける（直に走らせても）

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
        """バージョンを上げた commit で CHANGELOG.md にも節を足す（Release の本文はその節から作る）。"""
        claude = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        newest = next(line for line in text.splitlines() if line.startswith("## "))
        self.assertRegex(newest, rf"^## {re.escape(claude['version'])} - \d{{4}}-\d{{2}}-\d{{2}}$")


def skill_body(case, text, name):
    """frontmatter（1 行目の --- から次の --- の行まで）を除いた本文。"""
    case.assertTrue(text.startswith("---\n"), f"{name}: frontmatter で始まらない")
    case.assertIn("\n---\n", text[3:], f"{name}: frontmatter の閉じが見つからない")
    return text[text.index("\n---\n", 3) + 5:]


def frontmatter(path):
    """frontmatter の `鍵: 値` の行を辞書にする。値の前後の空白と引用符は外す（YAML の同じ意味の書き方で落ちないため）。"""
    head = path.read_text(encoding="utf-8").split("---", 2)[1]
    pairs = (line.split(":", 1) for line in head.splitlines() if ":" in line and not line.startswith((" ", "\t")))
    return {k.strip(): v.strip().strip("\"'") for k, v in pairs}


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
            if p.parent.name == "explore":
                continue  # 自分の置き場の references を読む既存のスキル（探索の本体。入口 unknowns から移した）
            text = p.read_text(encoding="utf-8")
            self.assertNotIn("references/", text, p)
            self.assertNotIn("playbooks/", text, p)

    def test_the_entry_forks_and_hands_over_to_the_body(self):
        """入口は、Claude Code では fork した子（指揮者）として、frontmatter の model と effort で動く（effort を書かないと、
        利用者の設定の effort で動いた。#96。利用者の環境変数 CLAUDE_CODE_EFFORT_LEVEL は frontmatter より強いと公式の文書にある）。frontmatter の model だけだと
        そのターンしか効かず、次の入力でセッションのモデルに戻った（2026-11-19 の実測）。進め方は本体 `conduct` にあり、入口はそれを呼ぶ。
        本体は fork しない（本体が fork すると、指揮者の中にもう 1 人の指揮者が立つ）。
        確かめるのは、入口の frontmatter の 3 行（effort は 1 回だけ）、入口の本文の最初の段落の「最初に、」で始まる文が本体の名前を挙げること、
        本体の frontmatter にその 3 つのキーが無いことだけ（fork した子が実際にそのモデルと effort で動くかは、実物の CLI で確かめる。
        本体の文が残っているかも見ない）。"""
        # 探索の入口 unknowns と本体 explore も同じ形（#58。frontmatter の model だけでは、2 ターン目からセッションのモデルで動いた）
        for entry, body in (("bb-writ", "conduct"), ("unknowns", "explore")):
            with self.subTest(entry=entry):
                text = (ROOT / "skills" / entry / "SKILL.md").read_text(encoding="utf-8")
                head = text.split("---", 2)[1]
                self.assertIn("\ncontext: fork\n", head)
                self.assertIn("\nmodel: fable\n", head)
                # effort を書かないと、指揮者は利用者の設定の effort で動いた（#96）。値は、Fable の指揮者で比べて決めた medium。
                # 同じ鍵の 2 回書きは後ろが勝ちうるので、1 つだけであることも見る
                self.assertEqual(re.findall(r"^effort:\s*(\S+)\s*$", head, re.M), ["medium"])
                paragraphs = [p for p in skill_body(self, text, entry).split("\n\n") if p.strip() and not p.lstrip().startswith("#")]
                first = [s for s in paragraphs[0].split("。") if s.startswith("最初に、")]  # 最初に読む先を言う文（書き換えたら、ここの目印も直す）
                self.assertEqual(len(first), 1, paragraphs[0][:80])
                self.assertIn(f"`bb-writ:{body}`", first[0])
                body_head = (ROOT / "skills" / body / "SKILL.md").read_text(encoding="utf-8").split("---", 2)[1]
                self.assertNotIn("\ncontext:", body_head)
                self.assertNotIn("\nmodel:", body_head)
                self.assertNotIn("\neffort:", body_head)  # 指揮者が本体を読み込んだときに effort が置き換わりうる

    def test_the_entry_conductor_definition_resolves_and_bounds_the_context(self):
        """入口の指揮者は、入口の frontmatter の agent が指すエージェント定義で動き、その autoCompactWindow で要約される（#93）。
        Claude Code は、agent の名前に当たる定義が無いと、黙って組み込みの general-purpose で動き、要約の窓も効かない
        （接頭辞の無い名前で、文脈が要約されずに伸びた。2.1.332 の試作）。定義の frontmatter の鍵が増えると、指揮者の道具・
        モデルを定義が決めてしまう（tools: Read で conduct を呼べなくなる）。確かめるのは、名前が `bb-writ:<定義の name>` で、
        その name の定義が agents/ に 1 つ在り、鍵がちょうど name・description・autoCompactWindow で、窓が Claude Code の下限
        100000 以上、400000 以下（#86 の指揮者の文脈 543k より十分小さい）ことだけ（要約が実際に起きるかは、実物の CLI で確かめる）。"""
        plugin, _, name = frontmatter(ROOT / "skills" / "bb-writ" / "SKILL.md")["agent"].partition(":")
        self.assertEqual(plugin, "bb-writ")
        [definition] = [p for p in sorted((ROOT / "agents").glob("*.md")) if frontmatter(p).get("name") == name]
        keys = frontmatter(definition)
        self.assertEqual(set(keys), {"name", "description", "autoCompactWindow"})
        head = definition.read_text(encoding="utf-8").split("---", 2)[1]
        self.assertEqual(len([line for line in head.splitlines() if ":" in line and not line.startswith((" ", "\t"))]), 3, head)  # 同じ鍵を 2 回書かない
        # 200k のような書き方は 200 と読まれる（公式の文書）。300_000 や引用符で包んだ値を CLI が数として受けるかは確かめていないので、
        # 引用符を外す前の行で、数字だけを通す
        [raw] = re.findall(r"^autoCompactWindow:\s*(\S+)\s*$", head, re.M)
        self.assertRegex(raw, r"^\d+$")
        window = int(raw)
        self.assertGreaterEqual(window, 100000)
        self.assertLessEqual(window, 400000)

    def test_the_conductor_definition_repeats_the_entry_rules_that_must_survive_compaction(self):
        """要約の後、fork の最初のメッセージ（入口の本文）は戻らず、呼び直すのは本体の conduct だけ。定義の本文は指揮者の
        システムプロンプトで要約されないので、入口の「Claude Code では次のとおりにする」の決まりを、同じ文で持つ（#93 の審査）。
        確かめるのは、入口のその節の箇条が定義の本文にそのまま在ることと、定義の本文が conduct の呼び直し・直接立てられたときに止まる
        条件・要約に依頼と伝言の原文を残すことを言う文を持つことだけ（従うかは実物の CLI で確かめる）。"""
        entry = (ROOT / "skills" / "bb-writ" / "SKILL.md").read_text(encoding="utf-8")
        lead = "そのため、Claude Code では次のとおりにする。\n\n"
        self.assertEqual(entry.count(lead), 1)
        rules = entry[entry.index(lead) + len(lead):].strip()
        self.assertTrue(rules.startswith("- "), rules[:40])
        # 要約で依頼がまるごと落ちたときに頼る決まり（AGENTS.md の入口の項）。写しをそろえるだけだと、両方から消しても通る
        self.assertIn("依頼が届いていなければ（何を頼まれたかが分からなければ）、進めずに、依頼の全文を求めて止まる。", rules)
        name = frontmatter(ROOT / "skills" / "bb-writ" / "SKILL.md")["agent"].partition(":")[2]
        [definition] = [p for p in sorted((ROOT / "agents").glob("*.md")) if frontmatter(p).get("name") == name]
        body = skill_body(self, definition.read_text(encoding="utf-8"), definition.name)
        self.assertIn(rules, body)
        self.assertIn("`bb-writ:conduct` を Skill ツールで呼び直して", body)
        # 定義だけが持つ 2 つ: 直接立てられたら止まる。要約に依頼と伝言の原文を残す（要約役が従わず落とすこともあり、
        # 依頼がまるごと落ちたときは、写した決まりの「依頼が届いていなければ止まる」に頼る〔止まる場面は実物で確かめていない〕。一部だけの落ちは見分けない）
        self.assertIn("Agent ツールの依頼として始まったとき（会話のどこにも入口の本文が無く、会話の最初が要約でもない）は、何もせずに", body)
        self.assertIn("利用者の依頼の原文（入口に渡された引数と、伝言で届いた利用者の入力）と、作業の記録（note.md）の道筋を、要約に一字も変えずに残す", body)

    def test_entry_keeps_what_to_ask_and_review_rules_within_the_compaction_head(self):
        """Claude Code は要約の後、呼んだスキルを先頭 5,000 トークンまで戻す（合計の枠を超えた古いものは落ちる）。
        入口の本体（`conduct`。入口が呼び、進め方の全文を持つ）の聞く範囲・呼び直しの文・返答の形・検証の条件 (1)〜(6) の文が、それぞれ 1 つずつ戻る先頭にあることを確かめる。
        確かめるのは目印の語が 1 回だけあり、条件の段落の終わり（5,600 字以内）より前にあることだけで、
        目印同士の順と文の意味は確かめない（意味は新しい文脈の読者役で確かめる）。目印の文を書き換えたら、ここの目印も直す。"""
        text = (ROOT / "skills" / "conduct" / "SKILL.md").read_text(encoding="utf-8")
        body = skill_body(self, text, "conduct")
        self.assertEqual(body.count("信じてよい条件"), 1, "検証の条件の段落が 1 つでない")
        start = body.index("信じてよい条件")
        self.assertIn("\n\n", body[start:], "検証の条件の段落の終わりが見つからない")
        end = body.index("\n\n", start)  # 条件 (1)〜(6) は 1 つの段落（分けたら、ここも直す）
        # 境目の 5,589 字は、前置きの 1 行を含めて opus の input_tokens の差で 4,946 トークン（2026-10-20 の実測）。
        # 上限まで約 50 トークンしか余裕が無い。字あたりのトークン数は中身（英数字と日本語の割合）で変わり、
        # 前置きの長さは置き場で変わり、Claude Code の切り詰めの数え方も確かめていないので、字数は近似にとどまる。
        self.assertLessEqual(end, 5600, f"検証の条件の段落の終わりが {end} 字目")
        anchors = (
            "このスキル（`bb-writ:conduct`）を呼び直して全文を読み直す",
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
        """要約の後に入口の本体が合計の枠から落ちたときのため、手引き 8 本・delegate・interrogate の本文の先頭 4,000 字以内に
        呼び直しの文を置く。単独でも呼ばれる delegate・interrogate は、入口から始めた仕事に限る。
        playbook-pause-safely・playbook-eval・reflect・correct・unknowns・原則・検証スキルには置いていない。
        呼び直すのは本体 `bb-writ:conduct`。入口 `bb-writ:bb-writ` を呼び直させる前のバージョンの文と、「`bb-writ:bb-writ` を呼び直…」の
        言い回しは、どのスキルにも残さない（Claude Code では、入口を呼ぶたびに fork した指揮者がもう 1 人立つ）。見るのはこの 3 つの
        文字列だけで、別の言い回しで入口を呼び直させる文は見ない。目印の文を書き換えたら、ここの目印も直す。"""
        plain = "会話が要約された後は、入口の本体 `bb-writ:conduct` を呼び直して全文を読み直す"
        scoped = "入口 `bb-writ` から始めた仕事で会話が要約された後は、入口の本体 `bb-writ:conduct` を呼び直して全文を読み直す"
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
        for p in sorted((ROOT / "skills").glob("*/SKILL.md")):  # 前のバージョンの文（入口そのものを呼び直させる）
            text = p.read_text(encoding="utf-8")
            for old in ("入口 `bb-writ` を呼び直して", "入口を呼び直して", "`bb-writ:bb-writ` を呼び直"):
                self.assertNotIn(old, text, p.parent.name)


class TestWorkflows(unittest.TestCase):
    """CI の定義（.github/workflows）。"""

    def test_ci_jobs_have_room_for_the_slow_macos_runners(self):
        """macOS の job は、成功した実行で 8〜12.9 分かかり（2026-11。0.24.0 でテストが増えた後の最長が 12.9 分）、日によって 1〜2 割揺れる。
        下限は、その最長の 3 倍（38.7 分）を切り上げた 39 分。上限 10 分では全部通るテストが 3 回続けて打ち切られた（2026-11）。
        見るのは、ファイルの中の「timeout-minutes:」の後に、空白と、引用符 1 つまでを挟んで、数字が続く形だけ（数字は 10 進の数として読む）。
        守らない範囲: ほかの書き方の上限（式 ${{ }}、符号つき、引用符の中の空白、鍵を引用符で囲む、など）と、上限を書かない job
        （既定は 360 分）は見ない。ファイルの文字を見るだけなので、step ごとの上限や、コメントの中の timeout-minutes でも落ちる
        （job の上限でなくても）。"""
        files = sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
        self.assertTrue(files)
        for p in files:
            for m in re.finditer(r"timeout-minutes:\s*[\"']?(\d+)", p.read_text(encoding="utf-8")):  # 引用符で書いた数も見る
                self.assertGreaterEqual(int(m.group(1)), 39, f"{p.name} の timeout-minutes: {m.group(1)}。最も遅い OS（macOS）の成功した実行の最長を "
                                        "gh run list・gh run view で見て、その 3 倍以上にする。テストを削って合わせない")

    def test_ci_also_runs_the_tests_with_the_temporary_place_under_a_symlink(self):
        """macOS の一時ディレクトリは symlink の下にあり、解く前と解いた道筋を比べるテストの誤りが、macOS の CI で何度か落ち（0.24.0 など）、
        0.28.0 の前にも手元で同じ形を作って見つかった。Linux の job でも、TMPDIR を symlink にした道筋で全テストを回す。見るのは、workflow に ln -s と、
        その道筋を TMPDIR にして、テストの名前で絞らずに tests/run.py を回す行があることだけ（その step が実際に動くか、どの job で動くかは見ない。
        コメントにした行、if: false の step、ln -s の行き先が違う形でも通る）。"""
        text = "\n".join(p.read_text(encoding="utf-8") for p in sorted((ROOT / ".github" / "workflows").glob("*.y*ml")))
        link = re.search(r"ln -s \S+ (\S+)", text)
        self.assertTrue(link, "workflow に、一時の置き場の symlink を作る ln -s が無い")
        self.assertRegex(text, r"(?m)TMPDIR=" + re.escape(link.group(1)) + r" python3 -B tests/run\.py(?: -j \d+)?$")


def script_strings(src, docstrings=True):
    """ソースの文字列の定数を、隣り合う文字列の継ぎ目と f 文字列の値を外して並べる（値の所は \0）。docstrings が偽なら、
    モジュール・クラス・関数の docstring を除く。"""
    import ast
    tree = ast.parse(src)
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    docs = set() if docstrings else {id(n.body[0].value) for n in ast.walk(tree) if isinstance(n, owners) and ast.get_docstring(n, clean=False) is not None}
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            out.append("".join(v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "\0" for v in node.values))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            out.append(node.value)
    return out


def readme_headings(text):
    """README の見出しを (段, 名前) で並べる。コードブロックの中の行は見出しにしない。"""
    found, fence = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
        elif not fence and (m := re.match(r"(#{1,6})\s+(.*?)\s*#*\s*$", line)):
            found.append((len(m.group(1)), m.group(2)))
    return found


class TestRulesTable(unittest.TestCase):
    """AGENTS.md の「規則と強制するもの」の行は、短い規則（経緯は括弧で短く）と強制するものの名前だけにする。見るのは行の長さと、名指ししたテストの名前の実在だけ。
    行として数えるのは「| 」で始まる行だけで、先頭の「| 」を落とした行は数えない。欄は最初の「 | 」で分けるので、
    コードの中の「 | 」で欄の長さを取り違えることがある（守らない）。"""

    def rows(self):
        text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        section = text[text.index("## 規則と強制するもの"):text.index("## commit")]
        rows = [line for line in section.splitlines() if line.startswith("| ") and not line.startswith("| 規則 |")]
        self.assertTrue(rows, "規則の表が見つからない")
        return rows

    def test_rules_table_rows_stay_short(self):
        for row in self.rows():
            rule, enforce = (cell.strip() for cell in row.split(" | ", 1))
            self.assertLessEqual(len(rule), 260, f"規則の文が長い: {rule[:40]}")
            self.assertLessEqual(len(enforce), 360, f"強制するものの文が長い: {rule[:40]}")

    def test_tests_named_in_the_rules_table_exist(self):
        """表が名指しするテストの名前（test_…・Test…）は、tests/ に在る（名前を変えたら表も直す）。"""
        defined = set()
        for path in (ROOT / "tests").glob("*.py"):
            defined |= set(re.findall(r"(?:def|class)\s+((?:test_|Test)\w+)", path.read_text(encoding="utf-8")))
        named = set(re.findall(r"`((?:test_|Test)\w+)`", "\n".join(self.rows())))
        self.assertTrue(named)
        self.assertEqual(named - defined, set())


class TestReadmeSections(unittest.TestCase):
    def test_sections_the_scripts_point_users_to_exist_in_the_readme(self):
        # launcher と work.py は、専用ログインが無いときに README の節の名前を示す。見出しを変えて文だけが古くならないように。
        # 守らない範囲: 「README の「X」の「Y」」の形でない書き方（「README の X 節」など）、+ でつないだ文字列、f 文字列の値で入れた名前は拾わない
        headings = readme_headings((ROOT / "README.md").read_text(encoding="utf-8"))
        named = []
        for script in ("launch.py", "work.py"):
            for s in script_strings((ROOT / "scripts" / script).read_text(encoding="utf-8")):
                named += [(script, top, sub) for top, sub in re.findall(r"README の「([^」\0]+)」(?:の「([^」\0]+)」)?", s)]
        self.assertTrue(named, "scripts が README の節を指す文が見つからない（文の形が変わった）")
        for script, top, sub in named:
            self.assertIn((2, top), headings, f"{script} が指す README の節「{top}」が無い")
            if sub:
                at = headings.index((2, top))
                body = []
                for level, name in headings[at + 1:]:
                    if level <= 2:
                        break
                    body.append((level, name))
                self.assertIn((3, sub), body, f"{script} が指す README の「{top}」の「{sub}」が無い")


class TestQuotedOutputs(unittest.TestCase):
    def test_what_the_scripts_print_and_the_skills_quote_is_looked_at_by_a_test(self):
        """スキル（skills/*/SKILL.md）と README.md が「…」で引く句のうち、scripts/*.py の文字列の定数に在る 8 字以上のものは、
        tests/*.py の文字列の定数にも在る。手引きは、補助コードの出す文を引いて、読者に次の手を決めさせる（「理由が『…』なら」
        「『…』（終了コード 2）で拒まれる」）。その文が変わると案内が黙って切れるので、文を見るテストを置かせる（引いた文を見るテストが
        無いまま、手引きに入った）。引いた句は、置き換えの印（<…>・…・（…））で切り、いちばん長い切れ端で照らす。docstring は、
        scripts の側では出す文でなく、tests の側では照合でないので、どちらも数えない。
        守らない範囲: 文字列が tests に在ることだけを見る（assert しているかは見ない）。同じ句を出す箇所が scripts に複数あると、
        1 か所でもテストが見ていれば通る。scripts の文字列の定数が補助コードの出す文かどうかは見分けない（記録の欄の名前やただの語も、
        補助コードの文を引いたのでない「…」も、scripts の文字列に在れば数える）。tests の側は、1 つの文字列の定数に続けて在る句だけを
        数える（f 文字列の値や + で継いで作る句は、在ると数えない）。「」でない引き方（バッククォート・言い換え）、終了コード、8 字に
        満たない句（置き換えの印で切れて 8 字に満たなくなる句を含む）は見ない。"""
        def strings(folder):
            return [s for f in sorted((ROOT / folder).glob("*.py")) for s in script_strings(f.read_text(encoding="utf-8"), docstrings=False)]
        scripts, tests = strings("scripts"), strings("tests")
        quoted, missing = 0, []
        for doc in [*sorted((ROOT / "skills").glob("*/SKILL.md")), ROOT / "README.md"]:
            for n, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
                for q in re.findall(r"「([^」]+)」", line):
                    key = max((part.strip() for part in re.split(r"<[^>]*>|…|（[^）]*）?", q)), key=len)
                    if len(key) < 8 or not any(key in s for s in scripts):
                        continue
                    quoted += 1
                    if not any(key in s for s in tests):
                        missing.append(f"{doc.relative_to(ROOT)}:{n}: 「{q}」")
        # 下限は、引き方か拾い方が変わって大きく減れば落ち、手引きの句をいくつか言い換えただけでは落ちない値
        self.assertGreaterEqual(quoted, 12, "スキルと README が「」で引く補助コードの文が、ほとんど見つからない。引き方か拾い方が変わったので、この検査の拾い方を直す")
        if missing:
            self.fail("スキルか README が「」で引く補助コードの文を、どのテストも見ていない。それぞれの文を assert するテストを tests に足す:\n"
                      + "\n".join(missing))


if __name__ == "__main__":
    unittest.main()
