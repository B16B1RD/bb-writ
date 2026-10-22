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
    _f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_env.json")  # 子の環境を絞る launcher でも、試験の設定を届ける
    os.environ.update({k: v for k, v in json.load(open(_f)).items() if k not in os.environ})
    open(os.environ["FAKE_ARGS"] + ".allenv", "w").write(json.dumps(sorted(k for k in os.environ if not k.startswith("FAKE_"))))
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
            "nofence": os.environ["FAKE_TEXT"].rstrip("`").rstrip()}.get(mode, os.environ["FAKE_TEXT"])
    raw = not os.environ.get("FAKE_RAW")  # 本物の CLI は U+2028 などをエスケープせずに出す
    if mode == "blockout":  # 結果の置き場をふさぐ（launcher の想定外の失敗を起こす）
        os.makedirs(os.path.join(os.environ["FAKE_OUT"], "stdout.jsonl"))
    if mode == "shapes":  # 形の違う行（オブジェクトでない行、item が文字列、深すぎる行）
        print("1"); print(json.dumps({"type": "item.completed", "item": "x"})); print(json.dumps({"type": "error", "error": "x"}))
        print("[" * 100000 + "]" * 100000)
    print(json.dumps({"type": "thread.started", "thread_id": "**" if mode == "glob-session" else os.environ.get("FAKE_THREAD", "t-1")}, ensure_ascii=raw))
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}, ensure_ascii=raw))
    if mode == "deep-usage":  # 読めるが、記録（indent 付き）には書けない深さ
        print('{"type": "turn.completed", "usage": ' + "[" * 3000 + "]" * 3000 + "}")
    elif mode == "unwritable-usage":  # 試験の sitecustomize が書き出しを失敗させる印
        print(json.dumps({"type": "turn.completed", "usage": {"__unwritable__": 1}}))
    else:
        print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5}}, ensure_ascii=raw))
    if mode == "shapes":  # 文字列でない thread_id（後の行が勝つ）
        print(json.dumps({"type": "thread.started", "thread_id": ["t"]}))
    if mode == "badbytes":  # UTF-8 でないバイト（途中で切れた多バイト文字）
        sys.stdout.flush(); sys.stdout.buffer.write(b"\xe3\x81\n"); sys.stderr.buffer.write(b"warn \xff\n")
