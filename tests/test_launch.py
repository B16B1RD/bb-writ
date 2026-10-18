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
    open(os.environ["FAKE_ARGS"] + ".scrub", "w").write(os.environ.get("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB", ""))
    os.makedirs(".claude/.cc-writes", exist_ok=True)  # 2.1.302 の --restricted が対象に作る空のディレクトリ
    import subprocess
    mode = os.environ.get("FAKE_MODE")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    common = lambda: subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], capture_output=True, text=True).stdout.strip()
    if mode == "own-commit":  # 自分のブランチに commit するのは正当。sandbox が作る空の config.worktree も残る
        gd = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-dir"], capture_output=True, text=True).stdout.strip()
        open(os.path.join(gd, "config.worktree"), "w").close()
        open("b.txt", "w").write("b"); subprocess.run(git + ["add", "b.txt"]); subprocess.run(git + ["commit", "-qm", "b"])
    if mode == "tamper-main":  # 統合先を動かす
        evil = subprocess.run(git + ["commit-tree", "-m", "x", "HEAD^{tree}"], capture_output=True, text=True).stdout.strip()
        subprocess.run(git + ["update-ref", "refs/heads/main", evil])
    if mode == "tamper-config":  # 親の git で動く設定を足す
        open(os.path.join(common(), "config"), "a").write("[core]\n\tfsmonitor = touch /tmp/pwned\n")
    if mode == "tamper-gitfile":  # 作業場所の .git を偽のディレクトリへ向け直す
        open(".git", "w").write("gitdir: /tmp/fake-git\n")
    if os.environ.get("FAKE_MODE") == "wrote":
        open(".claude/.cc-writes/x", "w").write("x")
    if os.environ.get("FAKE_MODE") == "checkout":  # worktree の子は、元の checkout にも作る
        import subprocess
        common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], capture_output=True, text=True).stdout.strip()
        os.makedirs(os.path.join(os.path.dirname(common), ".claude", ".cc-writes"), exist_ok=True)
    print(json.dumps({"type": "result", "result": os.environ["FAKE_TEXT"], "session_id": "s-1", "is_error": False,
                      "modelUsage": {"claude-sonnet-5-5": {}}, "total_cost_usd": 0.01, "num_turns": 2}))
