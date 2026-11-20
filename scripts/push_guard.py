#!/usr/bin/env python3
"""Claude Code の PreToolUse の hook（hooks/hooks.json）。入口 bb-writ か本体 conduct を呼んだセッションで、関門（work.py op run）を
通さずに外へ出す Bash のコマンド（git push・gh pr create・gh pr merge・gh release create）を拒否する。

入口も本体も呼ばないセッション（bb-writ を使っていない仕事）と、判定できない入力（会話記録が無い・読めない・形が違う）は通す。
hooks.json は Bash のすべての呼び出しでこれを起こし、外へ出すコマンドかはここで見る（Claude Code の if の欄は使わない。Grok Build は
if の欄のある handler を飛ばして読み込みのエラーを出す。Claude Code の文書も、if は $() などを含む呼び出しでは型によらず hook を
起こすと書き、判定はここに残る。Grok Build の呼び出しは tool_name が run_terminal_command で、会話記録の形も違うので、ここでは通す）。
op run の中の git push は、op run が Bash を通さずに起こすので、ここには来ない。このスクリプトを読めないときは、hooks.json の
コマンドが 0 で終わる（python3 が開けずに 2 で終わると、Claude Code は Bash を全部拒否する）。標準ライブラリだけ。

コマンドの読み方は、普段の書き方（連結、複数行の引用符、"$(cat <<'EOF' …)" の中の heredoc、前置きの代入と包むコマンド）を
bash と同じに読む所までにする。それより珍しい書き方（別の語に包む sh -c・eval・xargs、変数に入れたコマンド名、別名・関数）は
読まない（AGENTS.md の規則の表に、守らない範囲として書く）。
"""
import json
import sys

# 入口を呼んだ印。Skill ツールの呼び出しと、利用者がスラッシュで呼んだときの記録（2026-11-18、Claude Code 2.1.327 で確かめた形）。
# 入口は fork するスキルで、スラッシュで呼ぶと、記録は <command-name> の形にならず、本文の先頭が「/名前 引数」のままの行になる
# （2026-11-20、2.1.328。対話でも非対話でも）。指揮者（fork した子）の Bash でも、hook に届く会話記録は親のもの。
# 非対話の実行では、その利用者の行は指揮者が終わるまで書かれず、指揮者の Bash の時点で在るのは、入力を受け付けた行
# （type が queue-operation、content が入力の文）だけだった（同日）。この行も印に数える。
# 短い名前 /bb-writ で呼んでも入口は fork し、入力を受け付けた行の content は打ったままの「/bb-writ 引数」だった（2026-11-20、
# 2.1.329、非対話）。指揮者が終わってから書かれる利用者の行は「/bb-writ:bb-writ 引数」に展開されていた。そこで、正式な名前と
# 短い名前の両方を、利用者の行にも入力を受け付けた行にも同じ比べ方（slash_line）で数える（利用者の行に短い名前が打ったままで
# 残る形は見ていないが、残っていれば数える）。先頭に空白・改行のある入力は、非対話では fork せず、普通の文として親のモデルに
# 届いた。対話では Claude Code が先頭の空白を削って入口を呼び、行は /bb-writ:bb-writ で始まる（同日、2.1.329）。どちらでも、
# 先頭に空白・改行のある行は数えない。
# 本体 bb-writ:conduct も印に数える（続きの引き受けや要約の後に、入口を通らずに本体を直に呼んだセッションも、進め方の全文を持つ）。
# 本体の短い名前 conduct は数えない（ほかのプラグインの同じ名前のスキルと区別できない）。本体の名前で始まる素の行も数えない
# （本体は fork しないので、スラッシュで呼ぶと <command-name> の形の行が先に書かれる）
ENTRY_SKILLS = {"bb-writ:bb-writ", "bb-writ", "bb-writ:conduct"}
ENTRY_NAME = "/bb-writ:bb-writ"
ENTRY_SLASHES = (ENTRY_NAME, "/bb-writ")  # 入口をスラッシュで呼ぶ名前（正式な名前と短い名前）。conductor_hint.py も使う
SLASH_NAMES = {ENTRY_NAME, "/bb-writ:conduct"}
SLASH_HEADS = ("<command-message>", "<command-name>")

