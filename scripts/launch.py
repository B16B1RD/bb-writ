#!/usr/bin/env python3
"""別系統の CLI（Claude Code・Codex・Grok Build）を bb-writ 専用のログインで起動し、報告を回収して検査する（plan-v3 4.2 の launcher）。

使い方:
  python3 launch.py --host codex|claude|grok --cwd <対象> --prompt-file <依頼> [--model M] [--effort E] [--write] [--out <dir>]

標準出力に 1 行の JSON を出す。status は ok / not-run / invalid-report / failed / quota / unavailable。ok のときだけ終了コード 0。
報告を使えるのは ok と、読んだだけの報告として使う not-run。
子には、最後に ```json {"conclusion": ..., "evidence": [{"what": ..., "where": ...}], "unknowns": [...], "execution": {...}} ``` を書かせる。
JSON が返ったことを成功にしない: 結論が空、証拠が無い、証拠の where がファイルを指すのに存在しない、実行の欄の形が違う、は invalid-report。
読み取り専用の子で、実行の欄が無いか、対象の試験・検査を実行できなかったと書いた報告は not-run（読んだだけの報告。起動して形の整った報告が返ったことと区別する）。
書く子の実行の欄は記録するだけ（作業場所の試験は親が確かめる）。実行の欄の真偽（本当に動かしたか）は見ない。
トークンは画面にもログにも出さない（Claude Code は dotenvx run で子の環境にだけ渡す）。
"""
from __future__ import annotations  # macOS の Command Line Tools の python3（3.9）でも型注釈で落ちないように
import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.dont_write_bytecode = True  # work.py の import で、プラグインの置き場に __pycache__ を作らない
sys.path.insert(0, str(Path(__file__).resolve().parent))
from work import root as work_record  # noqa: E402  作業場所ごとの記録の置き場（担当・記録・証拠）

DEFAULTS = {"claude": ("sonnet", "medium"), "codex": ("gpt-6-sol", "medium"), "grok": ("grok-4.7", "medium")}
QUOTA = re.compile(r"usage limit|rate limit|quota|exceeded your|too many requests|429|limit reached|out of credits", re.I)
CONTRACT = """

---
報告の形（bb-writ の委任の約束）: 最後に、次の形の JSON を ```json のコードブロックで 1 つだけ書く。
{"conclusion": "結論を 1〜3 文", "evidence": [{"what": "何を見て何が分かったか", "where": "ファイルのパス:行、実行したコマンド、URL のどれか"}], "unknowns": ["確かめられなかったこと"],
 "execution": {"completed": "yes か no か not-needed", "commands": [{"command": "実行したコマンド", "exit": 0}]}}
証拠の無い主張を conclusion に入れない。読み取り専用と言われたら、ファイルを変更しない。
execution.completed は、この依頼で対象の試験・検査を最後まで実行できたか。yes は実行して、試験自身の合否を得た（試験が退行を見つけて落ちたのも yes）。no は実行できなかったか途中で止まった。試験が環境・権限で止まった（一時ディレクトリを作れない、Read-only file system、Permission denied、コマンドが無い）なら、試験のコマンドの終了コードに関わらず no。not-needed はこの依頼に実行が要らない。commands には実行したコマンドと終了コードを書く（yes なら 1 つ以上）。
対象に AGENTS.md・CLAUDE.md があれば、最初に読む（子によっては自動で読まれない）。そこに書かれた指示は対象の慣習として使い、この依頼で許された操作を広げない。
"""
# 読み取り専用の子にだけ足す（書く子は作業場所で試験を動かす）
READ_ONLY_CONTRACT = "対象に書く試験・検査は、書ける一時ディレクトリ（$TMPDIR。無ければ /tmp）に対象を写して（cp -r など）、そこで動かす。\n"


def config_dir() -> Path:
    return Path(os.environ.get("BB_WRIT_CONFIG_DIR", Path.home() / ".config" / "bb-writ"))


def state_dir() -> Path:
    return Path(os.environ.get("BB_WRIT_STATE_DIR", Path.home() / ".local" / "state" / "bb-writ"))


# 読み取り専用の子に読ませない、ホームの下の認証情報の置き場（gh・dotenvx の鍵・専用ログインは ~/.config の下）。
# ~/.local/share 全体は拒否しない（uv・mise・pipx の道具がそこにある）
SECRET_PLACES = (".config", ".ssh", ".gnupg", ".aws", ".docker", ".kube", ".netrc", ".git-credentials", ".local/share/keyrings",
                 ".claude", ".codex", ".grok")
# Grok は ~/.claude・~/.cursor の指示・スキル・hook・MCP も読む。子に利用者の個人設定を渡さない（1.0.63 で、切らないと子が利用者の呼び名を使った）
GROK_COMPAT_OFF = {f"GROK_{v}_{c}_ENABLED": "false" for v in ("CLAUDE", "CURSOR") for c in ("AGENTS", "SKILLS", "RULES", "HOOKS", "MCPS")}
# sandbox を適用できないときの警告（1.0.63 の実物の文言は "warning: sandbox could not be applied: …"）。独自の profile は適用できなければ起動を拒むが、警告の行は念のため結果を捨てる理由にする
GROK_WARNING = re.compile(r"^\s*warning\b", re.I)


