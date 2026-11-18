#!/usr/bin/env python3
"""Claude Code の PreToolUse の hook（hooks/hooks.json）。入口 bb-writ を呼んだセッションで、関門（work.py op run）を
通さずに外へ出す Bash のコマンド（git push・gh pr create・gh pr merge・gh release create）を拒否する。

入口を呼ばないセッション（bb-writ を使っていない仕事）と、判定できない入力（会話記録が無い・読めない・形が違う）は通す。
hooks.json は Bash のすべての呼び出しでこれを起こし、外へ出すコマンドかはここで見る（Claude Code の if の欄は使わない。Grok Build は
if の欄のある handler を飛ばして読み込みのエラーを出す。Grok Build の呼び出しは tool_name が run_terminal_command で、会話記録の形も
違うので、ここでは通す）。op run の中の git push は、op run が Bash を通さずに起こすので、ここには来ない。標準ライブラリだけ。
"""
import json
import re
import sys

# 入口を呼んだ印。Skill ツールの呼び出しと、利用者がスラッシュで呼んだときの記録（2026-11-18、Claude Code 2.1.327 で確かめた形）
ENTRY_SKILLS = {"bb-writ:bb-writ", "bb-writ"}
ENTRY_COMMAND = "<command-name>/bb-writ:bb-writ</command-name>"
# コマンドの区切り（行頭・;・&&・||・|・括弧）の直後の、環境変数の代入と git の大域の引数を挟んだ外へ出すコマンド
SEP = r"(?:^|[;&|(\n]|\bthen\b|\bdo\b)\s*"
ENV = r"(?:\w+=\S*\s+)*(?:env\s+(?:\w+=\S*\s+)*)?"
OUTWARD = re.compile(SEP + ENV + r"(?:git(?:\s+(?:-C|-c|--git-dir|--work-tree)\s+\S+|\s+--?[\w-]+(?:=\S+)?)*\s+push\b"
                     r"|gh\s+(?:pr\s+(?:create|merge)|release\s+create)\b)")
REASON = ("bb-writ: 外に出す操作は、playbook-deliver のとおり work.py op run で実行する（担当・作業の記録・返す前の判定の"
          "監査役を揃えてから、`work.py op run --key push:<先>@<commit> ... -- git push ...`）。素の git push・gh では出さない。"
          "work.py を実行できなければ、出さずにエラーを利用者に返す。利用者が自分で出すなら、利用者がターミナルで実行する。")


def used_entry(path: str) -> bool:
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if "bb-writ" not in line:
                continue
            try:
                content = (json.loads(line).get("message") or {}).get("content")
            except (ValueError, AttributeError):
                continue
            if isinstance(content, str):
                if ENTRY_COMMAND in content:
                    return True
                continue
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use" and b.get("name") == "Skill" and (b.get("input") or {}).get("skill") in ENTRY_SKILLS:
                    return True
                if b.get("type") == "text" and ENTRY_COMMAND in (b.get("text") or ""):
                    return True
    return False


def main() -> int:
    try:
        d = json.load(sys.stdin)
        command = (d.get("tool_input") or {}).get("command") or ""
        if d.get("tool_name") != "Bash" or not OUTWARD.search(command) or not used_entry(d["transcript_path"]):
            return 0
    except Exception:  # 判定できなければ通す（ほかの作業を止めない）
        return 0
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": REASON}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