# 区切り（コマンドの境目）。( ) と ` と $( は文脈の出入りとして別に扱う
SEPARATORS = set(";&|{}!\n")
KEYWORDS = {"then", "else", "do", "elif", "if", "while", "until"}
# 前に付いて、後ろのコマンドを起こす語と、その値を取る引数
WRAPPERS = {"env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
            "sudo": {"-u", "--user", "-g", "--group", "-C", "--close-from", "-h", "--host", "-p", "--prompt"},
            "nice": {"-n", "--adjustment"}, "time": {"-f", "--format", "-o", "--output"},
            "nohup": set(), "command": set(), "builtin": set(), "exec": {"-a"}, "setsid": set(), "stdbuf": {"-i", "-o", "-e"}}
TIMEOUT_WITH_VALUE = {"-s", "--signal", "-k", "--kill-after"}
GIT_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix", "--config-env",
                  "--attr-source"}
GH_WITH_VALUE = {"-R", "--repo"}
NOT_SENT = {"--dry-run", "-n", "--help", "-h"}  # git push で外へ何も出さない（試すだけ・使い方を見るだけ）
PUSH_WITH_VALUE = {"-o", "--push-option", "--receive-pack", "--exec", "--repo"}  # 値が NOT_SENT の語でも、値として読む
HELP = {"--help", "-h"}  # git の大域の引数の使い方を見るだけの指定。gh は --help だけ（gh の -h は副コマンドによって値になる）


def closing(command: str, i: int, open_: str, close: str) -> int:
    """command[i] が open_ のとき、対応する close の次の位置（無ければ末尾）。${…}・$((…)) に使う。"""
    depth = 0
    for j in range(i, len(command)):
        if command[j] == open_:
            depth += 1
        elif command[j] == close:
            depth -= 1
            if depth == 0:
                return j + 1
    return len(command)


def segments(command: str):
    return parse(command, 0, None)[0]


def parse(command: str, i: int, until):
    """command[i:] を区切りごとの語の並びに分け、(並びの一覧, 読み終えた位置) を返す。until が ")" か "`" なら、対応する
    閉じ（置き換えの終わり）で止まる。コマンドの置き換え（$( ) と `）の中は再帰で読んで並びに足し、外側では 1 つの語にする
    （二重引用符の中でも、引数の中でも、外側のコマンドを切らない）。一重引用符の中、${…}・$((…))、<<< の語、語の頭の # からの
    注釈、行の継続（\\ と改行）、heredoc（<<語）の本文の行は、コマンドとして読まない。手間は入力の長さに比例する
    （置き換えの入れ子が Python の再帰の上限を超えると例外になり、main が通す）。"""
    n = len(command)
    segs, words, word = [], [], []
    started, in_dq, depth, heredocs = False, False, 0, []  # depth: until が ")" のときの、中の ( の深さ

    def flush():
        nonlocal started
        if word or started:
            words.append("".join(word))
        word.clear()
        started = False

    def cut():
        nonlocal words
        flush()
        if words:
            segs.append(words)
        words = []

    while i < n:
        ch = command[i]
        if in_dq:
            if ch == "\\" and i + 1 < n:
                if command[i + 1] != "\n":
                    word.append(command[i + 1])
                i += 2
            elif ch == '"':
                in_dq, started = False, True
                i += 1
            elif command.startswith("$((", i) or command.startswith("${", i):
                j = closing(command, i + 1, command[i + 1], ")" if command[i + 1] == "(" else "}")
                word.append(command[i:j])
                i = j
            elif command.startswith("$(", i) or ch == "`":
                inner, i = parse(command, i + (2 if ch == "$" else 1), ")" if ch == "$" else "`")
                segs.extend(inner)
                word.append("$()")
            else:
                word.append(ch)
                i += 1
            continue
        if until == "`" and ch == "`" or until == ")" and ch == ")" and depth == 0:
            cut()
            return segs, i + 1
        if ch == "'":
            end = command.find("'", i + 1)
            end = n if end == -1 else end
            word.append(command[i + 1:end])
            started = True
            i = end + 1
        elif ch == '"':
            in_dq, started = True, True
            i += 1
        elif ch == "\\" and i + 1 < n:
            if command[i + 1] != "\n":
                word.append(command[i + 1])
            i += 2
        elif ch == "#" and not word and not started:  # 注釈は行の終わりまで
            end = command.find("\n", i)
            i = n if end == -1 else end
        elif command.startswith("<<<", i):
            word.append("<<<")
            i += 3
        elif command.startswith("<<", i):
            flush()
            strip_tabs = command.startswith("<<-", i)
            j = i + 2 + strip_tabs
            while j < n and command[j] in " \t":
                j += 1
            k = j
            while k < n and not command[k].isspace() and command[k] not in ";&|()<>":
                k += 1
            if k > j:
                heredocs.append((command[j:k].replace("'", "").replace('"', "").replace("\\", ""), strip_tabs))
            i = k
        elif command.startswith("$((", i) or command.startswith("${", i):
            j = closing(command, i + 1, command[i + 1], ")" if command[i + 1] == "(" else "}")
            word.append(command[i:j])
            i = j
        elif command.startswith("$(", i) or ch == "`":
            inner, i = parse(command, i + (2 if ch == "$" else 1), ")" if ch == "$" else "`")
            segs.extend(inner)
            word.append("$()")
        elif ch == "(":
            cut()
            depth += 1
            i += 1
        elif ch == ")":
            cut()
            depth = max(depth - 1, 0)
            i += 1
        elif ch == "\n" and heredocs:
            cut()
            for delim, strip_tabs in heredocs:  # 本文の行を、終わりの行まで読み飛ばす（<<- だけ行頭のタブを外す）
                start = i + 1
                while True:
                    end = command.find("\n", start)
                    line = command[start:n if end == -1 else end]
                    if (line.lstrip("\t") if strip_tabs else line) == delim or end == -1:
                        i = n if end == -1 else end
                        break
                    start = end + 1
            heredocs = []
            i += 1
        elif ch in "<>":  # 向け直しの演算子（>&・<&・>|・>>）の & と | は区切りでない。語の途中なら語を切る（2> の 2 は残す）
            if word and not "".join(word).isdigit():
                flush()
            word.append(ch)
            if i + 1 < n and command[i + 1] in "&|>":
                word.append(command[i + 1])
                i += 1
            i += 1
        elif ch in SEPARATORS:
            cut()
            i += 1
        elif ch.isspace():
            flush()
            i += 1
        else:
            word.append(ch)
            i += 1
    cut()
    return segs, n