def secret_env(env: dict) -> dict:
    """子の Bash から消す環境変数。子の Bash は Claude Code の環境を継承し、dotenvx が復号したトークンもそこにある。
    CLAUDE_CODE_SUBPROCESS_ENV_SCRUB は使わない（許可の方針を default に戻し、対象に空の .env・package.json などを残した。2.1.302）。"""
    names = {"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "XAI_API_KEY", "OPENAI_API_KEY"}
    names |= {k for k in env if re.search(r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|(^|_)KEY($|_)|API_?KEY", k, re.I)}
    return {"envVars": [{"name": n, "mode": "deny"} for n in sorted(names)]}


READ_ONLY_ENV = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TERM", "TZ", "TMPDIR",
                 "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE"}


def outside_reads(keep: list[Path]) -> list[str]:
    """ホームの下で、keep のどれにも至らない場所。keep に至る経路の上の兄弟だけを並べ、経路そのものは拒否しない。"""
    keep_set = {p.resolve() for p in keep}
    out = []

    def walk(d: Path):
        try:
            entries = sorted(d.iterdir())
        except OSError:
            return
        for e in entries:
            r = e.resolve() if e.is_symlink() else e
            if r in keep_set:
                continue
            if not e.is_symlink() and any(e in k.parents for k in keep_set):
                walk(e)
            else:
                out.append(str(e))

    walk(Path.home().resolve())
    return out


def secret_reads(cfg: Path) -> dict:
    """子の Bash に読ませない認証情報の置き場（専用ログインの置き場は ~/.config の外にも置ける）。Git の設定だけは読ませる。"""
    return {"denyRead": [str(Path.home() / p) for p in SECRET_PLACES] + [str(cfg)], "allowRead": [str(Path.home() / ".config" / "git")]}


def home_entries(skip: list[Path]) -> tuple[set[str], set[str], set[str]]:
    """ホームの 2 段目までの (すべての場所, 空のファイルと空のディレクトリ, 読めない場所)。skip とその下は見ない。
    読めない場所を黙って飛ばすと、子が権限を落とした場所の残りを見逃すので、呼ぶ側が前後で比べる。
    子の CLI の sandbox は、守る対象のうち無いものを空のファイルで作り、残すことがある（.bash_profile が残ると、ログインシェルが .bashrc を読まなくなった。2.1.302）。"""
    every, empty, unreadable = set(), set(), set()

    def scan(d: Path, depth: int):
        try:
            entries = list(os.scandir(d))
        except PermissionError:  # 権限で読めない場所だけを数える（走査の途中で消えた一時ファイルは数えない）
            unreadable.add(str(d))
            return
        except OSError:
            return
        for e in entries:
            p = Path(e.path)
            if any(p == s or s in p.parents for s in skip):
                continue
            every.add(e.path)
            try:
                if e.is_symlink():
                    continue
                if e.is_dir():
                    with os.scandir(e.path) as it:  # 閉じないと ResourceWarning が出る
                        bare = not any(it)
                    if bare:
                        empty.add(e.path)
                    elif depth < 2:
                        scan(p, depth + 1)
                elif e.is_file() and e.stat().st_size == 0:
                    empty.add(e.path)
            except PermissionError:
                unreadable.add(e.path)
            except OSError:
                continue

    scan(Path.home(), 1)
    return every, empty, unreadable


def grok_ro_writable(home: Path) -> list[Path]:
    """Grok の read-only の sandbox でも書ける場所（公式文書 18-sandbox と、実物の sessions/sandbox-events.jsonl の read_write_paths）。"""
    roots = [Path("/tmp"), Path("/var/tmp"), Path(tempfile.gettempdir()), home]  # gettempdir は macOS の一時ディレクトリ
    return sorted({p.resolve() for p in roots})


GIT_TIMEOUT = float(os.environ.get("BB_WRIT_GIT_TIMEOUT") or 60)  # 子が管理ファイルを FIFO にすると、git はそれを開いたまま止まる（環境変数は試験だけが使う）


def grok_git_dir(cwd: Path) -> tuple[Path | None, str | None]:
    """worktree の commit が書く Git の共通ディレクトリ。Git の外なら None。"""
    g = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--git-common-dir"], capture_output=True, text=True, timeout=GIT_TIMEOUT)
    if g.returncode == 0:
        return (cwd / g.stdout.strip()).resolve(), None
    if "not a git repository" in g.stderr:
        return None, None
    return None, f"Git の共通ディレクトリを取れない（git rev-parse: {g.stderr.strip()[:200]}）"


def worktree_dirs(cwd: Path) -> tuple[Path | None, Path | None, str | None]:
    """(その作業場所の管理ディレクトリ, 共通ディレクトリ, 理由)。Git の外なら (None, None, None)。"""
    g = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"],
                       capture_output=True, text=True, timeout=GIT_TIMEOUT)
    if g.returncode == 0:
        own, common = g.stdout.split()
        return Path(own).resolve(), Path(common).resolve(), None
    if "not a git repository" in g.stderr:
        return None, None, None
    return None, None, f"Git の管理ディレクトリを取れない（git rev-parse: {g.stderr.strip()[:200]}）"


def commit_dirs(own: Path, common: Path) -> list[Path]:
    """worktree で commit するのに要る、共通ディレクトリの中の場所。共通ディレクトリの直下・config・hooks と、
    他のブランチの ref は渡さない（書けると、利用者が次に git を動かしたとき sandbox の外でコードが動くか、統合先が動く）。"""
    head = (own / "HEAD").read_text(encoding="utf-8").strip()
    dirs = [common / "objects", own]
    if head.startswith("ref: refs/heads/"):
        parent = Path(head[len("ref: "):]).parent  # refs/heads/bb-writ。ref の lock はこのディレクトリに作られる
        dirs += [common / parent, common / "logs" / parent]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
    return dirs


def read_child_file(f: Path, keep: int = 0):
    """書く子が書き換えうるファイルを読む。FIFO・/dev/zero への symlink・巨大なファイルにされても止まらず尽きないよう、
    symlink は行き先と、行き先の種類（通常のファイルならその権限と中身のハッシュ）で表す。通常のファイルも権限と中身で表す。どれも止まらずに開いて種類を見て、
    通常のファイルだけを決まった大きさずつハッシュする。
    無ければ None。(表し, 先頭 keep バイト, 通常のファイルか) を返す。表しは中身全体の変化を映す。"""
    try:
        st = os.lstat(f)
    except FileNotFoundError:
        return None, b"", False
    if stat.S_ISLNK(st.st_mode):  # 行き先と、行き先が通常のファイルならその中身の変化も差分にする（symlink の config は普段の使い方でありうる）
        try:
            fd = os.open(f, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            return f"symlink: {os.readlink(f)!r}（開けない: {e.strerror}）", b"", False
        try:
            mode = os.fstat(fd).st_mode
            return f"symlink: {os.readlink(f)!r} " + (f"{stat.filemode(mode)} {digest_of(fd)}" if stat.S_ISREG(mode) else f"（{stat.filemode(mode)}）"), b"", False
        finally:
            os.close(fd)
    fd = os.open(f, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)  # FIFO でも開くところで止まらない
    try:
        mode = os.fstat(fd).st_mode  # 開いたものの種類（lstat の後に置き換えられても）
        if not stat.S_ISREG(mode):
            return f"（通常のファイルでない: {stat.filemode(mode)}）", b"", False
        head = []
        return f"{stat.filemode(mode)} {digest_of(fd, keep, head)}", b"".join(head), True  # 権限も（hook に実行権限を足すと git が動かす）
    finally:
        os.close(fd)


def digest_of(fd: int, keep: int = 0, head: list | None = None) -> str:
    """開いた通常のファイルを区切って読み、ハッシュを返す（大きくてもメモリは一定）。head に先頭 keep バイトを足す。"""
    h, kept = hashlib.sha256(), 0
    while chunk := os.read(fd, 1 << 20):
        h.update(chunk)
        if head is not None and kept < keep:
            head.append(chunk[:keep - kept])
            kept += len(head[-1])
    return f"sha256: {h.hexdigest()}"


def read_head(f: Path) -> str:
    """作業場所の HEAD。小さければ中身（末尾の空白の変化も見るので整えない）、大きければ大きさとハッシュで表す。"""
    digest, head, regular = read_child_file(f, 4097)
    if digest is None:
        return "（無い）"
    if not regular:  # symlink・通常のファイルでない
        return digest
    return head.decode("utf-8", "surrogateescape") if len(head) <= 4096 else f"（4 KiB を超える。{digest}）"


def git_snapshot(own: Path, common: Path, cwd: Path) -> dict:
    """書く子の前後で比べる Git の管理情報。子の書いた .git ファイルを読まないよう、共通ディレクトリを明示して読む。"""
    for f in (common / "config", common / "packed-refs"):  # git が開いたまま止まる・尽きるもの（FIFO・デバイス）を渡さない
        try:
            mode = os.stat(f).st_mode  # git と同じく symlink はたどる（symlink の config は普段の使い方でありうる）
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(mode):
            raise ValueError(f"{f} が通常のファイルでない: {stat.filemode(mode)}")
    refs = subprocess.run(["git", "--git-dir", str(common), "for-each-ref", "--format=%(refname) %(objectname)"],
                          capture_output=True, check=True, timeout=GIT_TIMEOUT).stdout.decode("utf-8", "surrogateescape").split("\n")  # ref の名前は UTF-8 とは限らない（比べるだけ）
    files = [cwd / ".git", own / "commondir", own / "gitdir", own / "config.worktree", common / "config",
             common / "objects" / "info" / "alternates"]
    for d in (common / "hooks", common / "info"):
        files += sorted(f for f in d.rglob("*") if f.is_file() or f.is_symlink()) if d.is_dir() else []
    return {"refs": dict(r.split(" ", 1) for r in refs if " " in r),
            "files": {str(f): read_child_file(f)[0] for f in files},
            "head": read_head(own / "HEAD")}


def git_changes(before: dict, after: dict, common: Path) -> list[str]:
    """子が変えてよいのは、起動時に checkout していたブランチの ref と、その作業場所の HEAD・index だけ。
    ref は前後の値と戻すコマンドを添える（自動では戻さない。子の実行中に親や別のタスクが正当に動かしたかもしれない）。"""
    head = before["head"].strip()
    mine = head[len("ref: "):] if head.startswith("ref: ") else None
    out = []
    for r in sorted(set(before["refs"]) | set(after["refs"])):
        old, new = before["refs"].get(r), after["refs"].get(r)
        if r != mine and old != new:
            undo = f"git --git-dir {common} update-ref {r} {old}" if old else f"git --git-dir {common} update-ref -d {r}"
            out.append(f"{r}: {old or '(無し)'} → {new or '(削除)'}（戻すなら: {undo}）")
    out += sorted(f for f in set(before["files"]) | set(after["files"]) if before["files"].get(f) != after["files"].get(f))
    if after["head"] != before["head"]:
        out.append(f"HEAD: {before['head']!r} → {after['head']!r}")
    return out


def grok_home(login: Path, extends: str, read_write: list[Path]) -> tuple[Path, str]:
    """起動ごとの GROK_HOME を作り、(場所, profile 名) を返す。Grok の sandbox は read-only でも GROK_HOME に書けるので、
    sandbox.toml・config.toml・hooks・trusted_folders.toml を後の子と共有しない。共有するのはログインの auth.json（symlink）だけ。
    子が置き換えうるので、home の auth.json を専用ログインへ書き戻すことはしない。
    組み込みの profile は適用できないと警告して強制せずに続くが、独自の profile は起動を拒む（公式文書 18-sandbox）ので、読み取り専用も独自の profile にする。"""
    base = state_dir() / "grok-homes"
    base.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), dir=base))
    (home / "auth.json").symlink_to(login / "auth.json")
    rw = [str(p.resolve()) for p in read_write]
    (home / "sandbox.toml").write_text(f'[profiles.bbwrit]\nextends = "{extends}"\nread_write = {json.dumps(rw)}\n', encoding="utf-8")
    return home, "bbwrit"