''').lstrip()

FAKE_CLAUDE = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import json, os, sys
    _f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_env.json")  # 子の環境を絞る launcher でも、試験の設定を届ける
    os.environ.update({k: v for k, v in json.load(open(_f)).items() if k not in os.environ})
    open(os.environ["FAKE_ARGS"] + ".allenv", "w").write(json.dumps(sorted(k for k in os.environ if not k.startswith("FAKE_"))))
    open(os.environ["FAKE_ARGS"], "w").write(json.dumps(sys.argv[1:]))
    open(os.environ["FAKE_ARGS"] + ".scrub", "w").write(os.environ.get("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB", ""))
    open(os.environ["FAKE_ARGS"] + ".env", "w").write(json.dumps(sorted(k for k in os.environ if k.startswith(("CLAUDE", "ANTHROPIC_")))))
    os.makedirs(".claude/.cc-writes", exist_ok=True)  # 2.1.302 の --restricted が対象に作る空のディレクトリ
    import subprocess
    mode = os.environ.get("FAKE_MODE")
    if mode in ("home-ssh", "home-ssh-used"):  # 2.1.309 の sandbox がホームに作る空のディレクトリ
        os.makedirs(os.path.join(os.environ["HOME"], ".claude", "ssh"), mode=0o700, exist_ok=True)
        if mode == "home-ssh-used":
            open(os.path.join(os.environ["HOME"], ".claude", "ssh", "x"), "w").write("x")
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
    if mode == "home-residue":  # sandbox の設定が、守る対象のうち無いものをホームに空で作って残す（SUBPROCESS_ENV_SCRUB のとき。2.1.302）
        home = os.environ["HOME"]
        open(os.path.join(home, ".bash_profile"), "w").close(); open(os.path.join(home, ".netrc"), "w").close()
        os.makedirs(os.path.join(home, ".config", "pip"))
    if os.environ.get("FAKE_MODE") == "wrote":
        open(".claude/.cc-writes/x", "w").write("x")
    if os.environ.get("FAKE_MODE") == "checkout":  # worktree の子は、元の checkout にも作る
        import subprocess
        common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], capture_output=True, text=True).stdout.strip()
        os.makedirs(os.path.join(os.path.dirname(common), ".claude", ".cc-writes"), exist_ok=True)
    if mode == "home-residue-lock":  # 空のファイルを残し、別の場所を読めなくする
        open(os.path.join(os.environ["HOME"], ".netrc"), "w").close()
        os.chmod(os.path.join(os.environ["HOME"], ".locked-later"), 0)
    if mode == "home-lock":  # ホームの場所を読めなくする
        os.chmod(os.path.join(os.environ["HOME"], ".config"), 0)
    if mode == "home-lock-root":  # ホームそのものを読めなくする
        os.chmod(os.environ["HOME"], 0)
    if mode == "hook-symlink":  # 行き先の無い symlink を hooks に足す
        os.symlink("/nonexistent/hook", os.path.join(common(), "hooks", "pre-commit"))
    if mode == "config-include-fifo":  # 設定は通常のファイルのまま、git に FIFO を読ませる
        fifo = os.path.join(common(), "evil-include")
        os.mkfifo(fifo)
        open(os.path.join(common(), "config"), "a").write(f"[include]\n\tpath = {fifo}\n")
    if mode in ("packed-fifo", "config-fifo"):  # git が開いたまま止まるものに置き換える（Claude Code の書く子は共有の .git に書ける）
        f = os.path.join(common(), "packed-refs" if mode == "packed-fifo" else "config")
        if os.path.exists(f):
            os.unlink(f)
        os.mkfifo(f)
    if mode == "lock-claude-dir":  # 後片付けの場所を読めなくする
        os.chmod(".claude", 0)
    if mode == "bad-head":  # 自分の作業場所の HEAD を UTF-8 でない中身にする
        gd = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-dir"], capture_output=True, text=True).stdout.strip()
        open(os.path.join(gd, "HEAD"), "wb").write(b"ref: refs/heads/bb-writ/\xff\n")
    if mode in ("head-fifo", "head-dir", "head-zero"):  # HEAD を通常のファイルでないものにする
        gd = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-dir"], capture_output=True, text=True).stdout.strip()
        os.unlink(os.path.join(gd, "HEAD"))
        {"head-fifo": os.mkfifo, "head-dir": os.mkdir, "head-zero": lambda p: os.symlink("/dev/zero", p)}[mode](os.path.join(gd, "HEAD"))
    if mode == "lock-git-dir":  # 自分の管理ディレクトリを読めなくする（空の config.worktree を残して）
        gd = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-dir"], capture_output=True, text=True).stdout.strip()
        open(os.path.join(gd, "config.worktree"), "w").close()
        open(os.environ["FAKE_ARGS"] + ".gitdir", "w").write(gd)
        os.chmod(gd, 0)
    if mode == "head-space":  # HEAD の末尾だけを変える
        gd = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-dir"], capture_output=True, text=True).stdout.strip()
        h = open(os.path.join(gd, "HEAD"), "rb").read()
        open(os.path.join(gd, "HEAD"), "wb").write(h.rstrip(b"\n") + b" \n")
    if mode == "shapes":  # 深すぎる行
        print('{"a": ' + "[" * 100000 + "]" * 100000 + "}")
    result = os.environ["FAKE_TEXT"]
    if mode == "unwritable-report":  # 試験の sitecustomize が書き出しを失敗させる印
        result = result.replace('"conclusion": "', '"__unwritable__": 1, "conclusion": "')
    if mode == "deep":  # 読めるが、記録（indent 付き）には書けない深さの報告
        result = result.replace('"conclusion": "', '"z": ' + "[" * 3000 + "]" * 3000 + ', "conclusion": "')
    if mode == "surrogate":  # JS の JSON.stringify は単独のサロゲートを \ud83d の形で出す。本文と、報告の JSON の中の両方
        result = "\ud83d" + result.replace('"conclusion": "', '"conclusion": "\\ud83d')
    if os.environ.get("FAKE_EXIT"):  # 子が失敗した（後処理の理由と並ぶか確かめる）
        print(json.dumps({"type": "result", "result": "", "session_id": "s-1", "is_error": True})); sys.exit(int(os.environ["FAKE_EXIT"]))
    print(json.dumps({"type": "result", "result": result, "session_id": ["s"] if mode == "shapes" else "s-1", "is_error": False,
                      "modelUsage": "x" if mode == "shapes" else {"claude-sonnet-5-5": {}}, "total_cost_usd": 0.01, "num_turns": 2},
                     ensure_ascii=not os.environ.get("FAKE_RAW")))
''').lstrip()

FAKE_GROK = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import json, os, sys
    _f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_env.json")  # 子の環境を絞る launcher でも、試験の設定を届ける
    os.environ.update({k: v for k, v in json.load(open(_f)).items() if k not in os.environ})
    open(os.environ["FAKE_ARGS"] + ".allenv", "w").write(json.dumps(sorted(k for k in os.environ if not k.startswith("FAKE_"))))
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
                      "modelUsage": "x" if mode == "shapes" else {"grok-4.7-build": {}}, "total_cost_usd": 0.02, "num_turns": 3,
                      **({"sessionId": ["g"]} if mode == "shapes" else {})}))
''').lstrip()

FAKE_DOTENVX = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import os, sys
    args = sys.argv[1:]
    os.execvp(args[args.index("--") + 1], args[args.index("--") + 1:])
''').lstrip()


