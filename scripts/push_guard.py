#!/usr/bin/env python3
"""Claude Code の PreToolUse の hook（hooks/hooks.json）。入口 bb-writ を呼んだセッションで、関門（work.py op run）を
通さずに外へ出す Bash のコマンド（git push・gh pr create・gh pr merge・gh release create）を拒否する。

入口を呼ばないセッション（bb-writ を使っていない仕事）と、判定できない入力（会話記録が無い・読めない・形が違う）は通す。
hooks.json は Bash のすべての呼び出しでこれを起こし、外へ出すコマンドかはここで見る（Claude Code の if の欄は使わない。Grok Build は
if の欄のある handler を飛ばして読み込みのエラーを出す。Grok Build の呼び出しは tool_name が run_terminal_command で、会話記録の形も
違うので、ここでは通す）。op run の中の git push は、op run が Bash を通さずに起こすので、ここには来ない。このスクリプトを
読めないときは、hooks.json のコマンドが 0 で終わる（python3 が開けずに 2 で終わると、Claude Code は Bash を全部拒否する）。標準ライブラリだけ。
"""
import json
import sys

# 入口を呼んだ印。Skill ツールの呼び出しと、利用者がスラッシュで呼んだときの記録（2026-11-18、Claude Code 2.1.327 で確かめた形）
ENTRY_SKILLS = {"bb-writ:bb-writ", "bb-writ"}
ENTRY_COMMAND = "<command-name>/bb-writ:bb-writ</command-name>"
SLASH_HEADS = ("<command-message>", "<command-name>")
# 外へ出すコマンドの見分けは、正規表現でなく、引用符を見ながら区切りで切り、語に分けて先頭から読む（入力の長さに比例する手間。
# 正規表現の繰り返しの入れ子は、git の大域の引数が長く続く入力で 10 秒を超えた）。
SEPARATORS = set(";&|(){}!`\n")
KEYWORDS = {"then", "else", "do", "elif", "if", "while", "until"}
PREFIX = {"time", "nohup", "command", "builtin", "exec"}
GIT_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix", "--config-env"}


def segments(command: str):
    """区切り（; & | ( ) { } ! ` 改行）で切った断片を、語の並びで返す。一重引用符の中は区切らない。二重引用符の中は、
    コマンドの置き換え（` と $( ）だけを区切る。行の継続（\\ と改行）は消し、語の頭の # から行の終わりまで（注釈）と、
    heredoc（<<語）の本文の行は読み飛ばす（ファイルや文書を書く本文はコマンドでない）。"""
    words, word, quote, i, heredocs = [], [], None, 0, []
    def flush():
        if word:
            words.append("".join(word)); word.clear()
    while i < len(command):
        ch = command[i]
        if quote == "'":
            word.append(ch) if ch != "'" else None; quote = None if ch == "'" else quote
        elif quote == '"':
            if ch == "\\" and i + 1 < len(command):
                if command[i + 1] != "\n":  # 行の継続は消える
                    word.append(command[i + 1])
                i += 1
            elif ch == '"':
                quote = None
            elif ch == "`" or command.startswith("$(", i):
                flush(); yield words; words = []; i += 1 if ch == "`" else 2; quote = None; continue
            else:
                word.append(ch)
        elif ch == "\\" and i + 1 < len(command):
            if command[i + 1] != "\n":  # 行の継続は消える
                word.append(command[i + 1])
            i += 1
        elif ch == "#" and not word:  # 注釈は行の終わりまで
            end = command.find("\n", i)
            i = len(command) if end == -1 else end
            continue
        elif ch in "'\"":
            quote = ch
        elif command.startswith("<<", i) and not command.startswith("<<<", i):
            flush()
            j = i + 2 + (command[i + 2:i + 3] == "-")
            while j < len(command) and command[j] in " \t":
                j += 1
            k = j
            while k < len(command) and not command[k].isspace() and command[k] not in ";&|()<>":
                k += 1
            if k > j:
                heredocs.append(command[j:k].strip("'\"\\"))
            i = k
            continue
        elif ch == "\n" and heredocs:
            flush(); yield words; words = []
            for delim in heredocs:  # 本文の行を、終わりの行まで読み飛ばす
                end = command.find("\n", i + 1)
                while end != -1 and command[i + 1:end].strip() != delim:
                    i, end = end, command.find("\n", end + 1)
                i = len(command) if end == -1 else end
            heredocs = []
        elif ch in SEPARATORS:
            flush(); yield words; words = []
        elif ch.isspace():
            flush()
        else:
            word.append(ch)
        i += 1
    flush(); yield words


