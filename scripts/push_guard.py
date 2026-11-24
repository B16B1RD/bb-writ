#!/usr/bin/env python3
"""Claude Code の PreToolUse の hook（hooks/hooks.json）。入口 bb-writ か本体 conduct を呼んだセッションで、関門（work.py op run）を
通さずに外へ出す Bash のコマンド（git push・gh pr create・gh pr merge・gh release create）を拒否する。

入口も本体も呼ばないセッション（bb-writ を使っていない仕事）と、判定できない入力は通す。親の会話記録が無い・読めないときも、
子の Bash では子の会話記録を見る（entry_child）。親の会話記録の印でも子の会話記録でも決まらなければ通す。
hooks.json は Bash のすべての呼び出しでこれを起こし、外へ出すコマンドかはここで見る（Claude Code の if の欄は使わない。Grok Build は
if の欄のある handler を飛ばして読み込みのエラーを出す。Claude Code の文書も、if は $() などを含む呼び出しでは型によらず hook を
起こすと書き、判定はここに残る。Grok Build の呼び出しは tool_name が run_terminal_command で、会話記録の形も違うので、ここでは通す）。
op run の中の git push は、op run が Bash を通さずに起こすので、ここには来ない。このスクリプトを読めないときは、hooks.json の
コマンドが 0 で終わる（python3 が開けずに 2 で終わると、Claude Code は Bash を全部拒否する）。標準ライブラリだけ。

コマンドの読み方は、普段の書き方（連結、複数行の引用符、"$(cat <<'EOF' …)" の中の heredoc、前置きの代入と包むコマンド）を
bash と同じに読む所までにする。それより珍しい書き方（別の語に包む sh -c・eval・xargs、変数に入れたコマンド名、別名・関数）は
読まない（下の「守らない範囲」）。

入口は別の子（指揮者）として動くので、親の会話記録の印だけでは、次の 2 つの場面で指揮者の素の push が通った（2026-11-20、
Claude Code 2.1.329）。非対話で --input-format stream-json に本文をブロックの並びで渡して入口を呼ぶと、入力を受け付けた行に本文が
無い。対話で、指揮者が動いている間に親で /clear を打つと、hook に届く transcript_path と session_id が新しいセッションのものになる。
そこで、子の Bash の呼び出し（hook の入力に agent_id が在る。親の Bash には無い。agent_type は general-purpose で、入口の子と
見分けられない）では、子の会話記録も見る（entry_child）。入口の子の会話記録の先頭の行は、入口のスキルの本文を読み込んだ行
（content が「Base directory for this skill: <プラグインの置き場>/skills/bb-writ」で始まる）。孫（指揮者が Agent ツールで立てた子）は、
同じ置き場の meta.json の parentAgentId でたどる。cache に写すプラグインの置き場は更新のたびに変わるので、先頭の行の置き場は、
hook の置き場と同じ場所か、同じ親の下の別の置き場（版の兄弟）なら同じ入口と数える（same_entry）。

守らない範囲（守るのは、上の読み方と前置きで書いた普段の書き方だけ。判定できなければ通し、このスクリプトを読めなければ hook も 0 で終わる）:
- 通す書き方: 別の語に包む（sh -c '…'・eval・xargs・trap・coproc・find -exec・| sh・env -S "git push"）、前置きの一覧に無い包むコマンド
  （flock・ionice・パスで書いた /usr/bin/env）、置き換えや変数で作ったコマンド名（$(echo git) push・x=git; $x push）、別名・関数・
  スクリプト・git -c alias.…・{git,push}・git.exe、gh の組み込みの別名（gh pr new）、外へ出すほかの道具（gh api・git subtree・
  git lfs の push・gh repo create --push）、まれな bash の構文（heredoc の終わりの EOF)"、語に続けた <<<、$'…'、((… << …))、git と
  副コマンドの間の &>、二重引用符の中の $( ) の case の分岐、heredoc の本文や ${…}・$((…)) の中の展開）、Python の再帰の上限を超える
  置き換えの入れ子。
- 誤って止める書き方: 配列の代入（a=(git push)）、関数の定義（f() { git push; }）、env -S x git push、git --version push・
  git --exec-path /x push・git --super-prefix p push・command -v git push、gh pr create -h、副コマンドの前の gh --help、まとめた
  短い引数の dry-run（git push -nq）。
- 指揮者を見分けずに通す形: プラグインの置き場の道筋に改行が在る、子の会話記録の名前のものが FIFO（hook が時間の上限で終わる）、
  孫の meta.json が無い、hook の置き場と親の違う置き場で始まった指揮者（marketplace や入れ方を替えた後）、入口の skills/bb-writ 自体が
  symlink の置き場で、別の版の置き場で始まった指揮者、前の版の hook を使い続けるセッションで、その版の置き場が片付けで消えた後。
- 指揮者と誤って数える形: 入口と関係のない子の先頭の行が、このプラグインの入口の置き場の文で始まる、過去の指揮者と同じ agent_id が
  関係のない子に付く、hook の置き場と同じ親の下の別のディレクトリの skills/bb-writ を読み込んだ子（same_entry）、先頭の行の道筋が
  空か相対で、hook の作業ディレクトリから解くと入口の置き場か、その隣の skills/bb-writ になる。
- 入口を呼ばないセッションは止めない。書き手の本文や道具の結果、ほかのスラッシュの引数に写った印は、入口を呼んだ印と数えない。
  Codex と Grok Build では止めない（入口の文だけ）。macOS の実物は確かめていない。
"""
import glob
import json
import os
import re
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
ENTRY_SLASHES = (ENTRY_NAME, "/bb-writ")
SLASH_NAMES = {ENTRY_NAME, "/bb-writ:conduct"}
SLASH_HEADS = ("<command-message>", "<command-name>")