CODEX_PROFILE = "bbwrit-read-only"


def codex_read_only(scratch: Path) -> list[str]:
    """読み取り専用の Codex の権限。read-only を継ぎ、書けるのは起動ごとの一時ディレクトリ（$TMPDIR）だけ。
    -s read-only では mktemp -d も失敗し、試験を動かせない審査役が読んだだけの判定を返した（0.163.0）。
    -s（sandbox_mode）は default_permissions より優先されるので渡さない。ネットワークは read-only の既定のまま出られない。"""
    scratch.mkdir()
    return ["-c", f'default_permissions="{CODEX_PROFILE}"', "-c", f'permissions.{CODEX_PROFILE}.extends=":read-only"',
            "-c", f'permissions.{CODEX_PROFILE}.filesystem={{":tmpdir"="write"}}']


def codex_breach(eff) -> str | None:
    """読み取り専用の Codex の実効の権限（記録の turn_context）が、渡した profile と違えばその理由。
    読み込まれたどの設定の sandbox_mode も default_permissions より優先され（対象の .codex/config.toml・専用ログインの config.toml）、
    同じ profile に書ける場所を足すこともできる。広ければ読み取り専用でなく、一時ディレクトリに書けなければ試験を動かせない。
    実効の権限を記録から読めなければ、どちらとも確かめられないので理由にする。"""
    turns = obj(eff).get("turns") or []
    if not turns or any(t["writable"] is None or t["network"] is None for t in turns):
        return "Codex の実効の権限を記録（turn_context の permission_profile）から確かめられない"
    tmp = (":tmpdir",)
    for t in turns:  # どの turn も（合わせると、ある turn だけ書けないことが隠れる）
        wide = [w for w in t["writable"] if w not in tmp]
        if wide or t["network"] != "restricted":
            return f"Codex の実効の権限が、読み取り専用の profile より広い（書ける場所: {wide[:5]}、ネットワーク: {t['network'][:50]}）"
        if not any(w in tmp for w in t["writable"]):
            return "Codex の実効の権限で、一時ディレクトリに書けない（設定の sandbox_mode が profile より優先されたかもしれない）"
    return None


