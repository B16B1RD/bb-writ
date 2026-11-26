"""入口を呼んだセッションで、関門（work.py op run）を通さない外へ出すコマンドを拒否する hook（scripts/push_guard.py）。

見ないこと: 会話記録の符号化の指定（encoding・errors="replace"）。不正なバイトのある記録で入口の印を読めるかは試験で固定しない。
git push の試すだけの指定は完全一致で比べる（短い引数をまとめた -nq・-nv の dry-run は、試すだけと読まずに止める。
誤って止める所）。
語に続く << の前で語を切ることは、崩れた入力でしか差が出ないので例を置かない。
守らない範囲（通す書き方・誤って止める書き方）は scripts/push_guard.py の module の docstring。"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent)); import process_limit  # noqa: E402,F401  試験に上限を掛ける（直に走らせても）

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "push_guard.py"
HOOKS = ROOT / "hooks" / "hooks.json"

# 入口を呼んだ印の、会話記録の行（2026-11-18、Claude Code 2.1.327 の記録の形）
BY_SKILL_TOOL = {"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "tool_use", "id": "toolu_1", "name": "Skill", "input": {"skill": "bb-writ:bb-writ"}}]}}
BY_BARE_NAME = {"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "tool_use", "id": "toolu_3", "name": "Skill", "input": {"skill": "bb-writ"}}]}}
BY_SLASH = {"type": "user", "message": {"role": "user", "content":
    "<command-message>bb-writ:bb-writ</command-message>\n<command-name>/bb-writ:bb-writ</command-name>\n<command-args>直して</command-args>"}}
BY_SLASH_IN_TEXT_BLOCK = {"type": "user", "message": {"role": "user", "content": [
    {"type": "text", "text": "<command-name>/bb-writ:bb-writ</command-name>\n<command-args>直して</command-args>"}]}}
OTHER_SKILL = {"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "tool_use", "id": "toolu_2", "name": "Skill", "input": {"skill": "bb-writ:playbook-docs-skill"}}]}}
# 本体を直に呼んだ印（続きの引き受け・要約の後。入口を通らないので、入口の印は付かない）
BY_BODY_SKILL_TOOL = {"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "tool_use", "id": "toolu_8", "name": "Skill", "input": {"skill": "bb-writ:conduct"}}]}}
BY_BODY_SLASH = {"type": "user", "message": {"role": "user", "content":
    "<command-message>bb-writ:conduct</command-message>\n<command-name>/bb-writ:conduct</command-name>\n<command-args>続きを</command-args>"}}
BY_BODY_SLASH_IN_TEXT_BLOCK = {"type": "user", "message": {"role": "user", "content": [
    {"type": "text", "text": "<command-name>/bb-writ:conduct</command-name>\n<command-args>続きを</command-args>"}]}}
# 本体の短い名前は数えない（ほかのプラグインの同じ名前のスキルと区別できない）
BODY_BARE_NAME = {"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "tool_use", "id": "toolu_9", "name": "Skill", "input": {"skill": "conduct", "args": "bb-writ の続き"}}]}}
OTHER_PLUGIN_BODY_SLASH = {"type": "user", "message": {"role": "user", "content":
    "<command-message>other:conduct</command-message>\n<command-name>/other:conduct</command-name>\n<command-args>bb-writ の続き</command-args>"}}
# 先頭に空白・改行のある行は数えない。非対話では、Claude Code がスラッシュの呼び出しとして扱わず、親のモデルに普通の文として届いた
# （2026-11-20、2.1.328 と 2.1.329）。対話では、2.1.329 は先頭の空白を削って入口を呼び、行は /bb-writ:bb-writ で始まる
NAME_AFTER_A_SPACE = {"type": "user", "message": {"role": "user", "content": " /bb-writ:bb-writ 直して"}}
NAME_AFTER_A_NEWLINE = {"type": "queue-operation", "operation": "enqueue", "content": "\n/bb-writ:bb-writ 直して"}
# 本体の名前で始まる素の行は数えない（本体は fork しないので、スラッシュで呼ぶと <command-name> の形の行が先に書かれる）
BODY_NAME_HEADS_A_QUEUED_ROW = {"type": "queue-operation", "operation": "enqueue", "content": "/bb-writ:conduct 続きを"}
# fork する入口をスラッシュで呼んだときの記録（2026-11-20、Claude Code 2.1.328。本文の先頭が「/名前 引数」のままの行）
BY_FORKED_SLASH = {"type": "user", "message": {"role": "user", "content": "/bb-writ:bb-writ 直して。push まで"}}
BY_FORKED_SLASH_ALONE = {"type": "user", "message": {"role": "user", "content": "/bb-writ:bb-writ"}}
BY_FORKED_SLASH_IN_TEXT_BLOCK = {"type": "user", "message": {"role": "user", "content": [
    {"type": "text", "text": "/bb-writ:bb-writ\n直して"}]}}
# 非対話の実行で、指揮者の Bash の時点に在る行（入力を受け付けた行。利用者の行は、指揮者が終わるまで書かれない）
BY_QUEUED_FORKED_SLASH = {"type": "queue-operation", "operation": "enqueue", "content": "/bb-writ:bb-writ 直して。push まで"}
# 短い名前 /bb-writ で呼んだ入口（2026-11-20、2.1.329、非対話、各 1 回）。名前の直後が半角の空白・タブ・改行・全角の空白の形と、
# 引数なしの形は、どれも実物で入口が fork し、指揮者が動いている間の入力を受け付けた行は、打ったままだった
SHORT_NAME_HEADS = {"空白の後に引数": "/bb-writ 直して", "引数なし": "/bb-writ", "タブの後に引数": "/bb-writ\t直して",
                    "改行の後に引数": "/bb-writ\n直して", "全角の空白の後に引数": "/bb-writ\u3000直して"}
BY_QUEUED_SHORT_SLASH = {"type": "queue-operation", "operation": "enqueue", "content": "/bb-writ 直して"}
# 利用者の行に短い名前が打ったままで残る形は、確かめたバージョンでは見ていない（残っていれば数える）
BY_SHORT_SLASH = {"type": "user", "message": {"role": "user", "content": "/bb-writ 直して"}}
BY_SHORT_SLASH_IN_TEXT_BLOCK = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "/bb-writ 直して"}]}}
# 入口の名前で始まらない文（名前の前方一致、ほかのスキル、本体の名前でない別の名前、先頭の空白・改行、文の途中の名前）
NOT_ENTRY_HEADS = ["/bb-writx 直して", "/bb-writer 直して", "/bb-writ-foo", "/bb-writ:other 直して", "/bb-writ:bb-writer 直して",
                   " /bb-writ 直して", "\n/bb-writ 直して", "さっきの /bb-writ の続き"]


# 入口の子（指揮者）の会話記録の先頭の行（2026-11-20、2.1.329 の形。content は入口のスキルの本文を読み込んだ文）
ENTRY_BASE = ROOT / "skills" / "bb-writ"
CONDUCTOR = "a1b2c3d4e5f607182"  # agent_id は 17 桁の 16 進の文字列だった
CONTINUED = {"type": "user", "message": {"role": "user", "content": "続きの行"}}


def entry_first_row(base=ENTRY_BASE, blocks=False, request="直して") -> dict:
    text = f"Base directory for this skill: {base}\n\n# bb-writ（入口）\n" + "本文" * 2000 + f"\n\nARGUMENTS: {request}"
    return {"type": "user", "isMeta": True,
            "message": {"role": "user", "content": [{"type": "text", "text": text}] if blocks else text}}


def queued(text: str) -> dict:
    return {"type": "queue-operation", "operation": "enqueue", "content": text}


def user_string(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def user_block(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


MENTION_ONLY = {"type": "user", "message": {"role": "user", "content": "bb-writ:bb-writ の使い方を教えて"}}
QUEUED_NOTIFICATION = {"type": "queue-operation", "operation": "enqueue", "content": "<task-notification>\n/bb-writ:bb-writ 直して の子が終わった"}
QUEUED_OTHER_NAME = {"type": "queue-operation", "operation": "enqueue", "content": "/bb-writ:bb-writer 直して"}
NAME_HEADS_ANOTHER_KIND_OF_ROW = {"type": "system", "subtype": "local_command", "content": "/bb-writ:bb-writ 直して"}
# 入口の名前で始まらない行（別の名前の前方一致、文の途中の名前、書き手の本文）
OTHER_NAME_WITH_THE_SAME_HEAD = {"type": "user", "message": {"role": "user", "content": "/bb-writ:bb-writer 直して"}}
OTHER_NAME_WITH_A_HYPHEN = {"type": "user", "message": {"role": "user", "content": "/bb-writ:bb-writ-x 直して"}}
NAME_IN_THE_MIDDLE = {"type": "user", "message": {"role": "user", "content": "さっきの /bb-writ:bb-writ 直して の結果を見せて"}}
NAME_HEADS_ASSISTANT_STRING = {"type": "assistant", "message": {"role": "assistant", "content": "/bb-writ:bb-writ 直して"}}
NAME_HEADS_ASSISTANT_BLOCK = {"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "text", "text": "/bb-writ:bb-writ 直して"}]}}
# 入口の名前を持つが、入口を呼んだ印ではない行（ほかの道具の入力、道具の結果に写った文字列）
NOT_SKILL_TOOL = {"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "tool_use", "id": "toolu_4", "name": "Read", "input": {"skill": "bb-writ:bb-writ"}}]}}
MARK_IN_TOOL_RESULT = {"type": "user", "message": {"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": "toolu_5", "text": "<command-name>/bb-writ:bb-writ</command-name>"}]}}
MARK_QUOTED_BY_ASSISTANT = {"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "text", "text": "記録には <command-name>/bb-writ:bb-writ</command-name> の行がある"}]}}
MARK_IN_ASSISTANT_STRING = {"type": "assistant", "message": {"role": "assistant", "content": "<command-name>/bb-writ:bb-writ</command-name>"}}
MARK_INSIDE_USER_TEXT = {"type": "user", "message": {"role": "user", "content":
    "記録の例: <command-name>/bb-writ:bb-writ</command-name> の行がある"}}
MARK_INSIDE_USER_BLOCK = {"type": "user", "message": {"role": "user", "content": [
    {"type": "text", "text": "記録の例: <command-name>/bb-writ:bb-writ</command-name>"}]}}
# 入口の名前を含むが形の違う行（input が文字列の Skill）。この後ろの印まで読み続ける
NULL_ROWS = [{"type": "assistant", "message": None, "x": "bb-writ"},
             {"type": "queue-operation", "operation": "enqueue", "content": None, "x": "bb-writ"},
             {"type": "assistant", "message": {"role": "assistant", "content": [
                 {"type": "tool_use", "id": "toolu_7", "name": "Skill", "input": None}]}, "x": "bb-writ"}]
SLASH_WITH_SPACES = {"type": "user", "message": {"role": "user", "content":
    "  <command-message>bb-writ:bb-writ</command-message>\n<command-name> /bb-writ:bb-writ </command-name>"}}
BROKEN_ROW = {"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "tool_use", "id": "toolu_6", "name": "Skill", "input": "bb-writ:bb-writ"}]}}
OTHER_SLASH_WITH_MARK = {"type": "user", "message": {"role": "user", "content":
    "<command-message>bb-writ:reflect</command-message>\n<command-name>/bb-writ:reflect</command-name>\n"
    "<command-args><command-name>/bb-writ:bb-writ</command-name> の行を読んで</command-args>"}}
MARK_HEADS_ASSISTANT_BLOCK = {"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "text", "text": "<command-name>/bb-writ:bb-writ</command-name>"}]}}
SKILL_NAME_NOT_TOOL_USE = {"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "server_tool_use", "id": "srv_1", "name": "Skill", "input": {"skill": "bb-writ:bb-writ"}}]}}
ENTRIES = {"スラッシュ（前後の空白）": SLASH_WITH_SPACES, "Skill ツール": BY_SKILL_TOOL, "Skill ツールの短い名前": BY_BARE_NAME, "スラッシュ": BY_SLASH,
           "スラッシュ（text ブロック）": BY_SLASH_IN_TEXT_BLOCK,
           "fork する入口のスラッシュ": BY_FORKED_SLASH, "fork する入口のスラッシュ（引数なし）": BY_FORKED_SLASH_ALONE,
           "fork する入口のスラッシュ（text ブロック）": BY_FORKED_SLASH_IN_TEXT_BLOCK,
           "fork する入口のスラッシュ（非対話。入力を受け付けた行）": BY_QUEUED_FORKED_SLASH,
           "本体の Skill ツール": BY_BODY_SKILL_TOOL, "本体のスラッシュ": BY_BODY_SLASH,
           "本体のスラッシュ（text ブロック）": BY_BODY_SLASH_IN_TEXT_BLOCK}

OUTWARD = ["git push origin main", "GH_CONFIG_DIR=x git -C . push -q origin HEAD:main", "cd repo && git -c user.name=w push",
           "git status && git push", "env GIT_TERMINAL_PROMPT=0 git push", "gh pr create --fill", "gh pr merge 3 --squash",
           "gh release create v1 --notes-file n.md",
           # 区切り
           "a; git push", "a | git push", "(git push)", "{ git push; }", "! git push", "echo `git push`", "x=$(git push)",
           'echo "$(git push)"', "git status\ngit push", "if x; then git push; fi", "if x; then a; else git push; fi",
           "for b in a; do git push; done",
           # git の大域の引数と包むコマンド
           "git --git-dir .git push", "git --work-tree w push", "git --no-pager push", 'git -C "my dir" push',
           "git -C 'a b' push", 'FOO="a b" git push', "env -i git push", "env -u X git push", "timeout 60 git push",
           "timeout -k 5 60 git push", "time git push", "nohup git push", "command git push", "sudo -u w git push",
           "/usr/bin/git push", "\\git push",
           # gh の大域の引数
           "gh -R o/r pr create", "gh --repo o/r pr merge 3", "gh --repo=o/r release create v1",
           # heredoc の後の行
           "cat <<E\ntext\nE\ngit push", "cat <<-E\n\ttext\n\tE\ngit push", "cat <<E>out.txt\ntext\nE\ngit push",
           # 行の継続・注釈の後の行・dotenvx・gh の副コマンドの前のフラグ・引用符に空白のある値・まとまり
           "git \\\n  push origin main", "git pu\\\nsh", "echo x # c\ngit push", "dotenvx run -f .env -- git push",
           "dotenvx run -- gh pr create", "gh pr --repo o/r merge 3", 'GIT_SSH_COMMAND="ssh -o X=no" git push',
           "x || { git push; }", "echo a#b; git push",
           # 二重引用符の中のコマンドの置き換えの後（Claude Code が commit で普通に書く形）、引数の中の置き換え
           "git commit -m \"$(cat <<'EOF'\nmsg\nEOF\n)\" && git push", "git commit -m \"$(cat <<'EOF'\nmsg\nEOF\n)\"\ngit push origin main",
           'b="$(git branch --show-current)"; git push -u origin "$b"', 'test -z "$(git status --porcelain)" && git push',
           'echo "n=$((n+1))" && git push', 'echo "a `date` b" && gh pr create --fill', 'git -C "$(pwd)" push',
           'FOO="$(printf x)" git push',
           # ${…}・$((…))・<<< の後、空の引用符の語
           "echo ${#x}; git push", "if [ ${#files[@]} -gt 0 ]; then git push; fi", "echo ''#b; git push",
           'cat <<< "hello"\ngit push', "echo $((1<<2))\ngit push", 'git -C "" push',
           # 向け直しと、前置きの引数
           "2>/dev/null git push", ">/dev/null git push", "time -p git push", "time -f %e git push", "2> err.txt git push", "nice -n 5 git push", "sudo --user w git push",
           "env --unset X git push", "timeout --signal KILL 60 git push", "timeout --kill-after 5 60 git push",
           "git --attr-source HEAD push",
           # 制御の語の直後
           "if git push; then :; fi", "while git push; do :; done", "until git push; do :; done",
           "if a; then :; elif git push; then :; fi",
           # 前置きの語と、その値を取る引数（要素ごと）
           "builtin git push", "exec git push", "exec -a n git push", "setsid git push", "stdbuf -o L git push",
           "env -C /tmp git push", "env --chdir /tmp git push", "sudo -g g git push", "sudo --group g git push",
           "sudo -C 3 git push", "sudo --close-from 3 git push", "sudo -h h git push", "sudo --host h git push",
           "sudo -p p git push", "sudo --prompt p git push", "nice --adjustment 5 git push", "time --format %e git push",
           "time -o f git push", "time --output f git push", "timeout -s KILL 60 git push",
           "git --namespace n push", "git --config-env a=B push",
           # git 2.43.0 では push しない（--exec-path は = の無い形だと置き場を表示して終わり、--super-prefix は未知の引数）が、
           # 値を取る引数として読み飛ばして止める。誤って止める側（守らない）
           "git --exec-path /x push", "git --super-prefix p push",
           # 向け直しの演算子だけの語は、次の語が行き先
           "< in.txt git push", ">> log.txt git push", ">& log.txt git push", "<& 0 git push", ">| f git push",
           "git push>/dev/null", "git >out push", "git > out push", "git 2>err push", "gh pr create>out.txt",
           # 前置き・大域の引数の値（空の一重引用符も値の語）
           "stdbuf -i 0 git push", "stdbuf -e 0 git push", "git -C '' push",
           # git push の値を取る引数の値は、試すだけ・使い方の指定でない
           "git push -o -n origin main", "git push --push-option -n origin main", "git push --receive-pack -n origin main",
           "git push --exec -n origin main", "git push --repo -n main",
           # 二重引用符の中の \" の後と、heredoc が 2 つ・語の前に空白
           'echo "a\\"b"; git push', "cat <<A; cat <<B\na\nA\nb\nB\ngit push", "cat << EOF\nx\nEOF\ngit push",
           # ( ) の入れ子、置き換えの中の ( ) を閉じた後、プロセスの置き換え、case の a) の後
           "(cd a; (b); git push)", 'echo "$( (true); git push)"', 'echo "$( (true))"; git push', "cat <(git push)",
           "case x in a) git push;; esac",
           # 二重引用符の中のバッククォート、${…} の入れ子の後
           'echo "`git push`"', 'echo "${a:-"${b}"}"; git push',
           # 試すだけの指定でない git push の引数、タブの区切り、語に続く <<
           "git push --no-verify origin main", "git\tpush", "x<<E\nE\ngit push",
           # env -S・--split-string は値を 1 語として読み飛ばす（本物の env はその値を命令にするので、これは誤って止める側。
           # 守らない）
           "env -S x git push", "env --split-string x git push",
           # 終わりの語に引用符や \ を付けた heredoc の、終わりの後
           'cat <<"EOF"\nx\nEOF\ngit push', "cat <<\\EOF\nx\nEOF\ngit push",
           # 誤って止める所（守らない）: gh の -h と、副コマンドの前の --help
           "gh pr create -h", "gh --help pr create"]
NOT_OUTWARD = ["python3 /p/scripts/work.py op run --dir . --key push:origin/main@abc -- git push origin main",
               'git commit -m "docs: git push の説明を直す"', 'git commit -m "x; git push"', "git commit -m 'a && git push'",
               'echo "次に git push する"', 'grep -n "git push" README.md', "git status", "gh pr view 3", "git pushx",
               "gh issue create --title t", "git log --grep push", "git stash push", "git remote set-url --push origin x",
               "gh api repos/o/r/pulls", "cat <<'EOF' > deploy.sh\ngit push origin main\nEOF",
               "cat <<-E\n\tgit push\n\tE",
               # 関門の行を折る・本文に外へ出す形の行がある（関門そのものを止めない）
               "python3 w.py op run --key push:origin/main@a -- \\\n  git push origin main",
               "python3 w.py op run --key pr:x -- gh pr create --body \"手順:\ngh pr merge は op run で\"",
               "python3 w.py op run --key pr:x -- gh pr create --body \"$(cat <<'B'\ngit push の前に\nB\n)\"",
               # 注釈・引用符の中の英文
               "echo x # then git push later", "echo x # ; git push",
               # 置き換えの中だけの文字列、関門の行の本文の置き換え
               'git commit -m "bump $(cat VERSION) git push guard"', 'echo "now $(date) git push will follow"',
               'git commit -m "$(date)" -m "then; git push later"', 'echo "$(printf x); git push"',
               'python3 w.py op run --key pr:x -- gh pr create --body "$(cat b.md)" --title "feat: (gh pr create) を止める"',
               # heredoc の終わりの行は完全に一致したものだけ（<< では前後の空白を外さない）
               "cat <<EOF\n EOF \ngit push\nEOF", "cat <<EOF\n  EOF\ngit push\nEOF",
               # 外へ何も出さない
               "git push --dry-run", "git push -n origin main", "gh pr create --help", "git push -h",
               "git --help push", "git -h push", "git push --help",
               # 空の一重引用符もコマンドの名前の語（bash は空の名前のコマンドを探して失敗し、git は起きない）
               "'' git push",
               # << は終わりの行のタブを外さない（本文が続く）、<< と語の間の空白の後でも本文は読まない
               "cat <<EOF\n\tEOF\ngit push\nEOF", "cat << EOF\ngit push\nEOF",
               # 代入でない語（名前が識別子でない）の後は、その語がコマンド
               "a-b=c git push",
               # git でない道具、dotenvx の run でない副コマンド、終わりの語に引用符や \ を付けた heredoc の本文
               "gitk push", "dotenvx get X -- git push", 'cat <<"EOF"\ngit push\nEOF', "cat <<\\EOF\ngit push\nEOF",
               # 二重引用符の中の \" の後も引用符の中、heredoc の終わりは完全に一致した行、2 つ目の heredoc の本文
               'echo "a\\"b; git push"', "cat <<EOF\nEOFX\ngit push\nEOF", "cat <<A; cat <<B\na\nA\ngit push\nB",
               # 数だけの語は向け直しでない（読み飛ばさない）
               "60 git push", 'git commit -m "feat: x" -m "then git push"', "git log --oneline | grep \"do git push\""]


class TestPushGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def transcript(self, *rows) -> str:
        p = Path(self.tmp.name) / "session.jsonl"
        p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows) + '{"broken\n')
        return str(p)

    def run_guard(self, command, transcript, tool="Bash", raw=None, **extra):
        stdin = raw if raw is not None else json.dumps({"session_id": "s", "transcript_path": transcript, "cwd": self.tmp.name,
                                                        "hook_event_name": "PreToolUse", "tool_name": tool,
                                                        "tool_input": {"command": command}, **extra})
        p = subprocess.run([sys.executable, "-B", str(GUARD)], input=stdin, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)  # 拒否も JSON で返す。終了コードで止めない（ほかの失敗と区別する）
        return json.loads(p.stdout)["hookSpecificOutput"] if p.stdout.strip() else None

    def test_outward_bash_calls_are_denied_in_a_session_that_used_the_entry(self):
        for name, entry in ENTRIES.items():
            t = self.transcript(OTHER_SKILL, BROKEN_ROW, *NULL_ROWS, entry)
            for c in OUTWARD:
                with self.subTest(entry=name, command=c):
                    out = self.run_guard(c, t)
                    self.assertIsNotNone(out, "外へ出すコマンドが通った")
                    self.assertEqual((out["hookEventName"], out["permissionDecision"]), ("PreToolUse", "deny"))
                    self.assertIn("work.py op run", out["permissionDecisionReason"])  # 関門へ戻す道を示す

    def test_other_bash_calls_pass_in_a_session_that_used_the_entry(self):
        t = self.transcript(BY_SKILL_TOOL)
        for c in NOT_OUTWARD:
            with self.subTest(command=c):
                self.assertIsNone(self.run_guard(c, t))

    def test_sessions_that_did_not_use_the_entry_are_not_touched(self):
        for rows in ((), (OTHER_SKILL,), (MENTION_ONLY,), tuple(NULL_ROWS), (NOT_SKILL_TOOL,), (MARK_IN_TOOL_RESULT,),
                     (MARK_QUOTED_BY_ASSISTANT,), (MARK_IN_ASSISTANT_STRING,),
                     (MARK_INSIDE_USER_TEXT,), (MARK_INSIDE_USER_BLOCK,), (BROKEN_ROW,),
                     (OTHER_SLASH_WITH_MARK,), (MARK_HEADS_ASSISTANT_BLOCK,), (SKILL_NAME_NOT_TOOL_USE,),
                     (OTHER_NAME_WITH_THE_SAME_HEAD,), (NAME_IN_THE_MIDDLE,), (NAME_HEADS_ASSISTANT_STRING,), (NAME_HEADS_ASSISTANT_BLOCK,),
                     (QUEUED_NOTIFICATION,), (QUEUED_OTHER_NAME,), (NAME_HEADS_ANOTHER_KIND_OF_ROW,),
                     (OTHER_NAME_WITH_A_HYPHEN,), (BODY_BARE_NAME,), (OTHER_PLUGIN_BODY_SLASH,),
                     (NAME_AFTER_A_SPACE,), (NAME_AFTER_A_NEWLINE,), (BODY_NAME_HEADS_A_QUEUED_ROW,)):
            t = self.transcript(*rows)
            with self.subTest(rows=[r.get("type") for r in rows], first=json.dumps(rows[:1], ensure_ascii=False)[:60]):
                self.assertIsNone(self.run_guard("git push origin main", t))

    def test_a_short_entry_name_at_the_head_counts(self):
        # 短い名前 /bb-writ で呼んだ入口も印に数える（入力を受け付けた行と、利用者の行の文字列・text ブロック）
        rows = {**{f"入力を受け付けた行（{k}）": queued(v) for k, v in SHORT_NAME_HEADS.items()},
                "利用者の行（文字列）": BY_SHORT_SLASH, "利用者の行（text ブロック）": BY_SHORT_SLASH_IN_TEXT_BLOCK}
        for name, row in rows.items():
            with self.subTest(name):
                self.assertIsNotNone(self.run_guard("git push origin main", self.transcript(OTHER_SKILL, BROKEN_ROW, *NULL_ROWS, row)),
                                     "短い名前で呼んだ入口の印を数えなかった")

    def test_a_wide_space_after_the_full_entry_name_counts(self):
        # 正式な名前の直後が全角の空白の入力も、入口は fork し、入力を受け付けた行は打ったままだった（2026-11-20、2.1.329、非対話、1 回）
        self.assertIsNotNone(self.run_guard("git push origin main", self.transcript(queued("/bb-writ:bb-writ\u3000直して"))),
                             "正式な名前の直後が全角の空白の入力を受け付けた行を、入口の印に数えなかった")

    def test_texts_that_do_not_head_with_an_entry_name_are_not_counted(self):
        # 入力を受け付けた行と利用者の行の両方で、入口の名前で始まらない文は数えない
        for text in NOT_ENTRY_HEADS:
            for kind, row in (("入力を受け付けた行", queued(text)), ("利用者の行（文字列）", user_string(text)),
                              ("利用者の行（text ブロック）", user_block(text))):
                with self.subTest(kind=kind, text=text):
                    self.assertIsNone(self.run_guard("git push origin main", self.transcript(row)),
                                      "入口の名前で始まらない文を、入口を呼んだ印に数えた")

    # 入口の子（指揮者）とその子孫の Bash。親の会話記録に印が無くても、子の会話記録の先頭の行で見分ける
    def parent(self, session="s1", rows=(OTHER_SKILL,), proj="proj") -> str:
        """親の会話記録 <一時>/<proj>/<セッション>.jsonl（既定は印の無い行だけ）を作り、その道筋を返す。"""
        p = Path(self.tmp.name) / proj / f"{session}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        return str(p)

    def child(self, session, agent_id, *rows, parent_id=None, raw=None, name=None, meta=True, proj="proj"):
        """子の会話記録 <一時>/<proj>/<セッション>/subagents/agent-<id>.jsonl と、meta が真なら meta.json を作る（raw は行の代わりに書く文）。"""
        d = Path(self.tmp.name) / proj / session / "subagents"
        p = d / (name or f"agent-{agent_id}.jsonl")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(raw if raw is not None else "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        if meta:
            fields = {"agentType": "general-purpose", **({"parentAgentId": parent_id, "spawnDepth": 2} if parent_id else {})}
            (d / f"agent-{agent_id}.meta.json").write_text(json.dumps(fields), encoding="utf-8")

    def test_a_push_by_the_conductor_is_denied_by_its_first_row(self):
        t = self.parent()
        self.child("s1", CONDUCTOR, entry_first_row(), CONTINUED)
        self.assertIsNotNone(self.run_guard("git push origin main", t, agent_id=CONDUCTOR), "指揮者の素の push が通った")

    def test_a_push_by_the_conductor_after_clear_is_denied(self):
        # /clear の後: hook に届く会話記録は新しいセッションのもの。そこの子の会話記録は続きの行だけ（meta.json は無い）で、
        # 入口の行は前のセッションに残る
        self.parent("s1")
        self.child("s1", CONDUCTOR, entry_first_row(), CONTINUED)
        t = self.parent("s2")
        self.child("s2", CONDUCTOR, CONTINUED, meta=False)
        self.assertIsNotNone(self.run_guard("git push origin main", t, agent_id=CONDUCTOR), "/clear の後の指揮者の素の push が通った")

    def test_a_push_by_the_conductor_is_denied_when_the_parent_transcript_is_missing(self):
        # hook の入力の会話記録のファイルがまだ無い（置き場は在る）ときも、親の印は無いものとして、子の会話記録で見分ける
        self.parent("s1")
        self.child("s1", CONDUCTOR, entry_first_row(), CONTINUED)
        t = str(Path(self.tmp.name) / "proj" / "s2.jsonl")
        self.assertFalse(Path(t).exists())
        self.assertIsNotNone(self.run_guard("git push origin main", t, agent_id=CONDUCTOR),
                             "親の会話記録が無いときに、指揮者の素の push が通った")

    def test_a_push_by_a_descendant_of_the_conductor_is_denied(self):
        t = self.parent()
        self.child("s1", CONDUCTOR, entry_first_row())
        self.child("s1", "b0000000000000001", CONTINUED, parent_id=CONDUCTOR)
        self.child("s1", "c0000000000000002", CONTINUED, parent_id="b0000000000000001")
        for name, aid in (("孫", "b0000000000000001"), ("ひ孫", "c0000000000000002")):
            with self.subTest(name):
                self.assertIsNotNone(self.run_guard("git push origin main", t, agent_id=aid), "指揮者の子孫の素の push が通った")

    def test_a_push_by_a_grandchild_started_after_clear_is_denied(self):
        # /clear の後に指揮者が立てた孫: 孫の会話記録と meta.json は新しいセッションの置き場に、指揮者の入口の行は前の置き場に在る
        self.parent("s1")
        self.child("s1", CONDUCTOR, entry_first_row(), CONTINUED)
        t = self.parent("s2")
        self.child("s2", CONDUCTOR, CONTINUED, meta=False)
        self.child("s2", "b0000000000000001", CONTINUED, parent_id=CONDUCTOR)
        self.assertIsNotNone(self.run_guard("git push origin main", t, agent_id="b0000000000000001"),
                             "/clear の後に指揮者が立てた孫の素の push が通った")

    def test_a_long_entry_row_counts(self):
        # 入口の行は依頼文を含む。300,000 字の行でも読む
        t = self.parent()
        row = entry_first_row(request="依" * 300000)
        self.assertGreater(len(json.dumps(row, ensure_ascii=False)), 300000)
        self.child("s1", CONDUCTOR, row)
        self.assertIsNotNone(self.run_guard("git push origin main", t, agent_id=CONDUCTOR), "長い依頼の指揮者の素の push が通った")

    def test_a_session_folder_with_glob_characters_counts(self):
        t = self.parent(proj="pr[o]j*")
        self.child("s1", CONDUCTOR, entry_first_row(), proj="pr[o]j*")
        self.assertIsNotNone(self.run_guard("git push origin main", t, agent_id=CONDUCTOR),
                             "置き場の道筋に glob の記号があるときに、指揮者の素の push が通った")

    def test_ancestors_are_followed_up_to_eight_levels(self):
        t = self.parent()
        self.child("s1", CONDUCTOR, entry_first_row())
        chain = [CONDUCTOR] + [f"g{i}" for i in range(1, 10)]
        for parent_id, aid in zip(chain, chain[1:]):
            self.child("s1", aid, CONTINUED, parent_id=parent_id)
        with self.subTest("8 段"):
            self.assertIsNotNone(self.run_guard("git push origin main", t, agent_id="g8"), "8 段たどった先の指揮者で止めなかった")
        with self.subTest("9 段"):
            self.assertIsNone(self.run_guard("git push origin main", t, agent_id="g9"), "9 段たどった先の指揮者で止めた")

    def test_an_entry_row_with_text_blocks_counts(self):
        t = self.parent()
        self.child("s1", CONDUCTOR, entry_first_row(blocks=True))
        self.assertIsNotNone(self.run_guard("git push origin main", t, agent_id=CONDUCTOR), "text のブロックの入口の行を数えなかった")

    def test_an_entry_path_through_a_symlink_counts(self):
        link = Path(self.tmp.name) / "plugin-link"
        link.symlink_to(ROOT, target_is_directory=True)
        t = self.parent()
        self.child("s1", CONDUCTOR, entry_first_row(link / "skills" / "bb-writ"))
        self.assertIsNotNone(self.run_guard("git push origin main", t, agent_id=CONDUCTOR), "symlink 越しの入口の道筋を数えなかった")

    def test_children_that_are_not_the_entry_are_not_touched(self):
        other_copy = Path(self.tmp.name) / "other" / "skills" / "bb-writ"
        other_copy.mkdir(parents=True)
        t = self.parent()
        self.child("s1", CONDUCTOR, entry_first_row())
        self.child("s1", "d1", entry_first_row(ROOT / "skills" / "conduct"))
        self.child("s1", "d2", entry_first_row(other_copy))
        self.child("s1", "d3", raw="{not json\n")
        self.child("s1", "d4", raw="")
        self.child("s1", "d5", {"type": "user", "isMeta": True, "message": {"role": "user"}})
        self.child("s1", "d6", entry_first_row(ROOT / "skills" / "bb-writ-x"))
        self.child("s1", "d7", entry_first_row(ROOT / "skills" / "bb-writ" / "refs"))
        for name, extra in (("agent_id が無い", {}), ("置き場にファイルが無い", {"agent_id": "e0000000000000009"}),
                            ("本体の置き場", {"agent_id": "d1"}), ("別の置き場の skills/bb-writ", {"agent_id": "d2"}),
                            ("先頭の行が JSON でない", {"agent_id": "d3"}), ("空のファイル", {"agent_id": "d4"}),
                            ("content が無い", {"agent_id": "d5"}), ("skills/bb-writ-x", {"agent_id": "d6"}),
                            ("skills/bb-writ/refs", {"agent_id": "d7"})):
            with self.subTest(name):
                self.assertIsNone(self.run_guard("git push origin main", t, **extra), "入口の子でない子の push を止めた")

    def test_a_loop_of_parents_ends_without_denial(self):
        t = self.parent()
        self.child("s1", "aaaa", CONTINUED, parent_id="bbbb")
        self.child("s1", "bbbb", CONTINUED, parent_id="aaaa")
        self.assertIsNone(self.run_guard("git push origin main", t, agent_id="aaaa"), "parentAgentId の輪で止めた")

    def test_malformed_agent_ids_are_not_used_in_the_search(self):
        # どの id にも、形を確かめずに置き場の探し方に使えば当たる、入口の行のファイルを置く
        t = self.parent()
        self.child("s1", CONDUCTOR, entry_first_row())
        long_id = "a" * 65
        for aid in ("7", "", long_id):
            self.child("s1", f"bait{len(aid)}", entry_first_row(), name=f"agent-{aid}.jsonl")
        self.child("s1", "bait-dots", entry_first_row(), name="agent-../x.jsonl")
        for name, aid in (("文字列でない", 7), ("空", ""), ("*", "*"), ("../x", "../x"), ("65 字", long_id)):
            with self.subTest(name):
                self.assertIsNone(self.run_guard("git push origin main", t, agent_id=aid), "形の違う agent_id で入口の行に当たった")

    def test_an_entry_row_after_the_first_row_does_not_count(self):
        t = self.parent()
        self.child("s1", CONDUCTOR, CONTINUED, entry_first_row())
        self.assertIsNone(self.run_guard("git push origin main", t, agent_id=CONDUCTOR), "2 行目の入口の行を数えた")

    def test_a_quiet_command_by_the_conductor_passes(self):
        t = self.parent()
        self.child("s1", CONDUCTOR, entry_first_row())
        self.assertIsNone(self.run_guard("git status", t, agent_id=CONDUCTOR), "指揮者の外へ出さないコマンドを止めた")

    def test_the_parent_mark_still_denies_with_an_agent_id(self):
        t = self.parent(rows=(BY_SKILL_TOOL,))
        self.assertIsNotNone(self.run_guard("git push origin main", t, agent_id=CONDUCTOR), "親の会話記録の印で止めなかった")

    def test_the_child_search_is_quick_with_many_sessions(self):
        # セッションの置き場が 300 個でも、時間の上限（5 秒。CI の遅い OS では、Python の起動だけで 1 秒に近づく）の中で終わる。
        # 全部の子の会話記録を読む実装との違いは、この試験では見分けない
        for i in range(300):
            self.parent(f"s{i:03}")
            self.child(f"s{i:03}", f"f{i:016x}", CONTINUED)
        self.child("s150", CONDUCTOR, entry_first_row())
        t = str(Path(self.tmp.name) / "proj" / "s299.jsonl")
        for name, aid, denied in (("指揮者", CONDUCTOR, True), ("置き場に無い子", "e0000000000000009", False)):
            with self.subTest(name):
                start = time.monotonic()
                out = self.run_guard("git push origin main", t, agent_id=aid)
                self.assertLess(time.monotonic() - start, 5)
                self.assertEqual(out is not None, denied)

    def test_the_denial_survives_an_output_encoding_without_japanese(self):
        # 拒否の文は日本語。出力の符号化が日本語を扱えない環境で、書き出しが落ちて拒否が消えない（落ちた hook は通す扱いになる）
        t = self.transcript(BY_SKILL_TOOL)
        stdin = json.dumps({"tool_name": "Bash", "transcript_path": t, "tool_input": {"command": "git push"}})
        for enc in ("ascii", "cp1252"):
            with self.subTest(enc=enc):
                p = subprocess.run([sys.executable, "-B", str(GUARD)], input=stdin, capture_output=True, text=True, timeout=30,
                                   env={**os.environ, "PYTHONIOENCODING": enc})
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual(json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_long_or_broken_commands_finish_quickly(self):
        # Bash の呼び出しごとに動くので、崩れた長い入力でも手間が入力の長さに比例する（正規表現の入れ子は 10 秒を超えた）
        t = self.transcript(BY_SKILL_TOOL)
        for name, c in (("git の大域の引数の連続", "git " + "--x " * 20000 + "pus"), ("包むコマンドの連続", "sudo -E " * 20000),
                        ("代入の連続", "a=" * 20000 + " git"), ("閉じない引用符", 'git -C "' * 5000),
                        ("終わりの無い heredoc", "cat <<E\n" + "git push\n" * 100000)):
            with self.subTest(name):
                start = time.monotonic()
                self.assertIsNone(self.run_guard(c, t))
                self.assertLess(time.monotonic() - start, 5)

    def test_a_grok_build_call_passes(self):
        # Grok Build は tool_name に run_terminal_command を渡し、会話記録も別の形。判定できないので通す
        t = self.transcript(BY_SKILL_TOOL)
        self.assertIsNone(self.run_guard("git push origin main", t, tool="run_terminal_command"))

    def test_input_it_cannot_judge_passes(self):
        t = self.transcript(BY_SKILL_TOOL)
        for name, raw in (("会話記録が無い", json.dumps({"tool_name": "Bash", "transcript_path": str(Path(self.tmp.name) / "none"),
                                                        "tool_input": {"command": "git push"}})),
                          ("会話記録の欄が無い", json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push"}})),
                          ("JSON でない", "git push"), ("空", ""),
                          ("Bash でない", json.dumps({"tool_name": "Edit", "transcript_path": t, "tool_input": {"command": "git push"}})),
                          ("コマンドが文字列でない", json.dumps({"tool_name": "Bash", "transcript_path": t, "tool_input": {"command": ["git", "push"]}}))):
            with self.subTest(name):
                self.assertIsNone(self.run_guard(None, None, raw=raw))
        for name, raw in (("tool_input が null", json.dumps({"tool_name": "Bash", "transcript_path": t, "tool_input": None})),
                          ("command が null", json.dumps({"tool_name": "Bash", "transcript_path": t, "tool_input": {"command": None}}))):
            with self.subTest(name):
                self.assertIsNone(self.run_guard(None, None, raw=raw))

    def test_the_hook_runs_the_guard_on_each_bash_call_and_passes_when_the_guard_is_missing(self):
        cfg = json.loads(HOOKS.read_text(encoding="utf-8"))
        entries = cfg["hooks"]["PreToolUse"]
        self.assertEqual([e["matcher"] for e in entries], ["Bash"])
        hooks = entries[0]["hooks"]
        # 判定は push_guard.py の中だけ。if の欄（Claude Code だけが解す）は置かない（Grok Build は handler を飛ばしてエラーを出す）
        self.assertEqual([(h["type"], "if" in h) for h in hooks], [("command", False)])
        self.assertLessEqual(hooks[0].get("timeout", 600), 30)
        t = self.transcript(BY_SKILL_TOOL)
        stdin = json.dumps({"tool_name": "Bash", "transcript_path": t, "tool_input": {"command": "git push"}})
        # Claude Code と Grok Build と同じく、command を sh で動かし、プラグインの置き場を CLAUDE_PLUGIN_ROOT で渡す
        unreadable = Path(self.tmp.name) / "unreadable"  # 在るが読めないスクリプト
        (unreadable / "scripts").mkdir(parents=True)
        (unreadable / "scripts" / "push_guard.py").write_text("", encoding="utf-8")
        (unreadable / "scripts" / "push_guard.py").chmod(0)
        self.addCleanup((unreadable / "scripts" / "push_guard.py").chmod, 0o600)
        spaced = Path(self.tmp.name) / "plugin root"  # 空白のある置き場
        (spaced / "scripts").mkdir(parents=True)
        (spaced / "scripts" / "push_guard.py").write_text(GUARD.read_text(encoding="utf-8"), encoding="utf-8")
        for name, root, denied in (("置き場がある", ROOT, True), ("空白のある置き場", spaced, True),
                                   ("置き場が消えた", Path(self.tmp.name) / "gone", False), ("置き場が空", "", False),
                                   *((("スクリプトが読めない", unreadable, False),) if os.geteuid() != 0 else ())):  # root は読めてしまう
            with self.subTest(name):
                p = subprocess.run(["sh", "-c", hooks[0]["command"]], input=stdin, capture_output=True, text=True, timeout=30,
                                   env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(root)})
                # 置き場が読めないときも 0 で終わる（python3 が開けずに終了コード 2 で終わると、Claude Code は Bash を全部拒否する）
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual(json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] if p.stdout.strip() else None,
                                 "deny" if denied else None)
        # -B: プラグインの置き場に __pycache__ を作らない（今は標準ライブラリしか読まないので、無くても作らない。文で固定する）
        self.assertIn("python3 -B", hooks[0]["command"])

    def test_the_hook_denies_a_push_when_only_a_queued_short_entry_name_is_in_the_transcript(self):
        # 非対話で短い名前で呼ぶと、指揮者が動いている間の会話記録は、打ったままの短い名前の入力を受け付けた行だけ。
        # hooks.json のコマンドを Claude Code と同じく sh で動かし、指揮者の素の push を拒否する
        hooks = json.loads(HOOKS.read_text(encoding="utf-8"))["hooks"]["PreToolUse"][0]["hooks"]
        t = self.transcript(BY_QUEUED_SHORT_SLASH)
        stdin = json.dumps({"tool_name": "Bash", "transcript_path": t, "tool_input": {"command": "git push origin main"}})
        p = subprocess.run(["sh", "-c", hooks[0]["command"]], input=stdin, capture_output=True, text=True, timeout=30,
                           env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(ROOT)})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.strip(), "短い名前の入力を受け付けた行だけの会話記録で、素の push が通った")
        self.assertEqual(json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_the_hook_denies_a_push_by_the_conductor(self):
        # hooks.json のコマンドを Claude Code と同じく sh で動かし、親の会話記録に印の無い指揮者の素の push を拒否する
        hooks = json.loads(HOOKS.read_text(encoding="utf-8"))["hooks"]["PreToolUse"][0]["hooks"]
        t = self.parent()
        self.child("s1", CONDUCTOR, entry_first_row())
        stdin = json.dumps({"tool_name": "Bash", "transcript_path": t, "agent_id": CONDUCTOR, "agent_type": "general-purpose",
                            "tool_input": {"command": "git push origin main"}})
        p = subprocess.run(["sh", "-c", hooks[0]["command"]], input=stdin, capture_output=True, text=True, timeout=30,
                           env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(ROOT)})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.strip(), "指揮者の素の push が通った")
        self.assertEqual(json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    # プラグインの更新の後: cache に写したプラグインの置き場はバージョンごとに変わる（cache/<marketplace>/<plugin>/<バージョン>/）。hook は新しいバージョンの
    # 置き場から動き、前のバージョンの置き場で始まった指揮者の先頭の行は前のバージョンの置き場を指す。
    # 親の会話記録に印は置かず、子の会話記録だけで決まる場面にする
    def cached_plugin(self, version) -> Path:
        """<一時>/cache/m/bb-writ/<バージョン> に、push_guard.py の写しを置いたプラグインの置き場を作り、その道筋を返す。"""
        place = Path(self.tmp.name) / "cache" / "m" / "bb-writ" / version
        (place / "scripts").mkdir(parents=True, exist_ok=True)
        (place / "scripts" / "push_guard.py").write_text(GUARD.read_text(encoding="utf-8"), encoding="utf-8")
        return place

    def hook_decision(self, plugin_root, transcript, agent_id):
        """hooks.json のコマンドを Claude Code と同じく sh で動かし（置き場は CLAUDE_PLUGIN_ROOT）、子の素の push の判定を返す。"""
        hooks = json.loads(HOOKS.read_text(encoding="utf-8"))["hooks"]["PreToolUse"][0]["hooks"]
        stdin = json.dumps({"tool_name": "Bash", "transcript_path": transcript, "agent_id": agent_id, "agent_type": "general-purpose",
                            "tool_input": {"command": "git push origin main"}})
        p = subprocess.run(["sh", "-c", hooks[0]["command"]], input=stdin, capture_output=True, text=True, timeout=30,
                           env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(plugin_root)})
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] if p.stdout.strip() else None

    def test_the_hook_denies_a_conductor_started_from_another_version_of_the_plugin(self):
        hook_root = self.cached_plugin("0.29.1")
        versions = Path(self.tmp.name) / "cache" / "m" / "bb-writ"
        t = self.parent()
        self.child("s1", "v1", entry_first_row(versions / "0.29.0" / "skills" / "bb-writ"), CONTINUED)
        self.child("s1", "v2", entry_first_row(versions / "1a2b3c4d5e6f" / "skills" / "bb-writ"), CONTINUED)
        self.child("s1", "v3", entry_first_row(versions / "0.29.0" / "skills" / "bb-writ", blocks=True), CONTINUED)
        message = "前のバージョンの置き場で始まった指揮者の素の push が通った"
        with self.subTest("前のバージョンの置き場がもう無い（14 日後に消えた後）"):
            self.assertFalse((versions / "0.29.0").exists())
            self.assertEqual(self.hook_decision(hook_root, t, "v1"), "deny", message)
        with self.subTest("バージョンの形でない名前の置き場（commit の SHA の 12 桁）"):
            self.assertEqual(self.hook_decision(hook_root, t, "v2"), "deny", message)
        with self.subTest("先頭の行が text のブロック"):
            self.assertEqual(self.hook_decision(hook_root, t, "v3"), "deny", message)
        (self.cached_plugin("0.29.0") / "skills" / "bb-writ").mkdir(parents=True)
        with self.subTest("前のバージョンの置き場が在る"):
            self.assertEqual(self.hook_decision(hook_root, t, "v1"), "deny", message)

    def test_the_hook_compares_another_version_by_real_paths(self):
        tmp = Path(self.tmp.name)
        hook_root = self.cached_plugin("0.29.1")
        (tmp / "link").symlink_to(tmp / "cache", target_is_directory=True)
        t = self.parent()
        self.child("s1", "l1", entry_first_row(tmp / "link" / "m" / "bb-writ" / "0.29.0" / "skills" / "bb-writ"))
        self.child("s1", "l2", entry_first_row(tmp / "cache" / "m" / "bb-writ" / "0.29.0" / "skills" / "bb-writ"))
        for name, root, aid in (("先頭の行の道筋が symlink 越し", hook_root, "l1"),
                                ("hook の置き場が symlink 越し", tmp / "link" / "m" / "bb-writ" / "0.29.1", "l2")):
            with self.subTest(name):
                self.assertEqual(self.hook_decision(root, t, aid), "deny", "symlink 越しの前のバージョンの置き場の指揮者の素の push が通った")

    def test_the_hook_denies_a_conductor_when_the_entry_folder_itself_is_a_symlink(self):
        # 置き場の skills/bb-writ 自体が別の場所への symlink: 実体の道筋は skills/bb-writ で終わらないが、同じ場所の指揮者は止める
        tmp = Path(self.tmp.name)
        hook_root = self.cached_plugin("0.29.1")
        target = tmp / "elsewhere" / "entry-body"
        target.mkdir(parents=True)
        (hook_root / "skills").mkdir()
        (hook_root / "skills" / "bb-writ").symlink_to(target, target_is_directory=True)
        t = self.parent()
        self.child("s1", "y1", entry_first_row(hook_root / "skills" / "bb-writ"))
        self.child("s1", "y2", entry_first_row(target))
        for name, aid in (("先頭の行が置き場の skills/bb-writ（symlink の道筋）", "y1"), ("先頭の行が symlink の行き先の道筋", "y2")):
            with self.subTest(name):
                self.assertEqual(self.hook_decision(hook_root, t, aid), "deny",
                                 "入口の skills/bb-writ が symlink の置き場で、指揮者の素の push が通った")

    def test_the_hook_denies_a_descendant_of_a_conductor_started_from_another_version(self):
        hook_root = self.cached_plugin("0.29.1")
        t = self.parent()
        self.child("s1", CONDUCTOR, entry_first_row(Path(self.tmp.name) / "cache" / "m" / "bb-writ" / "0.29.0" / "skills" / "bb-writ"))
        self.child("s1", "b0000000000000001", CONTINUED, parent_id=CONDUCTOR)
        self.assertEqual(self.hook_decision(hook_root, t, "b0000000000000001"), "deny",
                         "前のバージョンの置き場で始まった指揮者が立てた孫の素の push が通った")

    def test_the_hook_passes_children_of_places_that_are_not_another_version_of_the_plugin(self):
        cache = Path(self.tmp.name) / "cache"
        hook_root = self.cached_plugin("0.29.1")
        earlier = cache / "m" / "bb-writ" / "0.29.0"
        t = self.parent()
        cases = (("別の marketplace の下", cache / "m2" / "bb-writ" / "0.29.0" / "skills" / "bb-writ"),
                 ("別のプラグインの名前の下", cache / "m" / "other" / "0.29.0" / "skills" / "bb-writ"),
                 ("前のバージョンの本体の置き場（skills/conduct）", earlier / "skills" / "conduct"),
                 ("前のバージョンの skills/bb-writ-x", earlier / "skills" / "bb-writ-x"),
                 ("前のバージョンの skills/bb-writ/refs", earlier / "skills" / "bb-writ" / "refs"),
                 ("前のバージョンの skills の外の bb-writ", earlier / "bb-writ"),
                 ("前のバージョンの skills でない段の下の bb-writ", earlier / "other" / "bb-writ"),
                 ("1 段深い置き場", earlier / "x" / "skills" / "bb-writ"),
                 ("バージョンの親の skills/bb-writ", cache / "m" / "bb-writ" / "skills" / "bb-writ"),
                 ("親の名前に続きのある隣", cache / "m" / "bb-writ-x" / "0.29.0" / "skills" / "bb-writ"),
                 ("同じ marketplace とプラグインの名前の、別の根の下の置き場", Path(self.tmp.name) / "cache2" / "m" / "bb-writ" / "0.29.0" / "skills" / "bb-writ"))
        for i, (name, base) in enumerate(cases):
            self.child("s1", f"n{i}", entry_first_row(base), CONTINUED)
            with self.subTest(name):
                self.assertIsNone(self.hook_decision(hook_root, t, f"n{i}"), "入口のバージョンの兄弟でない置き場の子の push を止めた")
        # 対照: 同じ hook の置き場で、その置き場の skills/bb-writ で始まった指揮者は止める（hook が動いて比べている）
        self.child("s1", CONDUCTOR, entry_first_row(hook_root / "skills" / "bb-writ"))
        with self.subTest("対照: hook の置き場の skills/bb-writ"):
            self.assertEqual(self.hook_decision(hook_root, t, CONDUCTOR), "deny", "hook の置き場の指揮者の素の push が通った")


if __name__ == "__main__":
    unittest.main()
