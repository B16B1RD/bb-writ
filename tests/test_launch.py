"""launcher（scripts/launch.py。plan-v3 4.2）: 別系統の CLI の起動と、報告の検査。偽の CLI を PATH に置いて、成功と失敗の各場合を確かめる。"""

import errno
import json
import shlex
import shutil
import signal
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent)); import process_limit  # noqa: E402,F401  試験に上限を掛ける（直に走らせても）

ROOT = Path(__file__).resolve().parents[1]
LAUNCH = ROOT / "scripts" / "launch.py"
GOOD = ('ok.\\n```json\\n{"conclusion": "x は正しい", "evidence": [{"what": "読んだ", "where": "a.txt:1"}], "unknowns": [], '
        '"execution": {"completed": "yes", "commands": [{"command": "python3 -m unittest", "exit": 0}]}}\\n```')

FAKE_CODEX = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import json, os, sys
    _f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_env.json")  # 子の環境を絞る launcher でも、試験の設定を届ける
    os.environ.update({k: v for k, v in json.load(open(_f)).items() if k not in os.environ})
    open(os.environ["FAKE_ARGS"] + ".allenv", "w").write(json.dumps(sorted(k for k in os.environ if not k.startswith("FAKE_"))))
    open(os.environ["FAKE_ARGS"], "w").write(json.dumps(sys.argv[1:]))
    mode = os.environ.get("FAKE_MODE", "good")
    if os.environ.get("FAKE_STDERR"):  # 作業の途中で出す行（利用枠の文言など）
        print(os.environ["FAKE_STDERR"], file=sys.stderr, flush=True)
    if mode == "home-ssh":  # 並行する Claude Code の子が作ったものを、この委任が見る
        os.makedirs(os.path.join(os.environ["HOME"], ".claude", "ssh"), mode=0o700)
    if mode == "home-residue":  # どの子の CLI の sandbox も、ホームに空のファイルを残しうる
        open(os.path.join(os.environ["HOME"], ".bash_profile"), "w").close()
    if mode == "crash":
        print("boom", file=sys.stderr); sys.exit(1)
    if mode == "quota":
        print("ERROR: You've hit your usage limit.", file=sys.stderr); sys.exit(1)
    if mode == "apierror":
        print(json.dumps({"type": "turn.failed", "error": {"message": os.environ.get("FAKE_ERROR", "model is not supported")}})); sys.exit(1)
    text = {"good": os.environ["FAKE_TEXT"], "nojson": "結論だけ", "badpath": os.environ["FAKE_TEXT"].replace("a.txt", "missing.txt"),
            "absent": os.environ["FAKE_TEXT"].replace("a.txt:1", "missing.txt"),
            "nofence": os.environ["FAKE_TEXT"].rstrip("`").rstrip()}.get(mode, os.environ["FAKE_TEXT"])
    raw = not os.environ.get("FAKE_RAW")  # 本物の CLI は U+2028 などをエスケープせずに出す
    if mode == "blockout":  # 結果の置き場をふさぐ（launcher の想定外の失敗を起こす）
        os.makedirs(os.path.join(os.environ["FAKE_OUT"], "stdout.jsonl"))
    if mode == "shapes":  # 形の違う行（オブジェクトでない行、item が文字列、深すぎる行）
        print("1"); print(json.dumps({"type": "item.completed", "item": "x"})); print(json.dumps({"type": "error", "error": "x"}))
        print("[" * 100000 + "]" * 100000)
    thread = os.environ.get("FAKE_THREAD", "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0000")
    sessions = os.path.join(os.environ["CODEX_HOME"], "sessions")
    import glob
    # 試験が手で置いた記録（sessions/fake の外）があればそれを使う。偽の Codex の記録は起動ごとに書き直す（権限は引数で変わる）
    if mode != "no-rollout" and not [f for f in glob.glob(os.path.join(sessions, "**", f"rollout-*-{thread}.jsonl"), recursive=True)
                                     if os.path.dirname(f) != os.path.join(sessions, "fake")]:
        # 引数から、0.163.0 が記録する実効の権限を組み立てる（profile の名前や -c の書き方に結び付けない）
        import re
        argv = sys.argv[1:]
        conf = [argv[i + 1] if x in ("-c", "--config") else x[9:] for i, x in enumerate(argv)
                if x in ("-c", "--config") and i + 1 < len(argv) or x.startswith("--config=")]  # 引数の順のまま
        value = lambda key: next((c.split("=", 1)[1].strip().strip('"') for c in reversed(conf) if c.split("=", 1)[0].strip() == key), None)
        entry = lambda where, access: {"path": {"type": "special", "value": {"kind": where[1:]}} if where.startswith(":")
                                       else {"type": "path", "path": where}, "access": access}
        sandbox = value("sandbox_mode") or next((argv[i + 1] for i, x in enumerate(argv[:-1]) if x in ("-s", "--sandbox")), None)
        if "--approve-for-me" in argv:
            sandbox = "workspace-write"
        profile, network = value("default_permissions"), "restricted"
        added = [argv[i + 1] for i, x in enumerate(argv[:-1]) if x == "--add-dir"]
        entries = [entry(":root", "read")]
        if sandbox or not profile:  # どこかの sandbox_mode（-s・設定）は default_permissions より優先される（-s read-only は root を読むだけ）
            if sandbox == "workspace-write":
                entries += [entry(os.getcwd(), "write"), entry(":tmpdir", "write")] + [entry(d, "write") for d in added]
        else:
            if value(f"permissions.{profile}.extends") == ":workspace":
                entries += [entry(":workspace_roots", "write"), entry(":tmpdir", "write"), entry(":slash_tmp", "write")]
            for c in conf:
                if c.split("=", 1)[0].strip().startswith(f"permissions.{profile}.filesystem"):
                    entries += [entry(k, v) for k, v in re.findall(r'"([^"]+)"\s*=\s*"(read|write|deny)"', c)]
            if (value(f"permissions.{profile}.network.enabled") or "").lower() == "true":
                network = "enabled"
            entries += [entry(d, "write") for d in added]
        # 入力の書き換えの場面（対象の設定が同じ profile に足した・版で記録の形が変わった）
        extra = {"odd-deny-path": {"path": {"type": "weird"}, "access": "deny"}, "odd-entry": 42, "odd-access": entry(":root", "readwrite"), "odd-path": {"path": {"type": "weird"}, "access": "read"},
                 "slash-tmp-profile": entry(":slash_tmp", "write"), "wide-profile": entry(os.getcwd(), "write"),
                 "outdir-profile": entry(os.path.dirname(os.environ.get("TMPDIR", "/nonexistent/x")), "write"),
                 "workspace-profile": entry(":workspace_roots", "write"), "deny-tmp": entry(":tmpdir", "deny"),
                 "deny-entry": entry("/etc", "deny"), "path-like-special": {"path": {"type": "path", "path": ":tmpdir"}, "access": "write"},
                 "relative-path": {"path": {"type": "path", "path": "rel/x"}, "access": "read"}}.get(mode)
        if extra is not None:
            entries.append(extra)
        arg0 = os.path.join(os.environ["CODEX_HOME"], os.environ.get("FAKE_ARG0", os.path.join("tmp", "arg0-linux", "codex-arg0Ab12")))
        if os.environ.get("FAKE_ARG0_REAL"):  # Codex が symlink を解いた形で記録する場合
            arg0 = os.path.realpath(arg0)
        entries.append(entry(arg0 + ("/x" if mode == "arg0-nested" else ""), "read"))  # 0.163.0 は自分で足す（専用ログインの下）
        if mode == "read-under-deny":  # 対象の設定が、拒んだ場所の下に read を足した（より狭い指定が勝つ）
            entries.append(entry(os.path.join(os.environ["CODEX_HOME"], "auth.json"), "read"))
        if mode == "read-via-link":  # 拒んだ場所（symlink の行き先）を、解く前の書き方で開き直す
            entries.append(entry(os.path.join(os.environ["HOME"], ".aws", "credentials"), "read"))
        if mode == "sibling-read":  # 拒んだ場所と名前の先頭が同じ隣の場所の read（拒んだ場所の下ではない）
            entries.append(entry(os.path.dirname(os.environ["CODEX_HOME"]) + "2", "read"))
        if mode == "deny-dropped":  # 渡した deny が記録に無い
            entries = [e for e in entries if e["access"] != "deny"]
        if mode == "one-deny-dropped":  # 渡した deny のうち 1 つだけが記録に無い
            first = next(i for i, e in enumerate(entries) if e["access"] == "deny")
            entries = entries[:first] + entries[first + 1:]
        if mode == "deny-tmp-first":  # deny が write より前に並ぶ（同じ場所なら順に関わらず deny が勝つ）
            entries.insert(0, entry(":tmpdir", "deny"))
        perm = {"type": "managed", "file_system": {"type": "restricted", "entries": entries}, "network": network}
        if mode == "net-profile":
            perm["network"] = "enabled"
        if mode == "no-network":
            del perm["network"]
        if mode == "net-odd":
            perm["network"] = "restricted-but-open"
        if mode == "fs-odd-type":
            perm["file_system"]["type"] = "something-else"
        if mode == "legacy-sandbox":  # 対象の設定の sandbox_mode が profile より優先された（root を読むだけ）
            perm["file_system"]["entries"] = entries[:1]
        os.makedirs(os.path.join(sessions, "fake"), exist_ok=True)
        turn = lambda perm: json.dumps({"type": "turn_context", "payload": {"model": "gpt-6-sol", "effort": "medium",
                                                                           "sandbox_policy": {"type": "workspace-write"}, "permission_profile": perm}}) + "\n"
        wide = {**perm, "file_system": {"type": "restricted", "entries": entries + [entry(":root", "write")]}}
        narrow = {**perm, "file_system": {"type": "restricted", "entries": entries[:1]}}
        fs_unknown = {**perm, "file_system": {"type": "unrestricted"}}
        net_unknown = {k: v for k, v in perm.items() if k != "network"}
        # 1 つの記録に turn が 2 つ（どちらか一方だけが広い・狭い・読めない）
        net_open = {**perm, "network": "enabled"}
        big = {**perm, "user_instructions": "x" * (2 << 20)}  # 1 MiB を超える行（割れて読めない）
        bad = {"bad-json-after": '{"type": "turn_context", "pay\n', "bad-utf8-after": '\udcff\n'}.get(mode)  # 正しい turn の後の、短い読めない行
        undenied = {**perm, "file_system": {"type": "restricted", "entries": [e for e in entries if not (isinstance(e, dict) and e.get("access") == "deny")]}}
        turns = {"undenied-then-ok": [undenied, perm], "ok-then-undenied": [perm, undenied], "big-wide-line": [perm, {**big, "file_system": wide["file_system"]}], "net-open-then-ok": [net_open, perm], "ok-then-net-open": [perm, net_open], "wide-then-ok": [wide, perm], "ok-then-wide": [perm, wide], "narrow-then-ok": [narrow, perm], "ok-then-narrow": [perm, narrow],
                 "fs-unknown-then-ok": [fs_unknown, perm], "net-unknown-then-ok": [net_unknown, perm]}.get(mode, [perm])
        # 本物の exec resume は、同じ session の記録に turn を足す（FAKE_ROLLOUT_APPEND）
        open(os.path.join(sessions, "fake", f"rollout-1-{thread}.jsonl"), "a" if os.environ.get("FAKE_ROLLOUT_APPEND") else "w", errors="surrogateescape").write("".join(turn(x) for x in turns) + (bad or ""))
    if mode == "parent-moves-git":  # 子の実行中に、親や並行するタスクが ref と設定を正当に動かす（子は何もしない）
        import subprocess
        subprocess.run(["git", "branch", "parent-work"], check=True)
        subprocess.run(["git", "config", "branch.main.remote", "origin"], check=True)
    open(os.environ["FAKE_ARGS"] + ".tmpdir", "w").write(os.environ.get("TMPDIR", ""))
    if mode == "tmpdir":  # 書ける一時ディレクトリで試験を動かした
        import tempfile
        tempfile.mkdtemp()
    # ORPHAN
    if os.environ.get("FAKE_HANG_EARLY"):  # thread.started を出す前に止まる
        import time
        time.sleep(60)
    print(json.dumps({"type": "thread.started", "thread_id": "**" if mode == "glob-session" else thread}, ensure_ascii=raw))
    if os.environ.get("FAKE_HANG"):  # 時間切れ（本物は thread.started を最初に出してから作業する）
        import time
        sys.stdout.flush(); time.sleep(60)
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
    if os.environ.get("FAKE_EXIT"):  # 形の整った報告を出してから、失敗で終わる
        sys.exit(int(os.environ["FAKE_EXIT"]))
''').lstrip()

FAKE_CLAUDE = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import json, os, sys
    _f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_env.json")  # 子の環境を絞る launcher でも、試験の設定を届ける
    os.environ.update({k: v for k, v in json.load(open(_f)).items() if k not in os.environ})
    open(os.environ["FAKE_ARGS"] + ".allenv", "w").write(json.dumps(sorted(k for k in os.environ if not k.startswith("FAKE_"))))
    open(os.environ["FAKE_ARGS"], "w").write(json.dumps(sys.argv[1:]))
    open(os.environ["FAKE_ARGS"] + ".scrub", "w").write(os.environ.get("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB", ""))
    open(os.environ["FAKE_ARGS"] + ".env", "w").write(json.dumps({k: v for k, v in sorted(os.environ.items()) if k.startswith(("CLAUDE", "ANTHROPIC_"))}))
    os.makedirs(".claude/.cc-writes", exist_ok=True)  # 2.1.302 の --restricted が対象に作る空のディレクトリ
    import subprocess
    mode = os.environ.get("FAKE_MODE")
    if mode == "home-claude-only":  # 空の .claude だけを作る
        os.makedirs(os.path.join(os.environ["HOME"], ".claude"))
    if (mode or "").startswith("home-ssh"):  # 2.1.309 の sandbox がホームに作る空のディレクトリ
        ssh = os.path.join(os.environ["HOME"], ".claude", "ssh")
        os.makedirs(os.path.dirname(ssh), exist_ok=True)
        if mode == "home-ssh-link":  # 空のディレクトリへの symlink
            os.makedirs(os.path.join(os.environ["HOME"], "elsewhere"))
            os.symlink(os.path.join(os.environ["HOME"], "elsewhere"), ssh)
        elif mode == "home-ssh-file":
            open(ssh, "w").close()
        elif mode == "home-ssh-near":
            for d in (ssh, os.path.join(os.environ["HOME"], ".claude", "other"), os.path.join(os.environ["HOME"], ".claude", "ssh-agent"),
                      os.path.join(os.environ["HOME"], ".config", "ssh")):
                os.makedirs(d)
        else:
            os.makedirs(ssh, mode=0o700, exist_ok=True)
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
    if mode in ("cwd-residue", "cwd-residue-hang"):  # sandbox の置き場所を対象に空で作る。正常に終われば片付け、止められると残る（2.1.310）
        open(".bashrc", "w").close(); os.makedirs(".claude/agents/x"); open(".claude/settings.json", "w").close()
        open("note.txt", "w").write("x")  # 中身のあるものは残りでない
        os.makedirs("mixed"); open("mixed/empty", "w").close(); open("mixed/data", "w").write("x")  # 中身のあるものを含むディレクトリも
        os.symlink(".bashrc", "link")  # symlink は数えない
        if os.path.exists(".git"):  # 追跡させた空のファイルは、子が意図して足したもの
            open("keep", "w").close(); os.makedirs("pkg"); open("pkg/__init__.py", "w").close()
            subprocess.run(git + ["add", "keep", "pkg/__init__.py"])  # 新しいディレクトリの下に追跡させたものも
        if mode == "cwd-residue-hang":
            import time; time.sleep(60)
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
    if mode == "hook-chmod":  # 前からある実行できない hook に、実行権限だけを足す（中身は同じ。symlink なら行き先に）
        os.chmod(os.path.realpath(os.path.join(common(), "hooks", "pre-commit")), 0o755)
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
    if mode == "lock-target-dir":  # 対象の直下に、中を読めないディレクトリを作る（残りを確かめられない）
        os.mkdir("locked-new"); os.chmod("locked-new", 0)
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
    # ORPHAN
    if mode == "blockout":  # 結果の置き場をふさぐ（launcher の想定外の失敗を起こす）
        os.makedirs(os.path.join(os.environ["FAKE_OUT"], "stdout.jsonl"))
    result = os.environ["FAKE_TEXT"]
    argv = sys.argv[1:]
    sid = os.environ.get("FAKE_SID") or next((argv[i + 1] for i, x in enumerate(argv[:-1]) if x in ("--session-id", "--resume")), "s-1")  # 本物は渡した session を返す
    if mode in ("stop", "stop-deaf"):  # 動く間だけ対象に一時の置き場を持ち、TERM で片付けて終わる（Claude Code 2.1.310 の .claude/.cc-writes）
        import signal, time
        stage = os.environ["FAKE_ARGS"] + ".stage"  # 子だけが消す印（launcher の片付けと見分ける）
        open(stage, "w").close()
        def term(n, f):
            open(os.environ["FAKE_ARGS"] + ".term", "a").write(f"{n}\n")
            if mode == "stop":
                time.sleep(1)  # 片付けに時間がかかる（launcher が待たずに KILL すると、印が残る）
                os.unlink(stage)
                sys.exit(143)
        signal.signal(signal.SIGTERM, term)
        open(os.environ["FAKE_ARGS"] + ".started", "w").write(sid)
        time.sleep(60)
    if os.environ.get("FAKE_HANG"):  # 時間切れ（本物の -p --output-format json は、終わるまで何も出さない）
        import time
        time.sleep(60)
    if mode == "unwritable-report":  # 試験の sitecustomize が書き出しを失敗させる印
        result = result.replace('"conclusion": "', '"__unwritable__": 1, "conclusion": "')
    if mode == "deep":  # 読めるが、記録（indent 付き）には書けない深さの報告
        result = result.replace('"conclusion": "', '"z": ' + "[" * 3000 + "]" * 3000 + ', "conclusion": "')
    if mode == "surrogate":  # JS の JSON.stringify は単独のサロゲートを \ud83d の形で出す。本文と、報告の JSON の中の両方
        result = "\ud83d" + result.replace('"conclusion": "', '"conclusion": "\\ud83d')
    if os.environ.get("FAKE_EXIT"):  # 子が失敗した（後処理の理由と並ぶか確かめる）。FAKE_ERROR は失敗の結果の文
        print(json.dumps({"type": "result", "result": os.environ.get("FAKE_ERROR", ""), "session_id": sid, "is_error": True})); sys.exit(int(os.environ["FAKE_EXIT"]))
    print(json.dumps({"type": "result", "result": result, "session_id": ["s"] if mode == "shapes" else sid, "is_error": False,
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
    if os.environ.get("FAKE_STDERR"):  # 作業の途中で出す行（利用枠の文言など）
        print(os.environ["FAKE_STDERR"], file=sys.stderr, flush=True)
    if mode == "home-ssh":  # 並行する Claude Code の子が作ったものを、この委任が見る
        os.makedirs(os.path.join(os.environ["HOME"], ".claude", "ssh"), mode=0o700)
    if mode == "home-residue":  # どの子の CLI の sandbox も、ホームに空のファイルを残しうる
        open(os.path.join(os.environ["HOME"], ".bash_profile"), "w").close()
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
    # ORPHAN
    sid = next((sys.argv[i + 1] for i, x in enumerate(sys.argv[1:-1], 1) if x in ("-s", "-r")), "g-1")  # 本物は渡した session を返す
    # 本物と同じく、session の記録を home の sessions/<対象>/<session>/ に書き、-r はそれが無ければ失敗する
    where = os.path.join(os.environ["GROK_HOME"], "sessions", sys.argv[sys.argv.index("--cwd") + 1].replace("/", "%2F"), sid)
    if "-r" in sys.argv and not os.path.isfile(os.path.join(where, "updates.jsonl")):
        print(json.dumps({"type": "error", "message": "session not found"})); sys.exit(1)
    os.makedirs(where, exist_ok=True)
    open(os.path.join(where, "updates.jsonl"), "a").write("{}\n")
    if os.environ.get("FAKE_HANG"):  # 時間切れ（本物の -p --output-format json は、終わるまで何も出さない）
        import time
        sys.stderr.flush(); time.sleep(60)
    print(json.dumps({"text": os.environ["FAKE_TEXT"], "stopReason": "end_turn", "sessionId": sid,
                      "modelUsage": "x" if mode == "shapes" else {"grok-4.7-build": {}}, "total_cost_usd": 0.02, "num_turns": 3,
                      **({"sessionId": ["g"]} if mode == "shapes" else {})}))
''').lstrip()