def outward(command: str) -> bool:
    for w in segments(command):
        i, n = 0, len(w)
        while i < n:  # 前に付く語（制御の語、環境変数の代入、向け直し、包むコマンドとその引数）を読み飛ばす
            head = w[i]
            if head in KEYWORDS or ("=" in head and head.split("=", 1)[0].isidentifier()):
                i += 1
            elif redirect(head):  # 向け直し（2>/dev/null、> f）
                i += redirect(head)
            elif head in WRAPPERS:
                i += 1
                while i < n and w[i].startswith("-"):
                    i += 2 if w[i] in WRAPPERS[head] else 1
            elif head == "timeout":
                i += 1
                while i < n and w[i].startswith("-"):
                    i += 2 if w[i] in TIMEOUT_WITH_VALUE else 1
                i += 1  # 時間
            elif head == "dotenvx" and w[i + 1:i + 2] == ["run"]:  # dotenvx run …… -- <コマンド>
                i += 2
                while i < n and w[i] != "--":
                    i += 1
                i += 1
            else:
                break
        if i >= n:
            continue
        tool, i = w[i].rsplit("/", 1)[-1], i + 1
        if tool == "git":
            help_ = False
            while i < n and (w[i].startswith("-") or redirect(w[i])):
                help_ = help_ or w[i] in HELP
                i += 2 if w[i] in GIT_WITH_VALUE or redirect(w[i]) == 2 else 1
            if w[i:i + 1] == ["push"] and not help_ and not dry_or_help(w[i + 1:]):
                return True
        elif tool == "gh":
            sub = []  # フラグ（-R・--repo は値も）を除いた語の並びの先頭 2 つ
            while i < n and len(sub) < 2:
                if w[i].startswith("-"):
                    i += 2 if w[i] in GH_WITH_VALUE else 1
                else:
                    sub.append(w[i])
                    i += 1
            if sub in (["pr", "create"], ["pr", "merge"], ["release", "create"]) and "--help" not in w[i:]:
                return True
    return False


