"""入力の形の約束を、揺れを組み合わせた多数の入力で確かめる（Property-Based Testing。scripts/work.py の note_gaps・tail）。

順序・途中の kill・再開の約束は test_oprun_model.py が全部の順序でたどる。ここは、そのモデルの外に残る入力の形・文字コード・大きさを見る。
見つける力は乱数ではなく、生成する揺れの一覧で決まる（雛形と一般的な Markdown の揺れだけの生成器は、審査で 3・4 周目に見つかった
note_gaps の不具合を 1 つも見つけなかった）。揺れは次の出所から並べる。雛形（欄の名前・括弧）、手入力の揺れ（箇条書きの記号・全角の
「：」・半角の括弧・名前の後の空白・BOM・CRLF）、Markdown の記号（見出し・「#」だけの行・行頭の「#12」・表・字下げ）、過去の修正
（括弧を省いた名前・別の文言の括弧・同じ欄の 2 回目・欄の名前で始まるだけの行）、大きさの境界（tail の 64 KiB の窓と 8 KiB の上限）、
文字コード（多バイト文字・NUL・JSON で伸びる文字）。

Hypothesis（tests/requirements.txt）が無ければ飛ばす。CI は入れて、毎回同じ入力（derandomize）で回す。広く回すときは
BB_WRIT_PBT_EXAMPLES に例の数を渡す（乱数も毎回変わる）。
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent)); import process_limit  # noqa: E402,F401  試験に上限を掛ける（直に走らせても）

try:
    from hypothesis import given, settings, strategies as st, HealthCheck
except ImportError:  # 試験だけの依存。無ければこのファイルだけ飛ばす（理由は unittest の出力に出る）
    given = None

WORK = Path(__file__).resolve().parents[1] / "scripts" / "work.py"


def load_work():
    sys.dont_write_bytecode = True  # プラグインの置き場に __pycache__ を作らない
    spec = importlib.util.spec_from_file_location("work_under_test", WORK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


if given is not None:
    w = load_work()
    WIDE = int(os.environ.get("BB_WRIT_PBT_EXAMPLES", "0"))
    SETTINGS = settings(max_examples=WIDE or 200, derandomize=not WIDE, database=None, deadline=None,
                        suppress_health_check=list(HealthCheck))
    REQ = w.REQUIRED + [w.TRIAGE]
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
    HEADING = st.sampled_from(["## メモ", "# 別の節", "#", "###"])

    def written_name(draw, name):
        """欄の名前の書き方。雛形どおり・括弧を省く・半角の括弧・名前の後の空白。"""
        short, paren = name.split("（")[0], name[len(name.split("（")[0]):]
        forms = [name, short, short + " "] + ([short + paren.replace("（", "(").replace("）", ")")] if paren else [])
        return draw(st.sampled_from(forms))

    @st.composite
    def notes(draw):
        """(記録の本文, 空と判定されるべき欄の集合)。"""
        lines, empty, written = (["# 作業の記録", ""] if draw(st.booleans()) else []), set(), set()  # 見出しの無い記録は、BOM が最初の欄の行に付く
        for name in w.FIELDS:
            state = draw(st.sampled_from(["filled", "empty", "absent"])) if name in REQ else "filled"
            if state == "absent":
                empty.add(name)
                continue
            body = draw(st.lists(CONTENT, min_size=1, max_size=3)) if state == "filled" else []
            if state == "empty":
                empty.add(name)
                body = draw(st.sampled_from([[], ["   "], [""]]))
            bullet, colon = draw(st.sampled_from("-*+")), draw(st.sampled_from([":", "：", " :"]))
            head = f"{bullet} {written_name(draw, name)}{colon}"
            if body and body[0].strip() and draw(st.booleans()):
                head += " " + body.pop(0)
            lines += [head] + body
            written.add(name)
            for _ in range(draw(st.integers(0, 2))):  # 欄の後に挟むもの。どれも前の欄の中身を終わらせる
                lines.append(draw(st.one_of(HEADING, st.just(f"- {name.split('（')[0]}（未定）: 別の文言の括弧の行"))))
                if draw(st.booleans()):
                    lines.append("見出しや別の文言の欄の下に書いた行")
        for name in sorted(empty & written):  # 同じ欄の 2 回目は見ない（最初の欄が空なら空）
            if draw(st.booleans()):
                lines.append(f"- {name}: 後から書いた")
        text = "\n".join(lines) + "\n"
        if draw(st.booleans()):
            text = text.replace("\n", "\r\n")
        if draw(st.booleans()):
            text = "﻿" + text
        return text, empty

    def json_size(s):  # ops.jsonl に書かれる形（ensure_ascii=False）での大きさ
        return len(json.dumps(s, ensure_ascii=False).encode()) - 2

    CHUNK = st.sampled_from(["a", "あ", "😀", "\n", " ", "\r", "\t", '"', "\x00", "é"])
    RUN = st.one_of(st.integers(1, 200), st.integers(20_000, 70_000))  # 短い並びと、64 KiB の窓の境をまたぐ長い並び


@unittest.skipIf(given is None, "Hypothesis が無い（pip install -r tests/requirements.txt）")
class TestInputShapes(unittest.TestCase):
    if given is None:
        def test_input_shapes(self):  # 飛ばしたことを出力に残す（クラスが空だと何も出ない）
            pass
    else:
        @SETTINGS
        @given(notes())
        def test_the_gate_names_exactly_the_fields_left_empty_however_the_note_is_typed(self, case):
            text, empty = case
            with tempfile.TemporaryDirectory() as d:  # 読み方（BOM・CRLF）も含めて、関門と同じ経路で読む
                f = Path(d) / "note.md"
                f.write_bytes(text.encode("utf-8"))
                body = w.note_body(f)
            got = set(w.note_gaps(body, publish=True))
            self.assertEqual(got, empty, text)

        @SETTINGS
        @given(parts=st.lists(st.tuples(CHUNK, RUN), min_size=1, max_size=6), lines=st.integers(1, 30),
               budget=st.sampled_from([0, 200_000]))
        def test_the_recorded_tail_is_a_bounded_suffix_of_the_output(self, parts, lines, budget):
            # 上限が 8 KiB なら 64 KiB の窓の先頭は必ず削られ、文字の途中を飛ばす処理が結果に出ない。窓より大きな上限でも回す
            text = "".join(c * n for c, n in parts)
            with tempfile.TemporaryDirectory() as d:
                f = Path(d) / "run.log"
                f.write_bytes(text.encode("utf-8"))
                budget = budget or w.MAX_TAIL
                got = w.tail(f, lines, budget=budget)
            self.assertLessEqual(json_size(got), budget)  # 記録を膨らませない
            body = got[len("（先頭を省いた）"):] if got.startswith("（先頭を省いた）") else got
            full = text.encode("utf-8").rstrip().decode("utf-8")
            self.assertTrue(full.endswith(body), "出力の末尾でない（文字の途中から読んだか、行を取り違えた）")
            want = "\n".join(full.split("\n")[-lines:])
            if json_size(want) <= budget and len(text.encode("utf-8")) < 60 * 1024:  # 窓にも上限にも収まるなら、末尾の行そのまま
                self.assertEqual(got, want)


if __name__ == "__main__":
    unittest.main()