# 偽の CLI の共通の部分: Claude Code 2.1.309 の Bash のように、新しいセッションでプロセスを起こし、その下に 6 段の鎖（実物の sandbox の木と
# 同じくらいの深さ。最後の段が sleep 600）を起こして、自分は先に終わる。上の段はコマンド名に ) を含め（/proc の stat を最初の ) で切ると
# 読み違える）、主スレッドだけを終わらせて別のスレッドで動き続ける（ゾンビに見えるが生きていて、下の段もまだ付け替わらない）。
# 鎖は止められるシグナルを全部無視する（SIGKILL でなければ止まらない）
LATE = """
import ctypes, os, platform, subprocess, sys, threading, time
libc = ctypes.CDLL(None)
libc.prctl(15, b'x) Z 1 (', 0, 0, 0)
sleep, out, write, chain = sys.argv[1:5]
open(out + '.chain', 'w').close()
subprocess.Popen([sys.executable, '-c', chain, '5', out + '.chain', sleep, chain], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
while not os.path.exists(out + '.chain') or len(open(out + '.chain').read().split()) < 6:
    time.sleep(0.05)
threading.Thread(target=lambda: (time.sleep(1.5), write and open('late.txt', 'w').write('x'), time.sleep(600))).start()  # 子が終わった後に、作業場所へ書く
open(out + '.part', 'w').write(' '.join([str(os.getpid())] + open(out + '.chain').read().split()))
os.rename(out + '.part', out)
libc.syscall({'x86_64': 60, 'aarch64': 93}[platform.machine()], 0)  # SYS_exit: 主スレッドだけ終わる
"""
CHAIN = ("import os, signal, subprocess, sys; [signal.signal(s, signal.SIG_IGN) for s in signal.valid_signals() if s not in (signal.SIGKILL, signal.SIGSTOP, signal.SIGCHLD)]; d = int(sys.argv[1]); open(sys.argv[2], 'a').write(f'{os.getpid()} '); "
         "os.execv(sys.argv[3], [sys.argv[3], '600']) if d == 0 else subprocess.run([sys.executable, '-c', sys.argv[4], str(d - 1), *sys.argv[2:]])")
ORPHAN = textwrap.dedent(r'''
    open(os.environ["FAKE_ARGS"] + ".cli", "w").write(f"{os.getpid()} {os.getppid()}")  # 子の CLI（と dotenvx）
    if mode in ("orphan", "orphan-hang", "orphan-pipe"):
        import subprocess, time
        quiet = {} if mode == "orphan-pipe" else {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}  # orphan-pipe は子の出力を開いたまま
        subprocess.Popen([sys.executable, "-c", LATE_CODE, os.environ["FAKE_SLEEP"], os.environ["FAKE_ARGS"] + ".orphan", "" if mode == "orphan-hang" else "1",
                          CHAIN_CODE], start_new_session=True, stdin=subprocess.DEVNULL, **quiet)
        for _ in range(400):  # 上の段の主スレッドが終わる（ゾンビに見える）まで待ってから、自分が終わる
            try:
                top = open(os.environ["FAKE_ARGS"] + ".orphan").read().split()[0]
                if open(f"/proc/{top}/stat", "rb").read().rsplit(b")", 1)[1].split()[0] == b"Z":
                    break
            except (OSError, IndexError):
                pass
            time.sleep(0.05)
        if mode == "orphan-hang":  # 時間切れの経路（子が生きている間に書くことになるので、書かせない）
            time.sleep(60)
    if mode == "orphans-exit":  # 実行中に孤児になって終わるもの（subreaper の launcher の下に来る）
        import subprocess, time
        for _ in range(20):
            subprocess.run([sys.executable, "-c", "import subprocess, sys; subprocess.Popen([sys.argv[1], '0'])", os.environ["FAKE_SLEEP"]])
        time.sleep(1.5)
        launcher = os.getppid()
        while b"launch.py" not in open(f"/proc/{launcher}/cmdline", "rb").read():
            launcher = int(open(f"/proc/{launcher}/stat", "rb").read().rsplit(b")", 1)[1].split()[1])
        zombies = 0
        for d in filter(str.isdigit, os.listdir("/proc")):
            try:
                f = open(f"/proc/{d}/stat", "rb").read().rsplit(b")", 1)[1].split()
            except OSError:
                continue
            zombies += f[0] == b"Z" and int(f[1]) == launcher
        open(os.environ["FAKE_ARGS"] + ".zombies", "w").write(str(zombies))
''').lstrip("\n").replace("LATE_CODE", repr(LATE)).replace("CHAIN_CODE", repr(CHAIN))

FAKE_DOTENVX = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import json, os, subprocess, sys
    args = sys.argv[1:]
    if json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_env.json"))).get("FAKE_CLOSE_OUT"):
        # 子の出力を閉じてから待つ（出力の読み手は先に終わり、子の CLI だけが残る）
        p = subprocess.Popen(args[args.index("--") + 1:], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(1); os.close(2)
        sys.exit(p.wait())
    import signal
    p = subprocess.Popen(args[args.index("--") + 1:])  # 本物と同じく、子を起こして待つ（時間切れで止めると、claude が launcher の下に来る）
    import time
    for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):  # 本物と同じく、受けたシグナルを子に伝える（少し遅れて。グループにも送られると、子は 2 回受ける）
        signal.signal(s, lambda n, f: (time.sleep(0.3), p.send_signal(n)))
    sys.exit(p.wait())
