"""launcher（scripts/launch.py。plan-v3 4.2）: 別系統の CLI の起動と、報告の検査。偽の CLI を PATH に置いて、成功と失敗の各場合を確かめる。"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCH = ROOT / "scripts" / "launch.py"
GOOD = 'ok.\\n```json\\n{"conclusion": "x は正しい", "evidence": [{"what": "読んだ", "where": "a.txt:1"}], "unknowns": []}\\n```'

FAKE_CODEX = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import json, os, sys
    open(os.environ["FAKE_ARGS"], "w").write(json.dumps(sys.argv[1:]))
    mode = os.environ.get("FAKE_MODE", "good")
    if mode == "crash":
        print("boom", file=sys.stderr); sys.exit(1)
    if mode == "quota":
        print("ERROR: You've hit your usage limit.", file=sys.stderr); sys.exit(1)
    if mode == "apierror":
        print(json.dumps({"type": "turn.failed", "error": {"message": "model is not supported"}})); sys.exit(1)
    text = {"good": os.environ["FAKE_TEXT"], "nojson": "結論だけ", "badpath": os.environ["FAKE_TEXT"].replace("a.txt", "missing.txt"),
            "absent": os.environ["FAKE_TEXT"].replace("a.txt:1", "missing.txt"),
            "nofence": os.environ["FAKE_TEXT"].rstrip("`").rstrip()}[mode]
    print(json.dumps({"type": "thread.started", "thread_id": "t-1"}))
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5}}))
''').lstrip()

FAKE_CLAUDE = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import json, os, sys
    open(os.environ["FAKE_ARGS"], "w").write(json.dumps(sys.argv[1:]))
    print(json.dumps({"type": "result", "result": os.environ["FAKE_TEXT"], "session_id": "s-1", "is_error": False,
                      "modelUsage": {"claude-sonnet-5-5": {}}, "total_cost_usd": 0.01, "num_turns": 2}))
''').lstrip()

FAKE_GROK = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import json, os, sys
    env = {k: v for k, v in os.environ.items() if k.startswith("GROK_")}
    open(os.environ["FAKE_ARGS"], "w").write(json.dumps(sys.argv[1:] + ["ENV=" + json.dumps(env, sort_keys=True)]))
    mode = os.environ.get("FAKE_MODE", "good")
    if mode == "signedout":
        print(json.dumps({"type": "error", "message": "Not signed in."})); sys.exit(1)
    if mode == "nosandbox":
        print("warning: sandbox profile could not be applied; continuing without enforcement", file=sys.stderr)
    print(json.dumps({"text": os.environ["FAKE_TEXT"], "stopReason": "end_turn", "sessionId": "g-1",
                      "modelUsage": {"grok-4.7-build": {}}, "total_cost_usd": 0.02, "num_turns": 3}))
''').lstrip()

FAKE_DOTENVX = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import os, sys
    args = sys.argv[1:]
    os.execvp(args[args.index("--") + 1], args[args.index("--") + 1:])
''').lstrip()


class TestLaunch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.bin, self.cfg, self.state, self.cwd = t / "bin", t / "cfg", t / "state", t / "repo"
        for d in (self.bin, self.cfg / "codex-home", self.cfg / "grok-auth", self.state, self.cwd):
            d.mkdir(parents=True)
        (self.cwd / "a.txt").write_text("x\n")
        (self.cfg / "codex-home" / "auth.json").write_text("{}")
        (self.cfg / "grok-auth" / "auth.json").write_text("{}")
        (self.cfg / ".env").write_text("X=1\n")
        (self.cfg / ".env.keys").write_text("K=1\n")
        for name, body in (("codex", FAKE_CODEX), ("claude", FAKE_CLAUDE), ("grok", FAKE_GROK), ("dotenvx", FAKE_DOTENVX)):
            (self.bin / name).write_text(body.replace("/usr/bin/env python3", sys.executable))
            (self.bin / name).chmod(0o755)
        self.prompt = t / "prompt.md"
        self.prompt.write_text("x を確かめて")
        self.args_file = t / "args.json"

    def tearDown(self):
        self.tmp.cleanup()

    def run_launch(self, host, mode="good", extra=(), path=None, cwd=None):
        env = {**os.environ, "PATH": path if path is not None else f"{self.bin}:/usr/bin:/bin", "BB_WRIT_CONFIG_DIR": str(self.cfg),
               "BB_WRIT_STATE_DIR": str(self.state), "FAKE_MODE": mode, "FAKE_ARGS": str(self.args_file),
               "FAKE_TEXT": GOOD.encode().decode("unicode_escape")}
        p = subprocess.run([sys.executable, str(LAUNCH), "--host", host, "--cwd", str(cwd or self.cwd), "--prompt-file", str(self.prompt), *extra],
                           env=env, capture_output=True, text=True)
        return p.returncode, json.loads(p.stdout)

    def test_codex_ok_is_read_only_by_default(self):
        rc, rec = self.run_launch("codex")
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        self.assertEqual(rec["session"], "t-1")
        argv = json.loads(self.args_file.read_text())
        self.assertIn("read-only", argv)
        self.assertIn("報告の形", argv[-1])  # 報告の約束を子に渡す
        self.assertTrue((self.state / "delegations.jsonl").read_text().strip())

    def test_claude_ok_and_read_only_uses_dont_ask(self):
        rc, rec = self.run_launch("claude")
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        self.assertEqual(rec["model_effective"], ["claude-sonnet-5-5"])
        argv = json.loads(self.args_file.read_text())
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertNotIn("Edit", argv)

    def test_write_changes_permissions(self):
        self.run_launch("codex", extra=["--write"])
        argv = json.loads(self.args_file.read_text())
        self.assertIn("--approve-for-me", argv)
        self.assertEqual(argv[argv.index('--add-dir') + 1], str(self.state))
        self.run_launch('claude', extra=['--write'])
        argv = json.loads(self.args_file.read_text())
        self.assertEqual(argv[argv.index('--add-dir') + 1], str(self.state))

    def grok_target(self):
        """Grok の read-only は /tmp に書けるので、読み取り専用の試験の対象は /tmp の外に置く。"""
        d = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(d.cleanup)
        (Path(d.name) / "a.txt").write_text("x\n")
        return Path(d.name)

    def grok_argv(self):
        argv = json.loads(self.args_file.read_text())
        return argv[:-1], json.loads(argv[-1][len("ENV="):])

    def test_grok_ok_is_read_only_and_isolated(self):
        rc, rec = self.run_launch("grok", cwd=self.grok_target())
        self.assertEqual((rc, rec["status"], rec["session"], rec["model_effective"]), (0, "ok", "g-1", ["grok-4.7-build"]), rec)
        argv, env = self.grok_argv()
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertEqual(env["GROK_HOME"], str(self.cfg / "grok-auth"))
        self.assertEqual(env["GROK_CLAUDE_AGENTS_ENABLED"], "false")  # 利用者の CLAUDE.md を子に読ませない
        self.assertEqual(env["GROK_CLAUDE_SKILLS_ENABLED"], "false")

    def test_grok_read_only_refuses_target_under_tmp(self):
        rc, rec = self.run_launch("grok", cwd=Path("/tmp"))
        self.assertEqual(rec["status"], "unavailable")
        self.assertIn("読み取り専用にできない", rec["problems"][0])
        self.assertFalse(self.args_file.exists())  # 起動しない

    def test_grok_write_grants_state_and_git_dirs(self):
        subprocess.run(["git", "init", "-q", str(self.cwd)], check=True)
        for _ in range(2):
            rc, rec = self.run_launch("grok", extra=["--write"])
            self.assertEqual(rec["status"], "ok", rec)
        argv, _ = self.grok_argv()
        name = argv[argv.index("--sandbox") + 1]
        toml = (self.cfg / "grok-auth" / "sandbox.toml").read_text()
        self.assertEqual(toml.count(f"[profiles.{name}]"), 1)  # 同じ書き込み先なら profile を重ねない
        self.assertIn(json.dumps(str(self.state.resolve())), toml)
        self.assertIn(json.dumps(str((self.cwd / ".git").resolve())), toml)

    def test_grok_sandbox_warning_is_failed(self):
        rc, rec = self.run_launch("grok", "nosandbox", cwd=self.grok_target())
        self.assertEqual((rc, rec["status"]), (2, "failed"))
        self.assertTrue(any("could not be applied" in x for x in rec["problems"]), rec)

    def test_grok_error_message_is_kept(self):
        rc, rec = self.run_launch("grok", "signedout", cwd=self.grok_target())
        self.assertEqual(rec["status"], "failed")
        self.assertIn("Not signed in.", rec["problems"])

    def test_crash_is_failed(self):
        rc, rec = self.run_launch("codex", "crash")
        self.assertEqual((rc, rec["status"]), (2, "failed"))
        self.assertIn("boom", rec["problems"])

    def test_quota_is_reported_not_retried_elsewhere(self):
        rc, rec = self.run_launch("codex", "quota")
        self.assertEqual(rec["status"], "quota")

    def test_report_without_json_is_invalid(self):
        rc, rec = self.run_launch("codex", "nojson")
        self.assertEqual((rc, rec["status"]), (2, "invalid-report"))

    def test_evidence_pointing_at_missing_file_is_invalid(self):
        rc, rec = self.run_launch("codex", "badpath")
        self.assertEqual(rec["status"], "invalid-report")
        self.assertIn("証拠のファイルが無い: missing.txt", rec["problems"])

    def test_absence_evidence_without_line_is_allowed(self):
        rc, rec = self.run_launch("codex", "absent")  # 「ファイルが無いこと」を示す証拠は不正でない
        self.assertEqual(rec["status"], "ok", rec)

    def test_report_without_closing_fence_is_read(self):
        rc, rec = self.run_launch("codex", "nofence")
        self.assertEqual(rec["status"], "ok", rec)

    def test_api_error_message_is_kept(self):
        rc, rec = self.run_launch("codex", "apierror")
        self.assertEqual(rec["status"], "failed")
        self.assertIn("model is not supported", rec["problems"])

    def test_missing_cli_or_login_is_unavailable(self):
        rc, rec = self.run_launch("codex", path="/usr/bin:/bin")
        self.assertEqual(rec["status"], "unavailable")
        (self.cfg / ".env.keys").unlink()
        rc, rec = self.run_launch("claude")
        self.assertEqual(rec["status"], "unavailable")
        self.assertIn("専用ログインが無い", rec["problems"][0])
        (self.cfg / "grok-auth" / "auth.json").unlink()
        rc, rec = self.run_launch("grok", cwd=self.grok_target())
        self.assertEqual(rec["status"], "unavailable")
        self.assertIn("GROK_HOME=", rec["problems"][0])

    def test_multiple_launches_keep_separate_evidence(self):
        _, a = self.run_launch('codex')
        _, b = self.run_launch('codex')
        self.assertNotEqual(a['out'], b['out'])
        self.assertTrue((Path(a['out']) / 'record.json').exists())
        self.assertTrue((Path(b['out']) / 'record.json').exists())

    def test_explicit_output_directory_is_not_overwritten(self):
        out = self.state / 'existing'
        self.run_launch('codex', extra=['--out', str(out)])
        record = (out / 'record.json').read_text()
        env = {**os.environ, 'BB_WRIT_STATE_DIR': str(self.state)}
        p = subprocess.run([sys.executable, str(LAUNCH), '--host', 'codex', '--cwd', str(self.cwd),
                            '--prompt-file', str(self.prompt), '--out', str(out)],
                           env=env, capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual((out / 'record.json').read_text(), record)


if __name__ == "__main__":
    unittest.main()
