"""担当の貸し出しと外部操作の記録（scripts/work.py）: 二重書き込みの拒否、旧担当の停止、結果が分からない操作の照合。"""

import errno
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent)); import process_limit  # noqa: E402,F401  試験に上限を掛ける（直に走らせても）

WORK = Path(__file__).resolve().parents[1] / "scripts" / "work.py"



def with_risk(t, args):
    """--reviewed を渡す呼び出しに、push・pr に要る危険度の宣言（この試験の外の約束）を足す。宣言そのものを試す試験は auto_risk を偽にする。"""
    if "--reviewed" not in args or "--risk" in args or not getattr(t, "auto_risk", True):
        return args
    i = args.index("--") if "--" in args else len(args)
    return (*args[:i], "--risk", "低:試験の外", *args[i:])


FILLED_NOTE = ("# 作業の記録\n\n- 目的: 試験の外\n- 制約: 試験の外\n- できたと分かる確かめ方（述語）: 試験の外\n"
               "- 指摘の扱い（審査の指摘ごとに、対応する・記録のみ・退ける と理由を、直す前に）: 試験の外\n")


def decl(key):
    """push・pr に要る、届ける前の審査の宣言（この試験の外の約束）。"""
    return ("--reviewed", "none:試験の外", "--risk", "低:試験の外") if key.split(":")[0] in ("push", "pr") else ()


