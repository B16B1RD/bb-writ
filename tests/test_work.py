"""担当の貸し出しと外部操作の記録（scripts/work.py）: 二重書き込みの拒否、旧担当の停止、結果が分からない操作の照合。"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

WORK = Path(__file__).resolve().parents[1] / "scripts" / "work.py"


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
        self.assertEqual(self.w("op", "begin", "--token", a["token"], "--key", "pr-create:feat/x", "--what", "gh pr create")[0], 0)
        self.expire()  # A は結果を記録せずに止まり、期限が切れた（sleep で待つと、遅い機械で揺れた）
        rc, b = self.w("claim", "--holder", "B")
        self.assertEqual([o["key"] for o in b["pending_ops"]], ["pr-create:feat/x"])
        rc, again = self.w("op", "begin", "--token", b["token"], "--key", "pr-create:feat/x", "--what", "gh pr create")
        self.assertEqual(rc, 4)  # やり直さず、照合を求める
        rc, done = self.w("op", "done", "--token", b["token"], "--key", "pr-create:feat/x", "--result", "既存の PR #3 を確認")
        self.assertEqual((rc, done["op"]["state"]), (0, "done"))
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
import os, re, sys
cwd = os.getcwd()
login, perm = os.environ["FAKE_GH_DEFAULT"].split(":")
for item in os.environ.get("FAKE_GH_MAP", "").split(";"):
    if item and cwd.startswith(item.split("=")[0]):
        login, perm = item.split("=")[1].split(":")
if sys.argv[1:3] == ["api", "user"]:
    print(login)
elif sys.argv[1:3] == ["repo", "view"]:
    # 引数のリポジトリは、実物と同じく SSH の別名を解決せず、そのホストへ接続しに行く。名前は .git の有無まで照合する
    rest = sys.argv[3:]
    arg = next((a for i, a in enumerate(rest) if not a.startswith("-") and not (i and rest[i - 1] in ("--json", "--jq", "-q", "--template", "-t"))), None)
    m = arg and re.match(r"(?:[a-z]+://)?(?:[^@/]+@)?([^/:]+)[:/][^/]+/[^/]+$|[^/]+/[^/]+$", arg)
    host = m and m.group(1)
    if host and host != "github.com":
        print("error connecting to " + host, file=sys.stderr); sys.exit(1)
    if arg and arg != "github.com/B16B1RD/demo":  # work.py が渡すのはこの形だけ（OWNER/REPO だけでは GH_HOST で別のホストになる）
        print("GraphQL: Could not resolve to a Repository", file=sys.stderr); sys.exit(1)
    # 引数なしは gh の既定のリポジトリ。gh repo fork の後は upstream（FAKE_GH_UPSTREAM はそこでの権限）
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
        p = subprocess.run([sys.executable, str(WORK), *args], env=self.env, capture_output=True, text=True)
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
        rc, r = self.begin(tok, "push:feat", "--from", str(self.repo), "--verified", head)
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
            rc, r = self.begin(tok, key, "--from", str(self.repo), "--verified", head)
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
        rc, r = self.begin(tok, "push:feat", "--from", str(self.repo), "--verified", head)
        self.assertEqual((rc, r["op"]["account"]), (0, "B16B1RD"))
        self.assertEqual(self.begin(tok, "comment:issue-1", "--from", str(self.repo))[0], 0)
        for key in ("merge:pr-1", "release:v1"):
            rc, r = self.begin(tok, key, "--from", str(self.repo), "--verified", head)
            self.assertEqual(rc, 5, key)  # マージ・リリースは upstream に書けない
            self.assertIn("parent/demo", json.dumps(r, ensure_ascii=False), key)

    def test_pr_checks_the_upstream_not_the_fork(self):
        """PR は gh の既定のリポジトリ（upstream）に出る。見えなければ、自分の fork に書けても止める。"""
        self.env["FAKE_GH_UPSTREAM"] = "NONE"
        tok = self.claim()
        head = subprocess.run(["git", "rev-parse", "HEAD"], env=self.env, cwd=self.wt, check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(self.begin(tok, "pr:feat", "--from", str(self.repo), "--verified", head)[0], 5)

    def test_push_checks_the_push_url_not_the_fetch_url(self):
        """fetch は upstream、push は自分の fork（pushurl）という構成。push の権限は pushurl で見る。"""
        subprocess.run(["git", "remote", "set-url", "origin", "https://github.com/parent/demo.git"], env=self.env, cwd=self.repo, check=True)
        subprocess.run(["git", "remote", "set-url", "--push", "origin", "https://github.com/B16B1RD/demo.git"], env=self.env, cwd=self.repo, check=True)
        tok = self.claim()
        head = subprocess.run(["git", "rev-parse", "HEAD"], env=self.env, cwd=self.wt, check=True, capture_output=True, text=True).stdout.strip()
        rc, r = self.begin(tok, "push:feat", "--from", str(self.repo), "--verified", head)
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
            rc, r = self.begin(tok, f"push:feat@{i}", "--from", str(self.repo), "--verified", head)
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

    def test_unresolvable_ssh_host_is_refused_not_skipped(self):
        """ssh -G が失敗すると GitHub かどうか分からない。確かめを飛ばさずに止める。"""
        (self.bin / "ssh").write_text("#!/bin/sh\necho 'ssh: bad configuration' >&2\nexit 255\n")
        subprocess.run(["git", "remote", "set-url", "origin", "git@work:B16B1RD/demo.git"], env=self.env, cwd=self.repo, check=True)
        for url in ("git@work:B16B1RD/demo.git", "git@github.com:B16B1RD/demo.git", "ssh://git@work/B16B1RD/demo.git"):  # 名前が github.com でも、ssh の設定で別のホストになりうる
            subprocess.run(["git", "remote", "set-url", "origin", url], env=self.env, cwd=self.repo, check=True)
            rc, r = self.w("check", "--dir", str(self.wt), "--from", str(self.repo))
            self.assertEqual(rc, 5, url)
            self.assertIn("bad configuration", r["reason"], url)
        tok = self.claim()
        for key in ("comment:issue-1", "pr:feat", "merge:pr-1"):  # push 以外も、origin が分からなければ飛ばさない
            rc, r = self.begin(tok, key, "--from", str(self.repo))
            self.assertEqual(rc, 5, key)
            self.assertIn("bad configuration", r["reason"], key)

    def test_alias_origin_is_checked_for_other_operations_too(self):
        """push 以外も、確かめるかどうかは origin で決める。別名の origin で飛ばすと、見えないリポジトリへのコメントも通る。"""
        self.env["FAKE_SSH_ALIASES"] = "work=github.com"
        self.env["FAKE_GH_MAP"] = f"{Path(self.tmp.name).resolve() / 'personal'}=work-account:NONE"
        subprocess.run(["git", "remote", "set-url", "origin", "git@work:B16B1RD/demo.git"], env=self.env, cwd=self.repo, check=True)
        tok = self.claim()
        self.assertEqual(self.begin(tok, "comment:issue-1", "--from", str(self.repo))[0], 5)

    def test_internal_ops_need_no_account(self):
        tok = self.claim()
        self.assertEqual(self.begin(tok, "note:x")[0], 0)


class TestVerifiedCommit(unittest.TestCase):
    """公開する操作は、検証した commit が作業場所の HEAD と同じときだけ通す（plan-v3 6.2。検証の後に変わった成果物を出さない）。"""

    setUp, tearDown, w, claim, begin = (TestAccountCheck.setUp, TestAccountCheck.tearDown, TestAccountCheck.w,
                                        TestAccountCheck.claim, TestAccountCheck.begin)

    def head(self):
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.wt, check=True, capture_output=True, text=True).stdout.strip()

    def test_publish_needs_the_verified_commit(self):
        tok = self.claim()
        rc, r = self.begin(tok, "push:feat", "--from", str(self.repo))
        self.assertEqual(rc, 2)
        self.assertIn(self.head(), r["reason"])  # 今の HEAD を示す

    def test_publish_after_the_tree_changed_is_refused(self):
        verified = self.head()
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "after"], cwd=self.wt, check=True)
        tok = self.claim()
        rc, r = self.begin(tok, "pr:feat", "--from", str(self.repo), "--verified", verified)
        self.assertEqual(rc, 6)
        self.assertIn(self.head(), r["reason"])
        self.assertEqual(self.w("op", "pending", "--dir", str(self.wt))[1]["pending_ops"], [])  # 記録されない

    def test_publish_of_the_verified_commit_is_allowed_and_recorded(self):
        tok = self.claim()
        rc, r = self.begin(tok, "push:feat", "--from", str(self.repo), "--verified", self.head()[:10])  # 短い SHA も受ける
        self.assertEqual((rc, r["op"]["verified"]), (0, self.head()))

    def test_comment_needs_no_verified_commit(self):
        tok = self.claim()
        self.assertEqual(self.begin(tok, "comment:pr-1", "--from", str(self.repo))[0], 0)


if __name__ == "__main__":
    unittest.main()
