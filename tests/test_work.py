"""担当の貸し出しと外部操作の記録（scripts/work.py）: 二重書き込みの拒否、旧担当の停止、結果が分からない操作の照合。"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

WORK = Path(__file__).resolve().parents[1] / "scripts" / "work.py"



def decl(key):
    """push・pr に要る、届ける前の審査の宣言（この試験の外の約束）。"""
    return ("--reviewed", "none:試験の外") if key.split(":")[0] in ("push", "pr") else ()


class TestWork(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "wt"
        self.dir.mkdir()
        self.env = {**os.environ, "BB_WRIT_STATE_DIR": str(Path(self.tmp.name) / "state")}

    def tearDown(self):
        self.tmp.cleanup()

    def w(self, *args):
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

    def tearDown(self):
        self.tmp.cleanup()

    def w(self, *args):
        p = subprocess.run([sys.executable, str(WORK), *args], env=self.env, capture_output=True, text=True, errors="replace")
        return p.returncode, json.loads(p.stdout)

    def claim(self):
        return self.w("claim", "--dir", str(self.wt), "--holder", "A")[1]["token"]

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

    def test_merge_release_comment_and_issue_need_no_declaration(self):
        tok = self.claim()
        for key in ("merge:pr-1", "release:v1"):
            self.assertEqual(self.begin(tok, key, "--from", str(self.repo), "--verified", self.head())[0], 0, key)
        for key in ("comment:pr-1", "issue:new"):
            self.assertEqual(self.begin(tok, key, "--from", str(self.repo))[0], 0, key)

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
        return subprocess.run([sys.executable, str(WORK), *args], env=self.env, capture_output=True, text=True, input=stdin)

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
                                      "--what", "x", "--from", str(self.repo), "--verified", self.head(), "--reviewed", self.head(),
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
        return subprocess.Popen([sys.executable, str(WORK), "op", "run", "--dir", str(self.wt), "--token", tok, "--key", key,
                                 "--what", "x", "--from", str(self.repo), "--verified", self.head(), "--reviewed", self.head(),
                                 *self.script(body)], env={**self.env, **(env or {})}, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **kw)

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
                body = f"import json, os; f = {str(self.lease_file())!r}; s = {broken}; open(f + '.t', 'w').write(s); os.replace(f + '.t', f)"
                rc, r = self.run_op(tok, f"push:feat@{n}", "--reviewed", self.head(), *self.script(body))
                self.assertEqual(rc, 1 if n < 2 else 3, r)
                if n < 2:
                    self.assertIn("done を書けない", r["reason"])  # 監督役は黙って落ちず、理由を返す
                self.lease_file().write_text(good)
                self.assertIn(f"push:feat@{n}", self.pending())  # 照合で閉じる

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
                      and not (x.name.startswith("running") and x.name.endswith(".lock")))

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
        if at == "before-start":
            (gate / "before-check").write_text("")  # 手前の止まり所は通す
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

    def test_a_supervisor_that_cannot_start_closes_the_op(self):
        tok = self.claim()
        (self.lease_file().parent / "supervise.err").mkdir()  # 監督役の stderr を開けない
        marker = Path(self.tmp.name) / "ran"
        rc, r = self.run_op(tok, "push:feat@1", "--reviewed", self.head(), *self.script(f"open({str(marker)!r}, 'w')"))
        self.assertEqual(rc, 127)
        self.assertIn("監督役を起動できない", r["op"]["result"])
        self.assertFalse(marker.exists())
        self.assertEqual(self.pending(), [])  # 走っていないと記録して閉じる

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
        self.assertEqual(self.outputs(), [])  # 記録されないまま残った出力も消す
        self.assertEqual([(o["key"], o.get("running")) for o in self.w("op", "pending", "--dir", str(self.wt))[1]["pending_ops"]],
                         [("push:feat@1", None)])  # 終われば、後任が外部の状態と照合する
        self.assertEqual(self.w("claim", "--dir", str(self.wt), "--holder", "B")[0], 0)

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
