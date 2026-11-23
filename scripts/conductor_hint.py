#!/usr/bin/env python3
"""Claude Code の UserPromptSubmit の hook（hooks/hooks.json）。入口 bb-writ を呼んだセッションで、この hook が発火するたびに
（利用者の入力のほか、指揮者の終わりの知らせ・子からの伝言でも発火する）、文脈に案内を足す（hookSpecificOutput.additionalContext）。
案内するのは、入口が立てた指揮者（背景の子）の質問・報告への返事と、その仕事の続きは、親が自分で進めず、指揮者にも渡さずに、
利用者が指揮者に直接打つこと。親は、そのしかたを利用者に伝える。
指揮者は、親を通した返事を、本人の選択と確かめられずに断ることがある。

案内だけで、入力は止めない（AGENTS.md: 必須の機能を hook に置かない。hook が動かないホストでも、入口の description が同じことを
求める）。指揮者がいるか、入力が指揮者への返事かは、ここでは決めない（会話記録から確かめられない。親のモデルが案内の条件で決める）。

数える印は、入口（Skill ツールの bb-writ:bb-writ と短い名前 bb-writ、利用者の行の先頭のスラッシュの記録の名前
/bb-writ:bb-writ、利用者の行・入力を受け付けた行の文が /bb-writ:bb-writ か短い名前 /bb-writ で始まるもの）。入口の印の読み方は push_guard.py の entry_in を、
入口だけの名前で使う（判定を写して 2 つ持たない）。本体 bb-writ:conduct だけを呼んだセッションは数えない（入口を通らず、
指揮者がいない）。探索の入口 unknowns の印は、別の読み手 used_unknowns で数え、入口と別の案内を足す（素の push の守りの印には数えない）。
足さない場合: 入力が /bb-writ:unknowns・/unknowns・/bb-writ:explore で始まる（unknowns を呼ぶ入力と、失敗の後に本体を呼ぶ回避）、入力に agent_id がある（親でなく子への入力。指揮者の依頼文に混ざらないように）、入力が入口をスラッシュで呼ぶ形
（/bb-writ:bb-writ と短い名前 /bb-writ の 2 つで、名前そのものか、名前の直後が空白の類（空白・タブ・改行など）。比べ方は
push_guard.py の slash_line で、会話記録の印と同じ。これから指揮者を立てる入力。
入口を短い名前の /bb-writ で呼ぶと、会話記録の行は /bb-writ:bb-writ になるが、prompt は打ったままの /bb-writ で届く）、印が無い、
判定できない入力（JSON でない、transcript_path が無い・読めない、行の形が違う）。prompt が文字列でないときは、入口をスラッシュで
呼ぶ形ではないと見る。どれも何も出さずに 0 で終わる。python3 を動かせないとき・このスクリプトを読めないときは、hooks.json の
コマンドが何も出さずに 0 で終わる。標準ライブラリだけ。
"""
import json
import sys
from pathlib import Path

HINT = ("bb-writ: このセッションでは入口 bb-writ を呼んでいる。入口が立てた指揮者（背景の子）がいるなら、次のとおりにする。"
        "(1) 指揮者の質問・報告を利用者に伝えるときは、返事は指揮者に直接打つよう添える（動いている間は画面下の一覧で ↓ を 2 回押して"
        " Enter、終わった後は /tasks から指揮者を開いて、入力欄に打つ）。"
        "(2) 利用者の入力が、指揮者の質問・報告への返事か、その仕事の続きの依頼のときは、自分では進めず（ファイルも読まず、書き換えず）、"
        "指揮者にも渡さずに、同じ直接の返事のしかたを利用者に伝える（指揮者は、親を通した返事を、本人の選択と確かめられずに断ることがある）。"
        "別の用件のときと、指揮者がいないときは、そのまま自分で進める。")
