"""入力の形の約束を、揺れを組み合わせた多数の入力で確かめる（Property-Based Testing。scripts/work.py の note_gaps・tail）。

順序・途中の kill・再開の約束は test_oprun_model.py が全部の順序でたどる。ここは、そのモデルの外に残る入力の形・文字コード・大きさを見る。
見つける力は乱数ではなく、生成する揺れの一覧で決まる（雛形と一般的な Markdown の揺れだけの生成器は、審査で 3・4 周目に見つかった
note_gaps の不具合を 1 つも見つけなかった）。揺れは次の出所から並べる。
- 雛形: 欄の名前・括弧、必須の欄（周が 3 に達すると仕組みの要否も）。
- 手入力の揺れ: 箇条書きの記号（-・*・+・「・」・全角のハイフン・番号・チェックボックス）と、その後の空白（無し・タブ・全角）、
  全角の「：」、半角の括弧、名前の中・後・括弧の前の空白、大文字と小文字、強調、NFD、BOM、CRLF、欄を書く順。
- Markdown の記号: 見出し（「#」だけ・タブ・3 文字までの字下げ・全角の「＃」・setext）、行頭の「#12」、行の途中の「#」、表、字下げ。
- 過去の修正: 括弧を省いた名前、別の文言の括弧（本物の欄の前にも後にも）、同じ欄の 2 回目、欄の名前で始まるだけの行。
- 大きさの境界: tail の 64 KiB の窓のちょうど・またぐ多バイト文字・窓より長い末尾の空白、8 KiB の上限の近く。
- 文字コード: 多バイト文字・NUL・JSON で伸びる文字・全角の空白。
守らないもの: 雛形に無い名前の行（「- メモ:」）は中身に数える（work.py の field_of）。UTF-8 でない記録。budget が印の 24 バイト以下の tail。
壊した版で落ちることを確かめた（記号の後の空白を「 ?」に狭めた版だけは落ちない。名前を照らすときに空白を無視するので結果が変わらない）。

TestFixedExamples は、見つけた反例と境界の例を Hypothesis なしで回す（手元に Hypothesis が無くても、直した揺れの退行を落とす）。
TestInputShapes は Hypothesis（tests/requirements.txt）が無ければ飛ばす。CI は入れて、毎回同じ入力（derandomize）で回す。
広く回すときは BB_WRIT_PBT_EXAMPLES に例の数を渡す（乱数も毎回変わる）。
"""

import importlib.util
import json
import os
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent)); import process_limit  # noqa: E402,F401  試験に上限を掛ける（直に走らせても）

try:
    from hypothesis import example, given, settings, strategies as st, HealthCheck
except ImportError:  # 試験だけの依存。無ければ TestInputShapes だけ飛ばす（理由は unittest の出力に出る）
    given = None

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
FILLED = {f: f"- {f}: 書いた" for f in R}


def note_with(**over):
    """必須の欄を全部書いた記録の、i 番目の欄の行（0 から）を over[f"f{i}"] に替えたもの。"""
    return "\n".join(over.get(f"f{i}", FILLED[f]) for i, f in enumerate(R)) + "\n"


# (名前, 本文, 空と判定されるべき欄)。揺れた欄の行は、前の空の欄（制約）の後に置き、前の欄の中身に数えないかも見る
FIELD_LINE_FORMS = ["* {}: 書いた", "+ {}: 書いた", "・{}: 書いた", "－ {}: 書いた", "1. {}: 書いた", "2) {}: 書いた",
                    "- [ ] {}: 書いた", "-\t{}: 書いた", "-　{}: 書いた", "-{}: 書いた", "- {}：書いた", "- {} : 書いた",
                    "- **{}**: 書いた"]
NOTE_EXAMPLES = [
    *[(f"揺れた欄の行 {form!r}", note_with(f1="- 制約:", f2=form.format("できたと分かる確かめ方")), {R[1]}) for form in FIELD_LINE_FORMS],
    ("括弧ごと強調した名前", note_with(f1="- 制約:", f2="- **できたと分かる確かめ方（述語）**: 書いた"), {R[1]}),
    ("半角の括弧", note_with(f1="- 制約:", f2="- できたと分かる確かめ方(述語): 書いた"), {R[1]}),
    ("括弧の前の空白", note_with(f1="- 制約:", f2="- できたと分かる確かめ方 （述語）: 書いた"), {R[1]}),
    ("名前の中の空白を詰める", note_with(f1="- 制約:\n- ホストとsession ID: x"), {R[1]}),
    ("名前の大文字", note_with(f1="- 制約:\n- ホストと SESSION ID: x"), {R[1]}),
    ("別の文言の括弧の行（本物の欄の前）", note_with(f1="- 制約（未定）: 書いた\n- 制約:"), {R[1]}),
    ("別の文言の括弧の行（空の欄の後）", note_with(f1="- 制約:\n- できたと分かる確かめ方（未定）: x"), {R[1]}),
    ("同じ欄の 2 回目", note_with(f1="- 制約:") + "- 制約: 後から書いた\n", {R[1]}),
    ("先頭の BOM", "﻿" + note_with(), set()),
    ("NFD", unicodedata.normalize("NFD", note_with(f1="- 制約:", f2="- できたと分かる確かめ方: で")), {R[1]}),
    ("CRLF", note_with(f1="- 制約:").replace("\n", "\r\n"), {R[1]}),
    ("行頭の #12 は中身", note_with(f1="- 制約:\n#12 の件"), set()),
    ("行の途中の # は中身", note_with(f1="- 制約:\nメモ # 2"), set()),
    ("欄の名前で始まるだけの行・字下げした欄・表は中身",
     note_with(f0="- 目的:\n- 目的外のこと: x", f1="- 制約:\n  - 決めたことと根拠: x", f2="- できたと分かる確かめ方:\n| a | b |"), set()),
    *[(f"見出し {h!r} で中身が終わる", note_with(f1=f"- 制約:\n{h}\n下に書いた"), {R[1]})
      for h in ["#", "## メモ", "#\tメモ", "   ## メモ", "＃ メモ", "メモ\n===", "メモ\n---"]],
]
TAIL_EXAMPLES = [  # (名前, parts, lines, budget)
    ("窓の先頭が文字の 2 バイト目", [("あ", 30000), ("a", 2)], 30, 200_000),
    ("窓ちょうどの 1 行", [("a", 65536)], 1, 200_000),
    ("窓より長い末尾の空白", [("a", 5), (" ", 70000)], 1, 0),
    ("先頭の行を落としても印を付けない", [("b", 70000), ("\nlast", 1)], 1, 0),
    ("上限の近くの多バイト文字", [("😀", 1500)], 1, 0),
    ("上限を超える 1 行", [("a", 20000)], 1, 0),
    ("改行でない \\r は行を割らない", [("a\r", 10), ("b", 1)], 1, 0),
    ("全角の空白は削らない", [("x", 1), ("　", 1)], 1, 0),
]


