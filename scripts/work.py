#!/usr/bin/env python3
"""同じ作業場所への二重書き込みを防ぐ担当の貸し出し（lease）と、結果が分からない外部操作の記録（plan-v3 4.2・6.3）。

  work.py claim   --dir <作業場所> [--holder <誰か>] [--lease <分>]   → token を返す。生きた担当がいるか、コマンドが終わっていない op run があれば拒否（終了コード 3）
  work.py renew   --dir <作業場所> --token <t> [--lease <分>]          → 引き継がれていれば拒否（3）。旧担当はここで止まる
  work.py release --dir <作業場所> --token <t>                         → コマンドが終わっていない op run があれば拒否（4）
  work.py status  --dir <作業場所>
  work.py op begin   --dir <作業場所> --token <t> --key <操作の鍵> --what <何をするか> [--from <場所>] [--verified <c>] [--reviewed <c>|none:<理由>]
                     → 同じ鍵が既にあれば拒否（4）
  work.py op run     （op begin と同じ引数） -- <コマンド>              → begin が通ったときだけ --from（無ければ作業場所）でコマンドを
                     シェルを通さず標準入力なしで実行し、終了コードと出力（stderr を含む）の末尾 20 行を done に記録する。
                     終了コードはコマンドのもの（シグナルは 128+N、起動できなければ 127、begin の拒否は begin の値、担当を失って
                     done を書けなければ 3、done の書き込みに失敗したら 1、監督役が結果を返さずに終わったら、記録に終了コードが
                     あればそれ（監督役が書いた記録だけ読む。255 を超えればプロセスの終了コードは 1）、無ければ 1 で exit は null。
                     記録（done でも begun でも）が読めれば op を添える）。出力は全体を表示する。
                     コマンドが終わるまでは、コマンドとそれが残したもの（シェルの & など）もロックを持つ。終わった後は（done を
                     書けたかに関わらず）、残したものは数えない（外部操作を & で背景に回すと、終わる前に明け渡すので、そうしない）
                     実行と記録は別のセッションの監督役が行い、コマンドが終わるまで担当を保つ。op run が止められても続く
                     （ただし、ツールの呼び出しが終わるとそこで起こしたプロセスを全部止める sandbox（Codex）では、一緒に止まる）。
                     コマンドが終わるまで（監督役が強制終了されたら、コマンドとそれが残したものが終わるまで）、その作業場所の op run と
                     外部操作の op begin、その操作の op done を拒否する（4）
  work.py op done    --dir <作業場所> --token <t> --key <操作の鍵> --result <結果>    → 照合で見つけた結果を閉じるとき
  work.py op pending --dir <作業場所>                                 → 始めたが終わりの記録が無い操作（コマンドが終わっていない op run の操作は running: true。残っている出力は output）
  work.py check   --dir <作業場所> --from <外部操作を実行する場所>      → そこで使われる GitHub のアカウントと、push 先の origin への書き込み権限（書けない・アカウントが分からなければ 5）
  work.py task start --dir <checkout> --holder <誰か> [--base <commit等>] [--target <ブランチ>] → 専用 worktree と担当を作る
  work.py task list --dir <checkout>                               → タスク、担当、未完了の統合
  work.py task resume --dir <checkout> --task <識別子> --holder <誰か> → 作業場所を保持して担当を取得
  work.py integrate --dir <worktree> --token <t> --expected <統合先commit> --verified <検証済みcommit> → 排他・照合して fast-forward
  work.py task cleanup --dir <worktree> --token <t> --saved <別のブランチかタグ> → 変更が保存先に入っていれば片付け（squash・rebase の統合も可）

task と integrate の照合拒否は終了コード 6、担当の拒否は 3。統合のロックは期限では奪わず、子の Git にも渡す。
統合の記録はリポジトリ共通。中断後は前回の候補を含む統合先から完了を復元するか、新しく検証した候補で再開する。

公開する操作（鍵の種類が push・pr・merge・release）の op begin には --verified <検証した commit> が要る。作業場所の HEAD と違えば拒否する（6）。
push・pr には --reviewed も要る（初回の push も）。届ける前の審査（入口の条件 (5)・(6)）を通した commit の ID（16 進）か、
要らない理由を none:<理由> で書く。無い・形が違う・commit が見つからない・ID でなく参照に解決されたら拒否し（2）、作業場所の HEAD と違えば拒否する（6）。
HEAD と違うときは審査の後に変えたファイルを、ほかの拒否と通ったときは、この作業場所の記録で最後に終えた push・pr（ブランチを問わない）から変えたファイルを示す（材料で、保証ではない）。
op run・op begin を通さない git push には効かない。
審査を通したかどうかは確かめない。短い往復で審査を忘れたまま出すのを、宣言を求めて止める。
検証の後に成果物が変わったら、証拠を取り直してから出す（plan-v3 6.2）。merge は、GitHub 側でも gh pr merge --match-head-commit <同じ commit> で止める。

外部に出る操作（鍵の種類が push・pr・comment・merge・release・issue）の op begin には --from が要り、check を通す。
鍵の種類はこの 6 つか、外部に出ない操作の記録なら local（または local- で始まる名前）。ほかの種類（Push・gh-pr・push/…・tag）は、確かめずに記録されるので拒否する（2）。
gh のアカウントが分からなければ拒否する（5）。
外部操作は元の checkout（worktree なら `git rev-parse --git-common-dir` の親）から実行する。利用者のアカウントの切り替え
（gh のラッパー、git の includeIf、direnv）は、たいてい置き場所で決まるため（2026-10-17 の実測）。--from が元の checkout と違い、
そこで使われるアカウントが元の checkout のものと食い違えば拒否する。--from が元の checkout と別のリポジトリなら、そこでは確かめずに拒否する（2）。
push・merge・release は書き込み権限が無いアカウントも拒否する。comment・issue・pr は、書き込み権限が無くても
GitHub が受け付ける（公開リポジトリへのコメントや fork からの PR）ので、対象が見えれば通す。権限を見る対象は gh の既定の
リポジトリ（gh repo fork の後は upstream、GH_REPO があればそれ）で、gh 自身に解決させる。push だけは push 先の origin の最初の pushurl
（自分の fork。pushDefault・複数の pushurl は見ない）。黙って別のアカウントへ切り替えない。
push の SSH の形の URL は、git が使う ssh（GIT_SSH_COMMAND > core.sshCommand > GIT_SSH > ssh）の -G で実際のホストに直して見る。解決できなければ止める。
SSH の push は、同じ ssh の -T -o BatchMode=yes で GitHub が返す鍵のアカウントも gh のアカウントと照合する（別名や core.sshCommand で鍵を切り替える構成）。
繋がらなければ止める（known_hosts に無い ssh.github.com:443、ssh-agent の無いパスフレーズ付きの鍵も止まる）。
https の push に使われる git の credential helper のアカウントは見ない（認証情報に触れない）。IP・角括弧のホスト・/etc/hosts の別名と、リモートヘルパーの形
（persistent-https::…）は github.com とみなさない。

担当の生死はプロセスで判定しない（Codex の sandbox の中は別の pid 名前空間で、外の CLI が見えない。2026-10-17 の実測）。
op run のコマンドが終わっていないかも pid では見ず、操作ごとのロック（running-<鍵のハッシュ>.lock）の flock で見る
（ロックは監督役・コマンド・それが残したものが持ち、プロセスが死ねば OS が外す）。
環境変数 BB_WRIT_SUPERVISE_GATE は試験だけが使う（op run と監督役の中の止まり所。設定されていても 60 秒で先へ進み、コマンドには渡さない）。
claim は作業場所ごとの記録 note.md の雛形と証拠の置き場 evidence/ を作り、その場所と（書かれていれば）記録の中身を返す。後任は claim の出力だけで記録にたどり着く。
期限内に renew しない担当は止まったとみなし、別の担当が claim できる。claim のたびに token が変わるので、
旧担当の renew・op は拒否される。出力はどれも 1 行の JSON。
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import signal
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
        yield m.fileno()


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


def changed_since_publish(r: Path, d: Path, head: str) -> str:
    """最後に終えた push・pr で出した commit から HEAD までに変えたファイル。審査が要るかを決める材料で、保証ではない。"""
    done = [e for e in ops(r).values() if e.get("verified") and e.get("state") == "done" and e["key"].split(":")[0] in REVIEWED]
    if not done:
        return " この作業場所に、終えた push・pr の記録が無い"
    prev = max(done, key=lambda e: e["at"])["verified"]  # 鍵の並びは begin の順なので、終えた時刻で選ぶ
    rc, files, err = sh(["git", "diff", "--name-only", f"{prev}..{head}"], d)
    if rc:
        return f" 前回の push・pr（{prev}）からの差分を出せない: {err}"
    rc, _, err = sh(["git", "merge-base", "--is-ancestor", prev, head], d)
    note = {0: "", 1: "（前回の commit は HEAD の祖先でない。rebase などの後なので、取り込んだ変更も並ぶ）"}.get(rc, f"（祖先かを確かめられない: {err}）")
    return f" 前回の push・pr（{prev[:12]}）から変えたファイル{note}: " + (", ".join(files.splitlines()) or "なし")


def pending(r: Path) -> list:
    """始めたが終わりの記録が無い操作。op run の監督役がまだ動かしている操作には running: true を付ける。"""
    held = run_holder(r)
    return [{**e, **({"running": True} if e["key"] == held else {}),
             **({"output": str(run_log(r, e["key"]))} if run_log(r, e["key"]).is_file() else {})}  # 照合の材料
            for e in ops(r).values() if e.get("state") == "begun"]


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
REVIEWED = {"push", "pr"}  # 届ける前の審査を通した commit（か、要らない理由）を宣言させる操作
NEEDS_WRITE = {"push", "merge", "release"}  # 書き込み権限が要る操作。comment・issue・pr（fork から）は読めれば出せる
WRITE_OK = {"ADMIN", "MAINTAIN", "WRITE"}
NO_GITHUB_REMOTE = r"none of the git remotes configured for this repository point to a known GitHub host|no git remotes found"  # gh 2.102.0


def sh(cmd, cwd: Path):
    try:
        p = subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def checkout_of(d: Path) -> Path:
    c = checkout_or_none(d)
    if not c:
        raise SystemExit(out({"ok": False, "reason": f"git のリポジトリでない: {d}"}, 2))
    return c


def checkout_or_none(d: Path):
    rc, common, _ = sh(["git", "rev-parse", "--git-common-dir"], d)
    if rc:
        return None
    c = Path(common)
    return (c if c.is_absolute() else d / c).resolve().parent


def ssh_command(at: Path) -> list:
    """git が push に使う ssh。GIT_SSH_COMMAND > core.sshCommand（どちらもシェルで解釈）> GIT_SSH（プログラム）> ssh
    （git 2.43 の実測。core.sshCommand で鍵を切り替える構成もある）。"""
    cmd = os.environ.get("GIT_SSH_COMMAND")
    if not cmd:
        rc, cmd, _ = sh(["git", "config", "--get", "core.sshCommand"], at)
        cmd = cmd if rc == 0 else None
    return ["sh", "-c", cmd + ' "$@"', "sh"] if cmd else [os.environ.get("GIT_SSH") or "ssh"]


def github_repo(url: str, at: Path):
    """push 先の URL を、gh が受け取る github.com/OWNER/REPO と、SSH なら git と同じ ssh の接続先の引数にする。
    github.com でなければ (None, None)。
    SSH の別名（~/.ssh/config の Host）は ssh -G で実際のホスト名に直す。gh は既定のリポジトリの解決では
    別名を直すが、引数に渡した URL は直さず、別名のホストへ接続しに行って失敗する（2026-10-20 の実測）。
    別名を直せなければ GitHub かどうか分からないので、ValueError で止める（飛ばすと確かめずに通る）。"""
    m = re.match(r"(?:([a-z+]+)://)?(?:([^@/]+)@)?([^/:]+)(?::(\d+))?[:/]/?([^/]+/[^/]+?)(?:\.git)?/?$", url, re.I)
    if not m:
        return None, None
    scheme, user, host, port, name = m.groups()
    dest = None
    if scheme is None or "ssh" in scheme.lower():  # git と同じく user@host で渡す（ssh の設定の Match user が効く）
        dest = [*(["-p", port] if port and scheme else []), "--", f"{user}@{host}" if user else host]
        rc, conf, err = sh([*ssh_command(at), "-G", *dest], at)
        opts = dict(l.split(" ", 1) for l in reversed(conf.splitlines()) if " " in l) if rc == 0 else {}
        host = opts.get("hostname")
        if not host:
            raise ValueError(f"ssh -G で {url} のホストを解決できない: {err[:200]}")
        if not is_github(host) and opts.get("proxycommand", "none") != "none":  # 接続先は ProxyCommand が決める（ProxyJump は踏み台だけ）
            raise ValueError(f"ssh の設定の ProxyCommand が接続先を決めるので、GitHub か分からない: {opts['proxycommand'][:200]}")
    return (f"github.com/{name}", dest) if is_github(host) else (None, None)


def is_github(host: str) -> bool:
    host = host.lower().rstrip(".")  # 末尾の点は同じホスト（github.com.）
    return host == "github.com" or host.endswith(".github.com")


def key_login(dest: list, at: Path):
    """SSH の push で使われる鍵の GitHub のアカウント。GitHub は ssh -T に「Hi <login>!」を返す（終了コードは 1。deploy key は owner/repo）。"""
    _, o, e = sh([*ssh_command(at), "-T", "-o", "BatchMode=yes", *dest], at)
    m = re.search(r"Hi ([^!\s]+)!", o + "\n" + e)
    return (m.group(1), None) if m else (None, (e or o)[:300])


def account(at: Path, push: bool = False, key: bool = True) -> dict:
    """at で外部操作をしたときに使われる GitHub のアカウントと、対象のリポジトリへの権限（gh を at で実行して観測する）。
    push は push 先の origin の pushurl を見て、SSH なら鍵のアカウント（key_login）も見る。
    ほかは gh が操作する既定のリポジトリ（gh repo fork の後は upstream、GH_REPO があればそれ）を見る。既定の解決は gh 自身に
    させる。gh は別名を PATH の ssh -G <host> で直し（user を付けず、git の ssh も見ない）、失敗すれば名前のまま判定するので、
    真似るとずれる（2026-10-20 の実測）。gh が GitHub の remote を見つけられなければ、gh の操作も失敗するので飛ばす。"""
    dest = None
    if push:
        rc, url, _ = sh(["git", "remote", "get-url", "--push", "origin"], at)
        try:
            target, dest = (None, None) if rc else github_repo(url, at)
        except ValueError as e:
            return {"at": str(at), "github": True, "login": None, "repo": None, "permission": None, "error": str(e)}
        if not target:
            return {"at": str(at), "github": False, "remote": url or None}
    else:
        target = os.environ.get("GH_REPO")  # gh repo view は引数が無いと GH_REPO を見ない（issue・pr は見る）
    rc, login, err = sh(["gh", "api", "user", "--jq", ".login"], at)
    login = login if rc == 0 else ""  # 4xx の本文（JSON）は --jq を通らず stdout に出る（gh 2.102.0）
    rc, repo, err2 = sh(["gh", "repo", "view", *([target] if target else []), "--json", "nameWithOwner,viewerPermission",
                         "--jq", '.nameWithOwner + " " + .viewerPermission'], at)
    if rc and not target and re.search(NO_GITHUB_REMOTE, err2):
        return {"at": str(at), "github": False, "remote": None, "gh": err2[:200]}
    name, _, perm = repo.partition(" ") if rc == 0 else (None, None, None)
    res = {"at": str(at), "github": True, "login": login or None, "repo": name, "permission": perm or None,
           "error": None if login and rc == 0 else (err or err2)[:300]}
    if push and dest and key:
        res["key_login"], res["key_error"] = key_login(dest, at)
    return res


def check(d: Path, frm: Path, kind: str = "push") -> tuple[dict, int]:
    c = checkout_of(d)
    if checkout_or_none(frm) != c:  # 別の場所のアカウントで確かめて通さない
        return {"checkout": str(c), "ok": False, "reason": f"--from {frm} は元の checkout {c} と同じリポジトリでない。外部操作は元の checkout から実行する"}, 2
    write, push = kind in NEEDS_WRITE, kind == "push"
    here = account(frm, push)
    res = {"checkout": str(c), "from": here}
    if not here["github"]:
        return {**res, "ok": True, "skipped": ("origin の pushurl が github.com でない" if push else "gh が GitHub の remote を見つけられない") + "ので確かめを飛ばした"}, 0
    if not here["login"]:  # 比べる相手が分からないまま通さない（gh api user だけが失敗する token）
        return {**res, "ok": False, "reason": f"{frm} で gh のアカウントが分からない。gh auth status を確かめる"
                + (f"（エラー: {here['error']}）" if here.get("error") else "")}, 5
    if not here["permission"] or (write and here["permission"] not in WRITE_OK):
        return {**res, "ok": False, "reason": f"{frm} で使われるアカウント {here['login']} は {here['repo'] or 'origin'} に{'書け' if write else 'アクセスでき'}ない（権限: {here['permission'] or '見えない'}）。"
                f"外部操作は元の checkout {c} から実行する。そこでも{'書け' if write else 'アクセスでき'}なければ、利用者のアカウントの設定を確かめる"
                + (f"（エラー: {here['error']}）" if here.get("error") else "")}, 5
    if "key_login" in here and not here["key_login"]:
        return {**res, "ok": False, "reason": f"SSH で GitHub に繋がらず、push に使われる鍵のアカウントが分からない（ssh: {here['key_error']}）。"
                "git の push も同じ ssh で繋ぐので、ssh の設定（鍵・known_hosts）を確かめる"}, 5
    if "key_login" in here and here["key_login"].lower() != (here["login"] or "").lower():
        return {**res, "ok": False, "reason": f"SSH の push に使われる鍵のアカウント {here['key_login']} が gh のアカウント {here['login']} と違う。"
                "ssh の設定（別名の IdentityFile・core.sshCommand）で、gh と同じアカウントの鍵を使う"}, 5
    if frm.resolve() != c:
        base = account(c, push, key=False)
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


MAX_LEASE_MIN = 24 * 60


def alive(lease, now):
    """期限内の担当か。書く子は作業場所の記録に書けるので、上限を超える期限や壊れた値の担当は切れたものとみなす
    （期限を遠い将来にされると、親が担当を取り直せなくなる）。"""
    try:
        return bool(lease) and now < float(lease['expires_at']) <= now + MAX_LEASE_MIN * 60
    except (KeyError, TypeError, ValueError):
        return False


def lease_minutes(v):
    m = float(v)
    if not 0 <= m <= MAX_LEASE_MIN:
        raise argparse.ArgumentTypeError(f'担当の期限は 0 以上 {MAX_LEASE_MIN} 分以下')
    return m


def check_token(lease, token):
    if not lease or lease.get("token") != token:
        return "この token の担当ではない（引き継がれたか、解放された）。作業を止め、status で今の担当を確かめる"
    return None


def pending_or_note(r: Path):
    """拒否の応答に添える pending_ops。記録を読めなくても拒否は返す（添える情報なので）。"""
    try:
        return pending(r)
    except Exception as e:
        return f"記録を読めない: {e!r}"


def claim_at(r, d, holder, minutes):
    now = time.time()
    old = read(r / 'lease.json')
    if alive(old, now):
        return {'ok': False, 'reason': '生きた担当がいる。二重に書かない',
                'holder': old['holder'], 'expires_in_s': round(old['expires_at'] - now)}, 3
    held = run_holder(r)
    if held:  # 担当の期限が切れても（手放されても）、op run のコマンドが動いている間は取らせない。取ると、終わっていない操作をやり直せる
        return {'ok': False, 'reason': f'op run の外部操作がまだ動いている: {held}。終わるのを待つ（終われば監督役が done を書く）',
                'holder': old and old.get('holder'), 'pending_ops': pending_or_note(r)}, 3
    new = {'token': secrets.token_hex(8), 'holder': holder, 'dir': str(d.resolve()),
           'claimed_at': now, 'expires_at': now + minutes * 60,
           'previous': old and {'holder': old.get('holder'), 'expired_at': old.get('expires_at')}}
    write(r / 'lease.json', new)
    return {'ok': True, 'token': new['token'], 'previous': new['previous'],
            'pending_ops': pending(r), 'note': note(r)}, 0


def git(d, *args, fds=()):
    p = subprocess.run(['git', '-C', str(d), *args], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, pass_fds=fds)
    if p.returncode:
        raise ValueError(p.stderr.strip() or p.stdout.strip() or 'git が失敗した')
    return p.stdout.strip()


def contained(repo, commit, ref):
    """commit の変更が ref に入っているか。祖先でなくても（squash・rebase で統合した後に統合先が進んでも）、
    commit を ref へマージした結果が ref のツリーのままなら入っている。衝突・取り消し・未統合の commit は入っていない。"""
    if sh(['git', 'merge-base', '--is-ancestor', commit, ref], repo)[0] == 0:
        return True
    p = subprocess.run(['git', '-C', str(repo), 'merge-tree', '--write-tree', ref, commit], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True)
    if p.returncode not in (0, 1) or not p.stdout:
        raise ValueError('変更が保存先に入っているかを確かめられない（git merge-tree --write-tree には Git 2.38 以上が要る）: '
                         + (p.stderr.strip() or 'git が失敗した'))
    return p.returncode == 0 and p.stdout.split()[0] == git(repo, 'rev-parse', ref + '^{tree}')


def clean(d, ignored=False):
    return not git(d, 'status', '--porcelain', '--untracked-files=all', *(['--ignored=matching'] if ignored else []))


def current_lease(r, token):
    lease = read(r / 'lease.json')
    if not alive(lease, time.time()):
        lease = None
    why = check_token(lease, token)
    if why:
        raise PermissionError(why)
    return lease


def tasks_at(repo):
    r = root(repo) / 'tasks'
    r.mkdir(exist_ok=True)
    return r


def task_at(d):
    # 作業場所の記録の task.json は書く子が書き換えうるので、識別子だけを使い、中身は親だけが書くタスクの一覧から読む。
    ident = str((read(root(d) / 'task.json') or {}).get('task'))
    task = read(tasks_at(checkout_of(d)) / (ident + '.json')) if re.fullmatch('[a-f0-9]{16}', ident) else None
    if not task or Path(task['dir']).resolve() != d.resolve():
        raise ValueError('この場所は task start で作った作業場所ではない')
    return task


def task_dir(rr, ident):
    """タスクの作業場所。状態ディレクトリの worktrees/ の下に、リポジトリと task 識別子から決まる。"""
    return rr.parent.parent / 'worktrees' / rr.name / ident


def worktrees(repo):
    # -z は空白・改行を含むパスも分割しない。
    result, entry = [], {}
    for field in git(repo, 'worktree', 'list', '--porcelain', '-z').split('\0'):
        if not field:
            if entry:
                result.append(entry)
                entry = {}
        else:
            k, _, v = field.partition(' ')
            entry[k] = v
    return result


def task_command(a):
    repo = checkout_of(a.dir)
    rr = root(repo)
    with locked(rr) as rf:
        registry = tasks_at(repo)
        if a.action == 'list':
            tasks = []
            for p in sorted(registry.glob('*.json')):
                task = read(p)
                lease = read(root(Path(task['dir'])) / 'lease.json')
                tasks.append({**task, 'lease': {k: v for k, v in lease.items() if k != 'token'} if lease else None})
            return out({'ok': True, 'tasks': tasks, 'integration': read(rr / 'integration.json')})
        if a.action == 'start':
            source_task = read(root(a.dir) / 'task.json')
            target = a.target or (source_task or {}).get('target') or git(a.dir, 'symbolic-ref', '--quiet', '--short', 'HEAD')
            git(repo, 'show-ref', '--verify', 'refs/heads/' + target)
            base = git(repo, 'rev-parse', '--verify', (a.base or 'refs/heads/' + target) + '^{commit}')
            ident = secrets.token_hex(8)
            dest = task_dir(rr, ident)
            task = {'task': ident, 'dir': str(dest), 'branch': 'bb-writ/' + ident,
                    'base': base, 'target': target, 'checkout': str(repo)}
            # 作成の途中で止まっても、一覧から作業場所とブランチを追える。
            write(registry / (ident + '.json'), task)
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                git(repo, 'worktree', 'add', '-b', task['branch'], '--', str(dest), base, fds=(rf,))
            except (ValueError, OSError):
                # 終了済みの作成が何も残さなかった場合だけ記録を取り消す。
                # プロセス中断や、ブランチ・ファイルの一部が残る失敗は記録を保持する。
                if not dest.exists() and not commit_of(repo, 'refs/heads/' + task['branch']) and not any(
                        Path(w['worktree']).resolve() == dest.resolve() for w in worktrees(repo)):
                    (registry / (ident + '.json')).unlink()
                raise
            wr = root(dest)
            with locked(wr):
                write(wr / 'task.json', task)
                res, code = claim_at(wr, dest, a.holder, a.lease)
            return out({**task, **res}, code)
        if a.action == 'resume':
            if not re.fullmatch('[a-f0-9]{16}', a.task):
                raise ValueError('task list の task 識別子を指定する')
            task = read(registry / (a.task + '.json'))
            if not task:
                raise ValueError('そのタスクの記録が無い')
            # 記録は書き換えられうるので、行き先・ブランチ・基点を task 識別子から決まる値と照合してから git に渡す。
            dest = task_dir(rr, a.task)
            if (task.get('task'), Path(str(task.get('dir'))).resolve(), task.get('branch')) != (a.task, dest.resolve(), 'bb-writ/' + a.task) or \
                    not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', str(task.get('base'))):
                raise ValueError('タスクの記録が task start の作った値と合わない。記録を照合する')
            cleanup = task.get('cleanup')
            if cleanup and not dest.exists() and not commit_of(repo, 'refs/heads/' + task['branch']):
                if not contained(repo, cleanup['commit'], cleanup['saved']):
                    raise ValueError(f"片付けの途中の保存先 {cleanup['saved']} に、このタスクの変更が入っていない。記録と差分を照合する")
                wr = root(dest)
                with locked(wr):
                    (wr / 'lease.json').unlink(missing_ok=True)
                    (registry / (task['task'] + '.json')).unlink()
                return out({'ok': True, 'completed': True, **cleanup, 'note': note(wr)})
            trees = worktrees(repo)
            registered = any(Path(w['worktree']).resolve() == dest.resolve() for w in trees)
            if not dest.exists() and not registered:
                if any(w.get('branch') == 'refs/heads/' + task['branch'] or
                       Path(w['worktree']).resolve() == dest.resolve() for w in trees):
                    raise ValueError('別の場所か不完全な worktree 登録が残る。先に実物を照合する')
                dest.parent.mkdir(parents=True, exist_ok=True)
                branch = commit_of(repo, 'refs/heads/' + task['branch'])
                git(repo, 'worktree', 'add', *([] if branch else ['-b', task['branch']]),
                    '--', str(dest), task['branch'] if branch else task['base'], fds=(rf,))
                registered = True
            if not dest.is_dir() or not registered:
                raise ValueError('作業場所の作成が未完了か、登録と実物が異なる。記録と git worktree list を照合する')
            actual_branch = next(w.get('branch') for w in worktrees(repo) if Path(w['worktree']).resolve() == dest.resolve())
            git(dest, 'rev-parse', '--verify', 'HEAD')
            wr = root(dest)
            with locked(wr):
                write(wr / 'task.json', task)
                res, code = claim_at(wr, dest, a.holder, a.lease)
            return out({**task, **res, 'worktree_branch': actual_branch}, code)
        task = task_at(a.dir)
        wr = root(a.dir)
        with locked(wr) as wf:
            current_lease(wr, a.token)
            if git(a.dir, 'symbolic-ref', '--quiet', 'HEAD') != 'refs/heads/' + task['branch']:
                raise ValueError('タスクのブランチが変更されている。元のブランチに戻して保存先を照合する')
            journal = read(rr / 'integration.json')
            if not clean(a.dir, ignored=True) or pending(wr) or (journal and journal['dir'] == str(a.dir.resolve())):
                raise ValueError('未保存の変更・未完了の操作がある。作業場所を保持する')
            # 同じブランチ自身の名前を保存先として使うと、削除後に変更を失う。
            ref = git(repo, 'rev-parse', '--symbolic-full-name', a.saved)
            if not ref.startswith(('refs/heads/', 'refs/tags/')):
                raise ValueError(f'保存先 {a.saved} の解決先 {ref or "（名前の付いた ref でない）"} は、ローカルのブランチかタグでない。'
                                 'ローカルのブランチかタグを指定する')
            if ref == 'refs/heads/' + task['branch']:
                raise ValueError('タスク自身のブランチは片付けで削除するので、保存先にならない。統合先のブランチかタグを指定する')
            head = git(a.dir, 'rev-parse', 'HEAD')
            if not contained(repo, head, ref):
                raise ValueError(f'保存先 {ref} に、このタスクの変更が入っていない。統合してから片付ける')
            write(registry / (task['task'] + '.json'), {**task, 'cleanup': {'saved': ref, 'commit': head}})
            git(repo, 'worktree', 'remove', str(a.dir.resolve()), fds=(rf, wf))
            git(repo, 'branch', '-D', task['branch'], fds=(rf, wf))
            (wr / 'lease.json').unlink()
            (registry / (task['task'] + '.json')).unlink()
            return out({'ok': True, 'saved': ref, 'commit': head, 'note': note(wr)})


def integrate(a):
    repo = checkout_of(a.dir)
    rr, wr = root(repo), root(a.dir)
    if rr == wr:
        raise ValueError('統合は task start で作った作業場所から実行する')
    # 全コマンドで repository → worktree の順に取り、検証後の反映を直列にする。
    # 子の git に fd を渡す。親だけが停止しても、git の終了まで排他を保持する。
    with locked(rr) as rf, locked(wr) as wf:
        current_lease(wr, a.token)
        task = task_at(a.dir)
        ref = 'refs/heads/' + task['target']
        candidates = [w for w in worktrees(repo) if w.get('branch') == ref]
        if len(candidates) != 1:
            raise ValueError('統合先ブランチを checkout した作業場所が 1 つ必要')
        dest = Path(candidates[0]['worktree'])
        if dest.resolve() != repo.resolve():
            raise ValueError('ローカル統合先は元の checkout に限る。対象側の PR 経路などで統合する')
        target_lease = read(rr / 'lease.json')
        if alive(target_lease, time.time()):
            raise ValueError('統合先の checkout に生きた担当がいる。書き換えず待つ')
        if git(a.dir, 'symbolic-ref', '--quiet', 'HEAD') != 'refs/heads/' + task['branch']:
            raise ValueError('タスクのブランチが変更されている')
        journal_file = rr / 'integration.json'
        journal = read(journal_file)
        head, target = git(a.dir, 'rev-parse', 'HEAD'), git(repo, 'rev-parse', ref)
        if journal:
            if journal['dir'] != str(a.dir.resolve()):
                raise ValueError('別タスクの統合が未完了。task list から担当と作業場所を調べ、先に照合する')
            # 反映直後の中断だけは Git の実物から完了を復元できる。
            if a.verified == journal['verified'] and a.expected == journal['expected'] and clean(dest) and subprocess.run(['git', '-C', str(repo), 'merge-base', '--is-ancestor', journal['verified'], target],
                                              capture_output=True).returncode == 0:
                journal_file.unlink()
                return out({'ok': True, 'recovered': True, 'commit': journal['verified'], 'target_commit': target})
            if not clean(dest):
                raise ValueError('前回の統合が未完了。統合先の HEAD・差分を照合し、変更を保存してから再開する')
            # 統合先が別の版に変わった場合、前回の候補も保存した新版の検証が必要。
            git(repo, 'merge-base', '--is-ancestor', journal['verified'], head)
        if head != a.verified or target != a.expected:
            raise ValueError('検証した commit または統合先が変わった。最新の統合先を取り込み、再検証する')
        if not clean(a.dir) or not clean(dest):
            raise ValueError('作業場所か統合先に未 commit の変更がある。変更は保持する')
        git(repo, 'merge-base', '--is-ancestor', target, head)
        write(journal_file, {'dir': str(a.dir.resolve()), 'expected': target, 'verified': head, 'target': ref})
        try:
            git(dest, 'merge', '--ff-only', '--no-overwrite-ignore', head, fds=(rf, wf))
        except (ValueError, OSError):
            if git(repo, 'rev-parse', ref) == target and clean(dest):
                journal_file.unlink()
            raise
        if git(repo, 'rev-parse', ref) != head or not clean(dest):
            raise ValueError('統合後の実物が検証した commit と一致しない。記録と差分を照合する')
        journal_file.unlink()
        return out({'ok': True, 'commit': head, 'target': ref})


def op_step(a, r: Path, live, now: float, action: str):
    """op の begin・done を 1 件記録する。(結果, 終了コード) を返す。呼ぶ側が r の排他を持つ。"""
    why = check_token(live, a.token)
    if why:
        return ({"ok": False, "reason": why}, 3)
    if not a.key:
        return ({"ok": False, "reason": "--key が要る"}, 2)
    known = ops(r).get(a.key)
    acct = None
    kind = a.key.split(":")[0] if ":" in a.key else None
    # 書き間違い・言い換えを列挙して止めると漏れる（push/…・pr#12・pull-request）ので、外部に出ない操作の記録を明示させる
    if action == "begin" and len(a.key.encode("utf-8")) > 1024:  # ロックの中身の鍵を読み切れるように
        return ({"ok": False, "reason": "鍵の長さは 1024 バイトまで"}, 2)
    if action == "begin" and kind not in EXTERNAL and kind != "local" and not (kind or "").startswith("local-"):
        return ({"ok": False, "reason": f"鍵 {a.key!r} の種類が分からない。鍵は <種類:対象> で、種類は外部操作なら "
                    "push・pr・comment・merge・release・issue のどれか（小文字）、外部に出ない操作の記録なら local（または local- で始まる名前）"}, 2)
    if action == "begin" and kind in EXTERNAL:
        if not a.frm:
            return ({"ok": False, "reason": f"外部操作には --from <実行する場所> が要る。元の checkout は {checkout_of(a.dir)}"}, 2)
        res, code = check(a.dir, a.frm, a.key.split(":")[0])
        if code:
            return (res, code)
        acct = res["from"]
    verified = None
    if action == "begin" and a.key.split(":")[0] in PUBLISH:
        head = commit_of(a.dir, "HEAD")
        if not head:
            return ({"ok": False, "reason": f"作業場所 {a.dir} の HEAD を解決できない"}, 2)
        if not a.verified:
            return ({"ok": False, "reason": f"公開する操作には --verified <検証した commit> が要る。作業場所の HEAD は {head}"}, 2)
        verified = commit_of(a.dir, a.verified)
        if verified != head:
            return ({"ok": False, "reason": f"検証した commit {verified or a.verified} と作業場所の HEAD {head} が違う。"
                        "検証の後に成果物が変わった。今の HEAD で証拠を取り直してから出す"}, 6)
    reviewed = None
    if action == "begin" and a.key.split(":")[0] in REVIEWED:
        if not a.reviewed:
            return ({"ok": False, "reason": "push・pr には --reviewed <届ける前の審査を通した commit> か --reviewed none:<要らない理由> が要る。"
                        "試験・検査を足した・変えたなら、入口の条件 (5)・(6) をこの commit で通す。" + changed_since_publish(r, a.dir, head)}, 2)
        if a.reviewed.startswith("none:"):
            if not a.reviewed[5:].strip():
                return ({"ok": False, "reason": "--reviewed none: の後に、審査が要らない理由を書く。" + changed_since_publish(r, a.dir, head)}, 2)
            reviewed = "none: " + a.reviewed[5:].strip()
        else:
            # HEAD・ブランチ名のような動く参照は、審査した commit を書いた宣言と見分けられないので受けない
            if not re.fullmatch(r"[0-9a-f]{7,64}", a.reviewed):
                return ({"ok": False, "reason": f"--reviewed {a.reviewed!r} は commit の ID（16 進）でも none:<理由> でもない。"
                            + changed_since_publish(r, a.dir, head)}, 2)
            reviewed = commit_of(a.dir, a.reviewed)
            if not reviewed:
                return ({"ok": False, "reason": f"--reviewed の commit {a.reviewed} が見つからない。" + changed_since_publish(r, a.dir, head)}, 2)
            if not reviewed.startswith(a.reviewed):  # 16 進だけの名前のブランチ・タグは、git が ID より先に解決する
                return ({"ok": False, "reason": f"--reviewed {a.reviewed} は ID でなく参照（{reviewed[:12]}）に解決された。完全な ID を書く。"
                            + changed_since_publish(r, a.dir, head)}, 2)
            if reviewed != head:
                rc, files, err = sh(["git", "diff", "--name-only", f"{reviewed}..{head}"], a.dir)
                after = (", ".join(files.splitlines()) or "なし") if rc == 0 else f"（差分を出せない: {err}）"
                return ({"ok": False, "reason": f"審査を通した commit {reviewed} と作業場所の HEAD {head} が違う。"
                            f"審査の後に変えた。直した commit で審査を通し直してから出す。審査の後に変えたファイル: {after}"}, 6)
    if action == "begin" and known:
        return ({"ok": False, "reason": "同じ鍵の操作が既にある。外部の状態を照合してから進める（やり直さない）", "op": known}, 4)
    if action == "done" and not known:
        return ({"ok": False, "reason": "begin の記録が無い"}, 2)
    if action == "done" and known.get("state") == "done":  # 記録した結果を、照合の文言で上書きしない
        return ({"ok": False, "reason": "この操作は既に終わりを記録している", "op": known}, 4)
    e = {"key": a.key, "state": "begun" if action == "begin" else "done", "at": now, "holder": live["holder"]}
    e.update({"what": a.what} if action == "begin" else {"result": a.result})
    if action == "done":  # 書いた者の印。手で閉じた記録は None（監督役の記録の印を上書きして、終了コードと読まれないように）
        e["by"] = getattr(a, "by", None)
    if acct:
        e.update({"from": acct["at"], "account": acct.get("login")})
    if verified:
        e["verified"] = verified
    if reviewed:
        e["reviewed"] = reviewed
    with open(r / "ops.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")
    res = {"ok": True, "op": ops(r)[a.key]}
    if reviewed:
        res["changed"] = changed_since_publish(r, a.dir, head)
    return res, 0


def run_lock(r: Path, key: str) -> Path:
    """op run のロック。操作ごとに分ける。コマンドとそれが残したもの（シェルの & など）もこのロックを持つ。"""
    return r / f"running-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}.lock"


def run_holder(r: Path):
    """コマンドがまだ終わっていない（監督役が見届けていない）op run があれば、その操作の鍵を返す（作業場所に 1 つ）。
    無ければ None。呼ぶ側が r の排他を持つ。
    ロックの flock で見る。プロセスが死ねば OS が外すので、pid やプロセスの開始時刻を照らさない。
    監督役はコマンドが終わったらロックのファイルを消すので、コマンドが残したもの（git の credential daemon など）が
    消えたファイルを持っていても数えない。監督役が強制終了されて消されなかったロックは、コマンドとそれが残したものが
    終わるまで数える（外部操作を続けているかもしれない）。ただし鍵が done のロック（done を書いた直後に監督役が殺された）は
    数えない。そのファイルは、持つ者が居なくなったときに消す（それまで、呼ぶたびに記録を読む）。
    開けない・読めないロックは消さない（鍵を始めた記録から引き、done なら数えず、引けなければファイル名で数える）。"""
    held = None
    done = None

    def records():  # 読めなければ空（鍵を引けず、done とも分からないので、動いているとみなす側に倒れる）
        try:
            return {k: e for k, e in ops(r).items() if isinstance(k, str) and isinstance(e, dict)}
        except Exception:  # 手で壊した記録（形の違い、深い入れ子の RecursionError など）でも落ちない
            return {}
    for f in sorted(r.glob("running*.lock")):  # running.lock は 0.11.12 の名前
        fd = None
        try:
            fd = os.open(f, os.O_RDONLY)
            text = os.read(fd, 4096).decode("utf-8", "replace")  # 鍵は 1024 バイトまで（op_step が拒否する）
        except FileNotFoundError:  # 監督役が消した（開く前。開いた後に消えても読める）
            continue
        except OSError:  # 開けない・読めない。動いているかが分からないので、動いているとみなす。鍵は始めた記録から引く
            if fd is not None:
                os.close(fd)
            done = records() if done is None else done
            if any(e.get("state") == "done" and run_lock(r, k).name == f.name for k, e in done.items()):
                continue  # 鍵がもう done（flock で見るときと同じく数えない）
            keys = [k for k, e in done.items() if e.get("state") == "begun" and run_lock(r, k).name == f.name]
            held = held or (keys[0] if keys else f.name)
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                done = records() if done is None else done
                if held is None and done.get(text, {}).get("state") != "done":  # done を書いた直後に監督役が殺されたロックは数えない
                    held = text or "?"  # 鍵はそのまま比べる（空白を落とすと、記録の鍵と食い違う）
                continue
            try:
                f.unlink(missing_ok=True)  # 外れたロック。記録されないままの出力は、照合に使えるよう残す（op done で閉じたら消す）
            except OSError:  # 片付けなので、消せなくても続ける（外れているので数えない）
                pass
        finally:
            os.close(fd)
    return held


STOP_SIGNALS = {signal.SIGTERM, signal.SIGHUP, signal.SIGINT}  # 監督役が受けてコマンドに伝えるシグナル


def run_log(r: Path, key: str) -> Path:
    """op run の出力の置き場。操作ごとに分ける（作業場所に 1 つだと、次の実行が前の出力を上書きする）。"""
    return r / f"run-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}.log"


MAX_TAIL = 8 * 1024  # 記録に残す出力の上限（バイト）。改行の無い大きな出力で ops.jsonl を膨らませない


def tail(f: Path, lines: int = 20, budget: int = MAX_TAIL) -> str:
    """出力の末尾の lines 行を、記録（ops.jsonl に書く JSON の値）にして budget バイトまで返す。
    出力の末尾の ASCII の空白（改行を含む。全角の空白などは残す）は記録しない。最後の意味のある文字の前の 64 KiB だけを読む
    （そこを探して後ろから 64 KiB ずつ遡る。16 MiB 遡っても無ければ空を返す）。行の中の空白は変えない。"""
    if not f.is_file():
        return ""
    with open(f, "rb") as fh:
        end = fh.seek(0, os.SEEK_END)
        for _ in range(256):
            start = max(0, end - 64 * 1024)
            fh.seek(start)
            block = fh.read(end - start)
            if block.strip():
                end = start + len(block.rstrip())  # 最後の意味のある文字の後ろ
                break
            if start == 0:
                return ""  # 空白だけの出力
            end = start
        else:
            return ""
        start = max(0, end - 64 * 1024)
        partial = False
        if start > 0:  # 窓の先頭が行の途中か（1 バイト前が改行でなければ途中）
            fh.seek(start - 1)
            partial = fh.read(1) != b"\n"
        fh.seek(start)
        data = fh.read(end - start)
        if partial:  # 文字の途中から読まない（UTF-8 の継続バイトを飛ばす）
            k = 0
            while k < min(3, len(data)) and 0x80 <= data[k] <= 0xBF:
                k += 1
            data = data[k:]
        rows = data.split(b"\n")  # 行は改行だけで数える（\r の進捗表示で行を割らない）
    text = b"\n".join(rows[-lines:]).decode("utf-8", "replace")
    cut = partial and len(rows) <= lines  # 残す行の先頭が、行の途中から始まる

    def size(s):  # ops.jsonl に書かれる形（ensure_ascii=False）での大きさ
        return len(json.dumps(("（先頭を省いた）" if cut else "") + s, ensure_ascii=False).encode()) - 2
    if size(text) > budget:  # 上限に収まる最も長い末尾を探す（先頭から削る）
        cut, lo, hi = True, 0, len(text)
        while lo < hi:
            mid = (lo + hi) // 2
            if size(text[mid:]) <= budget:
                hi = mid
            else:
                lo = mid + 1
        text = text[lo:]
    return ("（先頭を省いた）" if cut else "") + text


def show(fh):
    """開いた出力の全体を、少しずつ stderr に写す。"""
    for chunk in iter(lambda: fh.read(64 * 1024), b""):
        sys.stderr.buffer.write(chunk)
    sys.stderr.buffer.flush()


def keep_lease(r: Path, token: str):
    """コマンドが動いている間、担当の期限を 20 分先に保つ。引き継がれていれば触らない。
    期限が切れていても、token が同じ（誰も取っていない）なら延ばす。コマンドはまだ動いているので。"""
    with locked(r):
        lease = read(r / "lease.json")
        now = time.time()
        if lease and lease.get("token") == token and float(lease["expires_at"]) < now + 19 * 60:
            lease["expires_at"] = now + 20 * 60
            write(r / "lease.json", lease)


def op_run(a, r: Path, argv: list) -> int:
    """begin の照合を通ったときだけ、argv を --from（無ければ作業場所）で実行し、終了コードと出力の末尾を done に記録する。
    begin とコマンドを手で並べると、begin が拒否されてもコマンドが走り、done には打った文字が残ったため。
    実行と記録は、別のセッションに起こした監督役（supervise）が行う。この op run が止められても、監督役はコマンドの終わりまで
    担当を保ち、done を書く。20 分を超えるコマンドの間に担当が切れ、後任が終わっていない操作をやり直したため。"""
    if not argv:
        return out({"ok": False, "reason": "op run には、-- の後に実行するコマンドが要る（例: -- git push origin <ブランチ>）"}, 2)
    fd = None
    try:
        with locked(r):
            lease = read(r / "lease.json")
            now = time.time()
            held = run_holder(r)  # 照合と記録の前に見る。拒否した操作の begun を残さない
            if held:
                res, code = {"ok": False, "reason": f"この作業場所で、op run の外部操作がまだ動いている: {held}。"
                             "終わるのを待つ（終われば監督役が done を書く）"}, 4
            else:
                res, code = op_step(a, r, lease if alive(lease, now) else None, now, "begin")
            if not code:
                fd = os.open(run_lock(r, a.key), os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # 鍵ごとの新しいファイルなので取れる（同じ鍵は op_step が 4 で拒否する）
                os.write(fd, a.key.encode("utf-8"))
                if float(lease["expires_at"]) < now + 20 * 60:
                    lease["expires_at"] = now + 20 * 60
                    write(r / "lease.json", lease)
        if code:
            return out(res, code)
        gate("after-begin")
        spec = json.dumps({"dir": str(a.dir), "token": a.token, "key": a.key, "cwd": str(a.frm or a.dir), "lock_fd": fd})
        # 出力のファイルは先に作って開いておく。消すのは監督役だけ（done を書けたら消す）で、消されても開いたファイルから表示できる
        # 監督役がシグナルの扱いを登録するまでの間に止められないよう、塞いだまま起こす（監督役が登録してから外す）
        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
        errf = shown = None
        try:
            run_log(r, a.key).write_bytes(b"")
            shown = open(run_log(r, a.key), "rb")
            errf = open(r / "supervise.err", "ab")
            sup = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "_supervise", spec, "--", *argv],
                                   pass_fds=[fd], start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=errf)
        except OSError as e:  # 監督役を起こせなければ、コマンドも走っていない。そう記録して閉じる
            a.result = f"exit 127: 起動の準備ができない（出力のファイルか監督役。コマンドは走っていない）: {e}"
            with locked(r):
                res, _ = op_step(a, r, read(r / "lease.json"), time.time(), "done")
                run_lock(r, a.key).unlink(missing_ok=True)
                try:
                    run_log(r, a.key).unlink(missing_ok=True)
                except OSError:
                    pass
            if shown:
                shown.close()
            return out({"ok": False, "exit": 127, "op": res.get("op"), "reason": a.result}, 127)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
            if errf:
                errf.close()
    finally:
        if fd is not None:
            os.close(fd)  # ロックは監督役が持ち続ける
    # 監督役は 2 回目の fork の先で動く（この op run の子孫から外れる）。結果は、その標準出力の 1 行目に終了コード、2 行目に JSON
    lines = sup.communicate()[0].decode("utf-8", "replace").strip().splitlines()
    with shown:  # 監督役が消していても、開いたファイルから読める
        try:
            show(shown)
        except OSError as e:
            sys.stderr.write(f"（出力を読めない: {e!r}）\n")
    rec = {}
    if len(lines) < 2:  # 監督役が結果を返さなかった。記録を読む（読めなければ、書けていないとみなす）
        try:
            rec = ops(r).get(a.key, {})
            rec = rec if isinstance(rec, dict) else {}
        except Exception:  # 手で壊した記録（形の違い、深い入れ子の RecursionError など）でも落ちない
            rec = {}
    if len(lines) < 2 and rec.get("state") == "done":  # done を書いた後に監督役が居なくなった。結果は記録にある
        result = rec.get("result")
        m = re.match(r"exit ([0-9]{1,10}):", result) if isinstance(result, str) and rec.get("by") == "supervise" else None  # 監督役が書いた記録だけ
        if not m:  # 手で閉じた記録など。走ったかは記録の result で判断する
            return out({"ok": False, "exit": None, "op": rec, "reason": "監督役が結果を返さずに終わった。記録の result で判断する"}, 1)
        rc = int(m.group(1))
        return out({"ok": rc == 0, "exit": rc, "op": rec, "reason": "監督役が結果を返さずに終わったが、結果は記録済み"}, rc if rc < 256 else 1)
    if len(lines) < 2:  # コマンドは走ったかもしれない（exit は分からない）。始めた記録があれば添える
        return out({"ok": False, "exit": None, **({"op": rec} if rec else {}),
                    "reason": f"監督役が結果を返さずに終わった。{r / 'supervise.err'} を読み、"
                    "op pending の running が消えるのを待ってから、外部の状態で照合する"}, 1)
    print(lines[1])
    return int(lines[0])


def finish(r: Path, spec: dict):
    """コマンドが終わったので、ロックのファイルを消す。done を書けたかに関わらず消す
    （コマンドが終わった後に二重に走るおそれは無い。done が無ければ begun が残り、照合で閉じる）。
    コマンドが残したものは、消えたファイルのロックを持つだけになり、次の操作を止めない。"""
    run_lock(r, spec["key"]).unlink(missing_ok=True)


def gate(name: str):
    """試験だけが使う止まり所。BB_WRIT_SUPERVISE_GATE のディレクトリに at-<name>（中身は pid）を書き、<name> ができるまで待つ。
    監督役の中の数 ms の窓（fork の後・コマンドを起こす前）に、試験がシグナルを送れるようにする。"""
    d = os.environ.get("BB_WRIT_SUPERVISE_GATE")
    if d and Path(d).is_dir():
        try:
            (Path(d) / f"at-{name}").write_text(str(os.getpid()))
        except OSError:  # 置き場が消えた。止まらずに進む
            return
        deadline = time.time() + 60  # 環境に残っていても、永久には待たない
        while not (Path(d) / name).exists() and time.time() < deadline:
            time.sleep(0.02)


def supervise(spec: dict, argv: list) -> int:
    """op run の監督役。コマンドを実行し、動いている間は担当を保ち、終わったら done を書いて結果を出す。
    操作のロックは op run から受け継いだ fd で持ち、コマンドにも渡す。コマンドが終わったらロックのファイルを消す。"""
    # 2 回目の fork。op run の子孫の木から外れ、木ごと止めるハーネスに巻き込まれない。保留中のシグナルは fork の子に
    # 受け継がれないので、中継（親）が fork の後に見て、パイプで子に渡す。子はそれを受け取るまで起こさない。
    # ここが「起こす前」の決め所で、これより後に中継へ届いたものは、起こした後に op run を止めたのと同じ扱い（コマンドは続く）
    rfd, wfd = os.pipe()
    if os.fork():
        os.close(rfd)
        gate("relay")
        os.write(wfd, (",".join(str(int(s)) for s in signal.sigpending() & STOP_SIGNALS) or "-").encode())
        os._exit(0)
    os.close(wfd)
    gate("relay-told")
    told = b""
    while chunk := os.read(rfd, 64):
        told += chunk
    os.close(rfd)
    told = told.decode()
    if told != "-" and not all(x.isdigit() for x in told.split(",")):  # 中継が渡す前に殺された。止めとみなし、起こさない
        early = {signal.SIGKILL}
    else:
        early = {signal.Signals(int(x)) for x in told.split(",") if x.isdigit()}
    d = Path(spec["dir"])
    r = root(d)
    p = None
    caught = []

    def forward(sig, _frame):  # 監督役は止まらず、コマンドに伝えて終わりを待つ（止まるとロックが外れ、やり直しが通る）
        caught.append(sig)
        if p is not None and p.poll() is None:
            p.send_signal(sig)  # 起こす前のシグナルは、起こさないことで受ける
    for sig in STOP_SIGNALS:
        signal.signal(sig, forward)
    # op run が塞いで起こした。塞いだまま、起こす前に届いたかを見る。起こした後で外すので、その間に届いたものもコマンドに伝わる
    gate("before-check")
    early |= signal.sigpending() & STOP_SIGNALS
    if early:
        caught.extend(sorted(early))
    warned = set()

    def warn(msg):  # 同じ文言は 1 回だけ（毎秒の延長の失敗で育たない）
        if msg not in warned:
            warned.add(msg)
            sys.stderr.write(f"{spec['key']}: {msg}\n")
            sys.stderr.flush()
    if caught:  # 起こす前に止めろと言われた。外部操作を始めない
        signal.pthread_sigmask(signal.SIG_UNBLOCK, STOP_SIGNALS)
        rc, output = 128 + caught[0], "起動の前に止められた（コマンドは走っていない）"
    else:
        try:
            gate("before-start")
            with open(run_log(r, spec["key"]), "ab") as logf:  # op run が作って開いたファイルに書く
                # 出力はファイルへ。op run が止められても、パイプの相手を失って書けなくならない
                p = subprocess.Popen(argv, cwd=spec["cwd"], stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                                     env={**{k: v for k, v in os.environ.items() if k != "BB_WRIT_SUPERVISE_GATE"}, "GIT_TERMINAL_PROMPT": "0"},
                                     # ロックをコマンドにも持たせる。監督役が kill -9 されても、コマンド（とそれが残したもの）が
                                     # 生きている間はロックが外れず、後任がやり直さない
                                     pass_fds=[spec["lock_fd"]],
                                     preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_UNBLOCK, STOP_SIGNALS))  # コマンドには塞がずに渡す
        except OSError as e:  # 起動できない（走っていない）のはここだけ
            rc, output = 127, f"起動できない（コマンドは走っていない）: {e}"
        else:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, STOP_SIGNALS)  # 起こしてから外す。待っていたシグナルはここで伝わる
            while True:
                try:
                    p.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    try:
                        keep_lease(r, spec["token"])
                    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:  # 延ばせなくても止まらない
                        warn(f"担当の期限を延ばせない: {e!r}")
            rc = p.returncode if p.returncode >= 0 else 128 - p.returncode  # シグナルで止まった。シェルと同じ 128+N にする
            try:
                output = tail(run_log(r, spec["key"]), budget=MAX_TAIL - len(f"exit {rc}: ".encode()))
            except OSError as e:
                output = f"（出力を読めない: {e!r}）"
    a = argparse.Namespace(key=spec["key"], token=spec["token"], dir=d, frm=None, verified=None, reviewed=None, what="",
                           result=f"exit {rc}: " + output, by="supervise")  # output は tail で末尾 20 行・8 KiB にしてある
    gate("before-done")
    code = None
    try:
        with locked(r):
            res, code = op_step(a, r, read(r / "lease.json"), time.time(), "done")  # 期限が切れても、token が同じなら書く
            if not code:  # 記録できた。全体の出力は残さない（秘密が出ていても、記録の末尾 20 行より長く残さない）
                try:
                    run_log(r, spec["key"]).unlink(missing_ok=True)
                except OSError as e:  # 消せなくても、ロックは明け渡す
                    warn(f"出力のファイルを消せない: {e!r}")
            finish(r, spec)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:  # 書けなくても結果は出す。begun は残るので照合で閉じる
        if code is not None:  # op_step は終えた（done を書けた、または担当を失った）。ロックのファイルを消せなかっただけ
            warn(f"ロックのファイルを消せない: {e!r}")
        else:
            warn(f"done を書けない: {e!r}")
            res, code = {"ok": False, "reason": f"done を書けない: {e!r}"}, 1
            try:
                finish(r, spec)  # 排他は取らない（取れないこともある）。コマンドは終わっていて、run_holder は消えたファイルを飛ばす
            except OSError:
                pass
    gate("before-print")
    os.close(spec["lock_fd"])  # 記録を書いたら、すぐ次の担当・操作に明け渡す
    if code:  # 実行の間に引き継がれた・解放された。begun のまま残るので、次の担当が照合する
        res, rc = {**res, "exit": rc, "output": output[-2000:]}, code
    else:
        res = {"ok": rc == 0, "exit": rc, "op": res["op"]}
    try:
        print(rc if rc < 256 else 1)
        print(json.dumps(res, ensure_ascii=False), flush=True)
    except BrokenPipeError:  # op run が止められた。結果は記録にある。終わり際の flush でも出さない
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser('task')
    ts = t.add_subparsers(dest='action', required=True)
    for name in ('start', 'resume', 'list', 'cleanup'):
        s = ts.add_parser(name)
        s.add_argument('--dir', type=Path, required=True)
        if name in ('start', 'resume'):
            s.add_argument('--holder', required=True)
            s.add_argument('--lease', type=lease_minutes, default=20)
        if name == 'start':
            s.add_argument('--base')
            s.add_argument('--target')
        if name == 'resume':
            s.add_argument('--task', required=True)
        if name == 'cleanup':
            s.add_argument('--token', required=True)
            s.add_argument('--saved', required=True)
    i = sub.add_parser('integrate')
    i.add_argument('--dir', type=Path, required=True)
    i.add_argument('--token', required=True)
    i.add_argument('--expected', required=True, help='検証時の統合先の完全な commit ID')
    i.add_argument('--verified', required=True, help='検証した作業場所の完全な commit ID')
    for name in ("claim", "renew", "release", "status"):
        s = sub.add_parser(name)
        s.add_argument("--dir", type=Path, required=True)
        if name in ("renew", "release"):
            s.add_argument("--token", required=True)
        if name in ("claim", "renew"):
            s.add_argument("--lease", type=lease_minutes, default=20, help="分")
        if name == "claim":
            s.add_argument("--holder", default=os.environ.get("USER", "?"))
    o = sub.add_parser("op")
    o.add_argument("action", choices=("begin", "done", "pending", "run"))
    o.add_argument("--dir", type=Path, required=True)
    o.add_argument("--token")
    o.add_argument("--key")
    o.add_argument("--what", default="")
    o.add_argument("--result", default="")
    o.add_argument("--from", dest="frm", type=Path, help="外部操作を実行する場所（push・pr・comment・merge・release・issue で必須）")
    o.add_argument("--verified", help="検証した commit（push・pr・merge・release で必須。作業場所の HEAD と一致させる）")
    o.add_argument("--reviewed", help="届ける前の審査を通した commit か none:<理由>（push・pr で必須。commit は作業場所の HEAD と一致させる）")
    c = sub.add_parser("check")
    c.add_argument("--dir", type=Path, required=True)
    c.add_argument("--from", dest="frm", type=Path, required=True)
    argv, run_argv = sys.argv[1:], []
    if argv[:1] == ["_supervise"] and "--" in argv:  # op run が起こす監督役（直接は呼ばない）
        return supervise(json.loads(argv[1]), argv[argv.index("--") + 1:])
    if argv[:2] == ["op", "run"] and "--" in argv:  # op run は -- の後ろを、シェルを通さずに実行するコマンドとして受け取る
        i = argv.index("--")
        argv, run_argv = argv[:i], argv[i + 1:]
    a = ap.parse_args(argv)
    if a.cmd in ('task', 'integrate'):
        try:
            return task_command(a) if a.cmd == 'task' else integrate(a)
        except PermissionError as e:
            return out({'ok': False, 'reason': str(e)}, 3)
        except (ValueError, OSError) as e:
            return out({'ok': False, 'reason': str(e)}, 6)
    if a.cmd == "check":
        res, code = check(a.dir, a.frm)
        return out(res, code)
    r = root(a.dir)
    if a.cmd == "op" and a.action == "run":
        return op_run(a, r, run_argv)
    with locked(r):
        now = time.time()
        lease = read(r / "lease.json")
        live = lease if alive(lease, now) else None
        if a.cmd == "status":
            return out({"dir": str(a.dir.resolve()), "holder": live and live["holder"], "expires_in_s": live and round(live["expires_at"] - now),
                        "stale": bool(lease and not live), "pending_ops": pending(r), "note": note(r)})
        if a.cmd == "claim":
            res, code = claim_at(r, a.dir, a.holder, a.lease)
            return out(res, code)
        if a.cmd in ("renew", "release"):
            why = check_token(live, a.token)
            if why:
                return out({"ok": False, "reason": why, "holder": live and live["holder"]}, 3)
            held = run_holder(r) if a.cmd == "release" else None
            if held:  # 手放すと、監督役が結果を記録できない
                return out({"ok": False, "reason": f"op run の外部操作がまだ動いている: {held}。終わるのを待ってから手放す"}, 4)
            if a.cmd == "renew":
                live["expires_at"] = now + a.lease * 60
                write(r / "lease.json", live)
            else:
                (r / "lease.json").unlink()
            return out({"ok": True})
        if a.action == "pending":
            return out({"pending_ops": pending(r)})
        held = run_holder(r)
        if held and ((a.action == "done" and a.key == held) or (a.action == "begin" and (a.key or "").split(":")[0] in EXTERNAL)):
            return out({"ok": False, "reason": f"この作業場所で、op run の外部操作がまだ動いている: {held}。終わるのを待つ（終われば監督役が done を書く）"}, 4)
        res, code = op_step(a, r, live, now, a.action)
        if a.action == "done" and (not code or (code == 4 and res.get("op", {}).get("state") == "done")):
            try:
                run_log(r, a.key).unlink(missing_ok=True)  # 閉じた（または既に終わっていた）。残しておいた出力はもう要らない
            except OSError as e:
                res = {**res, "warning": f"出力のファイルを消せない: {e!r}"}
        return out(res, code)


if __name__ == "__main__":
    sys.exit(main())