class TestWork(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "wt"
        self.dir.mkdir()
        self.env = {**os.environ, "BB_WRIT_STATE_DIR": str(Path(self.tmp.name) / "state")}

    def tearDown(self):
        self.tmp.cleanup()

    def w(self, *args):
        args = with_risk(self, args)
        p = subprocess.run([sys.executable, str(WORK), *args[:1], *args[1:], "--dir", str(self.dir)] if args[0] != "op" else
                           [sys.executable, str(WORK), "op", args[1], "--dir", str(self.dir), *args[2:]], env=self.env, capture_output=True, text=True)
        return p.returncode, json.loads(p.stdout)

    def expire(self):
        record = next((Path(self.tmp.name) / "state" / "work").glob("wt-*")) / "lease.json"
        record.write_text(json.dumps({**json.loads(record.read_text()), "expires_at": time.time() - 1}))

    def test_second_claim_is_refused_while_first_is_live(self):
        rc, a = self.w("claim", "--holder", "A")
        self.assertEqual(rc, 0)
        rc, b = self.w("claim", "--holder", "B")
        self.assertEqual((rc, b["holder"]), (3, "A"))

    def test_rewritten_lease_does_not_lock_out_the_next_holder(self):
        # 書く子は作業場所の記録に書けるので、期限を遠い将来にした担当や壊れた担当で、親の取り直しを止められない
        rc, a = self.w("claim", "--holder", "child")
        record = next((Path(self.tmp.name) / "state" / "work").glob("wt-*")) / "lease.json"
        for forged in ({**json.loads(record.read_text()), "expires_at": time.time() + 10 * 365 * 86400}, {"holder": "child", "expires_at": "x"}):
            record.write_text(json.dumps(forged))
            rc, b = self.w("claim", "--holder", "parent")
            self.assertEqual(rc, 0, b)
        p = subprocess.run([sys.executable, str(WORK), "claim", "--dir", str(self.dir), "--holder", "x", "--lease", "100000"],
                           env=self.env, capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)  # 上限を超える期限は受け付けない

    def test_expired_lease_can_be_taken_over_and_old_holder_is_stopped(self):
        rc, a = self.w("claim", "--holder", "A", "--lease", "0")  # すぐに期限切れ
        rc, b = self.w("claim", "--holder", "B")
        self.assertEqual((rc, b["previous"]["holder"]), (0, "A"))
        rc, r = self.w("renew", "--token", a["token"])  # 旧担当は止まる
        self.assertEqual(rc, 3)
        rc, r = self.w("op", "begin", "--token", a["token"], "--key", "push", "--what", "git push")
        self.assertEqual(rc, 3)
        self.assertEqual(self.w("renew", "--token", b["token"])[0], 0)

    def test_unknown_result_op_is_pending_and_not_restarted(self):
        rc, a = self.w("claim", "--holder", "A")
        self.assertEqual(self.w("op", "begin", "--token", a["token"], "--key", "local-deploy:staging", "--what", "deploy staging")[0], 0)
        self.expire()  # A は結果を記録せずに止まり、期限が切れた（sleep で待つと、遅い機械で揺れた）
        rc, b = self.w("claim", "--holder", "B")
        self.assertEqual([o["key"] for o in b["pending_ops"]], ["local-deploy:staging"])
        rc, again = self.w("op", "begin", "--token", b["token"], "--key", "local-deploy:staging", "--what", "deploy staging")
        self.assertEqual(rc, 4)  # やり直さず、照合を求める
        rc, done = self.w("op", "done", "--token", b["token"], "--key", "local-deploy:staging", "--result", "既存の PR #3 を確認")
        self.assertEqual((rc, done["op"]["state"]), (0, "done"))
        self.assertEqual(self.w("op", "pending")[1]["pending_ops"], [])

    def test_op_begun_by_an_earlier_version_can_still_be_closed(self):
        """前の版は種類を問わず記録した。版を上げた後も、残った操作を op done で閉じられる。"""
        rc, a = self.w("claim", "--holder", "A")
        with open(Path(a["note"]["path"]).parent / "ops.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"key": "pr-create:feat/x", "state": "begun", "at": time.time(), "holder": "A", "what": "gh pr create"}) + "\n")
        self.assertEqual([o["key"] for o in self.w("op", "pending")[1]["pending_ops"]], ["pr-create:feat/x"])
        self.assertEqual(self.w("op", "done", "--token", a["token"], "--key", "pr-create:feat/x", "--result", "PR #3 を確認")[0], 0)
        self.assertEqual(self.w("op", "pending")[1]["pending_ops"], [])

    def test_claim_hands_the_note_to_the_next_holder(self):
        rc, a = self.w("claim", "--holder", "A")
        self.assertFalse(a["note"]["filled"])
        self.assertTrue(Path(a["note"]["evidence_dir"]).is_dir())  # 証拠の置き場（/tmp に置かせない）
        Path(a["note"]["path"]).write_text("# 作業の記録\n\n- 目的: digits を足す\n", encoding="utf-8")
        self.expire()
        rc, b = self.w("claim", "--holder", "B")
        self.assertIn("digits を足す", b["note"]["body"])

    def test_release_frees_the_place(self):
        rc, a = self.w("claim", "--holder", "A")
        self.assertEqual(self.w("release", "--token", a["token"])[0], 0)
        self.assertEqual(self.w("claim", "--holder", "B")[0], 0)


FAKE_GH = """#!/usr/bin/env python3
# 置き場所でアカウントを切り替える gh のラッパーと同じく、カレントディレクトリでアカウントを選ぶ偽物。FAKE_GH_MAP は「接頭辞=login:権限」を ; で並べ、最後の項目は既定。
import os, re, subprocess, sys
cwd = os.getcwd()
login, perm = os.environ["FAKE_GH_DEFAULT"].split(":")
for item in os.environ.get("FAKE_GH_MAP", "").split(";"):
    if item and cwd.startswith(item.split("=")[0]):
        login, perm = item.split("=")[1].split(":")
if sys.argv[1:3] == ["api", "user"]:
    if login == "-":  # gh にログインしていない
        print("To get started with GitHub CLI, please run:  gh auth login", file=sys.stderr); sys.exit(4)
    if login == "403":  # GitHub App・Actions の token。gh は 4xx の本文を --jq に通さず stdout に出す（gh 2.102.0）
        print('{"message": "Resource not accessible by integration", "status": "403"}')
        print("gh: Resource not accessible by integration (HTTP 403)", file=sys.stderr); sys.exit(1)
    print(login)
elif sys.argv[1:3] == ["repo", "view"]:
    # 引数のリポジトリは、実物と同じく SSH の別名を解決せず、そのホストへ接続しに行く。名前は .git の有無まで照合する
    rest = sys.argv[3:]
    arg = next((a for i, a in enumerate(rest) if not a.startswith("-") and not (i and rest[i - 1] in ("--json", "--jq", "-q", "--template", "-t"))), None)
    m = arg and re.match(r"(?:[a-z]+://)?(?:[^@/]+@)?([^/:]+)[:/][^/]+/[^/]+$|[^/]+/[^/]+$", arg)
    host = m and m.group(1)
    if host and host != "github.com":
        print("error connecting to " + host, file=sys.stderr); sys.exit(1)
    if arg and arg not in ("github.com/B16B1RD/demo", os.environ.get("GH_REPO")):  # work.py が渡すのはこの形と GH_REPO だけ
        print("GraphQL: Could not resolve to a Repository", file=sys.stderr); sys.exit(1)
    # 引数なしは gh の既定のリポジトリ。実物と同じく remote の別名を PATH の ssh -G <host> で直し（失敗すれば名前のまま）、GitHub の remote を探す
    if not arg:
        urls = subprocess.run(["git", "remote", "-v"], capture_output=True, text=True).stdout.split()[1::3]
        def gh_host(u):
            m = re.match(r"(?:([a-z+]+)://)?(?:[^@/]+@)?([^/:]+)", u)
            if m.group(1) in (None, "ssh", "git+ssh", "ssh+git"):
                p = subprocess.run(["ssh", "-G", m.group(2)], capture_output=True, text=True)
                return next((l.split()[1] for l in p.stdout.splitlines() if l.startswith("hostname ")), m.group(2)) if p.returncode == 0 else m.group(2)
            return m.group(2)
        if not urls:
            print("no git remotes found", file=sys.stderr); sys.exit(1)
        if not any(gh_host(u).lower() == "github.com" for u in urls):
            print("none of the git remotes configured for this repository point to a known GitHub host.", file=sys.stderr); sys.exit(1)
    # gh repo fork の後は upstream（FAKE_GH_UPSTREAM はそこでの権限）
    if not arg and os.environ.get("FAKE_GH_UPSTREAM") == "NONE":
        print("GraphQL: Could not resolve to a Repository", file=sys.stderr); sys.exit(1)
    if not arg and os.environ.get("FAKE_GH_UPSTREAM"):
        print("parent/demo " + os.environ["FAKE_GH_UPSTREAM"]); sys.exit(0)
    if perm == "NONE":
        print("GraphQL: Could not resolve to a Repository", file=sys.stderr); sys.exit(1)
    print("B16B1RD/demo " + perm)
"""

FAKE_SSH = """#!/usr/bin/env python3
# ssh -G だけを真似る偽物。FAKE_SSH_ALIASES は「別名=HostName」を , で並べる。ほかは名前のまま。出力は本物と同じく複数行
import os, sys
arg = sys.argv[-1]; host = arg.split("@")[-1]  # git と同じく user@host で渡されれば、Match user も効く
aliases = dict(a.split("=") for a in os.environ.get("FAKE_SSH_ALIASES", "").split(",") if a)
print(f"host {host}\\nuser git\\nhostname {aliases.get(arg, aliases.get(host, host)).lower()}\\nport 22")
if "-T" in sys.argv:  # GitHub は鍵のアカウントを stderr に返し、終了コードは 1。FAKE_SSH_KEY が空なら鍵が無い
    keys = dict(a.split("=") for a in os.environ.get("FAKE_SSH_KEYS", "").split(",") if a)  # 別名ごとの鍵（Host ブロックの IdentityFile）
    key = keys.get(host, os.environ.get("FAKE_SSH_KEY", "B16B1RD"))
    print(f"Hi {key}! You've successfully authenticated, but GitHub does not provide shell access." if key
          else "git@github.com: Permission denied (publickey).", file=sys.stderr)
    sys.exit(1 if key else 255)
proxies = dict(a.split("=") for a in os.environ.get("FAKE_SSH_PROXIES", "").split(",") if a)  # 別名=ProxyCommand。無ければ本物と同じく行を出さない
if host in proxies:
    print("proxycommand " + proxies[host])
"""


class TestAccountCheck(unittest.TestCase):
    """外部操作は元の checkout から。食い違い・権限不足は op begin が拒否する（置き場所でアカウントが変わる環境の実測、2026-10-17）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name).resolve()  # macOS の /var は /private/var への symlink。gh が見るカレントディレクトリは実体のパス
        self.repo, self.wt, self.bin = t / "personal" / "demo", t / "elsewhere" / "wt", t / "bin"
        self.bin.mkdir()
        (self.bin / "gh").write_text(FAKE_GH.replace("/usr/bin/env python3", sys.executable))
        (self.bin / "gh").chmod(0o755)
        (self.bin / "ssh").write_text(FAKE_SSH.replace("/usr/bin/env python3", sys.executable))
        (self.bin / "ssh").chmod(0o755)
        # 利用者の git の設定（全体・system・環境変数。insteadOf で git@github.com: が https になる）が origin の形を変えないように
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}  # git の hook の中で回すときの GIT_DIR なども
        self.env = {**env, "BB_WRIT_STATE_DIR": str(t / "state"), "PATH": f"{self.bin}:{os.environ['PATH']}",
                    "FAKE_GH_MAP": f"{t / 'personal'}=B16B1RD:ADMIN", "FAKE_GH_DEFAULT": "work-account:NONE",
                    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        g = lambda *a, cwd=None: subprocess.run(["git", *a], env=self.env, cwd=cwd, check=True, capture_output=True)
        self.repo.mkdir(parents=True)
        g("init", "-q", "-b", "main", cwd=self.repo)
        g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init", cwd=self.repo)
        g("remote", "add", "origin", "https://github.com/B16B1RD/demo.git", cwd=self.repo)
        g("worktree", "add", "-q", "-b", "feat", str(self.wt), cwd=self.repo)
        # op run を w() を通さずに起こす試験のため、最初の HEAD を対象にした審査役の記録を置く（w() は渡した commit の分を置く）
        TestAccountCheck.review_record(self, g("rev-parse", "HEAD", cwd=self.wt).stdout.decode().strip())

    def tearDown(self):
        self.tmp.cleanup()

    def w(self, *args):
        args = with_risk(self, args)
        if "--reviewed" in args and getattr(self, "launcher_reviews", True):  # 審査役の記録との照合を試験しない試験では、記録を先に置く
            TestAccountCheck.review_record(self, args[args.index("--reviewed") + 1])
        p = subprocess.run([sys.executable, str(WORK), *args], env=self.env, capture_output=True, text=True, errors="replace",
                           cwd=self.tmp.name, timeout=300)  # 相対の out を解く場所を決める。止まる退行は止まらずに落とす
        return p.returncode, json.loads(p.stdout)

    def review_record(self, commit, record=None, **over):
        """launcher が読み取り専用の審査役を ok で返したときの、委任の記録の 1 行（launch.py の delegations.jsonl）と out の record.json。
        record を渡すと、record.json だけをその上書きで書く。"""
        p = subprocess.run(["git", "rev-parse", "--verify", "--quiet", commit + "^{commit}"], env=self.env, cwd=self.wt,
                           capture_output=True, text=True)
        if p.returncode or not re.fullmatch(r"[0-9a-f]{7,64}", commit):
            return
        state = Path(self.env["BB_WRIT_STATE_DIR"])
        out = state / "delegations" / f"r{len(list((state / 'delegations').glob('*'))) if (state / 'delegations').is_dir() else 0}"
        rec = {"host": "claude", "cwd": str(self.wt), "write": False, "out": str(out), "head": p.stdout.strip(), "status": "ok", **over}
        rec = {k: v for k, v in rec.items() if v is not ...}  # ... はその欄を書かない
        out.mkdir(parents=True)
        (out / "record.json").write_text(json.dumps({**rec, **(record or {})}, indent=1), encoding="utf-8")
        with open(state / "delegations.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        return str(out)

    def claim(self):
        r = self.w("claim", "--dir", str(self.wt), "--holder", "A")[1]
        if getattr(self, "fill_note", True):  # push・pr に要る記録の必須の欄（この試験の外の約束）。記録そのものを試す試験は偽にする
            Path(r["note"]["path"]).write_text(FILLED_NOTE, encoding="utf-8")
        return r["token"]

    def begin(self, tok, key, *frm):
        return self.w("op", "begin", "--dir", str(self.wt), "--token", tok, "--key", key, "--what", "x", *frm)

    def test_push_from_worktree_with_another_account_is_refused(self):
        tok = self.claim()
        rc, r = self.begin(tok, "push:feat", "--from", str(self.wt))
        self.assertEqual(rc, 5)
        self.assertIn(str(self.repo), r["reason"])  # 元の checkout から実行するよう示す
        self.assertEqual(self.w("op", "pending", "--dir", str(self.wt))[1]["pending_ops"], [])  # 記録されない

    def test_another_account_that_can_also_write_is_refused(self):
        """両方のアカウントが書けるリポジトリでは、黙って別の名前で push・PR が出る。食い違いだけで止める。"""
        self.env["FAKE_GH_DEFAULT"] = "work-account:WRITE"
        tok = self.claim()
        rc, r = self.begin(tok, "pr:feat", "--from", str(self.wt))
        self.assertEqual(rc, 5)
        self.assertIn("食い違う", r["reason"])

    def test_push_from_the_checkout_is_allowed_and_records_the_account(self):
        tok = self.claim()
        head = subprocess.run(["git", "rev-parse", "HEAD"], env=self.env, cwd=self.wt, check=True, capture_output=True, text=True).stdout.strip()
        rc, r = self.begin(tok, "push:feat", "--from", str(self.repo), "--verified", head, "--reviewed", "none:試験の外")
        self.assertEqual((rc, r["op"]["account"]), (0, "B16B1RD"))

    def test_checkout_account_without_write_is_refused_for_push_merge_release(self):
        self.env["FAKE_GH_MAP"] = f"{Path(self.tmp.name).resolve() / 'personal'}=work-account:READ"
        tok = self.claim()
        for key in ("push:feat", "merge:pr-1", "release:v1"):
            rc, r = self.begin(tok, key, "--from", str(self.repo))
            self.assertEqual(rc, 5, key)
            self.assertIn("READ", r["reason"])

    def test_comment_issue_pr_need_no_write(self):
        """GitHub では書き込み権限が無くてもコメント・Issue・fork からの PR ができる（公開リポジトリへの寄与）。"""
        self.env["FAKE_GH_MAP"] = f"{Path(self.tmp.name).resolve() / 'personal'}=work-account:READ"
        tok = self.claim()
        head = subprocess.run(["git", "rev-parse", "HEAD"], env=self.env, cwd=self.wt, check=True, capture_output=True, text=True).stdout.strip()
        for key in ("comment:issue-1", "issue:new", "pr:feat"):
            rc, r = self.begin(tok, key, "--from", str(self.repo), "--verified", head, *decl(key))
            self.assertEqual((rc, r["op"]["account"]), (0, "work-account"), key)

    def test_comment_where_the_repository_is_not_visible_is_refused(self):
        tok = self.claim()
        self.env["FAKE_GH_MAP"] = f"{Path(self.tmp.name).resolve() / 'personal'}=work-account:NONE"
        rc, r = self.begin(tok, "comment:issue-1", "--from", str(self.repo))
        self.assertEqual(rc, 5)

    def test_same_account_with_write_is_allowed_from_the_worktree(self):
        self.env["FAKE_GH_DEFAULT"] = "B16B1RD:WRITE"
        tok = self.claim()
        self.assertEqual(self.begin(tok, "comment:pr-1", "--from", str(self.wt))[0], 0)

    def test_push_to_own_fork_is_allowed_when_upstream_is_the_default(self):
        """gh repo fork の構成（origin が自分の fork、upstream が既定で読むだけ）。push は origin の権限で見る。"""
        self.env["FAKE_GH_UPSTREAM"] = "READ"
        tok = self.claim()
        head = subprocess.run(["git", "rev-parse", "HEAD"], env=self.env, cwd=self.wt, check=True, capture_output=True, text=True).stdout.strip()
        rc, r = self.begin(tok, "push:feat", "--from", str(self.repo), "--verified", head, "--reviewed", "none:試験の外")
        self.assertEqual((rc, r["op"]["account"]), (0, "B16B1RD"))
        self.assertEqual(self.begin(tok, "comment:issue-1", "--from", str(self.repo))[0], 0)
        for key in ("merge:pr-1", "release:v1"):
            rc, r = self.begin(tok, key, "--from", str(self.repo), "--verified", head, *decl(key))
            self.assertEqual(rc, 5, key)  # マージ・リリースは upstream に書けない
            self.assertIn("parent/demo", json.dumps(r, ensure_ascii=False), key)

    def test_pr_checks_the_upstream_not_the_fork(self):
        """PR は gh の既定のリポジトリ（upstream）に出る。見えなければ、自分の fork に書けても止める。"""
        self.env["FAKE_GH_UPSTREAM"] = "NONE"
        tok = self.claim()
        head = subprocess.run(["git", "rev-parse", "HEAD"], env=self.env, cwd=self.wt, check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(self.begin(tok, "pr:feat", "--from", str(self.repo), "--verified", head, "--reviewed", "none:試験の外")[0], 5)

    def test_push_checks_the_push_url_not_the_fetch_url(self):
        """fetch は upstream、push は自分の fork（pushurl）という構成。push の権限は pushurl で見る。"""
        subprocess.run(["git", "remote", "set-url", "origin", "https://github.com/parent/demo.git"], env=self.env, cwd=self.repo, check=True)
        subprocess.run(["git", "remote", "set-url", "--push", "origin", "https://github.com/B16B1RD/demo.git"], env=self.env, cwd=self.repo, check=True)
        tok = self.claim()
        head = subprocess.run(["git", "rev-parse", "HEAD"], env=self.env, cwd=self.wt, check=True, capture_output=True, text=True).stdout.strip()
        rc, r = self.begin(tok, "push:feat", "--from", str(self.repo), "--verified", head, "--reviewed", "none:試験の外")
        self.assertEqual((rc, r["op"]["account"]), (0, "B16B1RD"))

    def test_external_op_needs_from(self):
        tok = self.claim()
        rc, r = self.begin(tok, "push:feat")
        self.assertEqual(rc, 2)
        self.assertIn(str(self.repo), r["reason"])

    def test_non_github_origin_skips_the_check_and_says_so(self):
        subprocess.run(["git", "remote", "set-url", "origin", "https://gitlab.example/x/y.git"], env=self.env, cwd=self.repo, check=True)
        rc, r = self.w("check", "--dir", str(self.wt), "--from", str(self.wt))
        self.assertEqual(rc, 0)
        self.assertIn("飛ばした", r["skipped"])

    def test_push_through_an_ssh_alias_checks_the_real_host(self):
        """~/.ssh/config の別名（git@github.com-work:…）は gh repo view <url> が解決しない。ssh -G で github.com に直して見る。"""
        self.env["FAKE_SSH_ALIASES"] = "github.com-work=github.com,gh443=ssh.github.com"
        tok = self.claim()
        head = subprocess.run(["git", "rev-parse", "HEAD"], env=self.env, cwd=self.wt, check=True, capture_output=True, text=True).stdout.strip()
        for i, url in enumerate(("git@github.com-work:B16B1RD/demo.git", "ssh://git@gh443/B16B1RD/demo.git")):  # 443 番の ssh.github.com を指す別名も
            subprocess.run(["git", "remote", "set-url", "origin", url], env=self.env, cwd=self.repo, check=True)
            rc, r = self.begin(tok, f"push:feat@{i}", "--from", str(self.repo), "--verified", head, "--reviewed", "none:試験の外")
            self.assertEqual((rc, r["op"]["account"]), (0, "B16B1RD"), url)

    def test_every_github_url_form_is_checked_against_the_repository(self):
        """git が github.com へ push する URL の形はどれも確かめる。飛ばすと、権限の無いアカウントでも通る。
        READ で拒否されれば、gh に正しい名前（github.com/B16B1RD/demo）が届いている。偽の gh は .git 付きなど別の名前を拒否する。"""
        self.env["FAKE_SSH_ALIASES"] = "work=github.com,gh443=ssh.github.com,git@mu=github.com"  # mu は ssh の設定の Match user git だけで github.com
        subprocess.run(["git", "config", "url.git@github.com:.insteadOf", "gh:"], env=self.env, cwd=self.repo, check=True)  # git が展開する短縮
        self.env["FAKE_GH_MAP"] = f"{Path(self.tmp.name).resolve() / 'personal'}=work-account:READ"
        for url in ("https://github.com/B16B1RD/demo", "https://GitHub.com/B16B1RD/demo.git", "HTTPS://github.com/B16B1RD/demo/",
                    "git@github.com:B16B1RD/demo.git", "git@github.com:/B16B1RD/demo.git", "git@github.com:B16B1RD/demo.GIT",
                    "git@work:B16B1RD/demo.git", "work:B16B1RD/demo.git", "ssh://git@gh443:443/B16B1RD/demo.git",
                    "SSH://git@work/B16B1RD/demo.git", "git+ssh://git@work/B16B1RD/demo", "ssh+git://git@work/B16B1RD/demo", "https://www.github.com/B16B1RD/demo.git",
                    "https://x:token@github.com/B16B1RD/demo.git", "git@mu:B16B1RD/demo.git", "ssh://git@mu/B16B1RD/demo.git", "gh:B16B1RD/demo.git",
                    "https://github.com./B16B1RD/demo.git"):
            subprocess.run(["git", "remote", "set-url", "origin", url], env=self.env, cwd=self.repo, check=True)
            rc, r = self.w("check", "--dir", str(self.wt), "--from", str(self.repo))
            self.assertEqual(rc, 5, url)
            self.assertIn("READ", r["reason"], url)

    def test_origin_on_another_host_skips_the_check(self):
        self.env["FAKE_SSH_ALIASES"] = "lab=gitlab.com,github.com=ghe.example,git@nu=github.com"  # 名前が github.com でも、ssh の設定で別のホストへ行く
        for url in ("git@gitlab.com:x/y.git", "git@lab:x/y.git", "https://github.com.evil.example/x/y.git",
                    "https://evilgithub.com/x/y.git", "https://ghe.example/x/y.git", str(Path(self.tmp.name) / "bare.git"), "git@github.com:x/y.git", "nu:x/y.git"):  # nu は Match user git でだけ github.com。user を書かない origin には当たらず、名前のまま
            subprocess.run(["git", "remote", "set-url", "origin", url], env=self.env, cwd=self.repo, check=True)
            rc, r = self.w("check", "--dir", str(self.wt), "--from", str(self.repo))
            self.assertEqual(rc, 0, url)
            self.assertIn("飛ばした", r["skipped"], url)

    def test_https_origin_does_not_depend_on_the_ssh_config(self):
        """https の push は ssh を使わない。ssh の設定（github.com を別のホストへ向ける）にも、ssh の故障にも左右されない。"""
        self.env["FAKE_SSH_ALIASES"] = "github.com=ghe.example"
        self.env["FAKE_GH_MAP"] = f"{Path(self.tmp.name).resolve() / 'personal'}=work-account:READ"
        rc, r = self.w("check", "--dir", str(self.wt), "--from", str(self.repo))
        self.assertEqual(rc, 5)
        self.assertIn("READ", r["reason"])
        (self.bin / "ssh").write_text("#!/bin/sh\necho 'ssh: bad configuration' >&2\nexit 255\n")
        rc, r = self.w("check", "--dir", str(self.wt), "--from", str(self.repo))
        self.assertIn("READ", r["reason"])

    def test_proxy_command_alias_is_refused_unless_it_names_github(self):
        """HostName を書かず ProxyCommand で github.com へ繋ぐ別名は、ssh -G が別名のまま返す。接続先が分からないので止める。"""
        self.env["FAKE_SSH_ALIASES"] = "corp=github.com"
        self.env["FAKE_SSH_PROXIES"] = "pc=ssh -W github.com:22 bastion,corp=connect -H proxy %h %p"
        self.env["FAKE_GH_MAP"] = f"{Path(self.tmp.name).resolve() / 'personal'}=work-account:READ"
        for url, words in (("pc:B16B1RD/demo.git", "ProxyCommand"), ("git@corp:B16B1RD/demo.git", "READ")):  # HostName が github.com なら普通に確かめる
            subprocess.run(["git", "remote", "set-url", "origin", url], env=self.env, cwd=self.repo, check=True)
            rc, r = self.w("check", "--dir", str(self.wt), "--from", str(self.repo))
            self.assertEqual(rc, 5, url)
            self.assertIn(words, r["reason"], url)

    def test_unresolvable_ssh_host_is_refused_not_skipped(self):
        """ssh -G が失敗すると GitHub かどうか分からない。確かめを飛ばさずに止める。"""
        (self.bin / "ssh").write_text("#!/bin/sh\necho 'ssh: bad configuration' >&2\nexit 255\n")
        subprocess.run(["git", "remote", "set-url", "origin", "git@work:B16B1RD/demo.git"], env=self.env, cwd=self.repo, check=True)
        for url in ("git@work:B16B1RD/demo.git", "git@github.com:B16B1RD/demo.git", "ssh://git@work/B16B1RD/demo.git"):  # 名前が github.com でも、ssh の設定で別のホストになりうる
            subprocess.run(["git", "remote", "set-url", "origin", url], env=self.env, cwd=self.repo, check=True)
            rc, r = self.w("check", "--dir", str(self.wt), "--from", str(self.repo))
            self.assertEqual(rc, 5, url)
            self.assertIn("bad configuration", r["reason"], url)
        # push 以外は gh が操作する。gh は ssh -G に失敗すると名前のまま判定するので、名前が github.com なら gh も GitHub に出る
        tok = self.claim()
        self.env["FAKE_GH_MAP"] = f"{Path(self.tmp.name).resolve() / 'personal'}=work-account:NONE"
        subprocess.run(["git", "remote", "set-url", "origin", "git@github.com:B16B1RD/demo.git"], env=self.env, cwd=self.repo, check=True)
        for key in ("comment:issue-1", "pr:feat", "merge:pr-1"):
            self.assertEqual(self.begin(tok, key, "--from", str(self.repo))[0], 5, key)

    def test_alias_origin_is_checked_for_other_operations_too(self):
        """push 以外も、確かめるかどうかは origin で決める。別名の origin で飛ばすと、見えないリポジトリへのコメントも通る。"""
        self.env["FAKE_SSH_ALIASES"] = "work=github.com"
        self.env["FAKE_GH_MAP"] = f"{Path(self.tmp.name).resolve() / 'personal'}=work-account:NONE"
        subprocess.run(["git", "remote", "set-url", "origin", "git@work:B16B1RD/demo.git"], env=self.env, cwd=self.repo, check=True)
        tok = self.claim()
        self.assertEqual(self.begin(tok, "comment:issue-1", "--from", str(self.repo))[0], 5)

    def head_of_wt(self):
        return subprocess.run(["git", "rev-parse", "HEAD"], env=self.env, cwd=self.wt, check=True, capture_output=True, text=True).stdout.strip()

    def set_origin(self, *args):
        subprocess.run(["git", "remote", "set-url", *args], env=self.env, cwd=self.repo, check=True)

    def test_from_must_be_the_same_repository(self):
        """--from に git でない場所や別のリポジトリを渡すと、そこのアカウントで確かめて通ってしまう。元の checkout と同じリポジトリに限る。"""
        other = Path(self.tmp.name).resolve() / "personal" / "other"
        other.mkdir()
        subprocess.run(["git", "init", "-q"], env=self.env, cwd=other, check=True)
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/B16B1RD/demo.git"], env=self.env, cwd=other, check=True)
        plain = Path(self.tmp.name).resolve() / "personal" / "plain"
        plain.mkdir()
        tok = self.claim()
        for frm in (other, plain, Path(self.tmp.name) / "missing"):
            rc, r = self.begin(tok, f"comment:issue-{frm.name}", "--from", str(frm))
            self.assertEqual(rc, 2, frm)
            self.assertIn(str(self.repo), r["reason"], frm)
        self.assertEqual(self.begin(tok, "comment:issue-wt", "--from", str(self.repo))[0], 0)

    def test_mismatched_account_is_refused_for_every_external_operation(self):
        self.env["FAKE_GH_DEFAULT"] = "work-account:WRITE"
        tok = self.claim()
        for kind in ("push", "pr", "comment", "merge", "release", "issue"):
            rc, r = self.begin(tok, f"{kind}:x", "--from", str(self.wt), "--verified", self.head_of_wt(), *decl(kind))
            self.assertEqual(rc, 5, kind)
            self.assertIn("食い違う", r["reason"], kind)

    def test_checkout_account_unknown_or_blind_is_a_mismatch(self):
        """元の checkout で gh にログインしていない・対象が見えない場合も、比べずに通さない。"""
        self.env["FAKE_GH_DEFAULT"] = "B16B1RD:WRITE"
        tok = self.claim()
        for i, base in enumerate(("-:ADMIN", "work-account:NONE")):
            self.env["FAKE_GH_MAP"] = f"{Path(self.tmp.name).resolve() / 'personal'}={base}"
            rc, r = self.begin(tok, f"comment:issue-{i}", "--from", str(self.wt))
            self.assertEqual(rc, 5, base)
            self.assertIn("食い違う", r["reason"], base)

    def test_maintain_can_publish_and_triage_cannot(self):
        tok = self.claim()
        for i, (perm, code) in enumerate((("MAINTAIN", 0), ("TRIAGE", 5))):
            self.env["FAKE_GH_MAP"] = f"{Path(self.tmp.name).resolve() / 'personal'}=B16B1RD:{perm}"
            self.assertEqual(self.begin(tok, f"push:feat@{i}", "--from", str(self.repo), "--verified", self.head_of_wt(), "--reviewed", "none:試験の外")[0], code, perm)

    def test_other_operations_follow_the_remotes_gh_can_choose(self):
        """push 以外は gh の既定のリポジトリに出る。origin が GitHub でなくても、GitHub の remote があれば確かめる。"""
        self.env["FAKE_GH_UPSTREAM"] = "NONE"  # gh の既定（upstream）が見えない
        add = lambda *a: subprocess.run(["git", "remote", *a], env=self.env, cwd=self.repo, check=True)
        add("add", "upstream", "https://github.com/parent/demo.git")
        tok = self.claim()
        self.set_origin("origin", "https://gitlab.com/x/y.git")
        self.assertEqual(self.begin(tok, "comment:a", "--from", str(self.repo))[0], 5)  # GitLab の origin と GitHub の upstream
        add("remove", "origin")
        self.assertEqual(self.begin(tok, "comment:b", "--from", str(self.repo))[0], 5)  # origin が無い
        add("rename", "upstream", "origin")
        self.set_origin("--push", "origin", "https://gitlab.com/x/y.git")  # pushurl だけが GitHub でないミラー
        self.assertEqual(self.begin(tok, "comment:c", "--from", str(self.repo))[0], 5)
        self.set_origin("origin", "https://gitlab.com/x/y.git")
        rc, r = self.w("op", "begin", "--dir", str(self.wt), "--token", tok, "--key", "comment:d", "--what", "x", "--from", str(self.repo))
        self.assertEqual(rc, 0)  # GitHub の remote が 1 つも無ければ飛ばす
        self.assertTrue(self.w("check", "--dir", str(self.wt), "--from", str(self.repo))[1]["skipped"])

    def test_other_operations_follow_gh_not_the_git_ssh(self):
        """push 以外は gh が既定のリポジトリを決める。gh は git の ssh（core.sshCommand）ではなく PATH の ssh で別名を直す。"""
        wrap = self.bin / "ssh-plain"
        wrap.write_text(f"#!/bin/sh\nFAKE_SSH_ALIASES= exec {self.bin / 'ssh'} \"$@\"\n")
        wrap.chmod(0o755)
        self.env["FAKE_SSH_ALIASES"] = "ghalias=github.com"
        self.env["FAKE_GH_MAP"] = f"{Path(self.tmp.name).resolve() / 'personal'}=work-account:NONE"
        self.set_origin("origin", "git@ghalias:B16B1RD/demo.git")
        subprocess.run(["git", "config", "core.sshCommand", str(wrap)], env=self.env, cwd=self.repo, check=True)
        tok = self.claim()
        for key in ("comment:a", "pr:a", "issue:a"):
            self.assertEqual(self.begin(tok, key, "--from", str(self.repo))[0], 5, key)

    def test_gh_repo_is_the_target_of_other_operations(self):
        """GH_REPO があると gh の issue・pr はそこに出る。remote が GitHub でなくても、そのリポジトリを確かめる。"""
        self.set_origin("origin", "https://gitlab.com/x/y.git")
        self.env["GH_REPO"] = "B16B1RD/demo"
        tok = self.claim()
        self.env["FAKE_GH_MAP"] = f"{Path(self.tmp.name).resolve() / 'personal'}=work-account:NONE"
        rc, r = self.begin(tok, "comment:a", "--from", str(self.repo))
        self.assertEqual(rc, 5)
        self.assertIn("Could not resolve", r["reason"])  # gh のエラーを理由に書く
        self.env["FAKE_GH_MAP"] = f"{Path(self.tmp.name).resolve() / 'personal'}=B16B1RD:READ"
        rc, r = self.begin(tok, "comment:b", "--from", str(self.repo))
        self.assertEqual((rc, r["op"]["account"]), (0, "B16B1RD"))

    def test_ssh_push_checks_the_key_account(self):
        """SSH の push は鍵のアカウントで行われる。gh のアカウントと違えば止める（別名で鍵を切り替える構成）。"""
        self.env["FAKE_SSH_ALIASES"] = "work=github.com"
        self.set_origin("origin", "git@work:B16B1RD/demo.git")
        tok = self.claim()
        for i, (key, code) in enumerate((("B16B1RD", 0), ("b16b1rd", 0), ("work-account", 5), ("B16B1RD/demo", 5), ("", 5))):  # deploy key・鍵なし
            self.env["FAKE_SSH_KEY"] = key
            rc, r = self.begin(tok, f"push:feat@{i}", "--from", str(self.repo), "--verified", self.head_of_wt(), "--reviewed", "none:試験の外")
            self.assertEqual(rc, code, key)
            if code:
                self.assertIn("鍵のアカウント" + (" " + key if key else "が分からない"), r["reason"], key)
                self.assertIn("Permission denied" if not key else "違う", r["reason"], key)
        self.env["FAKE_SSH_KEY"], self.env["FAKE_SSH_KEYS"] = "work-account", "work=B16B1RD"  # 鍵は別名の Host ブロックで決まる
        self.assertEqual(self.begin(tok, "push:feat@alias-key", "--from", str(self.repo), "--verified", self.head_of_wt(), "--reviewed", "none:試験の外")[0], 0)
        self.set_origin("origin", "https://github.com/B16B1RD/demo.git")  # https の push は鍵を使わない
        self.assertEqual(self.begin(tok, "push:feat@https", "--from", str(self.repo), "--verified", self.head_of_wt(), "--reviewed", "none:試験の外")[0], 0)

    def test_git_ssh_command_and_core_ssh_command_are_used(self):
        """git が push に使う ssh（GIT_SSH_COMMAND > core.sshCommand > GIT_SSH）で解決する。既定の ssh では別名が分からない。"""
        wrap = self.bin / "ssh-work"
        wrap.write_text(f"#!/bin/sh\nFAKE_SSH_ALIASES=only=github.com FAKE_SSH_KEY=B16B1RD exec {self.bin / 'ssh'} \"$@\"\n")
        self.env["FAKE_SSH_KEY"] = ""  # 既定の ssh には鍵が無い。-T も git の ssh で繋ぐ
        wrap.chmod(0o755)
        self.set_origin("origin", "git@only:B16B1RD/demo.git")
        tok = self.claim()
        self.assertIn("skipped", self.w("check", "--dir", str(self.wt), "--from", str(self.repo))[1])
        subprocess.run(["git", "config", "core.sshCommand", f"{wrap} -o X=y"], env=self.env, cwd=self.repo, check=True)
        rc, r = self.begin(tok, "push:feat@core", "--from", str(self.repo), "--verified", self.head_of_wt(), "--reviewed", "none:試験の外")
        self.assertEqual((rc, r["op"]["account"]), (0, "B16B1RD"))  # 飛ばした場合は account が無い
        subprocess.run(["git", "config", "core.sshCommand", str(self.bin / "ssh")], env=self.env, cwd=self.repo, check=True)
        self.env["GIT_SSH_COMMAND"] = str(wrap)  # 環境変数が設定より優先
        rc, r = self.begin(tok, "push:feat@env", "--from", str(self.repo), "--verified", self.head_of_wt(), "--reviewed", "none:試験の外")
        self.assertEqual((rc, r["op"]["account"]), (0, "B16B1RD"))  # 飛ばした場合は account が無い
        del self.env["GIT_SSH_COMMAND"]
        subprocess.run(["git", "config", "core.sshCommand", str(wrap)], env=self.env, cwd=self.repo, check=True)
        self.env["GIT_SSH"] = str(self.bin / "ssh")  # 設定が GIT_SSH より優先
        rc, r = self.begin(tok, "push:feat@core-over-git-ssh", "--from", str(self.repo), "--verified", self.head_of_wt(), "--reviewed", "none:試験の外")
        self.assertEqual((rc, r["op"]["account"]), (0, "B16B1RD"))
        subprocess.run(["git", "config", "--unset", "core.sshCommand"], env=self.env, cwd=self.repo, check=True)
        self.env["GIT_SSH"] = str(wrap)
        rc, r = self.begin(tok, "push:feat@git-ssh", "--from", str(self.repo), "--verified", self.head_of_wt(), "--reviewed", "none:試験の外")
        self.assertEqual((rc, r["op"]["account"]), (0, "B16B1RD"))  # 飛ばした場合は account が無い

    def test_internal_ops_need_no_account(self):
        tok = self.claim()
        self.assertEqual(self.begin(tok, "local-note:x")[0], 0)
        self.assertEqual(self.begin(tok, "local-merge-base:x")[0], 0)  # 外部に出ない操作は local- で始める
        self.assertEqual(self.begin(tok, "local:pending")[0], 0)

    def test_external_looking_kinds_must_use_the_listed_names(self):
        """一覧に無い種類の鍵は、--from も権限も確かめずに記録されていた。書き間違い・言い換えを列挙しても漏れるので、
        外部操作の 6 つと local- で始まる種類のほかは止める。"""
        tok = self.claim()
        for key in ("Push:origin/main", "gh-pr:feat", "pr-create:feat", "issue_comment:1", "MERGE:pr-1", "push/origin/main@abc",
                    "pr#12", "gh pr:feat", "pull-request:x", "tag:v1", " push:x", "ｐｕｓｈ:x", "note:x", "localpush:x"):
            rc, r = self.begin(tok, key, "--from", str(self.repo))
            self.assertEqual(rc, 2, key)
            self.assertIn("push・pr・comment・merge・release・issue", r["reason"], key)
            self.assertIn("local-", r["reason"], key)
        self.assertEqual(self.w("op", "pending", "--dir", str(self.wt))[1]["pending_ops"], [])  # 記録されない

    def test_unknown_gh_account_is_refused(self):
        """gh api user だけが失敗する token では、アカウントが空のまま比べて通っていた。アカウントが分からなければ止める。"""
        tok = self.claim()
        for key in ("push:feat-a", "comment:issue-a", "push:feat-b", "comment:issue-b"):
            self.env["FAKE_GH_MAP"] = f"{Path(self.tmp.name).resolve() / 'personal'}={'-' if key.endswith('a') else '403'}:ADMIN"
            rc, r = self.begin(tok, key, "--from", str(self.repo), "--verified", self.head_of_wt(), *decl(key))
            self.assertEqual(rc, 5, key)
            self.assertIn("gh のアカウントが分からない", r["reason"], key)


class TestVerifiedCommit(unittest.TestCase):
    """公開する操作は、検証した commit が作業場所の HEAD と同じときだけ通す（plan-v3 6.2。検証の後に変わった成果物を出さない）。"""

    setUp, tearDown, w, claim, begin = (TestAccountCheck.setUp, TestAccountCheck.tearDown, TestAccountCheck.w,
                                        TestAccountCheck.claim, TestAccountCheck.begin)

    def head(self):
        return subprocess.run(["git", "rev-parse", "HEAD"], env=self.env, cwd=self.wt, check=True, capture_output=True, text=True).stdout.strip()

    def test_publish_needs_the_verified_commit(self):
        tok = self.claim()
        rc, r = self.begin(tok, "push:feat", "--from", str(self.repo))
        self.assertEqual(rc, 2)
        self.assertIn(self.head(), r["reason"])  # 今の HEAD を示す

    def test_publish_after_the_tree_changed_is_refused(self):
        verified = self.head()
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "after"], env=self.env, cwd=self.wt, check=True)
        tok = self.claim()
        rc, r = self.begin(tok, "pr:feat", "--from", str(self.repo), "--verified", verified, "--reviewed", "none:試験の外")
        self.assertEqual(rc, 6)
        self.assertIn(self.head(), r["reason"])
        self.assertEqual(self.w("op", "pending", "--dir", str(self.wt))[1]["pending_ops"], [])  # 記録されない

    def test_publish_of_the_verified_commit_is_allowed_and_recorded(self):
        tok = self.claim()
        rc, r = self.begin(tok, "push:feat", "--from", str(self.repo), "--verified", self.head()[:10], "--reviewed", self.head()[:10])  # 短い SHA も受ける
        self.assertEqual((rc, r["op"]["verified"], r["op"]["reviewed"]), (0, self.head(), self.head()))

    def test_comment_needs_no_verified_commit(self):
        tok = self.claim()
        self.assertEqual(self.begin(tok, "comment:pr-1", "--from", str(self.repo))[0], 0)



class TestReviewedCommit(unittest.TestCase):
    """push・pr は、届ける前の審査を通した commit（か、要らない理由）を宣言しないと出せない。"""
    setUp, tearDown, w, claim, begin, head = (TestAccountCheck.setUp, TestAccountCheck.tearDown, TestAccountCheck.w,
                                              TestAccountCheck.claim, TestAccountCheck.begin, TestVerifiedCommit.head)
    review_record = TestAccountCheck.review_record

    def commit(self, name):
        (self.wt / name).parent.mkdir(parents=True, exist_ok=True)
        (self.wt / name).write_text(name, encoding="utf-8")
        for cmd in (["git", "add", name], ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", name]):
            subprocess.run(cmd, env=self.env, cwd=self.wt, check=True)

    def publish(self, tok, key, *reviewed):
        return self.begin(tok, key, "--from", str(self.repo), "--verified", self.head(), *reviewed)

    def pending(self):
        return [e["key"] for e in self.w("op", "pending", "--dir", str(self.wt))[1]["pending_ops"]]

    def ops_file(self):
        return b"".join(p.read_bytes() for p in sorted((Path(self.tmp.name).resolve() / "state" / "work").glob("*/ops.jsonl")))

    def git(self, *args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], env=self.env, cwd=self.wt, check=True, capture_output=True)

    def pushed(self, tok, key):
        self.assertEqual(self.publish(tok, key, "--reviewed", self.head())[0], 0)
        self.assertEqual(self.w("op", "done", "--dir", str(self.wt), "--token", tok, "--key", key, "--result", "ok")[0], 0)

    def test_push_and_pr_need_the_risk_and_a_medium_or_high_risk_needs_a_review(self):
        # 危険度も理由も書かずに interrogate を省いて push できた（#17）
        self.auto_risk = False
        tok = self.claim()
        before = self.ops_file()
        for key in ("push:feat@1", "pr:feat"):
            for risk in ((), ("--risk", "中"), ("--risk", "中: "), ("--risk", "medium:普通のコード"), ("--risk", "低 理由")):
                rc, r = self.publish(tok, key, "--reviewed", self.head(), *risk)
                self.assertEqual(rc, 2, (key, risk))
                self.assertIn("--risk", r["reason"], (key, risk))
            for level in ("中", "高"):  # 中・高は interrogate を通すので、審査が要らない宣言とは並ばない
                rc, r = self.publish(tok, key, "--reviewed", "none:文書だけ", "--risk", f"{level}:普通のコード")
                self.assertEqual(rc, 2, (key, level))
                self.assertIn(f"危険度が{level}の変更は interrogate を通す", r["reason"], (key, level))
        self.assertEqual(self.ops_file(), before)
        self.assertEqual(self.publish(tok, "push:feat@1", "--reviewed", "none:文書だけ", "--risk", "低:文書だけ")[0], 0)
        self.assertEqual(self.publish(tok, "pr:feat", "--reviewed", self.head(), "--risk", "中: 普通のコード")[0], 0)
        self.assertEqual(self.publish(tok, "push:feat@2", "--reviewed", self.head(), "--risk", "高:外部操作")[0], 0)
        recs = {json.loads(x)["key"]: json.loads(x) for x in self.ops_file().decode().splitlines()}
        self.assertEqual((recs["push:feat@1"]["risk"], recs["pr:feat"]["risk"]), ("低: 文書だけ", "中: 普通のコード"))

    def test_first_push_and_pr_without_a_declaration_are_refused_and_not_recorded(self):
        tok = self.claim()
        before = self.ops_file()
        for key in ("push:feat@1", "pr:feat"):
            rc, r = self.publish(tok, key)
            self.assertEqual(rc, 2, key)
            self.assertIn("--reviewed", r["reason"], key)
            self.assertIn("終えた push・pr の記録が無い", r["reason"], key)
        self.assertEqual(self.ops_file(), before)
        self.assertEqual(self.publish(tok, "push:feat@1", "--reviewed", self.head())[0], 0)  # 言い直せば同じ鍵で通る

    def test_refusal_lists_the_files_changed_since_the_last_finished_push(self):
        tok = self.claim()
        self.commit("before.txt")
        self.pushed(tok, "push:feat@1")
        self.commit("between.txt")
        self.pushed(tok, "push:feat@2")
        self.commit("mid.txt")
        self.assertEqual(self.publish(tok, "push:feat@begun", "--reviewed", self.head())[0], 0)  # 終えていない push は前回に数えない
        self.commit("tests/test_new.py")
        self.commit("src/fix.py")
        rc, r = self.publish(tok, "push:feat@3")
        self.assertEqual(rc, 2)
        for name in ("mid.txt", "tests/test_new.py", "src/fix.py"):
            self.assertIn(name, r["reason"])
        for name in ("before.txt", "between.txt"):
            self.assertNotIn(name, r["reason"])

    def test_the_files_changed_are_shown_when_the_push_is_allowed(self):
        tok = self.claim()
        self.pushed(tok, "push:feat@1")
        self.commit("tests/test_new.py")
        rc, r = self.publish(tok, "push:feat@2", "--reviewed", self.head())
        self.assertEqual(rc, 0)
        self.assertIn("tests/test_new.py", r["changed"])

    def test_push_and_pr_of_a_commit_after_the_review_are_refused(self):
        reviewed = self.head()
        self.commit("docs.md")  # 試験に触れない変更でも、審査の後に変えたら出せない
        tok = self.claim()
        before = self.ops_file()
        for key in ("push:feat@1", "pr:feat"):
            rc, r = self.publish(tok, key, "--reviewed", reviewed)
            self.assertEqual(rc, 6, key)
            self.assertIn(self.head(), r["reason"], key)
            self.assertIn("docs.md", r["reason"], key)
        self.assertEqual(self.ops_file(), before)  # 拒否は何も記録しない

    def test_a_moving_ref_an_unknown_commit_or_a_misspelled_none_is_refused(self):
        tok = self.claim()
        self.git("branch", "cafe1234", "HEAD")  # 16 進だけの名前のブランチ
        before = self.ops_file()
        for value in ("HEAD", "@", "main", "deadbeef", "cafe1234", "None: 文書だけ", " none: 文書だけ", "none 文書だけ"):
            rc, r = self.publish(tok, "push:feat@1", "--reviewed", value)
            self.assertEqual(rc, 2, value)
        self.assertEqual(self.ops_file(), before)

    def test_a_later_push_without_a_declaration_is_refused_even_if_no_test_changed(self):
        tok = self.claim()
        self.pushed(tok, "pr:feat")  # pr を終えた記録も前回に数える
        self.commit("docs.md")
        rc, r = self.publish(tok, "push:feat@2")
        self.assertEqual(rc, 2)
        self.assertIn("docs.md", r["reason"])

    def test_a_merge_is_not_counted_as_the_last_push(self):
        tok = self.claim()
        self.pushed(tok, "push:feat@1")
        self.commit("before_merge.txt")
        self.assertEqual(self.begin(tok, "merge:pr-1", "--from", str(self.repo), "--verified", self.head())[0], 0)
        self.assertEqual(self.w("op", "done", "--dir", str(self.wt), "--token", tok, "--key", "merge:pr-1", "--result", "ok")[0], 0)
        rc, r = self.publish(tok, "push:feat@2")
        self.assertIn("before_merge.txt", r["reason"])

    def test_the_last_push_is_chosen_by_when_it_finished(self):
        tok = self.claim()
        self.assertEqual(self.publish(tok, "push:feat@a", "--reviewed", self.head())[0], 0)
        self.commit("after_a.txt")
        self.pushed(tok, "push:feat@b")
        self.assertEqual(self.w("op", "done", "--dir", str(self.wt), "--token", tok, "--key", "push:feat@a", "--result", "ok")[0], 0)
        rc, r = self.publish(tok, "push:feat@c")
        self.assertIn("after_a.txt", r["reason"])  # 最後に終えたのは a

    def test_a_rewritten_history_is_noted(self):
        tok = self.claim()
        self.commit("old.txt")
        self.pushed(tok, "push:feat@1")
        self.git("reset", "-q", "--hard", "HEAD~1")
        self.commit("new.txt")
        rc, r = self.publish(tok, "push:feat@2")
        self.assertEqual(rc, 2)
        self.assertIn("祖先でない", r["reason"])

    def test_no_review_needs_a_reason_and_is_recorded(self):
        tok = self.claim()
        for key in ("push:feat@empty", "pr:feat-empty"):
            self.assertEqual(self.publish(tok, key, "--reviewed", "none:  ")[0], 2, key)
        self.assertEqual(self.pending(), [])
        for key in ("push:feat@docs", "pr:feat"):
            rc, r = self.publish(tok, key, "--reviewed", "none: 文書だけ")
            self.assertEqual((rc, r["op"]["reviewed"]), (0, "none: 文書だけ"), key)

    def test_the_reviewed_commit_is_recorded_in_full_for_push_and_pr(self):
        tok = self.claim()
        for key in ("push:feat@1", "pr:feat"):
            rc, r = self.publish(tok, key, "--reviewed", self.head()[:10])
            self.assertEqual((rc, r["op"]["reviewed"]), (0, self.head()), key)

    def test_a_commit_no_reviewer_was_launched_on_is_refused(self):
        """Issue #14: 子を立てずに、書き手が HEAD を --reviewed に書いても通さない。"""
        self.launcher_reviews = False
        tok = self.claim()
        self.commit("tests/test_new.py")
        before = self.ops_file()
        for key in ("push:feat@1", "pr:feat"):
            rc, r = self.publish(tok, key, "--reviewed", self.head())
            self.assertEqual(rc, 6, key)
            self.assertIn("審査役の記録が無い", r["reason"], key)
        self.assertEqual(self.ops_file(), before)  # 拒否は何も記録しない

    def test_a_commit_a_read_only_reviewer_returned_ok_on_is_allowed_and_the_record_is_kept(self):
        self.launcher_reviews = False
        tok = self.claim()
        self.commit("tests/test_new.py")
        outs = [self.review_record(self.head()), self.review_record(self.head())]  # A と B
        rc, r = self.publish(tok, "push:feat@1", "--reviewed", self.head()[:10])
        self.assertEqual((rc, r["op"]["reviews"]), (0, outs), r)

    def test_records_of_another_commit_a_failed_or_unrun_review_or_a_writing_child_do_not_count(self):
        self.launcher_reviews = False
        tok = self.claim()
        before_fix = self.head()
        self.commit("tests/test_new.py")
        self.review_record(before_fix)  # 直す前の commit の審査
        h = self.head()
        for over in ({"status": "not-run"}, {"status": "failed"}, {"status": "invalid-report"}, {"status": "quota"}, {"status": "unavailable"},
                     {"status": ...},
                     {"write": True}, {"write": ...}, {"write": 0}, {"head": None}, {"head": h[:12]}, {"head": h.upper()}, {"out": ...}):
            self.review_record(h, **over)
        for record in ({"head": None}, {"status": "failed"}, {"write": True}, {"out": "/elsewhere"}):  # 行だけ書き換えた記録
            self.review_record(h, record=record)
        self.review_record(h, out="/a\x00b")  # 開けない名前の out（os.open が ValueError）
        gone = self.review_record(h)  # out の record.json が無い
        (Path(gone) / "record.json").unlink()
        fifo = self.review_record(h)  # out の record.json が FIFO（開くと止まる）
        (Path(fifo) / "record.json").unlink()
        os.mkfifo(Path(fifo) / "record.json")
        delegations = Path(self.env["BB_WRIT_STATE_DIR"]) / "delegations.jsonl"
        with open(delegations, "ab") as f:
            f.write(b'[1]\nnull\n"x"\n' + b'{"head": "' + h.encode() + b'", "status": "o\n' + b'{"x": "\xff"}\n' + b"[" * 100000 + b"\n")
        rc, r = self.publish(tok, "push:feat@1", "--reviewed", h)
        self.assertEqual(rc, 6, r)
        self.assertIn("record.json を読めない行が 6 行", r["reason"])  # 書きかけ・UTF-8 でない・深すぎる行と、record.json の無い・FIFO・開けない名前の行
        rel = Path(self.tmp.name) / "rel"  # 相対の out（work.py の場所で解かれる。launcher は絶対パスを書く）
        rel.mkdir()
        rel_out = "rel"  # w() の work.py は self.tmp.name で動く
        (rel / "record.json").write_text(json.dumps({"head": h, "status": "ok", "write": False, "out": rel_out}))
        with open(delegations, "a", encoding="utf-8") as f:
            f.write(json.dumps({"head": h, "status": "ok", "write": False, "out": rel_out}) + "\n")
        self.assertEqual(self.publish(tok, "push:feat@1", "--reviewed", h)[0], 6)
        delegations.chmod(0)  # 委任の記録そのものを読めない
        try:
            if not os.access(delegations, os.R_OK):  # root では権限で止められない
                rc, r = self.publish(tok, "push:feat@1", "--reviewed", h)
                self.assertEqual(rc, 6, r)
                self.assertIn("を読めない", r["reason"])
        finally:
            delegations.chmod(0o644)
        self.review_record(h)
        self.assertEqual(self.publish(tok, "push:feat@1", "--reviewed", h)[0], 0)

    def test_no_delegation_record_at_all_is_a_refusal_not_a_crash(self):
        self.launcher_reviews = False
        tok = self.claim()
        (Path(self.env["BB_WRIT_STATE_DIR"]) / "delegations.jsonl").unlink()  # setUp が置いた記録も無い（初めての利用）
        rc, r = self.publish(tok, "push:feat@1", "--reviewed", self.head())
        self.assertEqual(rc, 6, r)
        self.assertIn("審査役の記録が無い", r["reason"])
        self.assertNotIn("読めない", r["reason"])  # 無いのは壊れた印でない
        delegations = Path(self.env["BB_WRIT_STATE_DIR"]) / "delegations.jsonl"
        broken_link = lambda p: os.symlink(Path(self.tmp.name) / "no-such-file", p)
        for make in (os.mkfifo, Path.mkdir, broken_link):  # 読めない形。FIFO は開くと止まる
            with self.subTest(make=getattr(make, "__name__", "")):
                make(delegations)
                rc, r = self.publish(tok, "push:feat@1", "--reviewed", self.head())
                self.assertEqual(rc, 6, r)
                self.assertIn("を読めない", r["reason"])
                (delegations.rmdir if delegations.is_dir() else delegations.unlink)()

    def test_op_run_refuses_a_commit_no_reviewer_was_launched_on_and_does_not_run_the_command(self):
        self.launcher_reviews = False
        tok = self.claim()
        self.commit("tests/test_new.py")
        marker = Path(self.tmp.name) / "pushed"
        rc, r = self.w("op", "run", "--dir", str(self.wt), "--token", tok, "--key", "push:feat@1", "--what", "x", "--from", str(self.repo),
                       "--verified", self.head(), "--reviewed", self.head(), "--", sys.executable, "-c", f"open({str(marker)!r}, 'w')")
        self.assertEqual(rc, 6, r)
        self.assertIn("審査役の記録が無い", r["reason"])
        self.assertFalse(marker.exists())
        self.review_record(self.head())
        rc, r = self.w("op", "run", "--dir", str(self.wt), "--token", tok, "--key", "push:feat@1", "--what", "x", "--from", str(self.repo),
                       "--verified", self.head(), "--reviewed", self.head(), "--", sys.executable, "-c", f"open({str(marker)!r}, 'w')")
        self.assertEqual(rc, 0, r)
        self.assertTrue(marker.exists())

    def test_no_review_needs_no_reviewer_record(self):
        self.launcher_reviews = False
        tok = self.claim()
        self.assertEqual(self.publish(tok, "push:feat@1", "--reviewed", "none: 文書だけ")[0], 0)

    def test_merge_release_comment_and_issue_need_no_declaration(self):
        tok = self.claim()
        for key in ("merge:pr-1", "release:v1"):
            self.assertEqual(self.begin(tok, key, "--from", str(self.repo), "--verified", self.head())[0], 0, key)
        for key in ("comment:pr-1", "issue:new"):
            self.assertEqual(self.begin(tok, key, "--from", str(self.repo))[0], 0, key)

def permissions_bind():
    """chmod 0 のファイルを開けない（権限の制限が効く）か。root や CAP_DAC_OVERRIDE では効かない。"""
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "p"
        f.write_text("x")
        f.chmod(0)
        try:
            open(f).close()
            return False
        except PermissionError:
            return True
        finally:
            f.chmod(0o644)


PERMISSIONS_BIND = permissions_bind()


TRIAGE_LINE = "- 指摘の扱い（審査の指摘ごとに、対応する・記録のみ・退ける と理由を、直す前に）:"


PREMISE_LINE = "- 仕組みの要否（審査が 3 周に達したら、直しを書く前に、目的の値で仕組みあり・なし・別の形を比べて）:"


class TestPremiseAfterRounds(unittest.TestCase):
    """task start の作業場所を --cwd にして審査役・確かめ役を立てた、互いに異なる commit が 3 つに達したら、審査を通した commit の
    push・pr は、記録の仕組みの要否の欄が空なら拒否する。審査の周ごとに仕組みへ直しを足し、仕組みが目的に要るかを誰も問わないまま
    周が止まらなかった。照らすのは欄が空かどうかだけで、比べた値の真偽は見ない。拒否は note.empty の欄の名前で見る。"""
    setUp, tearDown, w, review_record = TestAccountCheck.setUp, TestAccountCheck.tearDown, TestAccountCheck.w, TestAccountCheck.review_record
    launcher_reviews = False  # 審査役の記録は、この試験が置く

    def start(self):
        r = self.w("task", "start", "--dir", str(self.repo), "--holder", "A")[1]
        self.wt, self.tok, self.note = Path(r["dir"]), r["token"], Path(r["note"]["path"])
        return r

    def git(self, *args, cwd=None):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], env=self.env, cwd=cwd or self.wt,
                              check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, name, amend=False):
        (self.wt / name).write_text(name, encoding="utf-8")
        self.git("add", name)
        self.git("commit", "-q", *(["--amend", "--no-edit"] if amend else ["-m", name]))
        return self.git("rev-parse", "HEAD")

    def publish(self, key, head, premise=None):
        self.note.write_text(FILLED_NOTE + ("" if premise is None else f"{PREMISE_LINE}{premise}\n"), encoding="utf-8")
        return self.w("op", "begin", "--dir", str(self.wt), "--token", self.tok, "--key", key, "--what", "x",
                      "--from", str(self.repo), "--verified", head, "--reviewed", head)

    def refused(self, key, head, premise=None):
        rc, res = self.publish(key, head, premise)
        return rc == 2 and res.get("note", {}).get("empty") == [PREMISE_LINE[2:-1]]

    def test_the_third_reviewed_commit_needs_the_premise_and_earlier_ones_do_not(self):
        r = self.start()
        self.review_record(r["base"])  # 基点そのものの審査は数えない
        heads = []
        for i in range(2):
            heads.append(self.commit(f"f{i}"))
            self.review_record(heads[-1])
        self.review_record(heads[1])  # 同じ commit を重ねて審査しても、周は増えない
        self.assertEqual(self.publish("push:feat@2", heads[1])[0], 0)  # 2 周目までは要らない
        self.w("op", "done", "--dir", str(self.wt), "--token", self.tok, "--key", "push:feat@2", "--result", "ok")
        heads.append(self.commit("f2"))
        self.review_record(heads[2])
        for premise in (None, "", "  "):  # 欄の行が無い（前の雛形の記録）、空、空白だけ
            with self.subTest(premise=premise):
                self.assertTrue(self.refused(f"push:feat@{premise!r}", heads[2], premise))
        rc, res = self.publish("push:feat@3", heads[2], " 列あり 590 秒・列なし 715 秒・job を分ける形 470 秒。別の形に替える")
        self.assertEqual(rc, 0, res)

    def test_the_field_can_use_the_short_name_and_several_lines(self):
        self.start()
        for i in range(3):
            self.review_record(head := self.commit(f"s{i}"))
        for i, body in enumerate(("- 仕組みの要否: 外す\n", f"{PREMISE_LINE}\n| 形 | 秒 |\n|---|---|\n| 列 | 590 |\n")):
            with self.subTest(body=body):
                self.note.write_text(FILLED_NOTE + body, encoding="utf-8")
                rc, res = self.w("op", "begin", "--dir", str(self.wt), "--token", self.tok, "--key", f"pr:feat@{i}", "--what", "x",
                                 "--from", str(self.repo), "--verified", head, "--reviewed", head)
                self.assertEqual(rc, 0, res)

    def test_only_ok_read_only_reviews_with_this_place_and_a_full_id_count(self):
        self.start()
        heads = [self.commit(f"o{i}") for i in range(4)]
        self.review_record(heads[0])
        self.review_record(heads[3])
        self.review_record(heads[1], status="failed")
        self.review_record(heads[2], write=True)
        self.review_record(heads[1], cwd=str(self.repo))  # ほかの場所（main の取り込みで履歴に入るほかのタスクの審査も同じ）
        with open(Path(self.env["BB_WRIT_STATE_DIR"]) / "delegations.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"head": heads[2][:12], "status": "ok", "write": False, "cwd": str(self.wt)}) + "\n")  # 短い ID
            f.write(json.dumps({"head": heads[2], "status": "ok", "write": False}) + "\n")  # cwd の無い行
        self.assertEqual(self.publish("push:feat@o", heads[3])[0], 0)  # 数えるのは 2 つだけ
        self.w("op", "done", "--dir", str(self.wt), "--token", self.tok, "--key", "push:feat@o", "--result", "ok")
        self.review_record(heads[1])
        self.assertTrue(self.refused("push:feat@o3", heads[3]))
        rel = os.path.relpath(self.wt, Path(self.tmp.name).resolve())  # 相対の --dir でも同じ作業場所として数える（w() は tmp で動く。
        # macOS の /var は /private/var への symlink で深さが違うので、実体のパスから相対にする）
        self.note.write_text(FILLED_NOTE, encoding="utf-8")
        rc, res = self.w("op", "begin", "--dir", rel, "--token", self.tok, "--key", "push:feat@rel", "--what", "x",
                         "--from", str(self.repo), "--verified", heads[3], "--reviewed", heads[3])
        self.assertEqual((rc, res.get("note", {}).get("empty")), (2, [PREMISE_LINE[2:-1]]), res)

    def test_reviews_of_commits_amended_away_still_count(self):
        self.start()
        self.review_record(self.commit("a"))
        self.review_record(self.commit("a2", amend=True))
        head = self.commit("a3", amend=True)
        self.review_record(head)
        self.assertTrue(self.refused("push:feat@amend", head))

    def test_a_reviewed_commit_merged_in_from_another_task_does_not_count(self):
        other = self.start()
        mine = self.start()  # 両方のタスクを同じ基点から始める
        self.wt, self.tok = Path(other["dir"]), other["token"]
        for i in range(2):
            self.review_record(self.commit(f"t{i}"))
        self.git("merge", "-q", "--ff-only", other["branch"], cwd=self.repo)  # ほかのタスクを main に統合した
        self.wt, self.tok, self.note = Path(mine["dir"]), mine["token"], Path(mine["note"]["path"])
        self.review_record(self.commit("m1"))
        before = self.git("rev-parse", "HEAD")
        self.git("merge", "-q", "--no-edit", "main")  # 統合の前に main を取り込む（playbook-parallel）
        head = self.git("rev-parse", "HEAD")
        self.assertNotEqual(head, before)  # 取り込みで、ほかのタスクの審査済みの commit が履歴に入った
        self.review_record(head)
        self.assertEqual(self.publish("push:feat@merged", head)[0], 0)  # 自分の周は 2

    def test_removing_the_task_json_of_the_place_does_not_stop_the_count(self):
        self.start()
        for i in range(3):
            self.review_record(head := self.commit(f"j{i}"))
        for f in (Path(self.env["BB_WRIT_STATE_DIR"]) / "work").glob("*/task.json"):
            f.unlink()  # 書く子が書き換えうる場所。タスクは親の一覧から作業場所の dir で引く
        self.assertTrue(self.refused("push:feat@notask", head))

    def test_the_main_checkout_claimed_beside_a_task_is_not_counted(self):
        self.start()  # 同じリポジトリにタスクがある。元の checkout は task start の作業場所でない
        self.w("release", "--dir", str(self.wt), "--token", self.tok)
        self.wt = self.repo
        r = self.w("claim", "--dir", str(self.repo), "--holder", "A")[1]
        self.tok, self.note = r["token"], Path(r["note"]["path"])
        heads = [self.commit(f"c{i}") for i in range(3)]
        for h in heads:
            self.review_record(h)
        self.assertEqual(self.publish("push:main@claim", heads[2])[0], 0)

    def test_a_place_not_made_by_task_start_is_not_counted(self):
        TestAccountCheck.review_record(self, subprocess.run(["git", "rev-parse", "HEAD"], env=self.env, cwd=self.wt,
                                                            capture_output=True, text=True).stdout.strip())
        r = self.w("claim", "--dir", str(self.wt), "--holder", "A")[1]
        self.tok, self.note = r["token"], Path(r["note"]["path"])
        heads = [self.commit(f"g{i}") for i in range(3)]
        for h in heads:
            self.review_record(h)
        self.assertEqual(self.publish("push:feat@claim", heads[2])[0], 0)  # task start でない場所は数えない


class TestNoteBeforePublish(unittest.TestCase):
    """push・pr は、作業の記録の目的・制約・確かめ方と、審査を通した commit なら指摘の扱いが空なら出せない。
    言い直し・記録・指摘の扱いの一覧を飛ばしたまま、長いセッションで作業が進んだ。照らすのは欄が空かどうかだけで、
    中身の真偽・書いた順・どの周の審査の指摘かは見ない（前の周の扱いが残っていれば通る）。拒否の文言は確かめず、
    拒否の JSON の note.empty（雛形の順の欄の名前）を見る。"""
    setUp, tearDown, w, begin, head, review_record = (TestAccountCheck.setUp, TestAccountCheck.tearDown, TestAccountCheck.w,
                                                      TestAccountCheck.begin, TestVerifiedCommit.head, TestAccountCheck.review_record)
    fill_note = False

    def claim(self, body=None):
        r = self.w("claim", "--dir", str(self.wt), "--holder", "A")[1]
        self.note = Path(r["note"]["path"])
        if body is not None:
            self.note.write_text(body, encoding="utf-8")
        return r["token"], r

    def publish(self, tok, key, reviewed):
        return self.begin(tok, key, "--from", str(self.repo), "--verified", self.head(), "--reviewed", reviewed)

    def head_note(self, purpose="x", limits="x", check="x", triage=None):
        return (f"# 作業の記録\n\n- 目的:{purpose}\n- 制約:{limits}\n- できたと分かる確かめ方（述語）:{check}\n"
                + ("" if triage is None else f"{TRIAGE_LINE}{triage}\n") + "- 決めたことと根拠: y\n")

    def test_the_template_as_claimed_is_refused_and_names_the_empty_fields(self):
        tok, r = self.claim()
        self.assertEqual(r["note"]["empty"], ["目的", "制約", "できたと分かる確かめ方（述語）"])  # 担当を取った場で知らせる
        for key in ("push:feat@1", "pr:feat"):
            rc, res = self.publish(tok, key, "none:文書だけ")
            self.assertEqual(rc, 2, key)
            self.assertIn(str(self.note), res["reason"])
            for name in ("目的", "制約", "できたと分かる確かめ方（述語）"):
                self.assertIn(name, res["reason"], key)
        self.assertEqual(self.pending(), [])  # 記録しない

    def test_each_required_field_alone_left_empty_is_refused(self):
        for empty in ("purpose", "limits", "check"):
            with self.subTest(empty=empty):
                tok, _ = self.claim(self.head_note(**{empty: "   "}))  # 空白だけは空
                rc, res = self.publish(tok, f"push:feat@{empty}", "none:文書だけ")
                self.assertEqual(rc, 2, res)
                name = {"purpose": "目的", "limits": "制約", "check": "できたと分かる確かめ方（述語）"}[empty]
                self.assertEqual(res["note"]["empty"], [name])
                self.w("release", "--dir", str(self.wt), "--token", tok)

    def test_filled_fields_pass_and_the_triage_is_not_needed_without_a_reviewed_commit(self):
        tok, r = self.claim(self.head_note())
        self.assertNotIn("empty", self.w("status", "--dir", str(self.wt))[1]["note"])
        self.assertEqual(self.publish(tok, "push:feat@1", "none:文書だけ")[0], 0)

    def test_a_reviewed_commit_needs_the_triage(self):
        for triage in (None, "", "  "):  # 欄の行が無い（前の雛形の記録）、空、空白だけ
            with self.subTest(triage=triage):
                tok, _ = self.claim(self.head_note(triage=triage))
                rc, res = self.publish(tok, f"pr:feat@{triage!r}", self.head())
                self.assertEqual(rc, 2, res)
                self.assertEqual(res["note"]["empty"], ["指摘の扱い（審査の指摘ごとに、対応する・記録のみ・退ける と理由を、直す前に）"])
                self.w("release", "--dir", str(self.wt), "--token", tok)
        tok, _ = self.claim(self.head_note(triage=" F1 対応する: 実際の入力で落ちる"))
        self.assertEqual(self.publish(tok, "push:feat@ok", self.head())[0], 0)

    def test_lines_under_a_field_count_until_the_next_field_or_heading(self):
        table = "\n| 指摘 | 扱い |\n|---|---|\n| F1 | 対応する |\n  - 字下げした行"
        tok, _ = self.claim(self.head_note(purpose="\n  - 字下げした目的", triage=table))
        self.assertEqual(self.publish(tok, "push:feat@1", self.head())[0], 0)
        self.w("release", "--dir", str(self.wt), "--token", tok)
        # 見出しの下や次の欄に書いたものは、空の欄の中身に数えない
        body = self.head_note(purpose="", triage="x").replace("- 制約:x", "## 目的\n書いた\n- 制約:x")
        tok, _ = self.claim(body)
        rc, res = self.publish(tok, "push:feat@2", self.head())
        self.assertEqual((rc, res["note"]["empty"]), (2, ["目的"]))
        self.w("release", "--dir", str(self.wt), "--token", tok)
        # 字下げした行や行の途中の欄の名前は、欄に数えない（その前にあっても、雛形の行の欄を見る）
        tok, _ = self.claim(self.head_note(purpose="", triage="x").replace("# 作業の記録\n", "# 作業の記録\n  - 目的: 字下げ\n"))
        rc, res = self.publish(tok, "push:feat@3", self.head())
        self.assertEqual((rc, res["note"]["empty"]), (2, ["目的"]))
        self.w("release", "--dir", str(self.wt), "--token", tok)
        # 見出しは # の数によらず止める。欄の名前は ":" までで見る（名前で始まるだけの行は、欄でも次の欄の始まりでもない）
        for i, body in enumerate((self.head_note(purpose="", triage="x").replace("- 制約:x", "# 見出し\n書いた\n- 制約:x"),
                                  self.head_note(purpose="", triage="x").replace("# 作業の記録\n", "# 作業の記録\n- 目的外のこと: x\n"))):
            tok, _ = self.claim(body)
            rc, res = self.publish(tok, f"push:feat@h{i}", self.head())
            self.assertEqual((rc, res["note"]["empty"]), (2, ["目的"]), body)
            self.w("release", "--dir", str(self.wt), "--token", tok)
        for i, purpose in enumerate(("\n- 制約のメモも目的の中身", "\n#123 の件（見出しでない）", "\nメモ # 2（行の途中の # は見出しでない）")):
            tok, _ = self.claim(self.head_note(purpose=purpose, triage="x"))
            self.assertEqual(self.publish(tok, f"push:feat@c{i}", self.head())[0], 0, purpose)
            self.w("release", "--dir", str(self.wt), "--token", tok)
        tok, _ = self.claim(self.head_note())
        self.w("release", "--dir", str(self.wt), "--token", tok)
        # 欄の名前は括弧の中を省いてよい。省いた行も欄なので、前の欄の中身に数えない
        short = "- 指摘の扱い: F1 対応する\n"
        tok, _ = self.claim(self.head_note(check="").replace("- 決めたことと根拠", short + "- 決めたことと根拠"))
        rc, res = self.publish(tok, "push:feat@short", self.head())
        self.assertEqual((rc, res["note"]["empty"]), (2, ["できたと分かる確かめ方（述語）"]))
        self.w("release", "--dir", str(self.wt), "--token", tok)
        tok, _ = self.claim(self.head_note().replace("- 決めたことと根拠", short + "- 決めたことと根拠"))
        self.assertEqual(self.publish(tok, "push:feat@short-ok", self.head())[0], 0)
        self.w("release", "--dir", str(self.wt), "--token", tok)
        # 別の文言の括弧の欄・「#」だけの空の見出しの下・2 回目の同じ欄は、空の欄を埋めない（同じ欄は最初だけを見る）
        tok, _ = self.claim(self.head_note(check="", triage="x").replace("# 作業の記録\n", "# 作業の記録\n- できたと分かる確かめ方（未定）: x\n"))
        rc, res = self.publish(tok, "push:feat@paren", self.head())
        self.assertEqual((rc, res["note"]["empty"]), (2, ["できたと分かる確かめ方（述語）"]))
        self.w("release", "--dir", str(self.wt), "--token", tok)
        for i, body in enumerate((self.head_note(purpose="", triage="x").replace("# 作業の記録\n", "# 作業の記録\n- 目的（別物）: x\n"),
                                  self.head_note(purpose="", triage="x").replace("- 制約:x", "#\n見出しの下\n- 制約:x"),
                                  self.head_note(purpose="", triage="x") + "- 目的: 後から書いた\n")):
            tok, _ = self.claim(body)
            rc, res = self.publish(tok, f"push:feat@x{i}", self.head())
            self.assertEqual((rc, res["note"]["empty"]), (2, ["目的"]), body)
            self.w("release", "--dir", str(self.wt), "--token", tok)
        tok, _ = self.claim(self.head_note())

    def test_renew_names_the_empty_fields_without_refusing(self):
        tok, _ = self.claim()
        rc, r = self.w("renew", "--dir", str(self.wt), "--token", tok)
        self.assertEqual((rc, r["note"]["empty"]), (0, ["目的", "制約", "できたと分かる確かめ方（述語）"]))
        self.note.write_text(self.head_note(), encoding="utf-8")
        rc, r = self.w("renew", "--dir", str(self.wt), "--token", tok)
        self.assertEqual(rc, 0)
        self.assertNotIn("note", r)

    def test_status_names_the_empty_fields(self):
        self.claim()
        self.assertEqual(self.w("status", "--dir", str(self.wt))[1]["note"]["empty"], ["目的", "制約", "できたと分かる確かめ方（述語）"])

    def test_a_missing_or_unreadable_note_is_refused_with_a_reason(self):
        tok, _ = self.claim(self.head_note())
        self.note.unlink()  # 記録のファイルが無いのは、欄が無いのと同じ
        rc, res = self.publish(tok, "push:feat@gone", "none:文書だけ")
        self.assertEqual((rc, res["note"]["empty"]), (2, ["目的", "制約", "できたと分かる確かめ方（述語）"]))
        # UTF-8 でない字があっても、欄が書いてあれば通す（置き換えて読む）
        self.note.write_bytes(self.head_note().encode("utf-8") + b"\xff\xfe\n")
        self.assertEqual(self.publish(tok, "push:feat@bytes", "none:文書だけ")[0], 0)
        if os.geteuid() == 0:
            return  # root は権限で読めなくならない
        lease = next(self.note.parent.glob("lease.json"))
        before = lease.read_bytes()
        self.note.chmod(0)
        try:
            rc, res = self.w("renew", "--dir", str(self.wt), "--token", tok, "--lease", "1")
            self.assertEqual(rc, 1)
            self.assertIn("読めない", res["reason"])  # 1 行の JSON で理由を返す
            self.assertEqual(lease.read_bytes(), before)  # 読めなければ期限を書かない
            rc, res = self.publish(tok, "push:feat@unreadable", "none:文書だけ")
            self.assertEqual(rc, 1)
            self.assertIn("読めない", res["reason"])
        finally:
            self.note.chmod(0o644)

    def test_a_note_that_is_not_a_regular_file_is_refused_before_the_lease_is_written(self):
        tok, _ = self.claim(self.head_note())
        self.w("release", "--dir", str(self.wt), "--token", tok)
        self.note.unlink()
        self.note.mkdir()
        rc, res = self.w("claim", "--dir", str(self.wt), "--holder", "A")
        self.assertEqual(rc, 1)
        self.assertIn("通常のファイルでない", res["reason"])  # 1 行の JSON で理由を返す
        self.assertFalse((self.note.parent / "lease.json").exists())  # token を受け取らない担当を残さない
        rc, res = self.w("status", "--dir", str(self.wt))
        self.assertEqual(rc, 1)
        self.assertIn("通常のファイルでない", res["reason"])
        self.note.rmdir()
        self.note.symlink_to(self.note.parent / "nowhere")  # 壊れた symlink も通常のファイルでない
        rc, res = self.w("status", "--dir", str(self.wt))
        self.assertEqual(rc, 1)
        self.assertIn("通常のファイルでない", res["reason"])
        self.assertFalse((self.note.parent / "nowhere").exists())  # 行き先に雛形を書かない
        self.note.unlink()
        if os.geteuid() == 0:
            return  # root は権限で書けなくならない
        tok, _ = self.claim()
        lease = self.note.parent / "lease.json"
        before = lease.read_bytes()
        self.note.unlink()
        self.note.parent.chmod(0o500)  # 消えた記録を作り直せない
        try:
            rc, res = self.w("renew", "--dir", str(self.wt), "--token", tok)
            self.assertEqual(rc, 1)
            self.assertIn("作れない", res["reason"])
            self.assertEqual(lease.read_bytes(), before)
        finally:
            self.note.parent.chmod(0o755)

    def test_other_operations_do_not_look_at_the_note(self):
        tok, _ = self.claim()
        self.assertEqual(self.w("op", "begin", "--dir", str(self.wt), "--token", tok, "--key", "local:x", "--what", "x")[0], 0)
        self.assertEqual(self.begin(tok, "comment:pr-1", "--from", str(self.repo))[0], 0)

    pending = TestReviewedCommit.pending


class TestOpRun(unittest.TestCase):
    """op run は、begin の照合を通ったときだけコマンドを実行し、実際の終了コードと出力を done に記録する。
    begin とコマンドを手で並べると、begin が拒否されてもコマンドが走り、結果に打った文字（ok）が残った。"""
    setUp, w, claim, head = (TestAccountCheck.setUp, TestAccountCheck.w, TestAccountCheck.claim, TestVerifiedCommit.head)
    pending = TestReviewedCommit.pending

    def run_op(self, tok, key, *flags_and_cmd):
        return self.w("op", "run", "--dir", str(self.wt), "--token", tok, "--key", key, "--what", "x",
                      "--from", str(self.repo), "--verified", self.head(), *flags_and_cmd)

    def script(self, body):
        return ["--", sys.executable, "-c", body]

    ops_file = TestReviewedCommit.ops_file

    def op(self, tok, key, *args):
        return self.w("op", "run", "--dir", str(self.wt), "--token", tok, "--key", key, "--what", "x", *args)

    def test_refused_begin_does_not_run_the_command(self):
        tok = self.claim()
        marker = Path(self.tmp.name) / "ran"
        touch = self.script(f"open({str(marker)!r}, 'w')")
        before = self.ops_file()
        rc, r = self.run_op(tok, "push:feat@1", *touch)  # --reviewed が無い
        self.assertEqual(rc, 2)
        self.assertIn("--reviewed", r["reason"])
        verified = self.head()
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "after"],
                       env=self.env, cwd=self.wt, check=True)
        refusals = (
            (3, self.op("bad-token", "push:feat@1", "--from", str(self.repo), "--verified", self.head(), "--reviewed", self.head(), *touch)),
            (5, self.op(tok, "push:feat@1", "--from", str(self.wt), "--verified", self.head(), "--reviewed", self.head(), *touch)),
            (6, self.op(tok, "push:feat@1", "--from", str(self.repo), "--verified", verified, "--reviewed", self.head(), *touch)),
        )
        for want, (rc, r) in refusals:
            self.assertEqual(rc, want, r)
        self.assertFalse(marker.exists())
        self.assertEqual(self.ops_file(), before)  # 拒否は何も記録しない
        self.assertEqual(self.run_op(tok, "pr:feat", "--reviewed", "none:文書だけ", *self.script("print(1)"))[0], 0)
        rc, r = self.run_op(tok, "pr:feat", "--reviewed", "none:文書だけ", *touch)
        self.assertEqual(rc, 4)
        self.assertFalse(marker.exists())

    def test_failed_command_is_recorded_with_its_exit_code(self):
        tok = self.claim()
        rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(),
                            *self.script("import sys; print('rejected by remote'); sys.exit(3)"))
        self.assertEqual(rc, 3)
        self.assertFalse(r["ok"])
        self.assertEqual(r["op"]["state"], "done")
        self.assertIn("exit 3", r["op"]["result"])
        self.assertIn("rejected by remote", r["op"]["result"])
        self.assertEqual(self.pending(), [])

    def test_successful_command_records_its_output(self):
        tok = self.claim()
        rc, r = self.run_op(tok, "pr:feat", "--reviewed", "none:文書だけ", *self.script("print('https://example.test/pull/7')"))
        self.assertEqual(rc, 0)
        self.assertTrue(r["ok"])
        self.assertIn("exit 0", r["op"]["result"])
        self.assertIn("https://example.test/pull/7", r["op"]["result"])
        self.assertEqual(self.w("op", "run", "--dir", str(self.wt), "--token", tok, "--key", "pr:feat", "--what", "x",
                                "--from", str(self.repo), "--verified", self.head(), "--reviewed", "none:文書だけ",
                                *self.script("print('again')"))[0], 4)  # 同じ鍵は二重に実行しない

    def test_stderr_and_the_last_lines_are_recorded(self):
        tok = self.claim()
        body = "import sys\nfor i in range(30): print('line', i)\nsys.stdout.flush()\nsys.stderr.write('remote rejected\\n')\nsys.exit(1)"
        rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script(body))
        self.assertEqual(rc, 1)
        self.assertIn("remote rejected", r["op"]["result"])
        self.assertIn("line 11\n", r["op"]["result"])  # stderr の 1 行と合わせて、末尾のちょうど 20 行
        self.assertNotIn("line 10\n", r["op"]["result"])

    def test_undecodable_output_is_still_recorded(self):
        tok = self.claim()
        rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(),
                            *self.script("import os, sys; os.write(1, bytes([255]) + b' done'); sys.exit(2)"))
        self.assertEqual(rc, 2)
        self.assertIn("exit 2", r["op"]["result"])
        self.assertIn("\ufffd done", r["op"]["result"])  # 復号できないバイトは置き換えて残す
        self.assertEqual(self.pending(), [])

    def test_exit_codes_and_signals_are_kept(self):
        tok = self.claim()
        rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script("import sys; sys.exit(7)"))
        self.assertEqual((rc, r["ok"]), (7, False))
        rc, r = self.run_op(tok, "push:feat@2", "--reviewed", self.head(),
                            *self.script("import os, signal; os.kill(os.getpid(), signal.SIGTERM)"))
        self.assertEqual((rc, r["ok"]), (143, False))
        self.assertIn("exit 143", r["op"]["result"])
        rc, r = self.run_op(tok, "push:feat@3", "--reviewed", self.head(),
                            *self.script("import os, signal; os.kill(os.getpid(), signal.SIGKILL)"))
        self.assertEqual(rc, 137)

    def test_a_command_that_cannot_start_is_recorded_as_failed(self):
        tok = self.claim()
        rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), "--", str(Path(self.tmp.name) / "no-such-command"))
        self.assertEqual(rc, 127)
        self.assertIn("exit 127", r["op"]["result"])
        self.assertEqual(self.pending(), [])

    def test_release_is_refused_while_the_command_runs(self):
        """手放すと、監督役が結果を記録できず、begun が残った。"""
        tok = self.claim()
        release = (f"import subprocess, sys; print('release', subprocess.run([sys.executable, {str(WORK)!r}, 'release', '--dir', "
                   f"{str(self.wt)!r}, '--token', {tok!r}], capture_output=True).returncode)")
        rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script(release))
        self.assertEqual(rc, 0, r)
        self.assertIn("release 4", r["op"]["result"])
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.w("release", "--dir", str(self.wt), "--token", tok)[0], 0)  # 終われば手放せる

    def test_an_expired_lease_not_taken_over_still_records_the_result(self):
        tok = self.claim()
        expire = ("import json, pathlib, time; f = next(pathlib.Path(" + repr(str(Path(self.tmp.name).resolve() / "state" / "work"))
                  + ").glob('*/lease.json')); d = json.loads(f.read_text()); d['expires_at'] = time.time() - 1; f.write_text(json.dumps(d))")
        rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script(expire))
        self.assertEqual(rc, 0)
        self.assertEqual(r["op"]["state"], "done")

    def test_a_short_lease_is_extended_before_the_command_runs(self):
        tok = self.claim()
        self.assertEqual(self.w("renew", "--dir", str(self.wt), "--token", tok, "--lease", "19")[0], 0)  # 20 分を切っている
        left = ("import json, pathlib, time; f = next(pathlib.Path(" + repr(str(Path(self.tmp.name).resolve() / "state" / "work"))
                + ").glob('*/lease.json')); print('left', int(json.loads(f.read_text())['expires_at'] - time.time()))")
        rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script(left))
        self.assertEqual(rc, 0)
        self.assertGreater(int(r["op"]["result"].split("left ")[1].split()[0]), 20 * 60 - 30)

    def lease_file(self):
        return next((Path(self.tmp.name).resolve() / "state" / "work").glob("*/lease.json"))

    def raw(self, *args, stdin=None):
        return subprocess.run([sys.executable, str(WORK), *with_risk(self, args)], env=self.env, capture_output=True, text=True, input=stdin)

    def test_every_refusal_of_begin_refuses_run_the_same_way(self):
        """op run の照合は op begin と同じ。拒否の理由を手で選んで試すと、試していない拒否の後だけコマンドを走らせる退行が通った。"""
        tok = self.claim()
        old = self.head()
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "after"],
                       env=self.env, cwd=self.wt, check=True)
        h = self.head()
        other = Path(self.tmp.name).resolve() / "personal" / "other"
        other.mkdir()
        subprocess.run(["git", "init", "-q"], env=self.env, cwd=other, check=True)
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/B16B1RD/demo.git"], env=self.env, cwd=other, check=True)
        repo = str(self.repo)
        cases = [
            (tok, "Push:feat", "--from", repo, "--verified", h, "--reviewed", h),
            (tok, "push:feat@1", "--verified", h, "--reviewed", h),
            (tok, "push:feat@1", "--from", str(other), "--verified", h, "--reviewed", h),
            (tok, "push:feat@1", "--from", str(self.wt), "--verified", h, "--reviewed", h),
            (tok, "push:feat@1", "--from", repo, "--reviewed", h),
            (tok, "push:feat@1", "--from", repo, "--verified", old, "--reviewed", h),
            (tok, "push:feat@1", "--from", repo, "--verified", h),
            (tok, "push:feat@1", "--from", repo, "--verified", h, "--reviewed", "HEAD"),
            (tok, "push:feat@1", "--from", repo, "--verified", h, "--reviewed", "deadbeef"),
            (tok, "push:feat@1", "--from", repo, "--verified", h, "--reviewed", "none:"),
            (tok, "push:feat@1", "--from", repo, "--verified", h, "--reviewed", old),
            ("bad-token", "push:feat@1", "--from", repo, "--verified", h, "--reviewed", h),
        ]
        marker = Path(self.tmp.name) / "ran"
        lease, before = self.lease_file().read_bytes(), self.ops_file()
        for token, key, *flags in cases:
            base = ["op", "{}", "--dir", str(self.wt), "--token", token, "--key", key, "--what", "x", *flags]
            begin = self.w(*[x.format("begin") if x == "{}" else x for x in base])[0]
            run = self.w(*[x.format("run") if x == "{}" else x for x in base], *self.script(f"open({str(marker)!r}, 'w')"))
            self.assertNotEqual(begin, 0, (key, flags))
            self.assertEqual(run[0], begin, (key, flags))
            self.assertNotIn("exit", run[1], (key, flags))  # exit が無いことで、コマンドが走らなかったと分かる
        self.assertFalse(marker.exists())
        self.assertEqual(self.ops_file(), before)
        self.assertEqual(self.lease_file().read_bytes(), lease)  # 拒否は担当の期限も延ばさない
        d = json.loads(lease)
        self.lease_file().write_text(json.dumps({**d, "expires_at": time.time() - 1}))
        args = ["--dir", str(self.wt), "--token", tok, "--key", "pr:feat", "--what", "x", "--from", repo, "--verified", h, "--reviewed", h]
        self.assertEqual(self.w("op", "begin", *args)[0], 3)
        self.assertEqual(self.w("op", "run", *args, *self.script(f"open({str(marker)!r}, 'w')"))[0], 3)  # 期限切れの担当は走らせない
        self.assertFalse(marker.exists())

    def test_a_file_that_cannot_be_executed_is_recorded_as_failed(self):
        tok = self.claim()
        f = Path(self.tmp.name) / "not-executable"
        f.write_text("#!/bin/sh\n")
        rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), "--", str(f))
        self.assertEqual(rc, 127)
        self.assertEqual(r["op"]["state"], "done")

    def test_the_command_gets_no_stdin_and_its_output_is_shown(self):
        tok = self.claim()
        p = self.raw("op", "run", "--dir", str(self.wt), "--token", tok, "--key", "push:feat@1", "--what", "x", "--from", str(self.repo),
                     "--verified", self.head(), "--reviewed", self.head(),
                     *self.script("import sys; print('stdin', len(sys.stdin.read())); print('shown')"), stdin="secret\n")
        self.assertEqual(p.returncode, 0)
        self.assertIn("stdin 0", json.loads(p.stdout)["op"]["result"])
        self.assertIn("shown", p.stderr)  # 出力の全体は stderr に出す

    def record_of(self, tok, key, body):
        p = self.raw("op", "run", "--dir", str(self.wt), "--token", tok, "--key", key, "--what", "x", "--from", str(self.repo),
                     "--verified", self.head(), "--reviewed", self.head(), *self.script(body))
        return json.loads(p.stdout)["op"]["result"].removeprefix("exit 0: "), p.stderr

    def test_the_record_keeps_the_last_20_lines_up_to_8_kib(self):
        tok = self.claim()
        rec, _ = self.record_of(tok, "push:feat@1", "for i in range(100): print(f'line {i} ' + 'x' * 200)\nprint('\\n' * 10, end='')")
        self.assertEqual([int(x.split()[1]) for x in rec.splitlines()], list(range(80, 100)))  # 末尾の空行は数えない
        rec, _ = self.record_of(tok, "push:feat@2", "import sys\nfor i in range(30): sys.stdout.write(f'step {i}\\r')\nprint()\nprint('end')")
        self.assertEqual(rec.splitlines()[-1], "end")
        self.assertIn("step 0", rec)  # \r では行を割らない
        rec, _ = self.record_of(tok, "push:feat@3", "for i in range(100): print(f'line {i} ' + 'x' * 3300)")
        self.assertTrue(rec.startswith("（先頭を省いた）"), rec[:40])
        self.assertLessEqual(len(rec.encode()), 8 * 1024 + 40)
        self.assertTrue(rec.rstrip().endswith("x"))
        rec, _ = self.record_of(tok, "push:feat@4", "import sys; sys.stdout.write('y' * 3_000_000)")
        self.assertLessEqual(len(rec.encode()), 8 * 1024 + 40)  # 改行の無い大きな出力でも、記録は膨らまない

    def test_trailing_blank_lines_of_any_whitespace_are_skipped(self):
        tok = self.claim()
        for n, tailer in enumerate(("'\\r\\n' * 40000", "' \\n' * 40000", "'\\t\\n' * 40000")):
            rec, _ = self.record_of(tok, f"push:feat@{n}", f"import sys\nprint('IMPORTANT: pushed abc123')\nsys.stdout.write({tailer})")
            self.assertEqual(rec.strip(), "IMPORTANT: pushed abc123", tailer)

    def test_the_whole_output_is_shown(self):
        tok = self.claim()
        _, shown = self.record_of(tok, "push:feat@1", "print('first line'); print('z' * 2_000_000); print('last line')")
        self.assertIn("first line", shown)  # 表示は全体（先頭も見える）
        self.assertIn("last line", shown)

    def test_the_command_cannot_prompt_on_the_terminal(self):
        tok = self.claim()
        rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(),
                            *self.script(f"import os; print('prompt', os.environ.get('GIT_TERMINAL_PROMPT'), 'sid', os.getsid(0) != {os.getsid(0)})"))
        self.assertIn("prompt 0 sid True", r["op"]["result"])  # 呼んだ側の端末のセッションから切り離す

    def test_only_op_run_takes_a_command_after_double_dash(self):
        tok = self.claim()
        p = self.raw("op", "begin", "--dir", str(self.wt), "--token", tok, "--key", "local:x", "--what", "x", "--", "echo", "hi")
        self.assertEqual(p.returncode, 2)  # begin は -- の後ろを黙って捨てずに止まる
        self.assertEqual(self.pending(), [])

    def test_a_local_key_runs_where_from_points(self):
        tok = self.claim()
        rc, r = self.op(tok, "local:probe", "--from", str(self.repo), *self.script("import os; print(os.getcwd())"))
        self.assertEqual(rc, 0)
        self.assertIn(str(self.repo.resolve()), r["op"]["result"])

    def left(self):
        return json.loads(self.lease_file().read_text())["expires_at"] - time.time()

    def test_the_lease_is_kept_while_a_long_command_runs(self):
        """20 分を超えるコマンドでも、動いている間は担当を保つ（切れると、後任が終わっていない push を照合してやり直した）。"""
        tok = self.claim()
        shrink = ("d = json.load(open(f)); d['expires_at'] = time.time() + 1; open(f + '.t', 'w').write(json.dumps(d)); os.replace(f + '.t', f); time.sleep(2.5); "  # 20 分の経過の代わり
                  "print('left', int(json.load(open(f))['expires_at'] - time.time())); ")
        body = ("import json, os, subprocess, sys, time; f = " + repr(str(self.lease_file())) + "; " + shrink + shrink +
                f"r = subprocess.run([sys.executable, {str(WORK)!r}, 'claim', '--dir', {str(self.wt)!r}, '--holder', 'B'], capture_output=True); "
                "print('claim', r.returncode)")
        rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script(body))
        self.assertEqual(rc, 0, r)
        lefts = [int(x.split()[0]) for x in r["op"]["result"].split("left ")[1:]]
        self.assertEqual(len(lefts), 2)
        for v in lefts:  # 2 回とも、19 分を切ったら 20 分先に延ばした
            self.assertGreater(v, 19 * 60, lefts)
        self.assertIn("claim 3", r["op"]["result"])

    def test_no_one_can_claim_while_the_command_runs_even_after_release_or_expiry(self):
        tok = self.claim()
        claim = f"subprocess.run([sys.executable, {str(WORK)!r}, 'claim', '--dir', {str(self.wt)!r}, '--holder', 'B'], capture_output=True).returncode"
        body = ("import json, os, subprocess, sys, time; f = " + repr(str(self.lease_file())) + "; "
                f"print('release', subprocess.run([sys.executable, {str(WORK)!r}, 'release', '--dir', {str(self.wt)!r}, '--token', {tok!r}], capture_output=True).returncode); "
                f"print('after release', {claim}); "
                "open(f + '.t', 'w').write(json.dumps({'token': 'x', 'holder': 'X', 'expires_at': time.time() - 1})); os.replace(f + '.t', f); "
                f"print('after expiry', {claim})")
        rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script(body))
        self.assertIn("release 4", r["output"] if "output" in r else r["op"]["result"])
        self.assertIn("after release 3", r["output"] if "output" in r else r["op"]["result"])
        self.assertIn("after expiry 3", r["output"] if "output" in r else r["op"]["result"])
        self.assertEqual(self.w("claim", "--dir", str(self.wt), "--holder", "B")[0], 0)  # 終われば取れる

    def test_the_lease_of_another_token_is_not_extended(self):
        tok = self.claim()
        body = ("import json, os, time; f = " + repr(str(self.lease_file())) + "; "
                "open(f + '.t', 'w').write(json.dumps({'token': 'other', 'holder': 'A', 'expires_at': time.time() + 60})); os.replace(f + '.t', f); "
                "time.sleep(2.5); print('left', int(json.load(open(f))['expires_at'] - time.time()))")
        rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script(body))
        self.assertEqual(rc, 3)  # token が違うので done も書かない
        self.assertLess(int(r["output"].split("left ")[1].split()[0]), 61)  # 同じ名前でも、別の token の担当は延ばさない
        self.assertEqual(self.pending(), ["push:feat@1"])

    def test_stopping_op_run_leaves_the_command_and_its_record_to_the_supervisor(self):
        """op run が止められても、監督役がコマンドを最後まで動かし、担当を保ち、done を書く。その間、外部操作と手での done は止める。"""
        for sig in (signal.SIGKILL, signal.SIGTERM):
            with self.subTest(sig=sig):
                tok = self.claim() if sig == signal.SIGKILL else self.tok2
                key = f"push:feat@{int(sig)}"
                go, done = self.go_file(f"go{int(sig)}"), Path(self.tmp.name) / f"done{int(sig)}"
                body = (f"import os, sys, time\nwhile not os.path.exists({str(go)!r}): time.sleep(0.05)\n"
                        "for i in range(2000): print('late output', i)\n"  # 親が止められた後に書く（パイプなら SIGPIPE で死んだ）
                        f"open({str(done)!r}, 'w')\nsys.exit(3)")
                p = subprocess.Popen([sys.executable, str(WORK), "op", "run", "--dir", str(self.wt), "--token", tok, "--key", key,
                                      "--what", "x", "--from", str(self.repo), "--verified", self.head(), "--reviewed", self.head(), "--risk", "低:試験の外",
                                      *self.script(body)], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                for _ in range(200):
                    if [o for o in self.pending() if o == key] and self.w("op", "pending", "--dir", str(self.wt))[1]["pending_ops"][0].get("running"):
                        break
                    time.sleep(0.05)
                p.send_signal(sig)
                p.wait()
                self.assertEqual([(o["key"], o.get("running")) for o in self.w("op", "pending", "--dir", str(self.wt))[1]["pending_ops"]],
                                 [(key, True)])  # 止められた後も、監督役が動かしていると分かる
                d = json.loads(self.lease_file().read_text())
                self.lease_file().write_text(json.dumps({**d, "expires_at": time.time() + 1}))
                time.sleep(2)  # 監督役が延ばす
                self.assertGreater(self.left(), 19 * 60)
                self.assertEqual(self.w("claim", "--dir", str(self.wt), "--holder", "B")[0], 3)
                kinds = [("begin", "--key", f"{k}:x", "--from", str(self.repo), "--verified", self.head(), "--reviewed", "none:x")
                         for k in ("push", "pr", "comment", "merge", "release", "issue")]
                for args in (*kinds, ("done", "--key", key, "--result", "閉じる")):
                    rc, r = self.w("op", args[0], "--dir", str(self.wt), "--token", tok, "--what", "x", *args[1:])
                    self.assertEqual(rc, 4, args)
                    self.assertIn(key, r["reason"])
                self.assertEqual(self.run_op(tok, "pr:feat", "--reviewed", "none:x", *self.script("print(1)"))[0], 4)
                self.assertEqual(self.op(tok, "local:note", *self.script("print(1)"))[0], 4)  # 監督役は作業場所に 1 つ
                self.assertEqual(self.pending(), [key])  # 拒否した操作は記録しない
                go.write_text("")
                for _ in range(200):
                    if not self.pending():
                        break
                    time.sleep(0.05)
                self.assertTrue(done.exists())
                rec = [json.loads(x) for x in self.ops_file().decode().splitlines() if json.loads(x)["key"] == key][-1]
                self.assertEqual(rec["state"], "done")
                self.assertIn("exit 3", rec["result"])
                self.assertIn("late output 1999", rec["result"])
                self.tok2 = tok

    def test_a_broken_lease_file_does_not_stop_the_supervisor(self):
        tok = self.claim()
        body = ("import os, time; f = " + repr(str(self.lease_file())) + "; good = open(f).read(); "
                "open(f + '.t', 'w').write('{'); os.replace(f + '.t', f); time.sleep(2.5); "
                "open(f + '.t', 'w').write(good); os.replace(f + '.t', f); print('restored')")
        rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script(body))
        self.assertEqual(rc, 0, r)
        self.assertIn("restored", r["op"]["result"])

    def tearDown(self):
        for go in getattr(self, "gos", []):  # 途中で落ちても、待っているコマンドを残さない（一時ディレクトリを消す前に）
            go.exists() or go.write_text("")
        if getattr(self, "gos", []):
            time.sleep(0.5)
        self.tmp.cleanup()

    def go_file(self, name="go"):
        go = Path(self.tmp.name) / name
        self.gos = [*getattr(self, "gos", []), go]
        return go

    def start_run(self, tok, key, body, env=None, **kw):
        p = subprocess.Popen([sys.executable, str(WORK), "op", "run", "--dir", str(self.wt), "--token", tok, "--key", key,
                              "--what", "x", "--from", str(self.repo), "--verified", self.head(), "--reviewed", self.head(), "--risk", "低:試験の外",
                              *self.script(body)], env={**self.env, **(env or {})}, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **kw)
        self.addCleanup(p.stdout.close)  # 読み切らない試験もある。閉じないと ResourceWarning が出る
        return p

    def wait_running(self, key):
        for _ in range(200):
            if [o for o in self.w("op", "pending", "--dir", str(self.wt))[1]["pending_ops"] if o["key"] == key and o.get("running")]:
                return
            time.sleep(0.05)
        self.fail(f"{key} が running にならない")

    def wait_done(self, key):
        for _ in range(200):
            recs = [json.loads(x) for x in self.ops_file().decode().splitlines() if json.loads(x)["key"] == key]
            if recs and recs[-1]["state"] == "done":
                return recs[-1]
            time.sleep(0.05)
        self.fail(f"{key} の done が書かれない")

    def wait_file(self, f):
        for _ in range(200):
            if f.exists():
                return
            time.sleep(0.05)
        self.fail(f"{f} ができない")

    def supervisor_of(self, ready):
        """ready を書いたコマンドの親（監督役）の pid。"""
        procs = subprocess.run(["ps", "-eo", "pid=,ppid=,args="], capture_output=True, text=True).stdout.splitlines()
        cmd = [x.split() for x in procs if str(ready) in x and "_supervise" not in x and " op run " not in x]
        self.assertEqual(len(cmd), 1, cmd)
        sup = cmd[0][1]
        self.assertIn("_supervise", subprocess.run(["ps", "-o", "args=", "-p", sup], capture_output=True, text=True).stdout)
        return int(sup)

    def test_a_signal_to_the_supervisor_is_passed_on_and_the_result_still_recorded(self):
        """監督役が止められると、ロックが外れて、動いているコマンドの横で次の op run が通った。"""
        tok = self.claim()
        for n, sig in enumerate((signal.SIGTERM, signal.SIGHUP, signal.SIGINT)):
            with self.subTest(sig=sig):
                key, go, ready = f"push:feat@{n}", self.go_file(f"go{n}"), Path(self.tmp.name) / f"ready{n}"
                body = (f"import os, signal, sys, time\nsignal.signal({int(sig)}, lambda *_: print('got', {int(sig)}, flush=True))\n"
                        f"open({str(ready)!r}, 'w')\nwhile not os.path.exists({str(go)!r}): time.sleep(0.05)\nsys.exit(5)")
                p = self.start_run(tok, key, body)
                self.wait_file(ready)  # コマンドが扱いを登録した
                os.kill(self.supervisor_of(ready), sig)
                time.sleep(0.3)
                self.assertEqual(self.run_op(tok, "pr:feat", "--reviewed", "none:x", *self.script("print(1)"))[0], 4)  # まだ動いている
                go.write_text("")
                rec = self.wait_done(key)
                self.assertIn("exit 5", rec["result"])
                self.assertIn(f"got {int(sig)}", rec["result"])  # コマンドに伝えた
                self.assertEqual(p.wait(), 5)

    def test_killing_the_whole_group_of_op_run_leaves_the_supervisor(self):
        tok = self.claim()
        go = self.go_file()
        p = self.start_run(tok, "push:feat@1", f"import os, sys, time\nwhile not os.path.exists({str(go)!r}): time.sleep(0.05)\nsys.exit(4)",
                           start_new_session=True)
        self.wait_running("push:feat@1")
        os.killpg(p.pid, signal.SIGKILL)  # 子孫ごと止めるハーネスの代わり
        p.wait()
        time.sleep(0.3)
        self.wait_running("push:feat@1")
        go.write_text("")
        self.assertIn("exit 4", self.wait_done("push:feat@1")["result"])

    def test_killing_op_run_and_all_its_descendants_leaves_the_supervisor(self):
        """ハーネスは、止めるプロセスの子孫を木ごと止める（Claude Code の TaskStop で、監督役もコマンドも死んだ）。"""
        tok = self.claim()
        go = self.go_file()
        ready = Path(self.tmp.name) / "ready"
        p = self.start_run(tok, "push:feat@1", f"import os, sys, time\nopen({str(ready)!r}, 'w')\nwhile not os.path.exists({str(go)!r}): time.sleep(0.05)\nsys.exit(6)")
        self.wait_file(ready)
        self.assertNotEqual(subprocess.run(["ps", "-o", "ppid=", "-p", str(self.supervisor_of(ready))], capture_output=True, text=True).stdout.strip(),
                            str(p.pid))  # 監督役は、もう op run の子ではない
        rows = [x.split() for x in subprocess.run(["ps", "-eo", "pid=,ppid="], capture_output=True, text=True).stdout.splitlines()]
        tree, grew = {p.pid}, True
        while grew:
            more = {int(pid) for pid, ppid in rows if int(ppid) in tree}
            grew = bool(more - tree)
            tree |= more
        for pid in tree:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        p.wait()
        time.sleep(0.3)
        self.wait_running("push:feat@1")
        go.write_text("")
        self.assertIn("exit 6", self.wait_done("push:feat@1")["result"])

    def test_a_broken_lease_file_at_the_end_is_reported_not_silent(self):
        tok = self.claim()
        good = self.lease_file().read_text()
        for n, broken in enumerate(("'{'", "json.dumps([1])", "json.dumps({**json.loads(open(f).read()), 'token': None, 'holder': None})[:-1] + ', \"x\": 1}'")):
            with self.subTest(broken=broken):
                body = (f"import json, os, subprocess; f = {str(self.lease_file())!r}; s = {broken}; open(f + '.t', 'w').write(s); os.replace(f + '.t', f); "
                        "print('left', subprocess.Popen(['sleep', '2'], close_fds=False).pid)")  # ロックを受け継いだ残り
                rc, r = self.run_op(tok, f"push:feat@{n}", "--reviewed", self.head(), *self.script(body))
                self.assertEqual(rc, 1 if n < 2 else 3, r)
                if n < 2:
                    self.assertIn("done を書けない", r["reason"])  # 監督役は黙って落ちず、理由を返す
                self.lease_file().write_text(good)
                pend = {o["key"]: o.get("running") for o in self.w("op", "pending", "--dir", str(self.wt))[1]["pending_ops"]}
                self.assertEqual(pend.get(f"push:feat@{n}", "absent"), None)  # 照合で閉じる。コマンドは終わったので、残りがあっても動いているとは数えない
                self.pid_gone(int(r["output"].split("left ")[1].split()[0]))

    def test_a_finished_op_cannot_be_closed_again(self):
        tok = self.claim()
        for _ in range(2):  # 手で閉じた操作も、閉じ直させない
            rc = self.w("op", ("begin" if _ == 0 else "done"), "--dir", str(self.wt), "--token", tok, "--key", "local:m", "--what", "x", "--result", "x")[0]
        self.assertEqual(rc, 0)
        self.assertEqual(self.w("op", "done", "--dir", str(self.wt), "--token", tok, "--key", "local:m", "--result", "y")[0], 4)
        rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script("import sys; print('rejected'); sys.exit(1)"))
        self.assertEqual(rc, 1)
        self.assertEqual(self.w("op", "done", "--dir", str(self.wt), "--token", tok, "--key", "push:feat@1", "--result", "照合した")[0], 4)
        rec = [json.loads(x) for x in self.ops_file().decode().splitlines() if json.loads(x)["key"] == "push:feat@1"][-1]
        self.assertIn("rejected", rec["result"])  # 本当の結果が残る

    def test_a_key_with_spaces_is_matched_as_written(self):
        tok = self.claim()
        go = self.go_file()
        key = "push:x "
        p = self.start_run(tok, key, f"import os, time\nwhile not os.path.exists({str(go)!r}): time.sleep(0.05)")
        self.wait_running(key)
        self.assertEqual(self.w("op", "done", "--dir", str(self.wt), "--token", tok, "--key", key, "--result", "x")[0], 4)
        go.write_text("")
        p.wait()

    def outputs(self):
        return sorted(x.name for x in self.lease_file().parent.iterdir()
                      if x.name not in ("lease.json", "ops.jsonl", "note.md", "evidence", "supervise.err", ".mutex", "task.json")
                      and not x.name.endswith(".lock"))

    def test_a_stop_before_the_command_starts_does_not_start_it(self):
        """監督役が起こす前に止めろと言われたら、外部操作を始めない（起こしてから殺すと、結果の分からない push を作る）。"""
        tok = self.claim()
        r = self.lease_file().parent
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            with self.subTest(sig=sig):
                key, marker = f"local:x{int(sig)}", Path(self.tmp.name) / f"ran{int(sig)}"
                self.assertEqual(self.w("op", "begin", "--dir", str(self.wt), "--token", tok, "--key", key, "--what", "x")[0], 0)
                helper = (  # op run と同じく、ロックを取り、シグナルを塞いで監督役を起こす。起こす前に止めを保留させておく
                    "import fcntl, json, os, signal, sys\n"
                    f"fd = os.open({str(r / ('running-' + str(int(sig)) + '.lock'))!r}, os.O_RDWR | os.O_CREAT); fcntl.flock(fd, fcntl.LOCK_EX); os.write(fd, {key!r}.encode())\n"
                    "os.set_inheritable(fd, True)\n"
                    f"signal.pthread_sigmask(signal.SIG_BLOCK, {{{int(sig)}}}); os.kill(os.getpid(), {int(sig)})\n"
                    f"spec = json.dumps({{'dir': {str(self.wt)!r}, 'token': {tok!r}, 'key': {key!r}, 'cwd': {str(self.wt)!r}, 'lock_fd': fd}})\n"
                    f"os.execv(sys.executable, [sys.executable, {str(WORK)!r}, '_supervise', spec, '--', sys.executable, '-c', \"open({str(marker)!r}, 'w')\"])")
                p = subprocess.run([sys.executable, "-c", helper], env=self.env, capture_output=True, text=True)
                rec = self.wait_done(key)
                self.assertIn(f"exit {128 + int(sig)}", rec["result"])
                self.assertIn("起動の前に止められた", rec["result"])
                self.assertFalse(marker.exists())
                self.assertNotIn("Traceback", p.stderr)  # 監督役が終わり際に落ちていない
                self.assertNotIn("Traceback", (r / "supervise.err").read_text() if (r / "supervise.err").exists() else "")

    def stop_at(self, gate, at, ran, extra=""):
        tok = self.claim() if not hasattr(self, "gtok") else self.gtok
        self.gtok = tok
        gate.mkdir(exist_ok=True)
        self.gos = [*getattr(self, "gos", []), gate / "before-check", gate / "before-start"]  # 落ちても監督役を待たせたままにしない
        key = f"push:{at}"
        for name in ("after-begin", "relay", "relay-told", "before-done", "before-print", *(["before-check"] if at == "before-start" else [])):
            (gate / name).write_text("")  # 手前の止まり所は通す
        p = self.start_run(tok, key, f"import sys, time\n{extra}open({str(ran)!r}, 'w')\ntime.sleep(1)", env={"BB_WRIT_SUPERVISE_GATE": str(gate)})
        self.wait_file(gate / f"at-{at}")
        os.kill(int((gate / f"at-{at}").read_text()), signal.SIGTERM)  # 監督役に、窓の中で止めを送る
        time.sleep(0.2)
        for name in ("before-check", "before-start"):
            (gate / name).write_text("")
        p.wait()
        return self.wait_done(key)["result"]

    def test_a_stop_right_after_the_fork_does_not_start_the_command(self):
        ran = Path(self.tmp.name) / "ran1"
        rec = self.stop_at(Path(self.tmp.name) / "gate1", "before-check", ran)
        self.assertIn("exit 143", rec)
        self.assertIn("起動の前に止められた", rec)
        self.assertFalse(ran.exists())

    def test_a_stop_right_before_the_start_is_passed_to_the_command(self):
        ran = Path(self.tmp.name) / "ran2"
        rec = self.stop_at(Path(self.tmp.name) / "gate2", "before-start", ran)
        self.assertNotIn("exit 0", rec)  # 起こしたコマンドに伝わり、最後までは走らない
        self.assertIn("exit 143", rec)

    def test_killing_the_relay_before_it_tells_does_not_start_the_command(self):
        """中継が保留を渡す前に殺されたら、止めとみなす（空の報せを「止め無し」と読んで起こした）。"""
        tok = self.claim()
        gate, ran = Path(self.tmp.name) / "gate3", Path(self.tmp.name) / "ran3"
        gate.mkdir()
        self.gos = [*getattr(self, "gos", []), gate / "relay", gate / "relay-told", gate / "before-check", gate / "before-start"]
        (gate / "after-begin").write_text("")
        p = self.start_run(tok, "push:feat@1", f"open({str(ran)!r}, 'w')", env={"BB_WRIT_SUPERVISE_GATE": str(gate)}, start_new_session=True)
        self.wait_file(gate / "at-relay")
        os.kill(int((gate / "at-relay").read_text()), signal.SIGKILL)
        os.killpg(p.pid, signal.SIGKILL)
        p.wait()
        for name in ("relay-told", "before-check", "before-start", "before-done", "before-print"):
            (gate / name).write_text("")
        rec = self.wait_done("push:feat@1")["result"]
        self.assertIn("exit 137", rec)
        self.assertIn("起動の前に止められた", rec)
        self.assertFalse(ran.exists())

    def test_a_leftover_does_not_block_once_the_command_ended_even_if_not_recorded(self):
        """done を書けなくても、コマンドが終われば二重に走るおそれは無い。残ったもの（credential daemon など）で 15 分止めない。"""
        tok = self.claim()
        body = ("import json, os, subprocess; f = " + repr(str(self.lease_file())) + "; "
                "open(f + '.t', 'w').write(json.dumps({**json.load(open(f)), 'token': 'other'})); os.replace(f + '.t', f); "
                "print('left', subprocess.Popen(['sleep', '2'], close_fds=False).pid)")
        rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script(body))
        self.assertEqual(rc, 3)  # 担当を失って記録できなかった
        left = int(r["output"].split("left ")[1].split()[0])
        self.assertEqual([(o["key"], o.get("running")) for o in self.w("op", "pending", "--dir", str(self.wt))[1]["pending_ops"]],
                         [("push:feat@1", None)])  # begun は照合のために残り、動いているとは数えない
        self.assertEqual(self.w("claim", "--dir", str(self.wt), "--holder", "B")[0], 3)  # 担当 other が生きているだけ
        self.pid_gone(left)

    def test_the_lock_file_is_removed_once_recorded(self):
        tok = self.claim()
        self.assertEqual(self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script("print(1)"))[0], 0)
        self.assertEqual(list(self.lease_file().parent.glob("running*.lock")), [])

    def work_module(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("work_under_test", WORK)
        work = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(work)
        return work

    def test_only_the_end_of_a_huge_output_is_read(self):
        work, f, got = self.work_module(), Path(self.tmp.name) / "huge.log", []
        f.write_bytes(b"x" * (4 * 1024 * 1024) + b"\nlast line\n" + b"\n" * 200_000)  # 末尾は改行だけの塊

        def counting_open(path, mode="r", *a, **kw):  # 読んだバイト数を数える
            fh = open(path, mode, *a, **kw)
            real = fh.read
            fh.read = lambda n=-1: got.append(len(b := real(n))) or b
            return fh
        work.open = counting_open
        self.assertTrue(work.tail(f).endswith("last line"))  # 改行だけの塊を飛ばし、最後の意味のある行を残す
        self.assertLess(sum(got), 1024 * 1024)  # 全体（4 MiB 余り）は読まない
        got.clear()
        f.write_bytes(b"line\n" + b"\n" * (20 * 1024 * 1024))  # 改行だけが 20 MiB
        work.tail(f)
        self.assertLess(sum(got), 17 * 1024 * 1024)  # 16 MiB で諦める

    def test_too_long_keys_are_refused(self):
        tok = self.claim()
        for key in ("local:" + "x" * 1100, "local:" + "あ" * 400):  # 長さはバイトで数える（後者は 406 字・1206 バイト）
            self.assertEqual(self.w("op", "begin", "--dir", str(self.wt), "--token", tok, "--key", key, "--what", "x")[0], 2)
        self.assertEqual(self.pending(), [])

    def test_the_record_stays_small_after_json_escaping(self):
        tok = self.claim()
        rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script("import sys; sys.stdout.write('\\x01' * 20000)"))
        stored = lambda s: len(json.dumps(s, ensure_ascii=False).encode()) - 2  # ops.jsonl に書かれる形の大きさ
        self.assertLessEqual(stored(r["op"]["result"]), 8 * 1024)
        rc, r = self.run_op(tok, "push:feat@2", "--reviewed", self.head(), *self.script("import os; os.write(1, b'\\xff' * 20000)"))
        self.assertLessEqual(stored(r["op"]["result"]), 8 * 1024)  # 「exit N: 」と省いた注記を含めて 8 KiB
        rc, r = self.run_op(tok, "push:feat@3", "--reviewed", self.head(), *self.script("print('あ' * 20000)"))
        self.assertLessEqual(stored(r["op"]["result"]), 8 * 1024)
        self.assertGreater(stored(r["op"]["result"]), 7 * 1024)  # 日本語でも 8 KiB 近くまで使う（ちょうどは test_the_trim_keeps_the_longest_tail_that_fits）
        rc, r = self.run_op(tok, "push:feat@4", "--reviewed", self.head(), *self.script("print('z' * 20000)"))
        self.assertLessEqual(stored(r["op"]["result"]), 8 * 1024)  # 「exit 0: 」を含めて上限に収める
        rc, r = self.run_op(tok, "push:feat@5", "--reviewed", self.head(),
                            *self.script("import os, signal; print('z' * 20000, flush=True); os.kill(os.getpid(), signal.SIGKILL)"))
        self.assertLessEqual(stored(r["op"]["result"]), 8 * 1024)  # シグナルで終わっても（exit 137: の幅で数える）

    def test_the_test_gate_is_not_passed_to_the_command(self):
        tok = self.claim()
        gate = Path(self.tmp.name) / "gate4"
        gate.mkdir()
        for name in ("after-begin", "relay", "relay-told", "before-check", "before-start", "before-done", "before-print"):
            (gate / name).write_text("")
        p = self.start_run(tok, "push:feat@1", "import os; print('gate', os.environ.get('BB_WRIT_SUPERVISE_GATE'))", env={"BB_WRIT_SUPERVISE_GATE": str(gate)})
        p.wait()
        self.assertIn("gate None", self.wait_done("push:feat@1")["result"])

    def test_a_second_op_run_right_after_the_first_begins_is_refused(self):
        """ロックは begin と同じ排他の中で作る（外で作ると、その間に 2 本目が通った）。"""
        tok = self.claim()
        gate = Path(self.tmp.name) / "gate5"
        gate.mkdir()
        self.gos = [*getattr(self, "gos", []), gate / "after-begin"]
        for name in ("relay", "relay-told", "before-check", "before-start", "before-done", "before-print"):
            (gate / name).write_text("")
        p = self.start_run(tok, "push:feat@1", "print(1)", env={"BB_WRIT_SUPERVISE_GATE": str(gate)})
        self.wait_file(gate / "at-after-begin")
        self.assertEqual(self.run_op(tok, "pr:feat", "--reviewed", "none:x", *self.script("print(2)"))[0], 4)
        (gate / "after-begin").write_text("")
        p.wait()

    def test_the_op_counts_as_running_until_done_is_written(self):
        """終わった印は done と同じ排他の中で付ける。先に付けると、手での op done が監督役より先に閉じ、本当の結果を失う。"""
        tok = self.claim()
        gate = Path(self.tmp.name) / "gate6"
        gate.mkdir()
        self.gos = [*getattr(self, "gos", []), gate / "before-done"]
        for name in ("after-begin", "relay", "relay-told", "before-check", "before-start", "before-print"):
            (gate / name).write_text("")
        p = self.start_run(tok, "push:feat@1", "print('real result')", env={"BB_WRIT_SUPERVISE_GATE": str(gate)})
        self.wait_file(gate / "at-before-done")
        self.assertEqual([(o["key"], o.get("running")) for o in self.w("op", "pending", "--dir", str(self.wt))[1]["pending_ops"]],
                         [("push:feat@1", True)])
        self.assertEqual(self.w("op", "done", "--dir", str(self.wt), "--token", tok, "--key", "push:feat@1", "--result", "照合")[0], 4)
        (gate / "before-done").write_text("")
        p.wait()
        self.assertIn("real result", self.wait_done("push:feat@1")["result"])

    def test_unrecorded_output_is_kept_even_if_op_run_was_stopped(self):
        """done を書けず、読む op run も居ないときの出力は、照合の唯一の材料なので残す。"""
        tok = self.claim()
        ready, go = Path(self.tmp.name) / "ready7", self.go_file("go7")
        body = (f"import json, os, time\nprint('SECRET-EVIDENCE', flush=True)\nopen({str(ready)!r}, 'w')\n"
                f"while not os.path.exists({str(go)!r}): time.sleep(0.05)\n"
                f"f = {str(self.lease_file())!r}; open(f + '.t', 'w').write(json.dumps({{**json.load(open(f)), 'token': 'other'}})); os.replace(f + '.t', f)")
        p = self.start_run(tok, "push:feat@1", body)
        self.wait_file(ready)
        p.kill()
        p.wait()
        go.write_text("")
        for _ in range(200):
            pend = self.w("op", "pending", "--dir", str(self.wt))[1]["pending_ops"]
            if pend and not pend[0].get("running"):
                break
            time.sleep(0.05)
        else:
            self.fail("監督役が終わらない")
        self.assertIn("SECRET-EVIDENCE", Path(pend[0]["output"]).read_text())

    def test_unrecorded_output_is_kept_when_op_run_is_alive(self):
        tok = self.claim()
        body = ("import json, os; f = " + repr(str(self.lease_file())) + "; print('SECRET-EVIDENCE'); "
                "open(f + '.t', 'w').write(json.dumps({**json.load(open(f)), 'token': 'other'})); os.replace(f + '.t', f)")
        rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script(body))
        self.assertEqual(rc, 3)
        pend = self.w("op", "pending", "--dir", str(self.wt))[1]["pending_ops"]
        self.assertIn("SECRET-EVIDENCE", Path(pend[0]["output"]).read_text())  # 別のセッションの後任にも材料を残す

    def test_a_refused_op_done_keeps_the_output(self):
        tok = self.claim()
        self.assertEqual(self.w("op", "begin", "--dir", str(self.wt), "--token", tok, "--key", "local:y", "--what", "x")[0], 0)
        work = self.work_module()
        log = work.run_log(self.lease_file().parent, "local:y")
        log.write_text("evidence")
        self.assertEqual(self.w("op", "done", "--dir", str(self.wt), "--token", "bad", "--key", "local:y", "--result", "x")[0], 3)
        self.assertTrue(log.is_file())  # 拒否された done では消さない
        self.assertEqual(self.w("op", "done", "--dir", str(self.wt), "--token", tok, "--key", "local:y", "--result", "x")[0], 0)
        self.assertFalse(log.is_file())

    def test_the_gate_gives_up_after_a_while_and_ignores_a_missing_directory(self):
        work = self.work_module()
        clock = iter(range(0, 10_000, 10))
        work.time = type("T", (), {"time": staticmethod(lambda: next(clock)), "sleep": staticmethod(lambda s: None)})
        gate = Path(self.tmp.name) / "gate8"
        gate.mkdir()
        os.environ["BB_WRIT_SUPERVISE_GATE"] = str(gate)
        self.addCleanup(os.environ.pop, "BB_WRIT_SUPERVISE_GATE", None)
        work.gate("never")  # 60 秒（偽の時計）で先へ進む
        self.assertTrue((gate / "at-never").is_file())
        self.assertLessEqual(next(clock), 80)  # 60 秒を少し過ぎたところで諦めた
        os.environ["BB_WRIT_SUPERVISE_GATE"] = str(gate / "missing")
        work.gate("x")  # 無い場所なら、すぐ進む

    @unittest.skipUnless(PERMISSIONS_BIND, "権限の制限が効かない（root など）")
    def test_a_lock_file_that_cannot_be_removed_after_done_is_not_reported_as_a_failed_done(self):
        tok = self.claim()
        d = self.lease_file().parent
        try:
            rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script(f"import os; print('ok'); os.chmod({str(d)!r}, 0o555)"))
        finally:
            os.chmod(d, 0o755)  # 一時ディレクトリを消せるように戻す
        self.assertEqual(rc, 0, r)  # done は書けた。ロックのファイルを消せなかっただけ
        self.assertEqual(r["op"]["state"], "done")

    @unittest.skipUnless(PERMISSIONS_BIND, "権限の制限が効かない（root など）")
    def test_the_gate_skips_a_place_it_cannot_write(self):
        work, gate = self.work_module(), Path(self.tmp.name) / "gate9"
        gate.mkdir()
        os.chmod(gate, 0o555)
        os.environ["BB_WRIT_SUPERVISE_GATE"] = str(gate)
        try:
            work.gate("x")  # 書けなければ、止まらずに進む（例外を出さない）
        finally:
            os.environ.pop("BB_WRIT_SUPERVISE_GATE", None)
            os.chmod(gate, 0o755)

    def test_a_failed_command_with_a_recorded_result_leaves_no_output(self):
        tok = self.claim()
        rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script("print('SECRET'); raise SystemExit(1)"))
        self.assertEqual((rc, r["op"]["state"]), (1, "done"))
        self.assertEqual(self.outputs(), [])  # 失敗しても、記録できたなら全体の出力は残さない
        ready = Path(self.tmp.name) / "ready-f2"
        p = self.start_run(tok, "push:feat@2", f"import time; open({str(ready)!r}, 'w'); time.sleep(1); print('SECRET'); raise SystemExit(1)")
        self.wait_file(ready)  # 監督役がコマンドを起こした
        p.kill()
        p.wait()
        self.wait_done("push:feat@2")
        for _ in range(100):
            if not self.outputs():
                break
            time.sleep(0.05)
        self.assertEqual(self.outputs(), [])  # op run が居なくても、記録できたなら監督役が消す

    def test_output_is_removed_before_it_is_shown(self):
        """表示の途中で op run が止められても、記録できた出力の全体を残さない。"""
        tok = self.claim()
        p = subprocess.Popen([sys.executable, str(WORK), "op", "run", "--dir", str(self.wt), "--token", tok, "--key", "push:feat@1",
                              "--what", "x", "--from", str(self.repo), "--verified", self.head(), "--reviewed", self.head(), "--risk", "低:試験の外",
                              *self.script("print('SECRET' * 2_000_000)")], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(p.stderr.close)
        p.stderr.read(1024)  # 表示が始まった
        p.kill()
        p.wait()
        self.wait_done("push:feat@1")
        self.assertEqual(self.outputs(), [])

    @unittest.skipUnless(PERMISSIONS_BIND, "権限の制限が効かない（root など）")
    def test_the_supervisor_survives_when_the_mutex_cannot_be_opened(self):
        tok = self.claim()
        m = self.lease_file().parent / ".mutex"
        try:
            rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script(f"import os; os.chmod({str(m)!r}, 0)"))
        finally:
            os.chmod(m, 0o644)
        self.assertEqual(rc, 1, r)
        self.assertIn("done を書けない", r["reason"])  # 黙って落ちず、理由を返す

    @unittest.skipUnless(PERMISSIONS_BIND, "権限の制限が効かない（root など）")
    def test_losing_the_lease_and_failing_to_remove_the_lock_still_says_3(self):
        tok = self.claim()
        d = self.lease_file().parent
        body = ("import json, os; f = " + repr(str(self.lease_file())) + "; "
                "open(f + '.t', 'w').write(json.dumps({**json.load(open(f)), 'token': 'other'})); os.replace(f + '.t', f); "
                f"os.chmod({str(d)!r}, 0o555)")
        try:
            rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script(body))
        finally:
            os.chmod(d, 0o755)
        self.assertEqual(rc, 3, r)  # 担当を失った（書くのをやめる）を「done を書けない」に化けさせない

    def test_a_lock_left_after_done_is_not_counted(self):
        """done を書いた直後に監督役が殺され、ロックのファイルが残っても、その操作は終わっている。"""
        tok = self.claim()
        self.assertEqual(self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script("print(1)"))[0], 0)
        work = self.work_module()
        lock = work.run_lock(self.lease_file().parent, "push:feat@1")
        holder = subprocess.Popen([sys.executable, "-c", f"import fcntl, os, time; fd = os.open({str(lock)!r}, os.O_RDWR | os.O_CREAT); "
                                   "os.write(fd, b'push:feat@1'); fcntl.flock(fd, fcntl.LOCK_EX); print('held', flush=True); time.sleep(60)"],
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        try:
            self.assertEqual(self.run_op(tok, "pr:feat", "--reviewed", "none:x", *self.script("print(2)"))[0], 0)
        finally:
            holder.kill()
        holder.wait()

    def test_the_key_limit_is_exactly_1024_bytes(self):
        tok = self.claim()
        self.assertEqual(self.w("op", "begin", "--dir", str(self.wt), "--token", tok, "--key", "local:" + "x" * 1018, "--what", "x")[0], 0)
        self.assertEqual(self.w("op", "begin", "--dir", str(self.wt), "--token", tok, "--key", "local:" + "x" * 1019, "--what", "x")[0], 2)

    def test_the_trim_keeps_the_longest_tail_that_fits(self):
        work, f = self.work_module(), Path(self.tmp.name) / "o.log"
        f.write_text("".join(chr(0x3042 + i % 80) for i in range(5000)))
        size = lambda s: len(json.dumps(s, ensure_ascii=False).encode()) - 2
        for budget in (1000, 2047, 4096):
            got = work.tail(f, budget=budget)
            body = got.removeprefix("（先頭を省いた）")
            self.assertLessEqual(size(got), budget)
            longer = f.read_text()[-(len(body) + 1):]
            self.assertGreater(size("（先頭を省いた）" + longer), budget)  # もう 1 字足すと超える

    def test_tail_gives_up_after_16_mib_of_blank_lines(self):
        work, f = self.work_module(), Path(self.tmp.name) / "blank.log"
        f.write_bytes(b"A\n" + b"\n" * (16 * 1024 * 1024 + 30 * 1024))  # 16 MiB 遡った先の 64 KiB には A が入る
        self.assertEqual(work.tail(f), "")  # 16 MiB で諦めたら、その先は読まない

    def test_the_record_drops_only_the_trailing_blank_part(self):
        """出力の末尾の空白（改行を含む）は記録しない。行の中の空白と、行頭の空白は変えない。"""
        work, f = self.work_module(), Path(self.tmp.name) / "sp.log"
        for data, want in ((b"  first  \n  CODE  \n \n\t\n", "  first  \n  CODE"),
                           (b"abc   ", "abc"),
                           (b"  \n", ""), (b" \t \n\n", ""),
                           (b" " * 100000, ""), (b" " * 100000 + b"\n", ""), (b"\t" * 70000 + b"\n\n", ""),  # 空白だけは空
                           (b"ERROR: x" + b" " * 70000, "ERROR: x"),  # 行末の空白が 64 KiB を超えても中身を残す
                           (b"abc" + b" " * (70 * 1024) + b"\n" + b" \n" * 40000, "abc")):
            f.write_bytes(data)
            self.assertEqual(work.tail(f, budget=10 ** 9), want, data[:20])
        f.write_bytes(b"ab\n" * 30000)  # 64 KiB を超えるが、窓の先頭は行の頭で、21 行以上ある
        self.assertEqual(work.tail(f), "\n".join(["ab"] * 20))
        f.write_bytes(b"x" * 100000)  # 1 行が窓より長い
        self.assertTrue(work.tail(f, budget=10 ** 9).startswith("（先頭を省いた）"))
        f.write_bytes(b"x" * 100 + b"\n" + b"y" * (64 * 1024))  # 窓の先頭がちょうど行の頭
        self.assertEqual(work.tail(f, budget=10 ** 9), "y" * (64 * 1024))
        f.write_bytes(b"aaa " + b"y" * (64 * 1024))  # 1 バイト前が空白（改行ではない）なら、行の途中
        self.assertTrue(work.tail(f, budget=10 ** 9).startswith("（先頭を省いた）y"))
        for ch in ("あ", "\U0001F600"):  # 3 バイトと 4 バイトの文字
            for pad in range(4):  # 窓の先頭が文字のどの位置でも、壊れた文字を出さない
                f.write_bytes(ch.encode() * 30000 + b"x" * pad)
                self.assertNotIn("\ufffd", work.tail(f, budget=10 ** 9), (ch, pad))
        f.write_bytes("ok\u3000\u3000\n".encode())  # 全角の空白は残す（落とすのは ASCII の空白だけ）
        self.assertEqual(work.tail(f), "ok\u3000\u3000")
        f.write_bytes(b"\xff\xfeA\n")  # 壊れたバイトは黙って消さず、置き換えて残す
        self.assertEqual(work.tail(f), "\ufffd\ufffdA")
        f.write_bytes(b"A\n\n\nB\n")  # 間の空行は残す
        self.assertEqual(work.tail(f), "A\n\n\nB")
        f.write_bytes(b"x" * 100 + b"\r" + b"y" * (64 * 1024))  # \r は行の区切りではない
        self.assertTrue(work.tail(f, budget=10 ** 9).startswith("（先頭を省いた）"))
        line = b"z" * 3000
        f.write_bytes(b"q" * 8000 + b"\n".join([line] * 20))  # 窓の中がちょうど 20 行で、先頭は行の途中
        start = len(f.read_bytes()) - 64 * 1024
        self.assertTrue(start > 0 and f.read_bytes()[start - 1:start] != b"\n")
        got = work.tail(f, budget=10 ** 9)
        self.assertTrue(got.startswith("（先頭を省いた）"), got[:12])

    def test_a_lock_file_that_vanishes_while_checking_is_skipped(self):
        work, r = self.work_module(), Path(self.tmp.name) / "rh"
        r.mkdir()
        (r / "running-x.lock").write_text("push:x")
        real = work.os.open

        def vanish(path, *a, **kw):  # glob の後、開く前に監督役が消した
            if str(path).endswith(".lock"):
                raise FileNotFoundError(errno.ENOENT, "消えた", str(path))
            return real(path, *a, **kw)
        work.os = type("O", (), {**{k: getattr(os, k) for k in dir(os) if not k.startswith("__")}, "open": staticmethod(vanish)})
        self.assertIsNone(work.run_holder(r))

    @unittest.skipUnless(PERMISSIONS_BIND, "権限の制限が効かない（root など）")
    def test_the_supervisor_survives_when_neither_done_nor_the_lock_removal_works(self):
        tok = self.claim()
        d, m = self.lease_file().parent, self.lease_file().parent / ".mutex"
        try:
            rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script(f"import os; os.chmod({str(m)!r}, 0); os.chmod({str(d)!r}, 0o555)"))
        finally:
            os.chmod(d, 0o755)
            os.chmod(m, 0o644)
        self.assertEqual(rc, 1, r)
        self.assertIn("done を書けない", r["reason"])

    def test_closing_an_already_finished_op_removes_its_output(self):
        tok = self.claim()
        self.assertEqual(self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script("print(1)"))[0], 0)
        log = self.work_module().run_log(self.lease_file().parent, "push:feat@1")
        log.write_text("left by a stopped op run")
        self.assertEqual(self.w("op", "done", "--dir", str(self.wt), "--token", tok, "--key", "push:feat@1", "--result", "x")[0], 4)
        self.assertFalse(log.is_file())  # 既に終わっていた。誰も消さない出力を残さない

    def test_a_supervisor_lost_after_done_returns_the_recorded_result(self):
        tok = self.claim()
        gate = Path(self.tmp.name) / "gate10"
        gate.mkdir()
        self.gos = [*getattr(self, "gos", []), gate / "before-print"]
        for name in ("after-begin", "relay", "relay-told", "before-check", "before-start", "before-done"):
            (gate / name).write_text("")
        p = subprocess.Popen([sys.executable, str(WORK), "op", "run", "--dir", str(self.wt), "--token", tok, "--key", "push:feat@1",
                              "--what", "x", "--from", str(self.repo), "--verified", self.head(), "--reviewed", self.head(), "--risk", "低:試験の外",
                              *self.script("print('SECRET')")], env={**self.env, "BB_WRIT_SUPERVISE_GATE": str(gate)},
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.wait_file(gate / "at-before-print")
        os.kill(int((gate / "at-before-print").read_text()), signal.SIGKILL)
        r = json.loads(p.communicate()[0])
        self.assertEqual(r["op"]["state"], "done")  # 照合を求めず、記録の結果を返す
        self.assertIn("SECRET", r["op"]["result"])
        self.assertEqual(self.outputs(), [])

    @unittest.skipUnless(PERMISSIONS_BIND, "権限の制限が効かない（root など）")
    def test_an_unreadable_record_still_returns_the_result(self):
        tok = self.claim()
        f = self.lease_file().parent / "ops.jsonl"
        try:
            rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script(f"import os; print('OUT'); os.chmod({str(f)!r}, 0)"))
        finally:
            os.chmod(f, 0o644)
        self.assertEqual(rc, 1)
        self.assertIn("done を書けない", r["reason"])  # 結果の JSON が届く
        self.assertIn("OUT", r["output"])

    def test_a_paused_op_run_does_not_keep_recorded_output(self):
        """op run が一時停止（Ctrl-Z）されても、done を書けた出力は監督役が消す。"""
        tok = self.claim()
        p = self.start_run(tok, "push:feat@1", "import time; time.sleep(0.5); print('SECRET-AFTER-DONE')")
        self.wait_running("push:feat@1")
        p.send_signal(signal.SIGSTOP)
        try:
            self.wait_done("push:feat@1")
            for _ in range(100):
                if not self.outputs():
                    break
                time.sleep(0.05)
            self.assertEqual(self.outputs(), [])
        finally:
            p.send_signal(signal.SIGCONT)
            p.wait()

    @unittest.skipUnless(PERMISSIONS_BIND, "権限の制限が効かない（root など）")
    def test_a_lock_that_cannot_be_opened_or_read_counts_as_running(self):
        tok = self.claim()
        work, r = self.work_module(), self.lease_file().parent
        for key, broken in (("push:y", "chmod"), ("push:z", "dir")):
            with self.subTest(broken=broken):
                self.assertEqual(self.w("op", "begin", "--dir", str(self.wt), "--token", tok, "--key", key, "--what", "x",
                                        "--from", str(self.repo), "--verified", self.head(), "--reviewed", self.head())[0], 0)
                lock = work.run_lock(r, key)
                if broken == "chmod":
                    lock.write_text(key)
                    lock.chmod(0)
                else:
                    lock.mkdir()  # 開けるが読めない
                try:
                    self.assertEqual(work.run_holder(r), key)  # ファイル名でなく鍵を返す
                    pend = {o["key"]: o.get("running") for o in self.w("op", "pending", "--dir", str(self.wt))[1]["pending_ops"]}
                    self.assertTrue(pend[key])  # 鍵で「動いている」と出る
                    self.assertEqual(self.w("op", "done", "--dir", str(self.wt), "--token", tok, "--key", key, "--result", "x")[0], 4)
                finally:
                    lock.chmod(0o644) if broken == "chmod" else None
                    lock.unlink() if broken == "chmod" else lock.rmdir()
                self.assertEqual(self.w("op", "done", "--dir", str(self.wt), "--token", tok, "--key", key, "--result", "x")[0], 0)

    @unittest.skipUnless(PERMISSIONS_BIND, "権限の制限が効かない（root など）")
    def test_a_failed_done_without_the_mutex_still_releases_the_lock_for_leftovers(self):
        """.mutex を開けずに done を書けなくても、ロックのファイルは排他なしで消す。残りがロックを持っていても次の操作を止めない。"""
        tok = self.claim()
        m = self.lease_file().parent / ".mutex"
        body = (f"import os, subprocess; os.chmod({str(m)!r}, 0); "
                "print('left', subprocess.Popen(['sleep', '3'], close_fds=False).pid)")
        try:
            rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script(body))
        finally:
            os.chmod(m, 0o644)
        self.assertEqual(rc, 1, r)
        left = int(r["output"].split("left ")[1].split()[0])
        self.assertEqual(self.run_op(tok, "pr:feat", "--reviewed", "none:x", *self.script("print(2)"))[0], 0)
        self.pid_gone(left)

    def test_tail_reads_back_exactly_16_mib(self):
        work, f = self.work_module(), Path(self.tmp.name) / "b16.log"
        f.write_bytes(b"A\n" + b"\n" * (16 * 1024 * 1024 - 32 * 1024))  # A は 16 MiB の内側
        self.assertEqual(work.tail(f), "A")

    @unittest.skipUnless(PERMISSIONS_BIND, "権限の制限が効かない（root など）")
    def test_output_that_cannot_be_removed_does_not_hold_the_lock_or_break_op_done(self):
        tok = self.claim()
        d = self.lease_file().parent
        work = self.work_module()
        log = work.run_log(d, "push:feat@1")
        try:  # 出力を消せない（置き場に書けない）。ロックも消せないが、op_step の結果を保ち、次の操作は数えない
            rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script(f"import os; print('x'); os.chmod({str(d)!r}, 0o555)"))
        finally:
            os.chmod(d, 0o755)
        self.assertEqual((rc, r["op"]["state"]), (0, "done"))
        self.assertIn("出力のファイルを消せない", (d / "supervise.err").read_text())
        os.chmod(d, 0o555)
        try:  # 既に終わった操作の op done で、出力を消せなくても、4 と結果を返す
            rc, r = self.w("op", "done", "--dir", str(self.wt), "--token", tok, "--key", "push:feat@1", "--result", "x")
        finally:
            os.chmod(d, 0o755)
        self.assertEqual(rc, 4)
        self.assertIn("消せない", r["warning"])
        self.assertTrue(log.is_file())

    def test_an_output_file_that_cannot_be_made_closes_the_op(self):
        tok = self.claim()
        work = self.work_module()
        work.run_log(self.lease_file().parent, "push:feat@1").mkdir()  # 出力のファイルを作れない
        marker = Path(self.tmp.name) / "ran-o"
        rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script(f"open({str(marker)!r}, 'w')"))
        self.assertEqual(rc, 127, r)
        self.assertFalse(marker.exists())
        self.assertEqual(self.pending(), [])  # 走っていないと記録して閉じる

    def test_a_supervisor_lost_after_done_returns_the_exit_code(self):
        tok = self.claim()
        gate = Path(self.tmp.name) / "gate11"
        gate.mkdir()
        self.gos = [*getattr(self, "gos", []), gate / "before-print"]
        for name in ("after-begin", "relay", "relay-told", "before-check", "before-start", "before-done"):
            (gate / name).write_text("")
        p = subprocess.Popen([sys.executable, str(WORK), "op", "run", "--dir", str(self.wt), "--token", tok, "--key", "push:feat@1",
                              "--what", "x", "--from", str(self.repo), "--verified", self.head(), "--reviewed", self.head(), "--risk", "低:試験の外",
                              *self.script("raise SystemExit(7)")], env={**self.env, "BB_WRIT_SUPERVISE_GATE": str(gate)},
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.wait_file(gate / "at-before-print")
        os.kill(int((gate / "at-before-print").read_text()), signal.SIGKILL)
        r = json.loads(p.communicate()[0])
        self.assertEqual((p.returncode, r["exit"], r["ok"]), (7, 7, False))  # 記録の終了コードを返す
        for name in ("before-print",):
            (gate / name).unlink(missing_ok=True)
            (gate / f"at-{name}").unlink(missing_ok=True)
        p = subprocess.Popen([sys.executable, str(WORK), "op", "run", "--dir", str(self.wt), "--token", tok, "--key", "push:feat@2",
                              "--what", "x", "--from", str(self.repo), "--verified", self.head(), "--reviewed", self.head(), "--risk", "低:試験の外",
                              *self.script("print('fine')")], env={**self.env, "BB_WRIT_SUPERVISE_GATE": str(gate)},
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.wait_file(gate / "at-before-print")
        os.kill(int((gate / "at-before-print").read_text()), signal.SIGKILL)
        r = json.loads(p.communicate()[0])
        self.assertEqual((p.returncode, r["exit"], r["ok"]), (0, 0, True))
        self.assertTrue(r["op"]["result"].startswith("exit 0:"))

    def test_a_lost_supervisor_and_an_unreadable_record_still_return_json(self):
        tok = self.claim()
        f = self.lease_file().parent / "ops.jsonl"
        for n, broken in enumerate(("{broken\n", '{"x": 1}\n', "[1]\n", '{"key": 1, "state": "begun"}\n', '{"key": ["a"]}\n', "[" * 200000 + "\n", None)):
            with self.subTest(broken=broken):
                gate = Path(self.tmp.name) / f"gate12-{n}"
                gate.mkdir()
                self.gos = [*getattr(self, "gos", []), gate / "before-print"]
                for name in ("after-begin", "relay", "relay-told", "before-check", "before-start", "before-done"):
                    (gate / name).write_text("")
                key = f"push:feat@{n}"
                p = subprocess.Popen([sys.executable, str(WORK), "op", "run", "--dir", str(self.wt), "--token", tok, "--key", key,
                                      "--what", "x", "--from", str(self.repo), "--verified", self.head(), "--reviewed", self.head(), "--risk", "低:試験の外",
                                      *self.script("print(1)")], env={**self.env, "BB_WRIT_SUPERVISE_GATE": str(gate)},
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                self.wait_file(gate / "at-before-print")
                good = f.read_text()
                if broken is None:  # 形は正しいが result が文字列でない
                    f.write_text(good + json.dumps({"key": key, "state": "done", "result": None}) + "\n")
                else:
                    f.write_text(broken)
                try:
                    os.kill(int((gate / "at-before-print").read_text()), signal.SIGKILL)
                    r = json.loads(p.communicate()[0])  # 落ちずに JSON を返す
                    self.assertIsNone(r["exit"])
                    if broken is None:  # 読めた記録は op に添える
                        self.assertEqual(r["op"]["state"], "done")
                    else:  # 読めなければ、直す行を案内する
                        self.assertIn("行目", r["reason"])
                        self.assertIn("その行を直して", r["reason"])
                finally:
                    f.write_text(good)  # 1 つ落ちても、後の subTest を道連れにしない

    @unittest.skipUnless(Path("/dev/fd").is_dir(), "開いた fd を数えられない")
    def test_run_holder_does_not_leak_fds_on_unreadable_locks(self):
        work, r = self.work_module(), Path(self.tmp.name) / "rh3"
        r.mkdir()
        (r / "running-d.lock").mkdir()  # 開けるが読めない
        before = len(os.listdir("/dev/fd"))
        for _ in range(50):
            work.run_holder(r)
        self.assertLessEqual(len(os.listdir("/dev/fd")), before + 1)

    def test_an_unreadable_lock_of_a_done_op_is_not_counted(self):
        tok = self.claim()
        self.assertEqual(self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script("print(1)"))[0], 0)
        work = self.work_module()
        work.run_lock(self.lease_file().parent, "push:feat@1").mkdir()  # 読めないロックが残った（鍵は done）
        self.assertEqual(self.run_op(tok, "pr:feat", "--reviewed", "none:x", *self.script("print(2)"))[0], 0)

    def test_an_unreadable_lock_is_matched_to_its_own_key_among_several(self):
        tok = self.claim()
        work = self.work_module()
        for key in ("push:a", "push:b"):
            self.assertEqual(self.w("op", "begin", "--dir", str(self.wt), "--token", tok, "--key", key, "--what", "x",
                                    "--from", str(self.repo), "--verified", self.head(), "--reviewed", self.head())[0], 0)
        lock = work.run_lock(self.lease_file().parent, "push:b")
        lock.mkdir()  # push:b のロックだけが読めない
        try:
            pend = {o["key"]: o.get("running") for o in self.w("op", "pending", "--dir", str(self.wt))[1]["pending_ops"]}
            self.assertEqual(pend, {"push:a": None, "push:b": True})
            self.assertEqual(self.w("op", "done", "--dir", str(self.wt), "--token", tok, "--key", "push:b", "--result", "x")[0], 4)
            self.assertEqual(self.w("op", "done", "--dir", str(self.wt), "--token", tok, "--key", "push:a", "--result", "x")[0], 0)
        finally:
            lock.rmdir()

    def test_an_unreadable_lock_without_a_record_still_counts(self):
        tok = self.claim()
        self.assertEqual(self.w("op", "begin", "--dir", str(self.wt), "--token", tok, "--key", "local:other", "--what", "x")[0], 0)  # ほかの記録があっても
        lock = self.lease_file().parent / "running.lock"  # 0.11.12 の名前。始めた記録に鍵が無い
        lock.mkdir()
        try:
            self.assertEqual(self.w("op", "begin", "--dir", str(self.wt), "--token", tok, "--key", "pr:feat", "--what", "x",
                                    "--from", str(self.repo), "--verified", self.head(), "--reviewed", "none:x")[0], 4)
        finally:
            lock.rmdir()

    @unittest.skipUnless(PERMISSIONS_BIND, "権限の制限が効かない（root など）")
    def test_a_lost_supervisor_with_a_record_that_cannot_be_read_by_permission(self):
        tok = self.claim()
        gate = Path(self.tmp.name) / "gate13"
        gate.mkdir()
        self.gos = [*getattr(self, "gos", []), gate / "before-print"]
        for name in ("after-begin", "relay", "relay-told", "before-check", "before-start", "before-done"):
            (gate / name).write_text("")
        f = self.lease_file().parent / "ops.jsonl"
        p = subprocess.Popen([sys.executable, str(WORK), "op", "run", "--dir", str(self.wt), "--token", tok, "--key", "push:feat@1",
                              "--what", "x", "--from", str(self.repo), "--verified", self.head(), "--reviewed", self.head(), "--risk", "低:試験の外",
                              *self.script("print(1)")], env={**self.env, "BB_WRIT_SUPERVISE_GATE": str(gate)},
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.wait_file(gate / "at-before-print")
        f.chmod(0)
        try:
            os.kill(int((gate / "at-before-print").read_text()), signal.SIGKILL)
            r = json.loads(p.communicate()[0])
        finally:
            f.chmod(0o644)
        self.assertIsNone(r["exit"])
        self.assertIn("記録を読めない", r["reason"])  # 行の形でなく、読めないことを伝える
        self.assertNotIn("その行を直して", r["reason"])

    def test_a_lost_supervisor_with_a_hand_written_result_does_not_invent_an_exit_code(self):
        tok = self.claim()
        gate = Path(self.tmp.name) / "gate14"
        gate.mkdir()
        self.gos = [*getattr(self, "gos", []), gate / "before-print"]
        for name in ("after-begin", "relay", "relay-told", "before-check", "before-start", "before-done"):
            (gate / name).write_text("")
        f = self.lease_file().parent / "ops.jsonl"
        p = subprocess.Popen([sys.executable, str(WORK), "op", "run", "--dir", str(self.wt), "--token", tok, "--key", "push:feat@1",
                              "--what", "x", "--from", str(self.repo), "--verified", self.head(), "--reviewed", self.head(), "--risk", "低:試験の外",
                              *self.script("print(1)")], env={**self.env, "BB_WRIT_SUPERVISE_GATE": str(gate)},
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.wait_file(gate / "at-before-print")
        with open(f, "a", encoding="utf-8") as fh:  # 別の人が照合の文で閉じ直した記録
            fh.write(json.dumps({"key": "push:feat@1", "state": "done", "result": "照合した: exit 0: 外部に出ていた", "by": None}, ensure_ascii=False) + "\n")
        os.kill(int((gate / "at-before-print").read_text()), signal.SIGKILL)
        r = json.loads(p.communicate()[0])
        self.assertIsNone(r["exit"])  # 終了コードをこしらえない
        self.assertEqual(p.returncode, 1)
        self.assertFalse(r["ok"])
        self.assertEqual(r["op"]["result"], "照合した: exit 0: 外部に出ていた")  # 記録を添える（途中の exit 0: は読まない）

    def test_locks_still_count_when_the_record_cannot_be_read(self):
        work, r = self.work_module(), Path(self.tmp.name) / "rh4"
        r.mkdir()
        (r / "running-u.lock").mkdir()  # 読めないロック。鍵も引けない
        for bad in ("{broken\n", '{"x": 1}\n', "[1]\n", "null\n", '{"key": ["a"]}\n', '{"key": 1, "state": "begun"}\n', "[" * 200000 + "\n"):
            (r / "ops.jsonl").write_text(bad)
            self.assertEqual(work.run_holder(r), "running-u.lock", bad[:20])
        (r / "ops.jsonl").write_text("{broken\n")
        (r / "running-u.lock").rmdir()
        lock = r / "running-h.lock"
        lock.write_text("push:h")
        holder = subprocess.Popen([sys.executable, "-c", f"import fcntl, os, time; fd = os.open({str(lock)!r}, os.O_RDWR); "
                                   "fcntl.flock(fd, fcntl.LOCK_EX); print('held', flush=True); time.sleep(60)"], stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            self.assertEqual(work.run_holder(r), "push:h")  # 取られているロックは、記録が読めなくても数える
        finally:
            holder.kill()
            holder.wait()

    def test_a_full_width_exit_in_a_hand_written_result_is_not_an_exit_code(self):
        tok = self.claim()
        gate = Path(self.tmp.name) / "gate15"
        gate.mkdir()
        self.gos = [*getattr(self, "gos", []), gate / "before-print"]
        for name in ("after-begin", "relay", "relay-told", "before-check", "before-start", "before-done"):
            (gate / name).write_text("")
        f = self.lease_file().parent / "ops.jsonl"
        p = subprocess.Popen([sys.executable, str(WORK), "op", "run", "--dir", str(self.wt), "--token", tok, "--key", "push:feat@1",
                              "--what", "x", "--from", str(self.repo), "--verified", self.head(), "--reviewed", self.head(), "--risk", "低:試験の外",
                              *self.script("print(1)")], env={**self.env, "BB_WRIT_SUPERVISE_GATE": str(gate)},
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.wait_file(gate / "at-before-print")
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"key": "push:feat@1", "state": "done", "result": "exit ０: 照合した", "by": None}, ensure_ascii=False) + "\n")
        os.kill(int((gate / "at-before-print").read_text()), signal.SIGKILL)
        r = json.loads(p.communicate()[0])
        self.assertIsNone(r["exit"])  # 全角の数字は終了コードと読まない

    def test_hand_written_results_that_look_like_exit_codes_are_not_read(self):
        tok = self.claim()
        f = self.lease_file().parent / "ops.jsonl"
        cases = [(r, {"by": None}) for r in ("exit 12abc", "exit " + "9" * 5000 + ": x", "exit 0: 照合で、外部に無かった", "exit 0 照合した", " exit 0: x")]
        cases += [("exit 0: 手で書いた", {}), ("exit 0: 手で書いた", {"state": "-"})]  # by の無い行（state も無い行）。前の行（監督役）の印を引き継がない
        cases += [(r, {"by": "supervise"}) for r in ("exit 256: x", "exit 1000: x", "exit " + "9" * 5000 + ": x")]  # 終了コードの値域の外
        for n, (result, by) in enumerate(cases):
            with self.subTest(result=result[:12], by=by):
                gate = Path(self.tmp.name) / f"gate16-{n}"
                gate.mkdir()
                self.gos = [*getattr(self, "gos", []), gate / "before-print"]
                for name in ("after-begin", "relay", "relay-told", "before-check", "before-start", "before-done"):
                    (gate / name).write_text("")
                key = f"push:feat@{n}"
                p = subprocess.Popen([sys.executable, str(WORK), "op", "run", "--dir", str(self.wt), "--token", tok, "--key", key,
                                      "--what", "x", "--from", str(self.repo), "--verified", self.head(), "--reviewed", self.head(), "--risk", "低:試験の外",
                                      *self.script("print(1)")], env={**self.env, "BB_WRIT_SUPERVISE_GATE": str(gate)},
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                self.wait_file(gate / "at-before-print")
                with open(f, "a", encoding="utf-8") as fh:
                    line = {"key": key, "state": "done", "result": result, **by}
                    fh.write(json.dumps({k: v for k, v in line.items() if v != "-"}) + "\n")
                os.kill(int((gate / "at-before-print").read_text()), signal.SIGKILL)
                r = json.loads(p.communicate()[0])
                self.assertIsNone(r["exit"])

    def test_a_record_broken_while_running_is_reported_and_the_lock_is_released(self):
        tok = self.claim()
        go = self.go_file("go-broken")
        p = self.start_run(tok, "push:feat@1", f"import os, time\nwhile not os.path.exists({str(go)!r}): time.sleep(0.05)")
        self.wait_running("push:feat@1")
        f = self.lease_file().parent / "ops.jsonl"
        good = f.read_text()
        f.write_text(good + '{"key": 1}\n')
        go.write_text("")
        r = json.loads(p.communicate()[0].decode().strip().split("\n")[-1])
        f.write_text(good)
        self.assertIn("done を書けない", r["reason"])  # 監督役は落ちずに結果を返す
        self.assertIn("行目", r["reason"])
        self.assertEqual(list(f.parent.glob("running-*.lock")), [])  # ロックを明け渡す

    def test_op_run_answers_in_json_when_the_record_is_broken(self):
        tok = self.claim()
        f = self.lease_file().parent / "ops.jsonl"
        f.write_text('{"key": 1}\n')
        touched = Path(self.tmp.name) / "ran"
        rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script(f"open({str(touched)!r}, 'w')"))
        self.assertEqual(rc, 1)
        self.assertIn("1 行目", r["reason"])
        self.assertFalse(touched.exists())  # 記録を読めないまま走らせない

    def test_a_claim_does_not_leave_a_lease_when_the_record_is_broken(self):
        tok = self.claim()
        lease = self.lease_file()
        self.assertEqual(self.w("release", "--dir", str(self.wt), "--token", tok)[0], 0)
        (lease.parent / "ops.jsonl").write_text('{"key": 1, "state": "begun"}\n')
        rc, r = self.w("claim", "--dir", str(self.wt), "--holder", "B")
        self.assertEqual(rc, 1)
        self.assertIn("1 行目", r["reason"])
        self.assertFalse(lease.exists())  # 誰も token を受け取らない担当を残さない

    def test_output_with_unicode_line_separators_keeps_the_record_readable(self):
        tok = self.claim()
        rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script("print('a\\u2028b\\x85c\\x1cd\\u2029e')"))
        self.assertEqual(rc, 0, r)
        rc, p = self.w("op", "pending", "--dir", str(self.wt))
        self.assertEqual((rc, p["pending_ops"]), (0, []))  # 記録を 1 行として読める
        rc, r = self.run_op(tok, "push:feat@2", "--reviewed", self.head(), *self.script("print(1)"))
        self.assertEqual(rc, 0, r)  # 次の操作も通る

    def test_a_last_record_line_without_a_newline_is_read(self):
        self.claim()
        (self.lease_file().parent / "ops.jsonl").write_text(json.dumps({"key": "push:x", "state": "begun"}))  # 改行で終わらない
        rc, r = self.w("op", "pending", "--dir", str(self.wt))
        self.assertEqual((rc, [o["key"] for o in r["pending_ops"]]), (0, ["push:x"]))  # 黙って捨てない

    def test_a_malformed_record_is_refused_with_the_line_number(self):
        tok = self.claim()
        f = self.lease_file().parent / "ops.jsonl"
        for bad in ('{"key": ["a"], "state": "begun"}', '{"key": 1}', "[1]", "{broken", '{"key": "push:\xff"}', "", "   "):
            with self.subTest(bad=bad):
                f.write_bytes((json.dumps({"key": "push:ok", "state": "begun"}) + "\n" + bad + "\n").encode("latin-1"))
                for args in (["op", "done", "--token", tok, "--key", "push:ok", "--result", "x"], ["op", "pending"], ["status"]):
                    rc, r = self.w(*args[:1 + (args[0] == "op")], "--dir", str(self.wt), *args[1 + (args[0] == "op"):])
                    self.assertEqual(rc, 1, args)  # 黙って続けず、直す場所を返す（トレースバックでなく JSON）
                    self.assertIn("2 行目", r["reason"])

    def test_a_supervisor_lost_while_running_returns_the_begun_record(self):
        tok = self.claim()
        gate = Path(self.tmp.name) / "gate17"
        gate.mkdir()
        self.gos = [*getattr(self, "gos", []), gate / "before-done"]
        for name in ("after-begin", "relay", "relay-told", "before-check", "before-start"):
            (gate / name).write_text("")
        p = subprocess.Popen([sys.executable, str(WORK), "op", "run", "--dir", str(self.wt), "--token", tok, "--key", "push:feat@1",
                              "--what", "x", "--from", str(self.repo), "--verified", self.head(), "--reviewed", self.head(), "--risk", "低:試験の外",
                              *self.script("print(1)")], env={**self.env, "BB_WRIT_SUPERVISE_GATE": str(gate)},
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.wait_file(gate / "at-before-done")
        os.kill(int((gate / "at-before-done").read_text()), signal.SIGKILL)
        r = json.loads(p.communicate()[0])
        self.assertIsNone(r["exit"])
        self.assertEqual(r["op"]["state"], "begun")  # 始めた記録を添える（壊れたのではない）

    def test_a_claim_refused_for_a_running_op_still_answers_when_the_record_is_broken(self):
        tok = self.claim()
        go = self.go_file("go-c")
        p = self.start_run(tok, "push:feat@1", f"import os, time\nwhile not os.path.exists({str(go)!r}): time.sleep(0.05)")
        self.wait_running("push:feat@1")
        f = self.lease_file().parent / "ops.jsonl"
        good = f.read_text()
        d = json.loads(self.lease_file().read_text())
        self.lease_file().write_text(json.dumps({**d, "expires_at": time.time() - 1}))
        try:
            f.write_text("{broken\n")
            rc, r = self.w("claim", "--dir", str(self.wt), "--holder", "B")
            self.assertEqual(rc, 1)  # 落ちずに、直す行を返す
            self.assertIn("1 行目", r["reason"])
            self.assertIn("push:feat@1", r["reason"])  # 走っている操作を伝え、終わってから直させる
            self.assertNotEqual(json.loads(self.lease_file().read_text())["holder"], "B")
        finally:
            f.write_text(good)
            go.write_text("")
            p.wait()

    def test_a_supervisor_that_cannot_start_closes_the_op(self):
        tok = self.claim()
        (self.lease_file().parent / "supervise.err").mkdir()  # 監督役の stderr を開けない
        marker = Path(self.tmp.name) / "ran"
        rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script(f"open({str(marker)!r}, 'w')"))
        self.assertEqual(rc, 127)
        self.assertIn("起動の準備ができない", r["op"]["result"])
        self.assertIn("コマンドは走っていない", r["reason"])  # 入口はこの句で「走っていない」と読む
        self.assertFalse(marker.exists())
        self.assertEqual(self.pending(), [])  # 走っていないと記録して閉じる
        self.assertEqual(self.outputs(), [])  # 空の出力のファイルも残さない

    @unittest.skipUnless(PERMISSIONS_BIND, "権限の制限が効かない（root など）")
    def test_unreadable_output_keeps_the_exit_code_of_the_command(self):
        tok = self.claim()
        body = "import glob, os, sys; [os.chmod(f, 0) for f in glob.glob(" + repr(str(self.lease_file().parent) + "/*.log") + ")]; sys.exit(9)"
        rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script(body))
        self.assertEqual(rc, 9)  # 走ったコマンドを「起動できない」にしない
        self.assertIn("出力を読めない", r["op"]["result"])
        for f in self.lease_file().parent.glob("*.log"):
            f.chmod(0o600)

    def test_the_output_is_removed_when_op_run_was_stopped(self):
        tok = self.claim()
        go = self.go_file()
        p = self.start_run(tok, "push:feat@1", f"import os, time\nwhile not os.path.exists({str(go)!r}): time.sleep(0.05)\nprint('secret')")
        self.wait_running("push:feat@1")
        p.kill()
        p.wait()
        go.write_text("")
        self.assertIn("secret", self.wait_done("push:feat@1")["result"])  # 末尾は記録に残る
        for _ in range(40):
            if not self.outputs():
                break
            time.sleep(0.05)
        self.assertEqual(self.outputs(), [])

    def test_only_external_begin_and_the_running_done_are_refused_while_running(self):
        tok = self.claim()
        self.assertEqual(self.w("op", "begin", "--dir", str(self.wt), "--token", tok, "--key", "local:old", "--what", "x")[0], 0)
        go = self.go_file()
        p = self.start_run(tok, "push:feat@1", f"import os, time\nwhile not os.path.exists({str(go)!r}): time.sleep(0.05)")
        self.wait_running("push:feat@1")
        got = {o["key"]: o.get("running") for o in self.w("op", "pending", "--dir", str(self.wt))[1]["pending_ops"]}
        self.assertEqual(got, {"local:old": None, "push:feat@1": True})  # 動いているのは監督役の操作だけ
        self.assertEqual(self.w("op", "begin", "--dir", str(self.wt), "--token", tok, "--key", "local:y", "--what", "x")[0], 0)
        self.assertEqual(self.w("op", "done", "--dir", str(self.wt), "--token", tok, "--key", "push:feat@1", "--result", "x")[0], 4)
        self.assertEqual(self.w("op", "done", "--dir", str(self.wt), "--token", tok, "--key", "local:old", "--result", "x")[0], 0)  # 動いていない操作は閉じてよい
        go.write_text("")
        p.wait()

    def pid_gone(self, pid):
        for _ in range(100):  # ゾンビ（親が刈り取っていない）も終わったとみなす
            st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
            if not st or st.startswith("Z"):
                return
            time.sleep(0.05)
        self.fail(f"{pid} が終わらない")

    def test_a_process_the_command_leaves_behind_does_not_block_once_recorded(self):
        """コマンドが残したもの（git の credential daemon など）はロックを受け継ぐ。記録の後は次の操作を止めない（15 分待たされた）。"""
        tok = self.claim()
        rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), "--", "sh", "-c", "sleep 3 >/dev/null 2>&1 & echo left $!")
        self.assertEqual(rc, 0)
        left = int(r["op"]["result"].split("left ")[1].split()[0])
        self.assertEqual(self.run_op(tok, "pr:feat", "--reviewed", "none:x", *self.script("print(1)"))[0], 0)  # 残りが動いていても始められる
        self.assertEqual(self.w("op", "pending", "--dir", str(self.wt))[1]["pending_ops"], [])
        self.pid_gone(left)

    def test_killing_the_supervisor_with_sigkill_does_not_let_the_op_run_twice(self):
        """監督役が kill -9 されても、コマンドがロックを持つので、後任は取れず、やり直せない。"""
        tok = self.claim()
        go, ready = self.go_file(), Path(self.tmp.name) / "ready"
        p = self.start_run(tok, "push:feat@1", f"import os, time\nopen({str(ready)!r}, 'w')\nwhile not os.path.exists({str(go)!r}): time.sleep(0.05)")
        self.wait_file(ready)
        sup = self.supervisor_of(ready)
        os.kill(sup, signal.SIGKILL)
        self.pid_gone(sup)
        d = json.loads(self.lease_file().read_text())
        self.lease_file().write_text(json.dumps({**d, "expires_at": time.time() - 1}))  # 延ばす者がいなくなり、期限が切れた
        self.assertEqual(self.w("claim", "--dir", str(self.wt), "--holder", "B")[0], 3)
        self.assertEqual(self.run_op(tok, "push:feat@1:retry", "--reviewed", self.head(), *self.script("print(1)"))[0], 4)  # やり直せない
        self.assertEqual([(o["key"], o.get("running")) for o in self.w("op", "pending", "--dir", str(self.wt))[1]["pending_ops"]],
                         [("push:feat@1", True)])
        self.assertIsNone(json.loads(p.stdout.read())["exit"])  # 結果を返さずに終わった。走ったかは分からない
        go.write_text("")
        p.wait()
        for _ in range(100):
            if not self.w("op", "pending", "--dir", str(self.wt))[1]["pending_ops"][0].get("running"):
                break
            time.sleep(0.05)
        pend = self.w("op", "pending", "--dir", str(self.wt))[1]["pending_ops"]
        self.assertEqual([(o["key"], o.get("running")) for o in pend], [("push:feat@1", None)])  # 終われば、後任が外部の状態と照合する
        self.assertTrue(Path(pend[0]["output"]).is_file())  # 照合の材料として出力を残す
        d = json.loads(self.lease_file().read_text())
        self.lease_file().write_text(json.dumps({**d, "expires_at": time.time() + 600}))
        self.assertEqual(self.w("op", "done", "--dir", str(self.wt), "--token", tok, "--key", "push:feat@1", "--result", "照合した")[0], 0)
        self.assertEqual(self.outputs(), [])  # 照合で閉じたら消す
        self.assertEqual(list(self.lease_file().parent.glob("*.lock")), [])  # 外れたロックのファイルも残さない

    def test_the_lease_is_extended_below_19_minutes_and_after_expiry_with_the_same_token(self):
        tok = self.claim()
        for shift in (5 * 60, -1):
            with self.subTest(shift=shift):
                body = ("import json, os, time; f = " + repr(str(self.lease_file())) + "; d = json.load(open(f)); "
                        f"d['expires_at'] = time.time() + {shift}; open(f + '.t', 'w').write(json.dumps(d)); os.replace(f + '.t', f); "
                        "time.sleep(2.5); print('left', int(json.load(open(f))['expires_at'] - time.time()))")
                rc, r = self.run_op(tok, f"push:feat@{shift}", "--reviewed", self.head(), *self.script(body))
                self.assertEqual(rc, 0, r)
                self.assertGreater(int(r["op"]["result"].split("left ")[1].split()[0]), 20 * 60 - 5)  # 20 分先まで

    def test_the_output_file_is_removed_after_it_is_read(self):
        tok = self.claim()
        self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script("print('first secret')"))
        self.assertEqual(self.outputs(), [])  # 名前を問わず、出力のファイルを残さない
        p = self.raw("op", "run", "--dir", str(self.wt), "--token", tok, "--key", "push:feat@2", "--what", "x", "--from", str(self.repo),
                     "--verified", self.head(), "--reviewed", self.head(), *self.script("print('second')"))
        self.assertIn("second", p.stderr)
        self.assertNotIn("first secret", p.stderr)

    def test_a_lease_file_without_an_expiry_does_not_stop_the_supervisor(self):
        tok = self.claim()
        for n, broken in enumerate(("d['expires_at'] = None", "del d['expires_at']", "d = [1]")):
            body = ("import json, os, time; f = " + repr(str(self.lease_file())) + "; good = open(f).read(); d = json.loads(good); "
                    f"{broken}; open(f + '.t', 'w').write(json.dumps(d)); os.replace(f + '.t', f); time.sleep(2.5); "
                    "open(f + '.t', 'w').write(good); os.replace(f + '.t', f)")
            rc, r = self.run_op(tok, f"push:feat@{n}", "--reviewed", self.head(), *self.script(body))
            self.assertEqual(rc, 0, (broken, r))
        errs = (self.lease_file().parent / "supervise.err").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(errs), len(set(errs)), errs)  # 毎秒の失敗でも、同じ文言は 1 回だけ
        self.assertIn("担当の期限を延ばせない", (self.lease_file().parent / "supervise.err").read_text(encoding="utf-8"))

    def test_command_runs_in_the_from_directory_without_a_shell(self):
        tok = self.claim()
        rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), "--", "echo", "a | false", "--", "b")
        self.assertEqual(rc, 0)
        self.assertIn("a | false -- b", r["op"]["result"])  # 最初の -- だけで切る
        rc, r = self.run_op(tok, "push:feat@2", "--reviewed", self.head(), *self.script("import os; print(os.getcwd())"))
        self.assertIn(str(self.repo.resolve()), r["op"]["result"])

    def test_missing_command_is_refused_before_begin(self):
        tok = self.claim()
        before = self.ops_file()
        for tail in ((), ("--",)):
            rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *tail)
            self.assertEqual(rc, 2, tail)
        self.assertEqual(self.ops_file(), before)

    def test_a_local_key_without_from_runs_in_the_workspace(self):
        tok = self.claim()
        rc, r = self.op(tok, "local:probe", *self.script("import os; print(os.getcwd())"))
        self.assertEqual(rc, 0)
        self.assertIn(str(self.wt.resolve()), r["op"]["result"])


if __name__ == "__main__":
    unittest.main()