# 入口の子（指揮者）とその子孫の見分け方（2026-11-20、2.1.329。非対話の 7 つの呼び方と対話の 8 つの場面で、指揮者の Bash の入力を見た）。
# 子の会話記録は <transcript_path の置き場>/<セッションの ID>/subagents/agent-<agent_id>.jsonl。入口の子の先頭の行は
# {"type": "user", "isMeta": true, "message": {"role": "user", "content": "Base directory for this skill: <置き場>/skills/bb-writ\n\n…"}}。
# /clear の後は、新しいセッションの置き場にも同じ名前のファイルができる（続きの行だけ）。前のセッションの置き場のものは残り、
# 先頭は入口の行のままなので、全部のセッションの置き場から探す。孫の先頭の行は入口の行でなく、同じ置き場の
# agent-<孫の id>.meta.json の parentAgentId が指揮者の agent_id（指揮者の meta.json に parentAgentId は無い）。
GLOB_SAFE_AGENT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
# 先頭の行を読む長さの上限。入口の行の content は、入口の本文の後ろに「ARGUMENTS: <依頼文>」を含むので、依頼が長いと行も長い
# （約 26 万字を超える依頼で、262,144 字の上限では行が切れて読めなかった）。上限を超える行は、JSON として読めず、当たらなかったものとして通す
FIRST_ROW_LIMIT = 16777216
BASE_HEAD = "Base directory for this skill: "
ENTRY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills", "bb-writ")
ANCESTOR_LIMIT = 8

SEPARATORS = set(";&|{}!\n")
KEYWORDS = {"then", "else", "do", "elif", "if", "while", "until"}
WRAPPERS = {"env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
            "sudo": {"-u", "--user", "-g", "--group", "-C", "--close-from", "-h", "--host", "-p", "--prompt"},
            "nice": {"-n", "--adjustment"}, "time": {"-f", "--format", "-o", "--output"},
            "nohup": set(), "command": set(), "builtin": set(), "exec": {"-a"}, "setsid": set(), "stdbuf": {"-i", "-o", "-e"}}
TIMEOUT_WITH_VALUE = {"-s", "--signal", "-k", "--kill-after"}
GIT_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix", "--config-env",
                  "--attr-source"}
GH_WITH_VALUE = {"-R", "--repo"}
NOT_SENT = {"--dry-run", "-n", "--help", "-h"}
PUSH_WITH_VALUE = {"-o", "--push-option", "--receive-pack", "--exec", "--repo"}
HELP = {"--help", "-h"}  # git の大域の引数の使い方を見るだけの指定。gh は --help だけ（gh の -h は副コマンドによって値になる）


def closing(command: str, i: int, open_: str, close: str) -> int:
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
    started, in_dq, depth, heredocs = False, False, 0, []

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
        elif ch == "#" and not word and not started:
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
            for delim, strip_tabs in heredocs:
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
        while i < n:
            head = w[i]
            if head in KEYWORDS or ("=" in head and head.split("=", 1)[0].isidentifier()):
                i += 1
            elif redirect(head):
                i += redirect(head)
            elif head in WRAPPERS:
                i += 1
                while i < n and w[i].startswith("-"):
                    i += 2 if w[i] in WRAPPERS[head] else 1
            elif head == "timeout":
                i += 1
                while i < n and w[i].startswith("-"):
                    i += 2 if w[i] in TIMEOUT_WITH_VALUE else 1
                i += 1
            elif head == "dotenvx" and w[i + 1:i + 2] == ["run"]:
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
            sub = []
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
    rest = word.lstrip("0123456789")
    if rest[:1] not in ("<", ">"):
        return 0
    return 2 if rest in ("<", ">", ">>", ">&", "<&", ">|") else 1


