#!/usr/bin/env python3
"""同じ作業場所への二重書き込みを防ぐ担当の貸し出し（lease）と、結果が分からない外部操作の記録（plan-v3 4.2・6.3）。

  work.py claim   --dir <作業場所> [--holder <誰か>] [--lease <分>]   → token を返す。生きた担当がいれば拒否（終了コード 3）
  work.py renew   --dir <作業場所> --token <t> [--lease <分>]          → 引き継がれていれば拒否（3）。旧担当はここで止まる
  work.py release --dir <作業場所> --token <t>
  work.py status  --dir <作業場所>
  work.py op begin   --dir <作業場所> --token <t> --key <操作の鍵> --what <何をするか>  → 同じ鍵が既にあれば拒否（4）
  work.py op done    --dir <作業場所> --token <t> --key <操作の鍵> --result <結果>
  work.py op pending --dir <作業場所>                                 → 始めたが終わりの記録が無い操作
  work.py check   --dir <作業場所> --from <外部操作を実行する場所>      → そこで使われる GitHub のアカウントと、push 先の origin への書き込み権限（書けない・アカウントが分からなければ 5）
  work.py task start --dir <checkout> --holder <誰か> [--base <commit等>] [--target <ブランチ>] → 専用 worktree と担当を作る
  work.py task list --dir <checkout>                               → タスク、担当、未完了の統合
  work.py task resume --dir <checkout> --task <識別子> --holder <誰か> → 作業場所を保持して担当を取得
  work.py integrate --dir <worktree> --token <t> --expected <統合先commit> --verified <検証済みcommit> → 排他・照合して fast-forward
  work.py task cleanup --dir <worktree> --token <t> --saved <別のブランチかタグ> → 保存先と差分を照合して片付け

task と integrate の照合拒否は終了コード 6、担当の拒否は 3。統合のロックは期限では奪わず、子の Git にも渡す。
統合の記録はリポジトリ共通。中断後は前回の候補を含む統合先から完了を復元するか、新しく検証した候補で再開する。

公開する操作（鍵の種類が push・pr・merge・release）の op begin には --verified <検証した commit> が要る。作業場所の HEAD と違えば拒否する（6）。
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


def claim_at(r, d, holder, minutes):
    now = time.time()
    old = read(r / 'lease.json')
    if alive(old, now):
        return {'ok': False, 'reason': '生きた担当がいる。二重に書かない',
                'holder': old['holder'], 'expires_in_s': round(old['expires_at'] - now)}, 3
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
                git(repo, 'merge-base', '--is-ancestor', cleanup['commit'], cleanup['saved'])
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
            if not ref.startswith(('refs/heads/', 'refs/tags/')) or ref == 'refs/heads/' + task['branch']:
                raise ValueError('保存先には、このタスク以外のブランチかタグを指定する')
            head = git(a.dir, 'rev-parse', 'HEAD')
            git(repo, 'merge-base', '--is-ancestor', head, ref)
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
        kind = a.key.split(":")[0] if ":" in a.key else None
        # 書き間違い・言い換えを列挙して止めると漏れる（push/…・pr#12・pull-request）ので、外部に出ない操作の記録を明示させる
        if a.action == "begin" and kind not in EXTERNAL and kind != "local" and not (kind or "").startswith("local-"):
            return out({"ok": False, "reason": f"鍵 {a.key!r} の種類が分からない。鍵は <種類:対象> で、種類は外部操作なら "
                        "push・pr・comment・merge・release・issue のどれか（小文字）、外部に出ない操作の記録なら local（または local- で始まる名前）"}, 2)
        if a.action == "begin" and kind in EXTERNAL:
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
