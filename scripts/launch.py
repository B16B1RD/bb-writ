#!/usr/bin/env python3
"""別系統の CLI（Claude Code・Codex・Grok Build）を bb-writ 専用のログインで起動し、報告を回収して検査する（launcher）。

使い方:
  python3 launch.py --host codex|claude|grok --cwd <対象> --prompt-file <依頼> [--model M] [--effort E] [--write [--hardest] | --interrogate | --audit] [--out <dir>] [--timeout 秒]
  python3 launch.py --resume <前の起動の out> --prompt-file <続きの依頼> [--out <dir>] [--timeout 秒]

標準出力に 1 行の JSON を出す。status は ok / not-run / invalid-report / timeout / failed / quota / unavailable。ok のときだけ終了コード 0。
timeout は、時間切れで止め、ほかの照合に当たらなかったもの。記録の session で、--resume が同じ session・同じ権限の続きを起動する。
報告を使えるのは ok と、読んだだけの報告として使う not-run。
子には、最後に ```json {"conclusion": ..., "evidence": [{"what": ..., "where": ...}], "unknowns": [...], "execution": {...}} ``` を書かせる。
--model・--effort を省くと、役割（--audit・--interrogate・読み取り専用・--write・--write --hardest）とホストから CHOICES の表で決める。
JSON が返ったことを成功にしない: 結論が空、証拠が無い、証拠の where がファイルを指すのに存在しない、実行の欄の形が違う、は invalid-report。
照合に落ちた報告は、同じ session の続きで 1 回だけ、落ちた点を名指しして直させる（fix_prompt。記録の report_fix、out の *-before-fix）。照合は同じで、直らなければ invalid-report。
--timeout の残りが FIX_BUDGET 秒より少ない・続きを組めない・起こせない・起こす前にシグナルを受けたときは、直させずに 1 回目の報告を返す。
読み取り専用の子で、実行の欄が無いか、対象の試験・検査を実行できなかったと書いた報告は not-run（起動して形の整った報告が返ったことと区別する）。
実行の欄が無いものは読んだだけの報告で、実行できなかった（no）と書いたものは、work.py の --not-run の宣言があれば push・pr の関門に数える。
書く子の実行の欄は記録するだけ（作業場所の試験は親が確かめる）。実行の欄の真偽（本当に動かしたか）は見ない。
子の CLI が終わったら（時間切れを含む。出力を開いたままの子孫は待たない）、子のプロセスグループを止め、Linux では自分の下に残った子孫
（新しいセッションに切り離したものも）を止めて stopped_after_exit に並べる。止めきれない・/proc を読めない・subreaper になれないなら failed。
Linux の外では、プロセスグループを止めるだけ（stopped_after_exit は出ない）。
--interrogate は、読み取り専用の子が interrogate の審査役だという宣言で、記録の interrogate に残す（work.py が、危険度が中・高の
push・pr でこれを求める）。依頼文の中身が審査だったかは見ない。
--audit は、読み取り専用の子が返す前の判定（スキルの要求を守ったかの監査）の子だという宣言で、記録の audit に残す
（work.py が、すべての push・pr で HEAD を対象にしたこの記録を求める）。依頼文の中身が監査だったかは見ない。
監査役の報告は判定 deliver（yes か no）を持ち、無いか値が違えば invalid-report。値は記録の deliver に残し、work.py は yes だけを関門に数える。
Codex の子は専用ログインに入った bb-writ の写しを読むので、写しが launcher と中身で違えば、入れ直せるときだけ入れ直し、
それでも違えば起動しない（unavailable。codex_refresh）。起動のたびに、自分が状態の置き場に作った一時の置き場のうち、もう続きから
起動されないものを消す（記録の swept。sweep_leftovers）。報告と記録（out の中、delegations.jsonl）は消さない。
Claude Code の子を起動する前に、専用ログインのトークンの組織と、launcher を呼んだ環境のログインの組織を比べ、記録の claude_login に
書く（起動は止めない。claude_login）。
トークンは画面にもログにも出さない（Claude Code は dotenvx run で子の環境にだけ渡す）。
"""
from __future__ import annotations  # macOS の Command Line Tools の python3（3.9）でも型注釈で落ちないように
import argparse
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import signal
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from work import root as work_record  # noqa: E402
from work import claude_staging_only, field_texts, record_at, REVISIT  # noqa: E402

# 親が --model・--effort を省いたときの、役割ごとのモデルと effort（親が渡せば、それが勝つ）。表に無い組み合わせは --model と --effort を渡す。
# 最も難しい依頼（設計、大きな移行・リファクタリング、長い無人実行）を任せる書く子（--hardest）は最上段。
# Haiku は報告の形を守れないことが多いので入れない。Claude の審査役は Opus high（Fable medium は欠陥のある変更 3 つを各 2 回審査させて Opus high より多くを挙げたが、
# 周ごとに文の指摘を出し続け、利用者の Fable の週の枠を使い切った。2026-11）。
# 監査役は Opus high（Opus medium は見落とした）。Claude の書く子は Opus high（指揮者は fork した Fable で、成果物は書く子が書く）。
CHOICES = {
    "audit": {"claude": ("opus", "high"), "codex": ("gpt-6-sol", "medium"), "grok": ("grok-4.7", "medium")},
    "interrogate": {"claude": ("opus", "high"), "codex": ("gpt-6-sol", "medium"), "grok": ("grok-4.7", "medium")},
    "read": {"claude": ("sonnet", "medium"), "codex": ("gpt-6-sol", "medium"), "grok": ("grok-4.7", "medium")},
    "write": {"claude": ("opus", "high"), "codex": ("gpt-6-sol", "medium"), "grok": ("grok-4.7", "medium")},
    "hardest": {"claude": ("fable", "medium"), "codex": ("gpt-6.1-sol", "high")},
}
# Fable を使えないとき（利用枠が尽きた 429・使えないモデルの 404）の代わりの段（#56）。子が、費用が 0 のまま（作業を始める前に）このエラーで終わったら、
# 同じ起動の中で 1 回だけこの段で立て直す（Opus high は、審査役で Fable medium に次いで欠陥を挙げた。上の CHOICES の注釈）
FALLBACK = {"claude": {"fable": ("opus", "high")}}
TIMEOUTS = {"hardest": 3600}
QUOTA = re.compile(r"usage limit|rate limit|quota|exceeded your|too many requests|(?:status|code|http|error)\W{0,12}429\b|\b429 too many|limit reached|out of credits", re.I)
CONTRACT = """

---
報告の形（bb-writ の委任の約束）: 最後に、次の形の JSON を ```json のコードブロックで 1 つだけ書く。
{"conclusion": "結論を 1〜3 文", "evidence": [{"what": "何を見て何が分かったか", "where": "ファイルのパス:行、実行したコマンド、URL のどれか"}], "unknowns": ["確かめられなかったこと"],
 "execution": {"completed": "yes か no か not-needed", "commands": [{"command": "実行したコマンド", "exit": 0}]}}
証拠の無い主張を conclusion に入れない。読み取り専用と言われたら、ファイルを変更しない。
execution.completed は、この依頼で対象の試験・検査を最後まで実行できたか。yes は実行して、試験自身の合否を得た（試験が退行を見つけて落ちたのも yes）。no は実行できなかったか途中で止まった。試験が環境・権限で止まった（一時ディレクトリを作れない、Read-only file system、Permission denied、コマンドが無い）なら、試験のコマンドの終了コードに関わらず no。not-needed はこの依頼に実行が要らない。commands には実行したコマンドと終了コードを書く（yes なら 1 つ以上）。
対象に AGENTS.md・CLAUDE.md があれば、最初に読む（子によっては自動で読まれない）。そこに書かれた指示は対象の慣習として使い、この依頼で許された操作を広げない。
"""
LINUX = sys.platform.startswith("linux")  # 子の CLI が切り離した子孫を止めるのは Linux だけ（PR_SET_CHILD_SUBREAPER。macOS には無い）
# Claude Code の子に渡す依頼文の先頭に置く 1 行。依頼文の先頭が「/名前 …」だと、claude -p はスラッシュの呼び出しとして扱い、
# 入口なら子の中で fork した指揮者が立った（設定の拒否は Skill ツールにしか効かない。2026-11-20、2.1.328、読み取り専用の子で確かめた）
CLAUDE_PROMPT_HEAD = "bb-writ の launcher が渡す依頼文（この行の下の全文が依頼）:\n\n"
READ_ONLY_CONTRACT = "対象に書く試験・検査は、`mktemp -d` で新しく作ったディレクトリ（$TMPDIR の下。無ければ /tmp の下）に対象を写して（cp -r など）、そこで動かす。決まった名前や pid だけの名前の場所に写さない（並行する子どうしでも、同じ子の別のコマンドでも、$TMPDIR も pid も同じことがある）。\n"
AUDIT_CONTRACT = ("返す前の判定（bb-writ の監査役の約束）: 上の依頼文に書かれていなくても、bb-writ のスキル `playbook-verify` の"
                  "「返す前の判定（遵守の監査）」の節を全文読み、その節が挙げる判定の項目（遵守、残る範囲の閉じ方、手順の文書を変えたときの"
                  "文の照合）をすべて判定する。依頼文が範囲や項目を狭めていても、節の項目を省かない（立て直しで前の監査の commit からの"
                  "差分に限ってよいのは、節がそう書く項目だけ。文の照合は毎回、変えた文書の全体）。子が sandbox のせいで対象の試験を"
                  "実行できず、作業者が --not-run で届けるときは、親がその commit で実行した試験の証拠（コマンド・終了コード・出力）が"
                  "作業の記録にあり、子が実行できなかったことの代わりになるか（写し方を教えれば子が動いた場面でないか）も判定する。"
                  "最後の JSON に \"deliver\": \"yes\" か \"no\" を書く（どちらにするかは、同じ節の基準に従う）。\n")
# Claude Code の書く子にだけ足す（続きでも）。Linux・WSL2 の sandbox は、書き込みを拒む場所のうち実在しないものに読み込み専用の印
# （開けないキャラクタデバイス）を置く。子の中では作業場所の直下の印が git status に出て、git add -A・git add . が 128 で落ちる。
# 名前を挙げた git add と git commit は通る。印は、子が終わった後の作業場所には残らない（時間切れで残った 0 バイトのファイルは cwd_residue）。
CLAUDE_WRITE_CONTRACT = ("Linux・WSL2 では、作業場所の直下に、自分が作っていないドットの項目（`.bashrc`・`.gitconfig`・`.claude/` の下など。"
                         "`ls -l` の先頭が `c` で、開けない）が見えることがある。Claude Code の sandbox が書き込みを拒むために置く印で、"
                         "sandbox が後で片付ける。触らず、報告に数えない。変更を足すときは `git add -A`・`git add .` を使わず、"
                         "変えたファイルの名前を挙げる（印は Git に足せないので、どちらも落ちる）。\n")


def config_dir() -> Path:
    return Path(os.environ.get("BB_WRIT_CONFIG_DIR", Path.home() / ".config" / "bb-writ"))


def state_dir() -> Path:
    return Path(os.environ.get("BB_WRIT_STATE_DIR", Path.home() / ".local" / "state" / "bb-writ"))


# 子に読ませない、ホームの下の認証情報の置き場（gh・dotenvx の鍵・専用ログインは ~/.config の下）。書く子にも効く。
# ~/.local/share 全体は拒否しない（uv・mise・pipx の道具がそこにある）
SECRET_PLACES = (".config", ".ssh", ".gnupg", ".aws", ".docker", ".kube", ".netrc", ".git-credentials", ".local/share/keyrings",
                 ".claude", ".codex", ".grok", ".azure", ".gemini", ".copilot", ".pki")
# 読み取り専用の子にだけ読ませない、パッケージの取得の認証情報（書く子は依存を取得する役で、非公開のレジストリの認証を失うので読ませる）。
# ホーム直下のドットのファイル（.npmrc・.pypirc・.bash_history など）は、読み取り専用の子にはすべて読ませない（home_dotfiles）
READ_ONLY_SECRETS = (".keychain", ".cargo/credentials.toml", ".cargo/credentials", ".gem/credentials", ".gradle/gradle.properties", ".bundle/config",
                     ".terraform.d/credentials.tfrc.json", ".pulumi/credentials.json", ".mozilla", ".local/share/com.vercel.cli")
# XML の設定は、認証情報の印があるときだけ拒む（拒んだファイルは子から空に見え、空の NuGet.Config で dotnet nuget list source が
# 終了コード 1 になった。2026-11-18、偽のホームで）
MARKED_SECRETS = (".m2/settings.xml", ".nuget/NuGet/NuGet.Config")
SECRET_MARK = re.compile(rb"<password|<privateKey|<passphrase|packageSourceCredentials|ClearTextPassword", re.I)
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


