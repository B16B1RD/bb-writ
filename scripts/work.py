#!/usr/bin/env python3
"""同じ作業場所への二重書き込みを防ぐ担当の貸し出し（lease）と、結果が分からない外部操作の記録（plan-v3 4.2・6.3）。

  work.py claim   --dir <作業場所> [--holder <誰か>] [--lease <分>]   → token を返す。生きた担当がいれば拒否（終了コード 3）
  work.py renew   --dir <作業場所> --token <t> [--lease <分>]          → 引き継がれていれば拒否（3）。旧担当はここで止まる
  work.py release --dir <作業場所> --token <t>
  work.py status  --dir <作業場所>
  work.py op begin   --dir <作業場所> --token <t> --key <操作の鍵> --what <何をするか>  → 同じ鍵が既にあれば拒否（4）
  work.py op done    --dir <作業場所> --token <t> --key <操作の鍵> --result <結果>
  work.py op pending --dir <作業場所>                                 → 始めたが終わりの記録が無い操作
  work.py check   --dir <作業場所> --from <外部操作を実行する場所>      → そこで使われる GitHub のアカウントと、push 先の origin への書き込み権限（書けなければ 5）

公開する操作（鍵の種類が push・pr・merge・release）の op begin には --verified <検証した commit> が要る。作業場所の HEAD と違えば拒否する（6）。
検証の後に成果物が変わったら、証拠を取り直してから出す（plan-v3 6.2）。merge は、GitHub 側でも gh pr merge --match-head-commit <同じ commit> で止める。

外部に出る操作（鍵の種類が push・pr・comment・merge・release・issue）の op begin には --from が要り、check を通す。
外部操作は元の checkout（worktree なら `git rev-parse --git-common-dir` の親）から実行する。利用者のアカウントの切り替え
（gh のラッパー、git の includeIf、direnv）は、たいてい置き場所で決まるため（2026-10-17 の実測）。--from が元の checkout と違い、
そこで使われるアカウントが元の checkout のものと食い違えば拒否する。push・merge・release は書き込み権限が無いアカウントも拒否する。comment・issue・pr は、書き込み権限が無くても
GitHub が受け付ける（公開リポジトリへのコメントや fork からの PR）ので、対象が見えれば通す。権限を見る対象は gh の既定の
リポジトリ（gh repo fork の後は upstream）で、push だけは push 先の origin（自分の fork）。黙って別のアカウントへ切り替えない。

担当の生死はプロセスで判定しない（Codex の sandbox の中は別の pid 名前空間で、外の CLI が見えない。2026-10-17 の実測）。
claim は作業場所ごとの記録 note.md の雛形と証拠の置き場 evidence/ を作り、その場所と（書かれていれば）記録の中身を返す。後任は claim の出力だけで記録にたどり着く。
期限内に renew しない担当は止まったとみなし、別の担当が claim できる。claim のたびに token が変わるので、
旧担当の renew・op は拒否される。出力はどれも 1 行の JSON。
"""
import argparse
import fcntl
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path


def root(d: Path) -> Path:
    base = Path(os.environ.get("BB_WRIT_STATE_DIR", Path.home() / ".local" / "state" / "bb-writ")) / "work"
    real = str(d.resolve())
    r = base / (Path(real).name + "-" + hashlib.sha256(real.encode()).hexdigest()[:10])
    r.mkdir(parents=True, exist_ok=True)
    return r


@contextmanager
def locked(r: Path):
    with open(r / ".mutex", "w") as m:
        fcntl.flock(m, fcntl.LOCK_EX)
        yield


def read(p: Path):
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def write(p: Path, obj):
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def ops(r: Path) -> dict:
    out = {}
    f = r / "ops.jsonl"
    if f.is_file():
        for line in f.read_text(encoding="utf-8").splitlines():
            e = json.loads(line)
            out.setdefault(e["key"], {}).update(e)
    return out


def pending(r: Path) -> list:
    return [e for e in ops(r).values() if e.get("state") == "begun"]


NOTE = """# 作業の記録

- 目的:
- 制約:
- できたと分かる確かめ方（述語）:
- 決めたことと根拠:
- 済んだことと証拠:
- 今の状態（ブランチ・commit・PR）:
- 次の一手:
- ホストと session ID:
"""