def command(a, prompt: str, scratch: Path) -> tuple[list[str], dict, str | None]:
    """起動するコマンドと環境。使えなければ (…, …, 理由)。scratch は読み取り専用の Codex に渡す一時ディレクトリ。"""
    cfg = config_dir()
    env = dict(os.environ)
    if a.host == "claude":
        env_file, keys = cfg / ".env", cfg / ".env.keys"
        for tool in ("claude", "dotenvx"):
            if not shutil.which(tool):
                return [], env, f"{tool} が PATH に無い"
        if not env_file.is_file() or not keys.is_file():
            return [], env, f"専用ログインが無い: {env_file} と {keys.name}（claude setup-token の結果を dotenvx で保存する）"
        # 親の Claude Code の環境（CLAUDE_CODE_PLUGIN_DIRS の利用者の mod、CLAUDE_EFFORT など。mod は sandbox の外で動く）と API の鍵・接続先を渡さない。
        # 鍵があると専用ログインの代わりに別料金の API へ落ちうる。専用ログインのトークンは dotenvx が子の環境にだけ入れる
        env = {k: v for k, v in env.items() if not k.startswith(("CLAUDE", "ANTHROPIC_"))}
        env["CLAUDE_CONFIG_DIR"] = str(cfg / "claude-home")
        if a.write:
            # 書く子に渡すのは、対象（作業場所）とその記録だけ。タスクの一覧・統合の記録・他の worktree は親だけが書く。
            # Bash は sandbox で同じ範囲に閉じ、--restricted でファイル系のツールも同じ範囲に閉じる（対象の .claude の設定も読まない）。
            # ネットワークは、これまでどおり出られるようにする（sandbox の既定は宛先なしで、依存の取得もできなくなる）
            perm = ["--permission-mode", "auto", "--restricted", "--tools", "Read,Grep,Glob,Edit,Write,Bash,Skill,WebSearch,WebFetch", "--add-dir", str(work_record(a.cwd))]
            settings = {"sandbox": {"enabled": True, "allowUnsandboxedCommands": False, "failIfUnavailable": True,
                                    "network": {"allowedDomains": ["*"]}, "filesystem": secret_reads(cfg), "credentials": secret_env(env)}}
        else:
            # 読み取り専用は、コマンドの許可規則でなく OS の sandbox で守る（git log/diff/show は --output で書けた。2.1.302 で実測）。
            # 書き込みは対象を含めて拒否し、ネットワークは許可した宛先が無いので出られない。
            # --restricted は対象の .claude の設定（hook・許可規則）を読まず、ファイル系のツールを対象の中に閉じる。その下では
            # --allowedTools が効かないので、許可は --settings に書く。sandbox を起動できなければ子を起動しない（failIfUnavailable）
            # linked worktree では、sandbox が共有の .git とその周り（元のリポジトリの親まで書けた。同日）への書き込みを
            # 自動で許すので、ホーム・共通ディレクトリ・対象と共通ディレクトリの共通の親も拒否する
            git_dir, why = grok_git_dir(a.cwd)
            if why:
                return [], env, why
            deny = [str(a.cwd), str(Path.home())] + ([str(git_dir), os.path.commonpath([a.cwd, git_dir])] if git_dir else [])
            # 読みは、ホームのうち対象・共通ディレクトリ・Git の設定・PATH 上の道具に至る経路の外を拒否する（名前を挙げた置き場の外にある
            # 別のリポジトリの .env なども読ませない）。ホーム全体を拒否して対象だけ戻すと、bwrap が対象の中の保護ファイルを作れずに起動しなかった（同日）
            home = Path.home()
            keep = [a.cwd, *([git_dir] if git_dir else []), home / ".gitconfig", home / ".config" / "git",
                    *(Path(d) for d in env.get("PATH", "").split(os.pathsep) if d.startswith(str(home)))]
            # 子の Bash に渡す環境も最小限にする（読み取り専用の子は git と読むコマンドしか使わない）
            env = {k: v for k, v in env.items() if k in READ_ONLY_ENV or k.upper().endswith("_PROXY") or k.startswith("LC_")}
            env["CLAUDE_CONFIG_DIR"] = str(cfg / "claude-home")
            perm = ["--permission-mode", "dontAsk", "--restricted", "--tools", "Read,Grep,Glob,Bash,Skill"]
            settings = {"permissions": {"allow": ["Read", "Grep", "Glob", "Bash", "Skill"]},
                        "sandbox": {"enabled": True, "allowUnsandboxedCommands": False, "failIfUnavailable": True,
                                    "filesystem": {"denyWrite": sorted(set(deny)), "denyRead": outside_reads(keep) + [str(cfg)]},
                                    "credentials": secret_env(env)}}
        cmd = ["dotenvx", "run", "-q", "-f", str(env_file), "-fk", str(keys), "--",
               "claude", "-p", prompt, "--output-format", "json", "--model", a.model, "--effort", a.effort,
               "--permission-prompts", "none", *perm, "--settings", json.dumps(settings),
               "--plugin-dir", str(Path(__file__).resolve().parents[1])]  # --restricted は専用ログインに入れたプラグインを読まないので、この bb-writ を渡す
        return cmd, env, None
    if a.host == "grok":
        if not shutil.which("grok"):
            return [], env, "grok が PATH に無い"
        login = cfg / "grok-auth"
        if not (login / "auth.json").is_file():
            return [], env, f"専用ログインが無い: {login}/auth.json（GROK_HOME={login} grok login）"
        if a.write:
            own, common, why = worktree_dirs(a.cwd)
            if why:
                return [], env, why
            home, profile = grok_home(login, "workspace", [work_record(a.cwd), *(commit_dirs(own, common) if own else [])])
        else:
            # 起動ごとの home は状態ディレクトリの下にできるので、それと重なる対象も止める
            for d in grok_ro_writable(state_dir() / "grok-homes"):
                if a.cwd == d or d in a.cwd.parents or a.cwd in d.parents:
                    return [], env, f"対象と {d} が重なる。Grok の read-only の sandbox はそこへの書き込みを止めないので、読み取り専用にできない"
            home, profile = grok_home(login, "read-only", [])
        # 親の GROK_*（GROK_AGENT・GROK_SANDBOX・GROK_CONFIG など）と API キーを渡さない。API キーがあるとログインの代わりに別料金の API へ落ちる
        env = {k: v for k, v in env.items() if not k.startswith("GROK_") and k not in ("XAI_API_KEY", "RUST_LOG")}
        env.update(GROK_HOME=str(home), GROK_MEMORY="0", **GROK_COMPAT_OFF)
        cmd = ["grok", "-p", prompt, "--output-format", "json", "-m", a.model, "--effort", a.effort, "--cwd", str(a.cwd),
               "--sandbox", profile, "--always-approve",
               "--no-leader", "--no-auto-update", "--no-subagents", "--disable-web-search", "--disallowed-tools", "x_search"]
        return cmd, env, None
    if not shutil.which("codex"):
        return [], env, "codex が PATH に無い"
    home = cfg / "codex-home"
    if not (home / "auth.json").is_file():
        return [], env, f"専用ログインが無い: {home}/auth.json（CODEX_HOME={home} codex login）"
    env["CODEX_HOME"] = str(home)
    if a.write:
        # 作業記録と、worktree で commit するのに要る共通ディレクトリの中の場所だけを渡す（管理ディレクトリが無いと index.lock を作れなかった。0.163.0）
        own, common, why = worktree_dirs(a.cwd)
        if why:
            return [], env, why
        mode = ["--approve-for-me", "-c", "sandbox_workspace_write.network_access=true"]
        for d in [work_record(a.cwd), *(commit_dirs(own, common) if own else [])]:
            mode += ["--add-dir", str(d)]
    else:
        mode = codex_read_only(scratch)
        env["TMPDIR"] = str(scratch.resolve())
    cmd = ["codex", "exec", "--json", "-m", a.model, "-c", f'model_reasoning_effort="{a.effort}"',
           "--skip-git-repo-check", "-C", str(a.cwd), *mode, prompt]
    return cmd, env, None