def outs_of_place(cwd: Path) -> list[Path]:
    rows = []
    try:
        with open(state_dir() / "delegations.jsonl", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except (ValueError, RecursionError):
                    continue
    except OSError:
        return []
    return [Path(r["out"]) for r in rows if isinstance(r, dict) and r.get("cwd") == str(cwd)
            and isinstance(r.get("out"), str) and os.path.isabs(r["out"]) and Path(r["out"]).is_dir()]


def secret_reads(cfg: Path) -> dict:
    """書く子の Bash に読ませない認証情報の置き場（専用ログインの置き場は ~/.config の外にも置ける）。Git の設定だけは読ませる。
    在るものだけを渡す（無い場所を渡すと、sandbox がホームに空の場所を作り、時間切れで止めると残る）。"""
    git = Path.home() / ".config" / "git"
    return {"denyRead": [str(Path.home() / p) for p in SECRET_PLACES if os.path.lexists(Path.home() / p)] + [str(cfg)],
            "allowRead": [str(git)] if os.path.lexists(git) else []}


def git_config_files() -> set[Path]:
    """読ませる Git の全体の設定のファイル: ~/.gitconfig（GIT_CONFIG_GLOBAL で付け替えたもの）と、include.path・includeIf.*.path が
    指すもの（入れ子の include も。相対の道筋は、それを書いたファイルの置き場から）。
    守らない範囲: includeIf の条件（gitdir: など）は launcher の作業ディレクトリで評価するので、対象でだけ合う条件の先の入れ子は拾わない
    （子の git がその設定を読めないだけ）。"""
    home = Path.home()
    files = {home / ".gitconfig"}
    if os.environ.get("GIT_CONFIG_GLOBAL") and os.environ["GIT_CONFIG_GLOBAL"] != os.devnull:
        files.add(Path(os.path.expanduser(os.environ["GIT_CONFIG_GLOBAL"])))
    try:  # --null は鍵と値を改行と NUL で区切る（includeIf の条件に空白があっても割れない）。--show-origin は書いたファイル
        out = subprocess.run(["git", "config", "--global", "--includes", "--show-origin", "--null", "--get-regexp", r"^include(if\..*)?\.path$"],
                             capture_output=True, text=True, timeout=GIT_TIMEOUT).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    parts = out.split("\0")
    for origin, entry in zip(parts[0::2], parts[1::2]):
        value = entry.split("\n", 1)[1] if "\n" in entry else ""
        if not origin.startswith("file:") or not value.strip():
            continue
        q = Path(os.path.expanduser(value.strip()))
        files.add(q if q.is_absolute() else Path(origin[5:]).parent / q)
    return files


def read_only_secrets() -> list[Path]:
    """読み取り専用の子に読ませない、ホームの下の認証情報の置き場のうち在るもの（XML の設定は印のあるものだけ）と、
    Git の設定の中の Git の認証情報（credential store の XDG の置き場）。
    守らない範囲: 印を探すのは先頭の 1 MiB だけで、UTF-16 の XML と、印の一覧に無い書き方（NuGet の apikeys など）は拒まない。
    印を探すファイルを FIFO にすると、launcher はそこで止まる。"""
    home = Path.home()
    out = [home / p for p in SECRET_PLACES + READ_ONLY_SECRETS + (".config/git/credentials",) if os.path.lexists(home / p)]
    for p in MARKED_SECRETS:
        try:
            with open(home / p, "rb") as f:
                if SECRET_MARK.search(f.read(1 << 20)):
                    out.append(home / p)
        except OSError:
            continue
    return out


def home_dotfiles() -> list[Path]:
    """ホーム直下の、ディレクトリでないドットの場所（.npmrc・.pypirc・.bash_history・.env.keys・.claude.json など。一覧は完成しない）。
    Git の設定のファイルと、行き先がホームの外の symlink（~/.bash_history -> /dev/null、~/.hosts -> /etc/hosts など。拒むと行き先を拒む
    ことになる）は除く。読み取り専用の Codex・Grok の子に 1 つずつ拒む（Claude Code の子はホームを丸ごと拒む）。symlink は行き先を拒むので、
    対象の中のファイルを指すもの（dotfiles のリポジトリを指す ~/.bashrc など）は、対象の中のそのファイルも子から読めなくなる（守らない）。
    行き先がホームの外のもの（WSL2 の /mnt/c の下の .npmrc など）は拒まない（ホームの外は守らない範囲）。"""
    keep = git_config_files()
    keep |= {Path(os.path.realpath(f)) for f in keep}  # symlink の ~/.gitconfig の行き先も Git の設定（Codex・Grok の子の git が読む）
    try:
        entries = sorted(Path.home().iterdir())
    except OSError:
        return []
    home = Path(os.path.realpath(Path.home()))
    return [e for e in entries if e.name.startswith(".") and not e.is_dir() and e not in keep
            and (not e.is_symlink() or home in Path(os.path.realpath(e)).parents)]


def claude_reads(cwd: Path, git_dir: Path | None, env: dict, audit: bool) -> dict:
    """読み取り専用の Claude Code の子の Bash の読み。ホームを丸ごと拒み、道具の置き場と対象を戻し、戻した中の認証情報を拒む（#38）。
    - 戻す: ドットで始まるディレクトリのうち認証情報の一覧に無いもの、PATH のうちホームの下のディレクトリ（bin はその親。
      親がホームなら bin だけ）、Git の設定のファイル（通常のファイルだけ）と ~/.config/git、対象と Git の共通ディレクトリ、監査役なら
      作業の記録・その場所で立てた子の out・bb-writ のスキル。対象と記録のほかは、道筋が symlink も .. も通らないもの（realpath と
      同じ綴り）だけを戻し、ホームそのもの・認証情報の置き場・その中は戻さない。
    - 拒む: ホーム（symlink の道筋なら実の道筋も）、戻した場所の中に起動の時点で在る認証情報の置き場、symlink を通る認証情報の置き場の
      起動の時点で在る行き先（システムの道筋でも）、bb-writ の設定と状態の置き場（戻した対象・記録は、その中でも読める）。
    ファイル単位の拒否は、子の実行中の置き換え（rename）で外れるので、ホームはディレクトリごと拒む。
    守らない範囲: 戻したドットのディレクトリの中の、一覧に無い認証情報（認証情報の置き場の中から、そこへ張った symlink の行き先も）。
    戻した中で 1 つずつ拒んだファイルの置き換え。起動の後にできた行き先。ホームの外（WSL2 の /mnt/c/Users の下など）。
    PATH の bin の親が別のリポジトリなら、その中の .env など。Git の設定の include が指す通常のファイル。認証情報の置き場そのものの
    symlink が対象を指すと、同じ道筋が許可と拒否に並ぶ。symlink を通る道具・設定と、HOME が symlink の道筋のときは戻らず、子の試験は
    not-run になる。ホーム直下のドットのファイルを読む道具（~/.tool-versions など）は落ちる。
    """
    home = Path.home()
    real_home = Path(os.path.realpath(home))
    secrets = read_only_secrets()
    real_secrets = {Path(os.path.realpath(p)) for p in secrets}
    git_xdg = home / ".config" / "git"
    own = [config_dir(), state_dir()]
    real_own = {Path(os.path.realpath(o)) for o in own}

    def safe(a: Path) -> bool:
        r = Path(os.path.realpath(a))
        if str(r) != str(a) or r in real_secrets or any(o == r or o in r.parents for o in real_own):
            return False
        return a == git_xdg or not any(q in r.parents for q in real_secrets)

    dotdirs = [e for e in sorted(home.iterdir()) if e.name.startswith(".") and e.is_dir()]
    tools = []
    for d in env.get("PATH", "").split(os.pathsep):
        q = Path(d)
        if q.is_absolute() and home in q.parents and q.is_dir():
            tools.append(q.parent if q.name == "bin" and q.parent != home else q)
    gits = [f for f in git_config_files() if f.is_file()]
    places = [a for a in [*dotdirs, *tools, *gits, git_xdg] if os.path.lexists(a) and safe(a)]
    extra = [work_record(cwd), Path(__file__).resolve().parents[1] / "skills", *outs_of_place(cwd)] if audit else []
    links = [r for p in secrets if str(r := Path(os.path.realpath(p))) != str(p) and r.exists()]
    allow = [*places, cwd, *([git_dir] if git_dir else []), *extra]
    inner = [p for p in secrets if p.exists() and any(a in p.parents for a in allow)]
    deny = [home, *([real_home] if real_home != home else []), *inner, *links, *own, *(Path(os.path.realpath(o)) for o in own)]
    return {"denyRead": list(dict.fromkeys(str(p) for p in deny)), "allowRead": list(dict.fromkeys(str(p) for p in allow))}


def home_entries(skip: list[Path]) -> tuple[set[str], set[str], set[str]]:
    """ホームの 2 段目までの (すべての場所, 空のファイルと空のディレクトリ, 読めない場所)。skip とその下は見ない。
    読めない場所を黙って飛ばすと、子が権限を落とした場所の残りを見逃すので、呼ぶ側が前後で比べる。
    子の CLI の sandbox は、守る対象のうち無いものを空のファイルで作り、残すことがある（.bash_profile が残ると、ログインシェルが .bashrc を読まなくなった。2.1.302）。"""
    every, empty, unreadable = set(), set(), set()

    def scan(d: Path, depth: int):
        try:
            entries = list(os.scandir(d))
        except PermissionError:
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
                    with os.scandir(e.path) as it:
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


def cli_place(p: Path) -> bool:
    """子の CLI（Claude Code）が、CLAUDE_CONFIG_DIR に関わらずホームに作る自分の置き場の、中身の空のディレクトリか。
    ~/.claude の直下（名前は問わない。2.1.309 の ssh、2.1.327 の bridge-spawn・child-logs・daemon・file-history。使った機能で変わり、
    名前の一覧は完成しない）と ~/.config/anthropic。子のコマンドは本物の ~/.claude に書けない（Claude Code の子は sandbox の中だけ、
    Codex・Grok の子は拒否。2026-11-18）ので、ここの空のディレクトリは CLI 自身のもので、.bash_profile のようにほかの道具が読む場所でもない。
    数えないだけで消さない（次の起動でも使われ、並行する別の委任の子と競う）。どのホストの委任でも同じ（並行する Claude Code の子が作ったものを見る）。
    空のファイルと、空の ~/.claude そのものは数える。symlink は home_entries がもともと見ず、~/.claude が symlink ならその下も見ない。
    HOME が空のときは、ここ（Python）はホームを / と読み、子の CLI と一致しない（Node は空と読む）。"""
    home = Path.home()
    if not p.is_dir():
        return False
    return p.parent == home / ".claude" or p == home / ".config" / "anthropic"


# 書く子が書けた作業場所で動かす git の頭。git status・ls-files が設定から走らせる core.fsmonitor を外す（コマンド行の -c は、
# include・config.worktree の値より強く、中で起こす git にも伝わる。git 2.43 で測った。#64）。hooks（post-index-change）は
# status に --no-optional-locks を、入れ子の設定は --ignore-submodules（all・dirty）を、呼ぶ側が付ける。
# 守らない範囲: filter（どこで定めたものも外さない。利用者の設定が作業場所の中のスクリプトを指すもの、子が書ける include の先・
# config.worktree で定めたものも走る。LFS などの required の filter を外すと status が壊れるため。子が共通の config・config.worktree を
# 変えたときと、Git でなかった対象に .git を作ったときは、照合が止める）、Git でなかった対象を .git の無い形のリポジトリにされる
# こと（照合は .git の有無だけを見る）、統合の merge（元の checkout で利用者の設定の hooks・filter が走る）、promisor の remote の取りに行き（利用者の設定が
# include で作業場所の中のファイルを読み、子がそこに remote を定めると、参照の git も取りに行きの命令を走らせうる）、
# 前から .git が在る Git でない対象（壊れた .git の中に子がリポジトリを作っても、照合は止めない）、git 2.43 で測っていない
# 命令の出どころ、利用者・指揮者が後で動かす素の git。
QUIET_GIT = ["git", "-c", "core.fsmonitor=false"]


def cwd_residue(cwd: Path, before: set[str]) -> list[str]:
    """書く子の起動で対象の直下に新しくでき、中身が空のまま（空のファイルと空のディレクトリだけ。入れ子を含む）で、Git が追跡していない場所。
    Claude Code の sandbox は、守る対象のうち無いもの（.bashrc・.gitconfig・.claude/ の下など）を対象に空で作り、子の CLI が
    正常に終わると片付けるが、時間切れで止めると残った（2.1.310）。0 バイトのファイルが残ると task cleanup が未保存の変更とみなし、
    次の書き手の git add -A で commit に入りうる。黙って消さない（子が意図して作った空のファイルかもしれない）。
    Claude Code の空の置き場だけの .claude（claude_staging_only）は数えない（同じ場所の別のセッションも使い、消さない。#65）。
    before は起動の前の直下の場所で、その置き場だけの .claude を入れない（新しくできたものと同じに見る。前の子が残した .claude の下に、
    後の子が時間切れで残した 0 バイトの settings.json などを見落とした）。
    守らない範囲: 前からあるディレクトリの下、index と照らすのは対象が作業ツリーの根（.git がある）のときだけ、Git の設定・環境を
    書き換えた子や親（設定の FIFO・GIT_DIR）、pathspec の記号を含む名前、Python の再帰の上限を超える入れ子、index にある場所を
    含む新しいディレクトリの下の追跡外のもの（index とは直下の名前で照らす）。git の時間切れと、
    読めない場所での failed は試験で見ない。"""
    def bare(p: Path) -> bool:
        if p.is_symlink():
            return False
        if p.is_dir():
            return all(bare(c) for c in p.iterdir())
        return p.is_file() and p.stat().st_size == 0
    new = sorted(str(p) for p in cwd.iterdir() if str(p) not in before)
    left = [p for p in new if bare(Path(p)) and not claude_staging_only(p)]
    if left and (cwd / ".git").exists():
        out = subprocess.run([*QUIET_GIT, "-C", str(cwd), "ls-files", "-z", "--", *[Path(p).name for p in left]], capture_output=True, text=True, check=True, timeout=GIT_TIMEOUT)
        tracked = {cwd / n.split("/")[0] for n in out.stdout.split("\0") if n}
        left = [p for p in left if Path(p) not in tracked]
    return left


def grok_ro_writable(home: Path) -> list[Path]:
    """Grok の read-only の sandbox でも書ける場所（公式文書 18-sandbox と、実物の sessions/sandbox-events.jsonl の read_write_paths）。"""
    roots = [Path("/tmp"), Path("/var/tmp"), Path(tempfile.gettempdir()), home]  # gettempdir は macOS の一時ディレクトリ
    return sorted({p.resolve() for p in roots})


GIT_TIMEOUT = float(os.environ.get("BB_WRIT_GIT_TIMEOUT") or 60)  # 子が管理ファイルを FIFO にすると、git はそれを開いたまま止まる（環境変数は試験だけが使う）


def grok_git_dir(cwd: Path) -> tuple[Path | None, str | None]:
    g = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--git-common-dir"], capture_output=True, text=True, errors="surrogateescape", timeout=GIT_TIMEOUT)
    if g.returncode == 0:
        return (cwd / g.stdout.strip()).resolve(), None
    if "not a git repository" in g.stderr:
        return None, None
    return None, f"Git の共通ディレクトリを取れない（git rev-parse: {g.stderr.strip()[:200]}）"


def worktree_dirs(cwd: Path) -> tuple[Path | None, Path | None, str | None]:
    g = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"],
                       capture_output=True, text=True, errors="surrogateescape", timeout=GIT_TIMEOUT)
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
    if stat.S_ISLNK(st.st_mode):
        try:
            fd = os.open(f, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            return f"symlink: {os.readlink(f)!r}（開けない: {e.strerror}）", b"", False
        try:
            mode = os.fstat(fd).st_mode
            return f"symlink: {os.readlink(f)!r} " + (f"{stat.filemode(mode)} {digest_of(fd)}" if stat.S_ISREG(mode) else f"（{stat.filemode(mode)}）"), b"", False
        finally:
            os.close(fd)
    fd = os.open(f, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode):
            return f"（通常のファイルでない: {stat.filemode(mode)}）", b"", False
        head = []
        return f"{stat.filemode(mode)} {digest_of(fd, keep, head)}", b"".join(head), True  # 権限も（hook に実行権限を足すと git が動かす）
    finally:
        os.close(fd)


def digest_of(fd: int, keep: int = 0, head: list | None = None) -> str:
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
    if not regular:
        return digest
    return head.decode("utf-8", "surrogateescape") if len(head) <= 4096 else f"（4 KiB を超える。{digest}）"


def git_snapshot(own: Path, common: Path, cwd: Path) -> dict:
    """書く子の前後で比べる Git の管理情報。子の書いた .git ファイルを読まないよう、共通ディレクトリを明示して読む。"""
    for f in (common / "config", common / "packed-refs"):
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
    ref は前後の値と戻すコマンドを添える（自動では戻さない。子の実行中に親や別のタスクが正当に動かしたかもしれない）。
    リモート追跡の ref（refs/remotes/）は数えない。git fetch が書く場所で、共有の .git では、子の実行中に親が流した fetch でも動く
    （指揮者の fetch を、書く子の失敗に数えた）。work.py も launcher も、統合・CI の判定にリモート追跡の ref を使わない。"""
    head = before["head"].strip()
    mine = head[len("ref: "):] if head.startswith("ref: ") else None
    out = []
    for r in sorted(set(before["refs"]) | set(after["refs"])):
        old, new = before["refs"].get(r), after["refs"].get(r)
        if r != mine and old != new and not r.startswith("refs/remotes/"):
            undo = f"git --git-dir {common} update-ref {r} {old}" if old else f"git --git-dir {common} update-ref -d {r}"
            out.append(f"{r}: {old or '(無し)'} → {new or '(削除)'}（戻すなら: {undo}）")
    out += sorted(f for f in set(before["files"]) | set(after["files"]) if before["files"].get(f) != after["files"].get(f))
    if after["head"] != before["head"]:
        out.append(f"HEAD: {before['head']!r} → {after['head']!r}")
    return out


def grok_session_dir(old: Path, session: str) -> Path | None:
    if not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", session) or (old / "sessions").is_symlink():
        return None
    found = [d for d in old.glob(f"sessions/*/{session}") if d.is_dir() and not d.is_symlink() and not d.parent.is_symlink()]
    return found[0] if len(found) == 1 else None


def copy_grok_session(src: Path, old: Path, new: Path) -> None:
    """続きの新しい home に、前の home からその session の記録だけを写す。前の home の設定・hooks・trusted_folders.toml は
    子の sandbox から書けたので、続きの起動に読ませない（新しい home と、写した記録で -r が続いた。2026-10-25、1.0.70）。
    写すのは通常のファイルとディレクトリだけで、symlink は写さない（子が置いた symlink を、sandbox の外の Grok が開かないように）。
    記録の中身も子が書けた。続きの sandbox は新しい home の sandbox.toml の独自の profile に拠り、Grok は独自の profile を
    適用できなければ起動を拒む（公式文書 18-sandbox）。記録の sandbox_profile を書き換えると Grok は再開を拒み、続きは記録の grok_home が指す
    前の home を開かなかった（2026-10-25、1.0.70）。記録のほかの中身が -r に何をさせるかは確かめていない。"""
    for top, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(top, d))]
        to = new / Path(top).relative_to(old)
        to.mkdir(parents=True, exist_ok=True)
        for f in files:
            if not os.path.islink(os.path.join(top, f)) and os.path.isfile(os.path.join(top, f)):
                shutil.copyfile(os.path.join(top, f), to / f)


def grok_home(login: Path, extends: str, read_write: list[Path], deny: list[str] | None = None) -> tuple[Path, str]:
    """起動ごとの GROK_HOME を作り、(場所, profile 名) を返す。Grok の sandbox は read-only でも GROK_HOME に書けるので、
    sandbox.toml・config.toml・hooks・trusted_folders.toml を後の子と共有しない。共有するのはログインの auth.json（symlink）だけ。
    子が置き換えうるので、home の auth.json を専用ログインへ書き戻すことはしない。
    組み込みの profile は適用できないと警告して強制せずに続くが、独自の profile は起動を拒む（公式文書 18-sandbox）ので、読み取り専用も独自の profile にする。"""
    base = state_dir() / "grok-homes"
    base.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), dir=base))
    held, lost = hold_place(home)
    if held is not None:
        PLACES_HELD.append(held)
    elif lost != "unsupported":  # 排他を取れない環境では、片付けも消さないので、持たずに進む
        raise OSError(f"起動ごとの home {home} の排他を取れない（{lost}）")
    (home / "auth.json").symlink_to(login / "auth.json")
    rw = [str(p.resolve()) for p in read_write]
    toml = f'[profiles.bbwrit]\nextends = "{extends}"\nread_write = {json.dumps(rw)}\n'
    if deny is not None:  # Linux では bubblewrap の bind で読みと書きを拒む（公式文書 18-sandbox）。無い場所を書くと、ホームに空のファイルを作って残した
        toml += f"deny = {json.dumps(deny, ensure_ascii=False)}\n"
    (home / "sandbox.toml").write_text(toml, encoding="utf-8")
    return home, "bbwrit"


CODEX_PROFILE = "bbwrit-read-only"


def secret_denies(keep: list[Path], targets: list[Path] | None = None) -> list[str]:
    """読み取り専用の Codex・Grok の子に読ませない場所（targets の既定は read_only_secrets と home_dotfiles と専用ログイン。
    Claude Code の子はホームを丸ごと拒むので、この一覧を使わない）。
    -s read-only のころは、子が ~/.config の下を読めた（2026-10-23 の実測）。bwrap は、無い場所・symlink の場所・拒んだ場所の
    下の場所を書くと起動せず（~/.aws が /mnt/c を指す、~/.config の下の専用ログイン）、拒んだ ~/.codex の下に read を書いても
    codex 本体を実行できなかった（0.163.0、Linux）。そこで、あるものだけを symlink を解いた行き先で書き、入れ子は書かず、
    keep（codex 本体・Git の設定・対象）を含む置き場は、起動の時点の中身を 1 つずつ見て、keep を含まないものだけを拒む（再帰）。"""
    # 行き先と、symlink の節点（親を解き、最後の名前は解かない形。比べる置き場は解いた形なので、ホームが symlink の下でも
    # 節点を含むと分かるように。macOS の一時ディレクトリ /var は /private/var を指す）
    keep = {Path(os.path.realpath(k)) for k in keep} | {Path(os.path.realpath(Path(k).parent)) / Path(k).name for k in keep}
    places, seen = set(), set()

    def add(d: Path):
        d = Path(os.path.realpath(d))
        if any(k == d for k in keep) or d in seen:
            return
        seen.add(d)
        if any(d in k.parents for k in keep):
            for c in d.iterdir():
                if os.path.exists(c):
                    add(c)
        else:
            places.add(d)

    for d in (targets if targets is not None else read_only_secrets() + home_dotfiles()) + [config_dir()]:
        if os.path.exists(d):
            add(d)
    return sorted(str(d) for d in places if not any(o in d.parents for o in places))


def codex_read_only(scratch: Path, cwd: Path) -> tuple[list[str], list[str]]:
    """読み取り専用の Codex の権限と、拒む場所。read-only を継ぎ、書けるのは起動ごとの一時ディレクトリ（$TMPDIR）だけ。
    -s read-only では mktemp -d も失敗し、試験を動かせない審査役が読んだだけの判定を返した（0.163.0）。
    -s（sandbox_mode）は default_permissions より優先されるので渡さない。ネットワークは read-only の既定のまま出られない。"""
    git_dir, _ = grok_git_dir(cwd)
    # 残すものは、実機で要ると分かったものだけ: codex 本体とそのパッケージ（本体の 2 つ上。standalone の版で、拒んだ ~/.codex の中の本体を、sandbox の補助が
    # 実行できなかった）、Git の設定、対象とその共通ディレクトリ。PATH の道具などを足すと、そのたびに別の置き場を開けた（dotfiles など）。
    # 道具が名前を挙げた置き場の下にあれば拒まれ、子の試験は動かず not-run になる（読んだだけの報告として扱われる）。
    # 本体のパッケージが置き場の根・ホームそのもの（~/.codex/bin/codex の 2 つ上は ~/.codex）か、専用ログインの中なら残さない。
    # Git の設定が置き場の根そのもの（~/.config/git が ~/.ssh を指す symlink）なら残さない
    roots = {Path(os.path.realpath(d)) for d in [Path.home(), config_dir(), *(Path.home() / p for p in SECRET_PLACES)]}
    login = Path(os.path.realpath(config_dir()))
    binary = Path(os.path.realpath(shutil.which("codex") or "/"))
    package = binary.parent.parent
    keep = [binary, *([package] if package not in roots and login not in package.parents else []),
            *([gitconf] if (g := Path(os.path.realpath(gitconf := Path.home() / ".config" / "git"))) not in roots and login not in g.parents else []),
            cwd, *([git_dir] if git_dir else [])]
    denies = secret_denies(keep)
    scratch.mkdir(parents=True, exist_ok=True)
    rules = {":tmpdir": "write", **{d: "deny" for d in denies}}
    # TOML の基本文字列（JSON と同じ逃がし方。BMP の外の文字は、TOML に無いサロゲート対の \\u にせず、そのまま書く）
    table = ", ".join(f"{json.dumps(k, ensure_ascii=False)}={json.dumps(v)}" for k, v in rules.items())
    return (["-c", f'default_permissions="{CODEX_PROFILE}"', "-c", f'permissions.{CODEX_PROFILE}.extends=":read-only"',
             "-c", f"permissions.{CODEX_PROFILE}.filesystem={{{table}}}"], denies)


def codex_breach(turns: list, denies: list[str]) -> str | None:
    """読み取り専用の Codex の実効の権限（記録の turn_context。turn ごと）が、渡した profile と違えばその理由。
    読み込まれたどの設定の sandbox_mode も default_permissions より優先され（対象の .codex/config.toml・専用ログインの config.toml）、
    同じ profile に項目を足すこともできる。広ければ読み取り専用でなく、一時ディレクトリに書けなければ試験を動かせず、
    渡した deny が無いか、その下に read・write が足されていれば認証情報を読める（より狭い指定が勝つ。公式文書）。
    例外は Codex 自身が足す、専用ログインの tmp/arg0-<OS>/codex-arg0* そのものの read だけ（0.163.0 の Linux の実物の記録は arg0-linux。
    記録の場所は、解く前の形と解いた後の形の両方がこの形のときだけ例外にする。macOS の一時ディレクトリ /var/folders は /private/var への
    symlink で、解いた形とだけ比べると試験の Codex の子がいつも failed になり、どちらか一方で許すと symlink で auth.json を指させた）。
    実効の権限を記録から読めなければ、どれも確かめられないので理由にする。"""
    if not turns or any(t["writable"] is None or t["network"] is None for t in turns):
        return "Codex の実効の権限を記録（turn_context の permission_profile）から確かめられない（記録が無い、読めない行がある、形が違う）"
    tmp = (":tmpdir",)
    homes = {str(config_dir().resolve() / "codex-home" / "tmp"), str(Path(os.path.abspath(config_dir())) / "codex-home" / "tmp")}
    arg0 = re.compile(r"arg0-[^/]+/codex-arg0[^/]*")  # 専用ログインの tmp/ の下の、この形の 1 ファイルだけ
    for t in turns:
        wide = [w for w in t["writable"] if w not in tmp]
        if wide or t["network"] != "restricted":
            return f"Codex の実効の権限が、読み取り専用の profile より広い（書ける場所: {wide[:5]}、ネットワーク: {t['network'][:50]}）"
        if not any(w in tmp for w in t["writable"]):
            return "Codex の実効の権限で、一時ディレクトリに書けない（設定の sandbox_mode が profile より優先されたかもしれない）"
        recorded = {w for acc, w in t["rules"] if acc == "deny"}
        missing = [d for d in denies if d not in recorded]
        opened = [w for acc, w in t["rules"] if acc != "deny" and not w.startswith(":")
                  and any(r == d or r.startswith(d + "/") for d in denies for r in (w, os.path.realpath(w)))
                  and not (acc == "read" and all(any(r.startswith(h + "/") and arg0.fullmatch(r[len(h) + 1:]) for h in homes)
                                                for r in (w, os.path.realpath(w))))]
        if missing or opened:
            return f"Codex の実効の権限で、認証情報の置き場を拒めていない（記録に無い deny: {missing[:5]}、その下で開いた場所: {opened[:5]}）"
    return None


def is_bb_writ_copy(p: Path) -> bool:
    return (p / "skills").is_dir()


def codex_stale_copy(home: Path) -> tuple[str, str, str] | None:
    """専用ログインの Codex に入った bb-writ の写しが、この launcher の skills/・scripts/ と中身で違えば
    (写しの marketplace, 違いの説明, 手で入れ直す案内)。写しが無ければ None。
    Claude Code の子には --plugin-dir でこの bb-writ を渡すが、Codex には起動ごとに置き場を渡す引数が無く（0.171.0 の --help）、子は写しのスキルと
    補助スクリプトを読む（読み取り専用の子は、スキルの一覧は写しから見えるが、写しの本文は launcher が専用ログインの置き場の読みを拒むので読めなかった（2026-11-11）。バージョンを上げる前の変更は届かず、入れ直すまでの間に古い写しで 88 回中 8 回動いた。-c で plugins の enabled を
    切ってもスキルは見えたままだった。0.166.0）。バージョンの番号でなく中身で比べる（バージョンを上げていない変更を見逃すため）。
    写しにだけあるファイル（古いバージョンの残りも）・写しの置き場がどれか 1 つでも違えば止める。違うときに launcher が自分で入れ直すかは
    codex_refresh が決める（案内は、入れ直さないときに手で入れ直すコマンド）。守らない範囲: 読めないファイルは例外のまま
    failed になり、symlink の置き場の中は数えない（rglob がたどらない）。"""
    def tree(base: Path) -> dict:
        return {p.relative_to(base).as_posix(): p.read_bytes() for d in ("skills", "scripts") if (base / d).is_dir()
                for p in (base / d).rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    root = Path(__file__).resolve().parents[1]
    mine = tree(root)
    for copy in sorted((home / "plugins" / "cache").glob("*/bb-writ/*")):
        if not is_bb_writ_copy(copy):
            continue
        theirs = tree(copy)
        diff = sorted(k for k in mine.keys() | theirs.keys() if mine.get(k) != theirs.get(k))
        if diff:
            mp = copy.parent.parent.name
            return (mp, f"専用ログインの Codex の bb-writ の写し {copy} が、この bb-writ（{root}）と違う（{', '.join(diff[:3])}"
                        f"{' など' if len(diff) > 3 else ''}）。子は古い写しのスキルで動くので起動しない",
                    f"入れ直す: CODEX_HOME={home} codex plugin add bb-writ@{mp}"
                    f"（GitHub から入れた marketplace なら、先に CODEX_HOME={home} codex plugin marketplace upgrade {mp}。"
                    f"手元のディレクトリで登録した marketplace では upgrade は要らず、エラーになる）。launcher が自分で入れ直すのは、"
                    f"専用ログインの Codex で、その marketplace が手元のディレクトリで登録してあり、その置き場がこの bb-writ のときだけ。"
                    f"写しは marketplace に登録した置き場から作られるので、この bb-writ がその置き場と別の場所（作業場所など）にあれば、"
                    f"入れ直しても一致しない（統合してから入れ直す）")
    return None


CODEX_PLUGIN_TIMEOUT = float(os.environ.get("BB_WRIT_CODEX_PLUGIN_TIMEOUT") or 60)  # 環境変数は試験だけが使う


def codex_plugin(home: Path, *args: str) -> tuple[int | None, str, str, str]:
    """CODEX_HOME=<専用ログイン> codex plugin <args> を 1 回実行する。(終了コード, 標準出力, 標準エラー, 結果の文)。
    上限で止めた・起動できなかったときの終了コードは None。標準入力は閉じる。上限で止めるのは、起こした codex のプロセスだけ。"""
    try:
        p = subprocess.run(["codex", "plugin", *args], env={**os.environ, "CODEX_HOME": str(home)}, stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=CODEX_PLUGIN_TIMEOUT)
        code, out, err, result = p.returncode, p.stdout, p.stderr, f"終了コード {p.returncode}"
    except subprocess.TimeoutExpired as e:
        code, out, err, result = None, e.stdout or b"", e.stderr or b"", f"{CODEX_PLUGIN_TIMEOUT:g} 秒で終わらなかったので止めた"
    except OSError as e:
        code, out, err, result = None, b"", b"", f"起動できなかった（{e!r}）"
    return code, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), result


def codex_registration(home: Path, mp: str) -> tuple[bool, str]:
    """専用ログインの Codex に、marketplace mp の登録を聞く（codex plugin marketplace list --json。0.171.0 の形）。
    (この launcher が入れ直してよいか, 理由に足す句)。入れ直してよいのは、marketplaces の中の name が mp と等しい項目の
    marketplaceSource が、sourceType = "local" で、source が文字列で、絶対パスで、道筋を解くと launcher 自身の置き場と等しいとき
    （親・子・前方一致は等しくない。symlink を通る綴りでも、解いて同じなら等しい）。答えを使うのは、終了コードが 0 で、標準出力を
    この形で読めたときだけ。それ以外（起動できない・上限で止めた・0 以外で終わった・JSON でない・形が違う）は、登録を確かめられ
    なかったものとして、入れ直さない。同じ名前の項目が 2 つ並べば、最初の 1 つで決める（専用ログインで信頼したプロジェクトの
    .codex/config.toml の marketplace も並び、bb-writ が 2 つ並んだ。0.171.0）。"""
    code, out, err, result = codex_plugin(home, "marketplace", "list", "--json")
    try:
        if code != 0:
            raise ValueError(result)
        items = json.loads(out)["marketplaces"]
        if not isinstance(items, list) or not all(isinstance(x, dict) for x in items):
            raise TypeError("marketplaces")
        entry = next((x for x in items if x.get("name") == mp), None)
        if entry is not None:
            source = entry["marketplaceSource"]
            if not isinstance(source, dict) or not isinstance(source.get("sourceType"), str) or not isinstance(source.get("source"), str):
                raise TypeError("marketplaceSource")
    except (ValueError, KeyError, TypeError):
        tail = (out + err).strip()[-300:]
        return False, (f"launcher は marketplace の登録を確かめられなかったので、入れ直さない（CODEX_HOME={home} codex plugin marketplace "
                       f"list --json が {result}。出力の末尾: {tail or 'なし'}）")
    if entry is None:
        return False, f"専用ログインの Codex の答えに、marketplace {mp} の登録が無い"
    kind, where = source["sourceType"], source["source"]
    try:
        ours = kind == "local" and os.path.isabs(where) and Path(where).resolve() == Path(__file__).resolve().parents[1]
    except (OSError, RuntimeError, ValueError):  # 解けない道筋（symlink の輪・NUL）
        ours = False
    return ours, f"専用ログインの Codex が答えた marketplace {mp} の登録は sourceType = {json.dumps(kind, ensure_ascii=False)}、source = {json.dumps(where, ensure_ascii=False)}"


def codex_refresh(a, home: Path) -> str | None:
    """写しを比べ、違えば、入れ直せるときは入れ直してから比べ直す。同じ・写しが無ければ None（起動する）。入れ直した後は、どの写しも
    違わず、その marketplace の写しが在るときだけ None。それ以外は理由（起動しない）。
    順序: 専用ログインのディレクトリそのものの flock を取る（設定・状態の置き場に新しいファイルを置かない。取れなければ排他なしで比べ、
    違っても入れ直さない）→ 比べる → 違えば、専用ログインの Codex に登録を聞き（codex_registration）、その marketplace が手元の
    ディレクトリで、その置き場が launcher 自身の置き場のときだけ、codex plugin add bb-writ@<marketplace> を 1 回（remove はしない。
    remove は入れ直すまでプラグインを無くし、add だけで写しは置き換わる。marketplace upgrade もしない）→ 比べ直す → 排他を放す
    （どの道筋でも、子を起動する前）。
    比べから入れ直しまでを 1 つの排他の下に置くのは、ほかの launcher の add の途中の写しを読まないためと、同時の 2 つの add の片方が
    失敗しうるため（Codex 自身は flock を取らない）。
    add を実行したら a.codex_plugin_add に marketplace と終了コード（上限で止めた・起動できなかったら None）を載せる。add の成否は
    終了コードでなく、比べ直しで決める。入れ直しても違うときの理由には手の案内を付けず（同じコマンドを勧めない）、入れ直しの後に
    写しが無いときは付ける（後の起動は、写しの無い子を起動するため）。
    守らない範囲:
    - 動いている Codex の子が、入れ直しの改名の間に写しを読むと、写しが無いものとして動く（launcher は子がいても入れ直す）。
    - 排他は launcher どうしだけ。手の codex plugin の操作と同時の比べ・入れ直し、flock が排他にならないファイルシステムは守らない。
    - 比べた後に、写しや launcher の置き場の中身が替わることは見ない。
    - 条件は Codex の答え（codex plugin marketplace list --json の marketplaces の name・marketplaceSource の sourceType・source）で
      決める。その形で答えないバージョンと、一覧が失敗する状態では入れ直さない。答えは作業ディレクトリにも拠り（信頼したプロジェクトの
      marketplace も並ぶ）、同じ名前の項目が 2 つ並べば最初の 1 つで決める。add も同じ作業ディレクトリで実行する。
    - 入れ直しは、1 回の起動で 1 つの marketplace に 1 回だけ。入れ直しの後に写しが無いと止めるのは、add を実行したその起動だけで、
      後の起動は写しの無い子として起動する（add が改名の所で KILL されると、写しは無いまま残る）。
    - 入れ直しや排他の待ちの間に launcher を止めると、記録は残らず、動いている add は止めない。上限（60 秒）で止めるのは launcher が
      起こした codex のプロセスだけで、一覧と add のそれぞれに掛かる。排他の待ちに上限は無い。
    - macOS は偽の CLI の試験だけ。
    """
    fd, lost = None, None
    try:
        fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as e:
        lost = e
    try:
        stale = codex_stale_copy(home)
        if stale is None:
            return None
        mp, about, manual = stale
        if lost is not None:
            return f"{about}。{manual}。専用ログインの置き場 {home} の排他を取れないので、launcher は入れ直さない（{lost!r}）"
        ours, said = codex_registration(home, mp)
        if not ours:
            return f"{about}。{manual}。{said}"
        a.codex_plugin_add = {"marketplace": mp, "exit": None}
        code, out, err, result = codex_plugin(home, "add", f"bb-writ@{mp}")
        a.codex_plugin_add["exit"] = code
        stale, place = codex_stale_copy(home), home / "plugins" / "cache" / mp / "bb-writ"
        tail = (out + err).strip()[-300:]  # 失敗の文は標準エラーに出るので、後につなぐ（末尾に残る）
        ran = f"launcher は CODEX_HOME={home} codex plugin add bb-writ@{mp} を実行したが（{result}）"
        if stale is not None:
            return f"{stale[1]}。{ran}、写しはまだ違う（add の出力の末尾: {tail or 'なし'}）"
        if not any(is_bb_writ_copy(v) for v in place.glob("*")):
            return (f"{ran}、入れ直しの後に写しが無い（{place}）（add の出力の末尾: {tail or 'なし'}）。"
                    f"手で入れ直す: CODEX_HOME={home} codex plugin add bb-writ@{mp}")
        return None
    finally:
        if fd is not None:
            os.close(fd)


LOGIN_CHECK_TIMEOUT = float(os.environ.get("BB_WRIT_LOGIN_CHECK_TIMEOUT") or 60)  # 環境変数は試験だけが使う
LOGIN_CHECK_PROMPT = "ok とだけ返して"
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# SDK の debug の記録の、ヘッダの行（Claude Code 2.1.330 の形: "anthropic-organization-id": "<uuid>",）。名前は後ろで合わせ、anthropic-organization-id" で終わる名前の行の値を読む
LOGIN_ORG_HEADER = re.compile(rf'anthropic-organization-id"\s*:\s*"({UUID})"', re.I)
CLAUDE_LOGIN_NOTE = ("専用ログインのトークン（CLAUDE_CODE_OAUTH_TOKEN）は、launcher を呼んだ環境の Claude Code のログインとは別の組織のもの。"
                     "子は、そのトークンのアカウントの利用枠で動く。Claude のアカウントを切り替えたのなら、`claude setup-token` から"
                     "トークンを作り直す（README の「任意機能の準備」の「Claude Code の専用ログイン」）。わざと別のアカウントにしているなら、そのままでよい。")
CLAUDE_LOGIN_UNREAD_NOTE = ("専用ログインのトークンの組織を、問い合わせの出力から読めなかった（問い合わせは終了コード 0 で終わった）。Claude Code のバージョンが変わって、"
                            "launcher の読み方が合わなくなった見込みがある。この確かめ（トークンとログインの組織の比べ）は、いま働いていない。"
                            "Claude のアカウントを切り替えたときは、`claude setup-token` からトークンを作り直す（README の「任意機能の準備」の"
                            "「Claude Code の専用ログイン」）。")


def run_bounded(cmd: list[str], env: dict, cwd: Path) -> tuple[int | None, bytes, bytes, str]:
    """cmd を新しいセッションで 1 回実行し、標準出力と標準エラーをメモリの中だけで読む。(終了コード, 標準出力, 標準エラー, 結果の文)。
    標準入力は閉じる。LOGIN_CHECK_TIMEOUT 秒で終わらなければ、プロセスグループごと KILL し（dotenvx だけを止めると、下の claude が残る）、
    刈ってから、出力を閉じる。上限で止めたときは、終了コードは None で、上限までに読んだ出力も、返さずに捨てる。結果の文に出力を入れない。
    起動できない（OSError）ときは、例外のまま返す（呼ぶ側の claude_login が受ける）。出力の大きさに上限は無い。"""
    p = subprocess.Popen(cmd, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         start_new_session=True)
    try:
        out, err = p.communicate(timeout=LOGIN_CHECK_TIMEOUT)
        return p.returncode, out, err, f"終了コード {p.returncode}"
    except subprocess.TimeoutExpired:
        return None, b"", b"", f"{LOGIN_CHECK_TIMEOUT:g} 秒で終わらなかったので止めた"
    finally:
        if p.returncode is None:  # 上限で止めた（想定外の例外も）。まだ刈っていないので、グループの番号は別のプロセスに使い回されていない
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.wait()
        p.stdout.close()
        p.stderr.close()


def claude_login(cfg: Path) -> dict:
    """専用ログインのトークンの組織と、launcher を呼んだ環境の Claude Code のログインの組織を比べる。結果は記録の claude_login:
    {"org": "same"}・{"org": "different", "note": 案内}・{"org": "unknown", "why": 確かめられなかった理由}・
    {"org": "unknown", "why": …, "note": 確かめが働いていないことの案内}（覚えられなかったときは "cache" も）。どの結果でも、子の起動は
    止めない（想定外の例外も、起動できなかったことも unknown にして、例外の型の名前だけを残す。例外の文には問い合わせの出力が入りうる）。
    ログインの側: claude auth status --json の標準出力の orgId（終了コードは見ない）。uuid の形の文字列でなければ unknown で、
    問い合わせをしない。
    トークンの側: claude-login.json に .env の中身と、組織の ID を小文字にした文字列の SHA-256 を覚え、.env の中身が同じなら
    問い合わせない。違えば（覚えが無い・読めない・形が違うときも）、専用ログインで Haiku に短い問い合わせをし（--restricted で、
    専用ログインの hook・プラグインを読ませない）、ANTHROPIC_LOG=debug の出力の全体から、名前が anthropic-organization-id" で終わる
    ヘッダの値を読む。値が 1 種類なら覚え（終了コードが 0 でなくても使う）、0・2 種類以上は unknown で覚えない。問い合わせが終了コード 0 で
    終わったのに読めないとき（is_error は見ない）だけ note を付ける。覚えは 0600 で直に書き、壊れていれば次の起動で問い合わせ直す。
    記録・ファイルに、組織の ID・問い合わせの出力を書かない。.env の中身は SHA-256 にだけ使う。
    守らない範囲: 同じ組織の中の別の人のトークン。debug の出力の書式の変化（読めなければ unknown）。同時に起こした launcher は、それぞれ
    問い合わせる。問い合わせの間の .env の置き換え（前の中身の SHA-256 で覚える）。覚えの中身の正しさ。claude-login.json が FIFO
    （止まる）・symlink（指す先を書き換える）のとき。上限で止めるのはプロセスグループだけ。確かめのプロセスがホームに残したものは数えない。
    準備がこの後で失敗すると、記録に欄は出ない。BB_WRIT_LOGIN_CHECK_TIMEOUT（試験だけが使う）が数でないと、どの子も起動しない。
    確かめの間に launcher が止められると、記録は残らず、問い合わせのプロセスは自分で終わるまで残る。
    """
    digest = lambda b: hashlib.sha256(b).hexdigest()
    try:
        _, out, _, result = run_bounded(["claude", "auth", "status", "--json"], dict(os.environ), state_dir())
        try:
            status = json.loads(out)
        except ValueError:
            return {"org": "unknown", "why": f"claude auth status --json の出力を JSON として読めない（{result}）"}
        org = status.get("orgId") if isinstance(status, dict) else None
        if not (isinstance(org, str) and re.fullmatch(UUID, org, re.I)):
            return {"org": "unknown", "why": f"launcher を呼んだ環境の Claude Code のログインに、組織（orgId）が無いか、uuid の形でない（{result}）"}
        env_file, cache = cfg / ".env", state_dir() / "claude-login.json"
        env_sum = digest(env_file.read_bytes())
        try:
            known = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            known = None
        extra = {}
        if (isinstance(known, dict) and known.get("env") == env_sum and isinstance(known.get("org"), str)
                and re.fullmatch(r"[0-9a-f]{64}", known["org"])):
            token_org = known["org"]
        else:
            env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "ANTHROPIC_"))}
            env.update(CLAUDE_CONFIG_DIR=str(cfg / "claude-home"), ANTHROPIC_LOG="debug", CLAUDE_CODE_DISABLE_BACKGROUND_TASKS="1")
            code, out, err, result = run_bounded(
                ["dotenvx", "run", "-q", "-f", str(env_file), "-fk", str(cfg / ".env.keys"), "--", "claude", "-p", LOGIN_CHECK_PROMPT,
                 "--model", "haiku", "--output-format", "json", "--no-session-persistence", "--tools", "", "--restricted"], env, state_dir())
            found = {m.lower() for m in LOGIN_ORG_HEADER.findall((out + b"\n" + err).decode("utf-8", "replace"))}
            del out, err
            if len(found) != 1:
                return {"org": "unknown", "why": f"専用ログインの問い合わせの出力に、組織のヘッダ anthropic-organization-id の値が {len(found)} 種類（{result}）",
                        **({"note": CLAUDE_LOGIN_UNREAD_NOTE} if code == 0 else {})}
            token_org = digest(found.pop().encode())
            try:
                with os.fdopen(os.open(cache, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
                    json.dump({"env": env_sum, "org": token_org}, f)
            except OSError as e:
                extra = {"cache": f"claude-login.json に覚えられなかった（{type(e).__name__}）。次の起動でまた問い合わせる"}
        if token_org == digest(org.lower().encode()):
            return {"org": "same", **extra}
        return {"org": "different", "note": CLAUDE_LOGIN_NOTE, **extra}
    except Exception as e:
        return {"org": "unknown", "why": f"確かめの途中で想定外の失敗（{type(e).__name__}）"}


def command(a, prompt: str, scratch: Path) -> tuple[list[str], dict, str | None]:
    cfg = config_dir()
    env = dict(os.environ)
    if a.host == "claude":
        env_file, keys = cfg / ".env", cfg / ".env.keys"
        for tool in ("claude", "dotenvx"):
            if not shutil.which(tool):
                return [], env, f"{tool} が PATH に無い"
        setup = (f"claude setup-token のトークンを、dotenvx set CLAUDE_CODE_OAUTH_TOKEN <トークン> --no-native -f {env_file} -fk {keys} で置く"
                 "（手順と確かめ方は README の「任意機能の準備」の「Claude Code の専用ログイン」）")
        if not env_file.is_file():
            return [], env, f"専用ログインが無い: {env_file} が無い。{setup}"
        if not keys.is_file():
            # dotenvx 2.25 から、OS の secret store が使えると、鍵の既定の置き場が .env.keys でなくそちらになった（2.38 から、非対話の実行ではファイル）
            return [], env, (f"専用ログインの鍵が無い: {env_file} はあるが {keys} が無い。dotenvx 2.25 以降は、--no-native を付けないと"
                             f"鍵を OS の secret store（macOS のキーチェーンなど）に置くことがあり、launcher はそれを使わない。{setup}")
        # 親の Claude Code の環境（CLAUDE_CODE_PLUGIN_DIRS の利用者の mod、CLAUDE_EFFORT など。mod は sandbox の外で動く）と API の鍵・接続先を渡さない。
        # 鍵があると専用ログインの代わりに別料金の API へ落ちうる。専用ログインのトークンは dotenvx が子の環境にだけ入れる
        env = {k: v for k, v in env.items() if not k.startswith(("CLAUDE", "ANTHROPIC_"))}
        env["CLAUDE_CONFIG_DIR"] = str(cfg / "claude-home")
        # 背景の処理（Bash の run_in_background など）を使わせない。終わりの知らせが次のターンを起こすと、--output-format json は
        # 最後のターンの結果だけを返し、前のターンに書いた報告が消えた（2.1.310、審査役が 2 回 invalid-report。公式の headless の文書）
        env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
        if a.write:
            # 書く子に渡すのは、対象（作業場所）とその記録だけ。タスクの一覧・統合の記録・他の worktree は親だけが書く。
            # Bash は sandbox で同じ範囲に閉じ、--restricted でファイル系のツールも同じ範囲に閉じる（対象の .claude の設定も読まない）。
            # ネットワークは、これまでどおり出られるようにする（sandbox の既定は宛先なしで、依存の取得もできなくなる）
            perm = ["--permission-mode", "auto", "--restricted", "--tools", "Read,Grep,Glob,Edit,Write,Bash,Skill,WebSearch,WebFetch", "--add-dir", str(work_record(a.cwd))]
            # 入口 bb-writ:bb-writ は、呼ぶたびに fork した指揮者（frontmatter の model）を立てる。子が呼ぶと、子の中に
            # もう 1 人の指揮者が立つので拒む（本体 bb-writ:conduct とほかのスキルは呼べる。読み取り専用の子も同じ）
            settings = {"permissions": {"deny": ["Skill(bb-writ:bb-writ)"]},
                        "sandbox": {"enabled": True, "allowUnsandboxedCommands": False, "failIfUnavailable": True,
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
            if not os.environ.get("HOME") or Path.home().resolve() == Path("/"):
                return [], env, "HOME が無いか / を指すので、読み取り専用の Claude Code の子の読みを絞れない"
            if a.cwd == Path.home().resolve() or a.cwd in Path.home().resolve().parents:
                return [], env, "対象がホームそのものかホームを含む場所なので、読み取り専用の Claude Code の子にホームの認証情報を拒めない"
            if any(a.cwd == Path(os.path.realpath(p)) for p in read_only_secrets()):
                return [], env, f"対象 {a.cwd} が認証情報の置き場そのものなので、読み取り専用の Claude Code の子にその中の認証情報を拒めない"
            deny = [str(a.cwd), str(Path.home()), str(Path.home().resolve())] + ([str(git_dir), os.path.commonpath([a.cwd, git_dir])] if git_dir else [])
            reads = claude_reads(a.cwd, git_dir, env, a.audit)
            env = {k: v for k, v in env.items() if k in READ_ONLY_ENV or k.upper().endswith("_PROXY") or k.startswith("LC_")}
            env["CLAUDE_CONFIG_DIR"] = str(cfg / "claude-home")
            env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
            perm = ["--permission-mode", "dontAsk", "--restricted", "--tools", "Read,Grep,Glob,Bash,Skill"]
            settings = {"permissions": {"allow": ["Read", "Grep", "Glob", "Bash", "Skill"], "deny": ["Skill(bb-writ:bb-writ)"]},
                        "sandbox": {"enabled": True, "allowUnsandboxedCommands": False, "failIfUnavailable": True,
                                    "filesystem": {"denyWrite": sorted(set(deny)), **reads},
                                    "credentials": secret_env(env)}}
        cmd = ["dotenvx", "run", "-q", "-f", str(env_file), "-fk", str(keys), "--",
               "claude", "-p", CLAUDE_PROMPT_HEAD + prompt, "--output-format", "json", "--model", a.model, "--effort", a.effort,
               "--permission-prompts", "none", *perm, "--settings", json.dumps(settings),
               "--plugin-dir", str(Path(__file__).resolve().parents[1]),  # --restricted は専用ログインに入れたプラグインを読まないので、この bb-writ を渡す
               "--resume" if a.resume else "--session-id", a.session]
        return cmd, env, None
    if a.host == "grok":
        if not shutil.which("grok"):
            return [], env, "grok が PATH に無い"
        login = cfg / "grok-auth"
        if not (login / "auth.json").is_file():
            return [], env, f"専用ログインが無い: {login}/auth.json（GROK_HOME={login} grok login。README の「任意機能の準備」の「Grok Build の専用ログイン」）"
        src = a.grok_home and grok_session_dir(a.grok_home, a.session)
        if a.grok_home and not src:
            return [], env, f"前の home {a.grok_home} に session {a.session} の記録が 1 つだけ見つからない（symlink の下は見ない）"
        if a.write:
            own, common, why = worktree_dirs(a.cwd)
            if why:
                return [], env, why
            home, profile = grok_home(login, "workspace", [work_record(a.cwd), *(commit_dirs(own, common) if own else [])])
        else:
            for d in grok_ro_writable(state_dir() / "grok-homes"):
                if a.cwd == d or d in a.cwd.parents or a.cwd in d.parents:
                    return [], env, f"対象と {d} が重なる。Grok の read-only の sandbox はそこへの書き込みを止めないので、読み取り専用にできない"
            if not os.environ.get("HOME") or Path.home().resolve() == Path("/"):
                return [], env, "HOME が無いか / を指すので、読み取り専用の Grok の子に認証情報の置き場を拒めない"
            # 読みは、読み取り専用の Codex の子と同じ一覧を secret_denies で拒む（#43）。~/.config・~/.grok を丸ごと拒むと Grok が起動しない
            # （「Not signed in」、bwrap の execvp の Permission denied。1.0.89）ので、Grok 本体・その bin・Git の設定・専用ログインの
            # auth.json・対象を含む置き場は中を 1 つずつ拒む。子は Grok 自身が読む専用ログインの auth.json を読める（sandbox はプロセス全体に掛かる）
            git_dir, why = grok_git_dir(a.cwd)
            if why:
                return [], env, why
            keep = [Path(os.path.realpath(shutil.which("grok"))), Path.home() / ".grok" / "bin", Path.home() / ".config" / "git",
                    login / "auth.json", a.cwd, *([git_dir] if git_dir else [])]
            home, profile = grok_home(login, "read-only", [], secret_denies(keep))
        if src:
            copy_grok_session(src, a.grok_home, home)
        # 親の GROK_*（GROK_AGENT・GROK_SANDBOX・GROK_CONFIG など）と API キーを渡さない。API キーがあるとログインの代わりに別料金の API へ落ちる
        env = {k: v for k, v in env.items() if not k.startswith("GROK_") and k not in ("XAI_API_KEY", "RUST_LOG")}
        if not a.write:
            secret = {e["name"] for e in secret_env(env)["envVars"]}
            env = {k: v for k, v in env.items() if (k in READ_ONLY_ENV or k.upper().endswith("_PROXY") or k.startswith("LC_")) and k not in secret}
        env.update(GROK_HOME=str(home), GROK_MEMORY="0", **GROK_COMPAT_OFF)
        cmd = ["grok", "-p", prompt, "--output-format", "json", "-m", a.model, "--effort", a.effort, "--cwd", str(a.cwd),
               "--sandbox", profile, "--always-approve",
               "--no-leader", "--no-auto-update", "--no-subagents", "--disable-web-search", "--disallowed-tools", "x_search",
               "-r" if a.resume else "-s", a.session]
        return cmd, env, None
    if not shutil.which("codex"):
        return [], env, "codex が PATH に無い"
    home = cfg / "codex-home"
    if not (home / "auth.json").is_file():
        return [], env, f"専用ログインが無い: {home}/auth.json（CODEX_HOME={home} codex login。README の「任意機能の準備」の「Codex の専用ログイン」）"
    why = codex_refresh(a, home)
    if why:
        return [], env, why
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
        if not os.environ.get("HOME") or Path(os.environ["HOME"]).resolve() == Path("/"):
            return [], env, "HOME が無いか / を指すので、読み取り専用の Codex の子に認証情報の置き場を拒めない"
        # 子に渡す環境は、読み取り専用の Claude Code の子と同じ許可の一覧だけ（Codex は既定で KEY・SECRET・TOKEN を含む変数も子の
        # コマンドに残す。SSH_AUTH_SOCK・DBUS_SESSION_BUS_ADDRESS・親の GIT_* も渡さない）
        secret = {e["name"] for e in secret_env(env)["envVars"]}
        env = {k: v for k, v in env.items() if (k in READ_ONLY_ENV or k.upper().endswith("_PROXY") or k.startswith("LC_")) and k not in secret}
        env["CODEX_HOME"] = str(home)
        mode, a.codex_denies = codex_read_only(scratch, a.cwd)
        env["TMPDIR"] = str(scratch.resolve())
    # 再開（exec resume）は -C・-c・--add-dir などを受けないので、exec の側に置いたまま、依頼の前に挟む（0.164.0 で、権限も同じまま続いた）
    cmd = ["codex", "exec", "--json", "-m", a.model, "-c", f'model_reasoning_effort="{a.effort}"',
           "--skip-git-repo-check", "-C", str(a.cwd), *mode, *(["resume", a.session] if a.resume else []), prompt]
    return cmd, env, None


def obj(v) -> dict:
    return v if isinstance(v, dict) else {}


def text_of(v) -> str:
    return v if isinstance(v, str) else ""


def fable_unavailable(a, got: dict, timed_out: bool, stop: dict, left: list) -> bool:
    """表の段（別名 fable）を頼んだ子が、費用が 0 のまま（作業を始める前に）、利用枠が尽きた（429）か使えないモデル（404）の API のエラーで終わった。
    ほかのエラー（過負荷・回線・認証）、途中まで進んだ子（費用が 0 でない）、続き、時間切れ、止められた子（子を刈った後に受けたシグナルも）、
    止めきれない子孫が残った子は、立て直さない（#56）。
    立て直すと決めた後に受けたシグナルは、2 回目の子を起こした直後に、その子へ伝える（main の起こす周の頭）。
    守らない範囲: 刈った後のシグナルで立て直しをやめた記録に、その事実は残らない。残りの期限が
    わずかでも 2 回目の子を起こす。表のキーとの比べは別名そのままで、「Fable」「fable[1m]」は立て直さない。model_substituted は、
    日付つきの名前・別名でない名前（opusplan など）を頼むと誤って付く。試験は、404・401 の違い（偽の子は 429 と 529
    だけを返す）を見ない（404 で立て直すことは実物の CLI で 1 回確かめた）。時間切れ・止められた子・残った子孫・is_error・残りの期限が
    0 以下の条件も、試験は見ない（外しても試験は通る）。"""
    usage = got.get("usage") or {}
    return (not a.resume and a.model in FALLBACK.get(a.host, {}) and not timed_out and "sig" not in stop and "late" not in stop
            and not left and got.get("is_error") is True and got.get("terminal_reason") == "api_error"
            and got.get("api_error_status") in (404, 429) and not usage.get("cost_usd"))


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
            "terminal_reason": d.get("terminal_reason"), "api_error_status": d.get("api_error_status"),
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
                yield b'{"type": "turn_context", "payload": {}}'
                return
            yield line.rstrip(b"\n")


def where_of(path) -> str:
    path = obj(path)
    if path.get("type") == "special" and text_of(obj(path.get("value")).get("kind")):
        return ":" + obj(path.get("value"))["kind"]
    if path.get("type") == "path" and text_of(path.get("path")).startswith("/"):
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
    eff, perms, files = None, [], []
    if session and re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", session):
        files = list((config_dir() / "codex-home" / "sessions").glob(f"**/rollout-*-{session}.jsonl"))
        for f in files:
            for line in rollout_lines(f):
                try:
                    e = obj(json.loads(line.decode("utf-8")))
                except (ValueError, RecursionError):
                    perms.append((None, None, None))
                    continue
                if e.get("type") == "turn_context":
                    p = obj(e.get("payload"))
                    perm = obj(p.get("permission_profile"))
                    fs = obj(perm.get("file_system"))
                    entries = fs.get("entries") if fs.get("type") == "restricted" and isinstance(fs.get("entries"), list) else None
                    if entries is not None and not all(isinstance(x, dict) and x.get("access") in ("read", "write", "deny")
                                                       and where_of(x.get("path")) is not None for x in entries):
                        entries = None
                    eff = {"model": text_of(p.get("model")) or None, "effort": text_of(p.get("effort")) or None,
                           "sandbox": text_of(obj(p.get("sandbox_policy")).get("type")) or None}
                    # 書ける場所（特別な場所は :tmpdir の形）。0.163.0 の sandbox は、読み取り専用の profile でも workspace-write と記録される
                    denied = {where_of(x["path"]) for x in entries or [] if x["access"] == "deny"}  # 同じ場所なら deny が write に勝つ（公式文書）
                    perms.append((None if entries is None else [w for x in entries if x["access"] == "write" and (w := where_of(x["path"])) not in denied],
                                  text_of(perm.get("network")) or None,
                                  None if entries is None else [(x["access"], where_of(x["path"])) for x in entries]))
    if eff is not None:
        eff["turns"] = [{"writable": w, "network": n} for w, n, _ in perms]
        eff["writable"] = None if any(w is None for w, _, _ in perms) else sorted({x for w, _, _ in perms for x in w})
        nets = {n for _, n, _ in perms}
        eff["network"] = None if None in nets else next((n for n in sorted(nets) if n != "restricted"), "restricted")
    turns = [{"writable": w, "network": n, "rules": r or []} for w, n, r in perms] if eff is not None else []
    return {"text": text, "session": session, "model_effective": eff, "usage": usage, "errors": errors, "permissions": turns,
            "rollouts": len(files)}


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


def check_report(text: str, cwd: Path, audit: bool = False) -> tuple[dict | None, list[str]]:
    i = text.rfind("```json")
    if i < 0:
        return None, ["報告の JSON ブロックが無い"]
    try:
        r, _ = json.JSONDecoder().raw_decode(text[i + len("```json"):].lstrip())
    except (ValueError, RecursionError) as e:
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
        m = re.fullmatch(r"`?([^\s`:]+):(\d+)(?:[-–]\d+)?`?", where)
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
    if audit and r.get("deliver") not in ("yes", "no"):
        problems.append("deliver が yes / no のどちらでもない（監査役の判定）")
    return r, problems


def fix_prompt(problems: list[str], cwd: Path) -> str:
    """照合（check_report）に落ちた報告を、同じ session の続きで直させる依頼文。落ちた点（check_report の問題の文。証拠のファイルなら
    where に書かれた道筋）と、launcher がそれをどう読んだかを名指しする。どこからの相対かを launcher が推し量ることはしない（子は自分が
    見た道筋を知っている）。後に約束（CONTRACT など）を足して渡す。"""
    return ("bb-writ の launcher から: 直前の報告は、launcher の照合で次の点が通らず、使えない。作業はやり直さず、直した報告の JSON ブロックを、"
            "最後にもう一度 1 つだけ書く。\n" + "".join(f"- {p}\n" for p in problems)
            + f"launcher は、evidence の where が「ファイル:行」の形なら、絶対パスか、対象 {cwd} からの相対パスとして、そのファイルが在るかを確かめる"
            "（`~` は展開しない）。対象の外のファイル（作業の記録、`mktemp -d` の写しなど）は、絶対パスで書く。写しを引いたなら、対象の中の同じファイルの道筋に"
            "書き換える。1 つの where には、ファイル:行・コマンド・URL のどれか 1 つだけを書き（複数なら evidence の項目を分ける）、説明は what に書く。"
            "存在しないファイルを証拠にしない（無いことが分かったなら、それを what に書き、確かめたコマンドを where に書く）。\n")


def worktree_top(cwd: Path):
    """cwd を含む Git の作業ツリーの根（無ければ None）。子が書けた設定の命令を走らせない形で見る。"""
    try:
        g = subprocess.run([*QUIET_GIT, "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(g.stdout.strip()).resolve() if g.returncode == 0 and g.stdout.strip() else None


def interrogated(cwd: Path) -> list:
    """cwd と同じ Git の作業ツリーの中（根とその下の道筋）を --cwd にして --interrogate で立て、ok で返った審査役の記録（記録の順）。
    作業ツリーの外（親のディレクトリなど）の記録、head の無い記録（未 commit の変更があった・途中で HEAD が変わった）、作業ツリーの
    中に置いた別の worktree の記録、読めない行は数えない。守らない範囲: 記録の --cwd が消えた下の階層だと、作業ツリーの根を
    確かめられず数えない。head の無い記録は、次の周が比べる印の相手からも外れる（未 commit のまま審査役を立て、その直しを commit
    した後は、前のきれいな HEAD の審査役の印と比べる）。"""
    found = []
    top = worktree_top(cwd)
    if top is None:
        return found
    try:
        with open(state_dir() / "delegations.jsonl", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if not (isinstance(e, dict) and e.get("interrogate") is True and e.get("status") == "ok" and isinstance(e.get("cwd"), str)):
                    continue
                c = Path(e["cwd"])
                # head の無い記録（未 commit の変更があった・途中で HEAD が変わった）は周に数えない。作業ツリーの中に置いた別の
                # worktree（.worktrees/ など）の記録は、その worktree の根が違うので数えない
                if isinstance(e.get("head"), str) and (c == top or top in c.parents) and (c == top or worktree_top(c) == top):
                    found.append(e)
    except OSError:
        pass
    return found


def revisit_of(cwd: Path) -> tuple:
    """(作業の記録の「直しの見直し」の欄の中身の SHA-256 の先頭 16 桁（空なら ""）, 記録の道筋)。記録は cwd から上へたどって最初に在るもの。
    無ければ (None, 道筋)。作らない。"""
    top = worktree_top(cwd) or cwd
    notes = [record_at(d) / "note.md" for d in (cwd, *(p for p in cwd.parents if p == top or top in p.parents))]
    note = next((n for n in notes if n.is_file()), notes[0])  # 下の階層を --cwd にしても、作業ツリーの根までの記録を見る
    try:
        body = note.read_text(encoding="utf-8")
    except OSError:
        return None, note
    found = field_texts(body.splitlines(), REVISIT)
    text = found[0].strip() if found else ""
    return (hashlib.sha256(text.encode()).hexdigest()[:16] if text else ""), note


def unrevisited(cwd: Path) -> str:
    """前の HEAD で審査役を立てた作業場所で、別の HEAD に次の審査役を立てるのに、作業の記録の「直しの見直し」の欄が空か、
    前の審査役を立てたときから書き足していなければ、その理由。
    指摘の箇所だけを直して次の周に出し、直しの隣の抜けと直しが持ち込んだ矛盾が周ごとに出て、周が収束しなかった（2026-11）。
    守らない範囲: 欄の中身の真偽と、書き足したのがどの指摘の見直しかは見ない。作業の記録が無い場所（claim していない）と、
    未 commit の変更がある作業場所（審査役の記録の head が無く、周に数えない）は見ない。前の審査役は ok で返ったものだけを数える
    （not-run と記録の読めない行は数えない）。周ごとに新しい作業場所を --cwd にすると掛からない。作業の記録は --cwd から上へ
    たどるので、根を --cwd にして下の階層で claim した記録は見ない。同じ場所を別の仕事に使い回すと、前の仕事の審査役も数える
    （task start の作業場所は毎回新しい）。"""
    prior = interrogated(cwd)
    now = clean_head(cwd)
    if not prior or now is None or any(e.get("head") == now for e in prior):
        return ""
    h, note = revisit_of(cwd)
    if h is None:
        return ""
    name = REVISIT.split("（")[0]
    heads = ", ".join(dict.fromkeys(e["head"][:7] for e in prior if isinstance(e.get("head"), str)))
    if not h:
        return (f"この作業場所は前の HEAD（{heads}）で審査役を立てた。次の審査の前に、作業の記録 {note} の「{name}」の欄に、"
                f"指摘ごとに同じ性質の箇所・直しが前提を変えた箇所・変えた節の全体の矛盾を探した方法と結果を書く（欄の行が無ければ雛形の行を足す。"
                f"直しが無く、rebase・amend だけで HEAD が変わったなら、そう 1 行書く。interrogate の 5）")
    if h == prior[-1].get("revisit"):
        return (f"作業の記録 {note} の「{name}」の欄が、前の審査役を立てたときから変わっていない。前の周の指摘の直しについて、"
                f"同じ性質の箇所・直しが前提を変えた箇所・変えた節の全体の矛盾を探した方法と結果を書き足す（直しが無く、rebase・amend だけで"
                f" HEAD が変わったなら、そう 1 行書き足す。interrogate の 5）")
    return ""


def clean_head(cwd: Path) -> str | None:
    """対象の HEAD の commit。Git の作業ツリーでないか、未 commit の変更（staged・追跡外のファイルを含む。無視するものは除く）があれば None。
    子が見たものを commit に結び付け、work.py の --reviewed がこの記録と照らす（Issue #14）。
    親の GIT_DIR・GIT_WORK_TREE など（git rev-parse --local-env-vars）は外す（別の場所を見て、対象の変更を見落とす）。
    追跡外のファイルと、サブモジュールが別の commit を指す変更は、設定（status.showUntrackedFiles=no、diff.ignoreSubmodules・
    submodule.<名>.ignore）に関わらず数える。書く子の後の作業場所でも動くので、子が書けた設定の命令を走らせない形で見る（QUIET_GIT、
    --no-optional-locks。サブモジュールの中には入らず、中の未 commit の変更は数えない。#64）。
    assume-unchanged・skip-worktree で隠した変更は見分けない。"""
    try:
        local = subprocess.run(["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True, errors="surrogateescape", timeout=GIT_TIMEOUT)
        env = {k: v for k, v in os.environ.items() if k not in local.stdout.split()}
        head = subprocess.run(["git", "rev-parse", "--verify", "HEAD^{commit}"], cwd=cwd, env=env, capture_output=True, text=True, errors="surrogateescape", timeout=GIT_TIMEOUT)
        # バイトで受ける（ファイル名は UTF-8 とは限らない）。index を書き直すロックを取らない（利用者の git と競わない）
        status = subprocess.run([*QUIET_GIT, "--no-optional-locks", "status", "--porcelain", "--untracked-files=normal", "--ignore-submodules=dirty"], cwd=cwd, env=env,
                                capture_output=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if local.returncode or head.returncode or status.returncode or status.stdout.strip():
        return None
    return head.stdout.strip()


PR_SET_CHILD_SUBREAPER = 36


def become_subreaper() -> None:
    """子の CLI が新しいセッションに切り離した子孫（Claude Code 2.1.309 の Bash）を、子が終わった後に自分の下へ付け替えさせる（Linux）。"""
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_CHILD_SUBREAPER) が失敗した")


def load_now() -> float | None:
    try:
        return round(os.getloadavg()[0], 1)
    except (OSError, AttributeError):
        return None


UNCHECKED = ("Codex の実効の権限を記録（", "続きの Codex の記録ファイルが")
UNSAFE = {"unsafe", "breach", "other-session", "left", "git"}
STOP_GRACE = 30
# 照合に落ちた報告を直させる続きを起こすのに、--timeout の残りとして要る秒数（残りが少ないと、invalid-report を時間切れに変えるだけ）。
# 実物の Claude Code の続き（1 ターンで直した）は、起こしてから終わるまで 5.4 秒（CLI の duration_ms は 3.6 秒）。その 3 倍余り
# （Codex・Grok の続きは測っていない）
FIX_BUDGET = 20


def ask_to_stop(child: subprocess.Popen, stop: dict) -> None:
    """直下の子に TERM を 1 回だけ送る。グループには送らない（dotenvx が子へ伝え直し、子の CLI が TERM を 2 回受ける）。"""
    if "at" in stop or stop.get("reaped"):
        return
    stop["at"] = time.monotonic()
    try:
        os.kill(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def wait_for_exit(child: subprocess.Popen, timeout: float, stop: dict) -> None:
    """子の CLI が終わるのを待つ。期限が来たら TERM を送って片付けを待ち、それでも終わらなければ TimeoutExpired（いきなり KILL すると、子の CLI が
    対象に置き場を残した）。Linux では刈らずに待つ。刈らないので、止め終わるまで子の CLI とそのプロセスグループの番号は別のプロセスに
    使い回されず、終了コードも後の child.wait に残る。待つ間に自分の下へ来て終わった孤児は刈る（subreaper なので、刈らないとゾンビが溜まる）。
    Linux の外は刈るしかないので、刈ったらシグナルを伝えない印を立てる。"""
    deadline = time.monotonic() + timeout
    while True:
        r = None
        if LINUX:
            r = os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if r is not None and r.si_pid == child.pid:
                return
            if r is not None:
                os.waitpid(r.si_pid, 0)
        elif child.poll() is not None:
            stop["reaped"] = True
            return
        now = time.monotonic()
        if "at" in stop:
            limit = stop["at"] + STOP_GRACE
            if now > (limit if stop.get("timed_out") else min(limit, deadline)):
                raise subprocess.TimeoutExpired(child.args, timeout)
        elif now > deadline:
            stop["timed_out"] = True
            ask_to_stop(child, stop)
        if r is None:
            time.sleep(0.1)


def stop_descendants(group: int) -> tuple[list[str], list[str]]:
    """自分の子孫を、残らなくなるまで（5 秒まで）止める。(止めたもの, 止められずに残ったもの) を「pid コマンド行」で返す。
    become_subreaper の後、子の CLI を止めてから呼ぶ。1 周に止めるのは自分の直接の子だけで、刈れたら終わったとみなす（刈るまでは番号を
    保つので、調べてから止めるまでに別のプロセスへ使い回されない。ゾンビに見えても止める: 主スレッドだけ終わったプロセスは、ほかの
    スレッドが動く間はゾンビに見え、刈れず、その下の子孫もまだ付け替わっていない）。子が終わると、その下の子孫が自分の下に来るので、
    次の周で止める。止めたものには、刈る前に自分で終わっていたものも入る。子の CLI のプロセスグループ（group。止めてから呼ぶ）の
    ものは、子の CLI そのもの（dotenvx の下の claude など）なので、止めて刈るが並べない。/proc を読めなければ例外を出す。"""
    def children() -> dict[int, tuple[str, bool]]:
        found = {}
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open(f"/proc/{d}/stat", "rb") as f:
                    raw = f.read()
                fields = raw[raw.rindex(b")") + 2:].split()  # コマンド名は ) を含みうる
                if int(fields[1]) != os.getpid():
                    continue
                with open(f"/proc/{d}/cmdline", "rb") as f:  # 主スレッドが終わると空になるので、そのときはコマンド名
                    args = (f.read().replace(b"\0", b" ").strip() or raw[raw.index(b"(") : raw.rindex(b")") + 1]).decode("utf-8", "replace")
            except (FileNotFoundError, ProcessLookupError):
                continue
            found[int(d)] = (f"{d} {args}"[:300], int(fields[2]) == group)
        return found

    stopped, denied = {}, {}
    deadline = time.monotonic() + 5
    while (left := children()) and time.monotonic() < deadline:
        for pid, (line, cli) in left.items():
            if not cli:
                stopped.setdefault(pid, line)
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError as e:
                denied[pid] = f"{line}（{e!r}）"[:300]
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
        time.sleep(0.05)
    return sorted(v for k, v in stopped.items() if k not in left), sorted(denied.get(k, v[0]) for k, v in left.items())


def session_history(session: str) -> list[dict]:
    """同じ session のこれまでの起動の記録（delegations.jsonl）。前の記録へ戻って続けても、その session に起きたことを忘れないため。"""
    try:
        with open(state_dir() / "delegations.jsonl", encoding="utf-8", errors="replace") as f:
            lines = f.read().split("\n")
    except FileNotFoundError:
        return []
    found = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and session in (r.get("session"), r.get("session_asked")):
            found.append(r)
    return found


def hold_session(session: str):
    """この session の続きの排他。launcher が終わると（KILL でも）OS が放す。取れなければ None。"""
    d = state_dir() / "resuming"
    d.mkdir(parents=True, exist_ok=True)
    f = open(d / hashlib.sha256(session.encode()).hexdigest()[:32], "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


LEFTOVER_DAYS = 7
PLACES_HELD = []


def hold_place(p: Path):
    """一時の置き場 p を使う間（片付けは、消す間）の排他。置き場のディレクトリそのものの flock で、(fd, None) か (None, 理由) を返す。
    理由は "gone"（置き場が無い・symlink・ディレクトリでない）、"busy"（ほかの launcher が使っているか、片付けている）、
    "unsupported"（この環境では、ディレクトリの flock を取れない）。守る物そのものに排他を掛けるので、記録（delegations.jsonl）の値にも、
    ほかのファイルにも頼らない（記録の session から引く排他は、行を書き換えると外れた。隣に置く排他のファイルは、消されると外れた）。
    launcher が終わると（KILL でも）OS が放す。子が読めない権限にした置き場は、モードを 0700 に置き換えて開く（続きの起動では、
    記録が指す場所に対して行うので、記録を書き換えて、持ち主が開けない別のディレクトリを指させると、そのモードが 0700 に替わる。
    消しはしない）。
    排他を取った後に、名前を別のディレクトリに付け替えられることは守らない（sweep_leftovers の説明）。"""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        try:
            fd = os.open(p, flags)
        except PermissionError:
            if not stat.S_ISDIR(os.lstat(p).st_mode):
                raise
            os.chmod(p, 0o700)
            fd = os.open(p, flags)
    except OSError:
        return None, "gone"
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        return None, "busy" if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN) else "unsupported"
    try:
        same = os.path.samestat(os.fstat(fd), os.lstat(p))
    except OSError:
        same = False
    if not same:
        os.close(fd)
        return None, "gone"
    return fd, None


def usable_time(t, now: float) -> bool:
    """片付けの期限に使える時刻か。数でない値・無限大・未来（1 日より先）の時刻は使わない（偽の行 1 つで置き場を残し続けられないように）。"""
    try:
        return isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t) and t <= now + 86400
    except OverflowError:  # float にできない大きさの整数
        return False


def stamp_time(name: str):
    try:
        return time.mktime(time.strptime(name[:15], "%Y%m%d-%H%M%S"))
    except (ValueError, OverflowError):
        return None


def task_worktree_gone(cwd, base: Path) -> bool:
    """cwd が、もう無いタスクの作業場所（状態の置き場の worktrees/<リポジトリ>/<タスク> とその下）か。
    task cleanup が消した作業場所を見ていた子は、続きから起動できない（対象が無い）。ほかの場所が無いだけでは、こう扱わない
    （外した媒体や、作り直される場所かもしれない。そちらは期限で消える）。"""
    if not isinstance(cwd, str) or not cwd:
        return False
    trees = Path(os.path.realpath(base / "worktrees"))
    try:
        rel = Path(os.path.abspath(cwd)).relative_to(trees)
    except ValueError:
        return False
    return len(rel.parts) >= 2 and not os.path.lexists(trees / rel.parts[0] / rel.parts[1])


def remove_tree(p: Path):
    """置き場（実のディレクトリ）を中身ごと消す。道筋でなく、ディレクトリの fd を基準に、1 段ずつ symlink をたどらずに開いて降り、
    消すのも fd からの相対で行う。降りる途中で、中のディレクトリを外への symlink に差し替えられても、行き先には入らず OSError で止まる
    （道筋で開き直す形は、その差し替えで行き先の中を消した）。子は、書けない・読めないディレクトリ（0555・0000）や深い木を残せるので、
    rmtree は使わない（バージョンによって再帰し、失敗時の呼び直しの形も違う）。読めないディレクトリは、親の fd からの名前でモードを 0700 に
    置き換えてから開く（その瞬間に symlink に替えられると、行き先のディレクトリのモードが 0700 に替わる。消しはしない）。
    深い木でも、開いておく fd は KEEP 個ほどまで（上の段から閉じ、戻るときに上から開き直す。その分、手間は深さの 2 乗に比例して増える。
    深さ 3,000 の木で 0.2 秒ほど）。ディレクトリの中身は、読み終えてから消す（1 つのディレクトリの項目の一覧を、その間だけメモリに持つ。
    入りきらない数の項目があるディレクトリは MemoryError になり、その置き場は片付けられない。sweep_leftovers が理由を記録に出す）。
    読んでいる最中に消すと、その後の読みが残りの項目を返すかは実装による（POSIX は決めていない。macOS の HFS+ では、消した後の
    readdir が残りを読み飛ばした。shutil.rmtree も、読み終えてから消す）。"""
    KEEP = 32
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def open_dir(name, parent: int) -> int:
        try:
            return os.open(name, flags, dir_fd=parent)
        except PermissionError:
            if not stat.S_ISDIR(os.lstat(name, dir_fd=parent).st_mode):
                raise
            os.chmod(name, 0o700, dir_fd=parent)
            return os.open(name, flags, dir_fd=parent)

    frames = []  # 降りている道。各段は [fd（閉じてあれば None）, 親の中での名前, これから降りる子ディレクトリの名前]

    def fd_of(i: int) -> int:
        if frames[i][0] is None:
            fd = os.dup(frames[0][0])
            try:
                for j in range(1, i + 1):
                    nxt = open_dir(frames[j][1], fd)
                    if frames[j - 1][0] is None and j - 1 > i - KEEP:
                        frames[j - 1][0] = fd
                    else:
                        os.close(fd)
                    fd = nxt
            except BaseException:
                os.close(fd)
                raise
            frames[i][0] = fd
        return frames[i][0]

    def enter(fd: int, name) -> None:
        frames.append([fd, name, []])
        os.fchmod(fd, stat.S_IMODE(os.fstat(fd).st_mode) | 0o700)
        with os.scandir(fd) as entries:
            found = list(entries)
        for e in found:
            if e.is_dir(follow_symlinks=False):
                frames[-1][2].append(e.name)
            else:
                os.unlink(e.name, dir_fd=fd)
        held = [f for f in frames[1:] if f[0] is not None]
        if len(held) > KEEP:
            os.close(held[0][0])
            held[0][0] = None

    try:
        enter(os.open(p, flags), None)
        while frames:
            i = len(frames) - 1
            if frames[i][2]:
                name = frames[i][2][-1]
                enter(open_dir(name, fd_of(i)), name)
            else:
                fd, name, _ = frames.pop()
                if fd is not None:
                    os.close(fd)
                if frames:
                    os.rmdir(name, dir_fd=fd_of(len(frames) - 1))
                    frames[-1][2].pop()
        os.rmdir(p)
    finally:
        for f in frames:
            if f[0] is not None:
                os.close(f[0])


def leftover_places(base: Path) -> tuple:
    """launcher が状態の置き場に作る一時の置き場のうち、いまあるものと、並べられなかった理由。置き場は、読み取り専用の Codex の子の
    一時ディレクトリ（delegations/<名前>/tmp）と、Grok の子の起動ごとの home（grok-homes/<名前>）。実のディレクトリだけを数え、
    delegations・grok-homes そのもの、<名前>、tmp のどれかが symlink なら、その先は数えない（行き先は状態の置き場の外かもしれない）。
    記録に書かれた場所でなく、ここで並べたものだけを片付けの対象にする（記録が状態の置き場の外を指していても、そこは消さない）。"""
    found, problems = [], []
    for parent, inside in ((base / "delegations", "tmp"), (base / "grok-homes", None)):
        if parent.is_symlink():
            problems.append(f"{parent} が symlink なので、その下の一時の置き場は片付けない")
            continue
        try:
            entries = sorted(os.scandir(parent), key=lambda e: e.name)
        except FileNotFoundError:
            continue
        except OSError as e:
            problems.append(f"{parent} の中を並べられないので、その下の一時の置き場は片付けない: {e!r}"[:300])
            continue
        for e in entries:
            if not e.is_dir(follow_symlinks=False):
                continue
            p = Path(e.path) / inside if inside else Path(e.path)
            if not p.is_symlink() and p.is_dir():
                found.append((p, e.name))
    return found, problems


def leftover_uses(base: Path) -> dict:
    uses = {}
    try:
        with open(base / "delegations.jsonl", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(r, dict):
                    continue
                for k in ("scratch", "grok_home"):
                    if isinstance(r.get(k), str) and r[k]:
                        try:
                            uses.setdefault(os.path.realpath(r[k]), []).append(r)
                        except ValueError:  # 道筋にできない文字列
                            pass
    except OSError:
        pass
    return uses


def leftover_due(p: Path, name: str, uses: dict, base: Path, now: float) -> bool:
    """置き場 p が、もう続きから起動されないと言えるか（sweep_leftovers の説明の 2 つの条件）。"""
    rs = uses.get(os.path.realpath(p), [])
    times = [t for t in [*(r.get("at") for r in rs), stamp_time(name)] if usable_time(t, now)]
    gone = bool(rs) and all(task_worktree_gone(r.get("cwd"), base) for r in rs)
    return gone or (bool(times) and now - max(times) > LEFTOVER_DAYS * 86400)


def place_due(p: Path) -> bool:
    """一時の置き場 p（delegations/<名前>/tmp か grok-homes/<名前>）が、いま片付けの対象か。続きの起動は、対象の置き場からは続けない
    （まだ消えていなくても）。片付けが途中で止まって中身が半分になった置き場は、対象のままなので、そこからも続かない。対象のままなのは、
    記録の行が launcher の書いたものだけで、時計が戻らず、無くなったタスクの作業場所が同じ道筋に作り直されない間（その外は守らない。
    sweep_leftovers の説明）。"""
    base = state_dir()
    return leftover_due(p, p.parent.name if p.name == "tmp" else p.name, leftover_uses(base), base, time.time())


def sweep_leftovers(now: float, out: dict) -> None:
    """もう続きから起動されない一時の置き場（leftover_places）を消し、消した場所を out["removed"] に、消せなかった理由を out["problems"] に足す
    （途中で想定外の例外が出ても、そこまでに消した場所が out に残る）。
    消すのは、それを指す記録の対象が、どれも、もう無いタスクの作業場所（task_worktree_gone）のものか、最後に使われてから
    LEFTOVER_DAYS 日を過ぎたもの（記録の状態で分けない）。最後に使われた時刻は、記録の at（起動の終わり）と置き場の名前の日時
    （現地時刻）のうち、使える時刻（usable_time）の最も新しいもの。使える時刻が無い置き場は、期限では消さない。記録の無い置き場は、
    動いている最初の起動か、記録を書く前に止まった起動のもので、名前の日時だけで決める。
    使っている置き場は消さない: 使う launcher が置き場のディレクトリの排他（hold_place）を終わりまで持ち、片付けは排他を取れた置き場だけを、
    記録を読み直して決め直してから消す。記録の行を書き換えると、使っていない置き場を早く消させることはできる（報告と記録は残る）。
    守る相手は、記録の値、子が自分の置き場の中に残すもの、片付けの前からある状態の置き場の形（symlink の置き場など）。
    守らない範囲: 片付けや起動と同時に、sandbox の外の同じ利用者のプロセスが状態の置き場の中の名前を付け替える場合（付け替えた先を
    消しうる。読み取り専用の Codex・Grok の子は、置き場の改名と、親・親の親の中への作成に失敗した。親そのものの改名は確かめていない）。状態の置き場を、子の sandbox が書ける場所（/tmp など）の下に
    置いた場合。タイムゾーンの変更と夏時間の戻りによる、名前の日時のずれ。
    途中で止まって半分になった置き場は、記録が launcher の書いたものだけで、時計が戻らず、作業場所が同じ道筋に作り直されない間は
    対象のままで、その記録からの続きは拒否される（順序と途中の kill は tests/test_sweep_model.py が、この範囲で全部たどる）。その外（偽の行・
    時計の巻き戻し・作業場所の作り直し）では、半分の置き場が対象でなくなり、続きの子が写しの一部を失ったまま始まることがある。
    報告と記録（out の record.json・report.md、delegations.jsonl）は消さない。作っていない置き場（resuming/ の空のファイルなど）にも触れない。
    """
    base = state_dir()
    places, problems = leftover_places(base)
    out["problems"] += problems
    uses = leftover_uses(base)
    for p, name in places:
        if not leftover_due(p, name, uses, base, now):
            continue
        lock, why = hold_place(p)
        if lock is None:
            if why == "unsupported":
                out["problems"].append(f"この環境では、置き場のディレクトリの排他（flock）を取れないので、一時の置き場を片付けない: {p}"[:300])
                return
            continue
        try:
            if not leftover_due(p, name, leftover_uses(base), base, now):
                continue
            remove_tree(p)
            out["removed"].append(str(p))
            if p.name == "tmp":
                try:
                    p.parent.rmdir()  # tmp だけだった置き場（--out をほかに渡した起動）は、空になったら外す。報告のある置き場は空でないので残る
                except OSError:
                    pass
        except (OSError, RecursionError, MemoryError) as e:
            out["problems"].append(f"一時の置き場を片付けられない: {p}: {e!r}"[:300])
        finally:
            os.close(lock)


def launcher_error(out_dir: Path) -> str:
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
    ap.add_argument("--host", choices=("claude", "codex", "grok"))
    ap.add_argument("--cwd", type=Path)
    ap.add_argument("--prompt-file", type=Path, required=True)
    ap.add_argument("--model")
    ap.add_argument("--effort")
    ap.add_argument("--write", action="store_true", help="変更を許す（既定は読み取り専用）")
    ap.add_argument("--interrogate", action="store_true", help="読み取り専用の子が interrogate の審査役だと記録に残す")
    ap.add_argument("--audit", action="store_true", help="読み取り専用の子が返す前の判定（遵守の監査）の子だと記録に残す")
    ap.add_argument("--hardest", action="store_true", help="最も難しい依頼（設計、大きな移行・リファクタリング、長い無人実行）を任せる書く子（--write と一緒に）。--model・--effort を省くと最上段で起こす")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--timeout", type=int, help="秒。既定は 1800（--hardest は 3600）。0 は省いたのと同じ。負の値は確かめない")
    ap.add_argument("--resume", type=Path, help="前の起動の out。同じ session・同じ権限で続きから起動する（host・cwd・model・effort・write・interrogate・audit と役割（hardest）は記録から取る）")
    a = ap.parse_args()
    a.session, a.grok_home, a.codex_turns, a.prev_head, a.scratch, a.history = None, None, 0, None, None, []
    a.codex_plugin_add = None
    if a.resume:
        try:
            prev = obj(json.loads((a.resume / "record.json").read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            ap.error(f"--resume の記録を読めない: {e!r}"[:300])
        if a.host or a.cwd or a.model or a.effort or a.write or a.interrogate or a.audit or a.hardest:
            ap.error("--resume は host・cwd・model・effort・write・interrogate・audit・hardest を前の記録から取る（違う権限で続けない）。一緒に渡さない")
        if not (prev.get("host") in ("claude", "codex", "grok") and all(isinstance(prev.get(k), str) and prev[k] for k in ("cwd", "model_requested", "effort_requested", "session"))
                and isinstance(prev.get("write"), bool)):
            ap.error(f"--resume の記録に、続きから起動するのに要るもの（host・cwd・model・effort・write・session）が無い: {a.resume}")
        if prev.get("resumable") is not True:
            ap.error(f"前の起動の記録は続きから起動できない（{(prev.get('problems') or ['理由が記録に無い'])[0]}"[:300]
                     + "）。権限の照合に当たった・止めきれないプロセスがあった・Git の管理情報が変わったなどの記録は、確かめてから最初から起動し直す: "
                     + str(a.resume))
        a.lock = hold_session(prev["session"])
        if a.lock is None:
            ap.error(f"同じ session の続きが動いている。終わるのを待つ: {a.resume}")
        a.history = session_history(prev["session"])
        banned = [r for r in a.history if UNSAFE.intersection(r.get("blocked") or [])]
        if banned:
            ap.error(f"この session は続けない（{banned[-1].get('out')} の {'・'.join(sorted(UNSAFE.intersection(banned[-1]['blocked'])))}）。確かめて、最初から起動し直す: {a.resume}"[:600])
        left = sorted({f for r in [prev, *a.history] for k in ("cwd_residue", "home_residue") for f in r.get(k) or []
                       if os.path.lexists(f) and not claude_staging_only(f)})
        if left:  # 片付けずに続けると、続きの比較で「前からあるもの」になり、ok で見えなくなる。Claude Code の置き場だけの .claude は残す（#65）
            ap.error(f"これまでの起動の空の残りを確かめて片付けてから続ける: {left[:20]}"[:600])
        swept_note = (f"launcher は、最後に使われてから {LEFTOVER_DAYS} 日を過ぎた子の一時の置き場と、もう無いタスクの作業場所だけを見ていた子の一時の置き場を消す。"
                      "続きからでなく、新しく起動する")
        busy_note = "を、ほかの launcher が使っているか片付けている。少し待って、同じ --resume をもう一度実行する"
        # 続きが使う一時の置き場の排他を、ここで取って終わりまで持つ（片付けは、排他を取れた置き場だけを消す）。無ければ続けない:
        # 無いまま続けると、空の一時ディレクトリを作り直して起動し、子は前の起動で写した対象を失ったまま続きを始める。
        # 片付けの対象の置き場からも続けない（まだ消えていなくても。片付けが途中で止まって、中身が半分かもしれない）
        if prev["host"] == "codex" and not prev["write"] and not isinstance(prev.get("scratch"), str):
            ap.error(f"--resume の記録に、読み取り専用の Codex の一時ディレクトリ（scratch）が無い: {a.resume}")
        used = {"grok": ("grok_home", " grok_home ", "（Grok は前の home の session の記録から続ける）"),
                "codex": ("scratch", "一時ディレクトリ（scratch）", "")}.get(prev["host"] if prev["host"] == "grok" or not prev["write"] else None)
        if used:
            key, label, why_needed = used
            place = Path(prev[key]) if isinstance(prev.get(key), str) else None
            held, lost = hold_place(place) if place else (None, "gone")
            if held is not None:
                PLACES_HELD.append(held)
            elif lost == "busy":
                ap.error(f"--resume の記録の{label}{busy_note}: {a.resume}")
            elif lost == "gone" or place.is_symlink() or not place.is_dir():
                ap.error(f"--resume の記録の{label}が無い{why_needed}。{swept_note}: {a.resume}")
            if place_due(place):
                ap.error(f"--resume の記録の{label}は、もう続きから起動できない。{swept_note}: {a.resume}")
        a.host, a.cwd, a.model, a.effort, a.write = prev["host"], Path(prev["cwd"]), prev["model_requested"], prev["effort_requested"], prev["write"]
        a.session, a.grok_home = prev["session"], prev.get("grok_home") and Path(prev["grok_home"])
        a.interrogate = prev.get("interrogate") is True
        a.audit = prev.get("audit") is True
        a.revisit = prev.get("revisit") if isinstance(prev.get("revisit"), str) else None  # 続きの記録にも、次の周の関門が比べる印を残す
        a.role = prev.get("role") if prev.get("role") in CHOICES else None
        # 手で書き換えた記録の role が list・dict だと TypeError で止まる（launcher の書いた記録は文字列。守らない）
        a.codex_turns, a.prev_head, a.scratch = prev.get("codex_turns") or 0, prev.get("head"), prev.get("scratch")
    elif a.write and a.interrogate:
        ap.error("--interrogate は読み取り専用の審査役に付ける。--write と一緒に渡さない")
    elif a.audit and (a.write or a.interrogate):
        ap.error("--audit は読み取り専用の監査役に付ける。--write・--interrogate と一緒に渡さない（審査役と監査役は別の子）")
    elif a.hardest and not a.write:
        ap.error("--hardest は書く子に付ける。--write と一緒に渡す")
    elif not (a.host and a.cwd):
        ap.error("--host と --cwd が要る（--resume のときは記録から取る）")
    if not a.resume:
        a.role = "audit" if a.audit else "interrogate" if a.interrogate else ("hardest" if a.hardest else "write") if a.write else "read"
        if a.host in ("claude", "grok"):
            a.session = str(uuid.uuid4())  # 起動の前に決める。時間切れで止めると、結果の JSON が出ず、session が分からなかった（Codex は出力の最初の行に出る）
        chosen = CHOICES[a.role].get(a.host)
        if not (a.model and a.effort) and not chosen:
            ap.error(f"{a.host} の {a.role} の子には既定のモデルが無い。--model と --effort を渡す")
        a.chosen_by = "given" if a.model and a.effort else "table" if not (a.model or a.effort) else "given+table"
        a.model = a.model or chosen[0]
        a.effort = a.effort or chosen[1]
    a.timeout = a.timeout or TIMEOUTS.get(a.role, 1800)
    a.cwd = a.cwd.resolve()
    if a.interrogate and not a.resume:
        why = unrevisited(a.cwd)
        if why:
            ap.error(why)
        a.revisit = revisit_of(a.cwd)[0]
    stamp = time.strftime("%Y%m%d-%H%M%S") + '-' + secrets.token_hex(8)
    # 読み取り専用の Codex の子が書ける一時ディレクトリは、--out でなく bb-writ の状態の置き場に置く（--out は呼び出し側が選ぶので、
    # その下に置くと、対象のリポジトリや Git の管理ディレクトリの中かを判定し続けることになり、判定の穴が尽きなかった）
    scratch_at = Path(a.scratch) if a.scratch else state_dir() / "delegations" / stamp / "tmp"
    scratch = scratch_at.resolve()
    if a.host == "codex" and not a.write:  # 記録を作る前に（状態の置き場が対象の下だと、記録も対象の中にできた）
        if a.cwd in scratch.parents:
            ap.error(f"bb-writ の状態の置き場 {state_dir()} が対象の中にある。読み取り専用の Codex は一時ディレクトリに書けるので、対象の外に置く")
        # Git の管理ディレクトリの中（.git の下、bare の形）なら、子がそこに ref を作れる。git は呼ばずに形で見る
        # 解く前の道筋でも見る（.git/refs を外への symlink にすると、解いた後の祖先から .git が消えた）
        unresolved = Path(os.path.abspath(scratch_at))
        if any(d.name == ".git" or all((d / n).exists() for n in ("HEAD", "objects", "refs")) for d in [*scratch.parents, *unresolved.parents]):
            ap.error(f"bb-writ の状態の置き場 {state_dir()} が Git の管理ディレクトリの中にある。読み取り専用の Codex は一時ディレクトリに書けるので、外に置く")
    swept = {"removed": [], "problems": []}
    try:
        sweep_leftovers(time.time(), swept)
    except Exception as e:
        swept["problems"].append(f"一時の置き場の片付けが失敗した: {e!r}"[:300])
    try:  # --out を渡しても、子の読みの規則（claude_reads）を組む前に作る（無い置き場は拒みに入らなかった）
        state_dir().mkdir(parents=True, exist_ok=True)
    except OSError as e:
        ap.error(f"bb-writ の状態の置き場 {state_dir()} を作れない: {e}")
    out_dir = (a.out or state_dir() / "delegations" / stamp).resolve()
    try:
        out_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        ap.error(f"--out がもうある（前の起動の記録を上書きしない）: {out_dir}")
    try:
        shutil.copyfile(a.prompt_file, out_dir / "prompt.md")
    except OSError:
        pass
    rec = {"host": a.host, "cwd": str(a.cwd), "model_requested": a.model, "effort_requested": a.effort,
           **({"role": a.role} if a.role else {}), **({"chosen_by": a.chosen_by} if not a.resume else {}), "timeout_s": a.timeout,
           "write": a.write, "interrogate": a.interrogate, "audit": a.audit, **({"revisit": a.revisit} if getattr(a, "revisit", None) else {}), "out": str(out_dir), "session_asked": a.session, **({"resumed_from": str(a.resume.resolve())} if a.resume else {}),
           "load": {"cpus": os.cpu_count(), "start": load_now()},
           **({"swept": swept} if swept["removed"] or swept["problems"] else {})}
    blocked = []
    rec["head"] = rec["saw"] = clean_head(a.cwd)  # 子を起こす前に（子が見る作業ツリー）。saw は、後で head を外しても残す

    snapshot, why, prep, login = None, None, None, None
    contract = (CONTRACT + ("" if a.write else READ_ONLY_CONTRACT) + (AUDIT_CONTRACT if a.audit else "")
                + (CLAUDE_WRITE_CONTRACT if a.host == "claude" and a.write else ""))
    try:
        if a.write:
            own, common, why = worktree_dirs(a.cwd)
            if not why and own and own == common:
                # 元の checkout（とその下・submodule）の記録の置き場にはタスクの一覧があり、Grok の子には .git 全体が渡る
                why = "書く子は task start の作業場所（worktree）で起動する。元の checkout とその下では起動しない"
            if not why and own:
                snapshot = (own, common, git_snapshot(own, common, a.cwd))
        if not why:
            cmd, env, why = command(a, a.prompt_file.read_text(encoding="utf-8") + contract, scratch)
        if a.host == "codex" and not a.write and not why:
            rec["scratch"] = str(scratch)
            if not a.resume:  # 続きは、照合のときに取ってある。最初の起動は、作った直後のここで取る（記録を書く前から、片付けに消させない）
                held, lost = hold_place(scratch)
                if held is not None:
                    PLACES_HELD.append(held)
                elif lost != "unsupported":
                    why = f"一時ディレクトリ {scratch} の排他を取れない（{lost}）"
        if a.host == "grok" and not why:
            rec["grok_home"] = env["GROK_HOME"]
        if not why:
            # Claude Code の子は、対象（子が cd した下の階層を含む）と（worktree なら）元の checkout に、設定を書くときの空の
            # .claude/.cc-writes を作る（2.1.302・2.1.330）。消さない（同じ場所の別のセッションも使い、消すと、その設定の書き込みが
            # 拒まれた。#65）。その置き場だけの .claude は、task cleanup も書く子の残り（cwd_residue）も数えない
            if a.host == "claude":
                # 専用ログインのトークンの組織を確かめる。ホームの前後の比較の「前」を取る前に済ませる（確かめのプロセスがホームに
                # 残したものを、子の残りとして数えない）。記録に書くのは、子を起動するときだけ
                login = claude_login(config_dir())
            # 子の前後でホームを比べ、新しくできて空のままの場所を残りとして報告する。黙って消さない（同じ時刻に別の道具が作ったものかもしれない）
            skip = [config_dir(), state_dir(), out_dir, a.cwd, Path.home() / ".cache"]
            home_before, _, unreadable_before = home_entries(skip)
            # 読み取り専用の子は書かず、同じ対象で並行する審査役の一時的な場所を数えてしまう。Claude Code の空の置き場だけの .claude は
            # 前からあるものに数えない（cwd_residue）
            cwd_before = {str(p) for p in a.cwd.iterdir() if not claude_staging_only(p)} if a.write else None
            had_git = os.path.lexists(a.cwd / ".git")
            if LINUX:
                become_subreaper()
    except Exception as e:
        prep = [f"起動の前の準備ができない: {e!r}"[:300], f"詳細: {launcher_error(out_dir)}"]
    if a.codex_plugin_add is not None:
        rec["codex_plugin_add"] = a.codex_plugin_add
    t0, stop = time.monotonic(), {}  # 所要時間は、時間切れの期限と同じ単調時計で測る（WSL2 の壁時計は約 35 秒ごとに約 2.3 秒飛び、所要時間が延びた）
    if prep:
        rec.update(status="failed", problems=prep)
    elif why:
        rec.update(status="unavailable", problems=[why])
    else:
        if login is not None:
            rec["claude_login"] = login
        child, left, timed_out, exit_lost = None, [], False, False
        try:
            # 子を待つ間に止められたら（中断・呼び出し側の時間の上限）、直下の子に TERM を伝え、子が片付けて終わるのを待ってから、
            # 照合して記録を書く（続きから起動できる。木ごと KILL すると、子の CLI が対象に空の置き場を残した）。子を刈った後に受けた
            # シグナルでは、記録を書き終えるまで止まらない（照合の間の Ctrl-C で後処理の git が止まると、head などが欠けうる）。
            # 起動時に無視されていたシグナル（nohup の HUP）は無視のまま。受け手は Popen の前に置く（Popen の直後に受けたものも子に伝える）
            def forward(n, _frame):
                # 刈った後のシグナルは、刈った子に伝えない。立て直すかを決める前なら立て直しをやめ（fable_unavailable）、起こし直すと
                # 決めた後なら起こす周の頭で扱う（報告を直させる続きは起こさず、立て直した子と起こし終えていた続きには伝える）。
                # どちらでもなければ記録もしない
                if stop.get("reaped"):
                    stop.setdefault("late", n)
                    return
                if "sig" in stop:
                    return
                stop["sig"] = n
                if child is not None:
                    ask_to_stop(child, stop)
            for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                if signal.getsignal(s) is not signal.SIG_IGN:
                    signal.signal(s, forward)
            # 子が残した背景のプロセスが、終了後の照合の後に書き換えないよう、プロセスグループごと止める。子の CLI が新しいセッションに
            # 切り離した子孫（Claude Code の Bash）はグループの外なので、Linux では自分の下に来たものを残らなくなるまで止める
            fixing = None
            while True:
                if fixing is not None and "late" in stop:
                    rec["stopped_by_signal"] = stop.pop("late")
                    rec["problems"] = rec["problems"] + [f"launcher がシグナル {rec['stopped_by_signal']} を受けたので、直させる続きを起こさなかった"]
                    break
                try:
                    child = subprocess.Popen(cmd, cwd=a.cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             start_new_session=True)  # バイトで受ける（子の出力は UTF-8 とは限らない）
                except (OSError, ValueError) as e:  # 続きを起こせない（引数が長すぎる・符号にできない文字）。組めないときと同じく、今の報告を返す
                    if fixing is None:
                        raise
                    rec["problems"] = rec["problems"] + [f"launcher が続きで直させられない: {e!r}"[:600]]
                    break
                # 起こし直すと決めてからここまでは、前の子を刈ったままにして、受けたシグナルを前の子の番号へ伝えない。ここから伝え、
                # その間に受けたもの（最初の子なら、受け手を置いてから起こすまでに受けたもの）も、この子に伝える
                stop.pop("reaped", None)
                if "late" in stop:
                    stop.setdefault("sig", stop.pop("late"))
                if "sig" in stop:
                    ask_to_stop(child, stop)
                rec["session"] = a.session  # 後の照合で failed になっても、確かめた後に続きから起動できるように
                output, readers = {"stdout": bytearray(), "stderr": bytearray()}, []
                def read_all(k, f):
                    while chunk := f.read1(65536):
                        output[k] += chunk
                try:
                    if fixing is not None:
                        for name in ("report.md", "stdout.jsonl", "stderr.log"):
                            (out_dir / name).replace(out_dir / name.replace(".", "-before-fix.", 1))
                        (out_dir / "fix-prompt.md").write_text(fixing, encoding="utf-8", errors="backslashreplace")
                        first = {k: rec.pop(k) for k in ("report", "execution", "deliver", "exit_code", "model_effective", "usage") if k in rec}
                        rec["report_fix"] = {"asked": rec["problems"], "prompt": str(out_dir / "fix-prompt.md"), "usage": first.get("usage")}
                        if a.host == "grok":
                            rec["grok_home"] = env["GROK_HOME"]
                        fixing = None
                    for k, f in (("stdout", child.stdout), ("stderr", child.stderr)):
                        readers.append(threading.Thread(target=read_all, args=(k, f), daemon=True))
                        readers[-1].start()
                    # 出力が閉じるのでなく、子の CLI が終わるのを待つ（子孫が出力を開いたままでも）。起こし直した子には、残りの期限だけを渡す
                    try:
                        wait_for_exit(child, a.timeout - (time.monotonic() - t0), stop)
                    except subprocess.TimeoutExpired:
                        timed_out = True
                    timed_out = timed_out or bool(stop.get("timed_out"))
                finally:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)  # Linux では、まだ刈っていない（番号は使い回されていない）
                    except ProcessLookupError:
                        pass
                    finally:
                        stop["reaped"] = True  # 番号が使い回されうる。この後のシグナルは子に伝えない
                        if LINUX:
                            try:
                                try:
                                    child.wait(timeout=10)
                                except subprocess.TimeoutExpired:
                                    exit_lost = True
                                gone, left = stop_descendants(child.pid)
                                rec["stopped_after_exit"] = rec.get("stopped_after_exit", []) + gone
                            except Exception as e:
                                rec.setdefault("stopped_after_exit", [])
                                left = [f"子の残りを確かめられない: {e!r}"[:300], f"詳細: {launcher_error(out_dir)}"]
                for t in readers:
                    t.join(timeout=0 if left else 10)
                stdout, stderr = bytes(output["stdout"]), bytes(output["stderr"])
                (out_dir / "stdout.jsonl").write_bytes(stdout)
                (out_dir / "stderr.log").write_bytes(stderr)
                if any(t.is_alive() for t in readers):
                    raise RuntimeError("子の CLI が終わった後も、ほかのプロセスが子の出力を開いたまま")
                p = subprocess.CompletedProcess(cmd, None if exit_lost else child.poll(), stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace"))
                got = {"claude": parse_claude, "codex": parse_codex, "grok": parse_grok}[a.host](p.stdout)
                budget = a.timeout - (time.monotonic() - t0)
                if "model_fallback" not in rec and budget > 0 and fable_unavailable(a, got, timed_out, stop, left):
                    (out_dir / "stdout-first.jsonl").write_bytes(stdout)
                    (out_dir / "stderr-first.log").write_bytes(stderr)
                    to = FALLBACK[a.host][a.model]
                    rec["model_fallback"] = {"from": [a.model, a.effort], "to": list(to), "why": got["errors"][:1]}
                    a.model, a.effort, a.session = to[0], to[1], str(uuid.uuid4())
                    rec.update(model_requested=a.model, effort_requested=a.effort, chosen_by="fallback", session=a.session)
                    cmd = [a.model if i and cmd[i - 1] == "--model" else a.effort if i and cmd[i - 1] == "--effort"
                           else a.session if i and cmd[i - 1] == "--session-id" else x for i, x in enumerate(cmd)]
                    continue
                unsafe = [l for l in p.stderr.splitlines() if a.host == "grok" and GROK_WARNING.search(l)][-5:]
                rec.update(exit_code=p.returncode, session=got["session"] or a.session, model_effective=got["model_effective"], usage=got["usage"])
                asked = re.sub(r"\[.*\]$", "", a.model).lower()  # 「opus[1m]」の付記と大文字小文字は問わない
                if a.host == "claude" and got["model_effective"] and not any(asked in m.lower() for m in got["model_effective"]):
                    rec["model_substituted"] = True
                breach = a.host == "codex" and not a.write and codex_breach(got.get("permissions", [])[a.codex_turns:], a.codex_denies)
                if breach is None and a.codex_turns and got["rollouts"] != 1:  # 前の turn を除けるのは、同じ記録ファイルに足したときだけ（0.164.0 の実物）
                    breach = f"続きの Codex の記録ファイルが 1 つでない（{got['rollouts']} 個）。前の起動で照らした turn を除けず、実効の権限を確かめられない"
                if a.host == "codex" and not a.write and not breach:
                    rec["codex_turns"] = len(got["permissions"])
                stopped = ([] if not timed_out else [f"{a.timeout} 秒で終わらなかった"] if stop.get("timed_out")
                           else ["シグナルを受けて TERM を伝えた後、終わらなかったので止めた"])
                tail = [l for l in p.stderr.strip().splitlines() if "injected env" not in l and "Reading additional input" not in l][-5:]
                ended = [f"終了コード {p.returncode}"] + got["errors"] + tail if not timed_out and (p.returncode != 0 or got.get("is_error")) else []
                # 続きの session は、子の結果で確かめられたときだけ前と同じとみなす（時間切れとシグナルで止めた Claude Code・Grok は結果を出さない）
                other = a.resume and (got["session"] or not (timed_out or "sig" in stop)) and got["session"] != a.session
                doubts = ((["Grok の sandbox が約束どおりに効いたか分からない（警告）"] + unsafe if unsafe else [])
                          + ([f"続きの子の session を確かめられないか、前と違う（{got['session']}）"] if other else []) + ([breach] if breach else []))
                # 続けさせない理由は、選ばれた status の枝でなく、事実から決める（利用枠の枝が先に決まっても残す）
                blocked += [k for k, fact in (("unsafe", unsafe), ("other-session" if got["session"] else "session", other),
                                              ("unchecked" if breach and breach.startswith(UNCHECKED) else "breach", breach)) if fact]
                # 利用枠・権限の照合は、時間切れより先に見る（権限の約束どおりに動いたか分からない子を、続きから起動させない）。
                # 利用枠の文言を照らすのは、子が成功しなかったときだけ（成功した子の報告を、stderr の「429」「rate limit」などの一語で捨てていた）
                cli_failed = p.returncode != 0 or got.get("is_error") or not got["text"]
                if (timed_out or cli_failed) and QUOTA.search(p.stderr[-4000:] + " ".join(got["errors"])):
                    rec.update(status="quota", problems=["利用枠の上限に当たった（別料金の API へは切り替えない）"] + doubts + ended + stopped)
                elif unsafe or other or (timed_out and breach):
                    rec.update(status="failed", problems=doubts + ended + stopped)
                elif timed_out:
                    rec.update(status="timeout", problems=stopped + ([] if rec["session"] else ["session が分からず、続きから起動できない"]))
                elif cli_failed:
                    rec.update(status="failed", problems=(ended or [f"終了コード {p.returncode}"] + got["errors"] + tail) + doubts)
                elif breach:
                    rec.update(status="failed", problems=[breach])
                    (out_dir / "report.md").write_text(got["text"], encoding="utf-8", errors="backslashreplace")
                else:
                    report, problems = check_report(got["text"], a.cwd, a.audit)
                    ran = check_execution(report)[0] if report is not None else None
                    rec.update(status="invalid-report" if problems else "ok", report=report, problems=problems, execution=ran)
                    if a.audit and not problems:
                        rec["deliver"] = report["deliver"]
                    if not problems and ran in (None, "no") and not a.write:
                        rec.update(status="not-run", problems=["対象の試験・検査を実行できなかった（読んだだけの報告）" if ran == "no"
                                                               else "報告に実行の欄（execution）が無い（読んだだけの報告として扱う）"])
                    (out_dir / "report.md").write_text(got["text"], encoding="utf-8", errors="backslashreplace")
                # 照合に落ちた報告は、同じ session の続きで 1 回だけ、落ちた点を名指しして直させる（照合は緩めない。続きの依頼文は
                # fix_prompt）。止められた・止めきれない子孫が残った・続けさせない理由がある・期限が FIX_BUDGET より残っていない子は、
                # 直させない。刈った後に受けたシグナルは、続きを組む前（command() は Codex の排他の待ちや Grok の home の写しを含む）と、
                # 組んでいる間に届いた分を続きを起こす前（周の頭）に見る
                if (rec["status"] != "invalid-report" or "report_fix" in rec or not rec["session"] or budget < FIX_BUDGET
                        or blocked or left or exit_lost or "sig" in stop):
                    break
                if "late" in stop:
                    rec["stopped_by_signal"] = stop.pop("late")
                    rec["problems"] = rec["problems"] + [f"launcher がシグナル {rec['stopped_by_signal']} を受けたので、直させる続きを起こさなかった"]
                    break
                if not a.resume:  # 続きの子が前に見たのは、この起動で確かめた commit（終わりの head の照合で、前の起動の head として使う）
                    a.prev_head = rec["head"]
                a.resume, a.session, a.codex_turns = a.resume or out_dir, rec["session"], rec.get("codex_turns", a.codex_turns)
                a.grok_home = Path(rec["grok_home"]) if a.host == "grok" else a.grok_home
                fixing = fix_prompt(rec["problems"], a.cwd)
                cmd, env, why = command(a, fixing + contract, scratch)
                if why:
                    rec["problems"] = rec["problems"] + [f"launcher が続きで直させられない: {why}"[:600]]
                    break
            if "report_fix" in rec and rec["status"] not in ("ok", "not-run"):
                rec["problems"] = rec.get("problems", []) + ["launcher が同じ session の続きで直させる前の 1 回目の報告の問題: "
                                                             + " / ".join(rec["report_fix"]["asked"])[:2000]]
        except Exception as e:
            what = "子を起動できない" if child is None else "子の結果を読めない"
            blocked.append("error")
            rec.update(status="failed", problems=[f"{what}: {e!r}"[:300], f"詳細: {launcher_error(out_dir)}"])
        finally:
            if left:
                blocked.append("left")
                rec.update(status="failed", problems=["子の CLI が終わった後、子が起こしたプロセスを止められない。下のプロセスを確かめて止める"]
                           + left[:20] + rec.get("problems", []))
            if a.write and snapshot is None and not had_git and os.path.lexists(a.cwd / ".git"):
                # 子が作ったリポジトリの設定（filter など）は、次の起動の clean_head や、後の git が走らせる（#64）
                blocked.append("git")
                rec.update(status="failed", problems=["Git でなかった対象に、書く子がリポジトリ（.git）を作った。この場所で git を動かす前に"
                                                      "（launcher の起動も含む）、その設定と hooks を確かめる（偽の設定が sandbox の外で動きうる）",
                                                      str(a.cwd / ".git")] + rec.get("problems", []))
            if snapshot:
                try:
                    # Claude Code の sandbox は、worktree の管理ディレクトリに保護のための空の config.worktree を作る（2.1.302）。
                    # 起動前に無く空のままのファイルは、中身が無く git の動きを変えないので、消してから照合する
                    for f, h in snapshot[2]["files"].items():
                        if h is None and Path(f).is_file() and not Path(f).is_symlink() and Path(f).stat().st_size == 0:
                            Path(f).unlink()
                    changed = git_changes(snapshot[2], git_snapshot(*snapshot[:2], a.cwd), snapshot[1])
                    headline = "子が、自分のブランチ以外の Git の管理情報を変えた。"
                except Exception as e:
                    changed = [f"Git の管理情報を読めない（子が壊したかもしれない）: {e!r}"[:300], f"詳細: {launcher_error(out_dir)}"]
                    headline = "子の後に Git の管理情報を照合できない。"
                if changed:
                    blocked.append("git")
                    rec.update(status="failed", problems=[headline + "この作業場所で git を動かす前に、下の場所を確かめる（偽の設定が sandbox の外で動きうる）"]
                               + changed[:20] + rec.get("problems", []))
            if cwd_before is not None:
                try:
                    rec["cwd_residue"] = cwd_residue(a.cwd, cwd_before)
                except Exception as e:
                    rec.setdefault("cwd_residue", [])
                    blocked.append("cwd")
                    rec.update(status="failed", problems=rec.get("problems", []) + [f"対象の残りを確かめられない: {e!r}"[:600],
                                                                                     f"詳細: {launcher_error(out_dir)}"])
                if rec["cwd_residue"]:
                    rec.update(status="failed", problems=rec.get("problems", []) + [
                        "書く子の起動の後、対象の直下に、中身の空の場所が新しく残った（Claude Code 2.1.310 の sandbox は、時間切れで止めると"
                        "置き場所を残した）。子が意図して作ったもの（空の __init__.py など）でないか確かめ、違えば消す。0 バイトのファイルは "
                        "task cleanup が拒否し、git add -A で commit に入る"] + rec["cwd_residue"][:20])
            try:
                _, empty, unreadable = home_entries(skip)
                rec["home_residue"] = sorted(f for f in empty - home_before if not cli_place(Path(f)))
                if unreadable - unreadable_before:
                    raise PermissionError(f"読めない場所が増えた: {sorted(unreadable - unreadable_before)[:20]}")
            except Exception as e:
                rec.setdefault("home_residue", [])
                blocked.append("home")
                rec.update(status="failed", problems=[f"ホームの残りを確かめられない: {e!r}"[:600], f"詳細: {launcher_error(out_dir)}"]
                           + rec.get("problems", []))
            if rec["home_residue"]:
                rec.update(status="failed", problems=["子の起動の後、ホームに空のファイル・ディレクトリが新しく残った。中身を確かめて消す"
                                                      "（空の ~/.bash_profile があると、ログインシェルが ~/.profile と ~/.bashrc を読まない）"]
                           + rec["home_residue"][:20] + rec.get("problems", []))
    if rec["head"] and a.write:
        rec.update(head=None, head_lost="書く子の後は、HEAD と作業ツリーを確かめない（work.py は書く子の head を数えない）")
    elif rec["head"] and clean_head(a.cwd) != rec["head"]:
        rec.update(head=None, head_lost="子の起動の前に確かめた後、終わりまでに対象の HEAD か作業ツリーが変わったか、終わりに確かめられなかった")
    elif rec["head"] and a.resume and (seen := next((h for h in [a.prev_head, *(r["saw"] for r in a.history if "saw" in r)] if h != rec["head"]), rec["head"])) != rec["head"]:
        rec.update(head=None, head_lost=f"前の起動の head（{seen}）と違う。続きの子は前の起動で別のものを見た")
    if rec["status"] == "ok" and not a.write and rec["head"]:
        rec["next"] = ("審査の指摘なら、直す前に、作業の記録（claim の note.path）の「指摘の扱い」の欄に、指摘ごとに"
                       "対応する・記録のみ・退ける と理由を書く（入口の条件 (6)。push・pr はこの欄が空なら拒否される）"
                       + (("。監査役の判定は届けてよい（deliver: yes）。報告が挙げた整える点は、直して記録に書く（監査は立て直さない）"
                           if rec.get("deliver") == "yes" else
                           "。監査役の判定は差し戻し（deliver: no。push・pr の関門に数えない）。直して監査役を立て直すときは、前の監査の項目と、"
                           "前の監査の commit から HEAD までの差分だけを判定させる（手順の文書の文の照合だけは、毎回、変えた文書の全体）")
                          if a.audit else
                          "。直した後、次の審査役の前に「直しの見直し」の欄へ、同じ性質の箇所・直しが前提を変えた箇所・変えた節の全体の矛盾を"
                          "探した方法と結果を書き足す（interrogate の 5）"
                          "。監査役（--audit）は、扱いを書き、直しを commit し終えて HEAD を動かさないと決めてから立てる"
                          "（審査役と同じコマンドで続けて立てない）")
                       + "（`playbook-verify` の「返す前の判定」）")
    elif rec["status"] == "not-run" and rec.get("execution") == "no" and rec["head"]:
        rec["next"] = (("監査役の判定は差し戻し（deliver: no）。挙がった項目を閉じてから立て直す。" if rec.get("deliver") == "no" else "")
                       + "子が対象の試験・検査を実行できなかった。写し方（`mktemp -d` で作ったディレクトリに写して動かす）や依頼文の誤りなら、直して立て直す。"
                       "子の sandbox のせいで実行できない（headless Chrome・Node の子プロセス・127.0.0.1 など）なら、親が同じ commit で同じ試験を"
                       "実行して証拠（コマンド・終了コード・出力）を作業の記録に書き、push・pr の work.py op run に --not-run <子が実行できなかった理由> "
                       "を付ける（関門の外で出さない。監査役には親の証拠を判定させる。`playbook-deliver`）")
    rec["duration_s"] = round(time.monotonic() - t0, 1)
    rec["load"]["end"] = load_now()
    rec["at"] = round(time.time(), 3)  # 起動の終わり。一時の置き場を最後に使った時刻として、片付けの期限に使う（sweep_leftovers）
    if "sig" in stop:  # 止められた。ok で返った報告はそのまま使う（子が TERM の前に書き終えていた）
        rec["stopped_by_signal"] = stop["sig"]
        if rec["status"] != "ok":
            rec["problems"] = [f"launcher がシグナル {stop['sig']} を受けて、子に TERM を伝えて止めた"] + rec.get("problems", [])
    rec["blocked"] = sorted(set(blocked))
    rec["resumable"] = bool(rec.get("session")) and not blocked
    try:
        json.dumps(rec, ensure_ascii=False, indent=1)
    except (RecursionError, ValueError) as e:
        for d in (rec, rec.get("report_fix") or {}):
            d.update({k: None for k in ("report", "usage", "model_effective") if k in d})
        rec.update(status="failed", resumable=False, problems=[f"子の結果を記録に書けない（外した）: {e!r}"[:300]] + rec.get("problems", []))
        rec.pop("next", None)
    if rec["resumable"] and rec["status"] != "ok":
        rec["problems"] = rec.get("problems", []) + [f"--resume {out_dir} で続きから起動できる"]
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