ENTRY_SKILLS = {"bb-writ:bb-writ", "bb-writ"}  # 入口だけ（本体 bb-writ:conduct は数えない）
# 探索の入口 unknowns も別の子として動く（#58）。印は入口と別に持つ（素の push の守りの「入口を呼んだセッション」に数えない）
HINT_UNKNOWNS = ("bb-writ: このセッションでは探索の入口 unknowns を呼んでいる。unknowns が立てた子（背景の子）がいるなら、次のとおりにする。"
                 "(1) 子の質問・報告を利用者に伝えるときは、返事は子に直接打つよう添える（動いている間は画面下の一覧で ↓ を 2 回押して"
                 " Enter、終わった後は /tasks から子を開いて、入力欄に打つ）。"
                 "(2) 利用者の入力が、子の質問・報告への返事か、その探索の続きの依頼のときは、自分では進めず、子にも渡さずに、"
                 "同じ直接の返事のしかたを利用者に伝える。別の用件のときと、子がいないとき（子がモデルのエラーで失敗し、本体 explore を"
                 "名前で呼んで、このセッションで探索を続けているときを含む）は、そのまま自分で進める。")
UNKNOWNS_NAMES = ("/bb-writ:unknowns", "/unknowns")
SKIP_NAMES = UNKNOWNS_NAMES + ("/bb-writ:explore",)  # この入力そのものには足さない（入口を呼ぶ入力と、失敗の後に本体を呼ぶ回避）


def starts_with(text: str, names) -> bool:
    """文が、どれかの名前で始まり、名前の直後が空白の類か文の終わりか（先頭の空白は削らない。入口の比べ方と同じ）。"""
    return isinstance(text, str) and any(text.startswith(n) and (len(text) == len(n) or text[len(n)].isspace()) for n in names)


def used_unknowns(path: str, push_guard) -> bool:
    """会話記録に、探索の入口 unknowns を呼んだ印があるか。数えるのは、Skill ツールの bb-writ:unknowns の呼び出し、利用者の行の
    スラッシュの記録、利用者の行と入力を受け付けた行のうち、名前で始まる素の文（fork するスキルをスラッシュで呼ぶと、この形で残る）。
    短い名前は、素の文と受け付けた行の /unknowns だけを数え、Skill ツールの短い名前 unknowns とスラッシュの記録の /unknowns は数えない
    （どれも、ほかのプラグインの unknowns とは見分けない）。試験は、名前の境目（/bb-writ:unknownsx）・文の途中の名前・先頭の空白・
    enqueue でない行・壊れた行・深い入れ子の行（json.loads が RecursionError を上げる）の扱いを見ない。"""
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if "unknowns" not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:  # 形の違う行は飛ばし、後ろの行を読み続ける（used_entry と同じ）
                continue
            if not isinstance(d, dict):
                continue
            if d.get("type") == "queue-operation" and d.get("operation") == "enqueue" and starts_with(d.get("content"), UNKNOWNS_NAMES):
                return True
            m = d.get("message") if isinstance(d.get("message"), dict) else {}
            content = m.get("content")
            blocks = content if isinstance(content, list) else [content]
            for b in blocks:
                if d.get("type") == "assistant" and isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Skill" \
                        and isinstance(b.get("input"), dict) and b["input"].get("skill") == "bb-writ:unknowns":
                    return True
                text = b.get("text") if isinstance(b, dict) and b.get("type") == "text" else b
                if d.get("type") == "user" and isinstance(text, str) and (
                        starts_with(text, UNKNOWNS_NAMES) or push_guard.slash_name(text) == "/bb-writ:unknowns"):
                    return True
    return False


def main() -> int:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))  # 同じ置き場の push_guard（PYTHONSAFEPATH でも読む）
        import push_guard
        d = json.load(sys.stdin)
        prompt = d.get("prompt")
        calls_entry = isinstance(prompt, str) and (push_guard.slash_line(prompt) or starts_with(prompt, SKIP_NAMES))  # 入口・本体を呼ぶ入力
        if d.get("agent_id") or calls_entry:
            return 0
        hints = ([HINT] if push_guard.used_entry(d["transcript_path"], ENTRY_SKILLS, {push_guard.ENTRY_NAME}) else []) + (
            [HINT_UNKNOWNS] if used_unknowns(d["transcript_path"], push_guard) else [])
        if not hints:
            return 0
    except Exception:  # 判定できなければ何も足さない（入力を止めない）
        return 0
    # ensure_ascii（既定）: 出力の符号化が日本語を扱えない環境でも、書き出しで落ちて案内が消えないように
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "\n".join(hints)}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