''').lstrip()

FAKE_GROK = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import json, os, sys
    env = {k: v for k, v in os.environ.items() if k.startswith("GROK_") or k in ("XAI_API_KEY", "RUST_LOG")}
    open(os.environ["FAKE_ARGS"], "w").write(json.dumps(sys.argv[1:] + ["ENV=" + json.dumps(env, sort_keys=True)]))
    mode = os.environ.get("FAKE_MODE", "good")
    if mode == "signedout":
        print(json.dumps({"type": "error", "message": "Not signed in."})); sys.exit(1)
    if mode == "nosandbox":
        print("warning: sandbox could not be applied: landlock unavailable", file=sys.stderr)  # 1.0.63 の実物の文言
    if mode == "leadernote":
        print("note: sandbox profile 'x' was requested, so leader mode is off", file=sys.stderr)
    if mode == "refresh":  # トークンの更新で auth.json を置き換える
        auth = os.path.join(os.environ["GROK_HOME"], "auth.json")
        os.unlink(auth)
        open(auth, "w").write('{"refreshed": true}')
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

    def run_launch(self, host, mode="good", extra=(), path=None, cwd=None, env_extra=None):
        env = {**os.environ, "PATH": path if path is not None else f"{self.bin}:/usr/bin:/bin", "BB_WRIT_CONFIG_DIR": str(self.cfg),
               "BB_WRIT_STATE_DIR": str(self.state), "FAKE_MODE": mode, "FAKE_ARGS": str(self.args_file),
               "FAKE_TEXT": GOOD.encode().decode("unicode_escape"), **(env_extra or {})}
        p = subprocess.run([sys.executable, str(LAUNCH), "--host", host, "--cwd", str(cwd or self.cwd), "--prompt-file", str(self.prompt), *extra],
                           env=env, capture_output=True, text=True)
        return p.returncode, json.loads(p.stdout)

    def test_launch_leaves_no_bytecode_in_the_plugin(self):
        subprocess.run(["rm", "-rf", str(ROOT / "scripts" / "__pycache__")], check=True)
        self.run_launch("codex")  # 試験の外と同じく -B なしで起動する
        self.assertFalse((ROOT / "scripts" / "__pycache__").exists())

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
        # 対象の .claude の hook・許可規則を読まない。書けるツールを渡さない
        self.assertIn("--restricted", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob,Bash,Skill")
        self.assertEqual(argv[argv.index("--plugin-dir") + 1], str(ROOT))  # --restricted でも bb-writ のスキルが届く
        # git の --output などの書き込みは OS の sandbox で止め、sandbox が無ければ起動しない。専用ログインの置き場は読ませない
        sandbox = json.loads(argv[argv.index("--settings") + 1])["sandbox"]
        self.assertEqual((sandbox["enabled"], sandbox["allowUnsandboxedCommands"], sandbox["failIfUnavailable"]), (True, False, True))
        fs = sandbox["filesystem"]
        self.assertTrue({str(self.cwd.resolve()), str(Path.home())} <= set(fs["denyWrite"]))
        for secret in (".config", ".ssh", ".claude", ".codex", ".grok"):  # gh のトークン・dotenvx の鍵・各 CLI のログイン
            self.assertIn(str(Path.home() / secret), fs["denyRead"])
        self.assertIn("AGENTS.md・CLAUDE.md", argv[argv.index("-p") + 1])  # 自動で読まれない対象の指示を、資料として読ませる

    def record_of(self, d):
        """子が work.py claim・status で受け取る、その作業場所の記録の置き場。"""
        env = {**os.environ, "BB_WRIT_STATE_DIR": str(self.state)}
        p = subprocess.run([sys.executable, str(ROOT / "scripts" / "work.py"), "status", "--dir", str(d)], env=env, capture_output=True, text=True)
        return str(Path(json.loads(p.stdout)["note"]["path"]).parent)

    def test_write_grants_only_the_work_record(self):
        # 書く子に state 全体（タスクの一覧・統合の記録・他の worktree）を渡さない
        self.run_launch("codex", extra=["--write"])
        argv = json.loads(self.args_file.read_text())
        self.assertIn("--approve-for-me", argv)
        self.assertEqual(argv[argv.index('--add-dir') + 1], self.record_of(self.cwd))
        self.run_launch('claude', extra=['--write'])
        argv = json.loads(self.args_file.read_text())
        self.assertEqual(argv[argv.index('--add-dir') + 1], self.record_of(self.cwd))
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "auto")
        self.assertIn("--restricted", argv)
        sandbox = json.loads(argv[argv.index("--settings") + 1])["sandbox"]
        self.assertEqual((sandbox["enabled"], sandbox["allowUnsandboxedCommands"], sandbox["failIfUnavailable"]), (True, False, True))
        self.assertEqual(sandbox["network"], {"allowedDomains": ["*"]})  # 書く子は依存の取得などで外へ出られる
        self.assertIn(str(self.cfg), sandbox["filesystem"]["denyRead"])  # 専用ログインと鍵は読ませない

    def test_claude_leaves_no_empty_cc_writes_in_target(self):
        self.run_launch("claude")
        self.assertFalse((self.cwd / ".claude").exists())  # この起動で新しくできた空のディレクトリは消す
        (self.cwd / ".claude").mkdir()
        self.run_launch("claude", "wrote")
        self.assertTrue((self.cwd / ".claude" / ".cc-writes" / "x").exists())  # 中身があれば消さない

    def test_claude_in_worktree_leaves_no_empty_cc_writes_in_checkout(self):
        repo, wt = self.cwd / "repo", self.cwd / "wt"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "i"], check=True)
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(wt)], check=True)
        (wt / "a.txt").write_text("x\n")
        self.run_launch("claude", "checkout", extra=["--write"], cwd=wt)
        self.assertEqual([p.name for p in (repo, wt) if (p / ".claude").exists()], [])

    def worktree(self, name="r"):
        repo, wt = self.cwd / name, self.cwd / (name + "-wt")
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "i"], check=True)
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "bb-writ/t1", str(wt)], check=True)
        (wt / "a.txt").write_text("x\n")
        return repo, wt

    def test_write_refuses_the_original_checkout(self):
        repo, wt = self.worktree()
        for cwd in (repo, repo / "sub"):
            cwd.mkdir(exist_ok=True)
            rc, rec = self.run_launch("claude", extra=["--write"], cwd=cwd)
            self.assertEqual(rec["status"], "unavailable", rec)
        self.assertFalse(self.args_file.exists())  # 起動しない

    def test_write_child_may_commit_only_to_its_own_branch(self):
        _, wt = self.worktree("own")
        rc, rec = self.run_launch("claude", "own-commit", extra=["--write"], cwd=wt)
        self.assertEqual(rec["status"], "ok", rec)
        self.assertEqual(list((self.cwd / "own" / ".git" / "worktrees").glob("*/config.worktree")), [])
        for mode in ("tamper-main", "tamper-config", "tamper-gitfile"):  # 統合先の ref、親の git で動く設定、作業場所の .git
            _, wt = self.worktree(mode)
            rc, rec = self.run_launch("claude", mode, extra=["--write"], cwd=wt)
            self.assertEqual(rec["status"], "failed", (mode, rec))
            self.assertIn("git を動かす前に", rec["problems"][0])

    def test_claude_read_only_in_worktree_denies_the_common_git_dir(self):
        repo, wt = self.worktree()
        self.run_launch("claude", cwd=wt, env_extra={"MY_SERVICE_TOKEN": "x"})
        argv = json.loads(self.args_file.read_text())
        sandbox = json.loads(argv[argv.index("--settings") + 1])["sandbox"]
        self.assertIn(str((repo / ".git").resolve()), sandbox["filesystem"]["denyWrite"])  # sandbox は共有の .git を自動で許すので
        denied = {e["name"] for e in sandbox["credentials"]["envVars"] if e["mode"] == "deny"}
        self.assertTrue({"CLAUDE_CODE_OAUTH_TOKEN", "GH_TOKEN", "MY_SERVICE_TOKEN"} <= denied)  # 子の Bash から認証情報の環境変数を消す
        self.assertIn(str(Path.home()), sandbox["filesystem"]["denyWrite"])
        self.assertEqual(Path(str(self.args_file) + ".scrub").read_text(), "")  # SCRUB は許可の方針を default に戻すので使わない

    def grok_target(self):
        """Grok の read-only は /tmp に書けるので、読み取り専用の試験の対象は /tmp の外に置く。"""
        d = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(d.cleanup)
        (Path(d.name) / "a.txt").write_text("x\n")
        return Path(d.name)

    def grok_argv(self):
        argv = json.loads(self.args_file.read_text())
        return argv[:-1], json.loads(argv[-1][len("ENV="):])

    def grok_profile_of(self, argv, env):
        """その起動だけの GROK_HOME にある profile。専用ログインの場所には sandbox.toml を作らない。"""
        name = argv[argv.index("--sandbox") + 1]
        home = Path(env["GROK_HOME"])
        self.assertEqual(home.parent, self.state / "grok-homes")
        self.assertEqual((home / "auth.json").resolve(), (self.cfg / "grok-auth" / "auth.json").resolve())
        self.assertFalse((self.cfg / "grok-auth" / "sandbox.toml").exists())
        toml = (home / "sandbox.toml").read_text()
        self.assertEqual(toml.count("[profiles."), 1, toml)
        return toml.split(f"[profiles.{name}]\n", 1)[1]

    def test_grok_ok_is_read_only_and_isolated(self):
        rc, rec = self.run_launch("grok", cwd=self.grok_target(),
                                  env_extra={"GROK_AGENT": "1", "XAI_API_KEY": "k", "GROK_SANDBOX": "off", "RUST_LOG": "debug"})
        self.assertEqual((rc, rec["status"], rec["session"], rec["model_effective"]), (0, "ok", "g-1", ["grok-4.7-build"]), rec)
        argv, env = self.grok_argv()
        # 組み込みの read-only は適用できないと強制せずに続くので、適用できなければ起動を拒む独自の profile で渡す
        self.assertIn('extends = "read-only"', self.grok_profile_of(argv, env))
        self.assertEqual(rec["grok_home"], env["GROK_HOME"])  # 再開に使う home を記録する
        compat = {f"GROK_{v}_{c}_ENABLED" for v in ("CLAUDE", "CURSOR") for c in ("AGENTS", "SKILLS", "RULES", "HOOKS", "MCPS")}
        self.assertEqual(env, {"GROK_HOME": env["GROK_HOME"], "GROK_MEMORY": "0", **{k: "false" for k in compat}})

    def test_grok_parent_api_key_is_not_passed(self):
        self.run_launch("grok", cwd=self.grok_target(), env_extra={"XAI_API_KEY": "k"})
        self.assertNotIn("XAI_API_KEY", self.grok_argv()[1])

    def test_grok_read_only_refuses_targets_overlapping_writable_places(self):
        target = self.grok_target()
        var_tmp = tempfile.TemporaryDirectory(dir="/var/tmp")
        self.addCleanup(var_tmp.cleanup)
        for cwd, state in ((Path("/tmp"), None), (Path(var_tmp.name), None), (self.cwd, None),  # self.cwd は /tmp の下
                           (Path("/"), None), (target, target / "state")):  # 対象が起動ごとの GROK_HOME（read-only でも書ける）を含む
            rc, rec = self.run_launch("grok", cwd=cwd, env_extra={"BB_WRIT_STATE_DIR": str(state)} if state else None)
            self.assertEqual(rec["status"], "unavailable", cwd)
            self.assertIn("読み取り専用にできない", rec["problems"][0])
        self.assertFalse(self.args_file.exists())  # 起動しない

    def test_grok_write_grants_state_and_worktree_git_dir(self):
        repo = self.cwd / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "i"], check=True)
        wt = self.cwd / "wt"
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "bb-writ/t1", str(wt)], check=True)
        (wt / "a.txt").write_text("x\n")  # 偽の子の報告が指す証拠
        rc, rec = self.run_launch("grok", extra=["--write"], cwd=wt)
        self.assertEqual(rec["status"], "ok", rec)
        profile = self.grok_profile_of(*self.grok_argv())
        self.assertIn('extends = "workspace"', profile)
        rw = json.loads(profile.split("read_write = ", 1)[1])
        common = (repo / ".git").resolve()
        # commit に要る場所だけ。共通ディレクトリの直下・config・hooks と、state 全体は渡さない
        self.assertEqual(sorted(rw), sorted([str(Path(self.record_of(wt)).resolve()), str(common / "objects"), str(common / "refs" / "heads" / "bb-writ"),
                                             str(common / "logs" / "refs" / "heads" / "bb-writ"), str(common / "worktrees" / "wt")]))

    def test_grok_parallel_launches_have_separate_homes(self):
        env = {**os.environ, "PATH": f"{self.bin}:/usr/bin:/bin", "BB_WRIT_CONFIG_DIR": str(self.cfg), "BB_WRIT_STATE_DIR": str(self.state),
               "FAKE_ARGS": str(self.args_file), "FAKE_TEXT": GOOD.encode().decode("unicode_escape")}
        procs = [subprocess.Popen([sys.executable, str(LAUNCH), "--host", "grok", "--cwd", str(self.cwd), "--prompt-file", str(self.prompt), "--write"],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(8)]
        homes = {json.loads(pr.communicate()[0])["grok_home"] for pr in procs}
        self.assertEqual(len(homes), 8)
        self.assertFalse((self.cfg / "grok-auth" / "sandbox.toml").exists())

    def test_grok_auth_replaced_in_the_home_is_not_written_back(self):
        # 子は自分の home に書けるので、そこで置き換わった auth.json（別のアカウントかもしれない）を専用ログインへ昇格させない
        rc, rec = self.run_launch("grok", "refresh", cwd=self.grok_target())
        self.assertEqual(rec["status"], "ok", rec)
        self.assertEqual((self.cfg / "grok-auth" / "auth.json").read_text(), "{}")

    def test_grok_warning_is_failed_but_note_is_not(self):
        rc, rec = self.run_launch("grok", "nosandbox", cwd=self.grok_target())
        self.assertEqual((rc, rec["status"]), (2, "failed"))
        self.assertTrue(any("could not be applied" in x for x in rec["problems"]), rec)
        rc, rec = self.run_launch("grok", "leadernote", cwd=self.grok_target())
        self.assertEqual(rec["status"], "ok", rec)

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
