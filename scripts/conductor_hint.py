#!/usr/bin/env python3
"""Claude Code の UserPromptSubmit の hook（hooks/hooks.json）。入口 bb-writ を呼んだセッションで、利用者の入力のたびに、
入口が立てた指揮者（背景の子）への返事と、その仕事の続きは、親が自分で進めずに SendMessage で同じ指揮者に渡すよう、
文脈に案内を足す（hookSpecificOutput.additionalContext）。

案内だけで、入力は止めない（AGENTS.md: 必須の機能を hook に置かない。hook が動かないホストでも、入口の description が同じことを
求める）。指揮者がいるか、入力が指揮者への返事かは、ここでは決めない（会話記録から確かめられない。親のモデルが案内の条件で決める）。

数える印は入口だけ（Skill ツールの bb-writ:bb-writ と短い名前 bb-writ、利用者の行の先頭のスラッシュの記録の名前
/bb-writ:bb-writ、利用者の行・入力を受け付けた行の文が /bb-writ:bb-writ で始まるもの）。読み方は push_guard.py の entry_in を、
入口だけの名前で使う（判定を写して 2 つ持たない）。本体 bb-writ:conduct だけを呼んだセッションは数えない（入口を通らず、
指揮者がいない）。
足さない場合: 入力に agent_id がある（親でなく子への入力。指揮者の依頼文に混ざらないように）、入力が入口のスラッシュの呼び出し
そのもの（これから指揮者を立てる）、印が無い、判定できない入力（JSON でない、欄が無い、会話記録を読めない、行の形が違う）。
どれも何も出さずに 0 で終わる。このスクリプトを読めないときは、hooks.json のコマンドが 0 で終わる。標準ライブラリだけ。
"""
import json
import sys
from pathlib import Path

HINT = ("bb-writ: このセッションでは入口 bb-writ を呼んでいる。入口が立てた指揮者（背景の子）がいるなら、いまの利用者の入力が、"
        "指揮者の質問・報告への返事か、その仕事の続きの依頼のときは、自分では進めずに（ファイルも読まず、書き換えず）、"
        "SendMessage で同じ指揮者に渡す（終わって止まっている指揮者も、SendMessage で続きから再開できる）。"
        "別の用件のときと、指揮者がいないときは、そのまま自分で進める。")
ENTRY_SKILLS = {"bb-writ:bb-writ", "bb-writ"}  # 入口だけ（本体 bb-writ:conduct は数えない）


def main() -> int:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))  # 同じ置き場の push_guard（PYTHONSAFEPATH でも読む）
        import push_guard
        d = json.load(sys.stdin)
        prompt = d.get("prompt")
        if (d.get("agent_id") or isinstance(prompt, str) and push_guard.slash_line(prompt)
                or not push_guard.used_entry(d["transcript_path"], ENTRY_SKILLS, {push_guard.ENTRY_NAME})):
            return 0
    except Exception:  # 判定できなければ何も足さない（入力を止めない）
        return 0
    # ensure_ascii（既定）: 出力の符号化が日本語を扱えない環境でも、書き出しで落ちて案内が消えないように
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": HINT}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
