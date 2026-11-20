"""入口を呼んだセッションで、関門（work.py op run）を通さない外へ出すコマンドを拒否する hook（scripts/push_guard.py）。

見ないこと: 会話記録の符号化の指定（encoding・errors="replace"）。不正なバイトのある記録で入口の印を読めるかは試験で固定しない。
git push の試すだけの指定は完全一致で比べる（短い引数をまとめた -nq・-nv の dry-run は、試すだけと読まずに止める。
AGENTS.md の誤って止める所）。
語に続く << の前で語を切ることは、崩れた入力でしか差が出ないので例を置かない。
読み違えを守らない書き方は、AGENTS.md の規則の表の行にある。"""
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
# 先頭に空白・改行のある行は、Claude Code がスラッシュの呼び出しとして扱わない（2026-11-20、2.1.328。親のモデルに普通の文として届いた）
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
           # 値を取る引数として読み飛ばして止める。誤って止める側（AGENTS.md の規則の表の誤って止める例）
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
           # AGENTS.md の規則の表の誤って止める例）
           "env -S x git push", "env --split-string x git push",
           # 終わりの語に引用符や \ を付けた heredoc の、終わりの後
           'cat <<"EOF"\nx\nEOF\ngit push', "cat <<\\EOF\nx\nEOF\ngit push",
           # 誤って止める所（守らない。AGENTS.md の規則の表）: gh の -h と、副コマンドの前の --help
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

    def run_guard(self, command, transcript, tool="Bash", raw=None):
        stdin = raw if raw is not None else json.dumps({"session_id": "s", "transcript_path": transcript, "cwd": self.tmp.name,
                                                        "hook_event_name": "PreToolUse", "tool_name": tool,
                                                        "tool_input": {"command": command}})
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

if __name__ == "__main__":
    unittest.main()
