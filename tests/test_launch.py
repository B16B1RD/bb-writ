"""launcher（scripts/launch.py）: 別系統の CLI の起動と、報告の検査。偽の CLI を PATH に置いて、成功と失敗の各場合を確かめる。"""

import errno
import json
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
GOOD = ('ok.\\n```json\\n{"conclusion": "x は正しい", "deliver": "yes", "evidence": [{"what": "読んだ", "where": "a.txt:1"}], "unknowns": [], '
        '"execution": {"completed": "yes", "commands": [{"command": "python3 -m unittest", "exit": 0}]}}\\n```')

FAKE_CODEX = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import json, os, sys
    _f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_env.json")  # 子の環境を絞る launcher でも、試験の設定を届ける
    os.environ.update({k: v for k, v in json.load(open(_f)).items() if k not in os.environ})
    if sys.argv[1:2] == ["plugin"]:  # codex plugin …。exec の記録（FAKE_ARGS）は書かず、呼び出しを FAKE_ARGS.plugin に 1 行ずつ足す
        try:  # 標準入力が閉じてある（/dev/null）か。macOS でも同じ判定になるよう、装置と inode で比べる
            _null = os.path.samestat(os.fstat(0), os.stat(os.devnull))
        except OSError:
            _null = False
        _fd = os.open(os.environ["FAKE_ARGS"] + ".plugin", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        os.write(_fd, (json.dumps({"argv": sys.argv[1:], "CODEX_HOME": os.environ.get("CODEX_HOME"),
                                   "stdin_is_devnull": _null}) + "\n").encode())  # 1 回の write
        os.close(_fd)
        import re, shutil, tempfile, time
        _home = os.environ.get("CODEX_HOME") or os.path.join(os.environ["HOME"], ".codex")  # 本物と同じく、無ければ ~/.codex

        def _wait(mark=None):  # 試験が FAKE_ARGS.go を置くまで待つ（上限つき。試験は落ちた道でも .go を置く）
            if mark:
                open(os.environ["FAKE_ARGS"] + mark, "w").close()
            _end = time.time() + 30
            while not os.path.exists(os.environ["FAKE_ARGS"] + ".go") and time.time() < _end:
                time.sleep(0.02)

        def _config():
            try:
                return open(os.path.join(_home, "config.toml"), encoding="utf-8").read()
            except FileNotFoundError:
                return ""
        if sys.argv[2:4] == ["marketplace", "list"]:  # 0.171.0 の --json の形。動きは FAKE_LIST で選ぶ
            _list = os.environ.get("FAKE_LIST", "")
            if _list == "hang":
                _wait()
            # 設定が壊れている・置き場に manifest が無い。本物（0.171.0）は、失敗のとき標準出力に何も出さない。偽物が標準出力に長い行を
            # 先に出すのは、訳の出力に末尾を使うこと（標準エラーの失敗の文が残ること）を見るため
            if _list == "fail":
                print("progress " + "x" * 400)
                print("Error: failed to load marketplace(s): fake manifest is missing", file=sys.stderr)
                sys.exit(1)
            if _list.startswith("out:"):  # 試験が組んだ標準出力（JSON でないもの、形の違う JSON）
                sys.stdout.write(_list[4:])
                sys.exit(0)
            print("WARNING: fake warning", file=sys.stderr)  # 本物も標準エラーに出すことがある（標準出力には混ざらない）
            _items = [{"name": n, "root": json.loads(src), "marketplaceSource": {"sourceType": json.loads(kind), "source": json.loads(src)}}
                      for n, kind, src in re.findall(r'^\[marketplaces\.(\S+?)\]\nsource_type = (".*")\nsource = (".*")$', _config(), re.M)]
            print(json.dumps({"marketplaces": _items}, indent=2), flush=True)
            if _list == "answer-hang":  # 答えを出してから終わらない（上限で止められる）
                _wait()
            sys.exit(1 if _list == "answer-fail" else 0)  # answer-fail: 答えを出してから 1 で終わる
        _how = os.environ.get("FAKE_ADD", "replace")
        _mp = sys.argv[3].split("@", 1)[1] if len(sys.argv) > 3 and "@" in sys.argv[3] else ""
        _base = os.path.join(_home, "plugins", "cache", _mp)
        if sys.argv[2:3] == ["remove"]:  # 本物は写しを消す
            shutil.rmtree(os.path.join(_base, "bb-writ"), ignore_errors=True)
            sys.exit(0)
        if _how in ("hang", "wait"):  # 終わらない／置き換える前に待つ（どちらも .go まで）
            _wait(".adding")
        if _how == "noop":
            sys.exit(0)
        if _how == "fail":  # 標準出力の長い行の後に、標準エラーへ失敗の文（理由に残すのは、つないだものの末尾）
            print("progress " + "x" * 400)
            print("Error: failed to back up plugin cache entry: No such file or directory", file=sys.stderr)
            sys.exit(1)
        _m = re.search(r"^\[marketplaces\." + re.escape(_mp) + r"\]\n(?:[^\[\n].*\n|\n)*?source = (\".*\")$", _config(), re.M)
        if not _m:
            print(f"Error: plugin `bb-writ` was not found in marketplace `{_mp}`", file=sys.stderr)
            sys.exit(1)
        # 本物と同じく、別の場所に全部を組み、古い写しを退けてから、改名で置く（版のディレクトリは 1 つだけ）
        os.makedirs(_base, exist_ok=True)
        _build = tempfile.mkdtemp(prefix="plugin-install-", dir=_base)
        for _d in ("skills", "scripts"):
            shutil.copytree(os.path.join(json.loads(_m.group(1)), _d), os.path.join(_build, "bb-writ", "9.9.9", _d),
                            ignore=shutil.ignore_patterns("__pycache__"))
        _backup = tempfile.mkdtemp(prefix="plugin-backup-", dir=_base)
        if os.path.exists(os.path.join(_base, "bb-writ")):
            os.rename(os.path.join(_base, "bb-writ"), os.path.join(_backup, "bb-writ"))
        if _how == "gap":  # 古い写しを退けた後、新しい写しを置く前（写しが無い瞬間）を、.go まで引き延ばす
            _wait(".gap")
        if _how in ("drop", "hollow"):  # 古い写しを退けて、新しい写しを置かずに 1 で終わる（写しの無い状態を残す）
            shutil.rmtree(_build)
            if _how == "hollow":  # skills の無い版のディレクトリだけを置く（写しと数えない形）
                os.makedirs(os.path.join(_base, "bb-writ", "9.9.9", "scripts"))
            print("progress " + "x" * 400)  # 標準出力の長い行の後に、標準エラーへ失敗の文（理由に残すのは、つないだものの末尾）
            print("Error: failed to activate updated plugin cache entry: I/O error (os error 5)", file=sys.stderr)
            sys.exit(1)
        os.rename(os.path.join(_build, "bb-writ"), os.path.join(_base, "bb-writ"))
        shutil.rmtree(_build); shutil.rmtree(_backup)
        sys.exit(1 if _how == "replace-fail" else 0)
    open(os.environ["FAKE_ARGS"] + ".allenv", "w").write(json.dumps(sorted(k for k in os.environ if not k.startswith("FAKE_"))))
    open(os.environ["FAKE_ARGS"], "w").write(json.dumps(sys.argv[1:]))
    open(os.environ["FAKE_ARGS"] + ".calls", "a").write(json.dumps(sys.argv[1:]) + "\n")  # 起動ごとに 1 行
    resumed = "resume" in sys.argv[1:]
    _n = len(open(os.environ["FAKE_ARGS"] + ".calls").read().splitlines())  # FAKE_<名>_<n> は、n 回目の起動だけの FAKE_<名>
    os.environ.update({k[:-len(f"_{_n}")]: v for k, v in list(os.environ.items()) if k.startswith("FAKE_") and k.endswith(f"_{_n}")})
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
    if resumed and os.environ.get("FAKE_RESUMED_TEXT"):  # 続きでは、報告を直して返す
        text = os.environ["FAKE_RESUMED_TEXT"]
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
        open(os.path.join(sessions, "fake", f"rollout-1-{thread}.jsonl"), "a" if os.environ.get("FAKE_ROLLOUT_APPEND") or resumed else "w", errors="surrogateescape").write("".join(turn(x) for x in turns) + (bad or ""))
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
    if sys.argv[1:3] == ["auth", "status"] or os.environ.get("ANTHROPIC_LOG") == "debug":
        # launcher の、専用ログインの組織の確かめ（claude auth status --json と、ANTHROPIC_LOG=debug の問い合わせ）。既存の記録（FAKE_ARGS など）と
        # 対象には触らず、呼び出しを FAKE_ARGS.login に 1 行ずつ足す。FAKE_LOGIN: auth status の答え（claude.ai・none・garbage・hang・
        # bad-org・num-org・list・api_key）、FAKE_LOGIN_ORG: ログインの組織、FAKE_TOKEN_ORG: トークンの組織（既定はログインと同じ）、
        # FAKE_PROBE: 問い合わせの形（noheader・401・two・hang・stderr・notuuid・exit1）、FAKE_PROBE_HOME: 問い合わせがホームの直下に空のファイルを作る
        import time
        kind = "auth" if sys.argv[1:3] == ["auth", "status"] else "probe"
        try:
            _null = os.path.samestat(os.fstat(0), os.stat(os.devnull))
        except OSError:
            _null = False
        _fd = os.open(os.environ["FAKE_ARGS"] + ".login", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        os.write(_fd, (json.dumps({"kind": kind, "argv": sys.argv[1:], "pid": os.getpid(), "ppid": os.getppid(), "cwd": os.getcwd(), "stdin_is_devnull": _null,
                                   "env": {k: v for k, v in sorted(os.environ.items()) if k.startswith(("CLAUDE", "ANTHROPIC_"))}}) + "\n").encode())
        os.close(_fd)
        login_org = os.environ.get("FAKE_LOGIN_ORG", "1a2b3c4d-1111-4111-8111-aaaaaaaaaaaa")
        if kind == "auth":  # 2.1.330 の欄の形（値は偽）
            how = os.environ.get("FAKE_LOGIN", "claude.ai")
            if how == "hang":
                print("Hangmarkerzq", flush=True)
                time.sleep(60)
            if how == "garbage":
                print("Not logged in"); sys.exit(1)
            if how == "none":
                print(json.dumps({"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty"})); sys.exit(1)
            if how == "list":
                print("[]"); sys.exit(0)
            if how == "api_key":  # 環境に ANTHROPIC_API_KEY が在るとき（orgId 無し）
                print(json.dumps({"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty"})); sys.exit(0)
            org = {"bad-org": login_org + "x", "num-org": 12345}.get(how, login_org)  # bad-org: uuid の後に 1 字
            print(json.dumps({"loggedIn": True, "authMethod": "claude.ai" if how in ("bad-org", "num-org") else how, "apiProvider": "firstParty",
                              "email": "someone@example.invalid", "orgId": org, "orgName": "Fakeorgnamezq", "subscriptionType": "max"}))
            sys.exit(0)
        how, org = os.environ.get("FAKE_PROBE", ""), os.environ.get("FAKE_TOKEN_ORG", login_org)
        if how == "notuuid":  # uuid の後に 1 字
            org = org + "x"
        other = "2c3d4e5f-2222-4222-8222-cccccccccccc" if how == "two" else org
        status = 401 if how == "401" else 200
        head = [] if how in ("noheader", "401") else [f'    "anthropic-organization-id": "{org}",']
        tail = [] if how in ("noheader", "401") else [f'  "anthropic-organization-id": "{other}",']
        # ANTHROPIC_LOG=debug の claude -p の標準出力の形（2.1.330 の抜粋。値は偽。Probemarkerzq は漏れを探す目印）
        lines = ['[log_e93a2a] sending request {', '  method: "POST",', '  url: "https://api.anthropic.com/v1/messages?beta=true",',
                 '  body: { messages: [ { role: "user", content: "Probemarkerzq" } ] },',
                 '  headers: {', '    authorization: "***",', '  },', '}',
                 f'[log_e93a2a, request-id: "req_x"] post https://api.anthropic.com/v1/messages?beta=true succeeded with status {status} in 10ms',
                 '[log_e93a2a] response start {', '  url: "https://api.anthropic.com/v1/messages?beta=true",', f'  status: {status},',
                 '  headers: {', *head, '    "anthropic-ratelimit-unified-7d-status": "allowed",', '  },', '  durationMs: 10,', '}',
                 '  "request-id": "req_x",', *tail, '  "server": "cloudflare",',
                 "} ReadableStream { locked: false, state: 'readable', supportsBYOB: false }"]
        if os.environ.get("FAKE_PROBE_HOME"):  # 確かめが残したもの（子の残りに数えない）
            open(os.path.join(os.environ["HOME"], ".probe-left"), "w").close()
        if how == "stderr":  # ヘッダの記録が標準エラーにだけ出る
            print("\n".join(lines), file=sys.stderr, flush=True)
        else:
            print("\n".join(lines), flush=True)
        if how == "hang":
            time.sleep(60)
        if how in ("401", "exit1"):  # exit1: ヘッダは在るが、終了コード 1
            print(json.dumps({"type": "result", "is_error": True, "result": "Failed to authenticate. API Error: 401"})); sys.exit(1)
        print(json.dumps({"type": "result", "is_error": False, "result": "ok", "session_id": "p-1"}))
        sys.exit(0)
    open(os.environ["FAKE_ARGS"] + ".allenv", "w").write(json.dumps(sorted(k for k in os.environ if not k.startswith("FAKE_"))))
    open(os.environ["FAKE_ARGS"], "w").write(json.dumps(sys.argv[1:]))
    open(os.environ["FAKE_ARGS"] + ".scrub", "w").write(os.environ.get("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB", ""))
    open(os.environ["FAKE_ARGS"] + ".env", "w").write(json.dumps({k: v for k, v in sorted(os.environ.items()) if k.startswith(("CLAUDE", "ANTHROPIC_"))}))
    os.makedirs(".claude/.cc-writes", exist_ok=True)  # 2.1.302 の --restricted が対象に作る空のディレクトリ
    import subprocess
    _calls = os.environ["FAKE_ARGS"] + ".calls"  # FAKE_<名>_<n> は、n 回目の起動（この起動は .calls の行の数 + 1 回目）だけの FAKE_<名>
    _n = (len(open(_calls).read().splitlines()) if os.path.exists(_calls) else 0) + 1
    os.environ.update({k[:-len(f"_{_n}")]: v for k, v in list(os.environ.items()) if k.startswith("FAKE_") and k.endswith(f"_{_n}")})
    mode = os.environ.get("FAKE_MODE")
    if mode == "home-claude-only":  # 空の .claude だけを作る
        os.makedirs(os.path.join(os.environ["HOME"], ".claude"))
    if mode == "home-cli-places":  # 2.1.327 の CLI が、試験を実行した子でホームに作った空のディレクトリ（名前は使った機能で変わる）
        for d in ("bridge-spawn", "child-logs", "daemon", "file-history", "ssh", "made-up-by-a-later-version"):
            os.makedirs(os.path.join(os.environ["HOME"], ".claude", d), exist_ok=True)
        os.makedirs(os.path.join(os.environ["HOME"], ".config", "anthropic"))
    if mode == "home-cli-places-near":  # 同じ場所でも、空のファイルと、ほかの場所の空のディレクトリは数える
        home = os.environ["HOME"]
        os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
        open(os.path.join(home, ".claude", "empty-file"), "w").close()
        open(os.path.join(home, ".config", "anthropic"), "w").close()
        os.makedirs(os.path.join(home, ".anthropic")); os.makedirs(os.path.join(home, ".config", "claude"))
        os.makedirs(os.path.join(home, ".claude", "kept")); open(os.path.join(home, ".claude", "kept", "x"), "w").write("x")  # 中身のあるものは残りでない
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
    if os.environ.get("FAKE_APPEND_FILE"):  # 対象の中の 1 つのファイルに書き足す（include の先・hooks・設定など）
        place = os.environ["FAKE_APPEND_FILE"].replace("@ARGS@", os.environ["FAKE_ARGS"])
        os.makedirs(os.path.dirname(place) or ".", exist_ok=True)
        open(place, "a").write(os.environ["FAKE_APPEND_TEXT"].replace("@ARGS@", os.environ["FAKE_ARGS"]))
        os.chmod(place, 0o755)
    if os.environ.get("FAKE_MODIFY"):  # 追跡するファイルの更新時刻だけを進める（大きさが同じなので、status は filter を通して中身を比べ、index を書き直す）
        import time as _t
        os.utime(os.environ["FAKE_MODIFY"], (_t.time() + 5, _t.time() + 5))
    if os.environ.get("FAKE_LEAVE_EMPTY"):  # 対象の直下に空のファイルを残す（cwd_residue が index と照らす道を通す）
        open("left-empty", "w").close()
    if os.environ.get("FAKE_PUT"):  # 子が置き換える・消すもの: [種類, 道筋, 値] の並び（@COMMON@ は共通ディレクトリ、@GITDIR@ は作業場所の管理ディレクトリ）
        for _kind, _place, _value in json.loads(os.environ["FAKE_PUT"]):
            for _mark, _flag in (("@COMMON@", "--git-common-dir"), ("@GITDIR@", "--git-dir")):
                if _mark in _place:
                    _place = _place.replace(_mark, subprocess.run(["git", "rev-parse", "--path-format=absolute", _flag],
                                                                  capture_output=True, text=True).stdout.strip())
            if os.path.lexists(_place):
                os.unlink(_place)
            if _kind == "hex":  # バイト列（UTF-8 でないものも）
                open(_place, "wb").write(bytes.fromhex(_value))
            elif _kind == "fill":  # [繰り返すバイト列, 回数, 末尾]（環境変数に載らない大きさ）
                open(_place, "wb").write(bytes.fromhex(_value[0]) * _value[1] + bytes.fromhex(_value[2]))
            elif _kind == "fifo":
                os.mkfifo(_place)
            elif _kind == "symlink":
                os.symlink(_value, _place)
            elif _kind == "sparse":  # 中身の無い大きなファイル
                open(_place, "wb").truncate(_value)
    if mode == "git-init-tamper":  # Git でない対象で git init し、設定に命令を置く（launcher は起動の前に照合する snapshot を持たない）
        import shlex
        subprocess.run(["git", "init", "-q"])
        subprocess.run(["git", "config", "core.fsmonitor", "touch " + shlex.quote(os.environ["FAKE_ARGS"] + ".pwned")])
    if mode == "tamper-submodule-filter":  # 入れ子のリポジトリの設定に filter を置き、サブモジュールとして index に足す（入れ子に入ると走る）
        import shlex, time as _t
        subprocess.run(["git", "init", "-q", "nested"])
        open("nested/b.txt", "w").write("b\n")
        open("nested/.gitattributes", "w").write("b.txt filter=y\n")
        subprocess.run(git + ["-C", "nested", "add", "b.txt", ".gitattributes"])
        subprocess.run(git + ["-C", "nested", "commit", "-q", "-m", "x"])
        subprocess.run(["git", "-C", "nested", "config", "filter.y.clean", "sh -c " + shlex.quote("touch " + shlex.quote(os.environ["FAKE_ARGS"] + ".pwned") + "; cat")])
        subprocess.run(["git", "add", "nested"], capture_output=True)
        os.utime("nested/b.txt", (_t.time() + 5, _t.time() + 5))
    if mode == "tamper-submodule":  # 入れ子のリポジトリの設定に命令を置き、サブモジュールとして index に足す（snapshot は見ない）
        subprocess.run(["git", "init", "-q", "nested"])
        subprocess.run(git + ["-C", "nested", "commit", "-q", "--allow-empty", "-m", "x"])
        import shlex
        subprocess.run(["git", "-C", "nested", "config", "core.fsmonitor", "touch " + shlex.quote(os.environ["FAKE_ARGS"] + ".pwned")])
        subprocess.run(["git", "add", "nested"], capture_output=True)
    if mode == "tamper-config":  # 親の git で動く設定を足す
        import shlex
        mark = shlex.quote(os.environ['FAKE_ARGS'] + '.pwned')  # fsmonitor と、外さない filter（status が中身を比べると走る）
        open(os.path.join(common(), "config"), "a").write(f"[core]\n\tfsmonitor = touch {mark}\n[filter \"z\"]\n\tclean = \"sh -c 'touch {mark}; cat'\"\n")
        open(".gitattributes", "a").write("a.txt filter=z\n")
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
        os.makedirs("dirs-only/a")  # ファイルを持たないディレクトリも数える（数えないのは Claude Code の空の置き場だけの .claude。#65）
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
    # FAKE_TEXT_FILE: 環境変数に載らない大きさの報告（1 つの値は 128 KiB まで）
    result = open(os.environ["FAKE_TEXT_FILE"], encoding="utf-8").read() if os.environ.get("FAKE_TEXT_FILE") else os.environ["FAKE_TEXT"]
    argv = sys.argv[1:]
    open(os.environ["FAKE_ARGS"] + ".calls", "a").write(json.dumps(argv) + "\n")  # 起動ごとに 1 行
    if "--resume" in argv and os.environ.get("FAKE_RESUMED_TEXT"):  # 続きでは、報告を直して返す
        result = os.environ["FAKE_RESUMED_TEXT"]
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
    if mode == "deliver-no":  # 監査役が差し戻した
        result = result.replace('"deliver": "yes"', '"deliver": "no"')
    if mode == "deliver-missing":  # 監査役が判定を書かなかった
        result = result.replace('"deliver": "yes", ', '')
    if mode == "deliver-upper":  # 判定の値の形が違う
        result = result.replace('"deliver": "yes"', '"deliver": "YES"')
    if mode in ("deliver-not-run", "deliver-no-not-run"):  # 試験を実行できなかった監査役（判定は書いた）
        result = result.replace('"completed": "yes"', '"completed": "no"')
        if mode == "deliver-no-not-run":
            result = result.replace('"deliver": "yes"', '"deliver": "no"')
    if mode == "unwritable-report":  # 試験の sitecustomize が書き出しを失敗させる印
        result = result.replace('"conclusion": "', '"__unwritable__": 1, "conclusion": "')
    if mode == "deep":  # 読めるが、記録（indent 付き）には書けない深さの報告
        result = result.replace('"conclusion": "', '"z": ' + "[" * 3000 + "]" * 3000 + ', "conclusion": "')
    if mode == "surrogate":  # JS の JSON.stringify は単独のサロゲートを \ud83d の形で出す。本文と、報告の JSON の中の両方
        result = "\ud83d" + result.replace('"conclusion": "', '"conclusion": "\\ud83d')
    if mode.startswith("fable-") and sys.argv[sys.argv.index("--model") + 1] == "fable":  # Fable の利用枠が尽きた（429）
        if mode == "fable-slow":  # 失敗を返すまでに時間がかかる（立て直した子の期限を確かめる）
            import time
            time.sleep(2)
        r = {"type": "result", "result": "You've hit your Fable limit", "session_id": sid, "is_error": True, "terminal_reason": "api_error",
             "api_error_status": 429, "total_cost_usd": 0.05 if mode == "fable-ran-out" else 0, "num_turns": 1}  # 尽きたのが途中なら費用がある
        if mode == "fable-overloaded":  # 一時の過負荷（立て直さない）
            r.update(result="API Error: 529 overloaded_error", api_error_status=529)
        if mode == "fable-no-reason":  # 終わり方の欄が無い失敗（立て直さない）
            r.pop("terminal_reason")
        print(json.dumps(r))
        sys.exit(1)
    if mode == "fable-slow" and sys.argv[sys.argv.index("--model") + 1] == "opus":  # 立て直した子が終わらない
        import time
        time.sleep(60)
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
    open(os.environ["FAKE_ARGS"] + ".calls", "a").write(json.dumps(sys.argv[1:]) + "\n")  # 起動ごとに 1 行
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
    text = os.environ["FAKE_RESUMED_TEXT"] if "-r" in sys.argv and os.environ.get("FAKE_RESUMED_TEXT") else os.environ["FAKE_TEXT"]  # 続きでは直して返す
    print(json.dumps({"text": text, "stopReason": "end_turn", "sessionId": sid,
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
    if os.environ.get("ANTHROPIC_LOG") == "debug":  # launcher の、専用ログインの組織の問い合わせ。自分の引数を FAKE_ARGS.login に 1 行足す（本番の子の呼び出しでは残さない）
        _fake = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_env.json")))
        _fd = os.open((os.environ.get("FAKE_ARGS") or _fake["FAKE_ARGS"]) + ".login", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        os.write(_fd, (json.dumps({"kind": "dotenvx", "argv": args}) + "\n").encode())
        os.close(_fd)
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


def claude_readable(p, fs) -> bool:
    """読み取り専用の Claude Code の子の Bash が p を読めるか。denyRead・allowRead のうち p を含む最も深い規則で決まる
    （同じ深さに両方あるときは拒否に倒すが、実物でどちらが勝つかは確かめていない。試験は、並べないことを別に見る）
    （allowRead は拒んだ中を戻し、その中の denyRead はまた拒む。2.1.327 の実物の子で、ホームの拒否・~/.local の戻し・その中の
    状態の置き場の拒否・その中の対象の戻しが、この順で効いた）。"""
    p = str(p)
    rules = [(r, "deny") for r in fs.get("denyRead", [])] + [(r, "allow") for r in fs.get("allowRead", [])]
    hits = [(len(r), k) for r, k in rules if p == r or p.startswith(r.rstrip("/") + "/")]
    return not hits or max(hits)[1] == "allow"


class TestLaunch(unittest.TestCase):
    def setUp(self):
        # 試験の中から直に呼ぶ git（リポジトリを用意する commit など）と、起こす launcher が、利用者の全体の Git 設定（core.hooksPath など）を
        # 読まないように（全体の設定を渡す試験は、env_extra で上書きする）。切り離しそのものを確かめる試験は無い
        # （外しても、全体の設定が試験の git の動きを変えない環境では落ちない）
        from unittest import mock
        no_global = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        no_global.start()
        self.addCleanup(no_global.stop)
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

    def launch_env(self, mode="good", path=None, env_extra=None):
        """偽の CLI・試験のホームと設定で launcher を起こす環境。偽の子に渡す FAKE_* は fake_env.json にも書く（子の環境を絞る launcher でも届く）。
        試験ごとに組んでいたころ、写し損ねて HOME と fake_env.json が抜け、launcher が実物のホームを走査し、偽の子が落ちたまま通った試験があった。"""
        # 起こした launcher が実物のホームと設定を見ないことを確かめる試験は無い（HOME を付け替えずに組んだ試験が、基点の版で通っていた）
        env = {**os.environ, "HOME": str(self.home), "PATH": path if path is not None else f"{self.bin}:/usr/bin:/bin", "BB_WRIT_CONFIG_DIR": str(self.cfg),
               "BB_WRIT_STATE_DIR": str(self.state), "FAKE_MODE": mode, "FAKE_ARGS": str(self.args_file),
               "FAKE_TEXT": GOOD.encode().decode("unicode_escape"), **(env_extra or {})}
        (self.bin / "fake_env.json").write_text(json.dumps({k: v for k, v in env.items() if k.startswith("FAKE_")}))
        return env

    def repo_git(self):
        """試験のリポジトリを動かす git。開発機の全体の除外（~/.config/git/ignore）と設定を読まない。"""
        env = {**os.environ, "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config"),
               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        return lambda *a, cwd=None: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd or self.cwd,
                                                   env=env, check=True, capture_output=True, text=True).stdout

    def run_launch(self, host, mode="good", extra=(), path=None, cwd=None, env_extra=None, proc_cwd=None, raw=False, stdin=None):
        self.orphans()  # 前の回が途中で落ちて読まれなかった偽の子孫を、上書きする前に片付けに回す
        env = self.launch_env(mode, path, env_extra)
        target = ["--host", host, "--cwd", str(cwd or self.cwd)] if host else []  # host が無いのは --resume（記録から取る）
        p = subprocess.run([sys.executable, str(LAUNCH), *target, "--prompt-file", str(self.prompt), *extra],
                           env=env, capture_output=True, text=True, timeout=120, cwd=proc_cwd, stdin=stdin)  # launcher が止まったら、試験ごと止まらずに落とす
        return p if raw else (p.returncode, json.loads(p.stdout))

    def launch_in_background(self, extra, mode="stop", env_extra=None):
        """launcher を起動し、偽の子が動き始めるのを待って返す（シグナル・同時の起動の試験）。"""
        for f in (".started", ".term", ".stage"):
            Path(str(self.args_file) + f).unlink(missing_ok=True)
        env = self.launch_env(mode, env_extra=env_extra)
        p = subprocess.Popen([sys.executable, str(LAUNCH), "--prompt-file", str(self.prompt), *extra], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(p.kill)
        deadline = time.time() + 30
        while not Path(str(self.args_file) + ".started").exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(Path(str(self.args_file) + ".started").exists())
        return p

    def launcher_site(self, body: str) -> dict:
        """launcher のプロセスにだけ、外の世界（時計・ファイルシステム）の振る舞いを差し替える環境。launcher の関数の名前には触れない。
        PYTHONPATH の sitecustomize が、起動したのが launch.py のときだけ body を動かす（偽の子の CLI には効かない）。"""
        site = Path(tempfile.mkdtemp(dir=self.tmp.name, prefix="site-"))
        # Python 3.9 の pathlib は、読み込むときに os.open などを写して持つ（差し替えた関数を後から読むと、メソッドとして呼ばれて落ちる）。
        # launch.py と同じく、差し替えの前に読んでおく
        (site / "sitecustomize.py").write_text("import os, sys\nif os.path.basename(sys.argv[0]) == 'launch.py':\n    import pathlib\n"
                                               + textwrap.indent(textwrap.dedent(body), "    "))
        return {"PYTHONPATH": str(site)}

    def launch_patched(self, patch, extra, mode="good", env_extra=None):
        """launch.py を import し、patch（Python の文）を当ててから main を動かす。起きる時機を決められない場面だけに使う。"""
        env = self.launch_env(mode, env_extra=env_extra)
        code = (f"import os, signal, subprocess, sys\nsys.path.insert(0, {str(LAUNCH.parent)!r})\nimport launch\n{textwrap.dedent(patch)}\n"
                f"sys.argv = ['launch.py', '--prompt-file', {str(self.prompt)!r}, *{list(extra)!r}]\nsys.exit(launch.main())\n")
        p = subprocess.run([sys.executable, "-B", "-c", code], env=env, capture_output=True, text=True, timeout=120)
        self.assertTrue(p.stdout.strip(), p.stderr)
        return p.returncode, json.loads(p.stdout)

    def test_a_stopped_resume_can_still_be_continued(self):
        """続きの子を止めても、続きから起動できる（止めた子は結果を出さないので、session を確かめられないのは権限の不明ではない）。
        実物の Claude Code 2.1 系で、続きを TERM で止めると resumable が偽になり、次の --resume が拒否された。"""
        rc, first = self.run_launch("claude", extra=["--timeout", "2"], env_extra={"FAKE_HANG": "1"})
        p = self.launch_in_background(["--resume", first["out"], "--timeout", "60"])
        p.send_signal(signal.SIGTERM)
        out, err = p.communicate(timeout=60)
        rec = json.loads(out)
        self.assertEqual((rec["status"], rec["resumable"], rec["stopped_by_signal"]), ("failed", True, int(signal.SIGTERM)), rec)
        rc, again = self.run_launch(None, extra=["--resume", rec["out"]])
        self.assertEqual((rc, again["status"]), (0, "ok"), again)

    def test_a_session_found_unsafe_cannot_be_continued_from_an_earlier_record(self):
        """続きの起動で sandbox が効かなかった session は、その前の記録からも続けさせない。照らすものが無かっただけの起動は、前の記録から
        続けられる（test_a_resume_checks_what_it_takes_over_from_the_last_launch の no-new-turn）。"""
        rc, first = self.run_launch("grok", extra=["--timeout", "2"], cwd=self.grok_target(), env_extra={"FAKE_HANG": "1"})
        rc, mid = self.run_launch(None, "nosandbox", extra=["--resume", first["out"]])
        self.assertEqual((mid["status"], mid["resumable"]), ("failed", False), mid)
        self.args_file.unlink()
        p = self.run_launch(None, extra=["--resume", first["out"]], raw=True)
        self.assertEqual(p.returncode, 2, p.stdout)
        self.assertIn("この session は続けない", p.stderr)
        self.assertFalse(self.args_file.exists())  # 子を起こさない

    def test_every_reason_that_bans_a_session_applies_from_an_earlier_record(self):
        """子が権限の外で動いたかもしれない理由は、後の起動の記録にあっても、前の記録からの続きを止める。別の session を返した記録も、頼んだ session で見つかる。"""
        for kind in ("unsafe", "breach", "other-session", "left", "git"):
            with self.subTest(kind):
                rc, first = self.run_launch("claude", extra=["--timeout", "2"], env_extra={"FAKE_HANG": "1"})
                line = {"session": "another" if kind == "other-session" else first["session"], "session_asked": first["session"], "blocked": [kind], "out": "x"}
                with open(self.state / "delegations.jsonl", "a") as f:
                    f.write(json.dumps(line) + "\n")
                self.args_file.unlink()
                p = self.run_launch(None, extra=["--resume", first["out"]], raw=True)
                self.assertEqual(p.returncode, 2, p.stdout)
                self.assertIn(kind, p.stderr)
                self.assertFalse(self.args_file.exists())

    def test_a_reason_of_having_nothing_to_check_keeps_the_earlier_record(self):
        for kind in ("session", "unchecked", "error"):
            with self.subTest(kind):
                rc, first = self.run_launch("claude", extra=["--timeout", "2"], env_extra={"FAKE_HANG": "1"})
                with open(self.state / "delegations.jsonl", "a") as f:
                    f.write(json.dumps({"session": first["session"], "blocked": [kind], "out": "x"}) + "\n")
                rc, again = self.run_launch(None, extra=["--resume", first["out"]])
                self.assertEqual(rc, 0, again)

    def test_a_resume_from_an_older_record_still_sees_later_empty_places(self):
        """後の起動が残した空の場所は、前の記録に戻って続けるときも止める（対象とホームの両方）。"""
        for key in ("cwd_residue", "home_residue"):
            with self.subTest(key):
                rc, first = self.run_launch("claude", extra=["--timeout", "2"], env_extra={"FAKE_HANG": "1"})
                left = self.home / ("left-" + key)
                left.write_text("")
                with open(self.state / "delegations.jsonl", "a") as f:
                    f.write(json.dumps({"session": first["session"], "blocked": [], key: [str(left)], "out": "x"}) + "\n")
                p = self.run_launch(None, extra=["--resume", first["out"]], raw=True)
                self.assertEqual(p.returncode, 2, p.stdout)
                self.assertIn(str(left), p.stderr)
                left.unlink()
                rc, again = self.run_launch(None, extra=["--resume", first["out"]])
                self.assertEqual(rc, 0, again)

    def test_a_breach_is_told_from_having_nothing_to_check_by_the_start_of_the_reason(self):
        """照らせなかった理由だけを unchecked に分ける。理由の文にはパスが入るので、文の途中の一致で分けない（拒めなかった場所の
        道筋に、照らせなかった理由の文の先頭と同じ語が入っていても breach）。"""
        with self.subTest("記録が無い"):
            rc, rec = self.run_launch("codex", "no-rollout")
            self.assertEqual(rec["blocked"], ["unchecked"], rec)
        with self.subTest("続きの記録ファイルが 2 つ"):
            rc, first = self.run_launch("codex", extra=["--timeout", "2"], env_extra={"FAKE_HANG": "1", "FAKE_ROLLOUT_APPEND": "1"})
            other = self.cfg / "codex-home" / "sessions" / "other"
            other.mkdir()
            shutil.copy(next((self.cfg / "codex-home" / "sessions" / "fake").glob("rollout-*")), other / "rollout-2-0199aaaa-bbbb-7ccc-8ddd-eeeeffff0000.jsonl")
            rc, again = self.run_launch(None, extra=["--resume", first["out"]], env_extra={"FAKE_ROLLOUT_APPEND": "1"})
            shutil.rmtree(other)
            self.assertEqual(again["blocked"], ["unchecked"], again)
        for name in ("plain", "Codex の実効の権限を記録（x", "続きの Codex の記録ファイルが x"):  # 拒む専用ログインの置き場の道筋の中の語
            with self.subTest(name):
                cfg = Path(self.tmp.name) / name / "cfg"
                shutil.copytree(self.cfg, cfg, symlinks=True)
                rc, rec = self.run_launch("codex", "deny-dropped", env_extra={"BB_WRIT_CONFIG_DIR": str(cfg)})
                self.assertIn(str(cfg.resolve()), rec["problems"][0], rec)  # 理由の文の途中に、その語が入った
                self.assertEqual(rec["blocked"], ["breach"], rec)

    def test_a_signal_during_the_timeout_grace_does_not_send_a_second_term(self):
        p = self.launch_in_background(["--host", "claude", "--cwd", str(self.cwd), "--timeout", "1"])
        term = Path(str(self.args_file) + ".term")
        deadline = time.time() + 20
        while not term.exists() and time.time() < deadline:
            time.sleep(0.02)
        p.send_signal(signal.SIGTERM)  # 子は 1 秒かけて片付ける
        out, _ = p.communicate(timeout=60)
        rec = json.loads(out)
        self.assertEqual(term.read_text(), f"{int(signal.SIGTERM)}\n")
        self.assertEqual(rec["status"], "timeout", rec)
        self.assertEqual(rec["problems"][1 if rec["problems"][0].startswith("launcher") else 0], "1 秒で終わらなかった", rec)

    def test_a_resume_refuses_while_the_last_launch_left_empty_places(self):
        """前の起動の空の残りを片付けずに続けると、続きの比較の「前からあるもの」に入って、ok で見えなくなる。"""
        d = self.worktree("residue")[1]
        rc, rec = self.run_launch("claude", "cwd-residue-hang", extra=["--write", "--timeout", "2"], cwd=d)
        self.assertEqual((rec["status"], rec["resumable"]), ("failed", True), rec)
        self.args_file.unlink()
        p = self.run_launch(None, extra=["--resume", rec["out"]], raw=True)
        self.assertEqual(p.returncode, 2, p.stdout)
        self.assertIn(str(d.resolve() / ".bashrc"), p.stderr)  # launcher は、解いた道筋で出す
        self.assertFalse(self.args_file.exists())
        for f in rec["cwd_residue"]:
            shutil.rmtree(f) if Path(f).is_dir() else Path(f).unlink()
        rc, again = self.run_launch(None, extra=["--resume", rec["out"]])
        self.assertNotIn(again["status"], ("unavailable",), again)
        self.assertEqual(again["cwd_residue"], [], again)

    def test_a_session_runs_in_one_resume_at_a_time(self):
        """同じ session の続きを同時に 2 つ起動しない。launcher が KILL されても排他は残らない。"""
        rc, first = self.run_launch("claude", extra=["--timeout", "2"], env_extra={"FAKE_HANG": "1"})
        p = self.launch_in_background(["--resume", first["out"], "--timeout", "60"], mode="stop-deaf")

        # この試験の置き場の偽の CLI だけを止める（pkill の無い環境では、偽の子は 60 秒で自分から終わる）。
        # この片付けを外しても試験は落ちない（試験の後に偽の子を残さないことは確かめない）
        def stop_fakes():
            try:
                subprocess.run(["pkill", "-KILL", "-f", str(self.bin)], check=False)
            except FileNotFoundError:
                pass
        self.addCleanup(stop_fakes)  # 途中で落ちても残さない
        second = self.run_launch(None, extra=["--resume", first["out"]], raw=True)
        self.assertEqual(second.returncode, 2, second.stdout)
        self.assertIn("同じ session の続きが動いている", second.stderr)
        p.kill()  # 子は残る（launcher の外の話）が、排他は手放す
        p.communicate(timeout=30)
        stop_fakes()  # launcher を KILL したので、偽の子（止めを聞かずに 60 秒眠る）が試験の後まで残る
        self.orphans()
        rc, again = self.run_launch(None, extra=["--resume", first["out"]])
        self.assertEqual(rc, 0, again)

    def test_a_resume_from_an_older_record_keeps_the_commits_the_session_saw(self):
        """前の記録に戻って続けても、session がその間に見た別の commit を忘れない（審査の head を書かない）。"""
        git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=self.cwd, check=True,
                                        capture_output=True, text=True).stdout.strip()
        git("init", "-q")
        git("add", "-A")
        git("commit", "-q", "-m", "1")
        h1 = git("rev-parse", "HEAD")
        rc, first = self.run_launch("claude", extra=["--timeout", "2"], env_extra={"FAKE_HANG": "1"})
        self.assertEqual(first["head"], h1)
        with open(self.state / "delegations.jsonl", "a") as f:  # 前の版の記録には saw が無い
            f.write(json.dumps({"session": first["session"], "out": "x"}) + "\n")
        rc, plain = self.run_launch(None, extra=["--resume", first["out"]])
        self.assertEqual(plain["head"], h1, plain)
        git("commit", "-q", "--allow-empty", "-m", "2")
        rc, mid = self.run_launch(None, extra=["--resume", first["out"]])
        self.assertIsNone(mid["head"], mid)
        git("reset", "-q", "--hard", h1)
        rc, again = self.run_launch(None, extra=["--resume", first["out"]])
        self.assertEqual(again["status"], "ok", again)
        self.assertIsNone(again["head"], again)
        self.assertNotIn("next", again)

    # 壁時計の飛びは、子の CLI が動き始めた後（偽の子が FAKE_ARGS を書いた後）に 100 秒進む時計で作る（launcher の待ち方には触れない）
    WALL_CLOCK_JUMPS_ONCE_THE_CHILD_RUNS = """
        import time
        _real = time.time
        time.time = lambda: _real() + (100 if os.path.exists(os.environ["FAKE_ARGS"]) else 0)
    """

    def test_the_duration_does_not_follow_a_jump_of_the_wall_clock(self):
        """所要時間（duration_s）は、壁時計が飛んでも延びない。WSL2 の壁時計は約 35 秒ごとに約 2.3 秒進み、それに重なった時間切れの
        起動の duration_s が 5 秒を超えた（単調時計では毎回約 3.8 秒。Issue #33）。子が動いている間に、壁時計だけを 100 秒進める。"""
        rc, rec = self.run_launch("claude", env_extra=self.launcher_site(self.WALL_CLOCK_JUMPS_ONCE_THE_CHILD_RUNS))
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        self.assertGreaterEqual(rec["duration_s"], 0, rec)  # 始めと終わりで時計を混ぜると、どちらかの向きで外れる
        self.assertLess(rec["duration_s"], 50, rec)
        self.assertGreater(rec["at"], time.time() + 50, rec)  # 起動の終わりの時刻（片付けの期限に使う）は、壁時計のまま

    def test_a_jump_of_the_wall_clock_does_not_bring_the_timeout_forward(self):
        """時間切れの期限も、壁時計が飛んでも早まらない（子が動き始めた後に、壁時計だけを 100 秒進める）。期限を子の起動の後に
        壁時計で決め直す形は、この試験は通る（守らない）。期限と所要時間の両方を壁時計に戻した形もこの試験は通り、
        test_the_duration_does_not_follow_a_jump_of_the_wall_clock が落とす。"""
        rc, rec = self.run_launch("claude", extra=["--timeout", "3"],
                                  env_extra={"FAKE_HANG": "1", **self.launcher_site(self.WALL_CLOCK_JUMPS_ONCE_THE_CHILD_RUNS)})
        self.assertEqual((rc, rec["status"], rec["problems"][:1]), (2, "timeout", ["3 秒で終わらなかった"]), rec)
        self.assertGreaterEqual(rec["duration_s"], 2.9, rec)  # 期限を壁時計で見ると、時計が飛んだ直後に TERM を送って 1 秒も経たずに終わる

    def test_a_timed_out_child_gets_a_term_and_time_to_clean_up(self):
        """時間切れも、まず TERM を送って子の片付けを待つ（いきなり KILL すると、子の CLI の置き場が残る）。"""
        rc, rec = self.run_launch("claude", "stop", extra=["--timeout", "2"])
        self.assertEqual(Path(str(self.args_file) + ".term").read_text(), f"{int(signal.SIGTERM)}\n")
        self.assertFalse(Path(str(self.args_file) + ".stage").exists())
        self.assertEqual((rec["status"], rec["resumable"]), ("timeout", True), rec)
        self.assertEqual(rec["problems"][0], "2 秒で終わらなかった")
        self.assertNotIn("stopped_by_signal", rec)

    def test_the_wait_honours_the_grace_outside_linux_too(self):
        """Linux の外でも、シグナルの後の猶予で待つのをやめる（--timeout の残りまで止まらなかった）。猶予は --timeout より長くならない。
        シグナルは、偽の子（TERM を受けても終わらない）が動き始めてから送る（偽の dotenvx がシグナルの受け手を置く前に送ると、dotenvx が
        すぐに終わり、猶予の道を通らずに通った）。"""
        patch = """\
            import time
            launch.LINUX = False
            launch.STOP_GRACE = 1
            real = subprocess.Popen
            def started(*a, **k):
                p = real(*a, **k)
                if k.get("start_new_session") and "--settings" in a[0]:  # 子の CLI の起動だけ（専用ログインの確かめも新しいセッションで起こす）
                    mark, until = os.environ["FAKE_ARGS"] + ".started", time.monotonic() + 10
                    while not os.path.exists(mark) and time.monotonic() < until:
                        time.sleep(0.05)
                    os.kill(os.getpid(), signal.SIGTERM)
                return p
            subprocess.Popen = started
            """
        t0 = time.monotonic()
        rc, rec = self.launch_patched(patch, ["--host", "claude", "--cwd", str(self.cwd), "--timeout", "20"], mode="stop-deaf")
        self.orphans()
        self.assertLess(time.monotonic() - t0, 15, rec)
        self.assertEqual(rec["stopped_by_signal"], int(signal.SIGTERM), rec)

    def test_a_missing_state_dir_does_not_lose_the_result(self):
        """--out を渡し、状態の置き場がまだ無くても、記録と標準出力の JSON を返す（審査の記録に数えられなかった）。"""
        shutil.rmtree(self.state)
        rc, rec = self.run_launch("claude", extra=["--out", str(Path(self.tmp.name) / "out-here")])
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        self.assertTrue((self.state / "delegations.jsonl").is_file())

    def test_an_out_that_exists_is_refused_before_the_child(self):
        out = Path(self.tmp.name) / "taken"
        out.mkdir()
        p = self.run_launch("claude", extra=["--out", str(out)], raw=True)
        self.assertEqual(p.returncode, 2)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn(str(out.resolve()), p.stderr)  # launcher は、解いた道筋で出す
        self.assertFalse(self.args_file.exists())

    def test_a_git_error_in_another_encoding_does_not_lose_the_record(self):
        """子が .git を壊して git の文言が UTF-8 でなくても、落ちずに記録を書く（Git の管理情報の変更の警告が消えた）。"""
        d = self.worktree("garbled")[1]
        rc, rec = self.run_launch("claude", extra=["--write"], cwd=d,
                                  env_extra={"FAKE_PUT": json.dumps([["hex", ".git", b"gitdir: /nonexistent/\xff\n".hex()]])})
        self.assertEqual((rc, rec["status"], rec["head"]), (2, "failed", None), rec)
        self.assertTrue((Path(rec["out"]) / "record.json").is_file())

    def test_git_output_in_another_encoding_reaches_the_record(self):
        """子を起こす前から .git の文言が UTF-8 でなくても記録を書く。"""
        d = self.worktree("garbled2")[1]
        (d / ".git").write_bytes(b"gitdir: /nonexistent/\xff\n")
        rc, rec = self.run_launch("claude", cwd=d)
        self.assertIsNone(rec["head"], rec)
        self.assertTrue((Path(rec["out"]) / "record.json").is_file())

    def test_a_signal_right_after_the_child_starts_is_not_lost(self):
        """子の起動の直後に受けたシグナルでも、子に TERM を伝えて記録を書く（記録が無いまま子が動き続け、INT では動いている子の置き場を消した）。
        起動の直後（受け手が子を知る前）に受けたシグナルを、起動の後で子へ伝えないと、子が止まらず落ちる。"""
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(sig=sig):
                site = self.launcher_site(f"""
                    import subprocess
                    real = subprocess.Popen
                    def started(*a, **k):
                        p = real(*a, **k)
                        if k.get("start_new_session") and "--settings" in a[0]:  # 子の CLI の起動だけ（専用ログインの確かめも新しいセッションで起こす）
                            os.kill(os.getpid(), {int(sig)})
                        return p
                    subprocess.Popen = started
                """)
                t0 = time.monotonic()
                rc, rec = self.run_launch("claude", "stop", env_extra=site)
                self.assertEqual(rec["stopped_by_signal"], int(sig), rec)
                self.assertTrue(rec["resumable"], rec)
                # 子に TERM が届いて止まった（終了コードは、TERM が届いた時機で変わる: 偽の dotenvx が受け手を置く前に受ければ -15、子へ伝えて
                # 子が片付けて終われば 143、偽の子が受け手を置く前に受ければ 241）。伝えなければ、偽の子は 60 秒眠ってから終了コード 0 で終わる。
                # TERM でなく KILL で止めると -9
                self.assertNotIn(rec["exit_code"], (0, -int(signal.SIGKILL)),
                                 f"子にシグナルが伝わらず、偽の子が最後まで眠って終了コード 0 で終わったか、子を TERM でなく KILL で止めた: {rec}")
                self.assertLess(time.monotonic() - t0, 30, rec)

    @unittest.skipUnless(sys.platform.startswith("linux"), "子を刈らずに待つのは Linux だけ")
    def test_a_child_reaped_late_does_not_read_as_exit_zero(self):
        """KILL の後も child.wait が切れ、子を別の場所で刈ると、poll が 0 を返した。終了コードは分からないと書く。"""
        site = self.launcher_site("""
            import subprocess
            real = subprocess.Popen.wait
            def wait(self, timeout=None):
                if timeout == 10:
                    raise subprocess.TimeoutExpired(self.args, timeout)
                return real(self, timeout)
            subprocess.Popen.wait = wait
        """)
        rc, rec = self.run_launch("claude", extra=["--timeout", "1"], env_extra={"FAKE_HANG": "1", **site})
        self.assertEqual(rec["status"], "timeout", rec)
        self.assertIsNone(rec["exit_code"], rec)

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
                # 確かめ役と監査役を 1 つのコマンドで続けて立て、監査役が毎回「直前の確かめ役の指摘の扱いが無い」で止まった（#34）
                self.assertIn("HEAD を動かさない", rec["next"])
        # 試験が見るのは役ごとの出し分け（目印の句）だけで、案内の意味（順序・範囲）は読者役で確かめる
        rc, rec = self.run_launch("claude", extra=["--interrogate"])  # --interrogate の審査役も、監査役の文でなく順序の文
        self.assertEqual((rc, "HEAD を動かさない" in rec["next"], "前の監査" in rec["next"]), (0, True, False), rec)
        # 書く子・ok でない子に next を出さないことは test_only_a_child_counted_as_a_review_is_told_to_write_the_triage が見る
        # 監査役が差し戻したら、立て直すときは前の監査の項目と、その後の差分に限る（#34）。届けてよいなら、整える点を直して立て直さない
        rc, rec = self.run_launch("claude", mode="deliver-no", extra=["--audit"])
        self.assertEqual((rc, "指摘の扱い" in rec["next"], "前の監査" in rec["next"], "HEAD を動かさない" in rec["next"]), (0, True, True, False), rec)
        rc, rec = self.run_launch("claude", extra=["--audit"])
        self.assertEqual((rc, "立て直さない" in rec["next"], "前の監査" in rec["next"]), (0, True, False), rec)
        # 試験を実行できなかった監査役（not-run）の案内は、差し戻したときだけ差し戻しを先に言う。判定は記録に残る（work.py が照らす）
        for mode, deliver, sent_back in (("deliver-no-not-run", "no", True), ("deliver-not-run", "yes", False)):
            rc, rec = self.run_launch("claude", mode=mode, extra=["--audit"])
            self.assertEqual((rec["status"], rec["deliver"], "差し戻し" in rec["next"]), ("not-run", deliver, sent_back), rec)
        rc, rec = self.run_launch("claude", mode="deliver-no-not-run")  # 監査役でない子には、報告の deliver に関わらず差し戻しと言わない
        self.assertEqual((rec["status"], "deliver" in rec, "差し戻し" in rec["next"]), ("not-run", False, False), rec)
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
        (self.cwd / "s" / "x.txt").write_text("x\n")  # サブモジュールの中の未 commit の変更は数えない（中の設定で命令を走らせないため。#64）
        rc, rec = self.run_launch("claude", env_extra={"GIT_CONFIG_GLOBAL": str(ignore)})
        self.assertEqual((rc, rec["head"]), (0, git("rev-parse", "HEAD")), rec)
        git("add", "x.txt", cwd=self.cwd / "s")
        git("commit", "-q", "-m", "x", cwd=self.cwd / "s")  # サブモジュールが別の commit を指すと、無視する設定があっても数える
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
        with self.subTest("the first run of a fix"):  # 報告を直させる前の子の値（記録の report_fix に写したもの）
            good = GOOD.encode().decode("unicode_escape")
            before = set((self.state / "delegations").iterdir())
            p = self.run_launch("codex", mode="unwritable-usage", raw=True, env_extra={
                "PYTHONPATH": str(site), "FAKE_TEXT": good.replace("a.txt:1", "other/a.txt:1"), "FAKE_RESUMED_TEXT": good, "FAKE_MODE_2": "good"})
            outs = set((self.state / "delegations").iterdir()) - before
            self.assertEqual([(d / "record.json").is_file() for d in outs], [True], p.stderr[-2000:])
            rec = json.loads(p.stdout)
            self.assertEqual((rec["status"], rec["resumable"], rec["report_fix"]["usage"]), ("failed", False, None), rec)
            self.assertTrue([x for x in rec["problems"] if "子の結果を記録に書けない" in x], rec)
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
        target = Path(self.tmp.name) / "locked-target"
        target.mkdir()
        (target / "a.txt").write_text("x\n")
        rc, rec = self.run_launch("claude", "lock-target-dir", extra=["--write"], cwd=target)
        os.chmod(target / "locked-new", 0o755)
        self.assertEqual((rec["status"], rec["resumable"]), ("failed", False), rec)  # 子が権限を変えた。続きから起動させない
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
        target = Path(self.tmp.name) / "keep-target"
        target.mkdir()
        (target / "a.txt").write_text("x\n")
        rc, rec = self.run_launch("claude", "lock-target-dir", extra=["--write"], cwd=target, env_extra={"FAKE_EXIT": "3"})
        os.chmod(target / "locked-new", 0o755)
        self.assertIn("対象の残りを確かめられない", " ".join(rec["problems"]))
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
        """形の崩れた報告（証拠が文字列・深すぎる入れ子・長すぎる where）でも落ちずに invalid-report を返し、子の長い文を
        そのまま記録に積まない（理由の 1 行は 400 字まで）。"""
        deep = Path(self.tmp.name) / "deep.md"
        deep.write_text('```json\n{"a": ' + "[" * 100000 + "]" * 100000 + "}", encoding="utf-8")
        for env, problem in (({"FAKE_TEXT": '```json\n{"conclusion": "c", "evidence": ["a.txt:1"]}'}, "where の無い証拠"),
                             ({"FAKE_TEXT_FILE": str(deep)}, "深"),
                             ({"FAKE_TEXT": '```json\n{"conclusion": "c", "evidence": [{"where": "' + "x" * 5000 + ':1"}]}'}, "証拠のファイルが無い")):
            with self.subTest(problem=problem):
                rc, rec = self.run_launch("claude", env_extra=env)
                self.assertEqual((rc, rec["status"]), (2, "invalid-report"), rec)
                problems = rec["problems"]
                if problem == "深":  # 読めないか、記録に書けない（Python の版で深さの上限が違う。3.15 の decoder は読める）
                    self.assertTrue([p for p in problems if "読めない" in p or "深すぎて" in p], problems)
                else:
                    self.assertTrue([p for p in problems if problem in p], problems)
                self.assertTrue(all(len(p) <= 400 for p in problems), problems)

    def test_git_snapshot_reads_refs_that_are_not_utf8(self):
        """名前が UTF-8 でない ref も、書く子の前後の照合に入る（子がそれを動かすと、Git の管理情報の変更として出る）。"""
        repo, wt = self.worktree("odd-ref")
        g = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        first = subprocess.run(g + ["rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        subprocess.run(g + ["commit", "-q", "--allow-empty", "-m", "2"], check=True)
        second = subprocess.run(g + ["rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        # macOS（APFS）は UTF-8 でないファイル名を拒むので、ref のファイルでなく packed-refs に書く
        packed = lambda h: b"# pack-refs with: peeled fully-peeled sorted \n" + h.encode() + b" refs/heads/bb-writ/a\xff\n"
        (repo / ".git" / "packed-refs").write_bytes(packed(first))
        rc, rec = self.run_launch("claude", extra=["--write"], cwd=wt,
                                  env_extra={"FAKE_PUT": json.dumps([["hex", "@COMMON@/packed-refs", packed(second).hex()]])})
        self.assertEqual((rc, rec["status"], rec["blocked"]), (2, "failed", ["git"]), rec)
        self.assertTrue([p for p in rec["problems"] if p.startswith("refs/heads/bb-writ/a") and f"{first} → {second}" in p], rec["problems"])

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
                         "対象に書く試験・検査は、`mktemp -d` で新しく作ったディレクトリ（$TMPDIR の下。無ければ /tmp の下）に対象を写して（cp -r など）、そこで動かす。決まった名前や pid だけの名前の場所に写さない（並行する子どうしでも、同じ子の別のコマンドでも、$TMPDIR も pid も同じことがある）。"):
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
        h = str(home.resolve())
        self.assertEqual(rules.get(":tmpdir"), "write")
        for denied in (f"{h}/.ssh", f"{h}/.config/gh", f"{h}/elsewhere/aws", f"{h}/.codex/auth.json", f"{h}/.codex/sessions"):
            self.assertEqual(rules.get(denied), "deny", (denied, rules))
        self.assertNotIn("read", rules.values())  # 拒んだ場所の下に read を書かない（本体を実行できなかった）
        for absent in (f"{h}/.config", f"{h}/.config/git",  # Git の設定を含む置き場は中身ごと、Git の設定は拒まない
                       f"{h}/.codex", f"{h}/.codex/packages", f"{h}/.gnupg", f"{h}/.aws", f"{h}/missing", f"{h}/.kube"):
            self.assertNotIn(absent, rules)
        self.assertEqual(rules.get(str(self.cfg.resolve())), "deny")  # 専用ログイン（試験ではホームの外）
        # 普段の専用ログインは ~/.config の下。拒む場所の下の場所は書かない
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

    def test_live_checks_give_the_launcher_command_every_value_it_reads(self):
        # 実機の確認の台本は、launcher の main を通らずに、自分で組んだ名前空間で command() を呼ぶ。main が起動の前に決める値
        # （--resume を足したときの resume・session、--audit を足したときの audit）が増えても、台本は CI で動かないので足し忘れ、
        # AttributeError で動かなくなっていた（引数の数は合っていたので、上の試験は通った）。台本に書かれた SimpleNamespace(...) を
        # そのまま組み、偽の CLI の環境で command() に渡す。コマンドの要素が全部文字列であることも見る（session を None のまま渡すと、
        # Claude Code の --session-id の値が None になり、起動の段で落ちる）。
        # 守らない範囲:
        # - SimpleNamespace(...) という名前の呼び出しでない組み方（argparse.Namespace・別名・types.SimpleNamespace）は見ない。2 か所の
        #   片方だけをそう組むと、その片方の欠けは見ずに通る（見つけた数が 0 のときだけ落ちる）。
        # - 値の中身は見ない。空の文字列の session と、f-string を通って文字列になる値（Codex の effort の None）は通る。resume を真にした
        #   名前空間も、session が文字列なら通る（Codex の書く名前空間は session が None なので落ちる）。
        # - host の変数には claude と codex だけを入れる（台本の hosts の一覧は読まない）。台本が host の変数で grok を起動するように
        #   なっても、Grok の経路（grok_home を読む）は動かさない。host='grok' を直に書いた名前空間は Grok の経路まで動かすが、
        #   grok_home が無ければ落ち、読み取り専用の Grok は、この試験の対象が一時ディレクトリの下にあるので値がそろっていても落ちる。
        # - command() の後の台本の処理（組んだコマンドの書き換え・実行）は見ない。
        # 台本の正当な書き換えでも落ちる形: 組んだ後に属性を足す（a.resume = None。値は SimpleNamespace(...) の中に書く）、command() に
        # 渡さない SimpleNamespace(...) を tests/verify_*.py に置く（全部を command() に渡すものとして扱う）、キーワードの値が
        # 組み込みの名前と host・repo・uuid のどれでもない名前を読む（NameError。下の辞書に足す）。
        import ast
        import uuid
        from types import SimpleNamespace
        from unittest import mock
        launch, seen = load_launch(), 0
        env = {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin", "BB_WRIT_CONFIG_DIR": str(self.cfg), "BB_WRIT_STATE_DIR": str(self.state)}
        for script in sorted((ROOT / "tests").glob("verify_*.py")):
            for call in ast.walk(ast.parse(script.read_text())):
                if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "SimpleNamespace"):
                    continue
                by_host = any(isinstance(n, ast.Name) and n.id == "host" for n in ast.walk(call))
                for host in ("claude", "codex") if by_host else (None,):
                    seen += 1
                    with self.subTest(script=script.name, line=call.lineno, host=host), mock.patch.dict(os.environ, env):
                        a = eval(compile(ast.Expression(call), str(script), "eval"),
                                 {"SimpleNamespace": SimpleNamespace, "uuid": uuid, "host": host, "repo": self.cwd})
                        cmd, _, why = launch.command(a, "x", self.state / "scratch")
                        self.assertIsNone(why)
                        self.assertEqual([c for c in cmd if not isinstance(c, str)], [], cmd)
        self.assertGreater(seen, 0, "台本が command() に渡す名前空間を 1 つも見つけられない（組み方を変えたなら、この試験も直す）")

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
                self.assertFalse(os.path.lexists(state / "delegations"), state)  # 委任の記録の置き場を作らない（置き場が symlink でも、行き先に）。見るのは delegations だけ
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

    def test_only_claude_write_children_are_told_about_the_sandbox_marks(self):
        # Claude Code の書く子の中では、sandbox が書き込みを拒むために置く印（作業場所の直下の、子が作っていないドットの項目）が
        # git status に出て、git add -A が 128 で落ちた。launcher は Claude Code の書く子にだけ（続きでも）、印に触らず報告に数えないことと、
        # 名前を挙げて足すことを約束に足す。全文を定数を通さずに書き、依頼文がその全文で終わることを見る（定数と比べると、文面を空にしても
        # 通る。文面は実物の子で測ってあるので、変えるなら、実物の子で測り直してからここも変える）
        contract = ("Linux・WSL2 では、作業場所の直下に、自分が作っていないドットの項目（`.bashrc`・`.gitconfig`・`.claude/` の下など。"
                    "`ls -l` の先頭が `c` で、開けない）が見えることがある。Claude Code の sandbox が書き込みを拒むために置く印で、"
                    "sandbox が後で片付ける。触らず、報告に数えない。変更を足すときは `git add -A`・`git add .` を使わず、"
                    "変えたファイルの名前を挙げる（印は Git に足せないので、どちらも落ちる）。\n")

        def given(host):  # 偽の CLI に渡った依頼文（Codex は最後の引数、Claude Code・Grok Build は -p の次）
            argv = json.loads(self.args_file.read_text())
            return argv[-1] if host == "codex" else argv[argv.index("-p") + 1]

        # 書く子は試験が作る作業場所で起動する。時間切れにして、続きの依頼文も見る
        rc, rec = self.run_launch("claude", extra=["--write", "--timeout", "2"], cwd=self.worktree("marks")[1], env_extra={"FAKE_HANG": "1"})
        self.assertEqual((rc, rec["status"]), (2, "timeout"), rec)
        first = given("claude")
        self.prompt.write_text("時間切れで止められた。続けて")
        rc, again = self.run_launch(None, extra=["--resume", rec["out"]])
        self.assertEqual((rc, again["status"], again["write"]), (0, "ok", True), again)
        for launch, prompt in (("最初の起動", first), ("続き（--resume）", given("claude"))):
            with self.subTest(child="Claude Code の書く子", launch=launch):
                self.assertTrue(prompt.endswith(contract), "依頼文が約束の文の全文で終わらない: " + prompt[-400:])
        # ほかの子には、決まった句が無いことだけを見る（別の文面で同じ約束が足されることは見ない）
        self.prompt.write_text("x を確かめて")
        for host, extra, child in (("claude", [], "読み取り専用の Claude Code の子"), ("codex", ["--write"], "Codex の書く子"),
                                   ("grok", ["--write"], "Grok Build の書く子")):
            with self.subTest(child=child):
                self.args_file.unlink(missing_ok=True)
                rc, rec = self.run_launch(host, extra=extra, cwd=self.worktree(host)[1] if extra else None)
                self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
                self.assertNotIn("sandbox が書き込みを拒むために置く印", given(host), child)

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
                    # 読み取り専用の子には、どのホストでも、子ごとに新しく作った場所へ写して動かす約束を渡す（#46。並行する
                    # Claude Code の子は $TMPDIR も pid も同じだった）。試験が見るのは子に渡す約束の文だけで、子が約束に従うかと、
                    # interrogate・playbook-verify の依頼文の雛形の文と、not-run の案内の写し方の文は見ない（守らない）
                    self.assertIn(load_launch().READ_ONLY_CONTRACT.strip(), prompt)
                    self.assertIn("`mktemp -d` で新しく作ったディレクトリ", prompt)
                    if status == "not-run":
                        self.assertTrue(rec["report"]["conclusion"])  # 報告は捨てず、読んだだけの審査として使える
                        self.assertIn("読んだだけ", rec["problems"][0])

    def test_claude_ok_and_read_only_uses_dont_ask(self):
        self.home = self.home.resolve()  # 一時の置き場が symlink の下（macOS の /var）でも、ホームは symlink を通らない道筋で渡す
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
            self.assertFalse(claude_readable(secret, fs), secret)
        self.assertTrue(claude_readable(home / ".config" / "git", fs))
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
            Path(str(self.args_file) + ".env").unlink(missing_ok=True)  # 前の回の子が書いたものを読まない
            rc, rec = self.run_launch("claude", extra=extra, env_extra={"CLAUDE_CODE_PLUGIN_DIRS": "~/.claude/mods/x", "CLAUDECODE": "1", "CLAUDE_EFFORT": "max", "ANTHROPIC_API_KEY": "k"})
            self.assertTrue(Path(str(self.args_file) + ".env").exists(), (extra, rec))  # この回の子が起動した
            env = json.loads(Path(str(self.args_file) + ".env").read_text())
            self.assertEqual((sorted(env), env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"]), (["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "CLAUDE_CONFIG_DIR"], "1"), extra)

    def test_the_model_comes_from_the_role_unless_the_parent_gives_one(self):
        # 親が --model・--effort を省くと、役割とホストの表で決める（親が毎回手で選ぶと、同じ審査役が Opus high と Sonnet medium に割れた）
        flags = {"read": [], "audit": ["--audit"], "interrogate": ["--interrogate"], "write": ["--write"], "hardest": ["--write", "--hardest"]}
        table = {("claude", "read"): ("sonnet", "medium"), ("claude", "audit"): ("opus", "high"), ("claude", "interrogate"): ("fable", "medium"),
                 ("claude", "write"): ("opus", "high"), ("claude", "hardest"): ("fable", "medium"),
                 ("codex", "read"): ("gpt-6-sol", "medium"), ("codex", "audit"): ("gpt-6-sol", "medium"), ("codex", "interrogate"): ("gpt-6-sol", "medium"),
                 ("codex", "write"): ("gpt-6-sol", "medium"), ("codex", "hardest"): ("gpt-6.1-sol", "high"),
                 ("grok", "read"): ("grok-4.7", "medium"), ("grok", "audit"): ("grok-4.7", "medium"), ("grok", "interrogate"): ("grok-4.7", "medium"),
                 ("grok", "write"): ("grok-4.7", "medium")}

        def given(host, argv):  # 子の CLI に渡ったモデルと effort
            m = argv[argv.index("--model" if host == "claude" else "-m") + 1]
            if host == "codex":
                return m, next(x for x in argv if x.startswith("model_reasoning_effort=")).split("=", 1)[1].strip('"')
            return m, argv[argv.index("--effort") + 1]

        for (host, role), (model, effort) in table.items():
            self.args_file.unlink(missing_ok=True)
            rc, rec = self.run_launch(host, extra=flags[role])
            self.assertEqual((rec["model_requested"], rec["effort_requested"], rec["role"], rec["chosen_by"], rec["timeout_s"]),
                             (model, effort, role, "table", 3600 if role == "hardest" else 1800), (host, role, rec))
            if host == "grok" and role in ("read", "audit", "interrogate"):  # 対象が /tmp の下なので、Grok の読み取り専用は起こさない
                self.assertFalse(self.args_file.exists(), rec)
            else:
                self.assertEqual(given(host, json.loads(self.args_file.read_text())), (model, effort), (host, role))
        # 親が渡したものが勝つ。片方だけなら、残りを表から取る
        for extra, want in ((["--audit", "--model", "sonnet", "--effort", "low"], ("sonnet", "low", "given")),
                            (["--audit", "--effort", "medium"], ("opus", "medium", "given+table")),
                            (["--audit", "--model", "sonnet"], ("sonnet", "high", "given+table")),
                            (["--write", "--hardest", "--timeout", "900"], ("fable", "medium", "table", 900)),
                            (["--write", "--timeout", "900"], ("opus", "high", "table", 900))):
            rc, rec = self.run_launch("claude", extra=extra)
            self.assertEqual((rec["model_requested"], rec["effort_requested"], rec["chosen_by"], rec["timeout_s"]),
                             want if len(want) == 4 else (*want, 1800), extra)
            self.assertEqual(given("claude", json.loads(self.args_file.read_text())), want[:2], extra)
        # 表に無い組み合わせと、書く子でない --hardest は、子を起こさずに引数の誤りとして止まる
        for host, extra, why in (("grok", ["--write", "--hardest"], "既定のモデルが無い"), ("grok", ["--write", "--hardest", "--model", "grok-4.7"], "既定のモデルが無い"),
                                 ("grok", ["--write", "--hardest", "--effort", "high"], "既定のモデルが無い"), ("claude", ["--hardest"], "--write と一緒に"),
                                 ("claude", ["--audit", "--write", "--hardest"], "--audit"), ("claude", ["--interrogate", "--write", "--hardest"], "--interrogate")):
            self.args_file.unlink(missing_ok=True)
            p = self.run_launch(host, extra=extra, raw=True)
            self.assertEqual(p.returncode, 2, (extra, p.stderr))
            self.assertIn(why, p.stderr)
            self.assertFalse(self.args_file.exists(), extra)  # 子は起きていない
        self.args_file.unlink(missing_ok=True)
        p = self.run_launch("grok", extra=["--write", "--hardest", "--model", "grok-4.7", "--effort", "high"], raw=True)
        self.assertEqual(json.loads(p.stdout)["chosen_by"], "given", p.stderr)  # 両方渡せば、表に無くても起こせる
        self.assertEqual(given("grok", json.loads(self.args_file.read_text())), ("grok-4.7", "high"))

    def test_a_hardest_writer_times_out_at_its_default_limit(self):
        # 最も難しい書く子は、--timeout を省くと、その役割の既定の上限で時間切れになる（既定を 2 秒にして、既定のまま時間切れになるのを見る）。
        # 待った秒数そのものは測らない: 待ちを記録の値から少しずらす退行（数秒長く・半分に）と、1800 秒を超える待ちだけを縮める退行は見ない
        rc, rec = self.launch_patched("launch.TIMEOUTS['hardest'] = 2", ["--host", "claude", "--cwd", str(self.cwd), "--write", "--hardest"],
                                      env_extra={"FAKE_HANG": "1"})
        self.assertEqual((rec["status"], rec["timeout_s"]), ("timeout", 2), rec)
        self.assertIn("2 秒で終わらなかった", " ".join(rec["problems"]))

    def test_a_resume_keeps_the_role_and_takes_no_new_hardest(self):
        # 続きは前の記録のモデル・effort と役割で起こす。最も難しい書く子の続きの時間の上限の既定も 3600 のまま。--hardest は一緒に渡せない
        rc, first = self.run_launch("claude", extra=["--write", "--hardest", "--timeout", "2"], env_extra={"FAKE_HANG": "1"})
        self.assertEqual((first["status"], first["role"], first["resumable"]), ("timeout", "hardest", True), first)
        p = self.run_launch(None, extra=["--resume", first["out"], "--hardest"], raw=True)
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("hardest", p.stderr)
        rc, again = self.run_launch(None, extra=["--resume", first["out"]])
        self.assertEqual((again["role"], again["model_requested"], again["effort_requested"], again["timeout_s"]),
                         ("hardest", "fable", "medium", 3600), again)
        rc, more = self.run_launch(None, extra=["--resume", again["out"], "--timeout", "900"])  # 続きの続きも役割を保ち、渡した上限が勝つ
        self.assertEqual((more["role"], more["timeout_s"]), ("hardest", 900), more)
        rc, other = self.run_launch("claude", extra=["--write", "--timeout", "2"], env_extra={"FAKE_HANG": "1"})
        rc, other_again = self.run_launch(None, extra=["--resume", other["out"]])  # 最も難しくない書く子の続きは 1800 のまま
        self.assertEqual((other_again["role"], other_again["timeout_s"]), ("write", 1800), other_again)

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
                self.assertEqual(rec["cwd_residue"], [str(d / ".bashrc"), str(d / ".claude"), str(d / "dirs-only")])
                for f in rec["cwd_residue"]:  # 黙って消さない
                    self.assertTrue(Path(f).exists())
        with self.subTest("read-only"):  # 読み取り専用の子は比べない（並行する審査役の一時的な場所を、互いの残りとして数えない）
            rc, rec = self.run_launch("claude", "cwd-residue", cwd=target("read-only"))
            self.assertEqual((rc, rec["status"], rec.get("cwd_residue")), (0, "ok", None), rec)
        with self.subTest("nothing-left"):
            rc, rec = self.run_launch("claude", extra=["--write"], cwd=target("clean"))
            self.assertEqual((rc, rec["status"], rec["cwd_residue"]), (0, "ok", []), rec)

    def test_empty_files_a_later_child_leaves_under_the_kept_claude_dir_fail(self):
        # launcher は Claude Code の子が残した空の .claude/.cc-writes を消さない（#65）ので、後の子の前から .claude が在る。
        # 後の書く子が時間切れでその下に残した 0 バイトのファイル（.claude/settings.json など）を見落とし、git add -A で commit に入った
        d = self.worktree("kept")[1].resolve()
        rc, rec = self.run_launch("claude", cwd=d)  # 前の子（審査役など）
        self.assertEqual(rec["status"], "ok", rec)
        self.assertTrue((d / ".claude" / ".cc-writes").is_dir())
        rc, rec = self.run_launch("claude", extra=["--write"], cwd=d)  # 置き場だけのままの .claude は、後の子の残りでもない
        self.assertEqual((rec["status"], rec["cwd_residue"]), ("ok", []), rec)
        rc, rec = self.run_launch("claude", "cwd-residue-hang", extra=["--write", "--timeout", "3"], cwd=d)
        self.assertEqual((rec["status"], rec["cwd_residue"]), ("failed", [str(d / ".bashrc"), str(d / ".claude"), str(d / "dirs-only")]), rec)

    def test_empty_files_a_child_makes_under_places_that_were_there_before_are_not_residue(self):
        # 起動の前からある場所の下は、空のディレクトリの下でも数えない（前にファイルを持たなかった場所をどれも新しく数えると、
        # 利用者が置いた空の build/ に普通の書く子が空の目印を作るだけで failed になった）
        for name, place in (("stamp", "build/.stamp"), ("pkg", "pkg/sub/__init__.py")):
            with self.subTest(name):
                d = self.worktree(name)[1].resolve()
                (d / place).parent.mkdir(parents=True)
                rc, rec = self.run_launch("claude", extra=["--write"], cwd=d, env_extra={"FAKE_APPEND_FILE": str(d / place), "FAKE_APPEND_TEXT": ""})
                self.assertEqual((rec["status"], rec["cwd_residue"]), ("ok", []), rec)
                self.assertEqual((d / place).stat().st_size, 0)  # 子が作った

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
                t0 = time.monotonic()
                rc, rec = self.run_launch(host, mode, extra=extra, cwd=cwd, env_extra={"FAKE_SLEEP": sleep})
                took = time.monotonic() - t0
                pids = self.orphans()
                self.assertEqual((len(pids), rec["status"]), (7, status), rec)
                if status == "timeout":  # 時間切れの理由だけ（止めた子の CLI は残りに並ばない）。期限を守る（所要時間で捕まえるのは、2 秒より大きな延びだけ）
                    self.assertEqual(rec["problems"], ["3 秒で終わらなかった", f"--resume {rec['out']} で続きから起動できる"], rec)
                    # 期限は launcher が測る時間で見る。launcher の処理は毎回約 3.8 秒で終わる。壁時計で測っていたときは、ときどき
                    # 約 2.3 秒延びて 5 秒を越えた（WSL2 の壁時計が約 35 秒ごとに約 2.3 秒進み、それに重なった起動。70 回中 4 回。
                    # Issue #33。今は所要時間を launcher も試験も単調時計で測る）。WSL2 では単調時計も実際より数 % 遅く進み（adjtimex の
                    # tick が標準より小さい）、壁時計がそれを飛んで取り戻していた。期限と所要時間は同じ単調時計なので、互いに食い違わない。
                    # 試験の側の時間では、返らないことだけを捕まえる。試験の側の計測を壁時計に戻す後退を落とす試験は無く、15〜60 秒の待ちの
                    # 上限（偽の子や印のファイルを待つ所、work.py の試験用の止まり所）は壁時計のまま（どれも、待つものが現れるまでの打ち切り）
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
            if what == "unkillable-cli":  # 子の CLI（偽の dotenvx と claude）に、TERM も SIGKILL も効かない
                _kill3, _killpg = os.kill, os.killpg
                fake = os.path.join(os.path.dirname(os.environ["FAKE_ARGS"]), "bin", "").encode()
                def kill(pid, sig):
                    if sig in (9, 15) and fake in open(f"/proc/{pid}/cmdline", "rb").read():
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
                    self.assertNotIn("claude_login", rec)  # 確かめは済んでいるが、起動しない記録には書かない
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
                t0 = time.monotonic()
                # 猶予を実際に 30 秒待つと、試験全体が CI の上限を超える
                rc, rec = self.launch_patched("launch.STOP_GRACE = 1", ["--host", host, "--cwd", str(self.cwd), "--timeout", "2"], mode=mode,
                                              env_extra={"FAKE_SLEEP": sleep, "PYTHONPATH": str(site), "FAKE_SITE": what, **({"FAKE_CLOSE_OUT": "1"} if closed else {})})
                self.orphans()
                self.assertEqual((rc, rec["status"]), (2, "timeout" if what == "waitid-storm" else "failed"), rec)  # 止めきれれば、続きから起動できる時間切れ
                self.assertTrue(any(words in p for p in rec["problems"]), rec)
                # 時間切れの TERM の後に猶予（waitid-storm は子の終わりが見えないので使い切る）。止まらない CLI はさらに 10 秒待ち、残りを 5 秒止め続ける
                self.assertLess(time.monotonic() - t0, 25 if what == "unkillable-cli" else 10, rec)
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

    def test_empty_dirs_the_claude_cli_makes_in_its_own_places_are_not_residue(self):
        # 2.1.309 の sandbox は子を起こすたびにホームへ空の .claude/ssh を作り、2.1.327 は使った機能で bridge-spawn・child-logs などと
        # ~/.config/anthropic も作った。残りとして委任が毎回 failed になった（#40）。数えないのは ~/.claude の直下（名前は問わない）と
        # ~/.config/anthropic の、空のディレクトリだけで、どのホストでも、~/.claude が前からあってもなくても同じ。消さない
        home = Path(self.tmp.name) / "home"
        h = lambda *p: str(home.joinpath(*p))
        cases = (  # (ホスト, 偽の CLI のモード, 引数, 前から .claude があるか, status, 残り, 後の ~/.claude/ssh)
            ("claude", "home-ssh", (), False, "ok", [], "dir"),
            ("claude", "home-ssh", ("--write",), False, "ok", [], "dir"),
            ("claude", "home-ssh", (), True, "ok", [], "dir"),  # 利用者のホームにはたいてい前から .claude がある
            ("codex", "home-ssh", (), False, "ok", [], "dir"),  # 並行する Claude Code の子が作ったものを、ほかの委任も見る
            ("grok", "home-ssh", (), True, "ok", [], "dir"),
            ("claude", "home-cli-places", (), False, "ok", [], "dir"),
            ("claude", "home-cli-places", ("--write",), True, "ok", [], "dir"),
            ("claude", "home-claude-only", (), False, "failed", [h(".claude")], "absent"),  # 空の .claude そのものは数える
            ("claude", "home-ssh-near", (), True, "failed", [h(".config", "ssh")], "dir"),  # ~/.claude の直下の隣は数えず、~/.config の下のほかの名前は数える
            ("claude", "home-cli-places-near", (), True, "failed",
             [h(".anthropic"), h(".claude", "empty-file"), h(".config", "anthropic"), h(".config", "claude")], "absent"),  # 空のファイル・ほかの場所は数える
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
        """子の後のホームの走査の途中で、別のセッションが一時ディレクトリを消しても（走査の途中で無くなった）、読めない場所に数えず ok で返す。
        消える時機は、launcher のプロセスの os.scandir が、子が動き始めた後に ~/.claude を開くときに作る（空かを見るとき、降りるとき）。"""
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "x").write_text("x")
        for when in (1, 2):
            with self.subTest(when=when):
                site = self.launcher_site(f"""
                    _real, _seen = os.scandir, []
                    def _flaky(d="."):
                        if not isinstance(d, int) and str(d).endswith("/.claude") and os.path.exists(os.environ["FAKE_ARGS"]):
                            _seen.append(d)
                            if len(_seen) == {when}:
                                open(os.environ["FAKE_ARGS"] + ".vanished", "w").close()
                                raise FileNotFoundError(2, "消えた", str(d))
                        return _real(d)
                    os.scandir = _flaky
                """)
                self.args_file.unlink(missing_ok=True)
                rc, rec = self.run_launch("claude", env_extra=site)
                self.assertTrue(Path(str(self.args_file) + ".vanished").exists(), "消える場面を作れなかった")
                Path(str(self.args_file) + ".vanished").unlink()
                self.assertEqual((rc, rec["status"]), (0, "ok"), rec)

    def test_child_files_are_read_without_blocking_or_exhausting_memory(self):
        """書く子が Git の管理情報（作業場所の config.worktree・HEAD、hooks）を FIFO・/dev/zero への symlink・巨大なファイルに替えても、
        launcher は止まらず尽きずに照合を終え、変わったものを全部出す（symlink は行き先の変化、ファイルは後ろの違いと消えたことも）。
        メモリの上限は Linux だけで掛ける。"""
        repo, wt = self.worktree("child-files")
        hooks, own = repo / ".git" / "hooks", repo / ".git" / "worktrees" / "child-files-wt"
        hooks.mkdir(exist_ok=True)
        (hooks / "l1").symlink_to("a-target")
        (hooks / "big").write_bytes(b"x" * 70000 + b"1")  # 先頭が同じでも、後ろの違いを見る
        (hooks / "gone").write_text("x")
        puts = [["fifo", "@GITDIR@/config.worktree", None], ["symlink", "@COMMON@/hooks/zero", "/dev/zero"],
                ["symlink", "@COMMON@/hooks/lfifo", str(own / "config.worktree")], ["symlink", "@COMMON@/hooks/l1", "b-target"],
                ["fill", "@COMMON@/hooks/big", [b"x".hex(), 70000, b"2".hex()]], ["remove", "@COMMON@/hooks/gone", None],
                ["sparse", "@COMMON@/hooks/huge", 768 << 20], ["symlink", "@GITDIR@/HEAD", "x" * 53]]  # 表しの長さがハッシュと同じになる行き先
        env = {"FAKE_PUT": json.dumps(puts)}
        if sys.platform.startswith("linux"):  # 大きなファイルも一定のメモリで読む（RLIMIT_AS が効くのは Linux）
            env.update(self.launcher_site("import resource\nresource.setrlimit(resource.RLIMIT_AS, (512 << 20, 512 << 20))"))
        rc, rec = self.run_launch("claude", extra=["--write"], cwd=wt, env_extra=env)
        self.assertEqual((rc, rec["status"], rec["blocked"]), (2, "failed", ["git"]), rec)
        changed = rec["problems"]
        for place in [own / "config.worktree", *(hooks / n for n in ("zero", "lfifo", "l1", "big", "gone", "huge"))]:
            self.assertIn(str(place.parent.resolve() / place.name), changed)
        self.assertTrue([p for p in changed if p.startswith("HEAD: ") and "symlink: " in p.split(" → ")[1]], changed)  # 空の HEAD と読み違えない

    def test_rollout_reading_is_bounded(self):
        """読み取り専用の Codex の子の記録（rollout）が、FIFO（書き手がいても）・ディレクトリ・巨大なものに替わっていても、launcher は
        止まらずに権限を「確かめられない」とする。全体の上限（256 MiB）を越えた記録は、その後ろに正しい turn があっても使わない。
        1 行の上限（1 MiB）は test_codex_read_only_wider_than_the_profile_is_failed の big-wide-line が見る。"""
        sessions = self.cfg / "codex-home" / "sessions"
        rc, rec = self.run_launch("codex")  # 偽の Codex が、引数どおりの正しい記録を sessions/fake に書く
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        good = next((sessions / "fake").glob("rollout-*")).read_bytes()

        def placed(n, make):  # 試験が sessions/fake の外に置いた記録を、偽の Codex はそのまま使う
            thread = f"0199aaaa-bbbb-7ccc-8ddd-eeeeffff{3000 + n}"
            (sessions / "other").mkdir(exist_ok=True)
            make(sessions / "other" / f"rollout-1-{thread}.jsonl")
            return {"FAKE_THREAD": thread}
        for n, (name, make) in enumerate((("fifo", os.mkfifo), ("dir", os.mkdir), ("huge", lambda f: f.write_bytes(b"") or os.truncate(f, 300 << 20)))):
            with self.subTest(name):
                env = placed(n, make)
                writer = os.open(sessions / "other" / f"rollout-1-{env['FAKE_THREAD']}.jsonl", os.O_RDWR) if name == "fifo" and sys.platform.startswith("linux") else None
                try:  # 書き手のいる FIFO（O_RDWR で書き手を持つのは Linux の振る舞い）
                    rc, rec = self.run_launch("codex", env_extra=env)
                finally:
                    if writer is not None:
                        os.close(writer)
                self.assertEqual((rc, rec["status"], rec["blocked"]), (2, "failed", ["unchecked"]), rec)
                self.assertIn("確かめられない", rec["problems"][0])
        with self.subTest("全体の上限の後ろの正しい turn"):
            pad = b'{"type": "event_msg", "pad": "' + b"a" * ((1 << 20) - 64) + b'"}\n'  # 1 行は 1 MiB の内側
            env = placed(9, lambda f: f.write_bytes(pad * 257 + good))
            rc, rec = self.run_launch("codex", env_extra=env)
            self.assertEqual((rc, rec["status"], rec["blocked"]), (2, "failed", ["unchecked"]), rec)

    def test_claude_staging_dirs_stay_for_the_sessions_that_share_them(self):
        # Claude Code が設定を書くときの空の .claude/.cc-writes は、同じ場所の別のセッションも使う。launcher が子の後に消すと、
        # 並行するセッションの次の設定の書き込みが拒まれ、「常に許可」の規則が黙って失われた（#65）。子の起動の間にできたもの
        # （作ったのが子か、並行するセッションかは見分けられない）を、対象にも元の checkout にも残し、書く子の残りにも数えない
        for extra in ([], ["--write"]):
            with self.subTest(write=bool(extra)):
                repo, wt = self.worktree("staging" + "-write" * bool(extra))
                rc, rec = self.run_launch("claude", "checkout", extra=extra, cwd=wt)
                self.assertEqual(rec["status"], "ok", rec)
                self.assertEqual([str(p) for p in (wt, repo) if not (p / ".claude" / ".cc-writes").is_dir()], [], rec)

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
            env = self.launch_env(mode)
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
                self.assertEqual(rec["stopped_by_signal"], int(sig))
                self.assertEqual((rc, rec["status"]), (2, "failed"), rec)
                self.assertIn(rec["exit_code"], (143, -signal.SIGTERM), rec)  # 子の CLI が TERM で終わった（包む dotenvx は 143 で返す）
                self.assertIn(f"launcher がシグナル {int(sig)} を受けて、子に TERM を伝えて止めた", rec["problems"])
                self.assertTrue(rec["resumable"], rec)
                self.assertEqual(rec["session"], Path(str(self.args_file) + ".started").read_text())
                self.assertTrue((Path(rec["out"]) / "record.json").is_file())
        real_git = shutil.which("git")  # 偽の git の中から呼ぶ本物（PATH を変える前に引いた絶対パス）
        # 偽の子が引数を書いた後（子の起動の後）の git だけを遅くする。子が終わってから印を置くと、印より先に照合を終えうる（macOS の CI）
        (self.bin / "git").write_text(f"#!/bin/sh\n[ -e \"{self.args_file}\" ] && sleep 3\nexec {real_git} \"$@\"\n")
        (self.bin / "git").chmod(0o755)
        try:  # 子が終わった後、照合の git が遅い間に受けたシグナルは、子を止めたと記録しない
            self.args_file.unlink(missing_ok=True)  # 前の起動の引数（残っていると、子の起動の前の git も遅くなり、子の起動の前に送ってしまう）
            env = self.launch_env()
            for g in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x"]):  # 照合が git を呼ぶよう head を持たせる
                subprocess.run([real_git, "-C", str(self.cwd), *g], check=True, capture_output=True)
            p = subprocess.Popen([sys.executable, str(LAUNCH), "--host", "claude", "--cwd", str(self.cwd), "--prompt-file", str(self.prompt)],
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            deadline = time.time() + 30
            while not Path(str(self.args_file)).exists() and time.time() < deadline:  # 偽の子が起動した（引数を書いた）
                time.sleep(0.05)
            time.sleep(2)  # 偽の子は終わり、launcher は照合の git（3 秒）を待っている
            self.assertIsNone(p.poll())  # launcher はまだ記録を書いていない
            p.send_signal(signal.SIGTERM)
            out, _ = p.communicate(timeout=90)
            rec = json.loads(out)
            self.assertNotIn("stopped_by_signal", rec)
            self.assertFalse(any("シグナル" in x for x in rec.get("problems", [])), rec)
        finally:
            (self.bin / "git").unlink()
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
                if host == "claude":  # Claude Code の子に渡す依頼文は、先頭に launcher の 1 行が付く（続きでも）
                    self.assertFalse(prompt.lstrip().startswith("/"), prompt)
                    prompt = prompt.split("\n\n", 1)[1]
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

    def test_an_auditor_is_told_to_judge_every_item_of_the_skill_whatever_the_prompt(self):
        """親が書いた監査役の依頼文が判定の項目を狭めても、監査役は playbook-verify の節の項目をすべて判定するよう約束を受ける
        （Claude Code の続きでも）。監査役でない子には足さない（試験が見るのは、読み取り専用・--interrogate・--write の最初の起動だけ。
        --hardest の子と、監査役でない子の続きに足さないことは見ない）。"""
        sentence = load_launch().AUDIT_CONTRACT
        # 約束の意味を決める句は、句ごと固定する（言い換えると落ちる。語の有無だけでは、意味を逆にした書き換えが通る）
        for phrase in ("bb-writ のスキル `playbook-verify` の「返す前の判定（遵守の監査）」の節を全文読み",
                       "文の照合）をすべて判定する。依頼文が範囲や項目を狭めていても、節の項目を省かない",
                       "差分に限ってよいのは、節がそう書く項目だけ。文の照合は毎回、変えた文書の全体）",
                       "作業者が --not-run で届けるときは、親がその commit で実行した試験の証拠",  # #39 の宣言の確かめ
                       "子が実行できなかったことの代わりになるか（写し方を教えれば子が動いた場面でないか）も判定する"):
            self.assertIn(phrase, sentence)
        mark = sentence.split(":", 1)[0]  # 約束の見出し句。足さない側も、約束そのものの句で見る
        verify = (ROOT / "skills" / "playbook-verify" / "SKILL.md").read_text()
        self.assertIn("\n## 返す前の判定（遵守の監査）\n", verify)  # 約束が名指しする節と、節が挙げる項目
        self.assertIn("\n- 文の照合（手順の文書を変えたとき）:", verify)
        prompt = lambda: next(x for x in json.loads(self.args_file.read_text()) if "報告の形（bb-writ の委任の約束）" in x)
        for host in ("claude", "codex"):
            with self.subTest(host=host):
                rc, rec = self.run_launch(host, extra=["--audit"])
                self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
                self.assertIn(sentence, prompt())
                if host == "claude":  # 続きの依頼文にも足す
                    rc, again = self.run_launch(None, extra=["--resume", rec["out"]])
                    self.assertEqual((rc, again["audit"]), (0, True), again)
                    self.assertIn(sentence, prompt())
                for extra in ([], ["--interrogate"], ["--write"]):
                    self.args_file.unlink(missing_ok=True)
                    rc, rec = self.run_launch(host, extra=extra)
                    self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
                    self.assertNotIn(mark, prompt())

    def test_an_auditor_must_say_whether_to_deliver_and_the_record_keeps_it(self):
        """監査役の報告は deliver（yes か no）を持つ。無いか値が違えば invalid-report（続きの起動でも）。no も、試験を実行できなかった
        not-run も、判定を記録（委任の記録の行と record.json の両方。work.py は両方を照らす）に残す（work.py が関門に数えないのは
        test_work の側で見る）。監査役でない子には deliver を求めない。"""
        recorded = lambda rec: json.loads((Path(rec["out"]) / "record.json").read_text())
        line = lambda rec: next(json.loads(x) for x in (self.state / "delegations.jsonl").read_text().splitlines() if json.loads(x)["out"] == rec["out"])
        for mode, status, deliver in (("good", "ok", "yes"), ("deliver-no", "ok", "no"), ("deliver-not-run", "not-run", "yes"),
                                      ("deliver-missing", "invalid-report", None), ("deliver-upper", "invalid-report", None)):
            with self.subTest(mode=mode):
                rc, rec = self.run_launch("claude", mode=mode, extra=["--audit"])
                self.assertEqual((rec["status"], rec.get("deliver"), recorded(rec).get("deliver"), line(rec).get("deliver")),
                                 (status, deliver, deliver, deliver), rec)
                if status == "invalid-report":
                    self.assertTrue(any("deliver" in x for x in rec["problems"]), rec)
        rc, first = self.run_launch("claude", extra=["--audit"])
        rc, again = self.run_launch(None, mode="deliver-missing", extra=["--resume", first["out"]])  # 続きの監査役も判定を求める
        self.assertEqual((again["audit"], again["status"], again.get("deliver")), (True, "invalid-report", None), again)
        rc, rec = self.run_launch("claude", mode="deliver-missing")
        self.assertEqual((rec["status"], "deliver" in rec), ("ok", False), rec)

    def test_a_fable_child_that_cannot_start_is_restarted_once_on_the_fallback_tier(self):
        """Fable を頼んだ Claude Code の子が、費用が 0 のまま（作業を始める前に） API のエラーで終わったら、同じ起動の中で Opus high で 1 回だけ
        立て直し、記録に model_fallback を残す（#56）。Fable を頼まない子と、続き（--resume）は立て直さない。"""
        args = lambda: [json.loads(x) for x in [self.args_file.read_text()]][0]
        rc, rec = self.run_launch("claude", mode="fable-unavailable", extra=["--interrogate", "--model", "fable", "--effort", "medium"])
        self.assertEqual((rc, rec["status"], rec["interrogate"]), (0, "ok", True), rec)
        self.assertEqual((rec["model_requested"], rec["effort_requested"]), ("opus", "high"), rec)
        self.assertEqual((rec["model_fallback"]["from"], rec["model_fallback"]["to"]), (["fable", "medium"], ["opus", "high"]), rec)
        self.assertIn("Fable limit", rec["model_fallback"]["why"][0])
        first = json.loads((Path(rec["out"]) / "stdout-first.jsonl").read_text().strip().splitlines()[-1])
        self.assertNotEqual(first["session_id"], rec["session"])  # 立て直しは新しい session（最初の起動の session を使い回さない）
        argv = args()
        self.assertEqual((argv[argv.index("--model") + 1], argv[argv.index("--effort") + 1], argv[argv.index("--session-id") + 1]),
                         ("opus", "high", rec["session"]))
        self.assertEqual(rec["chosen_by"], "fallback")
        rc, again = self.run_launch(None, mode="fable-unavailable", extra=["--resume", rec["out"]])  # 続きは代えた段のまま起こす
        argv = args()
        self.assertEqual((again["status"], argv[argv.index("--model") + 1], "model_fallback" in again), ("ok", "opus", False), again)
        recorded = json.loads((Path(rec["out"]) / "record.json").read_text())
        self.assertEqual(recorded["model_fallback"], rec["model_fallback"])
        rc, rec = self.run_launch("claude", mode="fable-unavailable", extra=["--model", "opus", "--effort", "high"])  # Fable を頼まない子
        self.assertEqual((rec["status"], "model_fallback" in rec), ("ok", False), rec)
        rc, rec = self.run_launch("claude", extra=["--model", "fable", "--effort", "high"])  # Fable で動いた子は立て直さない
        self.assertNotIn("model_fallback", rec)
        # Fable のまま時間切れで止めた子の続きは、同じエラーでも立て直さない（続きの会話を捨てて最初から起こさない）
        rc, stopped = self.run_launch("claude", extra=["--model", "fable", "--effort", "high", "--timeout", "2"], env_extra={"FAKE_HANG": "1"})
        self.assertEqual(stopped["status"], "timeout", stopped)
        rc, again = self.run_launch(None, mode="fable-unavailable", extra=["--resume", stopped["out"]])
        self.assertEqual((again["status"], "model_fallback" in again, again["model_requested"]), ("failed", False, "fable"), again)
        # 途中まで進んで尽きた子・一時の過負荷・終わり方の欄が無い失敗は、立て直さない
        for mode in ("fable-ran-out", "fable-overloaded", "fable-no-reason"):
            with self.subTest(mode=mode):
                rc, rec = self.run_launch("claude", mode=mode, extra=["--model", "fable", "--effort", "high"])
                self.assertEqual((rec["status"], "model_fallback" in rec, rec["model_requested"]), ("failed", False, "fable"), rec)

    def test_a_restarted_child_gets_only_the_time_left(self):
        """立て直した子には、残りの期限だけを渡す（1 回の起動は --timeout を大きく越えない。#56 の審査）。"""
        rc, rec = self.run_launch("claude", mode="fable-slow", extra=["--model", "fable", "--effort", "high", "--timeout", "4"])
        self.assertEqual((rec["status"], "model_fallback" in rec), ("timeout", True), rec)
        self.assertLess(rec["duration_s"], 6.5, rec)  # 取り直すと 2 秒の失敗の後に 4 秒待ち、さらに片付けを待つ

    def test_a_child_that_ran_on_another_model_is_recorded_as_substituted(self):
        """頼んだモデルと違うモデルで動いた Claude Code の子は、記録に model_substituted を残し、失敗にはしない（#56）。"""
        rc, rec = self.run_launch("claude", extra=["--model", "opus", "--effort", "high"])  # 偽の子は sonnet で動いたと返す
        self.assertEqual((rec["status"], rec.get("model_substituted")), ("ok", True), rec)
        for model in ("sonnet", "Sonnet", "sonnet[1m]"):  # 付記と大文字小文字は問わない
            rc, rec = self.run_launch("claude", extra=["--model", model, "--effort", "medium"])
            self.assertEqual((rec["status"], "model_substituted" in rec), ("ok", False), (model, rec))

    def test_an_auditor_is_recorded_and_its_resume_stays_one(self):
        """work.py は、すべての push・pr で HEAD を対象にした監査役（返す前の判定）の記録を求める。続きも監査役のまま、
        ほかの子の続きは監査役に化けない。審査役・書く子とは兼ねない。"""
        last = lambda: json.loads((self.state / "delegations.jsonl").read_text().splitlines()[-1])
        rc, rec = self.run_launch("claude")
        self.assertEqual((rc, rec["audit"], last()["audit"]), (0, False, False), rec)
        self.assertEqual((Path(rec["out"]) / "prompt.md").read_text(), self.prompt.read_text())  # どの子も依頼文を残す
        rc, rec = self.run_launch("claude", extra=["--audit", "--timeout", "2"], env_extra={"FAKE_HANG": "1"})
        self.assertEqual((rec["status"], rec["audit"], last()["audit"]), ("timeout", True, True), rec)
        self.assertIs(json.loads((Path(rec["out"]) / "record.json").read_text())["audit"], True)
        # 監査役が、記録の要約でなく、子に何を頼んだかを読めるように、依頼文を out に残す
        self.assertEqual((Path(rec["out"]) / "prompt.md").read_text(), self.prompt.read_text())
        rc, again = self.run_launch(None, extra=["--resume", rec["out"]])
        self.assertEqual((rc, again["status"], again["audit"], last()["audit"]), (0, "ok", True, True), again)
        rc, plain = self.run_launch("claude", extra=["--timeout", "2"], env_extra={"FAKE_HANG": "1"})
        old = Path(self.tmp.name) / "old-audit-record"  # 前の版の launcher の記録には欄が無い
        old.mkdir()
        (old / "record.json").write_text(json.dumps({k: v for k, v in plain.items() if k != "audit"}))
        for case, prev in (("launcher が書いた記録", plain["out"]), ("audit の欄の無い前の版の記録", str(old))):
            with self.subTest(case):
                p = self.run_launch(None, extra=["--resume", prev], raw=True)
                self.assertEqual(p.returncode, 0, p.stderr)
                again = json.loads(p.stdout)
                self.assertEqual((again["status"], again["audit"], last()["audit"]), ("ok", False, False), again)
        for extra in (["--host", "claude", "--cwd", str(self.cwd), "--write", "--audit"],
                      ["--host", "claude", "--cwd", str(self.cwd), "--interrogate", "--audit"],
                      ["--resume", rec["out"], "--audit"]):
            with self.subTest(extra=extra):
                self.args_file.unlink(missing_ok=True)
                p = self.run_launch(None, extra=extra, raw=True)
                self.assertEqual(p.returncode, 2, p.stderr)
                self.assertIn("一緒に渡さない", p.stderr)
                self.assertFalse(self.args_file.exists())  # 子を起動しない

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
        rc, plain = self.run_launch("claude", extra=["--timeout", "2"], env_extra={"FAKE_HANG": "1"})  # 条件 (5) の子の続きは審査役に化けない
        old = Path(self.tmp.name) / "old-record"  # 前の版の launcher の記録には欄が無い
        old.mkdir()
        (old / "record.json").write_text(json.dumps({k: v for k, v in plain.items() if k != "interrogate"}))
        for case, prev in (("launcher が書いた記録", plain["out"]), ("interrogate の欄の無い前の版の記録", str(old))):
            with self.subTest(case):
                p = self.run_launch(None, extra=["--resume", prev], raw=True)
                self.assertEqual(p.returncode, 0, p.stderr)
                again = json.loads(p.stdout)
                self.assertEqual((again["status"], again["interrogate"], last()["interrogate"]), ("ok", False, False), again)
        for extra, words in ((["--host", "claude", "--cwd", str(self.cwd), "--write", "--interrogate"], "--write と一緒に渡さない"),
                             (["--resume", rec["out"], "--interrogate"], "一緒に渡さない")):
            with self.subTest(extra=extra):
                self.args_file.unlink(missing_ok=True)
                p = self.run_launch(None, extra=extra, raw=True)
                self.assertEqual(p.returncode, 2, p.stderr)
                self.assertIn(words, p.stderr)
                self.assertFalse(self.args_file.exists())  # 子を起こさない

    # --- launcher は、自分が状態の置き場に作った一時の置き場を自分で片付ける（sweep_leftovers） ---

    def stamp(self, days_ago=0.0, tail="-0123456789abcdef"):
        """launcher が置き場に付ける形の名前（先頭が日時）。日時は days_ago 日前。"""
        return time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() - days_ago * 86400)) + tail

    def leftover(self, kind, name, *rows, link_to=None):
        """状態の置き場に、launcher が作る形の一時の置き場を置き、それを指す委任の記録（rows）を足す。kind は "tmp"（読み取り専用の Codex の子の
        一時ディレクトリ）か "home"（Grok の子の起動ごとの home）。中には、子が残す写しの形（書けないディレクトリと読むだけのファイル）を置く。"""
        p = self.state / "delegations" / name / "tmp" if kind == "tmp" else self.state / "grok-homes" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        if link_to is not None:
            p.symlink_to(link_to, target_is_directory=True)
        else:
            deep, shut = p / "copy" / "deep", p / "copy" / "shut"
            for d in (deep, shut):
                d.mkdir(parents=True)
                (d / "f").write_text("x")
            os.chmod(deep / "f", 0o444)
            os.chmod(deep, 0o555)
            os.chmod(shut, 0o000)  # 中身のある、読めないディレクトリ
            # 残った場合も、試験の一時ディレクトリを消せるように
            self.addCleanup(lambda: [os.chmod(d, 0o755) for d in (deep, shut) if d.is_dir() and not d.is_symlink()])
        with open(self.state / "delegations.jsonl", "a") as f:
            for r in rows:
                f.write(json.dumps({"scratch" if kind == "tmp" else "grok_home": os.path.realpath(p), "cwd": str(self.cwd.resolve()),
                                    "session": "s-" + name, "status": "ok", **r}) + "\n")
        return p

    def sweep_by_a_launch(self):
        """何でもない起動を 1 回する（launcher は起動のたびに片付ける）。記録を返す。"""
        rc, rec = self.run_launch("claude")
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        return rec

    def test_the_launcher_removes_its_leftovers_after_the_retention(self):
        """最後に使われてから 7 日を過ぎた一時の置き場は、記録の状態によらず消える（時間切れの子のものも）。報告と記録は消えない。"""
        old = time.time() - 8 * 86400
        gone = [self.leftover("tmp", self.stamp(9, "-" + s), {"at": old, "status": s}) for s in ("ok", "timeout", "failed", "not-run")]
        gone.append(self.leftover("home", self.stamp(9, "-home"), {"at": old}))
        shut = self.leftover("tmp", self.stamp(9, "-shut"), {"at": old})  # 子が、置き場そのものを読めない権限にして終わった
        os.chmod(shut, 0o000)
        self.addCleanup(lambda: shut.is_dir() and os.chmod(shut, 0o755))
        gone.append(shut)
        with_report = gone[0].parent  # --out を渡さない起動は、報告と記録が同じ置き場にある
        (with_report / "record.json").write_text("{}")
        (with_report / "report.md").write_text("報告")
        rows = (self.state / "delegations.jsonl").read_text()
        rec = self.sweep_by_a_launch()
        self.assertEqual([p for p in gone if os.path.lexists(p)], [])
        self.assertEqual(sorted(rec["swept"]["removed"]), sorted(str(p) for p in gone))
        self.assertEqual(rec["swept"]["problems"], [])
        self.assertEqual(sorted(x.name for x in with_report.iterdir()), ["record.json", "report.md"])
        self.assertEqual([p.parent.name for p in gone[1:4] + [shut] if p.parent.exists()], [])  # tmp だけだった置き場は、空になったら外す
        self.assertTrue((self.state / "delegations.jsonl").read_text().startswith(rows))  # 委任の記録は消さない（足すだけ）

    def test_places_used_recently_or_without_a_date_stay(self):
        now = time.time()
        kept = [self.leftover("tmp", self.stamp(30, "-resumed"), {"at": now - 30 * 86400}, {"at": now - 86400}),  # 古い置き場でも、続きの起動が最近
                self.leftover("tmp", self.stamp(30, "-order"), {"at": now - 86400}, {"at": now - 30 * 86400}),  # 記録の並びは時刻の順とは限らない
                self.leftover("tmp", self.stamp(1, "-newname"), {"at": now - 30 * 86400}),  # 記録の時刻より名前の日時が新しい
                self.leftover("tmp", self.stamp(1, "-noat"), {}),       # 記録に時刻が無い（前の版の記録）: 名前の日時で決める
                self.leftover("tmp", self.stamp(-30, "-future"), {}),   # 未来の日時の名前だけ: 使える時刻が無いので、期限では消さない
                self.leftover("tmp", "99991231-235959-far", {}),        # 日時として読めるが、遠い未来
                self.leftover("tmp", "名前に日時が無い", {}),             # 記録にも名前にも時刻が無ければ、期限では消さない
                self.leftover("tmp", "名前に日時が無く、時刻が真偽値", {"at": True}),  # 真偽値は時刻でない（1 秒として数えない）
                self.leftover("tmp", self.stamp(6.5, "-edge"), {"at": now - 6.5 * 86400}),
                self.leftover("home", self.stamp(1, "-home"), {"at": now - 86400})]
        rec = self.sweep_by_a_launch()
        self.assertEqual([p for p in kept if not p.is_dir()], [])
        self.assertNotIn("swept", rec)

    def test_places_of_a_cleaned_up_task_go_without_waiting(self):
        """片付けたタスクの作業場所（状態の置き場の worktrees/<リポジトリ>/<タスク>）を見ていた子の置き場は、期限を待たずに消える。"""
        trees = (self.state / "worktrees" / "repo-0123456789").resolve()
        (trees / "alive").mkdir(parents=True)
        now = time.time()
        gone = self.leftover("tmp", self.stamp(0, "-gone"), {"at": now, "cwd": str(trees / "cleaned")}, {"at": now, "cwd": str(trees / "cleaned" / "sub")})
        kept = [self.leftover("tmp", self.stamp(0, "-alive"), {"at": now, "cwd": str(trees / "alive")}),
                self.leftover("tmp", self.stamp(0, "-mixed"), {"at": now, "cwd": str(trees / "cleaned")}, {"at": now, "cwd": str(trees / "alive")}),
                # 作業場所でない対象が無いだけでは、すぐには消さない（外した媒体や、作り直される場所かもしれない）
                self.leftover("tmp", self.stamp(0, "-elsewhere"), {"at": now, "cwd": str(Path(self.tmp.name).resolve() / "no-such-place")}),
                self.leftover("tmp", self.stamp(0, "-repo"), {"at": now, "cwd": str(trees.parent / "no-such-repo")}),
                self.leftover("home", self.stamp(0, "-nocwd"), {"at": now, "cwd": None}),
                # 作業場所の名前が、行き先の無い symlink として残っている（名前があるので、無くなったとは見なさない）
                self.leftover("tmp", self.stamp(0, "-dangling"), {"at": now, "cwd": str(trees / "dangling")})]
        (trees / "dangling").symlink_to(trees / "nowhere", target_is_directory=True)
        rec = self.sweep_by_a_launch()
        self.assertFalse(os.path.lexists(gone))
        self.assertEqual([p for p in kept if not p.is_dir()], [])
        self.assertEqual((rec["swept"]["removed"], rec["swept"]["problems"]), ([str(gone)], []))  # 残る置き場が、判定の失敗で残ったのでないこと

    def test_a_real_read_only_codex_child_of_a_cleaned_up_task_is_swept_and_cannot_be_continued(self):
        """本物の起動が書いた記録の形で: タスクの作業場所を見た読み取り専用の Codex の子の一時ディレクトリは、作業場所が消えた後の起動で消え、
        その記録からは続きから起動できない。"""
        task = self.state / "worktrees" / "repo-0123456789" / "0123456789abcdef"
        task.mkdir(parents=True)
        (task / "a.txt").write_text("x\n")
        rc, first = self.run_launch("codex", cwd=task)
        self.assertEqual(first["status"], "ok", first)
        scratch = Path(first["scratch"])
        self.assertTrue(scratch.is_dir())
        self.sweep_by_a_launch()
        self.assertTrue(scratch.is_dir())  # 作業場所がある間は残る
        shutil.rmtree(task)
        rec = self.sweep_by_a_launch()
        self.assertFalse(scratch.exists())
        self.assertEqual([os.path.realpath(x) for x in rec["swept"]["removed"]], [str(scratch)])
        self.assertTrue((Path(first["out"]) / "record.json").is_file())  # 記録は残る
        self.args_file.unlink()
        p = self.run_launch(None, extra=["--resume", first["out"]], raw=True)
        self.assertEqual(p.returncode, 2, p.stdout)
        self.assertIn("新しく起動する", p.stderr)
        self.assertFalse(self.args_file.exists())

    def test_a_place_without_a_record_is_judged_by_the_date_in_its_name(self):
        """記録の無い置き場は、動いている最初の起動（記録は終わりに書く）か、記録を書く前に止まった起動のもの。名前の日時だけで決める。"""
        places = [self.leftover("tmp", self.stamp(0, "-running")), self.leftover("tmp", self.stamp(8, "-dead")),
                  self.leftover("home", self.stamp(0, "-running")), self.leftover("home", self.stamp(8, "-dead")),
                  self.leftover("home", "tmpabc123")]
        self.sweep_by_a_launch()
        self.assertEqual([os.path.lexists(p) for p in places], [True, False, True, False, True])

    def test_a_place_in_use_is_kept_whatever_the_records_say(self):
        """launcher が使っている置き場は、記録がどう書かれていても消えない（最初の起動も、続きも。Codex の一時ディレクトリも、Grok の home も）。
        使い終わった後の起動で消える。守りは、使う launcher が終わりまで持つ置き場のディレクトリの排他で、委任の記録の値には頼らない。"""
        gone = str(self.state.resolve() / "worktrees" / "r" / "cleaned")
        task = self.state / "worktrees" / "repo-0123456789" / "0123456789abcdef"
        task.mkdir(parents=True)
        (task / "a.txt").write_text("x\n")
        rows = self.state / "delegations.jsonl"

        def forge(place):  # その置き場を指す記録を、「片付けたタスクを見ていた別の session のもの」だけにする（本物の行は消す）
            # 本物の行は、道筋を解いて比べる（Grok の記録の grok_home は解く前の道筋で、一時の置き場が symlink の下にある macOS では、
            # 解いた道筋の文字列では見つからず、本物の行が残って置き場が対象にならなかった）
            key, real = "scratch" if place.name == "tmp" else "grok_home", os.path.realpath(place)
            kept = [x for x in (rows.read_text().splitlines() if rows.exists() else [])
                    if not any(isinstance(v, str) and v and os.path.realpath(v) == real for v in (json.loads(x).get("scratch"), json.loads(x).get("grok_home")))]
            self.assertTrue(all(real not in x for x in kept))
            rows.write_text("".join(x + "\n" for x in kept) + json.dumps({key: real, "cwd": gone, "session": "changed", "at": 0}) + "\n")

        def places():
            return set((self.state / "delegations").glob("*/tmp")) | set((self.state / "grok-homes").glob("*"))
        for host in ("codex", "grok"):
            cwd = task if host == "codex" else self.grok_target()
            for how in ("first", "resume"):
                with self.subTest(host=host, how=how):
                    before, extra = places(), ["--host", host, "--cwd", str(cwd)]
                    if how == "resume":
                        rc, first = self.run_launch(host, extra=["--timeout", "2"], cwd=cwd, env_extra={"FAKE_HANG": "1"})
                        before, extra = places(), ["--resume", first["out"]]
                    self.args_file.unlink(missing_ok=True)
                    p = subprocess.Popen([sys.executable, str(LAUNCH), "--prompt-file", str(self.prompt), *extra, "--timeout", "60"],
                                         env=self.launch_env(env_extra={"FAKE_HANG": "1"}), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    self.addCleanup(p.kill)
                    deadline = time.time() + 30
                    while not self.args_file.exists() and time.time() < deadline:  # 偽の子が動き始めるまで
                        time.sleep(0.05)
                    self.assertTrue(self.args_file.exists(), "子が起動しない")
                    # 続きが使う置き場: Codex は前と同じ一時ディレクトリ。Grok は新しい home と、session を写す元の前の home
                    used = (places() - before) | ({Path(first["scratch" if host == "codex" else "grok_home"])} if how == "resume" else set())
                    self.assertTrue(used)
                    for place in used:
                        forge(place)
                    self.sweep_by_a_launch()
                    self.assertEqual([x for x in used if not x.is_dir()], [], "使っている置き場が消えた")
                    p.send_signal(signal.SIGTERM)
                    p.communicate(timeout=60)
                    for place in used:
                        forge(place)
                    self.sweep_by_a_launch()
                    self.assertEqual([x for x in used if os.path.lexists(x)], [], "使い終わった置き場が残った")

    def test_a_place_used_again_while_the_sweep_was_deciding_is_kept(self):
        """片付けが置き場の排他を取るまでの間に、続きの起動がその置き場を使い終えて記録を足していたら、消さない（排他を取った後に、
        記録を読み直して決め直す）。その瞬間を作るために、排他を取る関数を名前で差し替える（hold_place の名前を変えたら、ここも直す）。"""
        place = self.leftover("tmp", self.stamp(9, "-raced"), {"at": time.time() - 8 * 86400})
        row = json.dumps({"scratch": os.path.realpath(place), "cwd": str(self.cwd.resolve()), "session": "s", "status": "ok", "at": time.time()})
        patch = (f"real = launch.hold_place\ndef raced(p):\n    got = real(p)\n    open({str(self.state / 'delegations.jsonl')!r}, 'a').write({row + chr(10)!r})\n"
                 "    return got\nlaunch.hold_place = raced")
        rc, rec = self.launch_patched(patch, ["--host", "claude", "--cwd", str(self.cwd)])
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        self.assertTrue(place.is_dir(), "使ったばかりの置き場が消えた")
        self.assertNotIn("swept", rec)

    def test_a_deep_tree_does_not_stop_the_sweep(self):
        """子が残した深い木（再帰では降りきれない深さ）も消え、名前の順でその後ろの置き場も消える。開いておけるファイルの数が
        少ない環境でも同じ（降りた段の fd を開いたままにしない）。上限は、launcher のプロセスで下げて確かめる。"""
        old = time.time() - 8 * 86400
        deep = self.leftover("tmp", self.stamp(9, "-a-deep"), {"at": old})
        fd = os.open(deep, os.O_RDONLY)  # fd からの相対で作る（絶対パスでは、道筋の長さの上限を超える。macOS は 1,024）
        try:
            for _ in range(1200):
                os.mkdir("t", dir_fd=fd)
                below = os.open("t", os.O_RDONLY, dir_fd=fd)
                os.close(fd)
                fd = below
        finally:
            os.close(fd)
        after = self.leftover("tmp", self.stamp(9, "-b-after"), {"at": old})
        rc, rec = self.run_launch("claude", env_extra=self.launcher_site(
            "import resource\nresource.setrlimit(resource.RLIMIT_NOFILE, (200, resource.getrlimit(resource.RLIMIT_NOFILE)[1]))"))
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        self.assertEqual((sorted(rec["swept"]["removed"]), rec["swept"]["problems"]), (sorted([str(deep), str(after)]), []))
        self.assertFalse(os.path.lexists(deep) or os.path.lexists(after))

    def test_a_place_with_thousands_of_files_in_one_directory_goes_in_one_sweep(self):
        """1 つのディレクトリに数千のファイルとディレクトリがある置き場も、1 回の片付けで消える（子は、対象の写しを置き場に作る）。
        消しながら読むと残りを読み飛ばす実装（macOS の HFS+ の readdir）では、項目の多いディレクトリで残りが出る。"""
        place = self.leftover("tmp", self.stamp(9, "-many"), {"at": time.time() - 8 * 86400})
        many = place / "copy" / "many"
        many.mkdir()
        for i in range(3000):
            (many / f"f{i:04d}").write_bytes(b"")
        for i in range(60):
            (many / f"d{i:02d}").mkdir()
            (many / f"d{i:02d}" / "x").write_bytes(b"")
        rec = self.sweep_by_a_launch()
        self.assertFalse(os.path.lexists(place))
        self.assertEqual((rec["swept"]["removed"], rec["swept"]["problems"]), ([str(place)], []))

    def test_a_place_goes_even_where_reading_a_directory_skips_what_follows_a_removal(self):
        """ディレクトリを位置で読み進める実装（消した分だけ後ろが詰まり、読みながら消すと残りを読み飛ばす）でも、置き場は 1 回の片付けで
        全部消える。その実装を、launcher の中の os.scandir（fd を渡す呼び出しだけ）を差し替えて作る: 呼ばれるたびに今の中身を並べ直して、
        何番目かで次を返す。見るのは結果（置き場が残らない）で、読む順そのものではない。"""
        place = self.leftover("tmp", self.stamp(9, "-skip"), {"at": time.time() - 8 * 86400})
        for i in range(40):
            (place / f"f{i:02d}").write_text("x")
        site = self.launcher_site("""
            import stat
            real_scandir = os.scandir
            class Entry:
                def __init__(self, fd, name):
                    self.fd, self.name = fd, name
                def is_dir(self, follow_symlinks=True):
                    return stat.S_ISDIR(os.stat(self.name, dir_fd=self.fd, follow_symlinks=follow_symlinks).st_mode)
            class ByPosition:
                def __init__(self, fd):
                    self.fd, self.pos = fd, 0
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
                def __iter__(self):
                    return self
                def __next__(self):
                    names = sorted(os.listdir(self.fd))
                    if self.pos >= len(names):
                        raise StopIteration
                    self.pos += 1
                    return Entry(self.fd, names[self.pos - 1])
            os.scandir = lambda p=".": ByPosition(p) if isinstance(p, int) else real_scandir(p)
        """)
        rc, rec = self.run_launch("claude", env_extra=site)
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        self.assertFalse(os.path.lexists(place))
        self.assertEqual((rec["swept"]["removed"], rec["swept"]["problems"]), ([str(place)], []))

    def test_a_place_too_big_to_list_in_memory_does_not_stop_the_others(self):
        """中身を並べるメモリが足りない置き場（項目が入りきらないディレクトリ）があっても、ほかの置き場の片付けは止まらない。その置き場は
        残り、理由が記録に出る。その場面を、launcher の中の remove_tree を、最初の 1 つの置き場だけ MemoryError にして作る。"""
        old = time.time() - 8 * 86400
        first, second = self.leftover("tmp", self.stamp(9, "-a-big"), {"at": old}), self.leftover("tmp", self.stamp(9, "-b-next"), {"at": old})
        patch = """
            real_remove, seen = launch.remove_tree, []
            def remove_tree(p):
                seen.append(p)
                if len(seen) == 1:
                    raise MemoryError()
                return real_remove(p)
            launch.remove_tree = remove_tree
        """
        rc, rec = self.launch_patched(patch, ["--host", "claude", "--cwd", str(self.cwd)])
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        self.assertEqual((first.is_dir(), os.path.lexists(second)), (True, False))
        self.assertEqual((rec["swept"]["removed"], len(rec["swept"]["problems"])), ([str(second)], 1), rec["swept"])
        self.assertIn("MemoryError", rec["swept"]["problems"][0])

    def test_times_that_cannot_be_right_do_not_keep_a_place(self):
        """数でない・無限大・未来の時刻の記録は、期限の時計に使わない（偽の行 1 つで、置き場を残し続けられない）。"""
        old = time.time() - 8 * 86400
        gone = [self.leftover("tmp", self.stamp(9, "-" + str(i)), {"at": old}, {"at": bad})
                for i, bad in enumerate((float("nan"), float("inf"), time.time() + 30 * 86400, 10 ** 400, "2026-11-10", True, None))]
        self.sweep_by_a_launch()
        self.assertEqual([p.parent.name for p in gone if os.path.lexists(p)], [])

    def test_a_linked_delegations_or_homes_directory_is_not_followed(self):
        """状態の置き場の delegations・grok-homes そのものが symlink なら、行き先の中は片付けない（状態の置き場の外かもしれない）。理由は記録に出る。"""
        outside = Path(self.tmp.name).resolve() / "elsewhere"
        for d in ("delegations", "grok-homes"):
            kept = outside / d / self.stamp(9, "-x") / ("tmp" if d == "delegations" else "")
            kept.mkdir(parents=True)
            (kept / "keep").write_text("x")
            shutil.rmtree(self.state / d, ignore_errors=True)
            (self.state / d).symlink_to(outside / d, target_is_directory=True)
        rec = self.run_launch("claude", extra=["--out", str(Path(self.tmp.name) / "out-linked")])[1]
        self.assertEqual(rec["status"], "ok", rec)
        self.assertEqual(len(list(outside.rglob("keep"))), 2)
        self.assertEqual(rec["swept"]["removed"], [])
        self.assertEqual(len(rec["swept"]["problems"]), 2, rec["swept"])
        self.assertIn("symlink", rec["swept"]["problems"][0])

    def test_only_real_places_inside_the_state_are_removed(self):
        """消すのは、状態の置き場の delegations/<名前>/tmp と grok-homes/<名前> の実のディレクトリだけ。symlink の行き先と、記録が指す外の場所は消さない。"""
        old = time.time() - 8 * 86400
        outside = Path(self.tmp.name).resolve() / "outside"
        for d in "abcde":
            (outside / d / "tmp").mkdir(parents=True)
            (outside / d / "tmp" / "keep").write_text("x")
        link_tmp = self.leftover("tmp", self.stamp(9, "-linktmp"), {"at": old}, link_to=outside / "a" / "tmp")
        link_dir = self.state / "delegations" / self.stamp(9, "-linkdir")
        link_dir.symlink_to(outside / "b", target_is_directory=True)
        link_home = self.leftover("home", self.stamp(9, "-linkhome"), {"at": old}, link_to=outside / "c" / "tmp")
        with open(self.state / "delegations.jsonl", "a") as f:  # 記録が、状態の置き場の外を指す
            f.write(json.dumps({"scratch": str(outside / "d" / "tmp"), "grok_home": str(outside / "d"), "at": old, "session": "x",
                                "cwd": str(self.state.resolve() / "worktrees" / "r" / "gone")}) + "\n")
        inner = self.leftover("tmp", self.stamp(9, "-inner"), {"at": old})  # 消える置き場の中の、外への symlink
        os.chmod(inner / "copy" / "deep", 0o755)
        (inner / "copy" / "deep" / "link").symlink_to(outside / "e" / "tmp", target_is_directory=True)
        rec = self.sweep_by_a_launch()
        self.assertEqual(sorted(str(x.relative_to(outside)) for x in outside.rglob("keep")), [f"{d}/tmp/keep" for d in "abcde"])
        self.assertTrue(link_tmp.is_symlink() and link_dir.is_symlink() and link_home.is_symlink())
        self.assertEqual((rec["swept"]["removed"], rec["swept"]["problems"]), ([str(inner)], []))

    def test_a_resume_is_refused_once_its_place_is_gone(self):
        """一時の置き場が無い記録からは、続きから起動しない（空で作り直して、前の起動の写しを失ったまま続けさせない）。"""
        for host, key, word in (("codex", "scratch", "一時ディレクトリ（scratch）が無い"), ("grok", "grok_home", "grok_home が無い")):
            for shape in ("gone", "link") if host == "codex" else ("gone",):  # 置き場の名前が symlink に替わる場面は、形が同じなので Codex だけ
                with self.subTest(host=host, shape=shape):
                    rc, first = self.run_launch(host, extra=["--timeout", "2"], cwd=self.grok_target() if host == "grok" else None, env_extra={"FAKE_HANG": "1"})
                    self.assertTrue(first["resumable"], first)
                    place = Path(first[key])
                    shutil.rmtree(place)
                    if shape == "link":  # 置き場の名前が、別の場所への symlink に替わっている
                        elsewhere = Path(tempfile.mkdtemp(dir=self.tmp.name))
                        place.symlink_to(elsewhere, target_is_directory=True)
                    self.args_file.unlink(missing_ok=True)
                    p = self.run_launch(None, extra=["--resume", first["out"]], raw=True)
                    self.assertEqual(p.returncode, 2, p.stdout)
                    self.assertIn(word, p.stderr)
                    self.assertIn("新しく起動する", p.stderr)
                    self.assertFalse(self.args_file.exists())  # 子を起こさない
                    self.assertFalse(place.is_dir() and not place.is_symlink())  # 空で作り直さない

    def test_a_place_due_for_removal_cannot_be_continued_even_before_it_is_removed(self):
        """片付けの対象になった置き場からは、まだ消えていなくても続きを起動しない。片付けが途中で止まって中身が半分になった置き場は、
        対象のままなので、そこから続くことが無い（偽の行・時計の巻き戻し・作業場所の作り直しが無い間。その順序は test_sweep_model が
        たどる）。続けられるかどうかが、片付けが先に走ったかに左右されない。"""
        task = self.state / "worktrees" / "repo-0123456789" / "0123456789abcdef"
        task.mkdir(parents=True)
        (task / "a.txt").write_text("x\n")
        rc, first = self.run_launch("codex", extra=["--timeout", "2"], cwd=task, env_extra={"FAKE_HANG": "1"})
        self.assertTrue(first["resumable"], first)
        os.rename(task, task.with_name("moved-away"))  # 見ていた作業場所が無くなった（置き場は、まだ誰も片付けていない）
        self.args_file.unlink(missing_ok=True)
        p = self.run_launch(None, extra=["--resume", first["out"]], raw=True)
        self.assertEqual(p.returncode, 2, p.stdout)
        self.assertIn("もう続きから起動できない", p.stderr)
        self.assertIn("新しく起動する", p.stderr)
        self.assertFalse(self.args_file.exists())  # 子を起こさない
        self.assertTrue(Path(first["scratch"]).is_dir())  # 拒むだけで、ここでは消さない（次の起動の片付けが消す）

    def test_a_directory_swapped_for_a_link_during_the_sweep_is_not_followed(self):
        """片付けが置き場の中を降りる途中で、並べ終えたディレクトリを外への symlink に差し替えられても、行き先の中は消えない。
        差し替えの時機を決めるために、launcher の中の os.open を差し替える（降りる先を開く直前に、名前を入れ替える）。"""
        outside = Path(self.tmp.name).resolve() / "outside-swap"
        outside.mkdir()
        (outside / "keep").write_text("x")
        place = self.leftover("tmp", self.stamp(9, "-swapped"), {"at": time.time() - 8 * 86400})
        (place / "victim").mkdir()
        (place / "victim" / "inner").write_text("x")
        site = self.launcher_site(f"""
            _real, _done = os.open, []
            def racing(path, flags, mode=0o777, *, dir_fd=None):
                if path == 'victim' and dir_fd is not None and not _done:
                    _done.append(1)
                    os.rename('victim', 'moved', src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                    os.symlink({str(outside)!r}, 'victim', dir_fd=dir_fd)
                return _real(path, flags, mode, dir_fd=dir_fd)
            os.open = racing
        """)
        rc, rec = self.run_launch("claude", env_extra=site)
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        self.assertTrue((outside / "keep").exists(), "差し替えた先の中が消えた")
        self.assertTrue((place / "victim").is_symlink())  # 差し替えは起きた
        self.assertEqual(rec["swept"]["removed"], [])
        self.assertIn("片付けられない", rec["swept"]["problems"][0])

    def test_a_place_swept_just_before_a_resume_takes_it_is_not_used(self):
        """続きの起動が置き場を開いてから排他を取るまでの間に、片付けがその置き場を消し終えていたら、続きを起動しない
        （消えたディレクトリの排他は取れてしまうので、取った後に、まだその名前にあるかを確かめる）。その瞬間を作るために、
        launcher の中の flock を差し替える（ディレクトリの排他を取る直前に、置き場を消す）。"""
        rc, first = self.run_launch("codex", extra=["--timeout", "2"], env_extra={"FAKE_HANG": "1"})
        place = first["scratch"]
        self.args_file.unlink(missing_ok=True)
        site = self.launcher_site(f"""
            import fcntl, shutil, stat
            real = fcntl.flock
            def late(fd, op):
                n = fd if isinstance(fd, int) else fd.fileno()
                if stat.S_ISDIR(os.fstat(n).st_mode):
                    shutil.rmtree({place!r}, ignore_errors=True)
                return real(fd, op)
            fcntl.flock = late
        """)
        p = self.run_launch(None, extra=["--resume", first["out"]], env_extra=site, raw=True)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("一時ディレクトリ（scratch）が無い", p.stderr)
        self.assertFalse(self.args_file.exists())      # 子を起こさない
        self.assertFalse(os.path.lexists(place))        # 空で作り直さない

    def test_a_place_directory_that_cannot_be_listed_is_reported(self):
        """delegations・grok-homes の中を並べられないときは、黙って飛ばさず、理由を記録に出す（無いだけなら、何も出さない）。"""
        (self.state / "delegations").mkdir()
        os.chmod(self.state / "delegations", 0o000)
        try:
            rc, rec = self.run_launch("claude", extra=["--out", str(Path(self.tmp.name) / "out-unlisted")])
        finally:
            os.chmod(self.state / "delegations", 0o755)  # 試験の一時ディレクトリを消せるように
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        self.assertEqual((rec["swept"]["removed"], len(rec["swept"]["problems"])), ([], 1), rec["swept"])
        self.assertIn("並べられない", rec["swept"]["problems"][0])

    def test_broken_lines_in_the_records_do_not_stop_the_sweep(self):
        """委任の記録に、読めない行・形の違う行があっても、片付けは止まらない（その行は使わないだけ）。"""
        place = self.leftover("tmp", self.stamp(9, "-ok"), {"at": time.time() - 8 * 86400})
        with open(self.state / "delegations.jsonl", "a") as f:
            f.write("{\n" + "[1, 2]\n" + '"文字列"\n' + "[" * 100000 + "]" * 100000 + "\n"
                    + json.dumps({"scratch": 123, "grok_home": ["x"], "at": "きのう"}) + "\n"
                    + json.dumps({"scratch": "a\u0000b", "at": 1}) + "\n" + "\n")
        rec = self.sweep_by_a_launch()
        self.assertEqual((rec["swept"]["removed"], rec["swept"]["problems"]), ([str(place)], []))

    def test_nothing_is_removed_where_a_directory_cannot_be_locked(self):
        """ディレクトリの排他（flock）を取れない環境では、使っている置き場を見分けられないので、片付けは何も消さず、理由を 1 度だけ記録に出す。
        最初の起動は、排他なしで進む。その環境を作るために、launcher の中の flock を、ディレクトリには使えない応答に差し替える。"""
        old = time.time() - 8 * 86400
        places = [self.leftover("tmp", self.stamp(9, "-one"), {"at": old}), self.leftover("home", self.stamp(9, "-two"), {"at": old})]
        site = self.launcher_site("""
            import errno, fcntl, stat
            real = fcntl.flock
            def unsupported(fd, op):
                n = fd if isinstance(fd, int) else fd.fileno()
                if stat.S_ISDIR(os.fstat(n).st_mode):
                    raise OSError(errno.EOPNOTSUPP, 'ディレクトリの排他は使えない')
                return real(fd, op)
            fcntl.flock = unsupported
        """)
        rc, rec = self.run_launch("codex", env_extra=site)
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)  # 読み取り専用の Codex の子（自分の一時ディレクトリを持つ）も起動する
        self.assertEqual([p for p in places if not p.is_dir()], [])
        self.assertEqual((rec["swept"]["removed"], len(rec["swept"]["problems"])), ([], 1), rec["swept"])
        self.assertIn("排他", rec["swept"]["problems"][0])

    def test_a_resume_waits_while_another_launcher_holds_its_place(self):
        """続きが使う置き場を、ほかの launcher が使っているか片付けている間は、続きを起動しない（待つように言う）。"""
        import fcntl
        rc, first = self.run_launch("codex", extra=["--timeout", "2"], env_extra={"FAKE_HANG": "1"})
        self.args_file.unlink(missing_ok=True)
        other = os.open(first["scratch"], os.O_RDONLY)  # 置き場の排他は、置き場のディレクトリそのものの flock
        self.addCleanup(lambda: os.close(other))
        fcntl.flock(other, fcntl.LOCK_EX)
        p = self.run_launch(None, extra=["--resume", first["out"]], raw=True)
        fcntl.flock(other, fcntl.LOCK_UN)
        self.assertEqual(p.returncode, 2, p.stdout)
        self.assertIn("少し待って", p.stderr)
        self.assertFalse(self.args_file.exists())
        self.assertTrue(Path(first["scratch"]).is_dir())
        rc, again = self.run_launch(None, extra=["--resume", first["out"]])  # 放された後は続けられる
        self.assertEqual(again["scratch"], first["scratch"], again)

    def test_a_place_that_cannot_be_removed_does_not_stop_the_launch(self):
        """片付けの失敗は、子の起動を失敗にしない。理由と、そこまでに消した場所が記録に出る。失敗を起こす時機を決められないので、
        launcher の関数を名前で差し替える（remove_tree・leftover_uses の名前を変えたら、ここも直す）。"""
        old = time.time() - 8 * 86400
        for word, patch, left in (
                ("片付けられない", "def no(p):\n    raise PermissionError(13, '消せない', str(p))\nlaunch.remove_tree = no", 2),
                # 1 つ目を消した後に、想定外の例外（OSError でない）が出る
                ("片付けが失敗した", "seen = []\nreal = launch.leftover_uses\ndef boom(base):\n    seen.append(1)\n    if len(seen) > 2:\n"
                                      "        raise RuntimeError('想定外')\n    return real(base)\nlaunch.leftover_uses = boom", 1)):
            with self.subTest(word):
                places = [self.leftover("tmp", self.stamp(9, f"-{word}-{i}"), {"at": old}) for i in (1, 2)]
                rc, rec = self.launch_patched(patch, ["--host", "claude", "--cwd", str(self.cwd)])
                self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
                self.assertEqual(len([p for p in places if p.is_dir()]), left)
                self.assertEqual(rec["swept"]["removed"], [str(p) for p in places if not p.is_dir()])
                self.assertIn(word, rec["swept"]["problems"][0])
                self.sweep_by_a_launch()  # 次の起動で、残りも片付く
                self.assertEqual([p for p in places if os.path.lexists(p)], [])

    def test_each_record_says_when_the_launch_ended(self):
        """片付けの期限の時計。記録と委任の記録の行の両方に、起動の終わりの時刻（at）がある。"""
        before = time.time()
        rc, rec = self.run_launch("claude")
        self.assertTrue(before - 0.01 <= rec["at"] <= time.time() + 0.01, rec["at"])
        self.assertEqual(json.loads((self.state / "delegations.jsonl").read_text().splitlines()[-1])["at"], rec["at"])
        self.assertEqual(json.loads((Path(rec["out"]) / "record.json").read_text())["at"], rec["at"])

    def test_a_launch_with_nothing_to_remove_makes_nothing_for_the_sweep(self):
        kept = self.leftover("tmp", self.stamp(1, "-recent"), {"at": time.time() - 86400})
        rec = self.sweep_by_a_launch()
        self.assertNotIn("swept", rec)
        # 状態の置き場に増えるのは、この起動の記録と報告だけ（片付けのための排他のファイルなどを作らない）。専用ログインの組織の覚え
        # （claude-login.json）は、片付けでなく確かめが書くので除く
        self.assertEqual(sorted(x.name for x in self.state.iterdir() if x.name != "claude-login.json"), ["delegations", "delegations.jsonl"])
        self.assertEqual(sorted(x.name for x in kept.parent.iterdir()), ["tmp"])
        self.assertEqual(sorted(x.name for x in Path(rec["out"]).iterdir() if x.name != "launcher-error.log"),
                         sorted(x.name for x in Path(rec["out"]).iterdir()))

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
        rc, rec = self.run_launch("claude", extra=["--write"], cwd=wt)  # 書く子は審査に数えない（書く子の後は head を確かめず、null）
        self.assertEqual((rec["status"], rec["head"], bool(rec["saw"])), ("ok", None, True), rec)
        self.assertNotIn("next", rec)
        body = {"conclusion": "c", "evidence": [{"what": "読んだ", "where": "a.txt:1"}], "unknowns": []}  # 実行の欄が無い報告
        rc, rec = self.run_launch("claude", cwd=wt, env_extra={"FAKE_TEXT": "```json\n" + json.dumps(body) + "\n```"})
        self.assertEqual((rec["status"], bool(rec["head"])), ("not-run", True), rec)
        self.assertNotIn("next", rec)
        body["execution"] = {"completed": "no", "commands": [{"command": "./test/run.sh", "exit": 1}]}  # 実行できなかったと書いた子には出口を出す（#39）
        rc, rec = self.run_launch("claude", cwd=wt, env_extra={"FAKE_TEXT": "```json\n" + json.dumps(body) + "\n```"})
        self.assertEqual((rec["status"], rec["execution"], bool(rec["head"])), ("not-run", "no", True), rec)
        self.assertIn("--not-run", rec["next"])
        self.assertIn("関門の外で出さない", rec["next"])
        (wt / "dirty.txt").write_text("x")  # 未 commit の変更があると head が無く、関門に数えないので出口も出さない
        rc, rec = self.run_launch("claude", cwd=wt, env_extra={"FAKE_TEXT": "```json\n" + json.dumps(body) + "\n```"})
        self.assertEqual((rec["status"], rec["head"]), ("not-run", None), rec)
        self.assertNotIn("next", rec)
        (wt / "dirty.txt").unlink()
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

    def clean_worktree(self, name):
        """起動の前にきれいな作業場所（記録に head が付き、終わりの clean_head が走る道を通す）。"""
        repo, wt = self.worktree(name)
        (repo / ".git" / "info").mkdir(exist_ok=True)
        (repo / ".git" / "info" / "exclude").write_text("a.txt\n")  # 偽の子の報告が証拠に指すファイル
        return repo, wt

    def test_a_write_child_planting_git_commands_does_not_get_them_run(self):
        # 書く子が、git status・ls-files が走らせる命令を仕込んでも、launcher は走らせない（#64。git 2.43 で測った出どころごと）
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        touch = "#!/bin/sh\ntouch @ARGS@.pwned\n"  # fsmonitor・hooks（入力を読まない）。filter は守らない（QUIET_GIT の注釈）
        fsmonitor = "[core]\n\tfsmonitor = touch @ARGS@.pwned\n"
        cases = {  # 段: (前からある利用者の設定 [(鍵, 値)]、無視させる名前、子が書くファイル、中身、子が書き換える追跡するファイル)
            "config": ([], None, None, None, "a.txt"),  # 偽の子の tamper-config が共通の config に fsmonitor と filter を足す（照合が止める）
            "include": ([("include.path", "@WT@/.gitcfg")], ".gitcfg", "@WT@/.gitcfg", fsmonitor, None),
            "worktree-config": ([("extensions.worktreeConfig", "true")], None, "@OWN@/config.worktree", fsmonitor, None),
            "script": ([("core.fsmonitor", "./fsmon.sh")], "fsmon.sh", "@WT@/fsmon.sh", touch, None),
            "hooks": ([("core.hooksPath", ".hk")], ".hk/", "@WT@/.hk/post-index-change", touch, "a.txt"),
            "submodule": ([], None, None, None, None),  # 偽の子の tamper-submodule-filter が入れ子のリポジトリを gitlink にする
        }
        for case, (settings, ignore, place, text, modify) in cases.items():
            with self.subTest(case=case):
                repo, wt = self.clean_worktree("plant-" + case)
                own = Path(subprocess.run(["git", "-C", str(wt), "rev-parse", "--absolute-git-dir"], capture_output=True, text=True,
                                          check=True).stdout.strip())
                if case in ("hooks", "config"):  # 追跡するファイルの更新時刻を進め、status に index の書き直しと filter を通させる
                    subprocess.run(git + ["-C", str(wt), "add", "-f", "a.txt"], check=True)
                    subprocess.run(git + ["-C", str(wt), "commit", "-q", "-m", "a"], check=True)
                if ignore:
                    with open(repo / ".git" / "info" / "exclude", "a") as f:
                        f.write(ignore + "\n")
                for key, value in settings:
                    subprocess.run(["git", "-C", str(repo), "config", key, value.replace("@WT@", str(wt))], check=True)
                if case == "include":
                    (wt / ".gitcfg").write_text("")
                env = {"FAKE_LEAVE_EMPTY": "1"}  # cwd_residue の ls-files も通す
                if place:
                    env.update(FAKE_APPEND_FILE=place.replace("@WT@", str(wt)).replace("@OWN@", str(own)), FAKE_APPEND_TEXT=text)
                if modify:
                    env["FAKE_MODIFY"] = modify
                Path(str(self.args_file) + ".pwned").unlink(missing_ok=True)
                mode = {"config": "tamper-config", "submodule": "tamper-submodule-filter"}.get(case, "good")
                rc, rec = self.run_launch("claude", mode, extra=["--write"], cwd=wt, env_extra=env)
                self.assertIsNotNone(rec["saw"], rec)  # 起動の前はきれいな作業ツリー（起動の前の clean_head が走る）
                self.assertIsNone(rec["head"], rec)  # 書く子の後は確かめない
                self.assertFalse(Path(str(self.args_file) + ".pwned").exists(), rec)
                if case in ("config", "worktree-config"):  # 共通の config・config.worktree の変化は照合が止め、片付けの git も動かさない
                    self.assertEqual((rec["status"], "git" in rec["blocked"]), ("failed", True), rec)
                else:  # 止めない段は、書く子の後の作業場所で審査役を立てる（起動の前の clean_head）
                    rc, review = self.run_launch("claude", cwd=wt)
                    self.assertFalse(Path(str(self.args_file) + ".pwned").exists(), (case, review))
                # 仕込みが本物であることの確かめ: 素の git status は、その命令を走らせる
                subprocess.run(["git", "-C", str(wt), "status", "--porcelain"], capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
                self.assertTrue(Path(str(self.args_file) + ".pwned").exists(), (case, rec))

    def test_a_required_filter_still_lets_the_review_head_be_recorded(self):
        # filter を外すと、LFS などの required の filter のあるリポジトリで status が壊れ、審査役の head が付かなかった（3 周目の審査）
        repo, wt = self.clean_worktree("required-filter")
        tools = Path(self.tmp.name) / "filter-tools"  # 作業場所の外の、利用者の道具
        tools.mkdir()
        (tools / "clean").write_text("#!/bin/sh\nsed 's/^SMUDGED //'\n")
        (tools / "smudge").write_text("#!/bin/sh\nsed 's/^/SMUDGED /'\n")
        for f in ("clean", "smudge"):
            (tools / f).chmod(0o755)
        for key, value in (("filter.p.clean", str(tools / "clean")), ("filter.p.smudge", str(tools / "smudge")), ("filter.p.required", "true")):
            subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
        (wt / ".gitattributes").write_text("big.bin filter=p\n")
        (wt / "big.bin").write_text("SMUDGED data\n")
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(wt)]
        subprocess.run(git + ["add", ".gitattributes", "big.bin"], check=True)
        subprocess.run(git + ["commit", "-q", "-m", "p"], check=True)
        os.utime(wt / "big.bin", (time.time() + 5, time.time() + 5))  # 中身を比べさせる（filter を通す）
        rc, rec = self.run_launch("claude", cwd=wt)
        head = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual((rec["status"], rec["head"]), ("ok", head), rec)

    def test_a_write_child_making_a_repository_in_a_target_outside_git_runs_no_git_after_it(self):
        # Git でない対象で子が git init して設定に命令を置いても、cwd_residue の ls-files は走らせない（1 周目の条件 (5) の子）。
        # 作ったリポジトリの設定（外さない filter を含む）は次の起動の clean_head が走らせうるので、照合の変化として止める（4 周目 A）
        Path(str(self.args_file) + ".pwned").unlink(missing_ok=True)
        rc, rec = self.run_launch("claude", "git-init-tamper", extra=["--write"], env_extra={"FAKE_LEAVE_EMPTY": "1"})
        self.assertTrue((self.cwd / ".git").is_dir(), rec)  # 偽の子が git init できた
        self.assertFalse(Path(str(self.args_file) + ".pwned").exists(), rec)
        self.assertIn(str(self.cwd.resolve() / "left-empty"), rec["cwd_residue"], rec)
        self.assertEqual((rec["status"], "git" in rec["blocked"]), ("failed", True), rec)
        self.assertIn(str(self.cwd.resolve() / ".git"), rec["problems"], rec)
        shutil.rmtree(self.cwd / ".git")
        (self.cwd / "left-empty").unlink()
        rc, rec = self.run_launch("claude", extra=["--write"])  # リポジトリを作らない書く子は止めない
        self.assertNotIn("git", rec["blocked"], rec)

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
            self.assertFalse(Path(str(self.args_file) + ".pwned").exists(), mode)  # 止めた後の片付けも、子が足した設定で git を動かさない
            if mode == "tamper-main":  # 動いた ref の前後の値と、戻すコマンドを示す
                self.assertTrue(any(x.startswith("refs/heads/main: ") and "update-ref refs/heads/main " in x for x in rec["problems"]), rec)
        _, wt = self.worktree("tamper-submodule")
        rc, rec = self.run_launch("claude", "tamper-submodule", extra=["--write"], cwd=wt)
        index = subprocess.run(["git", "ls-files", "-s", "nested"], cwd=wt, capture_output=True, text=True).stdout
        self.assertTrue(index.startswith("160000 "), (index, rec))  # 偽の子が入れ子のリポジトリをサブモジュールとして index に足せた
        self.assertFalse(Path(str(self.args_file) + ".pwned").exists(), rec)  # 片付けの git はサブモジュールの中で動かない

    def test_codex_write_in_worktree_gets_only_commit_dirs(self):
        repo, wt = self.worktree("cx")
        self.run_launch("codex", extra=["--write"], cwd=wt)
        argv = json.loads(self.args_file.read_text())
        dirs = [argv[i + 1] for i, x in enumerate(argv) if x == "--add-dir"]
        common = (repo / ".git").resolve()
        self.assertEqual(sorted(dirs), sorted([self.record_of(wt), str(common / "objects"), str(common / "refs" / "heads" / "bb-writ"),
                                               str(common / "logs" / "refs" / "heads" / "bb-writ"), str(common / "worktrees" / "cx-wt")]))

    def test_claude_read_only_denies_the_whole_home_and_lets_the_tools_back(self):
        # 認証情報の一覧だけを拒むと、一覧に無いホーム直下のファイル（.claude.json・.env.keys など）が読め、ファイル単位の拒否は
        # 置き換えで外れた。ホームを丸ごと拒み、道具の置き場と対象を戻す（#38。2.1.327 の実物の子で確かめた形）
        home = self.home.resolve()
        self.home = home
        state = home / ".local" / "state" / "bb-writ"  # 実物の既定の置き場。task start の作業場所はこの中にできる
        target = state / "worktrees" / "t" / "x"
        for d in ("conf", ".nvm/versions/node/v1/bin", ".nvm/versions/node/v1/lib", ".cargo/registry", ".local/share/keyrings", ".local/share/uv",
                  ".config/gh", ".config/git", ".ssh", ".m2", ".nuget/NuGet", "go-install/go/bin", "bin", "Projects/other", "elsewhere"):
            (home / d).mkdir(parents=True)
        target.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(target)], check=True)
        (target / "a.txt").write_text("x\n")  # 偽の子の報告が引く証拠
        for f, body in ((".env.keys", "K=1"), (".claude.json", "{}"), (".bash_history", "x"), (".npmrc", "//r/:_authToken=x"),
                        (".cargo/credentials.toml", "token"), (".gitconfig", "[include]\n\tpath = .gitconfig-extra\n\tpath = ~/.config/git/../gh/hosts.yml\n\tpath = ~/.ssh/git-include\n\tpath = conf/a\n"),
                        (".config/gh/hosts.yml", "t"), (".ssh/git-include", ""), ("conf/a", "[include]\n\tpath = b\n"), ("conf/b", ""), (".gitconfig-extra", ""),
                        (".m2/settings.xml", "<settings><servers><server><password>p</password></server></servers></settings>"),
                        (".nuget/NuGet/NuGet.Config", "<configuration><packageSources/></configuration>")):  # 印の無い XML は拒まない
            (home / f).write_text(body)
        (home / ".secret-link").symlink_to(home / ".ssh")  # 名前が一覧に無くても、行き先が認証情報の置き場なら戻さない
        (home / ".share-link").symlink_to(home / ".local" / "share")  # 行き先が認証情報の置き場を含む場所でも戻さない（中の keyrings を拒めない）
        for d in (".azure", ".gemini", ".copilot", ".pki", ".keychain", ".mozilla", ".terraform.d", ".pulumi", ".gradle", ".bundle"):
            (home / d).mkdir()  # 一覧に足したほかの CLI・クラウドのログインと鍵と、パッケージ・道具の認証情報の入れ物
        for f in (".terraform.d/credentials.tfrc.json", ".pulumi/credentials.json", ".gradle/gradle.properties", ".bundle/config",
                  ".config/git/credentials"):
            (home / f).write_text("x")
        (home / ".nuget" / "NuGet" / "Other.Config").write_text("")
        (home / ".cargo" / "credentials").symlink_to(home / "gone-credentials")  # 壊れた symlink の認証情報の置き場は、無い道筋を拒みに渡さない
        (home / ".tools-link").symlink_to(home / "elsewhere")  # symlink を通る道筋は、行き先がふつうの場所でも戻さない（行き先で判定すると穴が続いた）
        path = f"{home}/.nvm/versions/node/v1/bin:{home}/go-install/go/bin:{home}/bin:{self.bin}:/usr/bin:/bin"
        rc, rec = self.run_launch("claude", cwd=target, path=path,
                                  env_extra={"BB_WRIT_STATE_DIR": str(state), "GIT_CONFIG_GLOBAL": str(home / ".gitconfig")})
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        argv = json.loads(self.args_file.read_text())
        fs = json.loads(argv[argv.index("--settings") + 1])["sandbox"]["filesystem"]
        self.assertIn(str(home), fs["denyRead"])
        for p in (".nvm/versions/node/v1/lib", ".cargo/registry", ".local/share/uv", "go-install/go", "bin", ".gitconfig", ".gitconfig-extra",
                  ".config/git", ".nuget/NuGet/NuGet.Config", "conf/a", "conf/b"):  # 相対の include は、それを書いたファイルの置き場から
            self.assertTrue(claude_readable(home / p, fs), p)  # 道具の置き場（bin の隣）・Git の設定とその include・印の無い XML
        self.assertTrue(claude_readable(target, fs))  # 拒んだ状態の置き場の中の対象
        for p in (".env.keys", ".claude.json", ".bash_history", ".npmrc", ".cargo/credentials.toml", ".local/share/keyrings", ".config/gh",
                  ".ssh", ".m2/settings.xml", ".secret-link", ".share-link/keyrings", "Projects/other", ".local/state/bb-writ/delegations.jsonl",
                  "go-install", ".azure", ".gemini", ".copilot", ".pki", ".keychain", ".mozilla", ".terraform.d/credentials.tfrc.json",
                  ".pulumi/credentials.json", ".gradle/gradle.properties", ".bundle/config", ".config/git/credentials", ".tools-link", "elsewhere",
                  ".config/gh/hosts.yml", ".ssh/git-include"):  # 認証情報の置き場の中を指す include は戻さない
            self.assertFalse(claude_readable(home / p, fs), p)  # 一覧に無いホーム直下のファイル・戻した中の認証情報・ほかのリポジトリ
        self.assertFalse(claude_readable(self.cfg, fs))  # 専用ログインの置き場（ホームの外に置いても）
        self.assertEqual([d for d in fs["denyRead"] if not os.path.exists(d)], [])  # 在る（壊れた symlink でない）ものだけ
        self.assertFalse(any(p == str(home) for p in fs["allowRead"]))  # bin の親がホームなら bin だけ

    def test_claude_read_only_refuses_the_home_or_a_credential_place_as_target_and_denies_the_real_home(self):
        # 対象を戻すと、ホームか認証情報の置き場を丸ごと戻すことになる（ホームや ~/.config を Git で管理する配置）
        home = self.home.resolve()
        (home / "a.txt").write_text("x\n")
        (home / ".config" / "gh").mkdir(parents=True)
        for cwd, why in ((home, "ホームそのもの"), (home.parent, "ホームそのもの"), (home / ".config", "認証情報の置き場そのもの")):
            with self.subTest(cwd=cwd):
                rc, rec = self.run_launch("claude", cwd=cwd, env_extra={"HOME": str(home)})
                self.assertEqual(rec["status"], "unavailable", rec)
                self.assertIn(why, rec["problems"][0])
        link = Path(self.tmp.name) / "home-link"  # HOME が symlink の道筋なら、実の道筋も読み書きを拒む
        link.symlink_to(home)
        rc, rec = self.run_launch("claude", env_extra={"HOME": str(link)})
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        argv = json.loads(self.args_file.read_text())
        fs = json.loads(argv[argv.index("--settings") + 1])["sandbox"]["filesystem"]
        self.assertTrue({str(link), str(home)} <= set(fs["denyRead"]) & set(fs["denyWrite"]), fs)

    def test_read_only_children_without_a_home_to_deny_do_not_start(self):
        # HOME が無いか / だと、ホームの置き場の拒否が 1 つも出ない（黙って開く）。Codex は前から止めていた
        for host, home in (("claude", "/"), ("claude", ""), ("grok", "/"), ("grok", ""), ("codex", "/"), ("codex", "")):
            with self.subTest(host=host, home=home):
                rc, rec = self.run_launch(host, cwd=self.grok_target() if host == "grok" else None, env_extra={"HOME": home})
                self.assertEqual(rec["status"], "unavailable", rec)
                self.assertIn("HOME", rec["problems"][0])

    def test_claude_read_only_lets_back_only_plain_paths_and_denies_where_credentials_lead(self):
        # 戻す場所を道筋の綴りで決めると、認証情報の置き場への symlink・..・dotfiles の配置で、認証情報が読めた（#38 の 1 周目の審査）。
        # 行き先で判定して戻す形にも穴が続いたので、symlink も .. も通らない道筋だけを戻し、認証情報の置き場の行き先は拒む（2 周目）。
        # 置き場の中を辿って拒む形は、対象や道具を読めなくし起動を止めたので外した（6 周目）
        home = self.home.resolve()
        self.home = home
        outside = Path(self.tmp.name).resolve() / "aws-outside"
        for d in (".dotfiles/config/gh", ".dotfiles/ssh", "bin", "work/bin"):
            (home / d).mkdir(parents=True)
        outside.mkdir()
        (home / ".config").symlink_to(home / ".dotfiles" / "config")  # dotfiles でディレクトリごと symlink にする配置
        (home / ".ssh").symlink_to(home / ".dotfiles" / "ssh")
        (home / ".aws").symlink_to(outside)  # ホームの外の置き場
        (home / ".kube").symlink_to(home / "gone")  # 行き先の無い置き場の symlink は、無い道筋を拒みに入れない
        (home / ".home-link").symlink_to(home)
        (home / ".gh-link").symlink_to(home / ".dotfiles" / "config" / "gh")  # 認証情報の置き場の中を指す
        cargo = Path(self.tmp.name).resolve() / "cargo-elsewhere"  # 親の symlink を通る認証情報（~/.cargo を別のディスクへ）
        cargo.mkdir()
        (cargo / "credentials.toml").write_text("t")
        (home / ".cargo").symlink_to(cargo)
        (home / "myssh").symlink_to(home / ".dotfiles" / "ssh")
        (home / "myssh-bin").mkdir()
        (home / ".dotfiles" / "gitconfig").write_text('[include]\n\tpath = ~/.gitconfig-a\n[includeIf "gitdir:~/My Work/"]\n\tpath = ~/.gitconfig-work\n'
                                                     '[include]\n\tpath = ~/.git-credentials\n')
        (home / ".gitconfig").symlink_to(home / ".dotfiles" / "gitconfig")
        (home / ".gitconfig-a").write_text("[include]\n\tpath = .gitconfig-nested\n")
        for f in (".gitconfig-work", ".gitconfig-nested"):
            (home / f).write_text("")
        (home / ".git-credentials").write_text("[credential]\n\thelper = store\n")  # 正当な Git の設定の形の認証情報のファイル
        (home / ".gitconfig-a").write_text("[include]\n\tpath = .gitconfig-nested\n[includeIf \"gitdir:/nonexistent/\"]\n\tpath = ~\n\tpath = ~/Projects\n")  # ホーム・ディレクトリを指す include（条件が合わないので git は開かない）
        state = home / ".local" / "state" / "bbw-real"
        state.mkdir(parents=True)
        (state / "delegations.jsonl").write_text("")
        (home / "state-link").symlink_to(state)  # 状態の置き場を symlink の道筋で渡す
        (home / "Projects" / "other").mkdir(parents=True)  # ディレクトリを指す include は、Git の設定として戻さない

        proj = home / ".dotfiles" / "proj"  # 戻すドットのディレクトリの中の対象。置き場の中の symlink が対象の中・対象そのものを指しても、読めて起動する
        (proj / "skills" / "x").mkdir(parents=True)
        (proj / "a.txt").write_text("x\n")
        (home / ".claude" / "skills").mkdir(parents=True)
        (home / ".claude" / "skills" / "x").symlink_to(proj / "skills" / "x")
        (home / ".claude" / "skills" / "all").symlink_to(proj)
        (proj / "current").symlink_to(".")  # 対象の中の相対の symlink

        (home / ".env.keys").write_text("k")
        (home / ".dotfiles" / "ssh" / "bin").mkdir()  # 実の綴りで、symlink の認証情報の置き場の実の道筋そのもの（bin の親）と、その中を指す PATH
        (home / ".dotfiles" / "ssh" / "tools").mkdir()
        path = (f"{home}/myssh/bin:{home}/bin/../.ssh:{home}/work/bin:{home}/work/..:{home}/.env.keys:{home}/.dotfiles/ssh/bin:"
                f"{home}/.dotfiles/ssh/tools:{self.bin}:/usr/bin:/bin")  # work/.. はホームそのもの。PATH のファイル
        rc, rec = self.run_launch("claude", path=path, cwd=proj, env_extra={"GIT_CONFIG_GLOBAL": str(home / ".gitconfig"), "BB_WRIT_STATE_DIR": str(home / "state-link")})
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        argv = json.loads(self.args_file.read_text())
        fs = json.loads(argv[argv.index("--settings") + 1])["sandbox"]["filesystem"]
        for p in (home / ".dotfiles" / "config" / "gh", home / ".dotfiles" / "ssh", home / "myssh", home / ".ssh", home / ".git-credentials",
                  outside, home / ".home-link" / ".ssh", home / ".gh-link", cargo / "credentials.toml", home / ".cargo",
                  state / "delegations.jsonl", home / "Projects" / "other", home / ".env.keys"):
            self.assertFalse(claude_readable(p, fs), p)  # 行き先が認証情報の置き場・その中・ホームの外の置き場・ホームそのもの
        for p in (home / ".dotfiles" / "gitconfig", home / ".gitconfig-a", home / ".gitconfig-nested", home / ".gitconfig-work",
                  home / "work", home / ".dotfiles"):
            self.assertTrue(claude_readable(p, fs), p)  # 入れ子の include・条件に空白のある includeIf（行き先は、ドットのディレクトリとして戻る）
        for p in (proj, proj / "a.txt", proj / "skills" / "x"):  # 対象は読める
            self.assertTrue(claude_readable(p, fs), p)
        self.assertFalse(set(fs["allowRead"]) & set(fs["denyRead"]), fs)  # 同じ道筋を許可と拒否に並べない（同じ深さの勝ち負けは確かめていない）
        self.assertTrue(all(os.path.lexists(d) for d in fs["denyRead"]), fs["denyRead"])  # 在るものだけ
        self.assertNotIn(str(home / ".gitconfig"), fs["allowRead"])  # symlink の ~/.gitconfig は戻さない（守らない範囲）
        secrets = [home / ".dotfiles" / "config", home / ".dotfiles" / "ssh", cargo, outside]
        for a in map(Path, fs["allowRead"]):  # 戻す道筋は綴りのままの実の道筋で、ホーム・その祖先・認証情報の置き場とその中でない（前方一致で見えない .. も見る）
            self.assertEqual(a.resolve(), a, a)
            self.assertFalse(a == home or a in home.parents or any(a == q or q in a.parents for q in secrets), a)
        self.assertNotIn(str(home / ".config" / "git"), fs["allowRead"])  # ~/.config が symlink なら、Git の XDG の設定は戻さない

    def test_the_state_place_is_denied_even_when_it_does_not_exist_before_the_launch(self):
        # --out を渡すと、子の読みの規則を組む時点で状態の置き場がまだ無いことがあり、拒みに入らなかった（8 周目の審査）
        home = self.home.resolve()
        self.home = home
        state = home / ".local" / "state" / "fresh"
        (home / ".local").mkdir(parents=True)
        rc, rec = self.run_launch("claude", extra=["--out", str(Path(self.tmp.name) / "out-fresh")], env_extra={"BB_WRIT_STATE_DIR": str(state)})
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        argv = json.loads(self.args_file.read_text())
        fs = json.loads(argv[argv.index("--settings") + 1])["sandbox"]["filesystem"]
        self.assertFalse(claude_readable(state / "delegations.jsonl", fs), fs)

    def test_the_state_place_exists_before_the_read_rules_and_a_bad_one_stops_the_launch(self):
        # 子の読みの規則を組む時点で状態の置き場が在ること（無い道筋は sandbox がホームに空の場所を作りうる）と、作れなければ
        # 子を起こさず引数の誤りで止まり、空の --out を残さないこと（収束後の審査）
        state = Path(self.tmp.name) / "state-fresh"
        patch = f"""
_reads = launch.claude_reads
def claude_reads(*a, **k):
    open({str(Path(self.tmp.name) / 'seen')!r}, 'w').write(str(launch.state_dir().is_dir()))
    return _reads(*a, **k)
launch.claude_reads = claude_reads
"""
        rc, rec = self.launch_patched(patch, ["--host", "claude", "--cwd", str(self.cwd), "--out", str(Path(self.tmp.name) / "out-a")],
                                      env_extra={"BB_WRIT_STATE_DIR": str(state)})
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        self.assertEqual((Path(self.tmp.name) / "seen").read_text(), "True")
        bad = Path(self.tmp.name) / "state-file"
        bad.write_text("")
        p = self.run_launch("claude", extra=["--out", str(Path(self.tmp.name) / "out-b")], env_extra={"BB_WRIT_STATE_DIR": str(bad)}, raw=True)
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("状態の置き場", p.stderr)
        self.assertFalse((Path(self.tmp.name) / "out-b").exists())

    def test_home_dotfiles_judges_symlinks_by_the_real_home(self):
        # HOME が symlink の道筋でも、行き先がホームの中のドットのファイルの symlink は拒む（Codex・Grok の子）
        from unittest import mock
        home = self.home.resolve()
        (home / "dotfiles").mkdir()
        (home / "dotfiles" / "npmrc").write_text("t")
        (home / ".npmrc").symlink_to(home / "dotfiles" / "npmrc")
        link = Path(self.tmp.name) / "home-link"
        link.symlink_to(home)
        with mock.patch.dict(os.environ, {"HOME": str(link), "GIT_CONFIG_GLOBAL": os.devnull}):
            self.assertIn(link / ".npmrc", load_launch().home_dotfiles())

    def test_claude_read_only_lets_back_the_common_git_dir_and_only_what_exists(self):
        # worktree が状態の置き場にあり、共通ディレクトリがホームの別の場所にある配置（task start の作業場所）。無い ~/.config/git は渡さない
        from unittest import mock
        home = self.home.resolve()
        common, wt = home / "src" / "repo" / ".git", home / ".local" / "state" / "x" / "wt"
        for d in (common, wt, home / "conf"):
            d.mkdir(parents=True)
        (home / "conf" / "global").write_text("")  # GIT_CONFIG_GLOBAL で付け替えた全体の設定
        launch = load_launch()
        (home / ".bbw").mkdir()  # 設定の置き場をホーム直下のドットのディレクトリに（symlink の道筋で）付け替える。戻すドットのディレクトリと同じ道筋にしない
        (home / ".bbw" / "tools" / "bin").mkdir(parents=True)  # 置き場の中の PATH の道具も戻さない
        (home / ".bb-writ").symlink_to(home / ".bbw")
        with mock.patch.dict(os.environ, {"HOME": str(home), "BB_WRIT_STATE_DIR": str(home / ".local" / "state" / "x"),
                                          "BB_WRIT_CONFIG_DIR": str(home / ".bb-writ"), "GIT_CONFIG_GLOBAL": str(home / "conf" / "global")}):
            fs = launch.claude_reads(wt, common, {"PATH": f"{home / '.bbw' / 'tools' / 'bin'}:/usr/bin"}, False)
            audit = launch.claude_reads(wt, common, {"PATH": "/usr/bin"}, True)
        self.assertTrue(claude_readable(home / "conf" / "global", fs))
        for p in (home / ".bb-writ", home / ".bbw", home / ".bbw" / "tools"):
            self.assertFalse(claude_readable(p, fs), p)
        self.assertFalse(set(fs["allowRead"]) & set(fs["denyRead"]), fs)
        skills = str(ROOT / "skills")
        self.assertEqual((skills in fs["allowRead"], skills in audit["allowRead"]), (False, True))  # 監査役にだけ bb-writ のスキルを戻す
        self.assertTrue(claude_readable(common, fs) and claude_readable(wt, fs), fs)
        self.assertFalse(claude_readable(home / "src" / "other", fs))
        self.assertNotIn(str(home / ".config" / "git"), fs["allowRead"])
        self.assertTrue(all(os.path.lexists(a) for a in fs["allowRead"]), fs)

    def test_claude_children_cannot_invoke_the_entry(self):
        """入口 `bb-writ:bb-writ` は、Claude Code では呼ぶたびに fork した指揮者（frontmatter の model）を立てる。launcher の子
        （監査役は入口の名前を渡されて本文を読みに行く）が呼ぶと、子の中にもう 1 人の指揮者が立つので、子の設定で拒む。
        本体 `bb-writ:conduct` とほかのスキルは呼べるまま（子の道具に Skill が残り、読み取り専用の子の許可の一覧にも Skill が残る）。
        見るのは launcher が渡す設定と道具の一覧だけ。設定が実物の CLI で効くことは、実物の子で確かめた（2026-11-20、2.1.328）。"""
        repo, wt = self.worktree()
        for extra in ([], ["--interrogate"], ["--audit"], ["--write"]):
            self.args_file.unlink(missing_ok=True)
            self.run_launch("claude", cwd=wt, extra=extra)
            argv = json.loads(self.args_file.read_text())
            permissions = json.loads(argv[argv.index("--settings") + 1]).get("permissions", {})
            self.assertEqual(permissions.get("deny"), ["Skill(bb-writ:bb-writ)"], extra)
            self.assertIn("Skill", argv[argv.index("--tools") + 1].split(","), extra)
            if "--write" not in extra:  # 書く子は --permission-mode auto で、許可の一覧を持たない
                self.assertIn("Skill", permissions.get("allow", []), extra)

    def test_a_prompt_that_starts_with_a_slash_is_not_run_as_a_command(self):
        """依頼文の先頭が「/bb-writ:bb-writ …」だと、claude -p はスラッシュの呼び出しとして扱い、子の中で入口が fork した
        （設定の拒否は Skill ツールにしか効かない。2026-11-20、2.1.328）。launcher は、Claude Code の子に渡す依頼文の先頭に 1 行置く。
        見るのは、子の CLI に渡る文がスラッシュで始まらず、依頼文がその後にそのまま続くことだけ。"""
        repo, wt = self.worktree()
        prompt = wt.parent / "slash-prompt.md"
        prompt.write_text("/bb-writ:bb-writ この場所を調べて\n", encoding="utf-8")
        for extra in ([], ["--write"]):
            self.args_file.unlink(missing_ok=True)
            self.run_launch("claude", cwd=wt, extra=[*extra, "--prompt-file", str(prompt)])
            argv = json.loads(self.args_file.read_text())
            given = argv[argv.index("-p") + 1]
            self.assertFalse(given.lstrip().startswith("/"), extra)  # 先頭の空白を除いても、スラッシュで始まらない
            self.assertIn("\n/bb-writ:bb-writ この場所を調べて\n", given, extra)

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
        fs = sandbox["filesystem"]
        self.assertIn(str(self.home), fs["denyRead"])  # ホームは丸ごと拒み、対象と共通ディレクトリを戻す（#38）
        self.assertTrue(claude_readable(wt, fs) and claude_readable((repo / ".git").resolve(), fs))
        self.assertIn(str(self.home), sandbox["filesystem"]["denyWrite"])
        self.assertEqual(Path(str(self.args_file) + ".scrub").read_text(), "")  # SCRUB は許可の方針を default に戻すので使わない

    def test_claude_auditor_reads_the_record_and_the_children_of_its_place_only(self):
        """監査役（返す前の判定）は、作業の記録・その場所で立てた子の依頼文と報告を読む。読み取り専用の子はホームの下の対象の外を
        読めず、監査役が記録を読めないまま差分だけで判定した。ほかの場所の記録と子の out は読ませない。"""
        repo, wt = self.worktree()
        # macOS の一時ディレクトリは /var → /private/var の symlink の下にある。launcher は読める場所を解いた道筋で決めるので、
        # 試験のホームも解いた道筋にする（実物のホーム /Users/<名前> は symlink でない）
        self.home = self.home.resolve()
        self.state = self.home / "state"  # 記録の置き場がホームの下にあるとき（実物の既定 ~/.local/state/bb-writ）
        mine, other = self.home / "outs" / "mine", self.home / "outs" / "other"
        for d in (mine, other):
            d.mkdir(parents=True)
        self.state.mkdir()
        with open(self.state / "delegations.jsonl", "w", encoding="utf-8") as f:
            f.write(json.dumps({"cwd": str(wt.resolve()), "out": str(mine)}) + "\n" + "{壊れた行\n"
                    + json.dumps({"cwd": str(self.cwd.resolve()), "out": str(other)}) + "\n")
        record = Path(self.record_of(wt))
        record.mkdir(parents=True, exist_ok=True)
        readable = claude_readable
        for audit in (False, True):
            with self.subTest(audit=audit):
                self.run_launch("claude", cwd=wt, extra=["--audit"] if audit else [], env_extra={"BB_WRIT_STATE_DIR": str(self.state)})
                argv = json.loads(self.args_file.read_text())
                reads = json.loads(argv[argv.index("--settings") + 1])["sandbox"]["filesystem"]
                self.assertEqual((readable(record, reads), readable(mine, reads)), (audit, audit))
                self.assertFalse(readable(other, reads))

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

    def test_read_only_codex_also_denies_home_dotfiles_and_package_credentials_but_writers_keep_them(self):
        # 一覧に無いホーム直下のドットのファイル（.npmrc・.bash_history・.env.keys）とパッケージの取得の認証情報は、読み取り専用の子に
        # 読ませない（#38・#43）。書く子は依存を取得する役なので、パッケージの認証情報は読ませたまま。書く子の denyRead は在るものだけ
        home = self.home.resolve()
        self.home = home
        (home / ".cargo").mkdir()
        (home / ".gem").mkdir()
        for f in (".npmrc", ".bash_history", ".env.keys", ".gitconfig", ".gitconfig-extra", ".cargo/credentials.toml", ".gem/credentials"):
            (home / f).write_text("x")
        (home / ".gitconfig").write_text("[include]\n\tpath = .gitconfig-extra\n")
        (home / ".ssh").mkdir()
        (home / ".nuget" / "NuGet").mkdir(parents=True)  # 印のある XML の設定は拒む（印は大小を問わない）
        (home / ".nuget" / "NuGet" / "NuGet.Config").write_text("<configuration><PackageSourceCredentials><x><add key='Username' value='u'/></x></PackageSourceCredentials></configuration>")
        for f in (".cargo/credentials", ".local/share/com.vercel.cli/auth.json"):
            (home / f).parent.mkdir(parents=True, exist_ok=True)
            (home / f).write_text("x")
        (home / ".bash_history_link").symlink_to("/dev/null")  # 行き先がホームの外の symlink は拒まない（/dev/null を拒むことになる）
        (home / "notes.txt").write_text("x")  # ドットで始まらないホーム直下のファイルは拒まない
        hosts = Path(self.tmp.name) / "hosts"  # ホームの外の通常のファイル（/etc/hosts の形）も拒まない
        hosts.write_text("127.0.0.1 localhost\n")
        (home / ".hosts").symlink_to(hosts)
        proj = home / "proj"  # ホームの中の対象（dotfiles のリポジトリを審査する）
        proj.mkdir()
        (proj / "a.txt").write_text("x\n")  # 偽の子の報告が引く証拠
        (proj / "bashrc").write_text("x")
        (home / ".bashrc").symlink_to(proj / "bashrc")  # 行き先は拒む（対象の中のそのファイルも子から読めない。守らない範囲）
        (home / "dotfiles").mkdir()
        (home / "dotfiles" / "npmrc").write_text("t")
        (home / ".npmrc").unlink()
        (home / ".npmrc").symlink_to(home / "dotfiles" / "npmrc")  # stow の配置。行き先を拒む
        (home / ".gitconfig").rename(home / ".gitconfig-real")
        (home / ".gitconfig").symlink_to(home / ".gitconfig-real")  # Git の設定が symlink なら、行き先も Git の設定として拒まない
        rc, rec = self.run_launch("codex", cwd=proj, env_extra={"GIT_CONFIG_GLOBAL": str(home / ".gitconfig")})
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        rules = fs_rules(json.loads(self.args_file.read_text()))
        for f in (".bash_history", ".env.keys", ".cargo/credentials.toml", ".gem/credentials", ".ssh", ".nuget/NuGet/NuGet.Config",
                  ".cargo/credentials", ".local/share/com.vercel.cli"):
            self.assertEqual(rules.get(str(home / f)), "deny", f)
        for f in (".gitconfig", ".gitconfig-extra", ".cargo"):  # Git の設定とその include、認証情報の入れ物そのものは読ませる
            self.assertNotIn(str(home / f), rules)
        self.assertNotIn("/dev/null", rules)
        self.assertNotIn(str(hosts.resolve()), rules)
        self.assertNotIn(str(home / "notes.txt"), rules)
        self.assertEqual((rules.get(str(proj / "bashrc")), rules.get(str(home / "dotfiles" / "npmrc"))), ("deny", "deny"), rules)
        self.assertNotIn(str(home / ".gitconfig-real"), rules)  # symlink の ~/.gitconfig の行き先は Git の設定として拒まない
        self.run_launch("claude", extra=["--write"])
        argv = json.loads(self.args_file.read_text())
        reads = json.loads(argv[argv.index("--settings") + 1])["sandbox"]["filesystem"]["denyRead"]
        self.assertEqual(sorted(reads), sorted([str(home / ".ssh"), str(self.cfg)]))  # 在るものだけ。.npmrc・.cargo の認証情報は書く子に読ませる
        self.assertEqual(json.loads(argv[argv.index("--settings") + 1])["sandbox"]["filesystem"]["allowRead"], [])  # 無い ~/.config/git は渡さない

    def test_grok_read_only_denies_the_credential_places_keeps_what_it_runs_and_gets_a_narrow_env(self):
        # 読み取り専用の Grok の子は、ホームの認証情報の置き場を読め、親の環境（SSH_AUTH_SOCK・トークン）がそのまま渡っていた（#43）。
        # ~/.config・~/.grok を丸ごと拒むと Grok が起動しないので、Grok 本体・その bin・Git の設定・専用ログインの auth.json・対象と
        # その共通ディレクトリを含む置き場は中を 1 つずつ拒む。無い場所は書かない（Grok は起動を拒まず、ホームに空のファイルを作って残した）。
        # Grok の read-only は /tmp に書けるので対象を /tmp の外に置く。残すものが認証情報の置き場の中にある配置にして、拒まないことを見る
        d = self.writable_dir(Path.home())
        self.addCleanup(d.cleanup)
        home = Path(d.name).resolve()
        self.home = home
        for p in (".ssh", ".config/gh", ".config/git", ".config/tools", ".grok/bin", ".grok/sessions", "elsewhere", ".nvm/bin"):
            (home / p).mkdir(parents=True)
        for f in (".grok/auth.json", ".bash_history", ".npmrc", ".env.keys", ".gitconfig", ".config/git/credentials"):
            (home / f).write_text("x")
        (home / ".aws").symlink_to(home / "elsewhere")
        tool = home / ".config" / "tools" / "grok"  # 本体が ~/.config の下にある配置（中は偽の CLI を呼ぶだけ）
        tool.write_text(f"#!/bin/sh\nexec {self.bin / 'grok'} \"$@\"\n")
        tool.chmod(0o755)
        repo, target = home / ".config" / "repo", home / ".config" / "wt"  # 対象と、その共通ディレクトリが ~/.config の下にある配置
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "i"], check=True)
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(target)], check=True)
        (target / "a.txt").write_text("x\n")  # 偽の子の報告が引く証拠
        path = f"{home / '.config' / 'tools'}:{self.bin}:/usr/bin:/bin"
        rc, rec = self.run_launch("grok", cwd=target, path=path,
                                  env_extra={"SSH_AUTH_SOCK": "/s", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/d", "MY_SERVICE_TOKEN": "x",
                                             "XDG_RUNTIME_DIR": "/run/user/1", "LANG": "C.UTF-8", "LC_SECRET_TOKEN": "x", "BB_WRIT_STATE_DIR": str(home / "state")})
        self.state = home / "state"
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        argv, env = self.grok_argv()
        profile = self.grok_profile_of(argv, env)
        self.assertIn("deny = ", profile)
        deny = json.loads(profile.split("deny = ", 1)[1].splitlines()[0])
        expect = {home / ".ssh", home / ".config" / "gh", home / ".config" / "git" / "credentials", home / ".grok" / "auth.json",
                  home / ".grok" / "sessions", home / ".bash_history", home / ".npmrc", home / ".env.keys", home / "elsewhere",
                  self.cfg.resolve() / ".env", self.cfg.resolve() / ".env.keys", self.cfg.resolve() / "codex-home"}
        self.assertTrue({str(p) for p in expect} <= set(deny), deny)
        for kept in (home / ".config" / "git", home / ".config" / "tools" / "grok", home / ".grok" / "bin", home / ".gitconfig",
                     self.cfg.resolve() / "grok-auth" / "auth.json", target, repo / ".git", home / ".nvm", home):
            self.assertFalse(any(dd == str(kept) or kept.is_relative_to(dd) for dd in deny), kept)  # 残すものとそれを含む置き場は拒まない
        self.assertTrue(all(os.path.lexists(dd) for dd in deny), deny)  # 在るものだけ
        passed = set(json.loads(Path(str(self.args_file) + ".allenv").read_text()))
        self.assertFalse({"SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS", "MY_SERVICE_TOKEN", "XDG_RUNTIME_DIR", "LC_SECRET_TOKEN"} & passed, passed)
        self.assertTrue({"PATH", "HOME", "LANG", "GROK_HOME"} <= passed, passed)
        rc, rec = self.run_launch("grok", extra=["--write"], cwd=target, path=path,
                                  env_extra={"SSH_AUTH_SOCK": "/s", "BB_WRIT_STATE_DIR": str(home / "state")})  # 書く子は変えない（別に扱う）
        self.assertNotIn("deny = ", self.grok_profile_of(*self.grok_argv()))
        self.assertIn("SSH_AUTH_SOCK", set(json.loads(Path(str(self.args_file) + ".allenv").read_text())))

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

    def test_grok_write_grants_only_the_work_record_and_the_commit_dirs(self):
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
        env = self.launch_env()
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

    def test_quota_is_reported_only_when_the_child_did_not_succeed(self):
        rc, rec = self.run_launch("codex", "quota")
        self.assertEqual(rec["status"], "quota")
        # 結果が失敗（is_error）なら、終了コード 0 で本文があっても成功ではない（利用枠の文言を照らす）
        rc, rec = self.run_launch("claude", env_extra={"FAKE_EXIT": "0", "FAKE_ERROR": "API Error: 429 rate_limit_error"})
        self.assertEqual(rec["status"], "quota", rec)
        # 利用枠の文言を照らすのは、子が成功しなかったときだけ。成功した子の報告は、stderr の一語では捨てない
        for i, (line, extra, status) in enumerate((("WARN failed to refresh rate limits", {}, "ok"), ("request finished in 1429ms", {}, "ok"),
                                                   ("Reconnecting... 1/5 (unexpected status 429 Too Many Requests)", {}, "ok"),
                                                   ("stream error: last status: 429 Too Many Requests", {"FAKE_EXIT": "1"}, "quota"),
                                                   ("compile took 1429ms", {"FAKE_HANG": "1"}, "timeout"),  # 時間切れでも、時間の数字は利用枠でない
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

    def test_a_report_that_fails_the_check_is_fixed_once_in_the_same_session(self):
        """照合に落ちた報告（証拠の where が対象からの相対でない・JSON ブロックが無い）は、launcher が同じ session の続きで 1 回だけ、
        落ちた点と launcher の読み方を名指しして直させる。直れば ok、直らなければ（存在しないファイルを引き続けるなど）invalid-report で、
        問題には 2 回分が並ぶ。直させたことは記録の report_fix に残り、3 回目は起こさない。照合は緩めない。"""
        good = GOOD.encode().decode("unicode_escape")
        resumed = {"claude": lambda argv: argv[argv.index("--resume") + 1], "codex": lambda argv: argv[argv.index("resume") + 1],
                   "grok": lambda argv: argv[argv.index("-r") + 1]}
        prompt_of = {"claude": lambda argv: argv[argv.index("-p") + 1], "codex": lambda argv: argv[-1], "grok": lambda argv: argv[argv.index("-p") + 1]}
        for host in ("claude", "codex", "grok"):
            cwd = self.grok_target() if host == "grok" else self.cwd
            for first, problem in ((good.replace("a.txt:1", "other/a.txt:1"), "証拠のファイルが無い: other/a.txt"),
                                   ("結論だけ", "報告の JSON ブロックが無い")):
                for fixed in (True, False):
                    with self.subTest(host=host, problem=problem, fixed=fixed):
                        calls = Path(str(self.args_file) + ".calls")
                        calls.unlink(missing_ok=True)
                        rc, rec = self.run_launch(host, cwd=cwd, env_extra={"FAKE_TEXT": first, **({"FAKE_RESUMED_TEXT": good} if fixed else {})})
                        argvs = [json.loads(x) for x in calls.read_text().splitlines()]
                        self.assertEqual(len(argvs), 2, rec)  # 最初の起動と、直させる続きの 1 回だけ
                        self.assertEqual(resumed[host](argvs[1]), rec["session"], argvs[1])  # 同じ session の続き
                        asked = prompt_of[host](argvs[1])
                        self.assertIn(problem, asked)  # 落ちた点を名指しする
                        self.assertIn(f"対象 {cwd.resolve()} からの相対パス", asked)  # launcher がどう読んだか
                        self.assertIn("```json", asked)  # 報告の形の約束も
                        self.assertEqual(rec["report_fix"]["asked"], [problem], rec)
                        self.assertEqual((Path(rec["out"]) / "report-before-fix.md").read_text(encoding="utf-8"), first)
                        if fixed:
                            self.assertEqual((rc, rec["status"], rec["problems"]), (0, "ok", []), rec)
                        else:
                            self.assertEqual((rc, rec["status"]), (2, "invalid-report"), rec)
                            self.assertIn(problem, rec["problems"])  # 2 回目の問題と
                            self.assertTrue(any(problem in x and "1 回目" in x for x in rec["problems"]), rec)  # 1 回目の問題
        git = self.repo_git()
        git("init", "-q"); git("add", "a.txt"); git("commit", "-q", "-m", "a")
        rc, rec = self.run_launch("claude", env_extra={"FAKE_TEXT": "結論だけ", "FAKE_RESUMED_TEXT": good})
        self.assertEqual((rec["status"], rec["head"]), ("ok", git("rev-parse", "HEAD").strip()), rec)  # 直させた子も、見た commit の審査に数える

    def test_a_signal_after_deciding_to_start_another_child_is_not_lost(self):
        """報告を直させる続き（と Fable の立て直し）を起こすと決めた後に受けたシグナル。続きを組む間に受けたら、続きを起こさずに 1 回目の
        報告を返し、起こす間際に受けたら、起こした子に TERM を伝える。どちらも stopped_by_signal を残す（組む間に受けたものは捨てられて
        続きが期限まで動き、間際に受けたものは刈った子の番号へ送られて、続きの子は TERM を受けずに期限か STOP_GRACE の後に KILL された）。"""
        good = GOOD.encode().decode("unicode_escape")
        bad = good.replace("a.txt:1", "other/a.txt:1")
        calls = Path(str(self.args_file) + ".calls")
        with self.subTest("while building the fix"):
            calls.unlink(missing_ok=True)
            rc, rec = self.launch_patched("""
                real = launch.command
                def command(a, prompt, scratch):
                    if a.resume:  # 直させる続きを組む間
                        os.kill(os.getpid(), signal.SIGTERM)
                    return real(a, prompt, scratch)
                launch.command = command
                """, ["--host", "claude", "--cwd", str(self.cwd), "--timeout", "30"], env_extra={"FAKE_TEXT": bad, "FAKE_MODE_2": "stop"})
            self.assertEqual(len(calls.read_text().splitlines()), 1, rec)  # 続きは起こさない
            self.assertEqual((rc, rec["status"], rec.get("stopped_by_signal"), "report_fix" in rec), (2, "invalid-report", int(signal.SIGTERM), False), rec)
            self.assertEqual((Path(rec["out"]) / "report.md").read_text(encoding="utf-8"), bad)  # 1 回目の報告を返す
        with self.subTest("after reaping the first child, before building the fix"):
            calls.unlink(missing_ok=True)
            built = Path(str(self.args_file) + ".built")
            built.unlink(missing_ok=True)
            rc, rec = self.launch_patched(f"""
                real_check, real_command = launch.check_report, launch.command
                def check_report(*k, **kw):  # 1 回目の子を刈った後の照合の最中
                    os.kill(os.getpid(), signal.SIGTERM)
                    return real_check(*k, **kw)
                def command(a, prompt, scratch):
                    if a.resume:  # 続きを組んだ（Codex の排他の待ちや Grok の home の写しを含む）
                        open({str(built)!r}, "a").write("x")
                    return real_command(a, prompt, scratch)
                launch.check_report, launch.command = check_report, command
                """, ["--host", "claude", "--cwd", str(self.cwd), "--timeout", "30"], env_extra={"FAKE_TEXT": bad})
            self.assertFalse(built.exists(), rec)  # 続きを組まない
            self.assertEqual(len(calls.read_text().splitlines()), 1, rec)
            self.assertEqual((rc, rec["status"], rec.get("stopped_by_signal"), "report_fix" in rec), (2, "invalid-report", int(signal.SIGTERM), False), rec)
        for name, extra, env, started in (("fix", [], {"FAKE_TEXT": bad}, "report_fix"),
                                          ("fallback", ["--model", "fable", "--effort", "high"], {"FAKE_MODE_1": "fable-unavailable"}, "model_fallback")):
            with self.subTest(f"right before starting ({name})"):
                calls.unlink(missing_ok=True)
                t0 = time.monotonic()
                site = self.launcher_site("""
                    import signal, subprocess
                    real = subprocess.Popen
                    class Popen(real):
                        def __init__(self, args, *k, **kw):  # 2 回目の子の CLI を起こす間際（1 回目の子は .calls に書いた）
                            if kw.get("start_new_session") and "--settings" in args and os.path.exists(os.environ["FAKE_ARGS"] + ".calls"):
                                os.kill(os.getpid(), signal.SIGTERM)
                            super().__init__(args, *k, **kw)
                    subprocess.Popen = Popen
                """)
                rc, rec = self.run_launch("claude", extra=[*extra, "--timeout", "30"], env_extra={**env, "FAKE_MODE_2": "stop", **site})
                self.assertEqual((started in rec, rec.get("stopped_by_signal")), (True, int(signal.SIGTERM)), rec)  # 2 回目の子を起こした
                # その子が TERM を受けて終わった（終了コードは届いた時機で -15・143・241。伝えなければ KILL の -9）
                self.assertNotIn(rec["exit_code"], (0, -int(signal.SIGKILL)), rec)
                self.assertLess(time.monotonic() - t0, 15, rec)

    def test_a_fix_that_cannot_start_keeps_the_first_report(self):
        """続きを起こせない（依頼文に符号にできない文字がある、依頼文が引数に長すぎる）ときも、続きを組めないときと同じく、1 回目の報告を
        invalid-report のまま report.md に残して返す（failed で続けられない記録になり、長すぎるときは report.md も消えていた）。"""
        good = GOOD.encode().decode("unicode_escape")
        # Linux は 1 つの引数が MAX_ARG_STRLEN（ページの 32 倍）を、ほかは引数と環境の合計が ARG_MAX を超えると E2BIG
        size = os.sysconf("SC_PAGE_SIZE") * 32 if sys.platform.startswith("linux") else os.sysconf("SC_ARG_MAX")
        many = ", ".join('{"what": "w", "where": "%s/%d.txt:1"}' % ("d" * 200, i) for i in range(size // 200 + 1))
        calls = Path(str(self.args_file) + ".calls")
        for name, first in (("surrogate", good.replace("a.txt:1", "b\\ud83d.txt:1")),
                            ("too-long", good.replace('"where": "a.txt:1"}', '"where": "a.txt:1"}, ' + many))):
            with self.subTest(name):
                calls.unlink(missing_ok=True)
                env = self.launch_env(env_extra={"FAKE_RESUMED_TEXT": good})
                env.pop("FAKE_TEXT")  # 報告は偽の子にだけ渡す（大きな報告を launcher の環境に入れると、launcher の起動が E2BIG で落ちる）
                (self.bin / "fake_env.json").write_text(json.dumps({**json.loads((self.bin / "fake_env.json").read_text()), "FAKE_TEXT": first}))
                p = subprocess.run([sys.executable, str(LAUNCH), "--host", "claude", "--cwd", str(self.cwd), "--prompt-file", str(self.prompt)],
                                   env=env, capture_output=True, text=True, timeout=120)
                rec = json.loads(p.stdout)
                self.assertEqual(len(calls.read_text().splitlines()), 1, rec)
                self.assertEqual((p.returncode, rec["status"], rec["resumable"], rec["blocked"]), (2, "invalid-report", True, []), rec)
                self.assertTrue([x for x in rec["problems"] if x.startswith("launcher が続きで直させられない")], rec["problems"][-3:])
                self.assertEqual((Path(rec["out"]) / "report.md").read_text(encoding="utf-8"), first)
                self.assertNotIn("report_fix", rec)

    @unittest.skipUnless(sys.platform.startswith("linux"), "子の CLI が切り離した子孫を止めるのは Linux だけ（PR_SET_CHILD_SUBREAPER）")
    def test_a_fix_keeps_what_the_first_run_left(self):
        """直させる続きを起こしても、1 回目の子の生の出力（stdout-before-fix.jsonl・stderr-before-fix.log）と、1 回目の子の後に止めた
        子孫（stopped_after_exit）を残す（続きの出力で上書きし、止めた子孫の一覧も続きの分だけになっていた）。"""
        good = GOOD.encode().decode("unicode_escape")
        rc, rec = self.run_launch("claude", env_extra={"FAKE_TEXT": good.replace("a.txt:1", "other/a.txt:1"), "FAKE_RESUMED_TEXT": good,
                                                       "FAKE_MODE_1": "orphan", "FAKE_SLEEP": shutil.which("sleep")})
        pids = self.orphans()
        out = Path(rec["out"])
        self.assertEqual((rec["status"], len(pids)), ("ok", 7), rec)
        with self.subTest("stopped_after_exit"):
            self.assertEqual(sorted(int(p.split()[0]) for p in rec["stopped_after_exit"]), sorted(pids), rec)
        with self.subTest("raw output"):
            self.assertTrue((out / "stdout-before-fix.jsonl").is_file() and (out / "stderr-before-fix.log").is_file(), sorted(out.iterdir()))
            self.assertIn("other/a.txt", (out / "stdout-before-fix.jsonl").read_text())  # 1 回目の子の出力
            self.assertNotIn("other/a.txt", (out / "stdout.jsonl").read_text())  # 続きの子の出力

    def test_a_report_is_not_fixed_without_time_left(self):
        """--timeout の残りが少なければ、直させずに 1 回目の報告を返す（続きが時間切れになって invalid-report を timeout に変え、TERM を
        受けない子では --timeout を STOP_GRACE まで越えた）。"""
        good = GOOD.encode().decode("unicode_escape")
        bad = good.replace("a.txt:1", "other/a.txt:1")
        calls = Path(str(self.args_file) + ".calls")
        calls.unlink(missing_ok=True)
        rc, rec = self.run_launch("claude", extra=["--timeout", "5"], env_extra={"FAKE_TEXT": bad, "FAKE_RESUMED_TEXT": good})
        self.assertEqual(len(calls.read_text().splitlines()), 1, rec)
        self.assertEqual((rc, rec["status"], "report_fix" in rec), (2, "invalid-report", False), rec)
        self.assertEqual((Path(rec["out"]) / "report.md").read_text(encoding="utf-8"), bad)

    def test_a_fix_that_fails_does_not_show_the_first_report_as_current(self):
        """直させる続きが失敗しても、記録は 1 回目の報告（report・execution）を今の報告として見せない（report.md は消えたのに、記録には
        1 回目の報告と実行の欄が残っていた）。1 回目の報告は report-before-fix.md と report_fix に残る。"""
        bad = GOOD.encode().decode("unicode_escape").replace("a.txt:1", "other/a.txt:1")
        rc, rec = self.run_launch("claude", env_extra={"FAKE_TEXT": bad, "FAKE_EXIT_2": "1", "FAKE_ERROR_2": "boom"})
        out = Path(rec["out"])
        self.assertEqual((rc, rec["status"], rec["report_fix"]["asked"]), (2, "failed", ["証拠のファイルが無い: other/a.txt"]), rec)
        self.assertEqual([k for k in ("report", "execution", "deliver") if k in rec], [], rec)
        self.assertFalse((out / "report.md").exists())
        self.assertEqual((out / "report-before-fix.md").read_text(encoding="utf-8"), bad)

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
        self.assertIn("専用ログインの鍵が無い", rec["problems"][0])
        (self.cfg / "grok-auth" / "auth.json").unlink()
        rc, rec = self.run_launch("grok", cwd=self.grok_target())
        self.assertEqual(rec["status"], "unavailable")
        self.assertIn("GROK_HOME=", rec["problems"][0])
        self.assertIn("README の「任意機能の準備」の「Grok Build の専用ログイン」", rec["problems"][0])
        (self.cfg / "codex-home" / "auth.json").unlink()
        rc, rec = self.run_launch("codex")
        self.assertEqual(rec["status"], "unavailable")
        self.assertIn("CODEX_HOME=", rec["problems"][0])
        self.assertIn("README の「任意機能の準備」の「Codex の専用ログイン」", rec["problems"][0])

    def test_a_missing_claude_login_says_how_to_set_it_up(self):
        # 無いのが .env か、鍵の .env.keys だけかで、次にすることを示す。鍵だけ無いのは、たとえば dotenvx 2.25 以降が --no-native 無しで
        # 鍵を OS の secret store に置いたとき（macOS など）。守らない範囲: 文を固定しているので、意味を変えない言い換えでも落ちる
        env, keys = self.cfg / ".env", self.cfg / ".env.keys"
        setup = f"dotenvx set CLAUDE_CODE_OAUTH_TOKEN <トークン> --no-native -f {env} -fk {keys}"
        section = "README の「任意機能の準備」の「Claude Code の専用ログイン」"
        keys.unlink()
        rc, rec = self.run_launch("claude")
        self.assertEqual(rec["status"], "unavailable")
        why = rec["problems"][0]
        self.assertIn(f"専用ログインの鍵が無い: {env} はあるが {keys} が無い", why)
        self.assertIn("dotenvx 2.25 以降は、--no-native を付けないと鍵を OS の secret store", why)
        self.assertIn(setup, why)
        self.assertIn(section, why)
        env.unlink()
        rc, rec = self.run_launch("claude")
        self.assertEqual(rec["status"], "unavailable")
        why = rec["problems"][0]
        self.assertIn(f"専用ログインが無い: {env} が無い", why)
        self.assertNotIn("secret store", why)
        self.assertIn(f"claude setup-token のトークンを、{setup}", why)
        self.assertIn(section, why)

    # 専用ログインの組織の確かめ（launcher の claude_login）。偽の値は実在しない形（英字を含め、大文字にすると別の文字列になる）
    LOGIN_ORG = "1a2b3c4d-1111-4111-8111-aaaaaaaaaaaa"  # 偽の claude auth status の orgId の既定
    TOKEN_ORG = "5e6f7a8b-3333-4333-8333-bbbbbbbbbbbb"
    NO_ORG = "launcher を呼んだ環境の Claude Code のログインに、組織（orgId）が無いか、uuid の形でない"

    def login_calls(self, kind: str) -> list:
        """偽の claude・dotenvx が FAKE_ARGS.login に残した、確かめの呼び出し（kind は auth・probe・dotenvx）。"""
        f = Path(str(self.args_file) + ".login")
        return [c for c in map(json.loads, f.read_text().splitlines() if f.exists() else []) if c["kind"] == kind]

    def assert_no_login_leak(self, p):
        """組織の ID（2 つ。小文字・大文字・ハイフン無し）、偽の auth status のメールと組織名、偽の問い合わせの出力の目印が、launcher の標準出力・
        標準エラーと、状態の置き場の全ファイルに無い。"""
        texts = {"stdout": p.stdout, "stderr": p.stderr, **{str(f): f.read_text(errors="replace") for f in self.state.rglob("*") if f.is_file()}}
        words = [w for org in (self.LOGIN_ORG, self.TOKEN_ORG) for w in (org, org.replace("-", ""))]
        words += ["someone@example.invalid".lower(), "fakeorgnamezq", "probemarkerzq", "req_x"]
        for where, text in texts.items():
            for w in words:
                self.assertNotIn(w, text.lower(), where)

    def assert_gone(self, pid: int):
        """pid の偽のプロセスが残っていない。os.kill(pid, 0) が ProcessLookupError になるまで 5 秒待つ（/proc の無い OS でも）。
        Linux では、刈られる前のゾンビ（/proc/<pid>/stat の状態が Z）も終わったものと数える。守らない範囲: /proc の無い OS（macOS）では、
        ゾンビを生きているものと数える（5 秒のうちに刈られなければ落ちる）。番号が別のプロセスに使い回されたときは、それを残りと数えて落ちる。"""
        deadline = time.time() + 5
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            try:
                if Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[1].split()[0] == b"Z":
                    return
            except (OSError, IndexError):  # /proc が無い OS、調べる間に終わった
                pass
            if time.time() > deadline:
                os.kill(pid, signal.SIGKILL)
                self.fail(f"確かめのプロセス {pid} が残った")
            time.sleep(0.05)

    def test_a_token_from_another_organization_is_noted_without_stopping_the_child(self):
        """専用ログインのトークンの組織が、launcher を呼んだ環境のログインの組織と違うと、claude_login が different で、案内（claude setup-token）
        が付き、子は起動して ok で返り、続きから起動できる（確かめは status と続きの判定を変えない）。組織の ID・メール・組織名・問い合わせの
        出力は、標準出力・標準エラー・記録（record.json・delegations.jsonl）・claude-login.json のどこにも出ず、claude-login.json はモード 0600 で
        できる。比べを常に same にする、案内を落とす、起動を止める、different で続きを止める、どれかをどこかに書くと落ちる。"""
        p = self.run_launch("claude", env_extra={"FAKE_TOKEN_ORG": self.TOKEN_ORG}, raw=True)
        rec = json.loads(p.stdout)
        self.assertEqual((p.returncode, rec["status"], rec["resumable"], rec["blocked"]), (0, "ok", True, []), rec)
        self.assertTrue(self.args_file.exists())  # 子を起動した
        self.assertEqual(rec["claude_login"]["org"], "different", rec)
        self.assertEqual(set(rec["claude_login"]), {"org", "note"})
        self.assertIn("claude setup-token", rec["claude_login"]["note"])
        launch = load_launch()
        self.assertNotEqual(launch.CLAUDE_LOGIN_NOTE, launch.CLAUDE_LOGIN_UNREAD_NOTE)
        # 読めないときの案内と取り違えない（別の定数を出す後退を見る。2 つの定数の中身そのものを入れ替える編集は見ない: 文面は人が読む）
        self.assertEqual(rec["claude_login"]["note"], launch.CLAUDE_LOGIN_NOTE)
        self.assertEqual(json.loads((Path(rec["out"]) / "record.json").read_text(encoding="utf-8"))["claude_login"], rec["claude_login"])
        last = json.loads((self.state / "delegations.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(last["claude_login"], rec["claude_login"])
        saved = self.state / "claude-login.json"
        self.assertEqual(set(json.loads(saved.read_text())), {"env", "org"})
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assert_no_login_leak(p)

    def test_a_token_from_the_same_organization_adds_no_note(self):
        """トークンの組織がログインの組織と同じなら、claude_login は {"org": "same"} だけ（案内が無い）。ID の大文字・小文字は問わない
        （ヘッダの値だけ大文字、orgId だけ大文字）。比べを常に different にする、same に案内を付ける、どちらかの側を小文字にせず比べる、
        ヘッダの読み方で大文字・小文字を区別すると落ちる。"""
        for name, login, token in (("same", self.LOGIN_ORG, self.LOGIN_ORG), ("header-upper", self.LOGIN_ORG, self.LOGIN_ORG.upper()),
                                   ("orgid-upper", self.LOGIN_ORG.upper(), self.LOGIN_ORG)):
            with self.subTest(name):
                (self.state / "claude-login.json").unlink(missing_ok=True)
                rc, rec = self.run_launch("claude", env_extra={"FAKE_LOGIN_ORG": login, "FAKE_TOKEN_ORG": token})
                self.assertEqual((rc, rec["status"], rec["claude_login"]), (0, "ok", {"org": "same"}), rec)

    def test_the_token_organization_is_asked_once_per_env_file_content(self):
        """問い合わせは .env の中身ごとに 1 回で、2 回目からは覚えた結果を使う（claude auth status は毎回呼ぶ）。.env の中身を変えると、また問い合わせ、
        トークンの組織が変わっていれば結果も変わる。--resume の続きでも同じに確かめる。覚えない・.env の中身で見分けない・続きで確かめないと落ちる。"""
        rc, first = self.run_launch("claude")
        self.assertEqual((first["status"], first["claude_login"]), ("ok", {"org": "same"}), first)
        self.assertEqual((len(self.login_calls("auth")), len(self.login_calls("probe"))), (1, 1))
        rc, rec = self.run_launch("claude", env_extra={"FAKE_TOKEN_ORG": self.TOKEN_ORG})  # トークンの組織が変わっても、.env が同じなら問い合わせない
        self.assertEqual((rec["status"], rec["claude_login"]), ("ok", {"org": "same"}), rec)
        self.assertEqual((len(self.login_calls("auth")), len(self.login_calls("probe"))), (2, 1))
        (self.cfg / ".env").write_text("X=2\n")  # トークンを置き直した
        rc, rec = self.run_launch("claude", env_extra={"FAKE_TOKEN_ORG": self.TOKEN_ORG})
        self.assertEqual((rec["status"], rec["claude_login"]["org"]), ("ok", "different"), rec)
        self.assertEqual((len(self.login_calls("auth")), len(self.login_calls("probe"))), (3, 2))
        rc, rec = self.run_launch(None, extra=["--resume", first["out"]], env_extra={"FAKE_TOKEN_ORG": self.TOKEN_ORG})
        self.assertEqual((rec["status"], rec["claude_login"]["org"]), ("ok", "different"), rec)
        self.assertEqual((len(self.login_calls("auth")), len(self.login_calls("probe"))), (4, 2))

    def test_an_unknown_login_organization_skips_the_query_and_still_starts_the_child(self):
        """launcher を呼んだ環境のログインの組織が分からない（ログインが無い・JSON でない・終わらない・orgId が uuid の形でない・文字列でない・出力が
        dict でない・API キーのログイン）と、unknown で理由（why）だけを残し（案内 note は無い）、問い合わせをせず、子は起動して ok で返る。理由に、
        その場面の偽の出力に在る文字列（authMethod の値など）を写さない。ログインが無くても問い合わせる、起動を止める、上限を外す、orgId の形や
        出力の dict を見ない、出力を写すと落ちる。短い上限は、終わらない場面にだけ掛ける。"""
        no_org = self.NO_ORG
        for how, why, hidden in (("none", no_org, ["none", "firstparty"]), ("garbage", "JSON として読めない", ["not logged in"]),
                                 ("hang", "2 秒で終わらなかったので止めた", ["hangmarkerzq"]),
                                 ("bad-org", no_org, [self.LOGIN_ORG, "fakeorgnamezq", "someone@example.invalid", "claude.ai"]),
                                 ("num-org", no_org, ["12345", "fakeorgnamezq"]), ("list", no_org, ["[]"]), ("api_key", no_org, ["api_key"])):
            with self.subTest(how):
                Path(str(self.args_file) + ".login").unlink(missing_ok=True)
                self.args_file.unlink(missing_ok=True)
                started = time.monotonic()
                rc, rec = self.run_launch("claude", env_extra={"FAKE_LOGIN": how, **({"BB_WRIT_LOGIN_CHECK_TIMEOUT": "2"} if how == "hang" else {})})
                self.assertLess(time.monotonic() - started, 30)
                self.assertEqual((rc, rec["status"], rec["claude_login"]["org"]), (0, "ok", "unknown"), rec)
                self.assertEqual(set(rec["claude_login"]), {"org", "why"})
                self.assertIn(why, rec["claude_login"]["why"])
                for h in hidden:
                    self.assertNotIn(h, rec["claude_login"]["why"].lower())
                self.assertEqual(self.login_calls("probe"), [])
                self.assertTrue(self.args_file.exists())
                self.assert_gone(self.login_calls("auth")[0]["pid"])

    def test_an_unreadable_query_output_is_unknown_and_not_remembered(self):
        """問い合わせの出力に組織のヘッダが無い（終了コード 0）・終了コード 1 でヘッダが無い（401 の形）・値が 2 種類・値が uuid の形でない、のどれも
        unknown で、子は起動して ok で返り、結果を覚えない（次の起動でまた問い合わせる）。問い合わせが通った（終了コード 0）のに読めないときだけ、
        確かめが働いていないことの案内（note。claude setup-token を含む）が付く。組織の ID・問い合わせの出力の目印は、どこにも出ない。
        読めないものを same・different にする、unknown を覚える、uuid の形を見ない、通ったのに読めないときの案内を落とす、失敗した問い合わせにも
        案内を付ける、出力を写すと落ちる。"""
        unread_note = load_launch().CLAUDE_LOGIN_UNREAD_NOTE
        for how, why, noted in (("noheader", "0 種類（終了コード 0）", True), ("401", "0 種類（終了コード 1）", False),
                                ("two", "2 種類（終了コード 0）", True), ("notuuid", "0 種類（終了コード 0）", True)):
            with self.subTest(how):
                Path(str(self.args_file) + ".login").unlink(missing_ok=True)
                for _ in range(2):
                    p = self.run_launch("claude", env_extra={"FAKE_PROBE": how, "FAKE_TOKEN_ORG": self.TOKEN_ORG}, raw=True)
                    rec = json.loads(p.stdout)
                    self.assertEqual((p.returncode, rec["status"], rec["claude_login"]["org"]), (0, "ok", "unknown"), rec)
                    self.assertIn(why, rec["claude_login"]["why"])
                    if noted:
                        self.assertEqual(set(rec["claude_login"]), {"org", "why", "note"})
                        self.assertIn("claude setup-token", rec["claude_login"]["note"])
                        self.assertEqual(rec["claude_login"]["note"], unread_note)  # 別の組織の案内と取り違えない
                    else:
                        self.assertEqual(set(rec["claude_login"]), {"org", "why"})
                    self.assertFalse((self.state / "claude-login.json").exists())
                    self.assert_no_login_leak(p)
                self.assertEqual(len(self.login_calls("probe")), 2)

    def test_a_failed_query_that_shows_one_organization_is_still_used(self):
        """問い合わせが終了コード 1 で終わっても、ヘッダの値が 1 種類読めれば、それを組織として比べ（different）、覚える（次の起動は問い合わせない）。
        終了コードが 0 でないときにヘッダを捨てると落ちる。"""
        for _ in range(2):
            rc, rec = self.run_launch("claude", env_extra={"FAKE_PROBE": "exit1", "FAKE_TOKEN_ORG": self.TOKEN_ORG})
            self.assertEqual((rc, rec["status"], rec["claude_login"]["org"]), (0, "ok", "different"), rec)
        self.assertEqual(len(self.login_calls("probe")), 1)
        self.assertTrue((self.state / "claude-login.json").exists())

    def test_a_query_that_does_not_end_is_stopped_with_its_process_group(self):
        """問い合わせが終わらないと、上限でプロセスグループ（dotenvx とその下の claude）をまとめて止め、unknown で（失敗した問い合わせなので
        案内 note は無い）、子は起動して ok で返る。確かめのプロセスは残らない。上限を外す、dotenvx だけを止める（下の claude が残る）、
        止めた問い合わせに案内を付けると落ちる。上限は claude auth status にも掛かるので、遅い機械でも auth status が終わる長さにする。"""
        started = time.monotonic()
        p = self.run_launch("claude", env_extra={"FAKE_PROBE": "hang", "BB_WRIT_LOGIN_CHECK_TIMEOUT": "5"}, raw=True)
        rec = json.loads(p.stdout)
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual((p.returncode, rec["status"], rec["claude_login"]["org"]), (0, "ok", "unknown"), rec)
        self.assertEqual(set(rec["claude_login"]), {"org", "why"})
        self.assertIn("5 秒で終わらなかったので止めた", rec["claude_login"]["why"])
        self.assertFalse((self.state / "claude-login.json").exists())
        self.assert_no_login_leak(p)
        self.assertEqual(len(self.login_calls("probe")), 1, rec)  # 0 件は、claude auth status か、偽の CLI の起動が上限に当たったとき（問い合わせが無いか、偽の claude が呼び出しを残す前に止めた）
        probe = self.login_calls("probe")[0]
        self.assert_gone(probe["pid"])  # dotenvx の下の claude
        self.assert_gone(probe["ppid"])  # dotenvx

    def test_the_query_gets_only_its_own_claude_and_anthropic_variables(self):
        """問い合わせの環境には、親の CLAUDE*・ANTHROPIC_*（API キーなど）が渡らず、CLAUDE_CONFIG_DIR（専用ログインの置き場）・ANTHROPIC_LOG・
        CLAUDE_CODE_DISABLE_BACKGROUND_TASKS だけが在る。claude auth status には launcher の環境がそのまま渡る。どちらも、launcher の標準入力が
        pipe でも標準入力は閉じ、作業ディレクトリは状態の置き場。問い合わせは --restricted で起こし、dotenvx には、専用ログインの .env と
        .env.keys を -q・-f・-fk で渡す。子（本番の起動）の環境に ANTHROPIC_LOG は無い。親の変数を外さない、auth status の環境を絞る、
        標準入力を閉じない、--restricted・dotenvx の引数を落とす、子に ANTHROPIC_LOG を渡すと落ちる。"""
        self.maxDiff = None
        parent = {"ANTHROPIC_API_KEY": "sk-ant-fake", "ANTHROPIC_BASE_URL": "http://example.invalid", "ANTHROPIC_LOG": "info",
                  "CLAUDE_CONFIG_DIR": str(Path(self.tmp.name) / "parent-claude"), "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}
        for write in ((), ("--write",)):
            with self.subTest(write=bool(write)):
                Path(str(self.args_file) + ".login").unlink(missing_ok=True)
                (self.state / "claude-login.json").unlink(missing_ok=True)
                cwd = self.worktree("login")[1] if write else None
                rc, rec = self.run_launch("claude", extra=write, cwd=cwd, env_extra=parent, stdin=subprocess.PIPE)
                self.assertEqual((rec["status"], rec["claude_login"]), ("ok", {"org": "same"}), rec)
                (auth,), (probe,) = self.login_calls("auth"), self.login_calls("probe")
                self.assertEqual(probe["env"], {"CLAUDE_CONFIG_DIR": str(self.cfg / "claude-home"), "ANTHROPIC_LOG": "debug",
                                                "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"})
                self.assertEqual(probe["argv"], ["-p", "ok とだけ返して", "--model", "haiku", "--output-format", "json",
                                                 "--no-session-persistence", "--tools", "", "--restricted"])
                self.assertEqual(auth["argv"], ["auth", "status", "--json"])
                (dotenvx,) = self.login_calls("dotenvx")
                self.assertEqual(dotenvx["argv"], ["run", "-q", "-f", str(self.cfg / ".env"), "-fk", str(self.cfg / ".env.keys"), "--",
                                                   "claude", *probe["argv"]])
                self.assertEqual({k: auth["env"].get(k) for k in parent}, parent)
                for c in (auth, probe):
                    self.assertTrue(c["stdin_is_devnull"], c["kind"])
                    self.assertEqual(Path(c["cwd"]).resolve(), self.state.resolve())
                child = json.loads(Path(str(self.args_file) + ".env").read_text())
                self.assertNotIn("ANTHROPIC_LOG", child)
                self.assertNotIn("ANTHROPIC_API_KEY", child)

    def test_the_organization_header_is_read_from_standard_error_too(self):
        """問い合わせのヘッダの記録が標準エラーにだけ出ても、組織を読む（launcher は標準出力と標準エラーの両方を読む）。標準エラーを読まないと
        unknown になって落ちる。"""
        rc, rec = self.run_launch("claude", env_extra={"FAKE_PROBE": "stderr", "FAKE_TOKEN_ORG": self.TOKEN_ORG})
        self.assertEqual((rc, rec["status"], rec["claude_login"]["org"]), (0, "ok", "different"), rec)

    def test_an_unexpected_failure_in_the_check_does_not_stop_the_child(self):
        """確かめの中の想定外の例外は、OSError でもそうでない型でも、unknown にして例外の型の名前だけを残し（例外の文は写さない）、
        子は起動して ok で返る。例外を受けずに起動の前の準備の失敗にする、受ける型を OSError か RuntimeError に狭める、例外の文を写すと落ちる。"""
        for exc in ("FileNotFoundError", "RuntimeError"):  # claude を起動できないときの形（OSError）と、OSError でない型
            with self.subTest(exc):
                self.args_file.unlink(missing_ok=True)
                patch = f"""\
                    def boom(*a, **k):
                        raise {exc}("Leakmarkerzq")
                    launch.run_bounded = boom
                    """
                rc, rec = self.launch_patched(patch, ["--host", "claude", "--cwd", str(self.cwd)])
                self.assertEqual((rc, rec["status"], rec.get("claude_login", {}).get("org")), (0, "ok", "unknown"), rec)  # 例外を受けないと、起動の前の準備の失敗になり、欄も無い
                self.assertIn(exc, rec["claude_login"]["why"])
                self.assertNotIn("Leakmarkerzq", json.dumps(rec, ensure_ascii=False))
                self.assertTrue(self.args_file.exists())

    def test_what_the_check_leaves_in_home_is_not_the_child_residue(self):
        """確かめ（問い合わせ）がホームに残したものは、子の残り（home_residue）に数えない。確かめを、ホームの前後の比較の「前」を取った後へ動かすと、
        子の残りに並んで failed になり落ちる。"""
        rc, rec = self.run_launch("claude", env_extra={"FAKE_PROBE_HOME": "1"})
        self.assertTrue((self.home / ".probe-left").exists())  # 問い合わせが作った
        self.assertEqual((rc, rec["status"], rec["home_residue"]), (0, "ok", []), rec)

    def test_other_hosts_and_an_unavailable_claude_do_not_check_the_login(self):
        """Codex・Grok の子と、専用ログインの無い Claude Code（unavailable）では、claude auth status も問い合わせも呼ばれず、claude_login の欄が無い。
        どのホストでも確かめる、起動しないときも確かめると落ちる。"""
        for host, extra, cwd in (("codex", (), None), ("grok", ("--write",), self.worktree("login")[1])):  # Grok の読み取り専用は /tmp の外の対象が要る
            with self.subTest(host):
                rc, rec = self.run_launch(host, extra=extra, cwd=cwd)
                self.assertEqual(rec["status"], "ok", rec)
                self.assertNotIn("claude_login", rec)
        (self.cfg / ".env").unlink()
        rc, rec = self.run_launch("claude")
        self.assertEqual(rec["status"], "unavailable", rec)
        self.assertNotIn("claude_login", rec)
        self.assertFalse(Path(str(self.args_file) + ".login").exists())

    def test_a_broken_memory_of_the_token_organization_is_asked_again(self):
        """壊れた claude-login.json（JSON でない・形が違う・org が 64 桁の 16 進の後に余分な文字が続く）は無いものとして扱い、問い合わせて書き直す。
        書けない（ディレクトリになっている）ときは、起動を止めず、結果を使い、書けなかったことを cache に残す。壊れたものを信じる（org を前方一致で
        見るなど）、読めずに止まる、書けずに止まると落ちる。"""
        import hashlib
        saved = self.state / "claude-login.json"
        env_sum = hashlib.sha256((self.cfg / ".env").read_bytes()).hexdigest()
        login_sum = hashlib.sha256(self.LOGIN_ORG.encode()).hexdigest()
        for n, broken in enumerate(("not json", "[]", json.dumps({"env": env_sum, "org": 5}), json.dumps({"env": env_sum}),
                                    json.dumps({"env": env_sum, "org": "x" * 64}), json.dumps({"env": env_sum, "org": login_sum + "0"})), 1):
            with self.subTest(broken):
                saved.write_text(broken)
                rc, rec = self.run_launch("claude", env_extra={"FAKE_TOKEN_ORG": self.TOKEN_ORG})
                self.assertEqual((rec["status"], rec["claude_login"]["org"]), ("ok", "different"), rec)
                self.assertEqual(len(self.login_calls("probe")), n)
                self.assertEqual(set(json.loads(saved.read_text())), {"env", "org"})
        saved.unlink()
        saved.mkdir()
        rc, rec = self.run_launch("claude")
        self.assertEqual((rc, rec["status"], rec["claude_login"]["org"]), (0, "ok", "same"), rec)
        self.assertIn("覚えられなかった", rec["claude_login"]["cache"])

    def test_a_codex_child_does_not_start_on_a_stale_copy_of_bb_writ(self):
        # Codex の子は専用ログインに入れた写しのスキルと補助スクリプトを読む（読み取り専用の子は一覧だけで、本文は読めなかった）。
        # この launcher と中身が違えば起動しない
        cache = self.cfg / "codex-home" / "plugins" / "cache"

        def copy(ver="9.9.9", mp="bb-writ", dirs=("skills", "scripts")):
            c = cache / mp / "bb-writ" / ver
            for d in dirs:
                shutil.copytree(ROOT / d, c / d, ignore=shutil.ignore_patterns("__pycache__"))
            return c

        def append(p):
            p.write_text(p.read_text(encoding="utf-8") + "\n古い文\n", encoding="utf-8")

        def two(stale_first):  # 写しが 2 つあれば、どちらが古くても止める
            append((copy("9.9.8") if stale_first else copy("9.9.9")) / "skills" / "interrogate" / "SKILL.md")
            copy("9.9.9" if stale_first else "9.9.8")

        def pyc():
            (copy() / "scripts" / "__pycache__").mkdir()
            (cache / "bb-writ" / "bb-writ" / "9.9.9" / "scripts" / "__pycache__" / "launch.cpython-312.pyc").write_bytes(b"x")

        cases = [  # (場面, 用意, 期待, 理由に出る名前の marketplace と違うファイル)
            ("同じ中身", lambda: copy(), "ok", None),
            ("スキルが違う", lambda: append(copy() / "skills" / "interrogate" / "SKILL.md"), "unavailable", ("bb-writ", "skills/interrogate/SKILL.md")),
            ("補助スクリプトが違う", lambda: append(copy() / "scripts" / "work.py"), "unavailable", ("bb-writ", "scripts/work.py")),
            ("写しに無いファイル", lambda: (copy() / "skills" / "interrogate" / "SKILL.md").unlink(), "unavailable", ("bb-writ", "skills/interrogate/SKILL.md")),
            ("写しにだけあるファイル", lambda: (copy() / "skills" / "old-skill.md").write_text("x"), "unavailable", ("bb-writ", "skills/old-skill.md")),
            ("scripts の無い写し", lambda: copy(dirs=("skills",)), "unavailable", ("bb-writ", "scripts/launch.py")),
            ("別の名前の marketplace", lambda: append(copy(mp="mine") / "skills" / "interrogate" / "SKILL.md"), "unavailable", ("mine", "skills/interrogate/SKILL.md")),
            ("古い写しが先", lambda: two(True), "unavailable", ("bb-writ", "skills/interrogate/SKILL.md")),
            ("古い写しが後", lambda: two(False), "unavailable", ("bb-writ", "skills/interrogate/SKILL.md")),
            ("写しの __pycache__ は比べない", pyc, "ok", None),
            ("skills の無い置き場は写しでない", lambda: (cache / "bb-writ" / "bb-writ" / "9.9.9").mkdir(parents=True), "ok", None),
            ("写しが無い（bb-writ 無しの子として起動する）", lambda: None, "ok", None),
        ]
        for name, prepare, status, why in cases:
            with self.subTest(name):
                shutil.rmtree(cache, ignore_errors=True)
                prepare()
                self.args_file.unlink(missing_ok=True)
                rc, rec = self.run_launch("codex")
                self.assertEqual(rec["status"], status, rec)
                if why is None:
                    self.assertTrue(self.args_file.exists(), "codex が呼ばれていない")
                    continue
                mp, path = why
                self.assertEqual(rc, 2, rec)
                self.assertFalse(self.args_file.exists(), "古い写しのまま偽の codex が呼ばれた")
                self.assertIn(path, rec["problems"][0])
                for sub in (f"codex plugin marketplace upgrade {mp}", f"codex plugin add bb-writ@{mp}",
                            "統合してから入れ直す"):  # 作業場所の launcher では入れ直しても一致しないことも示す
                    self.assertIn(sub, rec["problems"][0])
                self.assertNotIn(f"codex plugin remove bb-writ@{mp}", rec["problems"][0])  # remove は写しと設定を消す。add だけで置き換わる

    # 専用ログインの Codex の写しを、launcher が入れ直す（codex plugin add）。写しは bb-writ を commit するたびに古くなり、
    # 利用者が毎回手で入れ直していた。入れ直すのは、専用ログインの Codex が答えた登録で、その marketplace が手元のディレクトリで、
    # その置き場がこの launcher のときだけ
    LIST = ["plugin", "marketplace", "list", "--json"]

    def stale_codex_copy(self, mp="bb-writ"):
        """専用ログインに、この bb-writ と 1 行だけ違う写し（古い版）を置く。"""
        c = self.cfg / "codex-home" / "plugins" / "cache" / mp / "bb-writ" / "9.9.8"
        for d in ("skills", "scripts"):
            shutil.copytree(ROOT / d, c / d, ignore=shutil.ignore_patterns("__pycache__"))
        p = c / "skills" / "interrogate" / "SKILL.md"
        p.write_text(p.read_text(encoding="utf-8") + "\n古い文\n", encoding="utf-8")
        return c

    def local_marketplace(self, mp="bb-writ", source=None, source_type="local"):
        """Codex CLI 0.171.0 が、marketplace を登録して bb-writ を入れたときに config.toml に書く形（偽の codex がこれを読んで一覧に答える）。"""
        return (f'[marketplaces.{mp}]\nsource_type = "{source_type}"\nsource = {json.dumps(str(ROOT if source is None else source))}\n\n'
                f'[plugins."bb-writ@{mp}"]\nenabled = true\n')

    def reset_codex_login(self):
        home = self.cfg / "codex-home"
        shutil.rmtree(home / "plugins", ignore_errors=True)
        (home / "config.toml").unlink(missing_ok=True)
        shutil.rmtree(self.home / ".codex", ignore_errors=True)
        for f in ("", ".plugin", ".go", ".gap", ".adding"):
            Path(str(self.args_file) + f).unlink(missing_ok=True)
        return home

    def plugin_calls(self) -> list:
        """偽の codex plugin が受けた呼び出し（引数と CODEX_HOME）。"""
        f = Path(str(self.args_file) + ".plugin")
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def plugin_changes(self) -> list:
        """偽の codex plugin が受けた add・remove の引数（一覧は除く）。"""
        return [c["argv"] for c in self.plugin_calls() if c["argv"][1:2] in (["add"], ["remove"])]

    def saved_record(self, rec) -> dict:
        return json.loads((Path(rec["out"]) / "record.json").read_text())

    def wait_until(self, done, what, limit=30):
        end = time.monotonic() + limit
        while not done():
            if time.monotonic() > end:
                self.fail(what)
            time.sleep(0.02)

    def start_codex_launcher(self, name, env, extra=()):
        """launcher を裏で起こす。試験は try/finally で release_fakes に渡す（片付けの前に止める）。"""
        return subprocess.Popen([sys.executable, str(LAUNCH), "--host", "codex", "--cwd", str(self.cwd), "--prompt-file", str(self.prompt),
                                 "--out", str(self.state / name), *extra], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def release_fakes(self, *procs):
        """待たせた偽の codex plugin を .go で終わらせ、起こした launcher を止める（試験が落ちた道でも、一時の置き場を消す前に）。
        add・一覧を待つ段の launcher は TERM でそのまま止まり、偽の codex plugin には伝わらないので、先に .go を置く。
        子を待つ段の launcher は、TERM を子に伝えて片付ける。"""
        Path(str(self.args_file) + ".go").touch()
        for p in procs:
            if p is None:
                continue
            if p.poll() is None:
                p.terminate()
            try:
                p.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                p.kill()
                p.communicate()
            except ValueError:  # 試験が読み終えた
                pass

    def test_a_stale_codex_copy_from_this_launchers_local_marketplace_is_reinstalled_with_add_only(self):
        # remove は写しと設定を消し、入れ直すまでプラグインが無い状態を作る。add だけで写しは置き換わる。
        # 一覧も add も、親の環境の CODEX_HOME でなく、専用ログインに向ける
        for mp, parent in (("bb-writ", None), ("mine", None), ("bb-writ", "試験のホームの .codex")):
            with self.subTest(marketplace=mp, parent_codex_home=parent):
                home = self.reset_codex_login()
                self.stale_codex_copy(mp)
                (home / "config.toml").write_text(self.local_marketplace(mp))
                rc, rec = self.run_launch("codex", env_extra={"CODEX_HOME": str(self.home / ".codex")} if parent else None)
                self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
                calls = self.plugin_calls()
                self.assertEqual([c["argv"] for c in calls], [self.LIST, ["plugin", "add", f"bb-writ@{mp}"]],
                                 "登録を 1 回聞き、codex plugin add を 1 回だけ")
                for c in calls:
                    self.assertEqual(Path(c["CODEX_HOME"] or "/").resolve(), home.resolve(), " ".join(c["argv"]) + " の CODEX_HOME が専用ログインでない")
                self.assertTrue(self.args_file.exists(), "入れ直した後に codex exec が呼ばれていない")
                self.assertEqual(self.saved_record(rec).get("codex_plugin_add"), {"marketplace": mp, "exit": 0}, "record.json")
                last = json.loads((self.state / "delegations.jsonl").read_text().splitlines()[-1])
                self.assertEqual(last.get("codex_plugin_add"), {"marketplace": mp, "exit": 0}, "delegations.jsonl の行")

    def test_a_reinstall_accepts_a_source_spelled_through_a_symlink(self):
        # 綴りが違っても、symlink を解いて同じ置き場なら、条件を満たす（Codex が答えた source の側も、launcher の置き場の側も）
        link = Path(self.tmp.name) / "bb-writ-link"
        link.symlink_to(ROOT, target_is_directory=True)
        for name, source, launcher in (("source が symlink を通る綴り", link, LAUNCH),
                                       ("launcher を symlink を通る綴りで起こす", ROOT, link / "scripts" / "launch.py")):
            with self.subTest(name):
                home = self.reset_codex_login()
                self.stale_codex_copy()
                (home / "config.toml").write_text(self.local_marketplace(source=source))
                p = subprocess.run([sys.executable, str(launcher), "--host", "codex", "--cwd", str(self.cwd), "--prompt-file", str(self.prompt)],
                                   env=self.launch_env(), capture_output=True, text=True, timeout=120)
                rec = json.loads(p.stdout)
                self.assertEqual((p.returncode, rec["status"]), (0, "ok"), rec)
                self.assertEqual(self.plugin_changes(), [["plugin", "add", "bb-writ@bb-writ"]], "同じ置き場なのに入れ直さない")

    def test_a_stale_codex_copy_is_not_reinstalled_unless_its_marketplace_is_this_launchers_local_directory(self):
        # 入れ直しても launcher と一致しない（GitHub から入れた、作業場所の launcher）か、Codex の答えを読めないときは、入れ直さずに止め、
        # 手で入れ直すコマンド（add だけ）を示す。どの条件が外れたか（Codex が答えた登録、読めなかった訳）も示す
        ok = self.local_marketplace()

        def listed(source):  # 古い写しの marketplace の項目の marketplaceSource を差し替えた答え
            return "out:" + json.dumps({"marketplaces": [{"name": "bb-writ", "root": str(ROOT), "marketplaceSource": source}]})
        unread = "登録を確かめられなかった"
        # 互いを指す symlink。resolve() は、3.9・3.11・3.12 では RuntimeError（Symlink loop）を出し、3.13 では例外を出さない
        # （3.9.25・3.11.14・3.12.12・3.13.11 で確かめた）。どちらでも、同じ結果（unavailable、add なし）になる
        loop = Path(self.tmp.name) / "loop-a"
        loop.symlink_to(Path(self.tmp.name) / "loop-b")
        (Path(self.tmp.name) / "loop-b").symlink_to(loop)
        cases = [  # (場面, 専用ログインの config.toml, 試験のホームの .codex/config.toml, 偽の一覧の動き, 理由に在る句)
            ("config.toml が無い（一覧が空）", None, None, "", ["登録が無い"]),
            ("sourceType が git", self.local_marketplace(source_type="git"), None, "", ['sourceType = "git"', json.dumps(str(ROOT))]),
            ("source が別のディレクトリ", self.local_marketplace(source=self.cwd), None, "", ['sourceType = "local"', json.dumps(str(self.cwd))]),
            ("source が launcher の置き場の親", self.local_marketplace(source=ROOT.parent), None, "", []),
            ("source が launcher の置き場の下", self.local_marketplace(source=ROOT / "skills"), None, "", []),
            ("source が相対パス", self.local_marketplace(source="."), None, "", []),  # launcher は launcher の置き場で起こす
            ("source が NUL を含む（解けない道筋）", self.local_marketplace(source=f"{ROOT}\0"), None, "", []),
            ("source が symlink の輪", self.local_marketplace(source=loop), None, "", []),
            ("条件を満たすのは別の名前の項目", self.local_marketplace(source_type="git") + "\n" + self.local_marketplace("mine"), None, "", []),
            ("条件を満たすのは名前が前方一致する項目（bb-writ-dev）", self.local_marketplace("bb-writ-dev"), None, "", ["登録が無い"]),
            ("条件を満たすのは試験のホームの .codex/config.toml", None, ok, "", ["登録が無い"]),
            ("一覧が 1 で終わる", ok, None, "fail", [unread, "failed to load marketplace(s)"]),
            ("一覧が条件を満たす答えを出してから 1 で終わる", ok, None, "answer-fail", [unread, "終了コード 1"]),
            ("一覧が JSON でない", ok, None, "out:not json", [unread]),
            ("marketplaces がオブジェクト", ok, None, "out:" + json.dumps({"marketplaces": {}}), [unread]),
            ("marketplaces の鍵が無い", ok, None, "out:{}", [unread]),
            ("項目に marketplaceSource の鍵が無い", ok, None, "out:" + json.dumps({"marketplaces": [{"name": "bb-writ", "root": str(ROOT)}]}), [unread]),
            ("項目が文字列", ok, None, "out:" + json.dumps({"marketplaces": ["bb-writ"]}), [unread]),
            ("marketplaceSource が null", ok, None, listed(None), [unread]),
            ("sourceType が数", ok, None, listed({"sourceType": 1, "source": str(ROOT)}), [unread]),
            ("sourceType が null", ok, None, listed({"sourceType": None, "source": str(ROOT)}), [unread]),
            ("sourceType が配列", ok, None, listed({"sourceType": ["local"], "source": str(ROOT)}), [unread]),
            ("source が数", ok, None, listed({"sourceType": "local", "source": 1}), [unread]),
            ("source が null", ok, None, listed({"sourceType": "local", "source": None}), [unread]),
            ("source が配列", ok, None, listed({"sourceType": "local", "source": [str(ROOT)]}), [unread]),
        ]
        for name, config, user_config, answer, said in cases:
            with self.subTest(name):
                home = self.reset_codex_login()
                self.stale_codex_copy()
                if config is not None:
                    (home / "config.toml").write_text(config)
                if user_config is not None:
                    (self.home / ".codex").mkdir()
                    (self.home / ".codex" / "config.toml").write_text(user_config)
                rc, rec = self.run_launch("codex", proc_cwd=str(ROOT), env_extra={"FAKE_LIST": answer})
                self.assertEqual((rc, rec["status"]), (2, "unavailable"), rec)
                self.assertEqual(self.plugin_changes(), [], "条件を満たさないのに codex plugin add・remove が呼ばれた")
                for c in self.plugin_calls():
                    self.assertEqual(Path(c["CODEX_HOME"] or "/").resolve(), home.resolve(), " ".join(c["argv"]) + " の CODEX_HOME が専用ログインでない")
                self.assertFalse(self.args_file.exists(), "古い写しのまま codex exec が呼ばれた")
                self.assertNotIn("codex_plugin_add", self.saved_record(rec))
                why = rec["problems"][0]
                for sub in ["codex plugin add bb-writ@bb-writ", "codex plugin marketplace upgrade bb-writ", "統合してから入れ直す", *said]:
                    self.assertIn(sub, why)
                self.assertNotIn("codex plugin remove", why)

    def test_no_reinstall_when_codex_cannot_start(self):
        # codex を起動できない（OSError）ときも、failed でなく unavailable で、登録を確かめられなかった訳を示す
        home = self.reset_codex_login()
        self.stale_codex_copy()
        (home / "config.toml").write_text(self.local_marketplace())
        fake = self.bin / "codex"
        body = fake.read_bytes()
        fake.write_text("#!/nonexistent/interpreter\n")  # 実行できるが、起動できない
        try:
            rc, rec = self.run_launch("codex")
        finally:
            fake.write_bytes(body)
            fake.chmod(0o755)
        self.assertEqual((rc, rec["status"]), (2, "unavailable"), rec)
        self.assertNotIn("codex_plugin_add", self.saved_record(rec))
        self.assertIn("登録を確かめられなかった", rec["problems"][0])
        self.assertIn("起動できなかった", rec["problems"][0])

    def test_a_reinstall_that_leaves_the_copy_stale_stops_with_the_add_result(self):
        # 成否は add の終了コードでなく、比べ直しで決める。まだ違えば起動せず、どの写しがどのファイルで違うかと、実行した add・
        # その終了コード・出力（標準出力の後に標準エラーをつないだ末尾）を理由に残す。失敗したのと同じコマンドを「入れ直す」とは勧めない
        for how, code, said in (("fail", 1, "failed to back up plugin cache entry"), ("noop", 0, None)):
            with self.subTest(add=how):
                home = self.reset_codex_login()
                self.stale_codex_copy()
                (home / "config.toml").write_text(self.local_marketplace())
                rc, rec = self.run_launch("codex", env_extra={"FAKE_ADD": how})
                self.assertEqual((rc, rec["status"]), (2, "unavailable"), rec)
                self.assertFalse(self.args_file.exists(), "入れ直しても古い写しのまま codex exec が呼ばれた")
                self.assertEqual(len(self.plugin_changes()), 1, "入れ直しは 1 回の起動で 1 回だけ")
                self.assertEqual(self.saved_record(rec).get("codex_plugin_add"), {"marketplace": "bb-writ", "exit": code}, "record.json")
                why = rec["problems"][0]
                for sub in ["skills/interrogate/SKILL.md", f"終了コード {code}", "codex plugin add bb-writ@bb-writ", *([said] if said else [])]:
                    self.assertIn(sub, why)
                self.assertNotIn("入れ直す: ", why)

    def test_a_reinstall_is_judged_by_comparing_again_not_by_the_add_exit_code(self):
        # add が 0 以外で終わっても、写しが置き換わっていれば起動する
        home = self.reset_codex_login()
        self.stale_codex_copy()
        (home / "config.toml").write_text(self.local_marketplace())
        rc, rec = self.run_launch("codex", env_extra={"FAKE_ADD": "replace-fail"})
        self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
        self.assertTrue(self.args_file.exists(), "写しが置き換わったのに codex exec が呼ばれていない")
        self.assertEqual(self.saved_record(rec).get("codex_plugin_add"), {"marketplace": "bb-writ", "exit": 1}, "record.json")

    def test_concurrent_launchers_reinstall_the_codex_copy_once(self):
        # 同時に 2 つの codex plugin add を流すと、片方が「failed to back up plugin cache entry」で落ちた（20 回中 4 回）。
        # launcher は比べから入れ直しまでを専用ログインの排他の下で行う。ほかの launcher の add が写しを置き換えている最中
        # （古い写しを退け、新しい写しを置く前）に比べると、写しが無いと見て bb-writ 無しの子を起動するか、読み損ねて failed になる
        home = self.reset_codex_login()
        self.stale_codex_copy()
        (home / "config.toml").write_text(self.local_marketplace())
        env = self.launch_env(env_extra={"FAKE_ADD": "gap"})  # 偽の add は、写しが無い瞬間を、試験が .go を置くまで引き延ばす
        first = second = None
        try:
            first = self.start_codex_launcher("first", env)
            self.wait_until(Path(str(self.args_file) + ".gap").exists, "1 つ目の launcher の add が、古い写しを退けたところまで進まない")
            second = self.start_codex_launcher("second", env)
            ino, second_record = os.stat(home).st_ino, self.state / "second" / "record.json"

            def second_waits():  # 2 つ目が専用ログインの排他を待っている（Linux の /proc/locks の待ち手の行）
                try:
                    rows = [x.split() for x in Path("/proc/locks").read_text().splitlines()]
                except OSError:
                    return False
                return any(len(r) > 6 and r[1] == "->" and r[5] == str(second.pid) and r[6].endswith(f":{ino}") for r in rows)
            # 2 つ目が排他を待つか、排他の外で比べて起動するまで待つ。待ち手が見えない環境では上限まで待つ（正しい launcher は、
            # どの順でも 2 つ目を .go の前に起動しないので、待ち方は後退を見つける力にだけ効く）
            end = time.monotonic() + 5
            while not (second_waits() or second_record.exists() or second.poll() is not None) and time.monotonic() < end:
                time.sleep(0.02)
            self.assertFalse(second_record.exists(), "2 つ目の launcher が、入れ直しの途中（写しが無い間）に比べて結果を出した")
            self.assertFalse(self.args_file.exists(), "入れ直しの途中に codex exec が呼ばれた")
            Path(str(self.args_file) + ".go").touch()
            recs = []
            for name, p in (("1 つ目", first), ("2 つ目", second)):
                out, err = p.communicate(timeout=60)
                self.assertTrue(out.strip(), f"{name}の launcher が結果を出さない: " + err)
                recs.append(json.loads(out))
        finally:
            self.release_fakes(first, second)
        self.assertEqual(self.plugin_changes(), [["plugin", "add", "bb-writ@bb-writ"]], "add は合わせて 1 回だけ")
        for name, rec in zip(("1 つ目", "2 つ目"), recs):
            self.assertEqual((rec["status"], rec.get("exit_code")), ("ok", 0), name + "の launcher の codex exec")
        self.assertEqual(sum("codex_plugin_add" in self.saved_record(r) for r in recs), 1, "入れ直した記録は片方だけ")

    def test_the_reinstall_lock_is_not_held_while_the_codex_child_runs(self):
        # 排他は比べ直しの直後に放す。動いている Codex の子を待たずに、次の launcher が入れ直す
        home = self.reset_codex_login()
        self.stale_codex_copy()
        (home / "config.toml").write_text(self.local_marketplace())
        first = None
        try:
            first = self.start_codex_launcher("first", self.launch_env(env_extra={"FAKE_HANG": "1"}), ["--timeout", "60"])
            self.wait_until(self.args_file.exists, "入れ直した後に 1 つ目の codex exec が呼ばれない")
            self.assertEqual(len(self.plugin_changes()), 1, "1 つ目の launcher が入れ直していない")
            placed = next((home / "plugins" / "cache" / "bb-writ" / "bb-writ").iterdir())  # add が置いた版の写しを、もう一度古くする
            target = placed / "skills" / "interrogate" / "SKILL.md"
            target.write_text(target.read_text(encoding="utf-8") + "\n古い文\n", encoding="utf-8")
            try:
                p = subprocess.run([sys.executable, str(LAUNCH), "--host", "codex", "--cwd", str(self.cwd), "--prompt-file", str(self.prompt)],
                                   env=self.launch_env(), capture_output=True, text=True, timeout=20)
            except subprocess.TimeoutExpired:
                self.fail("2 つ目の launcher が、1 つ目の子が動いている間、排他を待ち続けた")
            self.assertIsNone(first.poll(), "1 つ目の子が、2 つ目の launcher より先に終わった（順序を確かめられない）")
        finally:
            self.release_fakes(first)
        rec = json.loads(p.stdout)
        self.assertEqual(rec["status"], "ok", rec)
        self.assertEqual(self.saved_record(rec).get("codex_plugin_add"), {"marketplace": "bb-writ", "exit": 0}, "2 つ目の record.json")
        self.assertEqual(len(self.plugin_changes()), 2, "2 つ目の launcher が入れ直していない")

    def test_a_reinstall_that_does_not_finish_is_stopped_at_the_limit(self):
        # 一覧も add も、終わらなければ上限で止める（試験では 4 秒。上限は add の前の一覧にも掛かるので、偽の codex の起動が遅い機械でも
        # 一覧が先に止まらない長さにする）。launcher がそこで止まり続けない。止めた一覧がそれまでに出した答えは使わない
        unread = "登録を確かめられなかった"
        for name, fake, recorded, said in (("add が終わらない", {"FAKE_ADD": "hang"}, {"marketplace": "bb-writ", "exit": None}, None),
                                           ("一覧が終わらない", {"FAKE_LIST": "hang"}, None, unread),
                                           ("一覧が条件を満たす答えを出してから終わらない", {"FAKE_LIST": "answer-hang"}, None, unread)):
            with self.subTest(name):
                home = self.reset_codex_login()
                self.stale_codex_copy()
                (home / "config.toml").write_text(self.local_marketplace())
                t0 = time.monotonic()
                try:
                    rc, rec = self.run_launch("codex", env_extra={**fake, "BB_WRIT_CODEX_PLUGIN_TIMEOUT": "4"})
                finally:
                    self.release_fakes()
                self.assertLess(time.monotonic() - t0, 25, "launcher が上限で止めなかった")  # 偽の codex plugin は .go が無ければ 30 秒待つ
                self.assertEqual((rc, rec["status"]), (2, "unavailable"), rec)
                self.assertFalse(self.args_file.exists(), "止めた後に codex exec が呼ばれた")
                self.assertEqual(self.saved_record(rec).get("codex_plugin_add"), recorded, "record.json")
                self.assertIn("4 秒で終わらなかった", rec["problems"][0])
                if said:
                    self.assertEqual(self.plugin_changes(), [], "一覧を止めたのに codex plugin add が呼ばれた")
                    self.assertIn(said, rec["problems"][0])

    def test_no_start_when_the_reinstall_leaves_no_copy(self):
        # add が古い写しを退けた後、新しい写しを置かずに終わると、写しの無い状態が残る（古い写しは plugin-backup-* に残る。手で add する
        # まで、後の起動は、写しが無いものとして bb-writ 無しの子を起動する）。add を実行した後は、どの写しも違わず、その marketplace の
        # 写しが在るときだけ起動する。ほかの marketplace の
        # 同じ中身の写しでは通さない。理由に手で入れ直すコマンドを付けるのは、写しが無いままだと、次の起動からは、最初の比べで写しが
        # 無いものとして、bb-writ 無しの子が起動するため
        def other():
            c = self.cfg / "codex-home" / "plugins" / "cache" / "other" / "bb-writ" / "9.9.9"
            for d in ("skills", "scripts"):
                shutil.copytree(ROOT / d, c / d, ignore=shutil.ignore_patterns("__pycache__"))
        for name, prepare, how in (("写しが無いまま", lambda: None, "drop"), ("別の marketplace に同じ中身の写しが在る", other, "drop"),
                                   ("skills の無い版の置き場だけが在る", lambda: None, "hollow")):
            with self.subTest(name):
                home = self.reset_codex_login()
                self.stale_codex_copy()
                prepare()
                (home / "config.toml").write_text(self.local_marketplace())
                rc, rec = self.run_launch("codex", env_extra={"FAKE_ADD": how})
                self.assertEqual((rc, rec["status"]), (2, "unavailable"), rec)
                self.assertFalse(self.args_file.exists(), "入れ直しの後に写しが無いまま codex exec が呼ばれた")
                self.assertEqual(self.saved_record(rec).get("codex_plugin_add"), {"marketplace": "bb-writ", "exit": 1}, "record.json")
                why = rec["problems"][0]
                self.assertIn("codex plugin add bb-writ@bb-writ を実行したが（終了コード 1）", why)  # 実行したことの句
                self.assertIn("写しが無い", why)
                self.assertIn("手で入れ直す: ", why)  # 手の案内（実行したことの句とは別に、もう 1 回コマンドが在る）
                # 手のコマンドは専用ログインに向ける（CODEX_HOME が無いと、利用者の既定のログインに入れてしまう）。一時の置き場が symlink の
                # 下でも、launcher が書く道筋と試験の home の綴りは違いうるので、解いて比べる
                manual = why.split("手で入れ直す: ", 1)[-1]
                self.assertTrue(manual.startswith("CODEX_HOME="), "手で入れ直すコマンドに CODEX_HOME が無い: " + manual)
                given, command = manual[len("CODEX_HOME="):].split(" ", 1)
                self.assertEqual(Path(given).resolve(), home.resolve(), "手で入れ直すコマンドの CODEX_HOME が専用ログインでない")
                self.assertTrue(command.startswith("codex plugin add bb-writ@bb-writ"), "手で入れ直すコマンドの形が違う: " + manual)
                self.assertIn("add の出力の末尾: ", why)  # 標準出力の後につないだ標準エラーの失敗の文が、末尾に残る
                self.assertIn("failed to activate updated plugin cache entry", why)

    def test_a_reinstall_decides_by_the_first_of_two_entries_with_the_same_name(self):
        # Codex の答えには、同じ名前の項目が 2 つ並ぶことがある（専用ログインで信頼したプロジェクトの .codex/config.toml の marketplace も
        # 並んだ。0.171.0）。launcher は最初の 1 つで決める
        local = {"name": "bb-writ", "root": str(ROOT), "marketplaceSource": {"sourceType": "local", "source": str(ROOT)}}
        git = {"name": "bb-writ", "root": str(ROOT), "marketplaceSource": {"sourceType": "git", "source": "https://example.invalid/bb-writ.git"}}
        for name, items, started in (("最初が git、2 つ目が条件を満たす項目", [git, local], False),
                                     ("最初が条件を満たす項目、2 つ目が git", [local, git], True)):
            with self.subTest(name):
                home = self.reset_codex_login()
                self.stale_codex_copy()
                (home / "config.toml").write_text(self.local_marketplace())  # 偽の add が写しを組む出所
                rc, rec = self.run_launch("codex", env_extra={"FAKE_LIST": "out:" + json.dumps({"marketplaces": items})})
                if started:
                    self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
                    self.assertEqual(self.plugin_changes(), [["plugin", "add", "bb-writ@bb-writ"]], "最初の項目が条件を満たすのに入れ直さない")
                    continue
                self.assertEqual((rc, rec["status"]), (2, "unavailable"), rec)
                self.assertEqual(self.plugin_changes(), [], "最初の項目が git なのに codex plugin add が呼ばれた")
                self.assertIn('sourceType = "git"', rec["problems"][0])

    def test_a_reinstall_runs_codex_plugin_with_stdin_closed(self):
        # 一覧と add は、標準入力を閉じて（/dev/null で）起こす。launcher の標準入力をパイプにして起こし、継いだ標準入力が
        # たまたま /dev/null の環境でも、閉じ忘れを見分けられるようにする
        home = self.reset_codex_login()
        self.stale_codex_copy()
        (home / "config.toml").write_text(self.local_marketplace())
        p = subprocess.run([sys.executable, str(LAUNCH), "--host", "codex", "--cwd", str(self.cwd), "--prompt-file", str(self.prompt)],
                           env=self.launch_env(), input="", capture_output=True, text=True, timeout=120)
        rec = json.loads(p.stdout)
        self.assertEqual((p.returncode, rec["status"]), (0, "ok"), rec)
        calls = self.plugin_calls()
        self.assertEqual([c["argv"] for c in calls], [self.LIST, ["plugin", "add", "bb-writ@bb-writ"]], "一覧と add が 1 回ずつ呼ばれていない")
        for c in calls:
            self.assertTrue(c["stdin_is_devnull"], " ".join(c["argv"]) + " の標準入力が /dev/null でない")

    def test_no_reinstall_when_the_codex_copy_matches_or_is_absent(self):
        def same():
            c = self.cfg / "codex-home" / "plugins" / "cache" / "bb-writ" / "bb-writ" / "9.9.9"
            for d in ("skills", "scripts"):
                shutil.copytree(ROOT / d, c / d, ignore=shutil.ignore_patterns("__pycache__"))
        for name, prepare in (("写しが同じ", same), ("写しが無い", lambda: None)):
            with self.subTest(name):
                home = self.reset_codex_login()
                prepare()
                (home / "config.toml").write_text(self.local_marketplace())  # 条件は満たす
                rc, rec = self.run_launch("codex")
                self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
                self.assertEqual(self.plugin_calls(), [], "入れ直す要の無い写しに codex plugin が呼ばれた")
                self.assertNotIn("codex_plugin_add", self.saved_record(rec))

    def test_no_reinstall_without_the_lock_on_the_codex_login(self):
        # 専用ログインのディレクトリの排他を取れない環境では、排他なしで比べる。違えば入れ直さずに、手のコマンドを出して止め、
        # 同じなら子を起動する（開けないときも flock が失敗するときも、同じ 1 つの枝。試験が作れるのは開けないときだけ）
        for name, stale in (("写しが古い", True), ("写しが同じ", False)):
            with self.subTest(name):
                home = self.reset_codex_login()
                if stale:
                    self.stale_codex_copy()
                else:
                    c = home / "plugins" / "cache" / "bb-writ" / "bb-writ" / "9.9.9"
                    for d in ("skills", "scripts"):
                        shutil.copytree(ROOT / d, c / d, ignore=shutil.ignore_patterns("__pycache__"))
                (home / "config.toml").write_text(self.local_marketplace())
                home.chmod(0o300)  # 中の名前はたどれるが、ディレクトリそのものは開けない
                try:
                    try:
                        os.close(os.open(home, os.O_RDONLY | os.O_DIRECTORY))
                        opened = True
                    except PermissionError:
                        opened = False
                    if not opened:
                        rc, rec = self.run_launch("codex")
                finally:
                    home.chmod(0o700)  # 一時の置き場を消せるように（落ちたときも）
                if opened:
                    self.skipTest("この環境では、読めない権限のディレクトリも開ける（root など）")
                self.assertEqual(self.plugin_calls(), [], "排他を取れないのに codex plugin が呼ばれた")
                if not stale:
                    self.assertEqual((rc, rec["status"]), (0, "ok"), rec)
                    self.assertTrue(self.args_file.exists(), "写しが同じなのに codex exec が呼ばれていない")
                    continue
                self.assertEqual((rc, rec["status"]), (2, "unavailable"), rec)
                self.assertFalse(self.args_file.exists(), "古い写しのまま codex exec が呼ばれた")
                self.assertIn("排他を取れない", rec["problems"][0])
                self.assertIn("codex plugin add bb-writ@bb-writ", rec["problems"][0])

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
        env = self.launch_env()  # 拒否が退行しても、本物の codex と専用ログインに届かない（偽の CLI・試験のホームと設定）
        p = subprocess.run([sys.executable, str(LAUNCH), '--host', 'codex', '--cwd', str(self.cwd),
                            '--prompt-file', str(self.prompt), '--out', str(out)],
                           env=env, capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual((out / 'record.json').read_text(), record)


if __name__ == "__main__":
    unittest.main()