def redirect(word: str) -> int:
    """向け直しの語なら、読み飛ばす語の数（演算子だけの語は次の語が行き先で 2、行き先が付いていれば 1）。ほかは 0。"""
    rest = word.lstrip("0123456789")
    if rest[:1] not in ("<", ">"):
        return 0
    return 2 if rest in ("<", ">", ">>", ">&", "<&", ">|") else 1


def dry_or_help(args: list) -> bool:
    """git push の引数に、外へ何も出さない指定があるか（値を取る引数の値は数えない）。"""
    k = 0
    while k < len(args):
        if args[k] in PUSH_WITH_VALUE:
            k += 2
            continue
        if args[k] in NOT_SENT:
            return True
        k += 1
    return False


REASON = ("bb-writ: 外に出す操作は、playbook-deliver のとおり work.py op run で実行する（担当・作業の記録・返す前の判定の"
          "監査役を揃えてから、`work.py op run --key push:<先>@<commit> ... -- git push ...`）。素の git push・gh では出さない。"
          "work.py を実行できなければ、出さずにエラーを利用者に返す。利用者が自分で出すなら、利用者がターミナルで実行する。")


def used_entry(path: str, skills=ENTRY_SKILLS, slashes=SLASH_NAMES) -> bool:
    """会話記録に、数える名前（skills は Skill ツールの名前、slashes はスラッシュの記録の名前）で呼んだ印があるか。"""
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if "bb-writ" not in line:
                continue
            try:
                if entry_in(json.loads(line), skills, slashes):
                    return True
            except Exception:  # 形の違う行は飛ばし、後ろの行を読み続ける
                continue
    return False


def slash_name(text: str):
    """利用者の行の本文の先頭がスラッシュの記録なら、その名前（<command-name> の中身）。"""
    if not text.lstrip().startswith(SLASH_HEADS):
        return None
    start = text.find("<command-name>")
    end = text.find("</command-name>", start)
    return text[start + len("<command-name>"):end].strip() if start != -1 and end != -1 else None


def slash_line(text: str) -> bool:
    """文が、入口をスラッシュで呼ぶ形か。入口の 2 つの名前（/bb-writ:bb-writ と短い名前 /bb-writ）のどちらかで始まり、名前の直後が
    空白の類（空白・タブ・改行など）か文の終わり。先頭の空白・改行は削らずに比べる（先頭に空白のある入力は、非対話では入口を呼ばない）。
    利用者の行・入力を受け付けた行の本文（push_guard）と、hook に届いた入力（conductor_hint.py）の両方に、この 1 つの比べ方を使う。"""
    return any(text == name or text.startswith(name) and text[len(name)].isspace() for name in ENTRY_SLASHES)


def entry_in(row: dict, skills=ENTRY_SKILLS, slashes=SLASH_NAMES) -> bool:
    """1 行が入口（か本体）を呼んだ印か。Skill ツールの呼び出しか、利用者の行の先頭のスラッシュの記録の名前が入口・本体のものか、
    利用者の行・入力を受け付けた行の文が入口の名前（正式な名前か短い名前。slash_line）で始まるもの。書き手の本文や道具の結果、
    ほかのスラッシュの引数に写った印の文字列、文の途中に書かれた名前、先頭に空白・改行のある行、本体の名前で始まる素の行は数えない。
    skills・slashes は数える名前（既定は入口と本体。conductor_hint.py は入口だけを渡す）。slashes は <command-name> の記録の名前にだけ
    効き、行の先頭の素の名前（slash_line）は、いつも入口の 2 つの名前で比べる。"""
    if row.get("type") == "queue-operation":  # 入力を受け付けた行
        return slash_line(row["content"])  # 文字列でない形は、呼び出し側が飛ばす
    message = row.get("message") or {}
    content, by_user = message.get("content"), message.get("role") == "user"
    if isinstance(content, str):
        return by_user and (slash_name(content) in slashes or slash_line(content))
    for b in content if isinstance(content, list) else []:
        if b.get("type") == "tool_use" and b.get("name") == "Skill" and (b.get("input") or {}).get("skill") in skills:
            return True
        if by_user and b.get("type") == "text" and (slash_name(b.get("text") or "") in slashes or slash_line(b.get("text") or "")):
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