def note(r: Path) -> dict:
    f = r / "note.md"
    if not f.is_file():
        f.write_text(NOTE, encoding="utf-8")
    body = f.read_text(encoding="utf-8")
    ev = r / "evidence"
    ev.mkdir(exist_ok=True)
    return {"path": str(f), "filled": body.strip() != NOTE.strip(), "body": body if body.strip() != NOTE.strip() else None,
            "evidence_dir": str(ev)}


EXTERNAL = {"push", "pr", "comment", "merge", "release", "issue"}  # GitHub に出る操作の種類（鍵の ":" の前）
PUBLISH = {"push", "pr", "merge", "release"}  # 成果物を外に出す操作。検証した commit と一致させる
NEEDS_WRITE = {"push", "merge", "release"}  # 書き込み権限が要る操作。comment・issue・pr（fork から）は読めれば出せる
WRITE_OK = {"ADMIN", "MAINTAIN", "WRITE"}


def sh(cmd, cwd: Path):
    try:
        p = subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def checkout_of(d: Path) -> Path:
    rc, common, err = sh(["git", "rev-parse", "--git-common-dir"], d)
    if rc:
        raise SystemExit(out({"ok": False, "reason": f"git のリポジトリでない: {d}: {err}"}, 2))
    c = Path(common)
    return (c if c.is_absolute() else d / c).resolve().parent


def account(at: Path, push: bool = False) -> dict:
    """at で外部操作をしたときに使われる GitHub のアカウントと、対象のリポジトリへの権限（gh を at で実行して観測する）。
    対象は gh の既定のリポジトリ（gh repo fork の後は upstream）。push だけは push 先の origin。"""
    rc, url, _ = sh(["git", "remote", "get-url", "--push", "origin"], at)
    if rc or "github.com" not in url:
        return {"at": str(at), "github": False, "remote": url or None}
    _, login, err = sh(["gh", "api", "user", "--jq", ".login"], at)
    rc, repo, err2 = sh(["gh", "repo", "view", *([url] if push else []), "--json", "nameWithOwner,viewerPermission",
                         "--jq", '.nameWithOwner + " " + .viewerPermission'], at)
    name, _, perm = repo.partition(" ") if rc == 0 else (None, None, None)
    return {"at": str(at), "github": True, "login": login or None, "repo": name, "permission": perm or None,
            "error": None if login and rc == 0 else (err or err2)[:300]}


def check(d: Path, frm: Path, kind: str = "push") -> tuple[dict, int]:
    c = checkout_of(d)
    write, push = kind in NEEDS_WRITE, kind == "push"
    here = account(frm, push)
    res = {"checkout": str(c), "from": here}
    if not here["github"]:
        return {**res, "ok": True, "skipped": "origin が GitHub でないので確かめを飛ばした"}, 0
    if not here["permission"] or (write and here["permission"] not in WRITE_OK):
        return {**res, "ok": False, "reason": f"{frm} で使われるアカウント {here['login']} は {here['repo'] or 'origin'} に{'書け' if write else 'アクセスでき'}ない（権限: {here['permission'] or '見えない'}）。"
                f"外部操作は元の checkout {c} から実行する。そこでも{'書け' if write else 'アクセスでき'}なければ、利用者のアカウントの設定を確かめる"}, 5
    if frm.resolve() != c:
        base = account(c, push)
        res["checkout_account"] = base
        if base.get("login") != here["login"]:
            return {**res, "ok": False, "reason": f"{frm} のアカウント {here['login']} と元の checkout {c} のアカウント {base.get('login')} が食い違う。"
                    f"外部操作は元の checkout から実行する"}, 5
    return {**res, "ok": True}, 0


def commit_of(d: Path, rev: str):
    rc, sha, _ = sh(["git", "rev-parse", "--verify", "--quiet", rev + "^{commit}"], d)
    return sha if rc == 0 else None


def out(obj, code=0):
    print(json.dumps(obj, ensure_ascii=False))
    return code