def load_launch():
    import importlib.util
    spec = importlib.util.spec_from_file_location("launch_under_test", LAUNCH)
    launch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launch)  # launch.py は読み込みで sys.dont_write_bytecode を立てる
    return launch


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
        (self.bin / "fake_env.json").write_text(json.dumps({k: v for k, v in env.items() if k.startswith("FAKE_")}))
        p = subprocess.run([sys.executable, str(LAUNCH), "--host", host, "--cwd", str(cwd or self.cwd), "--prompt-file", str(self.prompt), *extra],
                           env=env, capture_output=True, text=True, timeout=120)  # launcher が止まったら、試験ごと止まらずに落とす
        return p.returncode, json.loads(p.stdout)

    def test_launch_leaves_no_bytecode_in_the_plugin(self):
        subprocess.run(["rm", "-rf", str(ROOT / "scripts" / "__pycache__")], check=True)
        self.run_launch("codex")  # 試験の外と同じく -B なしで起動する
        self.assertFalse((ROOT / "scripts" / "__pycache__").exists())

    def test_reports_with_unicode_line_separators_are_read(self):
        text = GOOD.encode().decode("unicode_escape").replace('"conclusion": "', '"conclusion": "\u2028\x85\u2029')
        self.assertIn("\u2028", text)
        rollout = self.cfg / "codex-home" / "sessions" / "2026" / "rollout-1-0199aaaa-bbbb-7ccc-8ddd-eeeeffff0001.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_bytes((json.dumps({"type": "event", "payload": {"output": "a\u2028b"}}, ensure_ascii=False) + "\n"
                             + '{"type": "tu\n1\n{"type": "turn_context", "payload": "x"}\n'  # 書きかけの行、形の違う行は飛ばして続ける
                             + json.dumps({"type": "turn_context", "payload": {"model": "m0", "sandbox_policy": "x"}}) + "\n"
                             + json.dumps({"type": "turn_context", "payload": {"model": "m", "effort": "e\u2028", "cwd": "a\u2028b",
                                                                               "sandbox_policy": {"type": [1]}}}, ensure_ascii=False)
                             + "\n").encode()
                            + b'{"type": "turn_context", "payload": {"model": "gpt-\xff"}}\n')  # UTF-8 として読めない行は使わない
        for host in ("codex", "claude"):
            with self.subTest(host=host):
                rc, rec = self.run_launch(host, env_extra={"FAKE_RAW": "1", "FAKE_TEXT": text, "FAKE_THREAD": "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0001"})
                self.assertEqual((rc, rec["status"]), (0, "ok"), rec)  # 報告を失わない
                if host == "codex":
                    self.assertEqual(rec["model_effective"]["model"], "m")  # 記録の行を割らずに読む
                    self.assertIsNone(rec["model_effective"]["sandbox"])  # 文字列でない値は記録しない
                    self.assertNotIn("\u2028", (self.state / "delegations.jsonl").read_text())  # 委任の記録は 1 行 1 件のまま

    def test_unexpected_child_output_still_writes_the_record(self):
        for host, mode in (("codex", "badbytes"), ("codex", "shapes"), ("codex", "glob-session"), ("claude", "surrogate"),
                           ("claude", "shapes"), ("grok", "shapes")):
            with self.subTest(mode=mode):
                before = (self.state / "delegations.jsonl").read_text().count("\n") if (self.state / "delegations.jsonl").exists() else 0
                rc, rec = self.run_launch(host, mode=mode, cwd=self.grok_target() if host == "grok" else None)
                self.assertEqual((rc, rec["status"]), (0, "ok"), rec)  # 報告は読め、落ちない
                if mode == "shapes":
                    self.assertIsNone(rec["session"])  # 文字列でない session は記録しない
                out = Path(rec["out"])
                self.assertTrue((out / "record.json").is_file())
                lines = (self.state / "delegations.jsonl").read_text().split("\n")
                self.assertEqual(len(lines) - 1, before + 1)  # 委任の記録を 1 行書く
                json.loads(lines[-2])
                if mode == "badbytes":
                    self.assertTrue((out / "stdout.jsonl").read_bytes().endswith(b"\xe3\x81\n"))  # 生のまま残す
                    self.assertIn(b"warn \xff\n", (out / "stderr.log").read_bytes())
                if mode == "surrogate":
                    self.assertIn("\\ud83d", (out / "report.md").read_text())

    def test_values_too_deep_to_record_still_write_the_record(self):
        for host, mode, status, problem in (("claude", "deep", "invalid-report", "深すぎて記録に書けない"),
                                            ("codex", "deep-usage", "failed", "子の結果を記録に書けない")):
            with self.subTest(mode=mode):
                rc, rec = self.run_launch(host, mode=mode)
                # 読める深さ・書ける深さは Python の版で違う（3.10 は深さ 3000 を読めず、3.13 以降は書ける）。launcher と同じ Python で見る
                try:
                    json.loads("[" * 3000 + "]" * 3000)
                    readable = True
                except RecursionError:
                    readable = False
                deep = []
                for _ in range(2999):
                    deep = [deep]
                try:
                    json.dumps(deep, indent=1)
                    writable = True
                except RecursionError:
                    writable = False
                if not readable:  # 報告は読めない報告に、usage の行は読めない行として飛ばされる
                    status, problem = ("invalid-report", "読めない") if mode == "deep" else ("ok", None)
                elif writable:
                    status, problem = "ok", None
                self.assertEqual(rec["status"], status, rec)
                if problem:
                    self.assertTrue([p for p in rec["problems"] if problem in p], rec)
                self.assertTrue((Path(rec["out"]) / "record.json").is_file())
                self.assertIn(rec["out"], (self.state / "delegations.jsonl").read_text())

    def test_values_that_cannot_be_written_still_write_the_record(self):
        # Python の版によらず受け皿を通す: 印の付いた値だけ、indent 付きの書き出しを失敗させる（試験だけが読む sitecustomize）
        site = Path(self.tmp.name) / "site"
        site.mkdir()
        (site / "sitecustomize.py").write_text(textwrap.dedent('''
            import json
            _dumps = json.dumps
            def dumps(o, *a, **k):
                if k.get("indent") and "__unwritable__" in _dumps(o):
                    raise RecursionError("maximum recursion depth exceeded")
                return _dumps(o, *a, **k)
            json.dumps = dumps
        '''))
        for host, mode, status, problem in (("claude", "unwritable-report", "invalid-report", "深すぎて記録に書けない"),
                                            ("codex", "unwritable-usage", "failed", "子の結果を記録に書けない")):
            with self.subTest(mode=mode):
                rc, rec = self.run_launch(host, mode=mode, env_extra={"PYTHONPATH": str(site)})
                self.assertEqual(rec["status"], status, rec)
                self.assertTrue([p for p in rec["problems"] if problem in p], rec)
                self.assertTrue((Path(rec["out"]) / "record.json").is_file())
                self.assertIn(rec["out"], (self.state / "delegations.jsonl").read_text())

    def test_a_write_child_breaking_its_head_is_reported(self):
        for mode in ("bad-head", "head-space", "head-fifo", "head-dir", "head-zero", "lock-git-dir", "packed-fifo", "config-fifo",
                     "config-include-fifo", "hook-symlink"):
            if mode == "lock-git-dir" and os.geteuid() == 0:
                continue  # root は権限で止められない
            with self.subTest(mode=mode):
                _, wt = self.worktree(mode)
                try:
                    rc, rec = self.run_launch("claude", mode, extra=["--write"], cwd=wt, env_extra={"BB_WRIT_GIT_TIMEOUT": "2"})
                finally:
                    if mode == "lock-git-dir":
                        os.chmod(Path(str(self.args_file) + ".gitdir").read_text(), 0o755)
                self.assertEqual(rec["status"], "failed", rec)
                self.assertIn("git を動かす前に", rec["problems"][0])
                if mode in ("lock-git-dir", "packed-fifo", "config-fifo", "config-include-fifo"):  # 照合できなかったことを返す
                    self.assertIn("照合できない", rec["problems"][0])
                    self.assertTrue([p for p in rec["problems"] if "Git の管理情報を読めない" in p], rec)
                    sign = {"packed-fifo": "通常のファイルでない", "config-fifo": "通常のファイルでない",  # git に渡す前に止める
                            "config-include-fifo": "TimeoutExpired"}.get(mode)  # git が止まっても、時間で切る
                    if sign:
                        self.assertTrue([p for p in rec["problems"] if sign in p], rec)
                elif mode == "hook-symlink":
                    self.assertTrue([p for p in rec["problems"] if p.endswith("hooks/pre-commit")], rec)
                else:
                    self.assertTrue([p for p in rec["problems"] if p.startswith("HEAD: ")], rec)
                    if mode in ("head-fifo", "head-dir"):  # 空の HEAD と見分けられる表し
                        self.assertTrue([p for p in rec["problems"] if p.startswith("HEAD: ") and "通常のファイルでない" in p], rec)
                self.assertIn(rec["out"], (self.state / "delegations.jsonl").read_text())

    @unittest.skipIf(os.geteuid() == 0, "root は権限で止められない")
    def test_a_child_locking_its_leftovers_still_writes_the_record(self):
        rc, rec = self.run_launch("claude", "lock-claude-dir")
        os.chmod(self.cwd / ".claude", 0o755)
        self.assertEqual(rec["status"], "failed", rec)
        self.assertIn("片付けられない", rec["problems"][0])
        self.assertIn(rec["out"], (self.state / "delegations.jsonl").read_text())

    def test_a_child_that_cannot_be_started_is_recorded(self):
        (self.bin / "codex").write_text("#!/nonexistent/interpreter\n")  # 実行できるが、起動できない
        rc, rec = self.run_launch("codex")
        self.assertEqual((rc, rec["status"]), (2, "failed"), rec)
        self.assertIn("子を起動できない", rec["problems"][0])
        self.assertIn(rec["out"], (self.state / "delegations.jsonl").read_text())

    def test_a_symlinked_shared_config_is_not_refused(self):
        for mode, status in (("own-commit", "ok"), ("tamper-config", "failed")):
            with self.subTest(mode=mode):
                repo, wt = self.worktree("linked-config-" + mode)
                real = Path(self.tmp.name) / ("real-config-" + mode)
                (repo / ".git" / "config").rename(real)
                (repo / ".git" / "config").symlink_to(real)  # 普段の使い方でありうる
                rc, rec = self.run_launch("claude", mode, extra=["--write"], cwd=wt)
                self.assertEqual(rec["status"], status, rec)  # 行き先の中身の変化も見る
                if status == "failed":
                    self.assertTrue([p for p in rec["problems"] if p.endswith("/config")], rec)

    @unittest.skipIf(os.geteuid() == 0, "root は権限で止められない")
    def test_leftovers_from_a_previous_child_are_recorded_before_launch(self):
        for host, leftover, write in (("claude", "locked-config-worktree", True), ("codex", "head-fifo", True), ("claude", "head-fifo", False)):
            with self.subTest(leftover=leftover, write=write):
                _, wt = self.worktree(leftover + str(write))
                gd = Path(subprocess.run(["git", "-C", str(wt), "rev-parse", "--path-format=absolute", "--git-dir"],
                                         capture_output=True, text=True, check=True).stdout.strip())
                if leftover == "head-fifo":
                    (gd / "HEAD").unlink()
                    os.mkfifo(gd / "HEAD")
                else:
                    (gd / "config.worktree").write_text("")
                    (gd / "config.worktree").chmod(0)
                try:
                    rc, rec = self.run_launch(host, extra=["--write"] if write else [], cwd=wt, env_extra={"BB_WRIT_GIT_TIMEOUT": "2"})
                finally:
                    if leftover != "head-fifo":
                        (gd / "config.worktree").chmod(0o644)
                self.assertEqual(rec["status"], "failed", rec)
                self.assertIn("起動の前の準備ができない", rec["problems"][0])
                self.assertIn(rec["out"], (self.state / "delegations.jsonl").read_text())

    @unittest.skipIf(os.geteuid() == 0, "root は権限で止められない")
    def test_problems_after_the_child_keep_the_earlier_reasons(self):
        _, wt = self.worktree("keep")
        rc, rec = self.run_launch("claude", "tamper-main", extra=["--write"], cwd=wt, env_extra={"FAKE_EXIT": "3"})
        self.assertIn("git を動かす前に", rec["problems"][0])
        self.assertIn("終了コード 3", rec["problems"])  # 子の失敗の理由を消さない
        rc, rec = self.run_launch("claude", "lock-claude-dir", env_extra={"FAKE_EXIT": "3"})
        os.chmod(self.cwd / ".claude", 0o755)
        self.assertIn("片付けられない", rec["problems"][0])
        self.assertIn("終了コード 3", rec["problems"])

    def test_an_unexpected_failure_after_the_child_still_writes_the_record(self):
        out = Path(self.tmp.name) / "out-blocked"
        rc, rec = self.run_launch("codex", mode="blockout", extra=("--out", str(out)), env_extra={"FAKE_OUT": str(out)})
        self.assertEqual((rc, rec["status"]), (2, "failed"), rec)
        self.assertIn("子の結果を読めない", rec["problems"][0])
        self.assertIn("Traceback", (out / "launcher-error.log").read_text())  # 詳細を残す
        self.assertTrue((out / "record.json").is_file())
        self.assertIn(str(out), (self.state / "delegations.jsonl").read_text())

    def test_report_check_does_not_crash_on_odd_reports(self):
        launch = load_launch()
        for text, problem in (('```json\n{"conclusion": "c", "evidence": ["a.txt:1"]}', "where の無い証拠"),
                              ('```json\n{"a": ' + "[" * 100000 + "]" * 100000 + "}", "深"),
                              ('```json\n{"conclusion": "c", "evidence": [{"where": "' + "x" * 5000 + ':1"}]}', "証拠のファイルが無い")):
            with self.subTest(problem=problem):
                r, problems = launch.check_report(text, self.cwd)
                if problem == "深":  # 読めないか、記録に書けない（Python の版で深さの上限が違う。3.15 の decoder は読める）
                    self.assertTrue([p for p in problems if "読めない" in p or "深すぎて" in p] or r is not None and json.dumps(r, indent=1), problems)
                else:
                    self.assertTrue([p for p in problems if problem in p], problems)
                self.assertTrue(all(len(p) <= 400 for p in problems))  # 子の長い文を、そのまま記録に積まない

    def test_git_snapshot_reads_refs_that_are_not_utf8(self):
        launch = load_launch()
        repo = Path(self.tmp.name) / "g"
        repo.mkdir()
        g = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        subprocess.run(g + ["init", "-q"], check=True)
        subprocess.run(g + ["commit", "-q", "--allow-empty", "-m", "x"], check=True)
        head = subprocess.run(g + ["rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        # macOS（APFS）は UTF-8 でないファイル名を拒むので、ref のファイルでなく packed-refs に書く
        (repo / ".git" / "packed-refs").write_bytes(b"# pack-refs with: peeled fully-peeled sorted \n" + head.encode() + b" refs/heads/bb-writ/a\xff\n")
        snap = launch.git_snapshot(repo / ".git", repo / ".git", repo)
        self.assertEqual(len(snap["refs"]), 2)

    def test_codex_ok_is_read_only_by_default(self):
        other = self.cfg / "codex-home" / "sessions" / "rollout-1-zz-t-1.jsonl"  # 別の記録（名前の末尾が session と同じ）
        other.parent.mkdir(parents=True)
        other.write_text(json.dumps({"type": "turn_context", "payload": {"model": "other"}}) + "\n")
        rc, rec = self.run_launch("codex")
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        self.assertEqual(rec["session"], "t-1")
        self.assertIsNone(rec["model_effective"])  # UUID の形でない session では探さない
        near = self.cfg / "codex-home" / "sessions" / "rollout-1-deadbeef-1234.jsonl"
        near.write_text(json.dumps({"type": "turn_context", "payload": {"model": "other"}}) + "\n")
        rc, rec = self.run_launch("codex", env_extra={"FAKE_THREAD": "deadbeef-1234"})
        self.assertIsNone(rec["model_effective"])
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
        home = Path.home().resolve()
        for secret in (home / ".config" / "gh", home / ".ssh", home / ".claude", home / ".codex", self.cfg):  # gh のトークン・鍵・各 CLI のログイン
            covered = any(str(secret) == r or str(secret).startswith(r + "/") for r in fs["denyRead"])
            self.assertTrue(covered or not secret.exists(), secret)
        self.assertFalse(any(str(home / ".config" / "git").startswith(r + "/") or str(home / ".config" / "git") == r for r in fs["denyRead"]))
        self.assertIn("AGENTS.md・CLAUDE.md", argv[argv.index("-p") + 1])  # 自動で読まれない対象の指示を、資料として読ませる

    def record_of(self, d):
        """子が work.py claim・status で受け取る、その作業場所の記録の置き場。"""
        env = {**os.environ, "BB_WRIT_STATE_DIR": str(self.state)}
        p = subprocess.run([sys.executable, str(ROOT / "scripts" / "work.py"), "status", "--dir", str(d)], env=env, capture_output=True, text=True)
        return str(Path(json.loads(p.stdout)["note"]["path"]).parent)

    def test_claude_child_gets_no_parent_claude_env(self):
        # 親の CLAUDE_CODE_PLUGIN_DIRS（利用者の mod）や API の鍵を子に渡さない。渡すのは専用の設定ディレクトリだけ
        self.run_launch("claude", env_extra={"CLAUDE_CODE_PLUGIN_DIRS": "~/.claude/mods/x", "CLAUDECODE": "1", "CLAUDE_EFFORT": "max", "ANTHROPIC_API_KEY": "k"})
        self.assertEqual(json.loads(Path(str(self.args_file) + ".env").read_text()), ["CLAUDE_CONFIG_DIR"])

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

    def test_child_leaving_empty_files_in_home_fails(self):
        # 起動の後にホームの直下へ空の .bash_profile が残ると、ログインシェルが .profile を読まず .bashrc も読まなくなった
        home = Path(self.tmp.name) / "home"
        (home / ".config").mkdir(parents=True)
        (home / ".existing-empty").write_text("")
        for extra in ((), ("--write",)):
            rc, rec = self.run_launch("claude", "home-residue", extra=extra, env_extra={"HOME": str(home)})
            self.assertEqual((rc, rec["status"]), (2, "failed"), rec)
            self.assertEqual(rec["home_residue"], [str(home / ".bash_profile"), str(home / ".config" / "pip"), str(home / ".netrc")])
            for f in rec["home_residue"]:  # 黙って消さない（同じ時刻に別の道具が作ったものかもしれない）
                self.assertTrue(Path(f).exists())
                Path(f).unlink() if Path(f).is_file() else Path(f).rmdir()
        rc, rec = self.run_launch("claude", env_extra={"HOME": str(home)})
        self.assertEqual((rc, rec["status"], rec.get("home_residue")), (0, "ok", []), rec)  # 前からある空のファイルは数えない

    def test_claude_child_leaves_no_ssh_dir_it_made_in_home(self):
        # 2.1.309 の sandbox は、子を起こすたびにホームへ空の .claude/ssh を作り、残りとして委任が毎回 failed になった
        home = Path(self.tmp.name) / "home"
        (home / ".config").mkdir(parents=True)
        for extra in ((), ("--write",)):
            rc, rec = self.run_launch("claude", "home-ssh", extra=extra, env_extra={"HOME": str(home)})
            self.assertEqual((rec["status"], rec["home_residue"]), ("ok", []), rec)
            self.assertFalse((home / ".claude").exists())  # この起動で作られた .claude ごと消す
        (home / ".claude" / "ssh").mkdir(parents=True)  # 前からある空の .claude/ssh は消さない
        rc, rec = self.run_launch("claude", "home-ssh", env_extra={"HOME": str(home)})
        self.assertEqual(rec["status"], "ok", rec)
        self.assertTrue((home / ".claude" / "ssh").is_dir())
        (home / ".claude" / "ssh").rmdir()
        rc, rec = self.run_launch("claude", "home-ssh-used", env_extra={"HOME": str(home)})
        self.assertEqual(rec["status"], "ok", rec)
        self.assertEqual((home / ".claude" / "ssh" / "x").read_text(), "x")  # 中身があれば消さない

    @unittest.skipIf(os.geteuid() == 0, "root は権限で止められない")
    def test_a_child_hiding_part_of_home_fails(self):
        home = Path(self.tmp.name) / "home"
        (home / ".config").mkdir(parents=True)
        (home / ".locked-before").mkdir()
        (home / ".locked-before").chmod(0)  # 前から読めない場所は数えない
        try:
            rc, rec = self.run_launch("claude", env_extra={"HOME": str(home)})
            self.assertEqual(rec["status"], "ok", rec)
            rc, rec = self.run_launch("claude", "home-lock", env_extra={"HOME": str(home)})
        finally:
            (home / ".config").chmod(0o755)
            (home / ".locked-before").chmod(0o755)
        self.assertEqual(rec["status"], "failed", rec)
        self.assertIn("ホームの残りを確かめられない", rec["problems"][0])
        try:
            rc, rec = self.run_launch("claude", "home-lock-root", env_extra={"HOME": str(home)})
        finally:
            home.chmod(0o755)
        self.assertEqual(rec["status"], "failed", rec)
        self.assertIn("ホームの残りを確かめられない", rec["problems"][0])
        (home / ".locked-later").mkdir()
        try:
            rc, rec = self.run_launch("claude", "home-residue-lock", env_extra={"HOME": str(home)})
        finally:
            (home / ".locked-later").chmod(0o755)
        self.assertEqual(rec["home_residue"], [str(home / ".netrc")], rec)  # 読めない場所が増えても、残りの一覧は消さない
        self.assertTrue([p for p in rec["problems"] if "ホームの残りを確かめられない" in p], rec)

    def test_files_vanishing_during_the_home_scan_are_not_unreadable(self):
        launch, home = load_launch(), Path(self.tmp.name) / "vanish"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "x").write_text("x")
        real_scandir = os.scandir
        for when in (1, 2):  # 空かを見るとき、降りるとき
            with self.subTest(when=when):
                seen = []

                def flaky(d):  # 別のセッションが一時ディレクトリを消した（走査の途中で無くなった）
                    if str(d).endswith(".claude"):
                        seen.append(d)
                        if len(seen) == when:
                            raise FileNotFoundError(d)
                    return real_scandir(d)
                old_home = os.environ.get("HOME")
                os.environ["HOME"], launch.os.scandir = str(home), flaky
                try:
                    _, _, unreadable = launch.home_entries([])
                finally:
                    launch.os.scandir = real_scandir
                    os.environ["HOME"] = old_home
                self.assertEqual((len(seen), unreadable), (when, set()))

    def test_child_files_are_read_without_blocking_or_exhausting_memory(self):
        launch, d = load_launch(), Path(self.tmp.name) / "cf"
        d.mkdir()
        os.mkfifo(d / "fifo")
        (d / "zero").symlink_to("/dev/zero")
        (d / "lfifo").symlink_to(d / "fifo")  # FIFO を指す symlink も、開くところで止まらない
        for name in ("fifo", "zero", "lfifo"):
            code = (f"import importlib.util; s = importlib.util.spec_from_file_location('l', {str(LAUNCH)!r}); "
                    f"m = importlib.util.module_from_spec(s); s.loader.exec_module(m); print(m.read_child_file(__import__('pathlib').Path({str(d / name)!r}))[0])")
            p = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=20)  # 開くところで止まらない
            self.assertEqual(p.returncode, 0, p.stderr[-300:])
            self.assertNotIn("sha256:", p.stdout)  # 読まずに種類で表す
            self.assertIn("（", p.stdout)
        (d / "l1").symlink_to("a-target")
        (d / "l2").symlink_to("b-target")
        self.assertNotEqual(launch.read_child_file(d / "l1")[0], launch.read_child_file(d / "l2")[0])  # 行き先の変化も差分
        (d / "l3").symlink_to("x" * 53)  # 表しの長さがハッシュと同じになる行き先
        self.assertFalse(launch.read_child_file(d / "l3")[2])
        self.assertTrue(launch.read_head(d / "l3").startswith("symlink: "))  # 空の HEAD と読み違えない
        a, b = d / "a", d / "b"
        a.write_bytes(b"x" * 70000 + b"1")
        b.write_bytes(b"x" * 70000 + b"2")
        self.assertNotEqual(launch.read_head(a), launch.read_head(b))  # 先頭が同じでも、後ろの違いを見る
        self.assertIsNone(launch.read_child_file(d / "missing")[0])
        if sys.platform.startswith("linux"):  # 大きなファイルも一定のメモリで読む（RLIMIT_AS が効くのは Linux）
            big = d / "big"
            with open(big, "wb") as f:
                f.truncate(768 << 20)  # 疎なファイル
            code = ("import resource, sys, importlib.util; resource.setrlimit(resource.RLIMIT_AS, (512 << 20, 512 << 20)); "
                    f"s = importlib.util.spec_from_file_location('l', {str(LAUNCH)!r}); m = importlib.util.module_from_spec(s); "
                    f"s.loader.exec_module(m); print(len(m.read_child_file(__import__('pathlib').Path({str(big)!r}))[0]))")
            p = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=120)
            self.assertEqual(p.stdout.strip(), str(len("sha256: ") + 64), p.stderr[-500:])

    def test_rollout_reading_is_bounded(self):
        launch, d = load_launch(), Path(self.tmp.name) / "ro"
        d.mkdir()
        f = d / "r.jsonl"
        f.write_bytes(b'{"type": "turn_context", "payload": {"model": "m"}}\n' * 10 + b"y" * (3 << 20) + b"\n")
        lines = list(launch.rollout_lines(f))
        self.assertTrue(all(len(l) <= 1 << 20 for l in lines))  # 巨大な行は割れて、読めない行になる
        self.assertEqual(list(launch.rollout_lines(f, limit=200))[-1], b'{"type": "turn_context", "payload": {}}')  # 上限で実効値を不明にする
        os.mkfifo(d / "fifo")
        self.assertEqual(list(launch.rollout_lines(d / "fifo")), [])  # 止まらない
        if sys.platform.startswith("linux"):  # 書き手のいる FIFO でも止まらない（O_RDWR で書き手を持つのは Linux の振る舞い）
            w = os.open(d / "fifo", os.O_RDWR)
            try:
                code = (f"import importlib.util; s = importlib.util.spec_from_file_location('l', {str(LAUNCH)!r}); m = importlib.util.module_from_spec(s); "
                        f"s.loader.exec_module(m); print(list(m.rollout_lines(__import__('pathlib').Path({str(d / 'fifo')!r}))))")
                p = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=20)
                self.assertEqual(p.stdout.strip(), "[]", p.stderr[-300:])
            finally:
                os.close(w)
        (d / "dir.jsonl").mkdir()
        self.assertEqual(list(launch.rollout_lines(d / "dir.jsonl")), [])  # 通常のファイルだけを読む
        big = d / "big.jsonl"
        with open(big, "wb") as fh:
            fh.truncate(300 << 20)  # 疎なファイル。既定の上限（256 MiB）を越える
        self.assertEqual(list(launch.rollout_lines(big))[-1], b'{"type": "turn_context", "payload": {}}')

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
            if mode == "tamper-main":  # 動いた ref の前後の値と、戻すコマンドを示す
                self.assertTrue(any(x.startswith("refs/heads/main: ") and "update-ref refs/heads/main " in x for x in rec["problems"]), rec)

    def test_codex_write_in_worktree_gets_only_commit_dirs(self):
        repo, wt = self.worktree("cx")
        self.run_launch("codex", extra=["--write"], cwd=wt)
        argv = json.loads(self.args_file.read_text())
        dirs = [argv[i + 1] for i, x in enumerate(argv) if x == "--add-dir"]
        common = (repo / ".git").resolve()
        self.assertEqual(sorted(dirs), sorted([self.record_of(wt), str(common / "objects"), str(common / "refs" / "heads" / "bb-writ"),
                                               str(common / "logs" / "refs" / "heads" / "bb-writ"), str(common / "worktrees" / "cx-wt")]))

    def test_claude_read_only_in_worktree_denies_the_common_git_dir(self):
        repo, wt = self.worktree()
        self.run_launch("claude", cwd=wt, env_extra={"MY_SERVICE_TOKEN": "x", "HTTPS_PROXY": "http://p"})
        argv = json.loads(self.args_file.read_text())
        sandbox = json.loads(argv[argv.index("--settings") + 1])["sandbox"]
        self.assertIn(str((repo / ".git").resolve()), sandbox["filesystem"]["denyWrite"])  # sandbox は共有の .git を自動で許すので
        denied = {e["name"] for e in sandbox["credentials"]["envVars"] if e["mode"] == "deny"}
        self.assertTrue({"CLAUDE_CODE_OAUTH_TOKEN", "GH_TOKEN"} <= denied)  # 子の Bash から認証情報の環境変数を消す
        passed = set(json.loads(Path(str(self.args_file) + ".allenv").read_text()))
        self.assertNotIn("MY_SERVICE_TOKEN", passed)  # 読み取り専用の子の環境は最小限。プロキシは残す
        self.assertTrue({"PATH", "HOME", "HTTPS_PROXY", "CLAUDE_CONFIG_DIR"} <= passed)
        reads = sandbox["filesystem"]["denyRead"]
        self.assertNotIn(str(Path.home()), reads)  # ホーム全体は拒否しない（bwrap が起動しない）
        self.assertFalse(any(str(wt).startswith(r + "/") or str(wt) == r for r in reads))  # 対象に至る経路は読める
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