def obj(v) -> dict:
    """子の JSON の値。オブジェクトでなければ空として扱う（子の出力は外からの入力）。"""
    return v if isinstance(v, dict) else {}


def text_of(v) -> str:
    return v if isinstance(v, str) else ""


def json_lines(out: str) -> list:
    """JSON Lines の行。区切りは \n だけ（CLI は文字列の中の U+2028・U+0085 をエスケープせずに出し、splitlines はそこでも割る）。"""
    return out.split("\n")


def parse_claude(out: str) -> dict:
    d = {}
    for line in json_lines(out):
        if line.startswith("{"):
            try:
                d = obj(json.loads(line))
            except (ValueError, RecursionError):
                continue
    return {"text": text_of(d.get("result")), "session": text_of(d.get("session_id")) or None, "is_error": d.get("is_error"),
            "errors": [str(d.get("result"))[:300]] if d.get("is_error") else [],
            "model_effective": sorted(obj(d.get("modelUsage")).keys()) or None,
            "usage": {"cost_usd": d.get("total_cost_usd"), "turns": d.get("num_turns")}}


def rollout_lines(f: Path, limit: int = 256 << 20):
    """Codex の記録の行（\n だけで分ける。json_lines と同じ理由）。子が書きうるので、通常のファイルだけを、
    1 行 1 MiB・全体 limit までで読む（巨大な行は割れて読めない行になり、権限は不明になる）。全体の上限を越えたら、
    途中までの実効値を使わないよう、読めない行を 1 つ返して終える。"""
    try:
        fd = os.open(f, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return
    if not stat.S_ISREG(os.fstat(fd).st_mode):  # fdopen はディレクトリを拒むので、その前に見る
        os.close(fd)
        return
    with os.fdopen(fd, "rb") as fh:
        read = 0
        while line := fh.readline(1 << 20):
            read += len(line)
            if read > limit:
                yield b'{"type": "turn_context", "payload": {}}'  # 上限を越えた。実効値は不明（各値を null にする）
                return
            yield line.rstrip(b"\n")


def where_of(path) -> str:
    """permission_profile の場所の表し。特別な場所は :tmpdir の形、ほかは絶対パス。読めない形は None。"""
    path = obj(path)
    if path.get("type") == "special" and text_of(obj(path.get("value")).get("kind")):
        return ":" + obj(path.get("value"))["kind"]
    if path.get("type") == "path" and text_of(path.get("path")):
        return path["path"]
    return None


def parse_codex(out: str) -> dict:
    text, session, usage, errors = "", None, None, []
    for line in json_lines(out):
        try:
            e = obj(json.loads(line))
        except (ValueError, RecursionError):
            continue
        item = obj(e.get("item"))
        if e.get("type") == "thread.started":
            session = text_of(e.get("thread_id")) or None
        elif e.get("type") == "item.completed" and item.get("type") == "agent_message":
            text = text_of(item.get("text"))
        elif e.get("type") == "turn.completed":
            usage = e.get("usage")
        elif e.get("type") in ("error", "turn.failed"):
            errors.append(str(e.get("message") or obj(e.get("error")).get("message") or "")[:300])
    eff, perms = None, []
    # 実効値は記録の turn_context から（メタデータだけを読む）。session は UUID の形のときだけ探す（glob に記号を渡さず、別の記録の末尾に当たらない）
    if session and re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", session):
        for f in (config_dir() / "codex-home" / "sessions").glob(f"**/rollout-*-{session}.jsonl"):
            for line in rollout_lines(f):
                try:
                    e = obj(json.loads(line.decode("utf-8")))
                except (ValueError, RecursionError):  # 書きかけの行、UTF-8 として読めない行（置き換えた文字を実効値にしない）。取れなければ null
                    perms.append((None, None))  # turn_context だったかもしれない（1 MiB を超えて割れた行も）。権限は不明として閉じる
                    continue
                if e.get("type") == "turn_context":
                    p = obj(e.get("payload"))
                    perm = obj(p.get("permission_profile"))
                    fs = obj(perm.get("file_system"))
                    entries = fs.get("entries") if fs.get("type") == "restricted" and isinstance(fs.get("entries"), list) else None
                    if entries is not None and not all(isinstance(x, dict) and x.get("access") in ("read", "write", "deny")
                                                       and where_of(x.get("path")) is not None for x in entries):
                        entries = None  # 読めない項目を「書ける場所ではない」と数えない（権限は不明）
                    eff = {"model": text_of(p.get("model")) or None, "effort": text_of(p.get("effort")) or None,
                           "sandbox": text_of(obj(p.get("sandbox_policy")).get("type")) or None}
                    # 書ける場所（特別な場所は :tmpdir の形）。0.163.0 の sandbox は、読み取り専用の profile でも workspace-write と記録される
                    denied = {where_of(x["path"]) for x in entries or [] if x["access"] == "deny"}  # 同じ場所なら deny が write に勝つ（公式文書）
                    perms.append((None if entries is None else [w for x in entries if x["access"] == "write" and (w := where_of(x["path"])) not in denied],
                                  text_of(perm.get("network")) or None))
    if eff is not None:  # 判定は turn ごと（codex_breach）。writable・network は表示のために合わせる（どれかが読めなければ不明）
        eff["turns"] = [{"writable": w, "network": n} for w, n in perms]
        eff["writable"] = None if any(w is None for w, _ in perms) else sorted({x for w, _ in perms for x in w})
        nets = {n for _, n in perms}
        eff["network"] = None if None in nets else next((n for n in sorted(nets) if n != "restricted"), "restricted")
    return {"text": text, "session": session, "model_effective": eff, "usage": usage, "errors": errors}


def parse_grok(out: str) -> dict:
    try:
        d = json.loads(out.strip() or "{}")
    except (ValueError, RecursionError):
        d = {}
    d = obj(d)
    error = d.get("type") == "error"
    return {"text": "" if error else text_of(d.get("text")), "session": text_of(d.get("sessionId")) or None, "is_error": error,
            "errors": [str(d.get("message") or "")[:300]] if error else [],
            "model_effective": sorted(obj(d.get("modelUsage")).keys()) or None,  # effort は結果に出ない
            "usage": {"cost_usd": d.get("total_cost_usd"), "turns": d.get("num_turns")}}


RAN = ("yes", "no", "not-needed")


def check_execution(r: dict) -> tuple[str | None, list[str]]:
    """報告の実行の欄。(completed, 形の問題)。欄が無ければ completed は None（読んだだけの報告として扱う）。
    意味（本当に実行したか）は見ない。形だけ: completed が 3 つの値のどれか、commands はコマンドと整数の終了コードの組、yes なら 1 つ以上。"""
    if "execution" not in r:
        return None, []
    ex = r["execution"]
    if not isinstance(ex, dict) or ex.get("completed") not in RAN:
        return None, [f"execution.completed が {' / '.join(RAN)} のどれでもない"]
    commands = ex.get("commands", [])
    if not isinstance(commands, list) or not all(isinstance(c, dict) and isinstance(c.get("command"), str) and c["command"].strip()
                                                 and type(c.get("exit")) is int for c in commands):
        return ex["completed"], ["execution.commands がコマンドと整数の終了コードの組の並びでない"]
    if ex["completed"] == "yes" and not commands:
        return "yes", ["execution.completed が yes なのに、実行したコマンドが無い"]
    return ex["completed"], []


def check_report(text: str, cwd: Path) -> tuple[dict | None, list[str]]:
    i = text.rfind("```json")
    if i < 0:
        return None, ["報告の JSON ブロックが無い"]
    try:  # 閉じのフェンスが無くても、最後のブロックの JSON を読む
        r, _ = json.JSONDecoder().raw_decode(text[i + len("```json"):].lstrip())
    except (ValueError, RecursionError) as e:  # 深い入れ子も、読めない報告
        return None, [f"報告の JSON が読めない: {e!r}"]
    if not isinstance(r, dict):
        return None, ["報告の JSON がオブジェクトでない"]
    try:  # 記録（indent 付きは Python の encoder で、C の decoder より浅い深さで落ちる）に書けるか
        json.dumps(r, indent=1)
    except (RecursionError, ValueError) as e:
        return None, [f"報告の JSON が深すぎて記録に書けない: {e!r}"]
    problems = []
    if not str(r.get("conclusion") or "").strip():
        problems.append("conclusion が空")
    ev = r.get("evidence")
    if not isinstance(ev, list) or not ev:
        problems.append("evidence が無い")
        ev = []
    for item in ev:
        where = str(obj(item).get("where") or "").strip()
        if not where:
            problems.append("where の無い証拠がある")
            continue
        m = re.fullmatch(r"`?([^\s`:]+):(\d+)(?:[-–]\d+)?`?", where)  # 「ファイル:行」の引用だけは実在を確かめる
        if m and not m.group(1).startswith("http"):
            path = m.group(1)
            p = Path(path) if Path(path).is_absolute() else cwd / path
            try:
                found = p.is_file()
            except OSError:  # 長すぎる名前など（3.12 の is_file は ENAMETOOLONG を投げる）
                found = False
            if not found:
                problems.append(f"証拠のファイルが無い: {path[:300]}")
    problems += check_execution(r)[1]
    return r, problems


def launcher_error(out_dir: Path) -> str:
    """想定外の例外のトレースバックを out に残し、その場所を返す。"""
    import traceback
    f = out_dir / "launcher-error.log"
    try:
        with open(f, "a", encoding="utf-8", errors="backslashreplace") as h:
            h.write(traceback.format_exc())
        return str(f)
    except OSError as e:
        return f"残せない: {e!r}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", choices=("claude", "codex", "grok"), required=True)
    ap.add_argument("--cwd", type=Path, required=True)
    ap.add_argument("--prompt-file", type=Path, required=True)
    ap.add_argument("--model")
    ap.add_argument("--effort")
    ap.add_argument("--write", action="store_true", help="変更を許す（既定は読み取り専用）")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--timeout", type=int, default=1800)
    a = ap.parse_args()
    a.model = a.model or DEFAULTS[a.host][0]
    a.effort = a.effort or DEFAULTS[a.host][1]
    a.cwd = a.cwd.resolve()
    # 絶対パスにし、symlink を解く（子は --cwd で動くので、相対の --out では子の TMPDIR が別の場所を指した。対象の中かは解いた形で見る）
    out_dir = (a.out or state_dir() / "delegations" / (time.strftime("%Y%m%d-%H%M%S") + '-' + secrets.token_hex(8))).resolve()
    if a.host == "codex" and not a.write and (a.cwd == out_dir or a.cwd in out_dir.parents):  # 作る前に（作ってから拒むと、対象の中に置き場が残る）
        ap.error(f"--out {out_dir} が対象の中にある。読み取り専用の Codex は出力の置き場の tmp/ に書けるので、対象の外に置く")
    out_dir.mkdir(parents=True, exist_ok=False)
    rec = {"host": a.host, "cwd": str(a.cwd), "model_requested": a.model, "effort_requested": a.effort,
           "write": a.write, "out": str(out_dir)}

    snapshot, why, prep = None, None, None
    try:  # 起動の前の準備も、前の子が残したもの（FIFO・権限・壊れた HEAD）に当たりうる。止まらず落ちずに記録して返す
        if a.write:
            own, common, why = worktree_dirs(a.cwd)
            if not why and own and own == common:
                # 元の checkout（とその下・submodule）の記録の置き場にはタスクの一覧があり、Grok の子には .git 全体が渡る
                why = "書く子は task start の作業場所（worktree）で起動する。元の checkout とその下では起動しない"
            if not why and own:
                snapshot = (own, common, git_snapshot(own, common, a.cwd))
        if not why:
            cmd, env, why = command(a, a.prompt_file.read_text(encoding="utf-8") + CONTRACT + ("" if a.write else READ_ONLY_CONTRACT),
                                    out_dir / "tmp")
        if a.host == "grok" and not why:
            rec["grok_home"] = env["GROK_HOME"]  # 再開（grok -r）はこの home で
        if not why:
            # --restricted の Claude Code は、対象と（worktree なら）元の checkout に空の .claude/.cc-writes を作る（2.1.302）。
            # 残すと task cleanup が未保存の変更とみなすので、この起動で新しくでき、空のままのものだけを後で消す
            made = []
            if a.host == "claude":
                git_dir, _ = grok_git_dir(a.cwd)
                for top in {a.cwd, *([git_dir.parent] if git_dir and git_dir.name == ".git" else [])}:
                    made += [d for d in (top / ".claude", top / ".claude" / ".cc-writes") if not d.exists()]
            # 子の前後でホームを比べ、新しくできて空のままの場所を残りとして報告する。黙って消さない（同じ時刻に別の道具が作ったものかもしれない）
            skip = [config_dir(), state_dir(), out_dir, a.cwd, Path.home() / ".cache"]
            home_before, _, unreadable_before = home_entries(skip)
    except Exception as e:
        prep = [f"起動の前の準備ができない: {e!r}"[:300], f"詳細: {launcher_error(out_dir)}"]
    t0 = time.time()
    if prep:
        rec.update(status="failed", problems=prep)
    elif why:
        rec.update(status="unavailable", problems=[why])
    else:
        child = None
        try:
            # 子が残した背景のプロセスが、終了後の照合の後に書き換えないよう、プロセスグループごと止める
            child = subprocess.Popen(cmd, cwd=a.cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     start_new_session=True)  # バイトで受ける（子の出力は UTF-8 とは限らない）
            try:
                stdout, stderr = child.communicate(timeout=a.timeout)
            finally:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            (out_dir / "stdout.jsonl").write_bytes(stdout)  # 照合の材料は生のまま残す
            (out_dir / "stderr.log").write_bytes(stderr)
            p = subprocess.CompletedProcess(cmd, child.wait(), stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace"))
            got = {"claude": parse_claude, "codex": parse_codex, "grok": parse_grok}[a.host](p.stdout)
            # 警告の行（sandbox の適用失敗、効かなかった引数）があれば、権限の約束どおりに動いたか分からないので結果を使わない
            unsafe = [l for l in p.stderr.splitlines() if a.host == "grok" and GROK_WARNING.search(l)][-5:]
            rec.update(exit_code=p.returncode, session=got["session"], model_effective=got["model_effective"], usage=got["usage"])
            if QUOTA.search(p.stderr[-4000:] + " ".join(got["errors"])):
                rec.update(status="quota", problems=["利用枠の上限に当たった（別料金の API へは切り替えない）"])
            elif unsafe:
                rec.update(status="failed", problems=["Grok が警告を出した（権限の約束どおりに動いたか分からない）"] + unsafe)
            elif p.returncode != 0 or got.get("is_error") or not got["text"]:
                tail = [l for l in p.stderr.strip().splitlines() if "injected env" not in l and "Reading additional input" not in l][-5:]
                rec.update(status="failed", problems=[f"終了コード {p.returncode}"] + got["errors"] + tail)
            elif breach := (a.host == "codex" and not a.write and codex_breach(got["model_effective"])):
                # 対象の設定が profile を広げた・狭めた（読み取り専用の結果として使わない）。報告は report.md に残す
                rec.update(status="failed", problems=[breach])
                (out_dir / "report.md").write_text(got["text"], encoding="utf-8", errors="backslashreplace")
            else:
                report, problems = check_report(got["text"], a.cwd)
                ran = check_execution(report)[0] if report is not None else None
                rec.update(status="invalid-report" if problems else "ok", report=report, problems=problems, execution=ran)
                if not problems and ran in (None, "no") and not a.write:  # 読んだだけの報告。実行が要る審査では審査役に数えない（interrogate の 3）
                    rec.update(status="not-run", problems=["対象の試験・検査を実行できなかった（読んだだけの報告）" if ran == "no"
                                                           else "報告に実行の欄（execution）が無い（読んだだけの報告として扱う）"])
                (out_dir / "report.md").write_text(got["text"], encoding="utf-8", errors="backslashreplace")  # 単独のサロゲート
        except subprocess.TimeoutExpired:
            rec.update(status="failed", problems=[f"{a.timeout} 秒で終わらなかった"])
        except Exception as e:  # 想定外。委任の記録は書き、失敗として返す（トレースバックは out に残す）
            what = "子を起動できない" if child is None else "子の結果を読めない"
            rec.update(status="failed", problems=[f"{what}: {e!r}"[:300], f"詳細: {launcher_error(out_dir)}"])
        finally:
            # 後処理は、子が書き換えた場所を触る。どの段が失敗しても、記録は書き、失敗として返す
            if snapshot:
                try:
                    # Claude Code の sandbox は、worktree の管理ディレクトリに保護のための空の config.worktree を作る（2.1.302）。
                    # 起動前に無く空のままのファイルは、中身が無く git の動きを変えないので、消してから照合する
                    for f, h in snapshot[2]["files"].items():
                        if h is None and Path(f).is_file() and not Path(f).is_symlink() and Path(f).stat().st_size == 0:
                            Path(f).unlink()
                    changed = git_changes(snapshot[2], git_snapshot(*snapshot[:2], a.cwd), snapshot[1])
                    headline = "子が、自分のブランチ以外の Git の管理情報を変えた。"
                except Exception as e:  # 子が管理情報を壊したかもしれない。黙らず、照合できなかったことを返す
                    changed = [f"Git の管理情報を読めない（子が壊したかもしれない）: {e!r}"[:300], f"詳細: {launcher_error(out_dir)}"]
                    headline = "子の後に Git の管理情報を照合できない。"
                if changed:
                    rec.update(status="failed", problems=[headline + "この作業場所で git を動かす前に、下の場所を確かめる（偽の設定が sandbox の外で動きうる）"]
                               + changed[:20] + rec.get("problems", []))
            try:
                for d in reversed(made):
                    if d.is_dir() and not d.is_symlink() and not any(d.iterdir()):
                        d.rmdir()
            except Exception as e:  # 子が権限を変えたなど。残ったものは task cleanup が未保存の変更とみなす
                rec.update(status="failed", problems=[f"子の起動で作られた空の .claude を片付けられない: {e!r}"[:300],
                                                      f"詳細: {launcher_error(out_dir)}"] + rec.get("problems", []))
            try:
                _, empty, unreadable = home_entries(skip)
                # Claude Code 2.1.309 の sandbox は、設定を組むときに CLAUDE_CONFIG_DIR に関わらずホームに空のディレクトリ .claude/ssh を作る
                # （サーバー側の機能フラグが有効なとき。~/.claude が無ければそれも）。CLI 自身の置き場で（CLI の中にほかに使う所は見つからない）、
                # .bash_profile のようにほかの道具が読む場所ではないので数えない。消さない
                # （消すと、並行する別の委任の子と競う）。どのホストの委任でも数えない（並行する Claude Code の子が作ったものを、ほかの委任が見る）。
                # 同じ名前の空のファイルは数える。symlink は home_entries がもともと見ない（行き先がホームの 2 段目までの空の場所なら、それを数える）。
                # ~/.claude が symlink なら、その下は見ない（行き先がホームの直下なら、行き先の ssh は空の残りとして数える）。
                # HOME が空のときは、ここ（Python）はホームを / と読み、子の CLI と一致しない（Node は空と読む）。そのときの子の .claude/ssh は見ない
                ssh = Path.home() / ".claude" / "ssh"
                rec["home_residue"] = sorted(empty - home_before - ({str(ssh)} if ssh.is_dir() else set()))
                if unreadable - unreadable_before:  # 子が権限を落とした場所は、残りを確かめられない
                    raise PermissionError(f"読めない場所が増えた: {sorted(unreadable - unreadable_before)[:20]}")
            except Exception as e:
                rec.setdefault("home_residue", [])
                rec.update(status="failed", problems=[f"ホームの残りを確かめられない: {e!r}"[:600], f"詳細: {launcher_error(out_dir)}"]
                           + rec.get("problems", []))
            if rec["home_residue"]:
                rec.update(status="failed", problems=["子の起動の後、ホームに空のファイル・ディレクトリが新しく残った。中身を確かめて消す"
                                                      "（空の ~/.bash_profile があると、ログインシェルが ~/.profile と ~/.bashrc を読まない）"]
                           + rec["home_residue"][:20] + rec.get("problems", []))
    rec["duration_s"] = round(time.time() - t0, 1)
    try:
        body = json.dumps(rec, ensure_ascii=False, indent=1)
    except (RecursionError, ValueError) as e:  # 子から来た値が深すぎる。外して、失敗として記録する
        rec.update({k: None for k in ("report", "usage", "model_effective") if k in rec}, status="failed",
                   problems=[f"子の結果を記録に書けない（外した）: {e!r}"[:300]] + rec.get("problems", []))
        body = json.dumps(rec, ensure_ascii=False, indent=1)
    # 子の文字列には単独のサロゲートがありうる（JS の JSON.stringify が出す）。書き出しで落ちず、\udXXX の形で残す
    (out_dir / "record.json").write_text(body, encoding="utf-8", errors="backslashreplace")
    with open(state_dir() / "delegations.jsonl", "a", encoding="utf-8") as f:  # 1 行 1 件。U+2028 なども生で入れない
        f.write(json.dumps({k: v for k, v in rec.items() if k != "report"}) + "\n")
    sys.stdout.reconfigure(errors="backslashreplace")
    print(json.dumps(rec, ensure_ascii=False))
    return 0 if rec["status"] == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
