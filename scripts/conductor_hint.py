#!/usr/bin/env python3
"""Claude Code の UserPromptSubmit の hook（hooks/hooks.json）。入口 bb-writ を呼んだセッションで、この hook が発火するたびに
（利用者の入力のほか、指揮者の終わりの知らせ・子からの伝言でも発火する）、入口が立てた指揮者（背景の子）への返事と、その仕事の
続きは、親が自分で進めずに SendMessage で同じ指揮者に渡すよう、文脈に案内を足す（hookSpecificOutput.additionalContext）。

案内だけで、入力は止めない（AGENTS.md: 必須の機能を hook に置かない。hook が動かないホストでも、入口の description が同じことを
求める）。指揮者がいるか、入力が指揮者への返事かは、ここでは決めない（会話記録から確かめられない。親のモデルが案内の条件で決める）。

数える印は入口だけ（Skill ツールの bb-writ:bb-writ と短い名前 bb-writ、利用者の行の先頭のスラッシュの記録の名前
/bb-writ:bb-writ、利用者の行・入力を受け付けた行の文が /bb-writ:bb-writ で始まるもの）。読み方は push_guard.py の entry_in を、
入口だけの名前で使う（判定を写して 2 つ持たない）。本体 bb-writ:conduct だけを呼んだセッションは数えない（入口を通らず、
指揮者がいない）。
足さない場合: 入力に agent_id がある（親でなく子への入力。指揮者の依頼文に混ざらないように）、入力がスラッシュのコマンドの形
（先頭が / で、/ と空白以外の文字が 1 つ以上続き、その次が空白か終わり。入口の名前そのものでは比べない。入口を短い名前の
/bb-writ で呼ぶと、会話記録の行は /bb-writ:bb-writ になるが、prompt は打ったままの /bb-writ で届く）、印が無い、判定できない
入力（JSON でない、transcript_path が無い・読めない、行の形が違う）。prompt が文字列でないときは、スラッシュのコマンドではないと
見る。どれも何も出さずに 0 で終わる。このスクリプトを読めないときは、hooks.json のコマンドが 0 で終わる。標準ライブラリだけ。
"""
import json
import re
import sys
from pathlib import Path

HINT = ("bb-writ: このセッションでは入口 bb-writ を呼んでいる。入口が立てた指揮者（背景の子）がいるなら、いまの利用者の入力が、"
        "指揮者の質問・報告への返事か、その仕事の続きの依頼のときは、自分では進めずに（ファイルも読まず、書き換えず）、"
        "SendMessage で同じ指揮者に渡す（終わって止まっている指揮者も、SendMessage で続きから再開できる）。"
        "別の用件のときと、指揮者がいないときは、そのまま自分で進める。")
ENTRY_SKILLS = {"bb-writ:bb-writ", "bb-writ"}  # 入口だけ（本体 bb-writ:conduct は数えない）
SLASH_COMMAND = re.compile(r"/[^\s/]+(\s|$)")  # スラッシュのコマンドの形（re.match で先頭から見る。/tmp/x は最初の語に / が 2 つ）


def main() -> int:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))  # 同じ置き場の push_guard（PYTHONSAFEPATH でも読む）
        import push_guard
        d = json.load(sys.stdin)
        prompt = d.get("prompt")
        if (d.get("agent_id") or isinstance(prompt, str) and SLASH_COMMAND.match(prompt)
                or not push_guard.used_entry(d["transcript_path"], ENTRY_SKILLS, {push_guard.ENTRY_NAME})):
            return 0
    except Exception:  # 判定できなければ何も足さない（入力を止めない）
        return 0
    # ensure_ascii（既定）: 出力の符号化が日本語を扱えない環境でも、書き出しで落ちて案内が消えないように
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": HINT}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