def check_token(lease, token):
    if not lease or lease.get("token") != token:
        return "この token の担当ではない（引き継がれたか、解放された）。作業を止め、status で今の担当を確かめる"
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("claim", "renew", "release", "status"):
        s = sub.add_parser(name)
        s.add_argument("--dir", type=Path, required=True)
        if name in ("renew", "release"):
            s.add_argument("--token", required=True)
        if name in ("claim", "renew"):
            s.add_argument("--lease", type=float, default=20, help="分")
        if name == "claim":
            s.add_argument("--holder", default=os.environ.get("USER", "?"))
    o = sub.add_parser("op")
    o.add_argument("action", choices=("begin", "done", "pending"))
    o.add_argument("--dir", type=Path, required=True)
    o.add_argument("--token")
    o.add_argument("--key")
    o.add_argument("--what", default="")
    o.add_argument("--result", default="")
    o.add_argument("--from", dest="frm", type=Path, help="外部操作を実行する場所（push・pr・comment・merge・release・issue で必須）")
    o.add_argument("--verified", help="検証した commit（push・pr・merge・release で必須。作業場所の HEAD と一致させる）")
    c = sub.add_parser("check")
    c.add_argument("--dir", type=Path, required=True)
    c.add_argument("--from", dest="frm", type=Path, required=True)
    a = ap.parse_args()
    if a.cmd == "check":
        res, code = check(a.dir, a.frm)
        return out(res, code)
    r = root(a.dir)
    now = time.time()
    with locked(r):
        lease = read(r / "lease.json")
        live = lease if lease and lease["expires_at"] > now else None
        if a.cmd == "status":
            return out({"dir": str(a.dir.resolve()), "holder": live and live["holder"], "expires_in_s": live and round(live["expires_at"] - now),
                        "stale": bool(lease and not live), "pending_ops": pending(r), "note": note(r)})
        if a.cmd == "claim":
            if live:
                return out({"ok": False, "reason": "生きた担当がいる。二重に書かない", "holder": live["holder"],
                            "expires_in_s": round(live["expires_at"] - now)}, 3)
            new = {"token": secrets.token_hex(8), "holder": a.holder, "dir": str(a.dir.resolve()), "claimed_at": now,
                   "expires_at": now + a.lease * 60, "previous": lease and {"holder": lease["holder"], "expired_at": lease["expires_at"]}}
            write(r / "lease.json", new)
            return out({"ok": True, "token": new["token"], "previous": new["previous"], "pending_ops": pending(r), "note": note(r)})
        if a.cmd in ("renew", "release"):
            why = check_token(live, a.token)
            if why:
                return out({"ok": False, "reason": why, "holder": live and live["holder"]}, 3)
            if a.cmd == "renew":
                live["expires_at"] = now + a.lease * 60
                write(r / "lease.json", live)
            else:
                (r / "lease.json").unlink()
            return out({"ok": True})
        if a.action == "pending":
            return out({"pending_ops": pending(r)})
        why = check_token(live, a.token)
        if why:
            return out({"ok": False, "reason": why}, 3)
        if not a.key:
            return out({"ok": False, "reason": "--key が要る"}, 2)
        known = ops(r).get(a.key)
        acct = None
        if a.action == "begin" and a.key.split(":")[0] in EXTERNAL:
            if not a.frm:
                return out({"ok": False, "reason": f"外部操作には --from <実行する場所> が要る。元の checkout は {checkout_of(a.dir)}"}, 2)
            res, code = check(a.dir, a.frm, a.key.split(":")[0])
            if code:
                return out(res, code)
            acct = res["from"]
        verified = None
        if a.action == "begin" and a.key.split(":")[0] in PUBLISH:
            head = commit_of(a.dir, "HEAD")
            if not a.verified:
                return out({"ok": False, "reason": f"公開する操作には --verified <検証した commit> が要る。作業場所の HEAD は {head}"}, 2)
            verified = commit_of(a.dir, a.verified)
            if verified != head:
                return out({"ok": False, "reason": f"検証した commit {verified or a.verified} と作業場所の HEAD {head} が違う。"
                            "検証の後に成果物が変わった。今の HEAD で証拠を取り直してから出す"}, 6)
        if a.action == "begin" and known:
            return out({"ok": False, "reason": "同じ鍵の操作が既にある。外部の状態を照合してから進める（やり直さない）", "op": known}, 4)
        if a.action == "done" and not known:
            return out({"ok": False, "reason": "begin の記録が無い"}, 2)
        e = {"key": a.key, "state": "begun" if a.action == "begin" else "done", "at": now, "holder": live["holder"]}
        e.update({"what": a.what} if a.action == "begin" else {"result": a.result})
        if acct:
            e.update({"from": acct["at"], "account": acct.get("login")})
        if verified:
            e["verified"] = verified
        with open(r / "ops.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        return out({"ok": True, "op": ops(r)[a.key]})


if __name__ == "__main__":
    sys.exit(main())