def outward(command: str) -> bool:
    for w in segments(command):
        i, n = 0, len(w)
        while i < n:  # 前に付く語（制御の語、環境変数の代入、env・timeout・sudo などの包むコマンドとその引数）を読み飛ばす
            head = w[i]
            if head in KEYWORDS or head in PREFIX or ("=" in head and head.split("=", 1)[0].isidentifier()):
                i += 1
            elif head in ("env", "sudo"):
                i += 1
                while i < n and w[i].startswith("-"):
                    i += 2 if w[i] in ("-u", "-C", "-g") else 1
            elif head == "dotenvx" and w[i + 1:i + 2] == ["run"]:  # dotenvx run …… -- <コマンド>
                i += 2
                while i < n and w[i] != "--":
                    i += 1
                i += 1
            elif head == "timeout":
                i += 1
                while i < n and w[i].startswith("-"):
                    i += 2 if w[i] in ("-s", "-k") else 1
                i += 1  # 時間
            else:
                break
        if i >= n:
            continue
        tool, i = w[i].rsplit("/", 1)[-1], i + 1
        if tool == "git":
            while i < n and w[i].startswith("-"):
                i += 2 if w[i] in GIT_WITH_VALUE else 1
            if w[i:i + 1] == ["push"]:
                return True
        elif tool == "gh":
            words = []  # フラグ（-R・--repo は値も）を除いた語の並びの先頭 2 つ
            while i < n and len(words) < 2:
                if w[i].startswith("-"):
                    i += 2 if w[i] in ("-R", "--repo") else 1
                else:
                    words.append(w[i]); i += 1
            if words in (["pr", "create"], ["pr", "merge"], ["release", "create"]):
                return True
    return False


REASON = ("bb-writ: 外に出す操作は、playbook-deliver のとおり work.py op run で実行する（担当・作業の記録・返す前の判定の"
          "監査役を揃えてから、`work.py op run --key push:<先>@<commit> ... -- git push ...`）。素の git push・gh では出さない。"
          "work.py を実行できなければ、出さずにエラーを利用者に返す。利用者が自分で出すなら、利用者がターミナルで実行する。")


def used_entry(path: str) -> bool:
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if "bb-writ" not in line:
                continue
            try:
                if entry_in(json.loads(line)):
                    return True
            except Exception:  # 形の違う行は飛ばし、後ろの行を読み続ける
                continue
    return False


def entry_in(row: dict) -> bool:
    """1 行が入口を呼んだ印か。Skill ツールの呼び出しか、利用者の行の本文の先頭がスラッシュの印（書き手の本文や道具の結果に
    写った印の文字列は数えない）。"""
    message = row.get("message") or {}
    content, by_user = message.get("content"), message.get("role") == "user"
    if isinstance(content, str):
        return by_user and content.lstrip().startswith(SLASH_HEADS) and ENTRY_COMMAND in content
    for b in content if isinstance(content, list) else []:
        if b.get("type") == "tool_use" and b.get("name") == "Skill" and (b.get("input") or {}).get("skill") in ENTRY_SKILLS:
            return True
        text = b.get("text") or ""
        if by_user and b.get("type") == "text" and text.lstrip().startswith(SLASH_HEADS) and ENTRY_COMMAND in text:
            return True
    return False


def main() -> int:
    try:
        d = json.load(sys.stdin)
        command = (d.get("tool_input") or {}).get("command") or ""
        if d.get("tool_name") != "Bash" or not outward(command) or not used_entry(d["transcript_path"]):
            return 0
    except Exception:  # 判定できなければ通す（ほかの作業を止めない）
        return 0
    # ensure_ascii（既定）: 出力の符号化が日本語を扱えない環境でも、書き出しで落ちて拒否が消えないように
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": REASON}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
