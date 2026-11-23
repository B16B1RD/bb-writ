"""入口を呼んだセッションで、hook が発火するたびに（利用者の入力のほか、指揮者の終わりの知らせ・子からの伝言でも）、指揮者への返事と
続きは、親が進めず・渡さずに、利用者が指揮者に直接打つよう案内する hook（scripts/conductor_hint.py）。

見ないこと: 案内の文面（どの言い回しで親が従うかは、実物で数える）。additionalContext が空でない文であることだけを見る。
会話記録の行の材料は tests/test_push_guard.py のものを使う（入口の印の読み方は push_guard.py の entry_in と同じなので、写しを作らない）。
探索の入口 unknowns の印（conductor_hint.py の used_unknowns）は、この試験の中で組んだ行で見る。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent)); import process_limit  # noqa: E402,F401  試験に上限を掛ける（直に走らせても）
from test_push_guard import (  # noqa: E402
    BODY_BARE_NAME, BODY_NAME_HEADS_A_QUEUED_ROW, BROKEN_ROW, BY_BARE_NAME, BY_BODY_SKILL_TOOL, BY_BODY_SLASH,
    BY_BODY_SLASH_IN_TEXT_BLOCK,
    BY_FORKED_SLASH, BY_FORKED_SLASH_ALONE, BY_FORKED_SLASH_IN_TEXT_BLOCK, BY_QUEUED_FORKED_SLASH, BY_QUEUED_SHORT_SLASH,
    BY_SKILL_TOOL, BY_SLASH,
    BY_SLASH_IN_TEXT_BLOCK, MARK_HEADS_ASSISTANT_BLOCK, MARK_IN_ASSISTANT_STRING, MARK_IN_TOOL_RESULT, MARK_INSIDE_USER_BLOCK,
    MARK_INSIDE_USER_TEXT, MARK_QUOTED_BY_ASSISTANT, MENTION_ONLY, NAME_AFTER_A_NEWLINE, NAME_AFTER_A_SPACE,
    NAME_HEADS_ANOTHER_KIND_OF_ROW, NAME_HEADS_ASSISTANT_BLOCK, NAME_HEADS_ASSISTANT_STRING, NAME_IN_THE_MIDDLE, NOT_SKILL_TOOL,
    NULL_ROWS, OTHER_NAME_WITH_A_HYPHEN, OTHER_NAME_WITH_THE_SAME_HEAD, OTHER_PLUGIN_BODY_SLASH, OTHER_SKILL,
    OTHER_SLASH_WITH_MARK, QUEUED_NOTIFICATION, QUEUED_OTHER_NAME, SKILL_NAME_NOT_TOOL_USE, SLASH_WITH_SPACES)

ROOT = Path(__file__).resolve().parents[1]
HINT_SCRIPT = ROOT / "scripts" / "conductor_hint.py"
HOOKS = ROOT / "hooks" / "hooks.json"
PROMPT = "A で"  # 指揮者の質問への返事のような、ふつうの入力


class TestConductorHint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def transcript(self, *rows) -> str:
        p = Path(self.tmp.name) / "session.jsonl"
        p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows) + '{"broken\n')
        return str(p)

    def stdin(self, transcript, prompt=PROMPT, **extra) -> str:
        return json.dumps({"session_id": "s", "transcript_path": transcript, "cwd": self.tmp.name, "permission_mode": "default",
                           "hook_event_name": "UserPromptSubmit", "prompt": prompt, **extra})

    def run_hint(self, stdin, env=None):
        """hook を子プロセスで動かし、足した文脈の JSON（無ければ None）を返す。0 で終わり、stderr に何も出さないことも見る。"""
        p = subprocess.run([sys.executable, "-B", str(HINT_SCRIPT)], input=stdin, capture_output=True, text=True, timeout=30,
                           env=env)
        self.assertEqual(p.returncode, 0, p.stderr)  # 0 以外では、Claude Code が利用者に失敗を見せる（2 なら入力を止める）
        self.assertEqual(p.stderr, "")  # traceback を出さない
        return json.loads(p.stdout) if p.stdout.strip() else None

    def hinted(self, *rows, prompt=PROMPT, **extra) -> bool:
        return self.run_hint(self.stdin(self.transcript(*rows), prompt, **extra)) is not None

    def test_a_session_that_called_only_the_body_gets_no_hint(self):  # R1
        # 本体を直に呼んだセッション（続きの引き受け・要約の後）には、入口が立てる指揮者（背景の子）がいない
        for name, rows in (("本体の Skill ツール", (BY_BODY_SKILL_TOOL,)), ("本体のスラッシュ", (BY_BODY_SLASH,)),
                           ("本体のスラッシュ（text ブロック）", (BY_BODY_SLASH_IN_TEXT_BLOCK,)),
                           ("本体の名前で始まる入力を受け付けた行", (BODY_NAME_HEADS_A_QUEUED_ROW,)),
                           ("本体の短い名前", (BODY_BARE_NAME,)), ("ほかのプラグインの本体", (OTHER_PLUGIN_BODY_SLASH,)),
                           ("本体の印を全部", (BY_BODY_SKILL_TOOL, BY_BODY_SLASH, BODY_NAME_HEADS_A_QUEUED_ROW))):
            with self.subTest(name):
                self.assertFalse(self.hinted(*rows), "本体だけを呼んだセッションに案内が付いた")

    def test_a_prompt_to_a_child_agent_gets_no_hint(self):  # R2
        # agent_id のある入力は、親でなく子（指揮者など）への入力。指揮者の依頼文に案内が混ざらないように足さない
        for agent_id in ("a1b2c3", "conductor"):
            with self.subTest(agent_id=agent_id):
                self.assertFalse(self.hinted(BY_SKILL_TOOL, agent_id=agent_id, agent_type="general-purpose"),
                                 "子への入力に案内が付いた")
        with self.subTest("agent_id だけ"):
            self.assertFalse(self.hinted(BY_SKILL_TOOL, agent_id="a1b2c3"), "agent_id だけの子への入力に案内が付いた")
        with self.subTest("agent_type だけ"):  # --agent で起こした親のセッション（agent_id は付かない）
            self.assertTrue(self.hinted(BY_SKILL_TOOL, agent_type="reviewer"), "agent_type だけの親の入力に案内が付かなかった")
        with self.subTest("agent_id が空"):  # 空は親の入力と見る
            self.assertTrue(self.hinted(BY_SKILL_TOOL, agent_id=""), "agent_id が空の親の入力に案内が付かなかった")

    def test_a_prompt_that_calls_the_entry_by_slash_gets_no_hint(self):  # R3
        # 入口をスラッシュで呼ぶ入力は、これから指揮者を立てる入力（短い名前の /bb-writ でも、prompt は打ったままで届く）
        for prompt in ("/bb-writ:bb-writ 直して", "/bb-writ:bb-writ", "/bb-writ:bb-writ\n続きを",
                       "/bb-writ 直して", "/bb-writ", "/bb-writ\t直して", "/bb-writ\n直して", "/bb-writ\u3000直して"):
            with self.subTest(prompt=prompt):
                self.assertFalse(self.hinted(BY_FORKED_SLASH, prompt=prompt), "入口をスラッシュで呼ぶ入力に案内が付いた")
        # 名前の前方一致、ほかのコマンド、/ で始まる普通の返事（パス・案の名前）、文の途中の名前には付ける。先頭に空白のある形は、
        # 2.1.329 では Claude Code が先頭の空白を削った prompt を hook に渡すので届かない。届いたときは、入口の呼び出しと見ずに足す
        for prompt in ("/bb-writer 直して", "/bb-writ:bb-writer 直して", "/bb-writ:conduct 続きを", "/compact",
                       "/tmp に保存して", "/README.md を編集して", "/A で進めて", "/案A で", "/tmp/x に保存して", "/",
                       "さっきの /bb-writ の続き", " /bb-writ 直して"):
            with self.subTest(prompt=prompt):
                self.assertTrue(self.hinted(BY_FORKED_SLASH, prompt=prompt), "入口をスラッシュで呼ぶ入力でないのに案内が付かなかった")

    def test_a_session_without_the_entry_gets_no_hint(self):  # R4
        for name, rows in (("空の会話記録", ()), ("ほかのスキル", (OTHER_SKILL,)), ("名前に触れただけ", (MENTION_ONLY,)),
                           ("形の違う行だけ", (BROKEN_ROW, *NULL_ROWS))):
            with self.subTest(name):
                self.assertFalse(self.hinted(*rows), "入口を呼んでいないセッションに案内が付いた")

    def test_copies_of_the_entry_name_are_not_counted(self):  # R5
        # 書き手の本文・道具の結果・ほかのスキルの引数・文の途中・名前の前方一致に写った入口の名前は、入口を呼んだ印ではない
        for rows in ((NOT_SKILL_TOOL,), (MARK_IN_TOOL_RESULT,), (MARK_QUOTED_BY_ASSISTANT,), (MARK_IN_ASSISTANT_STRING,),
                     (MARK_INSIDE_USER_TEXT,), (MARK_INSIDE_USER_BLOCK,), (OTHER_SLASH_WITH_MARK,), (MARK_HEADS_ASSISTANT_BLOCK,),
                     (SKILL_NAME_NOT_TOOL_USE,), (OTHER_NAME_WITH_THE_SAME_HEAD,), (OTHER_NAME_WITH_A_HYPHEN,),
                     (NAME_IN_THE_MIDDLE,), (NAME_HEADS_ASSISTANT_STRING,), (NAME_HEADS_ASSISTANT_BLOCK,),
                     (QUEUED_NOTIFICATION,), (QUEUED_OTHER_NAME,), (NAME_HEADS_ANOTHER_KIND_OF_ROW,),
                     (NAME_AFTER_A_SPACE,), (NAME_AFTER_A_NEWLINE,)):
            with self.subTest(first=json.dumps(rows[0], ensure_ascii=False)[:80]):
                self.assertFalse(self.hinted(*rows), "写った入口の名前を、入口を呼んだ印に数えた")

    def test_a_queued_entry_slash_counts(self):  # R6
        # 非対話の実行で、指揮者が動いている間に在るのは、入力を受け付けた行（queue-operation）だけ
        self.assertTrue(self.hinted(OTHER_SKILL, BROKEN_ROW, *NULL_ROWS, BY_QUEUED_FORKED_SLASH),
                        "入力を受け付けた行の入口の印を数えなかった")

    def test_a_queued_short_entry_slash_counts(self):
        # 非対話で短い名前 /bb-writ で呼ぶと、指揮者が動いている間に在るのは、打ったままの短い名前の入力を受け付けた行だけ
        self.assertTrue(self.hinted(OTHER_SKILL, BROKEN_ROW, *NULL_ROWS, BY_QUEUED_SHORT_SLASH),
                        "短い名前の入力を受け付けた行の入口の印を数えなかった")

    def test_a_forked_entry_slash_at_the_head_of_a_user_row_counts(self):  # R7
        # fork する入口をスラッシュで呼ぶと、利用者の行の本文の先頭が「/bb-writ:bb-writ 引数」のままになる
        for name, row in (("文字列", BY_FORKED_SLASH), ("引数なし", BY_FORKED_SLASH_ALONE),
                          ("text ブロック", BY_FORKED_SLASH_IN_TEXT_BLOCK)):
            with self.subTest(name):
                self.assertTrue(self.hinted(OTHER_SKILL, BROKEN_ROW, *NULL_ROWS, row), "利用者の行の先頭の入口のスラッシュを数えなかった")

    def test_both_skill_tool_names_of_the_entry_count(self):  # R8
        for name, row in (("bb-writ:bb-writ", BY_SKILL_TOOL), ("短い名前 bb-writ", BY_BARE_NAME)):
            with self.subTest(name):
                self.assertTrue(self.hinted(OTHER_SKILL, BROKEN_ROW, *NULL_ROWS, row), "Skill ツールの入口の呼び出しを数えなかった")

    def test_a_recorded_entry_slash_counts(self):
        # <command-name> の形のスラッシュの記録（前後の空白、text ブロックも）
        for name, row in (("文字列", BY_SLASH), ("text ブロック", BY_SLASH_IN_TEXT_BLOCK), ("前後の空白", SLASH_WITH_SPACES)):
            with self.subTest(name):
                self.assertTrue(self.hinted(OTHER_SKILL, BROKEN_ROW, *NULL_ROWS, row), "スラッシュの記録の入口の印を数えなかった")

    def test_a_session_that_called_unknowns_gets_the_unknowns_hint_but_is_not_an_entry_session(self):
        """探索の入口 unknowns も別の子として動くので、呼んだセッションには、その子への返事のしかたの案内を足す（#58）。
        入口の案内とは別の文で、素の push の守りの「入口を呼んだセッション」には数えない。unknowns をスラッシュで呼ぶ入力には足さない。"""
        sys.path.insert(0, str(ROOT / "scripts")); import push_guard  # noqa: E402
        unknowns_tool = {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_u", "name": "Skill", "input": {"skill": "bb-writ:unknowns"}}]}}
        unknowns_slash = {"type": "user", "message": {"role": "user", "content":
            "<command-message>bb-writ:unknowns</command-message>\n<command-name>/bb-writ:unknowns</command-name>\n<command-args>題</command-args>"}}
        raw = {"type": "user", "message": {"role": "user", "content": "/bb-writ:unknowns 題"}}  # fork するスキルをスラッシュで呼んだ形
        raw_block = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "/bb-writ:unknowns\n題"}]}}
        queued = {"type": "queue-operation", "operation": "enqueue", "content": "/bb-writ:unknowns 題"}
        quoted = {"type": "assistant", "message": {"role": "assistant", "content": "/bb-writ:unknowns を呼ぶとよい"}}  # 書き手の文は数えない
        self.assertIsNone(self.run_hint(self.stdin(self.transcript(OTHER_SKILL, quoted))))
        for name, row in (("入口だけ（スラッシュの素の行）", BY_FORKED_SLASH), ("入口だけ（Skill ツール）", BY_SKILL_TOOL)):
            with self.subTest(name):  # 入口だけのセッションに、unknowns の案内を足さない
                ctx = self.run_hint(self.stdin(self.transcript(OTHER_SKILL, row)))["hookSpecificOutput"]["additionalContext"]
                self.assertNotIn("unknowns", ctx)
        for name, row in (("Skill ツール", unknowns_tool), ("スラッシュの記録", unknowns_slash), ("素の行", raw),
                          ("素の行の text ブロック", raw_block), ("受け付けた行", queued)):
            with self.subTest(name):
                path = self.transcript(OTHER_SKILL, row)
                got = self.run_hint(self.stdin(path))
                self.assertIsNotNone(got, "unknowns を呼んだセッションに案内が無い")
                ctx = got["hookSpecificOutput"]["additionalContext"]
                self.assertIn("unknowns", ctx)
                self.assertNotIn("入口 bb-writ を呼んでいる", ctx)
                self.assertFalse(push_guard.used_entry(path))  # 素の push の守りは、unknowns だけのセッションを止めない
                for prompt in ("/bb-writ:unknowns 次の題", "/unknowns", "/bb-writ:explore 題"):  # 入口と、失敗の後に本体を呼ぶ回避
                    self.assertIsNone(self.run_hint(self.stdin(path, prompt)), prompt)

    def test_input_it_cannot_judge_ends_quietly(self):  # R9
        for name, raw in (("JSON でない", "A で"), ("空", ""), ("JSON の配列", "[1, 2]"),
                          ("会話記録の欄が無い", json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": PROMPT})),
                          ("会話記録の欄が null", self.stdin(None)),
                          ("会話記録が無い", self.stdin(str(Path(self.tmp.name) / "none"))),
                          ("会話記録がディレクトリ", self.stdin(self.tmp.name)),
                          ("content が None の行だけ", self.stdin(self.transcript(*NULL_ROWS)))):
            with self.subTest(name):
                self.assertIsNone(self.run_hint(raw), "判定できない入力に案内が付いた")
        # prompt が文字列でない・無い入力は、入口をスラッシュで呼ぶ形でないと見る（印があれば案内を付ける。落ちて案内が消えない）
        t = self.transcript(BY_SKILL_TOOL)
        for name, raw in (("prompt が null", self.stdin(t, None)), ("prompt が配列", self.stdin(t, ["/bb-writ:bb-writ"])),
                          ("prompt が数", self.stdin(t, 1)),
                          ("prompt が無い", json.dumps({"hook_event_name": "UserPromptSubmit", "transcript_path": t}))):
            with self.subTest(name):
                self.assertIsNotNone(self.run_hint(raw), "prompt が文字列でない入力で案内が消えた")
        with self.subTest("content が None の行の後ろの印"):  # 形の違う行は飛ばし、後ろの行を読み続ける
            self.assertIsNotNone(self.run_hint(self.stdin(self.transcript(*NULL_ROWS, BY_SKILL_TOOL))),
                                 "形の違う行で読むのを止め、後ろの入口の印を数えなかった")

    def test_the_hint_is_additional_context_json(self):  # R10
        p = subprocess.run([sys.executable, "-B", str(HINT_SCRIPT)], input=self.stdin(self.transcript(BY_SKILL_TOOL)),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(p.stdout.splitlines()), 1, "1 行の JSON でない")
        try:
            out = json.loads(p.stdout)
        except ValueError:
            self.fail(f"出力が JSON でない: {p.stdout[:80]!r}")
        self.assertEqual(list(out), ["hookSpecificOutput"])  # decision などを付けない（入力を止めない）
        self.assertEqual(sorted(out["hookSpecificOutput"]), ["additionalContext", "hookEventName"])
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertIsInstance(context, str)
        self.assertTrue(context.strip(), "足す文が空")

    def test_the_hint_survives_an_output_encoding_without_japanese(self):  # R11
        # 案内の文は日本語。出力の符号化が日本語を扱えない環境で、書き出しが落ちて案内が消えない
        stdin = self.stdin(self.transcript(BY_SKILL_TOOL))
        for enc in ("ascii", "cp1252"):
            with self.subTest(enc=enc):
                out = self.run_hint(stdin, env={**os.environ, "PYTHONIOENCODING": enc})
                self.assertIsNotNone(out, "案内が消えた")
                self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")

    def test_the_hint_comes_with_a_safe_sys_path(self):
        # PYTHONSAFEPATH では、スクリプトの置き場が sys.path に入らない。同じ置き場の push_guard を読めずに案内が消えない
        out = self.run_hint(self.stdin(self.transcript(BY_SKILL_TOOL)), env={**os.environ, "PYTHONSAFEPATH": "1"})
        self.assertIsNotNone(out, "PYTHONSAFEPATH で案内が消えた")

    def test_the_mark_counts_in_any_shape_of_transcript(self):
        mark = json.dumps(BY_SKILL_TOOL, ensure_ascii=False).encode("utf-8")
        other = json.dumps(OTHER_SKILL, ensure_ascii=False).encode("utf-8") + b"\n"
        for name, body in (("印の前に UTF-8 として不正なバイト列の行", b'{"x": "bb-writ \xff\xfe"}\n' + mark + b"\n"),
                           ("印が最後の行で、改行で終わらない", other + mark),
                           ("印の前に、印でない行が 5,000 行", other * 5000 + mark + b"\n")):
            with self.subTest(name):
                p = Path(self.tmp.name) / "raw.jsonl"
                p.write_bytes(body)
                self.assertIsNotNone(self.run_hint(self.stdin(str(p))), "会話記録の入口の印を数えなかった")

    def test_the_hook_definition_has_only_the_known_fields(self):
        # matcher・if などの欄を置かない（UserPromptSubmit は matcher を持たない。if は Grok Build が handler を飛ばす）
        entries = json.loads(HOOKS.read_text(encoding="utf-8"))["hooks"]["UserPromptSubmit"]
        self.assertEqual([sorted(e) for e in entries], [["hooks"]], "UserPromptSubmit の要素に hooks 以外の欄がある")
        self.assertEqual([sorted(h) for h in entries[0]["hooks"]], [["command", "timeout", "type"]],
                         "hook に type・command・timeout 以外の欄がある")

    def plugin_copy(self, name: str) -> Path:
        """hook が読むスクリプト（conductor_hint.py と、それが読む push_guard.py）を写したプラグインの置き場。"""
        root = Path(self.tmp.name) / name
        (root / "scripts").mkdir(parents=True)
        for s in ("conductor_hint.py", "push_guard.py"):
            shutil.copy(ROOT / "scripts" / s, root / "scripts" / s)
        return root

    def shell_env(self, **extra) -> dict:
        """hook のコマンドをシェルで動かす環境。利用者のホームと設定に左右されないように、HOME を試験の一時の置き場にし、
        BASH_ENV・ENV（非対話のシェルが読む起動の文）を渡さない（読めない .bashrc の診断が stderr に出て、確かめに当たった）。"""
        home = Path(self.tmp.name) / "home"
        home.mkdir(exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k not in ("BASH_ENV", "ENV")}
        return {**env, "HOME": str(home), **extra}

    def hook_command(self) -> str:
        cfg = json.loads(HOOKS.read_text(encoding="utf-8"))
        self.assertIn("UserPromptSubmit", cfg["hooks"], "hooks.json に UserPromptSubmit の hook が無い")
        entries = cfg["hooks"]["UserPromptSubmit"]
        self.assertEqual(len(entries), 1)
        hooks = entries[0]["hooks"]
        self.assertEqual([h["type"] for h in hooks], ["command"])
        self.assertLessEqual(hooks[0].get("timeout", 600), 30)
        return hooks[0]["command"]

    def test_the_hook_runs_on_each_prompt_and_ends_quietly_when_the_script_is_missing(self):  # R12
        command = self.hook_command()
        stdin = self.stdin(self.transcript(BY_SKILL_TOOL))
        unreadable = Path(self.tmp.name) / "unreadable"  # 在るが読めないスクリプト
        (unreadable / "scripts").mkdir(parents=True)
        (unreadable / "scripts" / "conductor_hint.py").write_text("", encoding="utf-8")
        (unreadable / "scripts" / "conductor_hint.py").chmod(0)
        self.addCleanup((unreadable / "scripts" / "conductor_hint.py").chmod, 0o600)
        # Claude Code と同じく、command を sh で動かし、プラグインの置き場を CLAUDE_PLUGIN_ROOT で渡す
        for name, root, hinted in (("置き場がある", ROOT, True), ("空白のある置き場", self.plugin_copy("plugin root"), True),
                                   ("置き場が消えた", Path(self.tmp.name) / "gone", False), ("置き場が空", "", False),
                                   *((("スクリプトが読めない", unreadable, False),) if os.geteuid() != 0 else ())):  # root は読めてしまう
            with self.subTest(name):
                p = subprocess.run(["sh", "-c", command], input=stdin, capture_output=True, text=True, timeout=30,
                                   env=self.shell_env(CLAUDE_PLUGIN_ROOT=str(root)))
                # 2 で終わると、Claude Code は利用者の入力を止め、ほかの 0 以外は利用者に失敗が見える。案内だけの hook なので、
                # python3 を動かせない・スクリプトを読めないときは、何も出さずに 0 で終わる（失敗は案内が無いことと同じに扱う。
                # スクリプトが先に stdout へ出した分は残る）
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual(p.stderr, "")
                self.assertEqual(json.loads(p.stdout)["hookSpecificOutput"]["hookEventName"] if p.stdout.strip() else None,
                                 "UserPromptSubmit" if hinted else None)
        # python3 を動かせない環境（無い、127 を返す shim、2 を返す実行体、実行権限の無いファイル）でも、何も出さずに 0 で終わる
        bins = {}
        for name, body, mode in (("無い", None, None), ("127 を返す shim", "#!/bin/sh\necho 'python3: not installed' >&2\nexit 127\n", 0o755),
                                 ("2 を返す実行体", "#!/bin/sh\necho 'python3: broken' >&2\nexit 2\n", 0o755),
                                 ("実行権限の無いファイル", "#!/bin/sh\nexit 0\n", 0o644)):
            d = Path(self.tmp.name) / f"bin-{len(bins)}"
            d.mkdir()
            if body is not None:
                (d / "python3").write_text(body, encoding="utf-8")
                (d / "python3").chmod(mode)
            bins[name] = d
        shells = [s for s in (shutil.which("sh"), shutil.which("bash")) if s]
        for shell in shells:
            for name, d in bins.items():
                with self.subTest(shell=shell, python3=name):
                    p = subprocess.run([shell, "-c", command], input=stdin, capture_output=True, text=True, timeout=30,
                                       env=self.shell_env(PATH=str(d), CLAUDE_PLUGIN_ROOT=str(ROOT)))
                    self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""),
                                     "python3 を動かせない環境で、hook が 0 以外か出力で終わった")

    def test_the_hook_does_not_write_pycache_into_the_plugin(self):  # R13
        # conductor_hint.py は push_guard.py を import する。-B が無いと、プラグインの置き場に __pycache__ ができる
        root = self.plugin_copy("plugin")
        env = {k: v for k, v in self.shell_env(CLAUDE_PLUGIN_ROOT=str(root)).items()
               if k not in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX")}
        p = subprocess.run(["sh", "-c", self.hook_command()], input=self.stdin(self.transcript(BY_SKILL_TOOL)),
                           capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.strip(), "hook が案内を出さなかった（試験の準備の誤り）")
        self.assertEqual(sorted(x.name for x in (root / "scripts").iterdir()), ["conductor_hint.py", "push_guard.py"],
                         "プラグインの置き場に __pycache__ ができた")


if __name__ == "__main__":
    unittest.main()
