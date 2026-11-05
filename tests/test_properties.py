"""入力の形の約束を、揺れを組み合わせた多数の入力で確かめる（Property-Based Testing。scripts/work.py の note_body・note_gaps・tail）。
確認した work.py: 5b091ff の欄の判定と tail に、note_body の utf-8-sig（f41aa6f）を足したもの。

順序・途中の kill・再開の約束は test_oprun_model.py が全部の順序でたどる。ここは、そのモデルの外に残る入力の形・文字コード・大きさを見る。
揺れは次の出所から並べる。
- 雛形: 欄の名前・括弧（省いてよい）、必須の欄（周が 3 に達すると仕組みの要否も）、欄を書く順。
- 書き足し方: 「:」の後に書く・次の行から書く・空白だけ・空行、CRLF、先頭の BOM。
- Markdown の記号: 見出し（「#」だけ・タブ・#が 1〜3 つ）、行頭の「#12」、行の途中の「#」、表、字下げ、入れ子の箇条書き。
- 過去の修正: 括弧を省いた名前、別の文言の括弧の行（その欄の前に書いても、その欄でない）、同じ欄の 2 回目、欄の名前で始まるだけの行、字下げした欄の名前。
- 大きさの境界: tail の 64 KiB の窓のちょうど・またぐ多バイト文字・窓より長い末尾の空白、8 KiB の上限の近く。境の例は生成器では
  当たりにくいので、TestFixedExamples の固定の例が受け持つ（生成器は窓をまたぐ長さと上限の近くの長さを多く引くだけ）。
  別の文言の括弧の行と、記号だけの行も、固定の例だけが確かめる（生成器は作らない）。
- 文字コード: 多バイト文字・NUL・JSON で伸びる文字・全角の空白。
省いたもの: 雛形の行を書き換えた欄の行（「* 目的:」「- 目的：」、NFD、別の文言の括弧の「- 制約（未定）:」）。欄と見なさず前の欄の中身に
数えるので、その前の空の欄は埋まって見える。手元の作業の記録（比べるための候補を除く 202 件）の欄らしい行 1,831 行に、記号や「:」を書き換えた欄の行は 0 行で、別の文言の括弧の
行 14 行は多くが欄の中の項目（「- 指摘の扱い（審査 1 回目）: …」。中身に数えるのが正しい）だったので、守らない範囲として
work.py の field_line に書いた（生成した揺れで見つかった穴を、揺れを読み分ける解析器で塞ぐと、審査の周ごとに新しい穴が出た）。
記号だけの行（区切り線「---」、空の「-」、HTML のコメント）は中身に数える（その欄は空と見なさない）。手元の記録の欄の中身に 0 行で、
この今の振る舞いを「---」と HTML のコメントの固定の例で固定する（空の「-」だけの行の例は無い）。UTF-8 でない記録、budget が印の 24 バイト以下の tail も見ない。

TestFixedExamples は、過去の修正の反例と境界の例と、守らないと決めた今の振る舞いの例を Hypothesis なしで回す（手元に Hypothesis が無くても、退行を落とす）。
TestInputShapes は Hypothesis（tests/requirements.txt）が無ければ飛ばす。CI は入れて、毎回同じ入力（derandomize）で回す。
広く回すときは BB_WRIT_PBT_EXAMPLES に例の数を渡す（乱数も毎回変わる）。
Hypothesis の置き場（定数のキャッシュの .hypothesis/）は、リポジトリに残さないよう、実行の終わりに消える一時ディレクトリに向ける。
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent)); import process_limit  # noqa: E402,F401  試験に上限を掛ける（直に走らせても）

try:
    from hypothesis import given, settings, strategies as st, HealthCheck
    from hypothesis.configuration import set_hypothesis_home_dir
except ImportError:  # 試験だけの依存。無ければ TestInputShapes だけ飛ばす（理由は unittest の出力に出る）
    given = None
else:
    # Hypothesis は、database=None でも置き場（局所のモジュールの定数のキャッシュ、文字の表など）を、import した時点の
    # 作業ディレクトリの .hypothesis/ に作る。リポジトリに残すと task cleanup が片付けを拒むので、置き場の全部を、
    # 実行の終わりに消える一時ディレクトリに向ける（HYPOTHESIS_STORAGE_DIRECTORY は子のプロセスにも渡るので使わない。
    # 強制終了したときは一時ディレクトリが残る）
    HYPOTHESIS_HOME = tempfile.TemporaryDirectory(prefix="bb-writ-hypothesis-")
    set_hypothesis_home_dir(HYPOTHESIS_HOME.name)

WORK = Path(__file__).resolve().parents[1] / "scripts" / "work.py"


def load_work():
    sys.dont_write_bytecode = True  # プラグインの置き場に __pycache__ を作らない
    spec = importlib.util.spec_from_file_location("work_under_test", WORK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


w = load_work()
MARK = "（先頭を省いた）"


def json_size(s):  # ops.jsonl に書かれる形（ensure_ascii=False）での大きさ
    return len(json.dumps(s, ensure_ascii=False).encode()) - 2


def check_note(t, text, empty, rounds=0):
    """関門と同じ経路（ファイルに書いて note_body で読む）で、空と判定される欄がちょうど empty か。"""
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "note.md"
        f.write_bytes(text.encode("utf-8"))
        body = w.note_body(f)
    t.assertEqual(set(w.note_gaps(body, publish=True, rounds=rounds)), set(empty), text)


def check_tail(t, parts, lines, budget):
    """tail が上限を超えず、出力の末尾の接尾辞を返し、行の途中から始まれば印を付け、窓と上限に収まれば末尾の行そのものを返すか。"""
    text = "".join(c * n for c, n in parts)
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "run.log"
        f.write_bytes(text.encode("utf-8"))
        budget = budget or w.MAX_TAIL
        got = w.tail(f, lines, budget=budget)
    t.assertLessEqual(json_size(got), budget)  # 記録を膨らませない
    body = got[len(MARK):] if got.startswith(MARK) else got
    full = text.encode("utf-8").rstrip().decode("utf-8")
    t.assertTrue(full.endswith(body), "出力の末尾でない（文字の途中から読んだか、行を取り違えた）")
    start = len(full) - len(body)
    if body and start > 0 and full[start - 1] != "\n":
        t.assertTrue(got.startswith(MARK), "行の途中から始まるのに、先頭を省いた印が無い")
    want = "\n".join(full.split("\n")[-lines:])
    if json_size(want) <= budget and len(want.encode("utf-8")) <= 64 * 1024:  # 窓にも上限にも収まるなら、末尾の行そのまま（印も無い）
        t.assertEqual(got, want)


R = w.REQUIRED + [w.TRIAGE]


def note_with(**over):
    """必須の欄を全部書いた記録の、i 番目の欄の行（0 から）を over[f"f{i}"] に替えたもの。"""
    return "# 作業の記録\n\n" + "\n".join(over.get(f"f{i}", f"- {f}: 書いた") for i, f in enumerate(R)) + "\n"


NOTE_EXAMPLES = [  # (名前, 本文, 空と判定されるべき欄)
    ("雛形のまま", w.NOTE, R),
    ("括弧を省いた名前（空の欄の次）", note_with(f1="- 制約:", f2="- できたと分かる確かめ方: 書いた"), {R[1]}),
    ("括弧を省いた名前（書いた欄）", note_with(f3="- 指摘の扱い: 書いた"), set()),
    ("次の行から書いた中身・表・入れ子", note_with(f0="- 目的:\n  書いた", f1="- 制約:\n| a | b |", f2="- できたと分かる確かめ方:\n  - 入れ子"), set()),
    ("空白だけ", note_with(f0="- 目的:   \n　\t"), {R[0]}),
    ("行頭の #12 は中身", note_with(f1="- 制約:\n#12 の件"), set()),
    ("行の途中の # は中身", note_with(f1="- 制約:\nメモ # 2"), set()),
    ("欄の名前で始まるだけの行・字下げした欄は中身", note_with(f0="- 目的:\n- 目的外のこと: x", f1="- 制約:\n  - 決めたことと根拠: x"), set()),
    ("字下げした欄の名前は、前にあっても、その欄でない", "  - 制約: 字下げ\n" + note_with(f1="- 制約:"), {R[1]}),
    ("別の文言の括弧の行は、その欄でない", "- できたと分かる確かめ方（未定）: x\n" + note_with(f2="- できたと分かる確かめ方（述語）:"), {R[2]}),
    ("同じ欄の 2 回目は見ない", note_with(f1="- 制約:") + "- 制約: 後から書いた\n", {R[1]}),
    ("必須でない欄の行で中身が終わる", note_with(f1="- 制約:\n- 決めたことと根拠: x"), {R[1]}),
    ("CRLF", note_with(f1="- 制約:").replace("\n", "\r\n"), {R[1]}),
    ("見出しの無い記録の先頭の BOM", "\ufeff" + "\n".join(f"- {f}: 書いた" for f in R) + "\n", set()),
    ("記号だけの行も中身に数える", note_with(f1="- 制約:\n---", f2="- できたと分かる確かめ方:\n<!-- 後で -->"), set()),
    *[(f"見出し {h!r} で中身が終わる", note_with(f1=f"- 制約:\n{h}\n下に書いた"), {R[1]}) for h in ["#", "## メモ", "#\tメモ", "### メモ"]],
]
TAIL_EXAMPLES = [  # (名前, parts, lines, budget)
    ("窓の先頭が文字の 2 バイト目", [("あ", 30000), ("a", 2)], 30, 200_000),
    ("窓ちょうどの 1 行", [("a", 65536)], 1, 200_000),
    ("窓より長い末尾の空白", [("a", 5), (" ", 70000)], 1, 0),
    ("先頭の行を落としても印を付けない", [("b", 70000), ("\nlast", 1)], 1, 0),
    ("上限の近くの多バイト文字", [("😀", 1500)], 1, 0),
    ("全角の空白は削らない", [("x", 1), ("　", 1)], 1, 0),
    ("上限を超える 1 行", [("a", 20000)], 1, 0),
    ("改行でない \\r は行を割らない", [("a\r", 10), ("b", 1)], 1, 0),
]


class TestFixedExamples(unittest.TestCase):
    def test_notes(self):
        for name, text, empty in NOTE_EXAMPLES:
            with self.subTest(name):
                check_note(self, text, empty)

    def test_the_premise_field_is_needed_after_three_rounds(self):
        premise = "- 仕組みの要否:\n- 決めたことと根拠: x"
        check_note(self, note_with() + premise + "\n", {w.PREMISE}, rounds=w.PREMISE_ROUNDS)
        check_note(self, note_with() + premise + "\n", set(), rounds=w.PREMISE_ROUNDS - 1)

    def test_tail_boundaries(self):
        for name, parts, lines, budget in TAIL_EXAMPLES:
            with self.subTest(name):
                check_tail(self, parts, lines, budget)


if given is not None:
    WIDE = int(os.environ.get("BB_WRIT_PBT_EXAMPLES", "0"))
    SETTINGS = settings(max_examples=WIDE or 200, derandomize=not WIDE, database=None, deadline=None,
                        suppress_health_check=list(HealthCheck))
    PROSE = ["試験を足す", "push しない", "x", "作業場所は main"]
    CONTENT = st.one_of(
        st.sampled_from(PROSE),
        st.sampled_from(PROSE).map(lambda s: "  " + s),            # 字下げした続き
        st.sampled_from(PROSE).map(lambda s: f"| {s} | 対応する |"),  # 表
        st.sampled_from(PROSE).map(lambda s: f"  - {s}"),           # 入れ子の箇条書き
        st.sampled_from(PROSE).map(lambda s: f"#12 {s}"),           # 行頭の Issue 番号（見出しでない）
        st.just("メモ # 2"),                                         # 行の途中の #
        st.just("- 目的外のこと: x"),                                 # 欄の名前で始まるだけの行（欄でない）
        st.just("  - 決めたことと根拠: 字下げした行"),                 # 字下げした欄の名前（欄でない）
    )
    HEADING = st.sampled_from(["## メモ", "# 別の節", "#", "###", "#\tタブの見出し"])

    @st.composite
    def notes(draw):
        """(記録の本文, 周の数, 空と判定されるべき欄の集合)。欄の行は雛形どおりか、括弧を省いた形。"""
        rounds = draw(st.sampled_from([0, w.PREMISE_ROUNDS]))
        req = R + ([w.PREMISE] if rounds >= w.PREMISE_ROUNDS else [])
        lines, empty, written = (["# 作業の記録", ""] if draw(st.booleans()) else []), set(), set()
        for name in draw(st.permutations(w.FIELDS)):  # 欄を書く順は見ない
            state = draw(st.sampled_from(["filled", "empty", "absent"])) if name in req else "filled"
            if state == "absent":
                empty.add(name)
                continue
            body = draw(st.lists(CONTENT, min_size=1, max_size=3)) if state == "filled" else []
            if state == "empty":
                empty.add(name)
                body = draw(st.sampled_from([[], ["   "], [""]]))
            head = f"- {draw(st.sampled_from([name, name.split('（')[0]]))}:"
            if body and body[0].strip() and draw(st.booleans()):
                head += " " + body.pop(0)
            lines += [head] + body
            written.add(name)
            for _ in range(draw(st.integers(0, 2))):  # 欄の後の見出し。前の欄の中身を終わらせる
                lines.append(draw(HEADING))
                if draw(st.booleans()):
                    lines.append("見出しの下に書いた行")
        for name in sorted(empty & written):  # 同じ欄の 2 回目は見ない（最初の欄が空なら空）
            if draw(st.booleans()):
                lines.append(f"- {name}: 後から書いた")
        text = "\n".join(lines) + "\n"
        if draw(st.booleans()):
            text = text.replace("\n", "\r\n")
        if draw(st.booleans()):
            text = "\ufeff" + text
        return text, rounds, empty

    CHUNK = st.sampled_from(["a", "あ", "😀", "\n", " ", "\r", "\t", '"', "\x00", "é", "　"])
    RUN = st.one_of(st.integers(1, 200), st.integers(1000, 4000), st.integers(20_000, 70_000))  # 短い並び、上限の近く、64 KiB の窓の境をまたぐ長い並び


@unittest.skipIf(given is None, "Hypothesis が無い（pip install -r tests/requirements.txt）")
class TestInputShapes(unittest.TestCase):
    if given is None:
        def test_input_shapes(self):  # 飛ばしたことを出力に残す（クラスが空だと何も出ない）
            pass
    else:
        def test_running_the_properties_leaves_nothing_in_the_repository(self):
            # Hypothesis は、sys.modules にある局所のモジュール（全試験では test_launch が読み込む launch.py から入る scripts/work.py）
            # の定数のキャッシュなどを、置き場に書く。task cleanup は無視されたファイルも残っていれば片付けを拒むので、
            # リポジトリの写しで work.py を sys.modules に入れてから性質の試験を回し、次の 3 つを見る。
            # 置き場にキャッシュが書かれた（この場面を作れている）、写しに何も増えない、子が終わると置き場が消える
            root = Path(__file__).resolve().parents[1]
            with tempfile.TemporaryDirectory() as d:
                copy = Path(d) / "repo"
                for rel in ("scripts/work.py", "tests/__init__.py", "tests/process_limit.py", "tests/test_properties.py"):
                    (copy / rel).parent.mkdir(parents=True, exist_ok=True)
                    (copy / rel).write_bytes((root / rel).read_bytes())
                before = {p.relative_to(copy) for p in copy.rglob("*")}
                code = ("import sys, unittest; sys.dont_write_bytecode = True; sys.path[:0] = ['.', 'scripts']; import work; "
                        "r = unittest.main(module='tests.test_properties', argv=['x', 'TestInputShapes.test_the_gate_names_exactly_the_fields_left_empty'], exit=False).result; "
                        "from hypothesis.configuration import storage_directory; c = storage_directory('constants', intent_to_write=False); "
                        "c = getattr(c, 'path', c); print(c.parent); print(len(list(c.glob('*'))) if c.is_dir() else 0); "
                        "sys.exit(0 if r.wasSuccessful() and r.testsRun == 1 else 1)")
                env = {k: v for k, v in os.environ.items() if k not in ("HYPOTHESIS_STORAGE_DIRECTORY", "BB_WRIT_PBT_EXAMPLES")}
                run = subprocess.run([sys.executable, "-B", "-c", code], cwd=copy, env=env, capture_output=True, text=True, timeout=300)
                self.assertEqual(run.returncode, 0, run.stderr[-2000:])
                home, cached = run.stdout.split("\n")[-3:-1]
                self.assertGreater(int(cached), 0, f"置き場 {home} に定数のキャッシュが書かれず、確かめる場面を作れていない")
                self.assertEqual(sorted(str(p) for p in {p.relative_to(copy) for p in copy.rglob("*")} - before), [])
                self.assertFalse(Path(home).exists(), f"子が終わっても置き場 {home} が残った")

        @SETTINGS
        @given(notes())
        def test_the_gate_names_exactly_the_fields_left_empty(self, case):
            text, rounds, empty = case
            check_note(self, text, empty, rounds)

        @SETTINGS
        @given(parts=st.lists(st.tuples(CHUNK, RUN), min_size=1, max_size=6), lines=st.integers(1, 30),
               budget=st.sampled_from([0, 200_000]))
        def test_the_recorded_tail_is_a_bounded_suffix_of_the_output(self, parts, lines, budget):
            # 上限が 8 KiB なら 64 KiB の窓の先頭は必ず削られ、文字の途中を飛ばす処理が結果に出ない。窓より大きな上限でも回す
            check_tail(self, parts, lines, budget)


if __name__ == "__main__":
    unittest.main()