def dry_or_help(args: list) -> bool:
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
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if "bb-writ" not in line:
                continue
            try:
                if entry_in(json.loads(line), skills, slashes):
                    return True
            except Exception:
                continue
    return False


def slash_name(text: str):
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
    if row.get("type") == "queue-operation":
        return slash_line(row["content"])
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


def first_row(path: str):
    with open(path, encoding="utf-8", errors="replace") as f:
        return json.loads(f.readline(FIRST_ROW_LIMIT))


def same_entry(base: str, entry: str) -> bool:
    """先頭の行の道筋 base が、hook の置き場の入口 entry（<親>/<名前>/skills/bb-writ）と同じ入口か。同じ場所か、同じ親の下の別の
    置き場（版の兄弟）の skills/bb-writ（<親>/<別の名前>/skills/bb-writ）なら同じ入口と数える。どちらも実体の道筋に直し、道筋の部品で
    比べる（違ってよいのは <名前> の 1 段だけ。文字列の前方一致では比べない）。同じ場所は、先に実体の道筋の一致で見る（入口の
    skills/bb-writ 自体が symlink の置き場では、実体の道筋が skills/bb-writ で終わらないので、兄弟の比べには当たらない）。置き場が
    在るかと、名前の形（版の文字列か）は見ない。

    理由: cache に写すプラグインの置き場は cache/<marketplace>/<plugin>/<版>/ で、更新のたびに変わる（公式の文書 plugins/loading）。
    前の版の置き場で始まった指揮者の先頭の行は、前の版の置き場を指したまま続く。
    誤って数える形: hook の置き場と同じ親の下の、別のディレクトリの skills/bb-writ を読み込んだ子も、同じ入口と数える。cache に写す形
    では、別の版の置き場。置き場からそのまま読む形（--plugin-dir・手元のディレクトリの marketplace）では、置き場の隣のディレクトリ
    （例: 置き場がホームの直下なら、ホームの .claude/skills/bb-writ）。
    守らない範囲は module の docstring。"""
    base, entry = os.path.realpath(base), os.path.realpath(entry)
    if base == entry:
        return True
    b, e = base.split(os.sep), entry.split(os.sep)
    return len(b) == len(e) >= 4 and b[-2:] == e[-2:] == ["skills", "bb-writ"] and b[:-3] == e[:-3]


def entry_row(row) -> bool:
    content = row["message"]["content"]
    texts = [content] if isinstance(content, str) else [b.get("text") for b in content if b.get("type") == "text"]
    return any(isinstance(t, str) and t.startswith(BASE_HEAD)
               and same_entry(t[len(BASE_HEAD):].split("\n", 1)[0], ENTRY_DIR) for t in texts)


def entry_child(transcript_path, agent_id) -> bool:
    """hook の入力の agent_id の子が、このプラグインの入口の fork した子（指揮者）か、その子孫か。子の会話記録を、
    transcript_path の置き場の全部のセッションの置き場から探し、先頭の行を見る。当たらなければ meta.json の parentAgentId を
    ANCESTOR_LIMIT 段までたどる。読めないもの（ファイルが無い、JSON でない、形が違う、権限が無い）は当たらなかったものとして飛ばす。"""
    try:
        sessions = glob.escape(os.path.dirname(transcript_path))
    except Exception:
        return False
    seen, current = set(), [agent_id]
    for _ in range(ANCESTOR_LIMIT + 1):
        parents = []
        for aid in current:
            if not isinstance(aid, str) or not GLOB_SAFE_AGENT_ID.fullmatch(aid) or aid in seen:
                continue
            seen.add(aid)
            for path in glob.glob(os.path.join(sessions, "*", "subagents", f"agent-{aid}.jsonl")):
                try:
                    if entry_row(first_row(path)):
                        return True
                except Exception:
                    continue
            for path in glob.glob(os.path.join(sessions, "*", "subagents", f"agent-{aid}.meta.json")):
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        parents.append(json.loads(f.read(FIRST_ROW_LIMIT))["parentAgentId"])
                except Exception:
                    continue
        if not parents:
            return False
        current = parents
    return False


def main() -> int:
    try:
        d = json.load(sys.stdin)
        command = (d.get("tool_input") or {}).get("command") or ""
        if d.get("tool_name") != "Bash" or not outward(command):
            return 0
        path = d.get("transcript_path")
        try:
            marked = used_entry(path)
        except Exception:
            marked = False
        if not (marked or entry_child(path, d.get("agent_id"))):
            return 0
    except Exception:
        return 0
    # ensure_ascii（既定）: 出力の符号化が日本語を扱えない環境でも、書き出しで落ちて拒否が消えないように
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": REASON}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