class TestFixedExamples(unittest.TestCase):
    def test_hand_typed_notes(self):
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
    HEADING = st.sampled_from(["## メモ", "# 別の節", "#", "###", "#\tタブの見出し", "   ## 字下げした見出し", "＃ 全角", "見出しの文\n==="])
    BULLET = st.sampled_from(["- ", "* ", "+ ", "・", "－ ", "1. ", "2) ", "- [ ] ", "-\t", "-  ", "-　", "-"])

    def written_name(draw, name):
        """欄の名前の書き方。雛形どおり・括弧を省く・半角の括弧・名前の後や括弧の前の空白・名前の中の空白と大文字・強調。"""
        short, paren = name.split("（")[0], name[len(name.split("（")[0]):]
        short = draw(st.sampled_from([short, short.replace(" ", ""), short.upper()])) if " " in short else short
        forms = [short + paren, short, short + " "] + ([short + paren.replace("（", "(").replace("）", ")"), short + " " + paren] if paren else [])
        label = draw(st.sampled_from(forms))
        return f"**{label.strip()}**" if draw(st.booleans()) else label

    @st.composite
    def notes(draw):
        """(記録の本文, 周の数, 空と判定されるべき欄の集合)。"""
        rounds = draw(st.sampled_from([0, w.PREMISE_ROUNDS]))
        req = R + ([w.PREMISE] if rounds >= w.PREMISE_ROUNDS else [])
        lines, empty, written = (["# 作業の記録", ""] if draw(st.booleans()) else []), set(), set()  # 見出しの無い記録は、BOM が最初の欄の行に付く
        for name in draw(st.permutations(w.FIELDS)):  # 欄を書く順は見ない（名前に空白のある欄が必須の欄の後に来ることもある）
            state = draw(st.sampled_from(["filled", "empty", "absent"])) if name in req else "filled"
            if state == "absent":
                empty.add(name)
                continue
            body = draw(st.lists(CONTENT, min_size=1, max_size=3)) if state == "filled" else []
            if state == "empty":
                empty.add(name)
                body = draw(st.sampled_from([[], ["   "], [""]]))
            head = f"{draw(BULLET)}{written_name(draw, name)}{draw(st.sampled_from([':', '：', ' :']))}"
            if body and body[0].strip() and draw(st.booleans()):
                head += " " + body.pop(0)
            lines += [head] + body
            written.add(name)
            for _ in range(draw(st.integers(0, 2))):  # 欄の後に挟むもの。どれも前の欄の中身を終わらせ、どの欄でもない
                other = draw(st.sampled_from(w.FIELDS)).split("（")[0]  # 別の文言の括弧の行は、その名前の本物の欄より前にも来る
                lines.append(draw(st.one_of(HEADING, st.just(f"- {other}（未定）: 別の文言の括弧の行"))))
                if draw(st.booleans()):
                    lines.append("見出しや別の文言の欄の下に書いた行")
        for name in sorted(empty & written):  # 同じ欄の 2 回目は見ない（最初の欄が空なら空）
            if draw(st.booleans()):
                lines.append(f"- {name}: 後から書いた")
        text = "\n".join(lines) + "\n"
        if draw(st.booleans()):
            text = unicodedata.normalize("NFD", text)  # 濁点を分けて保存するエディタ
        if draw(st.booleans()):
            text = text.replace("\n", "\r\n")
        if draw(st.booleans()):
            text = "﻿" + text
        return text, rounds, empty

    CHUNK = st.sampled_from(["a", "あ", "😀", "\n", " ", "\r", "\t", '"', "\x00", "é", "　"])
    RUN = st.one_of(st.integers(1, 200), st.integers(1000, 4000), st.integers(20_000, 70_000))  # 短い並び、上限の近く、64 KiB の窓の境をまたぐ長い並び


@unittest.skipIf(given is None, "Hypothesis が無い（pip install -r tests/requirements.txt）")
class TestInputShapes(unittest.TestCase):
    if given is None:
        def test_input_shapes(self):  # 飛ばしたことを出力に残す（クラスが空だと何も出ない）
            pass
    else:
        @SETTINGS
        @given(notes())
        def test_the_gate_names_exactly_the_fields_left_empty_however_the_note_is_typed(self, case):
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