''').lstrip()


def fs_rules(argv: list) -> dict:
    """読み取り専用の Codex に渡した filesystem の表（-c・--config のどちらでも）を、場所 → 権限の辞書にする。"""
    import re
    table = next(argv[i + 1] for i, x in enumerate(argv[:-1]) if x in ("-c", "--config") and ".filesystem=" in argv[i + 1].split("{")[0])
    return dict(re.findall(r'"([^"]+)"\s*=\s*"(read|write|deny)"', table))


def stop_leftover(pid: int):
    """試験の偽の子が残した sleep 600 と回し役だけを止める（番号が別のプロセスに使い回されていたら触らない）。"""
    try:
        if b"600" in Path(f"/proc/{pid}/cmdline").read_bytes() or b"(x) Z 1 ()" in Path(f"/proc/{pid}/stat").read_bytes():
            os.kill(pid, 9)
    except (OSError, ProcessLookupError):
        pass


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
        self.home = t / "default-home"  # 実物のホームを見ると、同じ時刻に別の道具が作った空のファイル（~/.claude.json.lock）を残りと数えて落ちる
        for d in (self.bin, self.cfg / "codex-home", self.cfg / "grok-auth", self.state, self.cwd, self.home):
            d.mkdir(parents=True)
        (self.cwd / "a.txt").write_text("x\n")
        (self.cfg / "codex-home" / "auth.json").write_text("{}")
        (self.cfg / "grok-auth" / "auth.json").write_text("{}")
        (self.cfg / ".env").write_text("X=1\n")
        (self.cfg / ".env.keys").write_text("K=1\n")
        for name, body in (("codex", FAKE_CODEX), ("claude", FAKE_CLAUDE), ("grok", FAKE_GROK), ("dotenvx", FAKE_DOTENVX)):
            (self.bin / name).write_text(body.replace("/usr/bin/env python3", sys.executable).replace("# ORPHAN\n", ORPHAN))
            (self.bin / name).chmod(0o755)
        self.prompt = t / "prompt.md"
        self.prompt.write_text("x を確かめて")
        self.args_file = t / "args.json"

    def tearDown(self):
        self.orphans()  # 試験が run_launch の途中で落ちても、偽の子孫を残さない（記録の置き場を消す前に読む）
        self.tmp.cleanup()

    def run_launch(self, host, mode="good", extra=(), path=None, cwd=None, env_extra=None, proc_cwd=None, raw=False):
        self.orphans()  # 前の回が途中で落ちて読まれなかった偽の子孫を、上書きする前に片付けに回す
        env = {**os.environ, "HOME": str(self.home), "PATH": path if path is not None else f"{self.bin}:/usr/bin:/bin", "BB_WRIT_CONFIG_DIR": str(self.cfg),
               "BB_WRIT_STATE_DIR": str(self.state), "FAKE_MODE": mode, "FAKE_ARGS": str(self.args_file),
               "FAKE_TEXT": GOOD.encode().decode("unicode_escape"), **(env_extra or {})}
        (self.bin / "fake_env.json").write_text(json.dumps({k: v for k, v in env.items() if k.startswith("FAKE_")}))
        target = ["--host", host, "--cwd", str(cwd or self.cwd)] if host else []  # host が無いのは --resume（記録から取る）
        p = subprocess.run([sys.executable, str(LAUNCH), *target, "--prompt-file", str(self.prompt), *extra],
                           env=env, capture_output=True, text=True, timeout=120, cwd=proc_cwd)  # launcher が止まったら、試験ごと止まらずに落とす
        return p if raw else (p.returncode, json.loads(p.stdout))

    def test_launch_leaves_no_bytecode_in_the_plugin(self):
        subprocess.run(["rm", "-rf", str(ROOT / "scripts" / "__pycache__")], check=True)
        self.run_launch("codex")  # 試験の外と同じく -B なしで起動する
        self.assertFalse((ROOT / "scripts" / "__pycache__").exists())

    def test_the_record_names_the_commit_the_child_saw_only_when_the_tree_matches_it(self):
        """work.py の --reviewed は、この head と照らす（Issue #14）。未 commit の変更があれば、子が見たものは commit と違う。"""
        last = lambda: json.loads((self.state / "delegations.jsonl").read_text().splitlines()[-1])
        rc, rec = self.run_launch("claude")
        self.assertEqual((rc, rec["head"]), (0, None))  # Git の作業ツリーでない
        self.assertNotIn("next", rec)  # 審査に数えない子には、指摘の扱いを書けと出さない
        git = lambda *a, cwd=self.cwd: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True,
                                                      capture_output=True, text=True).stdout.strip()
        unborn = Path(self.tmp.name) / "unborn"  # まだ commit が無い（ほかに変更の無い作業ツリー）
        unborn.mkdir()
        git("init", "-q", cwd=unborn)
        (unborn / "a.txt").write_text("x\n")  # 偽の子の報告が証拠に指すファイル。除外して、変更に数えない
        (unborn / ".git" / "info" / "exclude").write_text("a.txt\n")
        rc, rec = self.run_launch("claude", cwd=unborn)
        self.assertEqual((rc, rec["head"]), (0, None), rec)
        git("init", "-q")
        (self.cwd / ".gitignore").write_text("ignored.txt\n")
        git("add", "a.txt", ".gitignore")
        git("commit", "-q", "-m", "a")
        (self.cwd / "ignored.txt").write_text("x\n")  # 無視するファイルは変更に数えない
        index = self.cwd / ".git" / "index"
        before = (index.read_bytes(), index.stat().st_mtime_ns)
        os.utime(self.cwd / "a.txt", (time.time() + 100, time.time() + 100))  # 中身はそのまま。git status は index を書き直したがる
        rc, rec = self.run_launch("claude")
        self.assertEqual((rc, rec["head"]), (0, git("rev-parse", "HEAD")), rec)
        self.assertEqual((index.read_bytes(), index.stat().st_mtime_ns), before)  # 利用者の git と index のロックを競わない
        for host in ("claude", "codex"):  # 読み取り専用の Grok は /tmp の下の対象で起動しない
            with self.subTest(host=host):
                rc, rec = self.run_launch(host)
                self.assertEqual((rc, rec["head"], last()["head"]), (0, git("rev-parse", "HEAD"), rec["head"]), rec)
                self.assertIs(last()["write"], False)
                self.assertIn("指摘の扱い", rec["next"])  # 審査に数える子が返ったその場で、直す前に書く欄を出す
        no_untracked = Path(self.tmp.name) / "gitconfig"
        no_untracked.write_text("[status]\n\tshowUntrackedFiles = no\n")
        clean = Path(self.tmp.name) / "clean"
        clean.mkdir()
        git("init", "-q", cwd=clean)
        git("commit", "-q", "--allow-empty", "-m", "c", cwd=clean)
        dirty = {"tracked": lambda: (self.cwd / "a.txt").write_text("changed\n"),
                 "staged": lambda: ((self.cwd / "a.txt").write_text("staged\n"), git("add", "a.txt")),
                 "untracked": lambda: (self.cwd / "new.txt").write_text("x\n")}
        for name, make in dirty.items():
            # 追跡外は設定で隠しても数える。親の GIT_DIR・GIT_WORK_TREE が別の変更の無いリポジトリを指しても、対象を見る
            for env in ({}, {"GIT_CONFIG_GLOBAL": str(no_untracked)}, {"GIT_DIR": str(clean / ".git"), "GIT_WORK_TREE": str(clean)}):
                with self.subTest(dirty=name, env=env):
                    make()
                    rc, rec = self.run_launch("claude", env_extra=env)
                    self.assertEqual((rc, rec["head"]), (0, None), rec)
                    self.assertNotIn("next", rec)
                    git("reset", "-q", "--hard")
                    (self.cwd / "new.txt").unlink(missing_ok=True)
        rc, rec = self.run_launch("claude", mode="wrote")  # 子の実行の間に作業ツリーが変わった
        self.assertEqual((rc, rec["head"]), (0, None), rec)
        self.assertIn("変わった", rec["head_lost"])
        self.assertNotIn("next", rec)
        subprocess.run(["rm", "-rf", str(self.cwd / ".claude")], check=True)
        rc, rec = self.run_launch("claude", mode="own-commit")  # 子の実行の間に HEAD が別の commit に動いた（作業ツリーは変更なし）
        self.assertEqual((rc, git("status", "--porcelain"), rec["head"]), (0, "", None), rec)
        self.assertIn("変わった", rec["head_lost"])
        git("reset", "-q", "--hard", "HEAD~1")
        (self.cwd / "b.txt").write_text("b")  # 起動の前に変更があり、子の間に commit されて終わりは変更なし（子が見たのは commit でない）
        rc, rec = self.run_launch("claude", mode="own-commit")
        self.assertEqual((rc, git("status", "--porcelain"), rec["head"]), (0, "", None), rec)
        git("reset", "-q", "--hard", "HEAD~1")
        quote = Path(self.tmp.name) / "quote"
        quote.write_text("[core]\n\tquotePath = false\n")
        odd = os.fsencode(str(self.cwd)) + b"/\xff.txt"  # UTF-8 でない名前の追跡外のファイル
        try:
            open(odd, "w").close()
        except OSError as e:  # macOS の APFS は UTF-8 でない名前を作れない（そこでは起きない）
            if e.errno != errno.EILSEQ:
                raise
        else:
            rc, rec = self.run_launch("claude", env_extra={"GIT_CONFIG_GLOBAL": str(quote)})
            self.assertEqual((rc, rec["head"]), (0, None), rec)
            os.unlink(odd)
        index = self.cwd / ".git" / "index"  # git status が開いたまま止まる index（上限で止めて null）
        index.rename(self.cwd / ".git" / "index.bak")
        os.mkfifo(index)
        rc, rec = self.run_launch("claude", env_extra={"BB_WRIT_GIT_TIMEOUT": "2"})
        self.assertEqual((rc, rec["head"]), (0, None), rec)
        index.unlink()
        (self.cwd / ".git" / "index.bak").rename(index)
        rc, rec = self.run_launch("claude", path=str(self.bin))  # git が無い
        self.assertEqual(rec["head"], None, rec)
        bare = Path(self.tmp.name) / "bare.git"
        git("clone", "-q", "--bare", str(self.cwd), str(bare))
        rc, rec = self.run_launch("claude", cwd=bare)
        self.assertEqual(rec["head"], None, rec)
        rc, rec = self.run_launch("claude", extra=["--write"])  # 書く子（ここでは起動しない）は write が真
        self.assertIs(last()["write"], True)
        self.assertNotIn("next", rec)  # 書く子は審査に数えない
        sub = Path(self.tmp.name) / "sub"  # サブモジュールの中の変更は、無視する設定があっても数える
        sub.mkdir()
        git("init", "-q", cwd=sub)
        git("commit", "-q", "--allow-empty", "-m", "s", cwd=sub)
        git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "s")
        git("commit", "-q", "-m", "s")
        rc, rec = self.run_launch("claude")
        self.assertEqual((rc, rec["head"]), (0, git("rev-parse", "HEAD")), rec)
        ignore = Path(self.tmp.name) / "ignore-submodules"
        ignore.write_text("[diff]\n\tignoreSubmodules = all\n")
        (self.cwd / "s" / "x.txt").write_text("x\n")
        rc, rec = self.run_launch("claude", env_extra={"GIT_CONFIG_GLOBAL": str(ignore)})
        self.assertEqual((rc, rec["head"]), (0, None), rec)

    def test_reports_with_unicode_line_separators_are_read(self):
        text = GOOD.encode().decode("unicode_escape").replace('"conclusion": "', '"conclusion": "\u2028\x85\u2029')
        self.assertIn("\u2028", text)
        rollout = self.cfg / "codex-home" / "sessions" / "2026" / "rollout-1-0199aaaa-bbbb-7ccc-8ddd-eeeeffff0001.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_bytes((json.dumps({"type": "event", "payload": {"output": "a\u2028b"}}, ensure_ascii=False) + "\n"
                             + '{"type": "tu\n1\n{"type": "turn_context", "payload": "x"}\n'  # 書きかけの行、形の違う行は飛ばして続ける
                             + json.dumps({"type": "turn_context", "payload": {"model": "m0", "sandbox_policy": "x"}}) + "\n"
                             + json.dumps({"type": "turn_context", "payload": {"model": "m", "effort": "e\u2028", "cwd": "a\u2028b",
                                                                               "permission_profile": {"file_system": {"type": "restricted", "entries": [
                                                                                   {"path": {"type": "special", "value": {"kind": "tmpdir"}}, "access": "write"}]},
                                                                                   "network": "restricted"},
                                                                               "sandbox_policy": {"type": [1]}}}, ensure_ascii=False)
                             + "\n").encode()
                            + b'{"type": "turn_context", "payload": {"model": "gpt-\xff"}}\n')  # UTF-8 として読めない行は使わない
        for host in ("codex", "claude"):
            with self.subTest(host=host):
                rc, rec = self.run_launch(host, env_extra={"FAKE_RAW": "1", "FAKE_TEXT": text, "FAKE_THREAD": "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0001"})
                if host == "codex":  # 形の違う turn_context の権限は不明（どの turn の権限も合わせて見る）。報告は残す
                    self.assertEqual((rc, rec["status"]), (2, "failed"), rec)
                    self.assertIn("確かめられない", rec["problems"][0])
                    self.assertIn("\u2028", (Path(rec["out"]) / "report.md").read_text())
                else:
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
                if host == "codex" and mode in ("shapes", "glob-session"):  # session を読めず、実効の権限の記録を探せない
                    self.assertEqual((rc, rec["status"]), (2, "failed"), rec)
                    self.assertIn("確かめられない", rec["problems"][0])
                else:
                    self.assertEqual((rc, rec["status"]), (0, "ok"), rec)  # 報告は読め、落ちない
                if mode == "shapes":  # 文字列でない session は記録しない。Claude Code と Grok は、launcher が起動の前に決めて渡したもの
                    self.assertEqual(rec["session"], None if host == "codex" else self.session_arg(host))
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
                if status == "failed":  # 子から来た値を記録に書けないのは想定外の失敗。続けさせない
                    self.assertFalse(rec["resumable"], rec)
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
                if status == "failed":  # 子から来た値を記録に書けないのは想定外の失敗。続けさせず、続きの案内も付けない
                    self.assertEqual((rec["resumable"], [p for p in rec["problems"] if "--resume" in p]), (False, []), rec)
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

    def test_a_write_child_making_a_hook_executable_is_reported(self):
        for linked in (False, True):  # hook そのもの、作業ツリーの中のスクリプトを指す symlink の hook
            with self.subTest(linked=linked):
                repo, wt = self.worktree(f"hook-chmod-{linked}")
                hook = repo / ".git" / "hooks" / "pre-commit"
                script = (repo / "hook.sh") if linked else hook
                script.write_text("#!/bin/sh\n")
                script.chmod(0o644)  # git は実行できない hook を動かさない
                if linked:
                    hook.symlink_to(script)
                rc, rec = self.run_launch("claude", "hook-chmod", extra=["--write"], cwd=wt)
                self.assertEqual(rec["status"], "failed", rec)
                self.assertTrue([p for p in rec["problems"] if p.endswith("hooks/pre-commit")], rec)

    @unittest.skipIf(os.geteuid() == 0, "root は権限で止められない")
    def test_a_child_locking_its_leftovers_still_writes_the_record(self):
        rc, rec = self.run_launch("claude", "lock-claude-dir")
        os.chmod(self.cwd / ".claude", 0o755)
        self.assertEqual((rec["status"], rec["resumable"]), ("failed", False), rec)  # 子が権限を変えた。続きから起動させない
        self.assertIn("片付けられない", rec["problems"][0])
        target = Path(self.tmp.name) / "locked-target"
        target.mkdir()
        (target / "a.txt").write_text("x\n")
        rc, rec = self.run_launch("claude", "lock-target-dir", extra=["--write"], cwd=target)
        os.chmod(target / "locked-new", 0o755)
        self.assertEqual((rec["status"], rec["resumable"]), ("failed", False), rec)
        self.assertIn("対象の残りを確かめられない", " ".join(rec["problems"]))
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
        self.assertEqual((rc, rec["status"], rec["resumable"]), (2, "failed", False), rec)
        self.assertIn("子の結果を読めない", rec["problems"][0])
        self.assertIn("Traceback", (out / "launcher-error.log").read_text())  # 詳細を残す
        self.assertTrue((out / "record.json").is_file())
        self.assertIn(str(out.resolve()), (self.state / "delegations.jsonl").read_text())  # --out は symlink を解いた形で記録する
        out = Path(self.tmp.name) / "out-blocked-claude"  # session を起動の前に決めた子でも、想定外の失敗の後は続けさせない
        rc, rec = self.run_launch("claude", mode="blockout", extra=("--out", str(out)), env_extra={"FAKE_OUT": str(out)})
        self.assertEqual((rc, rec["status"], rec["resumable"], bool(rec["session"])), (2, "failed", False, True), rec)

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
        rc, rec = self.run_launch("codex", env_extra={"FAKE_THREAD": "t-1"})
        self.assertEqual(rec["session"], "t-1")
        self.assertIsNone(rec["model_effective"])  # UUID の形でない session では探さない
        self.assertEqual((rc, rec["status"]), (2, "failed"), rec)  # 実効の権限を確かめられない結果は使わない
        near = self.cfg / "codex-home" / "sessions" / "rollout-1-deadbeef-1234.jsonl"
        near.write_text(json.dumps({"type": "turn_context", "payload": {"model": "other"}}) + "\n")
        rc, rec = self.run_launch("codex", env_extra={"FAKE_THREAD": "deadbeef-1234"})
        self.assertIsNone(rec["model_effective"])
        rc, rec = self.run_launch("codex")
        self.assertEqual((rc, rec["status"], rec["execution"]), (0, "ok", "yes"), rec)
        self.assertEqual((rec["model_effective"]["writable"], rec["model_effective"]["network"]), ([":tmpdir"], "restricted"))
        argv = json.loads(self.args_file.read_text())
        self.assertNotIn("-s", argv)  # sandbox_mode は default_permissions より優先される
        self.assertNotIn("--approve-for-me", argv)
        self.assertIn('default_permissions="bbwrit-read-only"', argv)
        self.assertIn('permissions.bbwrit-read-only.extends=":read-only"', argv)
        self.assertEqual(fs_rules(argv).get(":tmpdir"), "write")
        self.assertIn("報告の形", argv[-1])  # 報告の約束を子に渡す
        self.assertIn('"execution": {"completed"', argv[-1])  # 実行の欄と、写して動かす場所
        self.assertIn("$TMPDIR", argv[-1])
        # 約束の意味を決める文は、文ごと固定する（語の有無だけでは、意味を逆にした書き換えが通った）
        for sentence in ("execution.completed は、この依頼で対象の試験・検査を最後まで実行できたか。yes は実行して、試験自身の合否を得た"
                         "（試験が退行を見つけて落ちたのも yes）。no は実行できなかったか途中で止まった。試験が環境・権限で止まった（一時ディレクトリを作れない、"
                         "Read-only file system、Permission denied、コマンドが無い）なら、試験のコマンドの終了コードに関わらず no。not-needed はこの依頼に"
                         "実行が要らない。commands には実行したコマンドと終了コードを書く（yes なら 1 つ以上）。\n",
                         "対象に書く試験・検査は、書ける一時ディレクトリ（$TMPDIR。無ければ /tmp）に対象を写して（cp -r など）、そこで動かす。"):
            self.assertIn(sentence, argv[-1])
        # 権限を変える設定は、この 3 つだけ（sandbox_mode・-s を足すと profile より優先され、別の書ける場所を足すと広がる）
        conf = [argv[i + 1] for i, x in enumerate(argv) if x in ("-c", "--config") and not argv[i + 1].startswith("model_reasoning_effort")]
        self.assertCountEqual([c.split("=", 1)[0] for c in conf], ["default_permissions", "permissions.bbwrit-read-only.extends",
                                                                  "permissions.bbwrit-read-only.filesystem"])
        self.assertTrue((self.state / "delegations.jsonl").read_text().strip())

    def test_codex_read_only_child_cannot_read_credential_places(self):
        # 読み取り専用の Claude Code の子と同じく、認証情報の置き場と専用ログインは読ませない（-s read-only のころは読めた）。
        # 無い場所・symlink・拒む場所の下の場所は書かない（bwrap が起動しなかった）。codex 本体を含む置き場は、本体の入った項目を除いて拒む
        home = self.home
        for d in (".ssh", ".config/gh", ".config/git", ".codex/packages/bin", ".codex/sessions", "elsewhere/aws"):
            (home / d).mkdir(parents=True)
        (home / ".codex" / "auth.json").write_text("{}")
        (home / ".aws").symlink_to(home / "elsewhere" / "aws")
        (home / ".gnupg").symlink_to(home / "missing")  # 行き先の無い symlink（後で実物に替える）
        real = home / ".codex" / "packages" / "bin" / "codex"
        real.write_text(f"#!/bin/sh\nexec {self.bin / 'codex'} \"$@\"\n")  # 本体はホームの .codex の下にある
        real.chmod(0o755)
        rc, rec = self.run_launch("codex", path=f"{real.parent}:/usr/bin:/bin")
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        argv = json.loads(self.args_file.read_text())
        rules = fs_rules(argv)
        import re
        h = str(home.resolve())
        self.assertEqual(rules.get(":tmpdir"), "write")
        for denied in (f"{h}/.ssh", f"{h}/.config/gh", f"{h}/elsewhere/aws", f"{h}/.codex/auth.json", f"{h}/.codex/sessions"):
            self.assertEqual(rules.get(denied), "deny", (denied, rules))
        self.assertNotIn("read", rules.values())  # 拒んだ場所の下に read を書かない（本体を実行できなかった）
        for absent in (f"{h}/.config", f"{h}/.config/git",  # Git の設定を含む置き場は中身ごと、Git の設定は拒まない
                       f"{h}/.codex", f"{h}/.codex/packages", f"{h}/.gnupg", f"{h}/.aws", f"{h}/missing", f"{h}/.kube"):
            if absent:
                self.assertNotIn(absent, rules)
        self.assertEqual(rules.get(str(self.cfg.resolve())), "deny")  # 専用ログイン（試験ではホームの外）
        # 普段の専用ログインは ~/.config の下。拒む場所の下の場所は書かない
        import shutil
        nested = home / ".config" / "bb-writ"
        shutil.copytree(self.cfg, nested)
        rc, rec = self.run_launch("codex", path=f"{real.parent}:/usr/bin:/bin", env_extra={"BB_WRIT_CONFIG_DIR": str(nested)})
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        argv = json.loads(self.args_file.read_text())
        rules = fs_rules(argv)
        self.assertEqual(rules.get(f"{h}/.config"), None)  # Git の設定を含むので、中身を 1 つずつ
        self.assertEqual((rules.get(f"{h}/.config/gh"), rules.get(f"{h}/.config/bb-writ")), ("deny", "deny"))
        self.assertNotIn(f"{h}/.config/git", rules)
        self.assertNotIn(f"{h}/.config/bb-writ/codex-home", rules)  # 入れ子は書かない
        # 対象が拒む置き場の下にあっても、対象は読ませる（その兄弟だけ拒む）。symlink の子が本体を含む場所を指すなら拒まない
        proj = home / ".config" / "nvim"
        (proj / "a.txt").parent.mkdir(parents=True)
        (proj / "a.txt").write_text("x\n")
        (home / ".codex" / "current").symlink_to(home / ".codex" / "packages")
        (home / ".codex" / "dangling").symlink_to(home / "nowhere")
        rc, rec = self.run_launch("codex", cwd=proj, path=f"{home / '.codex' / 'current' / 'bin'}:/usr/bin:/bin",
                                  env_extra={"BB_WRIT_CONFIG_DIR": str(nested)})
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        argv = json.loads(self.args_file.read_text())
        rules = fs_rules(argv)
        self.assertNotIn(str(proj.resolve()), rules)
        self.assertEqual(rules.get(f"{h}/.config/gh"), "deny")
        for absent in (f"{h}/.codex/current", f"{h}/.codex/packages", f"{h}/.codex/dangling", f"{h}/nowhere"):
            self.assertNotIn(absent, rules)
        launch = load_launch()
        (home / ".gnupg").unlink()
        for place in launch.SECRET_PLACES:
            q = home / place
            if not os.path.lexists(q):
                q.parent.mkdir(parents=True, exist_ok=True)
                (q.write_text("x") if place in (".netrc", ".git-credentials") else q.mkdir())
        rc, rec = self.run_launch("codex")  # 本体は試験の bin（ホームの外）
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        argv = json.loads(self.args_file.read_text())
        rules = fs_rules(argv)
        for place in launch.SECRET_PLACES:
            if place in (".config", ".aws"):  # Git の設定を含むので中身ごと、symlink は行き先で
                continue
            with self.subTest(place=place):
                self.assertEqual(rules.get(str((home / place).resolve())), "deny", (place, rules))
        # 名前に BMP の外の文字がある置き場も、TOML として壊さない
        odd = home / ".config" / "gh" / ".."  / "x😀"
        (home / ".config" / "x😀").mkdir()
        rc, rec = self.run_launch("codex")
        argv = json.loads(self.args_file.read_text())
        self.assertEqual(fs_rules(argv).get(f"{h}/.config/x😀"), "deny")
        self.assertFalse([x for x in argv if "\\ud83d" in x])  # TOML に無いサロゲート対の \\u にしない
        (home / ".config" / "git" / "credentials").write_text("x")  # Git の認証情報（XDG の置き場）は拒む
        rc, rec = self.run_launch("codex")
        rules = fs_rules(json.loads(self.args_file.read_text()))
        self.assertEqual(rules.get(f"{h}/.config/git/credentials"), "deny")
        self.assertNotIn(f"{h}/.config/git", rules)
        # ~/.config/git が外（dotfiles）を指す symlink でも、その節点を拒まない（git は ~/.config/git/config の道筋で読む）
        import shutil
        shutil.rmtree(home / ".config" / "git")
        (home / "dotfiles" / "git").mkdir(parents=True)
        (home / ".config" / "git").symlink_to(home / "dotfiles" / "git")
        rc, rec = self.run_launch("codex")
        rules = fs_rules(json.loads(self.args_file.read_text()))
        self.assertNotIn(f"{h}/.config", rules)
        self.assertNotIn(f"{h}/dotfiles/git", rules)
        self.assertEqual(rules.get(f"{h}/.config/gh"), "deny")
        # 本体が置き場の根の 1 段下（~/.codex/bin/codex）なら、2 つ上は ~/.codex（根）なので残さず、中の auth.json は拒む
        (home / ".codex" / "bin").mkdir()
        nested = home / ".codex" / "bin" / "codex"
        nested.write_text(f"#!/bin/sh\nexec {self.bin / 'codex'} \"$@\"\n")
        nested.chmod(0o755)
        rc, rec = self.run_launch("codex", path=f"{nested.parent}:/usr/bin:/bin")
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        rules = fs_rules(json.loads(self.args_file.read_text()))
        self.assertEqual(rules.get(f"{h}/.codex/auth.json"), "deny", rules)
        # 本体が置き場の直下にあっても、本体を拒まない
        direct = home / ".codex" / "codex"
        direct.write_text(f"#!/bin/sh\nexec {self.bin / 'codex'} \"$@\"\n")
        direct.chmod(0o755)
        rc, rec = self.run_launch("codex", path=f"{direct.parent}:/usr/bin:/bin")
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        rules = fs_rules(json.loads(self.args_file.read_text()))
        self.assertNotIn(str(direct.resolve()), rules)
        self.assertEqual(rules.get(f"{h}/.codex/auth.json"), "deny")
        # 対象の Git の共通ディレクトリが拒む置き場の下にあっても拒まない。認証情報の symlink が別の拒む場所の中を指すなら、入れ子は書かない
        g = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        main = home / ".claude" / "plugins" / "repo"
        subprocess.run(g + ["init", "-q", str(main)], check=True)
        subprocess.run(g + ["-C", str(main), "commit", "-q", "--allow-empty", "-m", "i"], check=True)
        wt2 = home / "work-wt"
        subprocess.run(g + ["-C", str(main), "worktree", "add", "-q", "-b", "w", str(wt2)], check=True)
        (wt2 / "a.txt").write_text("x\n")
        (home / ".kube").rename(home / ".kube-old")
        (home / ".kube").symlink_to(home / ".ssh" / "kube")
        (home / ".ssh" / "kube").mkdir()
        rc, rec = self.run_launch("codex", cwd=wt2)
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        rules = fs_rules(json.loads(self.args_file.read_text()))
        self.assertNotIn(f"{h}/.claude", rules)
        self.assertNotIn(f"{h}/.claude/plugins", rules)
        self.assertNotIn(str((main / ".git").resolve()), rules)
        self.assertEqual(rules.get(f"{h}/.ssh"), "deny")
        self.assertNotIn(f"{h}/.ssh/kube", rules)
        # 拒んだ場所を symlink の解く前の書き方で開き直す read も捕まえる。祖先を指す symlink でも、組み立てが止まる
        rc, rec = self.run_launch("codex", mode="read-via-link", cwd=wt2, env_extra={"FAKE_THREAD": "0199aaaa-bbbb-7ccc-8ddd-eeeeffff5000"})
        self.assertEqual(rec["status"], "failed", rec)
        self.assertIn("拒めていない", rec["problems"][0])
        (home / ".config" / "home-link").symlink_to(home)
        rc, rec = self.run_launch("codex", cwd=wt2)
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        # ~/.config/git が置き場の根（~/.ssh）を指す symlink なら、Git の設定として残さない（~/.ssh を拒む）
        shutil.rmtree(home / ".config" / "git") if (home / ".config" / "git").is_dir() and not (home / ".config" / "git").is_symlink() else (home / ".config" / "git").unlink()
        (home / ".config" / "git").symlink_to(home / ".ssh")
        rc, rec = self.run_launch("codex")
        rules = fs_rules(json.loads(self.args_file.read_text()))
        self.assertEqual(rules.get(f"{h}/.ssh"), "deny", rules)
        (home / ".config" / "git").unlink()  # 専用ログインそのものを指しても、残さない
        (home / ".config" / "git").symlink_to(self.cfg)
        rc, rec = self.run_launch("codex")
        rules = fs_rules(json.loads(self.args_file.read_text()))
        self.assertEqual(rules.get(str(self.cfg.resolve())), "deny", rules)
        # HOME が空か / なら、ホームの置き場を拒めないので起動しない
        for empty in ("", "/", "//", "/."):
            rc, rec = self.run_launch("codex", env_extra={"HOME": empty})
            self.assertEqual(rec["status"], "unavailable", rec)

    def test_git_config_symlink_is_kept_when_home_is_under_a_symlink(self):
        # macOS の一時ディレクトリ /var は /private/var を指す。ホームが symlink の下にあり、~/.config/git が外（dotfiles）を指す
        # symlink でも、~/.config を丸ごと拒まず、Git の設定の節点を残す（Linux の CI では出ず、macOS の CI だけで落ちた）
        real = Path(self.tmp.name) / "real-root"
        (real / "home" / ".config" / "gh").mkdir(parents=True)
        (real / "home" / "dotfiles" / "git").mkdir(parents=True)
        (Path(self.tmp.name) / "link-root").symlink_to(real)
        home = Path(self.tmp.name) / "link-root" / "home"
        (home / ".config" / "git").symlink_to(home / "dotfiles" / "git")
        rc, rec = self.run_launch("codex", env_extra={"HOME": str(home)})
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        rules = fs_rules(json.loads(self.args_file.read_text()))
        h = str((real / "home").resolve())
        self.assertNotIn(f"{h}/.config", rules)
        self.assertEqual(rules.get(f"{h}/.config/gh"), "deny", rules)
        for kept in (f"{h}/.config/git", f"{h}/dotfiles/git", f"{h}/dotfiles"):  # Git の設定は拒まない
            self.assertNotIn(kept, rules)
        # ~/.config そのものが symlink（dotfiles）の配置でも、~/.config を丸ごと拒まない
        (real / "home" / "dotfiles" / "config" / "gh").mkdir(parents=True)
        (real / "home" / ".config" / "git").unlink()
        shutil.rmtree(real / "home" / ".config")
        (real / "home" / ".config").symlink_to(real / "home" / "dotfiles" / "config")
        (real / "home" / "dotfiles" / "config" / "git").symlink_to(real / "home" / "dotfiles" / "git")
        rc, rec = self.run_launch("codex", env_extra={"HOME": str(home)})
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        rules = fs_rules(json.loads(self.args_file.read_text()))
        self.assertNotIn(f"{h}/dotfiles/config", rules)
        self.assertEqual(rules.get(f"{h}/dotfiles/config/gh"), "deny", rules)
        # ~/.config/git が専用ログインの中を指すなら、Git の設定として残さない
        login = Path(self.tmp.name) / "login-cfg"
        shutil.copytree(self.cfg, login)
        (login / "claude-home").mkdir()
        (real / "home" / "dotfiles" / "config" / "git").unlink()
        (real / "home" / "dotfiles" / "config" / "git").symlink_to(login / "claude-home")
        rc, rec = self.run_launch("codex", env_extra={"HOME": str(home), "BB_WRIT_CONFIG_DIR": str(login)})
        rules = fs_rules(json.loads(self.args_file.read_text()))
        self.assertEqual(rules.get(str(login.resolve())), "deny", rules)

    def test_codex_binary_inside_the_login_keeps_only_the_binary(self):
        # 本体が専用ログインの中（codex-home/bin/codex）にあると、2 つ上は codex-home。中の auth.json は拒む
        cfg = self.home / ".config" / "bb-writ"
        (cfg / "codex-home" / "bin").mkdir(parents=True)
        for f in (".env", ".env.keys"):
            (cfg / f).write_text("X=1\n")
        (cfg / "codex-home" / "auth.json").write_text("{}")
        real = cfg / "codex-home" / "bin" / "codex"
        real.write_text(f"#!/bin/sh\nexec {self.bin / 'codex'} \"$@\"\n")
        real.chmod(0o755)
        rc, rec = self.run_launch("codex", path=f"{real.parent}:/usr/bin:/bin", env_extra={"BB_WRIT_CONFIG_DIR": str(cfg)})
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        rules = fs_rules(json.loads(self.args_file.read_text()))
        a = str((cfg / "codex-home" / "auth.json").resolve())
        self.assertTrue([d for d, acc in rules.items() if acc == "deny" and (a == d or a.startswith(d + "/"))], rules)

    def test_tests_run_under_a_process_limit(self):
        # 試験が偽のコマンドで自分を呼び続けても、増えるのは試験の分だけ（利用者全体の上限を埋めて Claude Code を落とした）
        if not sys.platform.startswith("linux") or os.geteuid() == 0:
            self.skipTest("上限を掛けるのは Linux の一般の利用者だけ")
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
        self.assertNotEqual(soft, resource.RLIM_INFINITY)
        self.assertLess(soft, hard)  # 元の上限より下げてある（ほかのプロセスの余裕を残す）

    def test_live_checks_call_the_launcher_with_its_current_signature(self):
        # 実機の確認の台本は CI で動かないので、launcher の関数の引数が変わっても気づけなかった（command に一時ディレクトリを足した後、
        # verify_parallel_live.py が 2 つの引数で呼んだまま動かなくなっていた）
        import ast
        import inspect
        launch = load_launch()
        for script in sorted((ROOT / "tests").glob("verify_*.py")):
            tree = ast.parse(script.read_text())
            # launch.py を読み込んだ名前（変数の名前に頼らない: spec_from_file_location の行き先が launch.py の変数）
            names = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign) and "launch.py" in ast.dump(n.value) for t in n.targets
                     if isinstance(t, ast.Name)} | {"launcher"}
            mods = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                    and "module_from_spec" in ast.dump(n.value.func) and any(isinstance(a, ast.Name) and a.id in names for a in n.value.args)
                    for t in n.targets if isinstance(t, ast.Name)} | {"launcher"}
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) \
                        and node.func.value.id in mods:
                    with self.subTest(script=script.name, line=node.lineno):
                        f = getattr(launch, node.func.attr, None)
                        self.assertTrue(callable(f), node.func.attr)  # launch.py に無い関数を呼ばない
                        if any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords):
                            continue  # 展開する引数の数は、ここでは決まらない
                        inspect.signature(f).bind(*node.args, **{k.arg: k.value for k in node.keywords})

    def test_skipping_unwritable_places_is_only_for_read_only_file_systems(self):
        from unittest import mock
        for code, skipped in ((errno.EROFS, True), (errno.EACCES, False), (errno.ENOSPC, False)):
            with self.subTest(code=code), mock.patch("tempfile.TemporaryDirectory", side_effect=OSError(code, os.strerror(code))):
                if skipped:
                    with self.assertRaises(unittest.SkipTest):
                        self.writable_dir(Path("/var/tmp"))
                else:
                    try:  # 飛ばさずに失敗として伝わる（飛ばすと、この試験自身が飛ばされて通って見えた）
                        self.writable_dir(Path("/var/tmp"))
                    except unittest.SkipTest:
                        self.fail(f"errno {code} で試験を飛ばした")
                    except OSError:
                        pass
                    else:
                        self.fail("OSError が伝わらない")

    def test_live_check_tells_refused_from_untried_credential_reads(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("verify_live", ROOT / "tests" / "verify_sandbox_live.py")
        live = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(live)
        d = Path(self.tmp.name) / "scratch"
        d.mkdir()
        self.assertEqual(live.credentials_attempt(d), "not-tried")
        (d / "s8").write_text("1\n")
        (d / "s8.err").write_text("cat: x: Permission denied\n")
        self.assertEqual(live.credentials_attempt(d), "refused")
        (d / "s8").write_text("0\n")
        self.assertEqual(live.credentials_attempt(d), "other")
        (d / "s8").write_text("127\n")
        self.assertEqual(live.credentials_attempt(d), "other")
        (d / "s8").write_text("126\n")  # 実行できない（コマンドが拒まれたのでなく、起動できなかった）
        self.assertEqual(live.credentials_attempt(d), "other")
        (d / "s8").write_text("")  # 終了コードを書けなかった
        self.assertEqual(live.credentials_attempt(d), "other")
        (d / "s8").write_text("1\n")
        (d / "s8.err").write_text("cat: x: No such file or directory\n")  # 拒まれたのでなく、別の失敗
        self.assertEqual(live.credentials_attempt(d), "other")
        (d / "s8.err").unlink()
        self.assertEqual(live.credentials_attempt(d), "other")

    def test_codex_read_only_child_keeps_only_its_package_and_drops_secret_env(self):
        # 残すのは、実機で要ると分かったものだけ（codex 本体とそのパッケージ、Git の設定、対象とその共通ディレクトリ）。
        # nvm を ~/.config に入れた node・npm や、dotfiles を指す道具は、名前を挙げた置き場の下なので拒まれる（子の試験は動かず not-run）
        home = self.home
        pkg = home / ".config" / "nvm" / "versions" / "node" / "v22"
        (pkg / "lib" / "node_modules" / "@openai" / "codex" / "bin").mkdir(parents=True)
        (pkg / "lib" / "node_modules" / "@openai" / "codex" / "vendor").mkdir()
        (pkg / "bin").mkdir()
        (pkg / "bin" / "node").write_text("")
        js = pkg / "lib" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        js.write_text(f"#!/bin/sh\nexec {self.bin / 'codex'} \"$@\"\n")
        js.chmod(0o755)
        (pkg / "bin" / "codex").symlink_to(js)
        (home / ".config" / "gh").mkdir()
        dots = home / ".config" / "dotfiles"
        (dots / "bin").mkdir(parents=True)
        (dots / "secrets.env").write_text("X=1\n")
        (home / ".local" / "bin").mkdir(parents=True)
        (home / ".local" / "bin" / "tool").symlink_to(dots / "bin" / "tool")
        rc, rec = self.run_launch("codex", path=f"{home / '.local' / 'bin'}:{pkg / 'bin'}:/usr/bin:/bin")
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        rules = fs_rules(json.loads(self.args_file.read_text()))

        def covered(path):
            k = str(path.resolve())
            return [d for d, acc in rules.items() if acc == "deny" and (k == d or k.startswith(d + "/"))]
        self.assertFalse(covered(pkg / "lib" / "node_modules" / "@openai" / "codex" / "vendor"), rules)  # 本体のパッケージは残す
        for denied in (pkg / "bin" / "node", dots / "secrets.env", dots / "bin", home / ".config" / "gh"):
            self.assertTrue(covered(denied), (denied, rules))
        rc, rec = self.run_launch("codex", path=f"{pkg / 'bin'}:/usr/bin:/bin",
                                  env_extra={"GH_TOKEN": "ghp_fake", "AWS_SECRET_ACCESS_KEY": "x", "MY_API_KEY": "y",
                                             "SSH_AUTH_SOCK": "/tmp/agent", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/x", "GIT_DIR": "/x",
                                             "LC_TIME": "C", "HTTPS_PROXY": "http://proxy.invalid:1", "LC_SECRET_TOKEN": "z"})
        env = json.loads(Path(str(self.args_file) + ".allenv").read_text())
        for name in ("LC_TIME", "HTTPS_PROXY"):  # 言語とプロキシは渡す
            self.assertIn(name, env)
        launch = load_launch()
        for name in set(env) - {"PWD", "SHLVL", "_", "__CF_USER_TEXT_ENCODING"}:  # 子が受け取るのは、許可の一覧と launcher が足す 2 つだけ
            # （包みの sh が足す 3 つと、macOS が子のプロセスに足す __CF_USER_TEXT_ENCODING を除く）
            self.assertTrue(name in launch.READ_ONLY_ENV | {"CODEX_HOME", "TMPDIR"} or name.startswith("LC_") or name.upper().endswith("_PROXY"), name)
            self.assertNotIn(name, {e["name"] for e in launch.secret_env({name: ""})["envVars"]})
        for name in ("GH_TOKEN", "AWS_SECRET_ACCESS_KEY", "MY_API_KEY", "SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS", "GIT_DIR", "LC_SECRET_TOKEN"):
            self.assertNotIn(name, env)  # 許可の一覧の外は子に渡さない
        for name in ("PATH", "HOME", "CODEX_HOME", "TMPDIR"):
            self.assertIn(name, env)

    def test_codex_read_only_child_gets_its_own_writable_tmpdir(self):
        # -s read-only では mktemp -d も失敗し、審査役が試験を動かせなかった。書けるのは起動ごとの一時ディレクトリだけで、
        # それは --out（呼び出し側が選ぶ）でなく、いつも bb-writ の状態の置き場にできる
        rc, rec = self.run_launch("codex", mode="tmpdir")
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        scratch = Path(rec["scratch"])
        self.assertEqual(len(list(scratch.iterdir())), 1)  # 子の mkdtemp はそこにできる（証拠として残す）
        self.assertTrue(self.state.resolve() in scratch.parents)
        self.assertEqual(Path(str(self.args_file) + ".tmpdir").read_text(), str(scratch))
        repo, wt = self.worktree("out-in-repo")
        link = Path(self.tmp.name) / "link"
        link.symlink_to(self.cwd)
        for out in (self.cwd / "out", repo / ".git" / "refs" / "heads" / "x", link / "out2", "rel-out"):  # --out がどこでも
            with self.subTest(out=out):
                rc, rec = self.run_launch("codex", mode="tmpdir", extra=("--out", str(out)), cwd=wt, proc_cwd=self.tmp.name)
                self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
                scratch = Path(rec["scratch"])
                self.assertTrue(self.state.resolve() in scratch.parents, rec)
                self.assertEqual(len(list(scratch.iterdir())), 1)
                self.assertFalse((Path(rec["out"]) / "tmp").exists())
        self.assertEqual(rec["out"], str((Path(self.tmp.name) / "rel-out").resolve()))  # 相対の --out は絶対パスに（macOS の /tmp は /private/tmp）
        # 状態の置き場が対象そのもの・対象の下（深くても、対象の中を指す symlink でも）、Git の管理ディレクトリの中（共有の .git、
        # worktree の管理ディレクトリ、bare）なら、置き場も記録も作らずに止まる
        bare = Path(self.tmp.name) / "bare.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        (Path(self.tmp.name) / "state-into-target").symlink_to(wt / "st")
        (Path(self.tmp.name) / "plain" / ".git").mkdir(parents=True)  # .git という名前のディレクトリ（中身がそろっていなくても）
        linked = Path(self.tmp.name) / "linked-refs"  # .git/refs を外への symlink にする（解いた後の祖先に .git が無い）
        subprocess.run(["git", "init", "-q", str(linked)], check=True)
        (Path(self.tmp.name) / "refs-store").mkdir()
        shutil.rmtree(linked / ".git" / "refs")
        (linked / ".git" / "refs").symlink_to(Path(self.tmp.name) / "refs-store")
        (repo / ".git" / "st").mkdir()
        barelinked = Path(self.tmp.name) / "x.gitdir"  # bare の形（名前は .git でない）で、refs を外への symlink にする
        subprocess.run(["git", "init", "-q", "--bare", str(barelinked)], check=True)
        (Path(self.tmp.name) / "bare-refs-store").mkdir()
        shutil.rmtree(barelinked / "refs")
        (barelinked / "refs").symlink_to(Path(self.tmp.name) / "bare-refs-store")
        (Path(self.tmp.name) / "link-into-git").symlink_to(repo / ".git" / "st")
        for state, sign in ((wt, "対象の中"), (wt / "a" / "b" / "state", "対象の中"), (Path(self.tmp.name) / "state-into-target", "対象の中"),
                            (repo / ".git" / "bb-writ-state", "Git の管理ディレクトリ"),
                            (repo / ".git" / "worktrees" / "out-in-repo-wt" / "bb-writ-state", "Git の管理ディレクトリ"),
                            (bare / "bb-writ-state", "Git の管理ディレクトリ"),
                            (Path(self.tmp.name) / "plain" / ".git" / "st", "Git の管理ディレクトリ"),
                            (linked / ".git" / "refs" / "heads" / "state", "Git の管理ディレクトリ"),
                            (Path(self.tmp.name) / "link-into-git", "Git の管理ディレクトリ"),  # .git の中を指す symlink
                            (barelinked / "refs" / "heads" / "st", "Git の管理ディレクトリ")):  # bare の refs が外への symlink
            with self.subTest(state=state):
                before = sorted(str(x) for x in wt.rglob("*"))
                p = subprocess.run([sys.executable, str(LAUNCH), "--host", "codex", "--cwd", str(wt), "--prompt-file", str(self.prompt)],
                                   env={**os.environ, "PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home), "BB_WRIT_CONFIG_DIR": str(self.cfg),
                                        "BB_WRIT_STATE_DIR": str(state)}, capture_output=True, text=True, timeout=120)
                self.assertEqual(p.returncode, 2, p)
                self.assertIn(sign, p.stderr)
                self.assertEqual(sorted(str(x) for x in wt.rglob("*")), before)  # 対象の中に何も作らない
                self.assertFalse(os.path.lexists(state / "delegations") and not state.is_symlink(), state)  # 置き場も作らない
        # HEAD・objects・refs のどれか 2 つだけがある普通のディレクトリの下では起動する（bare の形は 3 つがそろうとき）
        for missing in ("HEAD", "objects", "refs"):
            with self.subTest(missing=missing):
                partial = Path(self.tmp.name) / f"no-{missing}"
                partial.mkdir()
                for n in {"HEAD", "objects", "refs"} - {missing}:
                    (partial / n).write_text("x\n") if n == "HEAD" else (partial / n).mkdir()
                rc, rec = self.run_launch("codex", cwd=wt, env_extra={"BB_WRIT_STATE_DIR": str(partial / "st")})
                self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        # 判定は読み取り専用の Codex だけ（ほかのホスト・書く子は、一時ディレクトリを書かせない）
        for host, extra in (("claude", ()), ("codex", ("--write",))):
            with self.subTest(host=host, extra=extra):
                rc, rec = self.run_launch(host, cwd=wt, extra=extra, env_extra={"BB_WRIT_STATE_DIR": str(repo / ".git" / "bb-writ-state2")})
                self.assertNotEqual(rec["status"], "unavailable", rec)
        # 子の TMPDIR は symlink を解いた絶対パス（状態の置き場が symlink でも）
        real = Path(self.tmp.name) / "real-state"
        real.mkdir()
        (Path(self.tmp.name) / "state-link").symlink_to(real)
        rc, rec = self.run_launch("codex", mode="tmpdir", env_extra={"BB_WRIT_STATE_DIR": str(Path(self.tmp.name) / "state-link")})
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        self.assertEqual(Path(str(self.args_file) + ".tmpdir").read_text(), rec["scratch"])
        self.assertTrue(rec["scratch"].startswith(str(real.resolve())))

    def test_codex_read_only_wider_than_the_profile_is_failed(self):
        (self.home / ".ssh").mkdir()  # 渡す deny を 2 つ以上にする（1 つだけ欠けた記録を、全部欠けた記録と見分ける）
        # 対象の .codex/config.toml の sandbox_mode は default_permissions より優先され、同じ profile に書ける場所も足せる
        for i, (mode, sign) in enumerate((("wide-profile", "広い"), ("net-profile", "広い"), ("legacy-sandbox", "書けない"),
                                          ("no-rollout", "確かめられない"), ("odd-entry", "確かめられない"), ("no-network", "確かめられない"),
                                          ("odd-access", "確かめられない"), ("odd-path", "確かめられない"), ("slash-tmp-profile", "広い"),
                                          ("outdir-profile", "広い"), ("net-odd", "広い"),
                                          ("wide-then-ok", "広い"), ("ok-then-wide", "広い"), ("narrow-then-ok", "書けない"),
                                          ("ok-then-narrow", "書けない"), ("fs-unknown-then-ok", "確かめられない"),
                                          ("net-unknown-then-ok", "確かめられない"), ("workspace-profile", "広い"), ("deny-tmp", "書けない"),
                                          ("net-open-then-ok", "広い"), ("ok-then-net-open", "広い"),
                                          ("big-wide-line", "確かめられない"), ("odd-deny-path", "確かめられない"), ("bad-json-after", "確かめられない"),
                                          ("bad-utf8-after", "確かめられない"), ("deny-tmp-first", "書けない"), ("path-like-special", "確かめられない"),
                                          ("read-under-deny", "拒めていない"), ("deny-dropped", "拒めていない"), ("arg0-nested", "拒めていない"),
                                          ("one-deny-dropped", "拒めていない"), ("undenied-then-ok", "拒めていない"), ("ok-then-undenied", "拒めていない"),
                                          ("relative-path", "確かめられない"), ("fs-odd-type", "確かめられない"))):
            with self.subTest(mode=mode):  # 記録は session ごと
                rc, rec = self.run_launch("codex", mode=mode, env_extra={"FAKE_THREAD": f"0199aaaa-bbbb-7ccc-8ddd-eeeeffff{1000 + i}"})
                self.assertEqual((rc, rec["status"], rec["resumable"]), (2, "failed", False), rec)  # 権限が分からない子は続けさせない
                self.assertIn(sign, rec["problems"][0])
                self.assertNotIn("report", rec)  # 報告を使わない
                self.assertTrue((Path(rec["out"]) / "report.md").is_file())  # 中身は残す
        rc, rec = self.run_launch("codex", mode="wide-profile", extra=["--write"])  # 書く子は対象に書ける
        self.assertEqual(rec["status"], "ok", rec)
        rc, rec = self.run_launch("codex", mode="deny-entry", env_extra={"FAKE_THREAD": "0199aaaa-bbbb-7ccc-8ddd-eeeeffff2000"})
        self.assertEqual(rec["status"], "ok", rec)  # 拒否の項目は広げない
        rc, rec = self.run_launch("codex", mode="sibling-read", env_extra={"FAKE_THREAD": "0199aaaa-bbbb-7ccc-8ddd-eeeeffff2001"})
        self.assertEqual(rec["status"], "ok", rec)  # 名前の先頭が同じ隣の場所は、拒んだ場所の下ではない
        # Codex 自身の arg0 の read の例外は、専用ログインの tmp/arg0-<名前>/codex-arg0* そのものだけ（OS の名前は問わない）
        for i, (arg0, status) in enumerate((("tmp/arg0-darwin/codex-arg0Zz9", "ok"), ("arg0-linux/codex-arg0Ab", "failed"),
                                            ("tmp/other/codex-arg0Ab", "failed"), ("tmp/arg0-/codex-arg0Ab", "failed"),
                                            ("tmp/arg0-linux/other", "failed"), ("auth.json", "failed"),
                                            ("tmp/arg0-a/b/codex-arg0X", "failed"), ("tmp/foo/arg0-a/codex-arg0X", "failed"),
                                            ("tmp2/arg0-a/codex-arg0X", "failed"), ("tmpZarg0-a/codex-arg0X", "failed"),  # 隣の場所
                                            ("../outside/arg0-a/codex-arg0X", "failed"))):
            with self.subTest(arg0=arg0):
                rc, rec = self.run_launch("codex", env_extra={"FAKE_ARG0": arg0, "FAKE_THREAD": f"0199aaaa-bbbb-7ccc-8ddd-eeeeffff{2100 + i}"})
                self.assertEqual(rec["status"], status, rec)
        # 例外の形の名前が、symlink で認証情報を指す（または認証情報が例外の形を指す）なら、例外にしない
        home_tmp = self.cfg / "codex-home" / "tmp" / "arg0-darwin"
        home_tmp.mkdir(parents=True)
        (self.cfg / "codex-home" / "auth.json").write_text("{}")
        (home_tmp / "codex-arg0Link").symlink_to(self.cfg / "codex-home" / "auth.json")
        (home_tmp / "codex-arg0Real").write_text("")
        (self.cfg / "codex-home" / "auth-link.json").symlink_to(home_tmp / "codex-arg0Real")
        for i, arg0 in enumerate(("tmp/arg0-darwin/codex-arg0Link", "auth-link.json")):
            with self.subTest(arg0=arg0):
                rc, rec = self.run_launch("codex", env_extra={"FAKE_ARG0": arg0, "FAKE_THREAD": f"0199aaaa-bbbb-7ccc-8ddd-eeeeffff{2200 + i}"})
                self.assertEqual(rec["status"], "failed", rec)
                self.assertIn("拒めていない", rec["problems"][0])
        # 専用ログインの置き場が symlink（macOS の /var/folders は /private/var を指す）でも、Codex 自身の arg0 の read は例外のまま
        real_cfg = Path(self.tmp.name) / "real-cfg"
        shutil.copytree(self.cfg, real_cfg)
        (Path(self.tmp.name) / "cfg-link").symlink_to(real_cfg)
        for i, real in enumerate(("", "1")):  # 解く前の形でも、解いた形でも記録されうる
            with self.subTest(real=real):
                rc, rec = self.run_launch("codex", env_extra={"BB_WRIT_CONFIG_DIR": str(Path(self.tmp.name) / "cfg-link"), "FAKE_ARG0_REAL": real,
                                                              "FAKE_THREAD": f"0199aaaa-bbbb-7ccc-8ddd-eeeeffff{2002 + i * 10}"})
                self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        # 引数で広げる・狭める（--add-dir、--config で書ける場所、ネットワーク、extends、sandbox_mode・-s）と、記録の照合が捕まえる
        (self.bin / "codex-real").write_text((self.bin / "codex").read_text())
        (self.bin / "codex-real").chmod(0o755)
        for i, (more, sign) in enumerate(((["--add-dir", str(self.cwd)], "広い"),
                                          (["--config", 'permissions.bbwrit-read-only.filesystem={":root"="write"}'], "広い"),
                                          (['--config=permissions.bbwrit-read-only.filesystem={":slash_tmp"="write"}'], "広い"),
                                          (["-c", "permissions.bbwrit-read-only.network.enabled=true"], "広い"),
                                          (["-c", 'permissions.bbwrit-read-only.extends=":workspace"'], "広い"),
                                          (["-c", 'sandbox_mode="workspace-write"'], "広い"), (["-s", "read-only"], "書けない"),
                                          (["--sandbox", "read-only"], "書けない"))):
            with self.subTest(more=more):
                (self.bin / "codex").write_text(f"#!/bin/sh\nexec {self.bin / 'codex-real'} \"$@\" {' '.join(repr(m) for m in more)}\n")
                rc, rec = self.run_launch("codex", env_extra={"FAKE_THREAD": f"0199aaaa-bbbb-7ccc-8ddd-eeeeffff{3000 + i}"})
                self.assertEqual((rc, rec["status"], rec["exit_code"]), (2, "failed", 0), rec)  # 子は動き、照合が止めた
                self.assertIn(sign, rec["problems"][0])

    def test_write_children_are_not_told_to_copy_and_are_not_not_run(self):
        # 写して動かすのは読み取り専用の子だけ。書く子は作業場所で試験を動かし、実行の欄が無くても「読んだだけ」にしない
        good = GOOD.encode().decode("unicode_escape")
        for host in ("codex", "claude"):
            for text, ran in ((good.replace(', "execution": {"completed": "yes", "commands": [{"command": "python3 -m unittest", "exit": 0}]}', ""), None),
                              (good.replace('"completed": "yes"', '"completed": "no"'), "no")):
              with self.subTest(host=host, ran=ran):
                rc, rec = self.run_launch(host, extra=["--write"], env_extra={"FAKE_TEXT": text})
                self.assertEqual((rc, rec["status"], rec["execution"]), (0, "ok", ran), rec)
                prompt = json.loads(self.args_file.read_text())
                prompt = prompt[-1] if host == "codex" else prompt[prompt.index("-p") + 1]
                self.assertIn('"execution"', prompt)
                self.assertNotIn("写して", prompt)

    def test_codex_read_only_review_survives_the_parent_moving_git(self):
        # 審査の間に親や並行するタスクが ref・設定を動かしても、読み取り専用の子の報告は使える（前後比較で誤って failed にしない）
        repo, wt = self.worktree("parent-moves")
        (repo / "a.txt").write_text("x\n")  # 報告の証拠が指すファイル
        for target in (wt, repo):
            with self.subTest(target=target):
                subprocess.run(["git", "-C", str(target), "branch", "-D", "parent-work"], capture_output=True)
                rc, rec = self.run_launch("codex", mode="parent-moves-git", cwd=target,
                                          env_extra={"FAKE_THREAD": f"0199aaaa-bbbb-7ccc-8ddd-eeeeffff{4000 + (target == repo)}"})
                self.assertEqual((rc, rec["status"]), (0, "ok"), rec)

    def test_network_refusal_counts_only_resolution_and_reachability_failures(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("verify_live", ROOT / "tests" / "verify_sandbox_live.py")
        live = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(live)
        import re
        for err, refused in (("urllib.error.URLError: <urlopen error [Errno -3] Temporary failure in name resolution>", True),
                             ("<urlopen error [Errno -2] Name or service not known>", True), ("[Errno 101] Network is unreachable", True),
                             ("<urlopen error [Errno -5] No address associated with hostname>", True),
                             ("<urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed>", False),
                             ("urllib.error.URLError: <urlopen error Tunnel connection failed: 407 Proxy Authentication Required>", False)):
            with self.subTest(err=err):
                self.assertEqual(bool(re.search(live.NETWORK_REFUSED, err)), refused)

    def test_reports_that_did_not_run_the_checks_are_not_ok(self):
        # 実行が要る審査で、読んだだけの報告を ok と数えた（Codex の read-only で mktemp -d が終了コード 2）
        def report(execution):
            body = {"conclusion": "c", "evidence": [{"what": "読んだ", "where": "a.txt:1"}], "unknowns": []}
            if execution is not None:
                body["execution"] = None if execution == "null" else execution
            return "```json\n" + json.dumps(body, ensure_ascii=False) + "\n```"
        cases = (  # (実行の欄, status, execution)
            (None, "not-run", None),  # 前の形の報告（欄なし）
            ({"completed": "no", "commands": [{"command": "mktemp -d", "exit": 2}]}, "not-run", "no"),
            ({"completed": "yes", "commands": [{"command": "bash tests/self-test.sh"}]}, "invalid-report", "yes"),  # 終了コードが無い
            ({"completed": "yes", "commands": []}, "invalid-report", "yes"),
            ({"completed": "yes", "commands": [{"command": "x", "exit": True}]}, "invalid-report", "yes"),
            ({"completed": "yes", "commands": [{"command": 42, "exit": 0}]}, "invalid-report", "yes"),
            ({"completed": "yes", "commands": [{"command": " ", "exit": 0}]}, "invalid-report", "yes"),
            ({"completed": "yes", "commands": [{"command": "x", "exit": 0.0}]}, "invalid-report", "yes"),
            ({"completed": "yes", "commands": ["sh test.sh"]}, "invalid-report", "yes"),
            ({"completed": "yes", "commands": {"command": "sh test.sh", "exit": 0}}, "invalid-report", "yes"),
            ({"completed": "maybe"}, "invalid-report", None),
            ("null", "invalid-report", None),  # "execution": null は欄が無いのと違う
            ({"completed": "no", "commands": None}, "invalid-report", "no"),
            ({"completed": "no", "commands": [{"command": " ", "exit": 2}]}, "invalid-report", "no"),
            ({"completed": "yes", "commands": [{"command": "x", "exit": "0"}]}, "invalid-report", "yes"),
            ({"completed": "yes"}, "invalid-report", "yes"),  # commands の鍵が無い
            ({"completed": "no", "commands": [{"command": 42, "exit": 2}]}, "invalid-report", "no"),
            ("yes", "invalid-report", None),
            ({"completed": "yes", "commands": [{"command": "bash tests/self-test.sh", "exit": 1}]}, "ok", "yes"),  # 試験が落ちたのも実行できた
            ({"completed": "not-needed"}, "ok", "not-needed"),
        )
        for host in ("codex", "claude", "grok"):
            for execution, status, ran in cases:
                with self.subTest(host=host, execution=execution):
                    rc, rec = self.run_launch(host, cwd=self.grok_target() if host == "grok" else None,
                                              env_extra={"FAKE_TEXT": report(execution)})
                    self.assertEqual((rc, rec["status"]), (0 if status == "ok" else 2, status), rec)
                    if status != "invalid-report":  # 形の違う欄の何を残すかは決めない
                        self.assertEqual(rec["execution"], ran, rec)
                    prompt = json.loads(self.args_file.read_text())
                    prompt = {"codex": lambda: prompt[-1], "claude": lambda: prompt[prompt.index("-p") + 1],
                              "grok": lambda: prompt[prompt.index("-p") + 1]}[host]()
                    self.assertIn("$TMPDIR", prompt)  # 読み取り専用の子には、どのホストでも写して動かす場所を渡す
                    self.assertIn("写して", prompt)
                    if status == "not-run":
                        self.assertTrue(rec["report"]["conclusion"])  # 報告は捨てず、読んだだけの審査として使える
                        self.assertIn("読んだだけ", rec["problems"][0])

    def test_claude_ok_and_read_only_uses_dont_ask(self):
        for d in (".config/gh", ".config/git", ".ssh", ".claude", ".codex"):  # 守る置き場が実際にあるホーム
            (self.home / d).mkdir(parents=True)
            (self.home / d / "x").write_text("x")
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
        self.assertTrue({str(self.cwd.resolve()), str(self.home)} <= set(fs["denyWrite"]))
        home = self.home.resolve()
        for secret in (home / ".config" / "gh", home / ".ssh", home / ".claude", home / ".codex", self.cfg):  # gh のトークン・鍵・各 CLI のログイン
            covered = any(str(secret) == r or str(secret).startswith(r + "/") for r in fs["denyRead"])
            self.assertTrue(covered, secret)
        self.assertFalse(any(str(home / ".config" / "git").startswith(r + "/") or str(home / ".config" / "git") == r for r in fs["denyRead"]))
        self.assertIn("AGENTS.md・CLAUDE.md", argv[argv.index("-p") + 1])  # 自動で読まれない対象の指示を、資料として読ませる

    def record_of(self, d):
        """子が work.py claim・status で受け取る、その作業場所の記録の置き場。"""
        env = {**os.environ, "BB_WRIT_STATE_DIR": str(self.state)}
        p = subprocess.run([sys.executable, str(ROOT / "scripts" / "work.py"), "status", "--dir", str(d)], env=env, capture_output=True, text=True)
        return str(Path(json.loads(p.stdout)["note"]["path"]).parent)

    def test_claude_child_gets_no_parent_claude_env(self):
        # 親の CLAUDE_CODE_PLUGIN_DIRS（利用者の mod）や API の鍵を子に渡さない。渡すのは専用の設定ディレクトリだけ
        # 背景の処理を止める変数は、書く子にも読み取り専用の子にも渡す（json の出力が最後のターンの結果だけになり、報告が消えた）
        for extra in ([], ["--write"]):
            self.run_launch("claude", extra=extra, env_extra={"CLAUDE_CODE_PLUGIN_DIRS": "~/.claude/mods/x", "CLAUDECODE": "1", "CLAUDE_EFFORT": "max", "ANTHROPIC_API_KEY": "k"})
            env = json.loads(Path(str(self.args_file) + ".env").read_text())
            self.assertEqual((sorted(env), env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"]), (["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "CLAUDE_CONFIG_DIR"], "1"), extra)

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
        for host, cwd in (("codex", None), ("grok", self.grok_target())):  # どの子でも比べる
            rc, rec = self.run_launch(host, "home-residue", cwd=cwd, env_extra={"HOME": str(home), "XAI_API_KEY": "k"})
            self.assertEqual((rec["status"], rec["home_residue"]), ("failed", [str(home / ".bash_profile")]), (host, rec))
            (home / ".bash_profile").unlink()

    def test_empty_places_the_child_leaves_in_the_target_fail(self):
        # 時間切れで止めた Claude Code の書く子が、対象に 0 バイトの .bashrc などと、空のディレクトリだけの .claude/ を残した（2.1.310）
        def target(name, git=False):  # 前の場合の残りが次の場合の「前からあるもの」にならないよう、場合ごとに新しい対象を使う
            if git:  # 書く子は worktree で起動する。.git がファイルの作業ツリー
                d = self.worktree(name)[1]
            else:
                d = Path(self.tmp.name) / name
                d.mkdir()
                (d / "a.txt").write_text("x\n")  # 偽の子の報告が証拠に挙げるファイル
            (d / ".existing-empty").write_text("")  # 前からある空のファイルは数えない
            return d.resolve()  # launcher は symlink を解いた場所で報告する（macOS の一時ディレクトリは /private/var の下）
        for name, mode, extra, git in (("write", "cwd-residue", ("--write",), False),
                                       ("hang", "cwd-residue-hang", ("--write", "--timeout", "3"), False),
                                       ("tracked", "cwd-residue", ("--write",), True)):  # Git の作業ツリーでは、index にある空のファイルを数えない
            with self.subTest(name):
                d = target(name, git)
                rc, rec = self.run_launch("claude", mode, extra=list(extra), cwd=d)
                self.assertEqual((rc, rec["status"], rec["resumable"]), (2, "failed", True), rec)  # 空の残りは、確かめて片付けた後に続けられる
                self.assertEqual(rec["problems"][-1], f"--resume {rec['out']} で続きから起動できる")
                self.assertEqual(rec["cwd_residue"], [str(d / ".bashrc"), str(d / ".claude")])
                for f in rec["cwd_residue"]:  # 黙って消さない
                    self.assertTrue(Path(f).exists())
        with self.subTest("read-only"):  # 読み取り専用の子は比べない（並行する審査役の一時的な場所を、互いの残りとして数えない）
            rc, rec = self.run_launch("claude", "cwd-residue", cwd=target("read-only"))
            self.assertEqual((rc, rec["status"], rec.get("cwd_residue")), (0, "ok", None), rec)
        with self.subTest("nothing-left"):
            rc, rec = self.run_launch("claude", extra=["--write"], cwd=target("clean"))
            self.assertEqual((rc, rec["status"], rec["cwd_residue"]), (0, "ok", []), rec)

    @unittest.skipUnless(sys.platform.startswith("linux"), "子の CLI が切り離した子孫を止めるのは Linux だけ（PR_SET_CHILD_SUBREAPER）")
    def test_processes_the_child_detached_do_not_outlive_it(self):
        # Claude Code 2.1.309 の Bash は、コマンドを新しいセッションで起こす。プロセスグループを止めるだけでは、子が終わった後も
        # 試験を回し続け、作業場所にも書けた（Issue #20）。どの子でも、正常に終わったときも時間切れのときも止め、記録に並べる。
        # 見るのは、子孫が 1.5 秒後に書く前に止めたことまでで、照合（Git の管理情報・ホームの比較）より前に止めた順序は見ない（照合は速く終わる）
        sleep = shutil.which("sleep")  # PATH を偽の置き場に変える前に、本物を絶対パスで引く
        bystander = subprocess.Popen([sleep, "600"], start_new_session=True)  # launcher の子孫でないものは止めない
        self.addCleanup(lambda: (bystander.kill(), bystander.wait()))
        cases = (  # (ホスト, 偽の CLI のモード, 引数, 対象（None は Grok の対象）, status)
            ("claude", "orphan", (), self.cwd, "ok"),
            ("claude", "orphan", ("--write",), self.cwd, "ok"),
            ("claude", "orphan-pipe", ("--timeout", "20"), self.cwd, "ok"),  # 子孫が子の出力を開いたままでも、子の CLI の終わりで返る
            ("claude", "orphan-hang", ("--timeout", "3"), self.cwd, "timeout"),
            ("codex", "orphan", (), self.cwd, "ok"),
            ("grok", "orphan", (), None, "ok"),
        )
        for host, mode, extra, cwd, status in cases:
            with self.subTest(host=host, mode=mode, extra=extra):
                cwd = cwd or self.grok_target()  # ホームに書けない環境では、この subTest だけ飛ばす
                (cwd / "late.txt").unlink(missing_ok=True)
                t0 = time.time()
                rc, rec = self.run_launch(host, mode, extra=extra, cwd=cwd, env_extra={"FAKE_SLEEP": sleep})
                took = time.time() - t0
                pids = self.orphans()
                self.assertEqual((len(pids), rec["status"]), (7, status), rec)
                if status == "timeout":  # 時間切れの理由だけ（止めた子の CLI は残りに並ばない）。期限を守る（所要時間で捕まえるのは、2 秒より大きな延びだけ）
                    self.assertEqual(rec["problems"], ["3 秒で終わらなかった", f"--resume {rec['out']} で続きから起動できる"], rec)
                    # 期限は launcher が測る時間で見る。launcher の処理は毎回約 3.43 秒で終わるが、その後、Python のコードの外で
                    # ときどき約 2 秒延びて 5 秒を越えた（9cc1037 でも同じ。15 回に 1〜2 回。原因は突き止めていない。2026-10-25）。
                    # 壁時計では、返らないことだけを捕まえる
                    self.assertLess(rec["duration_s"], 5, rec)
                    self.assertLess(took, 10, rec)
                for pid in pids:
                    with self.assertRaises(ProcessLookupError, msg=f"{pid} が残った: {rec}"):
                        os.kill(pid, 0)
                # 子の CLI（と dotenvx の下の claude）は並べない（時間切れで止めた CLI がまだゾンビになっていなくても）
                self.assertEqual(sorted(int(p.split()[0]) for p in rec["stopped_after_exit"]), sorted(pids), rec)
                self.assertTrue(any(p.endswith(f"{sleep} 600") for p in rec["stopped_after_exit"]), rec)
                self.assertIn(f"{pids[0]} (x) Z 1 ()", rec["stopped_after_exit"])  # 主スレッドが終わって cmdline が空なら、コマンド名を並べる
                if mode == "orphan-pipe":
                    self.assertLess(took, 15, rec)
                time.sleep(2)  # 止めていなければ、ここまでに作業場所へ書く
                self.assertFalse((cwd / "late.txt").exists())
        self.assertIsNone(bystander.poll())
        # 子の実行中に孤児になって終わったものは、待つ間に刈る（subreaper の下にゾンビを溜めない）。止めたものには並べない
        rc, rec = self.run_launch("claude", "orphans-exit", env_extra={"FAKE_SLEEP": sleep})
        self.assertEqual((rc, rec["stopped_after_exit"], Path(str(self.args_file) + ".zombies").read_text()), (0, [], "0"), rec)

    @unittest.skipUnless(sys.platform.startswith("linux"), "子の CLI が切り離した子孫を止めるのは Linux だけ（PR_SET_CHILD_SUBREAPER）")
    def test_a_child_the_launcher_cannot_stop_or_check_fails(self):
        # 止めきれない（子孫も、子の CLI 自身も）・/proc を読めない・subreaper になれない・出力を読めないときは、残ったかもしれないので
        # failed にする（黙って ok にしない。待ち続けない。終わった孤児が続いても、時間切れを守る）。止めるのを 1 回拒まれても、止め続ける（ほかの子孫も、次の周の同じ子孫も）
        sleep = shutil.which("sleep")
        site = Path(self.tmp.name) / "site"
        site.mkdir()
        (site / "sitecustomize.py").write_text(textwrap.dedent('''
            import builtins, ctypes, os, re
            what = os.environ.get("FAKE_SITE")
            if what == "unkillable":  # 偽の子孫だけ、SIGKILL が効かない（D 状態の代わり）
                _kill = os.kill
                def kill(pid, sig):
                    if sig == 9 and (b"600" in open(f"/proc/{pid}/cmdline", "rb").read() or b"(x) Z 1 ()" in open(f"/proc/{pid}/stat", "rb").read()):
                        return
                    _kill(pid, sig)
                os.kill = kill
            if what == "eperm-once":  # 偽の子孫を止めるのを、最初の 1 回だけ拒む
                _kill2, _denied = os.kill, []
                def kill(pid, sig):
                    if sig == 9 and not _denied and b"(x) Z 1 ()" in open(f"/proc/{pid}/stat", "rb").read():
                        _denied.append(pid)
                        raise PermissionError(1, "denied")
                    _kill2(pid, sig)
                os.kill = kill
            if what == "unkillable-cli":  # 子の CLI（偽の dotenvx と claude）に、SIGKILL が効かない
                _kill3, _killpg = os.kill, os.killpg
                fake = os.path.join(os.path.dirname(os.environ["FAKE_ARGS"]), "bin", "").encode()
                def kill(pid, sig):
                    if sig == 9 and fake in open(f"/proc/{pid}/cmdline", "rb").read():
                        return
                    _kill3(pid, sig)
                os.kill, os.killpg = kill, lambda *a: None
            if what == "waitid-storm":  # 終わった孤児が続く（子の CLI が終わったと報告される前に、別の番号を返し続ける）
                _waitid, _waitpid = os.waitid, os.waitpid
                class _Exited:
                    si_pid = 2 ** 22 + 1
                os.waitid = lambda idtype, id, options: _Exited() if idtype == os.P_ALL else _waitid(idtype, id, options)
                os.waitpid = lambda pid, options: (pid, 0) if pid == _Exited.si_pid else _waitpid(pid, options)
            if what == "no-thread":  # 出力を読むスレッドを起こせない
                import threading
                def start(self):
                    raise RuntimeError("スレッドを起こせない")
                threading.Thread.start = start
            if what == "unreadable":
                _open = builtins.open
                builtins.open = lambda f, *a, **k: (_ for _ in ()).throw(PermissionError(13, "denied", f)) if re.fullmatch(r"/proc/\\d+/stat", str(f)) else _open(f, *a, **k)
            if what == "no-subreaper":
                class _Libc:
                    def __init__(self, *a, **k):
                        pass
                    def prctl(self, *a):
                        return -1
                ctypes.CDLL = _Libc
        '''))
        rc, rec = self.run_launch("claude", "orphan", env_extra={"FAKE_SLEEP": sleep, "PYTHONPATH": str(site), "FAKE_SITE": "eperm-once"})
        pids = self.orphans()
        self.assertEqual((rc, rec["status"], sorted(int(p.split()[0]) for p in rec["stopped_after_exit"])), (0, "ok", sorted(pids)), rec)
        for pid in pids:
            with self.assertRaises(ProcessLookupError, msg=f"{pid} が残った: {rec}"):
                os.kill(pid, 0)
        for what, words in (("unkillable", "子が起こしたプロセスを止められない"), ("unreadable", "子の残りを確かめられない"),
                            ("no-subreaper", "起動の前の準備ができない")):
            with self.subTest(what=what):
                rc, rec = self.run_launch("claude", "orphan", env_extra={"FAKE_SLEEP": sleep, "PYTHONPATH": str(site), "FAKE_SITE": what})
                pids = self.orphans()
                self.assertEqual((rc, rec["status"], rec["resumable"]), (2, "failed", False), rec)  # 残ったかもしれないので続けさせない
                self.assertTrue(any(words in p for p in rec["problems"]), rec)
                if what == "no-subreaper":  # 子を起こさない
                    self.assertEqual(pids, [], rec)
                elif what == "unkillable":  # 止められなかった上の段を並べ、止めたものには並べない（下の段は、上の段が生きているので自分の下に来ない）
                    self.assertTrue(any(p.startswith(f"{pids[0]} ") for p in rec["problems"]), rec)
                    self.assertNotIn(pids[0], [int(p.split()[0]) for p in rec["stopped_after_exit"]], rec)
                else:
                    self.assertEqual(rec["stopped_after_exit"], [], rec)
        for host, mode, what, words, closed in (("claude", "orphan-hang", "unkillable-cli", "子が起こしたプロセスを止められない", False),
                                                ("claude", "orphan-hang", "unkillable-cli", "子が起こしたプロセスを止められない", True),
                                                ("claude", "orphan-hang", "no-thread", "スレッドを起こせない", False),
                                                ("claude", "orphan-hang", "waitid-storm", "2 秒で終わらなかった", False)):
            # 子の CLI は 60 秒眠る。止まらなければ待ち続けず（出力を閉じた CLI の終わりも待たない）、止まるなら止める
            with self.subTest(host=host, mode=mode, what=what, closed=closed):
                self.stop_fakes()  # 前の subTest が、止まらない子の CLI として残したもの
                self.addCleanup(self.stop_fakes)
                t0 = time.time()
                rc, rec = self.run_launch(host, mode, extra=("--timeout", "2"),
                                          env_extra={"FAKE_SLEEP": sleep, "PYTHONPATH": str(site), "FAKE_SITE": what, **({"FAKE_CLOSE_OUT": "1"} if closed else {})})
                self.orphans()
                self.assertEqual((rc, rec["status"]), (2, "timeout" if what == "waitid-storm" else "failed"), rec)  # 止めきれれば、続きから起動できる時間切れ
                self.assertTrue(any(words in p for p in rec["problems"]), rec)
                self.assertLess(time.time() - t0, 25 if what == "unkillable-cli" else 10, rec)  # 止まらない CLI は 10 秒待ち、残りを 5 秒止め続ける
                if what == "unkillable-cli":  # 止まらない子の CLI（Popen の子。claude なら偽の dotenvx）を並べる
                    cli = Path(str(self.args_file) + ".cli").read_text().split()[host == "claude"]
                    self.assertTrue(any(p.startswith(f"{cli} ") for p in rec["problems"]), rec)
                    self.assertEqual(rec["session"], self.session_arg(host))  # 出力を読めなくても、起動の前に決めた session は残る
                    self.assertFalse(rec["resumable"], rec)  # 時間切れでも、止めきれないプロセスが残れば続けさせない
                elif what == "no-thread":
                    self.assertEqual(self.fakes(), [], rec)

    def fakes(self) -> list:
        """偽の CLI（この試験の bin の下のもの）のうち、まだ動いているものの pid。"""
        alive = []
        for d in filter(str.isdigit, os.listdir("/proc")):
            try:
                if f"{self.bin}/".encode() in Path(f"/proc/{d}/cmdline").read_bytes():
                    alive.append(int(d))
            except OSError:
                continue
        return alive

    def stop_fakes(self):
        for pid in self.fakes():
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass

    def orphans(self) -> list:
        """偽の子が切り離して起こした子孫（上の段と 6 段の鎖）の pid。試験が落ちても残さないよう、片付けに登録する。"""
        orphan = Path(str(self.args_file) + ".orphan")
        if not orphan.exists():
            return []
        pids = [int(p) for p in orphan.read_text().split()]
        orphan.unlink()
        self.addCleanup(lambda: [stop_leftover(p) for p in pids])
        return pids

    def test_only_the_empty_ssh_dir_claude_code_makes_in_home_is_not_residue(self):
        # 2.1.309 の sandbox は、子を起こすたびにホームへ空の .claude/ssh を作り、残りとして委任が毎回 failed になった。
        # 数えないのは空のディレクトリの ~/.claude/ssh だけで、どのホストでも、~/.claude が前からあってもなくても同じ。消さない
        import shutil
        home = Path(self.tmp.name) / "home"
        h = lambda *p: str(home.joinpath(*p))
        cases = (  # (ホスト, 偽の CLI のモード, 引数, 前から .claude があるか, status, 残り, 後の ~/.claude/ssh)
            ("claude", "home-ssh", (), False, "ok", [], "dir"),
            ("claude", "home-ssh", ("--write",), False, "ok", [], "dir"),
            ("claude", "home-ssh", (), True, "ok", [], "dir"),  # 利用者のホームにはたいてい前から .claude がある
            ("codex", "home-ssh", (), False, "ok", [], "dir"),  # 並行する Claude Code の子が作ったものを、ほかの委任も見る
            ("grok", "home-ssh", (), True, "ok", [], "dir"),
            ("claude", "home-claude-only", (), False, "failed", [h(".claude")], "absent"),  # 空の .claude そのものは数える
            ("claude", "home-ssh-near", (), True, "failed", [h(".claude", "other"), h(".claude", "ssh-agent"), h(".config", "ssh")], "dir"),  # 隣・別の場所の ssh は数える
            ("claude", "home-ssh-file", (), True, "failed", [h(".claude", "ssh")], "file"),  # 同じ名前の空のファイルは数える
            ("claude", "home-ssh-link", (), True, "failed", [h("elsewhere")], "link"),  # symlink は見ない。行き先の空の場所は数える
            ("claude", "home-residue", (), True, "failed", [h(".bash_profile"), h(".config", "pip"), h(".netrc")], "absent"),
        )
        for host, mode, extra, had_claude, status, residue, ssh in cases:
            with self.subTest(host=host, mode=mode, extra=extra, had_claude=had_claude):
                shutil.rmtree(home, ignore_errors=True)
                (home / ".config").mkdir(parents=True)
                if had_claude:
                    (home / ".claude").mkdir()
                    (home / ".claude" / "settings.json").write_text("{}")
                rc, rec = self.run_launch(host, mode, extra=extra, cwd=self.grok_target() if host == "grok" else None,
                                          env_extra={"HOME": str(home), "XAI_API_KEY": "k"})
                self.assertEqual((rec["status"], rec["home_residue"]), (status, residue), rec)
                p = home / ".claude" / "ssh"
                got = "link" if p.is_symlink() else "dir" if p.is_dir() else "file" if p.is_file() else "absent"
                self.assertEqual(got, ssh, rec)  # 消さない
                for r in residue:  # 数えた残りも消さない
                    self.assertTrue(os.path.lexists(r), r)
                if had_claude:
                    self.assertEqual((home / ".claude" / "settings.json").read_text(), "{}")

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
        self.assertEqual((rec["status"], rec["resumable"]), ("failed", False), rec)
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
            self.assertEqual(p.stdout.strip(), str(len("-rw-r--r-- sha256: ") + 64), p.stderr[-500:])  # 権限と中身のハッシュ

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

    def session_arg(self, host) -> str:
        """launcher が起動の前に決めて子に渡した session（Claude Code は --session-id、Grok は -s）。"""
        argv = json.loads(self.args_file.read_text())
        return argv[argv.index({"claude": "--session-id", "grok": "-s"}[host]) + 1]

    def test_a_signal_to_the_launcher_stops_the_child_cleanly_and_keeps_the_record(self):
        """launcher が子を待つ間に TERM・HUP・INT を受けたら、直下の子に TERM を伝え、子が片付けて終わるのを待ち、
        照合して記録を書く（続きから起動できる）。中断で launcher と子をまとめて止めると、子の CLI（Claude Code 2.1.310）が
        対象に .claude/.cc-writes を残し、記録も残らなかった。TERM を無視する子は、--timeout の残りまでで止める。
        子が終わった後（照合の間）に受けたシグナルは、子を止めたと記録しない。猶予の秒数（30 秒）と、2 回目のシグナルの扱いは試験しない。"""
        def launch(mode, sig, timeout="60", ignore_hup=False, then=None):
            for f in (".started", ".term", ".stage"):
                Path(str(self.args_file) + f).unlink(missing_ok=True)
            env = {**os.environ, "HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin", "BB_WRIT_CONFIG_DIR": str(self.cfg),
                   "BB_WRIT_STATE_DIR": str(self.state), "FAKE_MODE": mode, "FAKE_ARGS": str(self.args_file),
                   "FAKE_TEXT": GOOD.encode().decode("unicode_escape")}
            (self.bin / "fake_env.json").write_text(json.dumps({k: v for k, v in env.items() if k.startswith("FAKE_")}))
            p = subprocess.Popen([sys.executable, str(LAUNCH), "--host", "claude", "--cwd", str(self.cwd), "--prompt-file", str(self.prompt),
                                  "--timeout", timeout], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 preexec_fn=(lambda: signal.signal(signal.SIGHUP, signal.SIG_IGN)) if ignore_hup else None)
            deadline = time.time() + 30
            while not Path(str(self.args_file) + ".started").exists() and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(Path(str(self.args_file) + ".stage").exists())
            p.send_signal(sig)
            if then:  # 無視されたシグナルの後も、子は動き続ける
                time.sleep(1)
                self.assertTrue(Path(str(self.args_file) + ".stage").exists())
                self.assertFalse(Path(str(self.args_file) + ".term").exists())
                p.send_signal(then)
            out, _ = p.communicate(timeout=90)
            return p.returncode, json.loads(out)
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            with self.subTest(sig=sig):
                rc, rec = launch("stop", sig)
                self.assertEqual(Path(str(self.args_file) + ".term").read_text(), f"{int(signal.SIGTERM)}\n")  # 子には TERM を 1 回だけ伝える
                self.assertFalse(Path(str(self.args_file) + ".stage").exists())  # launcher は、子が片付けて終わるのを待った
                self.assertFalse((self.cwd / ".claude").exists())
                self.assertEqual(rec["stopped_by_signal"], int(sig))
                self.assertEqual((rc, rec["status"]), (2, "failed"), rec)
                self.assertIn(rec["exit_code"], (143, -signal.SIGTERM), rec)  # 子の CLI が TERM で終わった（包む dotenvx は 143 で返す）
                self.assertIn(f"launcher がシグナル {int(sig)} を受けて、子に TERM を伝えて止めた", rec["problems"])
                self.assertTrue(rec["resumable"], rec)
                self.assertEqual(rec["session"], Path(str(self.args_file) + ".started").read_text())
                self.assertTrue((Path(rec["out"]) / "record.json").is_file())
        real_git = shutil.which("git")  # 偽の git の中から呼ぶ本物（PATH を変える前に引いた絶対パス）
        (self.bin / "git").write_text(f"#!/bin/sh\n[ -e \"{self.args_file}.done\" ] && sleep 3\nexec {real_git} \"$@\"\n")
        (self.bin / "git").chmod(0o755)
        try:  # 子が終わった後、照合の git が遅い間に受けたシグナルは、子を止めたと記録しない
            Path(str(self.args_file) + ".done").unlink(missing_ok=True)
            self.args_file.unlink(missing_ok=True)  # 前の起動の引数（残っていると、子の起動の前に送ってしまう）
            env = {**os.environ, "HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin", "BB_WRIT_CONFIG_DIR": str(self.cfg),
                   "BB_WRIT_STATE_DIR": str(self.state), "FAKE_MODE": "good", "FAKE_ARGS": str(self.args_file),
                   "FAKE_TEXT": GOOD.encode().decode("unicode_escape")}
            (self.bin / "fake_env.json").write_text(json.dumps({k: v for k, v in env.items() if k.startswith("FAKE_")}))
            for g in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x"]):  # 照合が git を呼ぶよう head を持たせる
                subprocess.run([real_git, "-C", str(self.cwd), *g], check=True, capture_output=True)
            p = subprocess.Popen([sys.executable, str(LAUNCH), "--host", "claude", "--cwd", str(self.cwd), "--prompt-file", str(self.prompt)],
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            deadline = time.time() + 30
            while not Path(str(self.args_file)).exists() and time.time() < deadline:  # 偽の子が起動した（引数を書いた）
                time.sleep(0.05)
            Path(str(self.args_file) + ".done").touch()  # ここから git は遅くなる
            time.sleep(2)  # 偽の子は終わり、launcher は照合の git（3 秒）を待っている
            self.assertIsNone(p.poll())  # launcher はまだ記録を書いていない
            p.send_signal(signal.SIGTERM)
            out, _ = p.communicate(timeout=90)
            rec = json.loads(out)
            self.assertNotIn("stopped_by_signal", rec)
            self.assertFalse(any("シグナル" in x for x in rec.get("problems", [])), rec)
        finally:
            (self.bin / "git").unlink()
            Path(str(self.args_file) + ".done").unlink(missing_ok=True)
            subprocess.run(["rm", "-rf", str(self.cwd / ".git")], check=True)
        rc, rec = launch("stop", signal.SIGHUP, ignore_hup=True, then=signal.SIGTERM)  # nohup で無視させた HUP は無視のまま
        self.assertEqual((rec["status"], rec["stopped_by_signal"]), ("failed", int(signal.SIGTERM)), rec)
        rc, rec = launch("stop-deaf", signal.SIGTERM, timeout="4")  # TERM を無視する子は、--timeout の残りで止める
        self.assertEqual(rec["status"], "timeout", rec)
        self.assertIn(f"launcher がシグナル {int(signal.SIGTERM)} を受けて、子に TERM を伝えて止めた", rec["problems"])
        self.assertTrue(any(x.startswith("シグナルを受けて TERM を伝えた後、終わらなかったので止めた") for x in rec["problems"]), rec)
        self.assertFalse(any(x.endswith("秒で終わらなかった") for x in rec["problems"]), rec)
        subprocess.run(["rm", "-rf", str(self.cwd / ".claude")], check=True)

    def test_a_timed_out_child_continues_from_where_it_stopped(self):
        # 時間切れで止めた子を最初から起動し直すと、それまでの作業（会話記録には残っている）を捨て、同じ時間をもう一度払った。
        # 本物の claude -p・grok -p は終わるまで何も出さないので、session は起動の前に決めて渡し、Codex は最初の thread.started から取る。
        # 続きは、同じ session・同じ権限の引数で、新しい依頼文だけを変えて起動する（実物の 3 つの CLI で、SIGKILL の後に文脈が続いた。2026-10-25）
        hang = {"FAKE_HANG": "1", "FAKE_ROLLOUT_APPEND": "1"}
        for host, extra, where in (("claude", (), None), ("claude", ("--write",), "worktree"), ("codex", (), None), ("grok", (), "grok")):
            with self.subTest(host=host, extra=extra):
                cwd = {"worktree": lambda: self.worktree("resume")[1], "grok": self.grok_target}.get(where, lambda: self.cwd)()
                self.prompt.write_text("x を確かめて")
                # 既定でない model・effort で起動する（続きが既定に戻ると見分けられるように）
                rc, rec = self.run_launch(host, extra=[*extra, "--model", "m-test", "--effort", "high", "--timeout", "2"], cwd=cwd, env_extra=hang)
                first, first_tmp = json.loads(self.args_file.read_text()), Path(str(self.args_file) + ".tmpdir").read_text() if host == "codex" else None
                session = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0000" if host == "codex" else self.session_arg(host)  # 偽の Codex の thread
                self.assertEqual((rc, rec["status"], rec["session"]), (2, "timeout", session), rec)
                self.assertEqual(rec["problems"], ["2 秒で終わらなかった", f"--resume {rec['out']} で続きから起動できる"])
                self.assertEqual(rec["load"]["cpus"], os.cpu_count())  # 時間切れが混雑によるかを後で見分ける材料
                self.assertIsInstance(rec["load"]["start"], float)
                self.assertIsInstance(rec["load"]["end"], float)
                if host == "grok":  # 前の home に子が書いたもの（sandbox の外の Grok が読む設定・hooks、記録の中の symlink）
                    old = Path(rec["grok_home"])
                    (old / "hooks").mkdir()
                    (old / "hooks" / "x.toml").write_text("x")
                    (old / "config.toml").write_text("x")
                    secret = Path(self.tmp.name) / "secret"
                    secret.write_text("s")
                    next(old.glob(f"sessions/*/{session}")).joinpath("link").symlink_to(secret)
                self.prompt.write_text("時間切れで止められた。続けて")
                rc, again = self.run_launch(None, extra=["--resume", rec["out"]], env_extra={"FAKE_ROLLOUT_APPEND": "1"})
                self.assertEqual((rc, again["status"], again["session"], again["resumed_from"]), (0, "ok", session, rec["out"]), again)
                self.assertEqual([again[k] for k in ("host", "cwd", "model_requested", "effort_requested", "write")],
                                 [rec[k] for k in ("host", "cwd", "model_requested", "effort_requested", "write")])
                self.assertEqual((again["model_requested"], again["effort_requested"]), ("m-test", "high"))
                self.assertEqual(again["problems"], [])  # ok の記録に続きの案内を付けない
                if host == "claude":  # 続きの子にも背景の処理を使わせない（書く子・読み取り専用の子のどちらも）
                    env = json.loads(Path(str(self.args_file) + ".env").read_text())
                    self.assertEqual(env.get("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"), "1", extra)
                resumed = json.loads(self.args_file.read_text())
                if host == "codex":  # exec resume は exec の引数を受けないので、exec の側に置いたまま、依頼の前に挟む
                    self.assertEqual((resumed[:-3], resumed[-3:-1]), (first[:-1], ["resume", session]))
                    prompt = resumed[-1]
                    # 前の起動と同じ一時ディレクトリ（子がそこへ写した対象に、続きでも書ける）
                    self.assertEqual((again["scratch"], Path(str(self.args_file) + ".tmpdir").read_text()), (rec["scratch"], first_tmp))
                else:
                    flag, again_flag = {"claude": ("--session-id", "--resume"), "grok": ("-s", "-r")}[host]
                    i = resumed.index("-p")
                    prompt = resumed[i + 1]
                    if host == "grok":  # 続きは新しい GROK_HOME で、前の home からその session の記録だけを写す
                        argv, env = resumed[:-1], json.loads(resumed[-1][len("ENV="):])
                        self.assertNotEqual(again["grok_home"], rec["grok_home"])
                        self.assertEqual(env["GROK_HOME"], again["grok_home"])
                        new = Path(again["grok_home"])
                        self.assertEqual(sorted(p.name for p in new.iterdir()), ["auth.json", "sandbox.toml", "sessions"])
                        copied = next(new.glob(f"sessions/*/{session}"))
                        self.assertEqual(sorted(p.name for p in copied.iterdir()), ["updates.jsonl"])  # symlink は写さない
                        self.assertEqual((copied / "updates.jsonl").read_text(), "{}\n{}\n")  # 前の記録に続けて書いた
                        resumed, first = argv, first[:-1]
                        i = resumed.index("-p")
                    self.assertEqual(resumed[:i + 1] + resumed[i + 2:], [again_flag if x == flag else x for x in first[:i + 1] + first[i + 2:]])
                self.assertTrue(prompt.startswith("時間切れで止められた。続けて"), prompt)

    def test_a_timeout_does_not_hide_other_failures(self):
        """時間切れの後も、利用枠・権限の照合・ホームと Git の照合は時間切れより先に決まる（権限が分からない子を続きから起動させない）。"""
        hang = {"FAKE_HANG": "1", "FAKE_ROLLOUT_APPEND": "1"}
        for host, mode, extra, where, words in (("grok", "nosandbox", (), "grok", "Grok の sandbox が約束どおりに効いたか分からない"),
                                                ("codex", "ok-then-wide", (), None, "広い"),
                                                ("claude", "home-residue", (), None, "ホームに空のファイル"),
                                                ("claude", "tamper-main", ("--write",), "worktree", "自分のブランチ以外")):
            with self.subTest(host=host, mode=mode):
                cwd = {"worktree": lambda: self.worktree("hide-" + mode)[1], "grok": self.grok_target}.get(where, lambda: self.cwd)()
                rc, rec = self.run_launch(host, mode, extra=[*extra, "--timeout", "2"], cwd=cwd, env_extra=hang)
                self.assertEqual((rc, rec["status"]), (2, "failed"), rec)
                self.assertTrue(any(words in p for p in rec["problems"]), rec)
                self.assertTrue(any(p.startswith("2 秒で終わらなかった") for p in rec["problems"]), rec)
                self.assertFalse([p for p in rec["problems"] if p.startswith("終了コード")], rec)  # 止めた子の終了コードは、CLI の失敗の理由ではない
                self.assertTrue(rec["session"], rec)
                if mode == "home-residue":  # 空の残りは、確かめて片付けた後に続きから起動できる
                    self.assertTrue(rec["resumable"], rec)
                    for f in sorted(rec["home_residue"], reverse=True):
                        (Path(f).rmdir if Path(f).is_dir() else Path(f).unlink)()
                    rc, again = self.run_launch(None, extra=["--resume", rec["out"]])
                    self.assertEqual((rc, again["status"]), (0, "ok"), again)
                    continue
                # 権限が約束どおりだったか分からない・Git の管理情報が変わった起動は、続きから起動させない（続きの結果に前の不明が残らない）
                self.assertFalse(rec["resumable"], rec)
                self.assertNotIn("codex_turns", rec)
                self.args_file.unlink()
                p = self.run_launch(None, extra=["--resume", rec["out"]], raw=True)
                self.assertEqual(p.returncode, 2, p.stderr)
                self.assertIn("続きから起動できない", p.stderr)
                self.assertFalse(self.args_file.exists())  # 子を起こさない

    def test_a_codex_resume_judges_the_earlier_turns_by_their_own_launch(self):
        # 読み取り専用の Codex の deny は起動の時点の中身から作るので、時間切れと続きの間に置き場が増えると、前の turn には新しい deny が無い。
        # 前の起動で照らし終えた turn は、続きで照らし直さない（照らし直すと、権限の正しい続きがいつも failed になった）
        (self.home / ".config" / "git").mkdir(parents=True)  # ~/.config の中を 1 つずつ拒む形にする
        env = {"FAKE_HANG": "1", "FAKE_ROLLOUT_APPEND": "1"}
        rc, rec = self.run_launch("codex", extra=["--timeout", "2"], env_extra=env)
        self.assertEqual((rec["status"], rec["codex_turns"]), ("timeout", 1), rec)
        (self.home / ".config" / "newtool").mkdir()
        rc, again = self.run_launch(None, extra=["--resume", rec["out"]], env_extra={"FAKE_ROLLOUT_APPEND": "1"})
        self.assertEqual((rc, again["status"], again["codex_turns"]), (0, "ok", 2), again)
        self.assertIn(str((self.home / ".config" / "newtool").resolve()), fs_rules(json.loads(self.args_file.read_text())))

    def test_a_resumed_review_counts_only_for_the_commit_it_started_on(self):
        # 続きの子は、前の起動で前の commit を読んだ。間に commit を進めて続けると、その ok が新しい commit の審査として
        # work.py の --reviewed を通った。続きの head は、前の起動と同じ commit のときだけ書く
        repo = self.worktree("head")[0]
        (repo / "a.txt").write_text("x\n")  # 偽の報告が証拠に挙げるファイル
        subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "a"], check=True)
        hang = {"FAKE_HANG": "1"}
        rc, rec = self.run_launch("claude", extra=["--timeout", "2"], cwd=repo, env_extra=hang)
        h1 = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual((rec["status"], rec["head"]), ("timeout", h1), rec)
        rc, again = self.run_launch(None, extra=["--resume", rec["out"]])
        self.assertEqual((again["status"], again["head"]), ("ok", h1), again)
        self.assertIn("指摘の扱い", again["next"])  # 審査に数える続きの子には、直す前に書く欄を出す
        # 続きの子にも背景の処理を使わせない（時間切れの後の続きは、長い試験の後で起きやすい）
        self.assertEqual(json.loads(Path(str(self.args_file) + ".env").read_text())["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"], "1")
        rc, rec = self.run_launch("claude", extra=["--timeout", "2"], cwd=repo, env_extra=hang)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "2"], check=True)
        rc, again = self.run_launch(None, extra=["--resume", rec["out"]])
        self.assertEqual((again["status"], again["head"]), ("ok", None), again)
        self.assertIn(f"前の起動の head（{h1}）と違う", again["head_lost"])
        self.assertNotIn("next", again)  # head を外した続きの子は審査に数えない

    def test_a_resume_checks_what_it_takes_over_from_the_last_launch(self):
        # 続きが前の起動から引き継ぐもの（Codex の照らし終えた turn、Grok の記録）は、引き継げる形かを続きの起動で確かめる
        hang = {"FAKE_HANG": "1", "FAKE_ROLLOUT_APPEND": "1"}
        with self.subTest("codex-other-thread"):  # 続きの子が別の thread で動いた（前の turn を除けるか分からない）
            rc, rec = self.run_launch("codex", extra=["--timeout", "2"], env_extra={**hang, "FAKE_THREAD": "0199aaaa-bbbb-7ccc-8ddd-eeeeffff9001"})
            rc, again = self.run_launch(None, extra=["--resume", rec["out"]], env_extra={"FAKE_ROLLOUT_APPEND": "1", "FAKE_THREAD": "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0001"})  # 前と違う thread
            self.assertEqual((rc, again["status"], again["resumable"]), (2, "failed", False), again)
            self.assertIn("前と違う（0199aaaa-bbbb-7ccc-8ddd-eeeeffff0001）", again["problems"][0])
        with self.subTest("codex-two-rollouts"):  # 同じ thread の記録ファイルが 2 つ（前の turn を数で除けない）
            rc, rec = self.run_launch("codex", extra=["--timeout", "2"], env_extra=hang)
            other = self.cfg / "codex-home" / "sessions" / "other"
            other.mkdir()
            shutil.copy(next((self.cfg / "codex-home" / "sessions" / "fake").glob("rollout-*")), other / "rollout-2-0199aaaa-bbbb-7ccc-8ddd-eeeeffff0000.jsonl")
            rc, again = self.run_launch(None, extra=["--resume", rec["out"]], env_extra={"FAKE_ROLLOUT_APPEND": "1"})
            shutil.rmtree(other)
            self.assertEqual((rc, again["status"]), (2, "failed"), again)
            self.assertIn("記録ファイルが 1 つでない（2 個）", again["problems"][0])
        grok = []  # Grok の対象は Grok の場面の中で作る（ホームに書けない環境では、その場面だけが飛ぶ。外で作ると後ろの場面がみな飛んだ）

        def grok_cwd():
            grok[:] = grok or [self.grok_target()]
            return grok[0]
        with self.subTest("grok-session-unknown"):  # 続きが session を返さない（前と同じか確かめられない）
            rc, rec = self.run_launch("grok", extra=["--timeout", "2"], cwd=grok_cwd(), env_extra=hang)
            rc, again = self.run_launch(None, "shapes", extra=["--resume", rec["out"]])
            self.assertEqual((rc, again["status"], again["resumable"]), (2, "failed", False), again)
            self.assertIn("session を確かめられないか", again["problems"][0])
        quota = "ERROR: You've hit your usage limit."
        # 続けさせない事実は、どの status の枝が選ばれても残る（利用枠・終了コードが先に決まっても、権限が分からない子を続けさせない）
        # 利用枠は、子が成功しなかったときだけ（時間切れか失敗で、利用枠の文言を出した）
        for host, mode, env, status, words in (("grok", "nosandbox", {"FAKE_STDERR": quota, "FAKE_HANG": "1"}, "quota", "could not be applied"),
                                               ("codex", "wide-profile", {"FAKE_STDERR": quota, "FAKE_EXIT": "1"}, "quota", "広い"),
                                               ("codex", "wide-profile", {"FAKE_EXIT": "1"}, "failed", "広い"),
                                               ("codex", "wide-profile", {"FAKE_STDERR": quota, **hang}, "quota", "広い")):
            with self.subTest("doubt-under-another-status", host=host, env=sorted(env)):
                rc, rec = self.run_launch(host, mode, extra=["--timeout", "2"], cwd=grok_cwd() if host == "grok" else None,
                                          env_extra={**env, "FAKE_THREAD": f"0199aaaa-bbbb-7ccc-8ddd-eeeeffff3{len(env)}{int('HANG' in str(env))}{int('EXIT' in str(env))}"})
                self.assertEqual((rec["status"], rec["resumable"]), (status, False), rec)
                self.assertTrue(any(words in p for p in rec["problems"]), rec)
                self.assertFalse([p for p in rec["problems"] if "--resume" in p], rec)  # 続けられない記録に、続きの案内を付けない
                self.assertNotIn("codex_turns", rec)
                self.args_file.unlink()
                self.assertEqual(self.run_launch(None, extra=["--resume", rec["out"]], raw=True).returncode, 2)
                self.assertFalse(self.args_file.exists())  # 子を起こさない
        with self.subTest("exit-code-in-resume"):  # 続きも、報告の形が整っていても終了コードで失敗にする
            rc, rec = self.run_launch("codex", extra=["--timeout", "2"], env_extra={**hang, "FAKE_THREAD": "0199aaaa-bbbb-7ccc-8ddd-eeeeffff6000"})
            rc, again = self.run_launch(None, extra=["--resume", rec["out"]], env_extra={"FAKE_ROLLOUT_APPEND": "1", "FAKE_THREAD": "0199aaaa-bbbb-7ccc-8ddd-eeeeffff6000", "FAKE_EXIT": "3"})
            self.assertEqual((again["status"], again["problems"][0]), ("failed", "終了コード 3"), again)
        with self.subTest("other-thread-in-timed-out-resume"):  # 時間切れの続きでも、Codex が出した thread は照らす
            rc, rec = self.run_launch("codex", extra=["--timeout", "2"], env_extra={**hang, "FAKE_THREAD": "0199aaaa-bbbb-7ccc-8ddd-eeeeffff8000"})
            rc, again = self.run_launch(None, "wide-then-ok", extra=["--resume", rec["out"], "--timeout", "2"], env_extra={
                **hang, "FAKE_THREAD": "0199aaaa-bbbb-7ccc-8ddd-eeeeffff8001"})
            self.assertEqual((again["status"], again["resumable"]), ("failed", False), again)
            self.assertIn("前と違う（0199aaaa-bbbb-7ccc-8ddd-eeeeffff8001）", again["problems"][0])
        with self.subTest("codex-stopped-before-thread"):  # session が分からない時間切れは、続けられないと書く（読み取り専用なら権限も照らせず failed）
            rc, rec = self.run_launch("codex", extra=["--write", "--timeout", "2"], cwd=self.worktree("early")[1], env_extra={"FAKE_HANG_EARLY": "1"})
            self.assertEqual((rec["status"], rec["resumable"], rec["session"]), ("timeout", False, None), rec)
            self.assertEqual(rec["problems"], ["2 秒で終わらなかった", "session が分からず、続きから起動できない"])
            rc, rec = self.run_launch("codex", extra=["--timeout", "2"], env_extra={"FAKE_HANG_EARLY": "1"})
            self.assertEqual((rec["status"], rec["resumable"]), ("failed", False), rec)
        with self.subTest("error-result-in-resume"):  # 結果が失敗（is_error）なら、終了コード 0 でも CLI の失敗として残す
            rc, rec = self.run_launch("claude", extra=["--timeout", "2"], env_extra=hang)
            rc, again = self.run_launch(None, extra=["--resume", rec["out"]], env_extra={"FAKE_SID": "other", "FAKE_EXIT": "0"})
            self.assertEqual((again["status"], again["resumable"]), ("failed", False), again)
            self.assertIn("終了コード 0", again["problems"])
        with self.subTest("other-thread-under-quota"):  # thread は起動ごとに別（同じ thread の記録に、前の subTest の turn が足されている）
            rc, rec = self.run_launch("codex", extra=["--timeout", "2"], env_extra={**hang, "FAKE_THREAD": "0199aaaa-bbbb-7ccc-8ddd-eeeeffff9000"})
            rc, again = self.run_launch(None, extra=["--resume", rec["out"]], env_extra={
                "FAKE_ROLLOUT_APPEND": "1", "FAKE_THREAD": "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0002", "FAKE_STDERR": quota, "FAKE_EXIT": "1"})
            self.assertEqual((again["status"], again["resumable"]), ("quota", False), again)
            self.assertTrue(any("前と違う（0199aaaa-bbbb-7ccc-8ddd-eeeeffff0002）" in p for p in again["problems"]), again)
        for env in ({"FAKE_STDERR": quota, "FAKE_EXIT": "1"}, {}):  # 続きが turn を書かずに利用枠で止まる・報告を返す（その子の権限を照らせない）
            with self.subTest("no-new-turn", env=sorted(env)):
                (self.home / ".config" / "git").mkdir(parents=True, exist_ok=True)
                thread = {"FAKE_THREAD": "0199aaaa-bbbb-7ccc-8ddd-eeeeffff4%03d" % len(env)}
                rc, rec = self.run_launch("codex", extra=["--timeout", "2"], env_extra={**hang, **thread})
                rc, mid = self.run_launch(None, "no-rollout", extra=["--resume", rec["out"]], env_extra={"FAKE_ROLLOUT_APPEND": "1", **thread, **env})
                # 照らすものが無いことを、照らして問題が無かったとは扱わない。その結果は使わず、その記録からは続けさせない
                self.assertEqual((mid["status"], mid["resumable"]), ("quota" if env else "failed", False), mid)
                self.assertTrue(any("確かめられない" in p for p in mid["problems"]), mid)
                self.assertNotIn("codex_turns", mid)
                # 前の記録からは続けられる（照らし終えた turn の数は前の記録にある）
                (self.home / ".config" / f"newtool-4{len(env)}").mkdir()
                rc, again = self.run_launch(None, extra=["--resume", rec["out"]], env_extra={"FAKE_ROLLOUT_APPEND": "1", **thread})
                self.assertEqual((again["status"], again["codex_turns"]), ("ok", 2), again)
        with self.subTest("timed-out-resume"):  # 時間切れになった続きは、その記録から続けられる（結果を出さない CLI の session は照らさない）
            rc, rec = self.run_launch("claude", extra=["--timeout", "2"], env_extra=hang)
            rc, mid = self.run_launch(None, extra=["--resume", rec["out"], "--timeout", "2"], env_extra=hang)
            self.assertEqual((mid["status"], mid["resumable"]), ("timeout", True), mid)
            self.assertIn(f"--resume {mid['out']} で続きから起動できる", mid["problems"])
            rc, again = self.run_launch(None, extra=["--resume", mid["out"]])
            self.assertEqual(again["status"], "ok", again)
        with self.subTest("crash-in-resume"):  # 続きが session を出す前に落ちても、終了コードと CLI の文言を残す
            rc, rec = self.run_launch("codex", extra=["--timeout", "2"], env_extra={**hang, "FAKE_THREAD": "0199aaaa-bbbb-7ccc-8ddd-eeeeffff7000"})
            rc, again = self.run_launch(None, "crash", extra=["--resume", rec["out"]])
            self.assertEqual((again["status"], again["resumable"]), ("failed", False), again)
            self.assertIn("終了コード 1", again["problems"])
            self.assertIn("boom", again["problems"])
        for name, plant in (("not-a-uuid", None), ("two-places", "two"), ("linked-sessions", "sessions"), ("linked-place", "place")):
            with self.subTest(name):  # 前の home の記録が 1 つだけ、symlink を通らずに見つからなければ、新しい home を作らずに止める
                rc, rec = self.run_launch("grok", extra=["--timeout", "2"], cwd=grok_cwd(), env_extra=hang)
                old = Path(rec["grok_home"])
                place = next(old.glob(f"sessions/*/{rec['session']}")).parent
                record = dict(rec)
                if plant is None:  # UUID の形でない session（同じ名前の記録の場所はある）
                    record["session"] = "-" * 36
                    shutil.copytree(place / rec["session"], place / record["session"])
                elif plant == "two":
                    shutil.copytree(place, old / "sessions" / "elsewhere")
                elif plant == "sessions":
                    (old / "sessions").rename(old / "real-sessions")
                    (old / "sessions").symlink_to(old / "real-sessions")
                else:
                    place.rename(old / "real-place")  # sessions の外へ（中に置くと、行き先そのものが記録として見つかる）
                    place.symlink_to(old / "real-place")
                out = Path(self.tmp.name) / ("rec-" + name)
                out.mkdir()
                (out / "record.json").write_text(json.dumps(record))
                homes = sorted((self.state / "grok-homes").iterdir())
                rc, again = self.run_launch(None, extra=["--resume", str(out)])
                self.assertEqual((rc, again["status"]), (2, "unavailable"), again)
                self.assertIn("1 つだけ見つからない", again["problems"][0])
                self.assertEqual(sorted((self.state / "grok-homes").iterdir()), homes)  # 空の home を残さない
        with self.subTest("quota-and-timeout"):  # 利用枠の文言は時間切れより先に決まる。利用枠が戻れば続けられる
            rc, rec = self.run_launch("codex", extra=["--timeout", "2"], env_extra={**hang, "FAKE_STDERR": "ERROR: You've hit your usage limit.",
                                                                     "FAKE_THREAD": "0199aaaa-bbbb-7ccc-8ddd-eeeeffff5000"})  # 本物の thread は起動ごとに別
            self.assertEqual((rc, rec["status"], rec["resumable"]), (2, "quota", True), rec)
            self.assertIn("2 秒で終わらなかった", rec["problems"])
        with self.subTest("dirty-then-clean"):  # 前の起動が未 commit の変更を見たなら、続きの時点で clean でも head を書かない
            repo = self.worktree("dirty")[0]
            (repo / "a.txt").write_text("x\n")
            rc, rec = self.run_launch("claude", extra=["--timeout", "2"], cwd=repo, env_extra=hang)
            self.assertEqual((rec["status"], rec["head"]), ("timeout", None), rec)
            subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "a"], check=True)
            rc, again = self.run_launch(None, extra=["--resume", rec["out"]])
            self.assertEqual((again["status"], again["head"]), ("ok", None), again)

    def test_an_interrogate_reviewer_is_recorded_and_its_resume_stays_one(self):
        """work.py は、危険度が中・高の push・pr で interrogate の審査役の記録を求める（Issue #25）。続きも同じ種類のまま。"""
        last = lambda: json.loads((self.state / "delegations.jsonl").read_text().splitlines()[-1])
        rc, rec = self.run_launch("claude")
        self.assertEqual((rc, rec["interrogate"], last()["interrogate"]), (0, False, False), rec)
        rc, rec = self.run_launch("claude", extra=["--interrogate", "--timeout", "2"], env_extra={"FAKE_HANG": "1"})
        self.assertEqual((rec["status"], rec["interrogate"], last()["interrogate"]), ("timeout", True, True), rec)
        self.assertIs(json.loads((Path(rec["out"]) / "record.json").read_text())["interrogate"], True)
        rc, again = self.run_launch(None, extra=["--resume", rec["out"]])
        self.assertEqual((rc, again["status"], again["interrogate"], last()["interrogate"]), (0, "ok", True, True), again)
        for extra, words in ((["--host", "claude", "--cwd", str(self.cwd), "--write", "--interrogate"], "--write と一緒に渡さない"),
                             (["--resume", rec["out"], "--interrogate"], "一緒に渡さない")):
            with self.subTest(extra=extra):
                self.args_file.unlink(missing_ok=True)
                p = self.run_launch(None, extra=extra, raw=True)
                self.assertEqual(p.returncode, 2, p.stderr)
                self.assertIn(words, p.stderr)
                self.assertFalse(self.args_file.exists())  # 子を起こさない

    def test_resume_takes_the_launch_only_from_the_record(self):
        """続きを違う権限・違う対象で起動しない。続きから起動できない記録では、子を起こさずに止める。"""
        rc, rec = self.run_launch("claude", extra=["--timeout", "2"], env_extra={"FAKE_HANG": "1"})
        out = Path(rec["out"])
        broken = {}
        for name, change in (("no-session", {"session": None}),  # Codex が thread.started を出す前に止まった
                             ("not-resumable", {"resumable": False}), ("old-record", {"resumable": None}),
                             *((f"no-{k}", {k: None}) for k in ("host", "cwd", "model_requested", "effort_requested", "write")),
                             ("write-not-bool", {"write": "false"}), ("host-unknown", {"host": "other"}),
                             ("no-home", {"host": "grok", "grok_home": str(Path(self.tmp.name) / "gone")}),
                             ("no-scratch", {"host": "codex", "write": False, "scratch": None})):
            broken[name] = Path(self.tmp.name) / name
            broken[name].mkdir()
            (broken[name] / "record.json").write_text(json.dumps({**rec, **change}))
        for extra, words in ((["--write"], "一緒に渡さない"), (["--host", "claude"], "一緒に渡さない"), (["--cwd", str(self.cwd)], "一緒に渡さない"),
                             (["--model", "opus"], "一緒に渡さない"), (["--effort", "max"], "一緒に渡さない")):
            with self.subTest(extra=extra):
                self.args_file.unlink(missing_ok=True)
                p = self.run_launch(None, extra=["--resume", str(out), *extra], raw=True)
                self.assertEqual(p.returncode, 2, p.stderr)
                self.assertIn(words, p.stderr)
                self.assertFalse(self.args_file.exists())  # 子を起こさない
        for extra, words in ((["--resume", str(broken["no-session"])], "session"), (["--resume", str(broken["no-home"])], "grok_home"),
                             (["--resume", str(broken["not-resumable"])], "続きから起動できない"), (["--resume", str(broken["old-record"])], "続きから起動できない"),
                             *((["--resume", str(broken[n])], "要るもの") for n in ("no-host", "no-cwd", "no-model_requested", "no-effort_requested",
                                                                                   "no-write", "write-not-bool", "host-unknown")),
                             (["--resume", str(broken["no-scratch"])], "scratch"), (["--resume", str(Path(self.tmp.name) / "missing")], "読めない"),
                             ([], "--host と --cwd が要る")):
            with self.subTest(extra=extra):
                self.args_file.unlink(missing_ok=True)
                p = self.run_launch(None, extra=extra, raw=True)
                self.assertEqual(p.returncode, 2, p.stderr)
                self.assertIn(words, p.stderr)
                self.assertFalse(self.args_file.exists())

    def worktree(self, name="r"):
        repo, wt = self.cwd / name, self.cwd / (name + "-wt")
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "i"], check=True)
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "bb-writ/t1", str(wt)], check=True)
        (wt / "a.txt").write_text("x\n")
        return repo, wt

    def test_only_a_child_counted_as_a_review_is_told_to_write_the_triage(self):
        """work.py が審査に数える子（読み取り専用・ok・head あり）にだけ、直す前に指摘の扱いを書けと出す。
        next は案内の文で、文言は「指摘の扱い」の欄の名前のほかは確かめない。"""
        repo, wt = self.worktree("next")
        (repo / ".git" / "info" / "exclude").write_text("a.txt\n")  # 偽の子の報告が証拠に指すファイル。変更に数えない
        rc, rec = self.run_launch("claude", cwd=wt)
        self.assertEqual((rec["status"], bool(rec["head"])), ("ok", True), rec)
        self.assertIn("指摘の扱い", rec["next"])
        rc, rec = self.run_launch("claude", extra=["--write"], cwd=wt)  # 書く子は、head があっても審査に数えない
        self.assertEqual((rec["status"], bool(rec["head"])), ("ok", True), rec)
        self.assertNotIn("next", rec)
        body = {"conclusion": "c", "evidence": [{"what": "読んだ", "where": "a.txt:1"}], "unknowns": []}  # 実行の欄が無い報告
        rc, rec = self.run_launch("claude", cwd=wt, env_extra={"FAKE_TEXT": "```json\n" + json.dumps(body) + "\n```"})
        self.assertEqual((rec["status"], bool(rec["head"])), ("not-run", True), rec)
        self.assertNotIn("next", rec)
        site = Path(self.tmp.name) / "site-next"  # 記録に書けず failed にした子にも出さない（test_values_that_cannot_be_written_still_write_the_record と同じ仕掛け）
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
        rc, rec = self.run_launch("codex", mode="unwritable-usage", cwd=wt, env_extra={"PYTHONPATH": str(site)})
        self.assertEqual((rec["status"], bool(rec["head"])), ("failed", True), rec)
        self.assertNotIn("next", rec)

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
        self.assertNotIn(str(self.home), reads)  # ホーム全体は拒否しない（bwrap が起動しない）
        self.assertFalse(any(str(wt).startswith(r + "/") or str(wt) == r for r in reads))  # 対象に至る経路は読める
        self.assertIn(str(self.home), sandbox["filesystem"]["denyWrite"])
        self.assertEqual(Path(str(self.args_file) + ".scrub").read_text(), "")  # SCRUB は許可の方針を default に戻すので使わない

    def writable_dir(self, parent: Path):
        """parent の下の使い捨てのディレクトリ。書けない環境（読み取り専用の審査役の sandbox の中など）では、理由を出して試験を飛ばす。"""
        try:
            return tempfile.TemporaryDirectory(dir=parent)
        except OSError as e:
            if e.errno != errno.EROFS:  # 読み取り専用のファイルシステム（sandbox の中）だけ。ほかの失敗は試験の失敗にする
                raise
            self.skipTest(f"{parent} に書けない環境（{e.strerror}）。Grok の読み取り専用の試験は /tmp の外の書ける場所が要る")

    def grok_target(self):
        """Grok の read-only は /tmp に書けるので、読み取り専用の試験の対象は /tmp の外に置く（実物のホームの下。launcher が見るのは試験の一時のホーム）。"""
        d = self.writable_dir(Path.home())
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
        self.assertEqual((rc, rec["status"], rec["session"], rec["model_effective"]), (0, "ok", self.session_arg("grok"), ["grok-4.7-build"]), rec)
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
        var_tmp = self.writable_dir(Path("/var/tmp"))
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
        self.assertFalse([p for p in rec["problems"] if p.startswith("終了コード")], rec)  # CLI は正常に終わった
        rc, rec = self.run_launch("grok", "leadernote", cwd=self.grok_target())
        self.assertEqual(rec["status"], "ok", rec)

    def test_grok_error_message_is_kept(self):
        rc, rec = self.run_launch("grok", "signedout", cwd=self.grok_target())
        self.assertEqual(rec["status"], "failed")
        self.assertIn("Not signed in.", rec["problems"])

    def test_crash_is_failed(self):
        rc, rec = self.run_launch("codex", "crash")
        self.assertEqual((rc, rec["status"], rec["resumable"]), (2, "failed", False))  # 実効の権限を照らせない
        self.assertIn("boom", rec["problems"])
        rc, rec = self.run_launch("codex", env_extra={"FAKE_EXIT": "3"})  # 報告は形が整っていても、終了コードで失敗にする
        self.assertEqual((rc, rec["status"], rec["problems"][0]), (2, "failed", "終了コード 3"), rec)

    def test_quota_is_reported_not_retried_elsewhere(self):
        rc, rec = self.run_launch("codex", "quota")
        self.assertEqual(rec["status"], "quota")
        # 結果が失敗（is_error）なら、終了コード 0 で本文があっても成功ではない（利用枠の文言を照らす）
        rc, rec = self.run_launch("claude", env_extra={"FAKE_EXIT": "0", "FAKE_ERROR": "API Error: 429 rate_limit_error"})
        self.assertEqual(rec["status"], "quota", rec)
        # 利用枠の文言を照らすのは、子が成功しなかったときだけ。成功した子の報告は、stderr の一語では捨てない
        for i, (line, extra, status) in enumerate((("WARN failed to refresh rate limits", {}, "ok"), ("request finished in 1429ms", {}, "ok"),
                                                   ("Reconnecting... 1/5 (unexpected status 429 Too Many Requests)", {}, "ok"),
                                                   ("stream error: last status: 429 Too Many Requests", {"FAKE_EXIT": "1"}, "quota"),
                                                   ("API Error: 429 rate_limit_error", {"FAKE_HANG": "1"}, "quota"),  # 時間切れ（成功していない）
                                                   ("", {"FAKE_TEXT": "", "FAKE_STDERR": "rate limit"}, "quota"),  # 終了コード 0 でも本文が無い
                                                   ("", {"FAKE_MODE": "apierror", "FAKE_ERROR": "exceeded retry limit, last status: 429 Too Many Requests"}, "quota"))):  # CLI のエラーにだけある
            with self.subTest(line=line, extra=sorted(extra)):
                mode = extra.pop("FAKE_MODE", "good")
                rc, rec = self.run_launch("codex", mode, extra=["--timeout", "2"], env_extra={"FAKE_STDERR": line, "FAKE_THREAD": f"0199aaaa-bbbb-7ccc-8ddd-eeeeffff{9100 + i}", **extra})
                self.assertEqual(rec["status"], status, rec)
                if extra.get("FAKE_EXIT"):  # 利用枠でも、失敗の終了コードと CLI の文言を残す
                    self.assertIn(f"終了コード {extra['FAKE_EXIT']}", rec["problems"], rec)
                if status == "ok":
                    self.assertTrue((Path(rec["out"]) / "report.md").is_file())  # 報告を捨てない

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
