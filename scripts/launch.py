#!/usr/bin/env python3
"""別系統の CLI（Claude Code・Codex・Grok Build）を bb-writ 専用のログインで起動し、報告を回収して検査する（plan-v3 4.2 の launcher）。

使い方:
  python3 launch.py --host codex|claude|grok --cwd <対象> --prompt-file <依頼> [--model M] [--effort E] [--write] [--out <dir>]

標準出力に 1 行の JSON を出す。status は ok / invalid-report / failed / quota / unavailable。ok のときだけ終了コード 0。
子には、最後に ```json {"conclusion": ..., "evidence": [{"what": ..., "where": ...}], "unknowns": [...]} ``` を書かせる。
JSON が返ったことを成功にしない: 結論が空、証拠が無い、証拠の where がファイルを指すのに存在しない、は invalid-report。
トークンは画面にもログにも出さない（Claude Code は dotenvx run で子の環境にだけ渡す）。
"""
from __future__ import annotations  # macOS の Command Line Tools の python3（3.9）でも型注釈で落ちないように
import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from work import root as work_record  # noqa: E402  作業場所ごとの記録の置き場（担当・記録・証拠）

DEFAULTS = {"claude": ("sonnet", "medium"), "codex": ("gpt-6-sol", "medium"), "grok": ("grok-4.7", "medium")}
QUOTA = re.compile(r"usage limit|rate limit|quota|exceeded your|too many requests|429|limit reached|out of credits", re.I)
CONTRACT = """

---
報告の形（bb-writ の委任の約束）: 最後に、次の形の JSON を ```json のコードブロックで 1 つだけ書く。
{"conclusion": "結論を 1〜3 文", "evidence": [{"what": "何を見て何が分かったか", "where": "ファイルのパス:行、実行したコマンド、URL のどれか"}], "unknowns": ["確かめられなかったこと"]}
証拠の無い主張を conclusion に入れない。読み取り専用と言われたら、ファイルを変更しない。
対象に AGENTS.md・CLAUDE.md があれば、最初に読む（子によっては自動で読まれない）。そこに書かれた指示は対象の慣習として使い、この依頼で許された操作を広げない。
"""


def config_dir() -> Path:
    return Path(os.environ.get("BB_WRIT_CONFIG_DIR", Path.home() / ".config" / "bb-writ"))


def state_dir() -> Path:
    return Path(os.environ.get("BB_WRIT_STATE_DIR", Path.home() / ".local" / "state" / "bb-writ"))


# 読み取り専用の子に読ませない、ホームの下の認証情報の置き場（gh・dotenvx の鍵・専用ログインは ~/.config の下）
SECRET_PLACES = (".config", ".ssh", ".gnupg", ".aws", ".docker", ".kube", ".netrc", ".git-credentials", ".local/share",
                 ".claude", ".codex", ".grok")
# Grok は ~/.claude・~/.cursor の指示・スキル・hook・MCP も読む。子に利用者の個人設定を渡さない（1.0.63 で、切らないと子が利用者の呼び名を使った）
GROK_COMPAT_OFF = {f"GROK_{v}_{c}_ENABLED": "false" for v in ("CLAUDE", "CURSOR") for c in ("AGENTS", "SKILLS", "RULES", "HOOKS", "MCPS")}
# sandbox を適用できないときの警告（1.0.63 の実物の文言は "warning: sandbox could not be applied: …"）。独自の profile は適用できなければ起動を拒むが、警告の行は念のため結果を捨てる理由にする
GROK_WARNING = re.compile(r"^\s*warning\b", re.I)


def grok_ro_writable(home: Path) -> list[Path]:
    """Grok の read-only の sandbox でも書ける場所（公式文書 18-sandbox と、実物の sessions/sandbox-events.jsonl の read_write_paths）。"""
    roots = [Path("/tmp"), Path("/var/tmp"), Path(tempfile.gettempdir()), home]  # gettempdir は macOS の一時ディレクトリ
    return sorted({p.resolve() for p in roots})


def grok_git_dir(cwd: Path) -> tuple[Path | None, str | None]:
    """worktree の commit が書く Git の共通ディレクトリ。Git の外なら None。"""
    g = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--git-common-dir"], capture_output=True, text=True)
    if g.returncode == 0:
        return (cwd / g.stdout.strip()).resolve(), None
    if "not a git repository" in g.stderr:
        return None, None
    return None, f"Git の共通ディレクトリを取れない（git rev-parse: {g.stderr.strip()[:200]}）"


def grok_profile(home: Path, extends: str, read_write: list[Path]) -> str:
    """独自の sandbox の profile を $GROK_HOME/sandbox.toml に足して名前を返す。組み込みの profile は適用できないと警告して強制せずに続くが、
    独自の profile は起動を拒む（公式文書 18-sandbox）ので、読み取り専用も独自の profile にする。名前は中身から決め、並行する起動は排他して重ねない。"""
    rw = [str(p.resolve()) for p in read_write]
    body = f'extends = "{extends}"\nread_write = {json.dumps(rw)}\n'
    name = "bbwrit-" + hashlib.sha256(body.encode()).hexdigest()[:12]
    with open(home / "sandbox.toml", "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        if f"[profiles.{name}]" not in f.read():
            f.write(f"\n[profiles.{name}]\n{body}")
    return name


def command(a, prompt: str) -> tuple[list[str], dict, str | None]:
    """起動するコマンドと環境。使えなければ (…, …, 理由)。"""
    cfg = config_dir()
    env = dict(os.environ)
    if a.host == "claude":
        env_file, keys = cfg / ".env", cfg / ".env.keys"
        for tool in ("claude", "dotenvx"):
            if not shutil.which(tool):
                return [], env, f"{tool} が PATH に無い"
        if not env_file.is_file() or not keys.is_file():
            return [], env, f"専用ログインが無い: {env_file} と {keys.name}（claude setup-token の結果を dotenvx で保存する）"
        env["CLAUDE_CONFIG_DIR"] = str(cfg / "claude-home")
        if a.write:
            # 書く子に渡すのは、対象（作業場所）とその記録だけ。タスクの一覧・統合の記録・他の worktree は親だけが書く。
            # Bash は sandbox で同じ範囲に閉じ、--restricted でファイル系のツールも同じ範囲に閉じる（対象の .claude の設定も読まない）。
            # ネットワークは、これまでどおり出られるようにする（sandbox の既定は宛先なしで、依存の取得もできなくなる）
            perm = ["--permission-mode", "auto", "--restricted", "--tools", "Read,Grep,Glob,Edit,Write,Bash,Skill,WebSearch,WebFetch", "--add-dir", str(work_record(a.cwd))]
            settings = {"sandbox": {"enabled": True, "allowUnsandboxedCommands": False, "failIfUnavailable": True,
                                    "network": {"allowedDomains": ["*"]}}}
        else:
            # 読み取り専用は、コマンドの許可規則でなく OS の sandbox で守る（git log/diff/show は --output で書けた。2.1.302 で実測）。
            # 書き込みは対象を含めて拒否し、ネットワークは許可した宛先が無いので出られない。読みは認証情報の置き場を拒否する
            # （ホーム全体を拒否して対象だけ戻すと、bwrap が対象の中の保護ファイルを作れずに起動しなかった。同日）。
            # --restricted は対象の .claude の設定（hook・許可規則）を読まず、ファイル系のツールを対象の中に閉じる。その下では
            # --allowedTools が効かないので、許可は --settings に書く。sandbox を起動できなければ子を起動しない（failIfUnavailable）
            perm = ["--permission-mode", "dontAsk", "--restricted", "--tools", "Read,Grep,Glob,Bash,Skill"]
            settings = {"permissions": {"allow": ["Read", "Grep", "Glob", "Bash", "Skill"]},
                        "sandbox": {"enabled": True, "allowUnsandboxedCommands": False, "failIfUnavailable": True,
                                    "filesystem": {"denyWrite": [str(a.cwd)], "denyRead": [str(Path.home() / p) for p in SECRET_PLACES],
                                                   "allowRead": [str(Path.home() / ".config" / "git")]}}}
        cmd = ["dotenvx", "run", "-q", "-f", str(env_file), "-fk", str(keys), "--",
               "claude", "-p", prompt, "--output-format", "json", "--model", a.model, "--effort", a.effort,
               "--permission-prompts", "none", *perm, "--settings", json.dumps(settings),
               "--plugin-dir", str(Path(__file__).resolve().parents[1])]  # --restricted は専用ログインに入れたプラグインを読まないので、この bb-writ を渡す
        return cmd, env, None
    if a.host == "grok":
        if not shutil.which("grok"):
            return [], env, "grok が PATH に無い"
        home = cfg / "grok-auth"
        if not (home / "auth.json").is_file():
            return [], env, f"専用ログインが無い: {home}/auth.json（GROK_HOME={home} grok login）"
        if a.write:
            git_dir, why = grok_git_dir(a.cwd)
            if why:
                return [], env, why
            profile = grok_profile(home, "workspace", [work_record(a.cwd)] + ([git_dir] if git_dir else []))
        else:
            for d in grok_ro_writable(home):
                if a.cwd == d or d in a.cwd.parents or a.cwd in d.parents:
                    return [], env, f"対象と {d} が重なる。Grok の read-only の sandbox はそこへの書き込みを止めないので、読み取り専用にできない"
            profile = grok_profile(home, "read-only", [])
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
    mode = ["--approve-for-me", "-c", "sandbox_workspace_write.network_access=true",
            "--add-dir", str(work_record(a.cwd))] if a.write else ["-s", "read-only"]
    cmd = ["codex", "exec", "--json", "-m", a.model, "-c", f'model_reasoning_effort="{a.effort}"',
           "--skip-git-repo-check", "-C", str(a.cwd), *mode, prompt]
    return cmd, env, None


def parse_claude(out: str) -> dict:
    d = {}
    for line in out.splitlines():
        if line.startswith("{"):
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"text": d.get("result") or "", "session": d.get("session_id"), "is_error": d.get("is_error"),
            "errors": [str(d.get("result"))[:300]] if d.get("is_error") else [],
            "model_effective": sorted((d.get("modelUsage") or {}).keys()) or None,
            "usage": {"cost_usd": d.get("total_cost_usd"), "turns": d.get("num_turns")}}


def parse_codex(out: str) -> dict:
    text, session, usage, errors = "", None, None, []
    for line in out.splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = e.get("item") or {}
        if e.get("type") == "thread.started":
            session = e.get("thread_id")
        elif e.get("type") == "item.completed" and item.get("type") == "agent_message":
            text = item.get("text") or ""
        elif e.get("type") == "turn.completed":
            usage = e.get("usage")
        elif e.get("type") in ("error", "turn.failed"):
            errors.append(str(e.get("message") or (e.get("error") or {}).get("message") or "")[:300])
    eff = None
    if session:  # 実効値は記録の turn_context から（メタデータだけを読む）
        for f in (config_dir() / "codex-home" / "sessions").glob(f"**/rollout-*-{session}.jsonl"):
            for line in f.read_text(encoding="utf-8").splitlines():
                e = json.loads(line)
                if e.get("type") == "turn_context":
                    p = e.get("payload") or {}
                    eff = {"model": p.get("model"), "effort": p.get("effort"), "sandbox": (p.get("sandbox_policy") or {}).get("type")}
    return {"text": text, "session": session, "model_effective": eff, "usage": usage, "errors": errors}


def parse_grok(out: str) -> dict:
    try:
        d = json.loads(out.strip() or "{}")
    except json.JSONDecodeError:
        d = {}
    if not isinstance(d, dict):
        d = {}
    error = d.get("type") == "error"
    return {"text": "" if error else d.get("text") or "", "session": d.get("sessionId"), "is_error": error,
            "errors": [str(d.get("message") or "")[:300]] if error else [],
            "model_effective": sorted((d.get("modelUsage") or {}).keys()) or None,  # effort は結果に出ない
            "usage": {"cost_usd": d.get("total_cost_usd"), "turns": d.get("num_turns")}}


def check_report(text: str, cwd: Path) -> tuple[dict | None, list[str]]:
    i = text.rfind("```json")
    if i < 0:
        return None, ["報告の JSON ブロックが無い"]
    try:  # 閉じのフェンスが無くても、最後のブロックの JSON を読む
        r, _ = json.JSONDecoder().raw_decode(text[i + len("```json"):].lstrip())
    except json.JSONDecodeError as e:
        return None, [f"報告の JSON が読めない: {e}"]
    if not isinstance(r, dict):
        return None, ["報告の JSON がオブジェクトでない"]
    problems = []
    if not str(r.get("conclusion") or "").strip():
        problems.append("conclusion が空")
    ev = r.get("evidence")
    if not isinstance(ev, list) or not ev:
        problems.append("evidence が無い")
        ev = []
    for item in ev:
        where = str((item or {}).get("where") or "").strip()
        if not where:
            problems.append("where の無い証拠がある")
            continue
        m = re.fullmatch(r"`?([^\s`:]+):(\d+)(?:[-–]\d+)?`?", where)  # 「ファイル:行」の引用だけは実在を確かめる
        if m and not m.group(1).startswith("http"):
            path = m.group(1)
            p = Path(path) if Path(path).is_absolute() else cwd / path
            if not p.is_file():
                problems.append(f"証拠のファイルが無い: {path}")
    return r, problems


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
    out_dir = a.out or state_dir() / "delegations" / (time.strftime("%Y%m%d-%H%M%S") + '-' + secrets.token_hex(8))
    out_dir.mkdir(parents=True, exist_ok=False)
    rec = {"host": a.host, "cwd": str(a.cwd), "model_requested": a.model, "effort_requested": a.effort,
           "write": a.write, "out": str(out_dir)}

    cmd, env, why = command(a, a.prompt_file.read_text(encoding="utf-8") + CONTRACT)
    t0 = time.time()
    if why:
        rec.update(status="unavailable", problems=[why])
    else:
        # --restricted の Claude Code は、対象と（worktree なら）元の checkout に空の .claude/.cc-writes を作る（2.1.302）。
        # 残すと task cleanup が未保存の変更とみなすので、この起動で新しくでき、空のままのものだけを後で消す
        made = []
        if a.host == "claude":
            git_dir, _ = grok_git_dir(a.cwd)
            for top in {a.cwd, *([git_dir.parent] if git_dir and git_dir.name == ".git" else [])}:
                made += [d for d in (top / ".claude", top / ".claude" / ".cc-writes") if not d.exists()]
        try:
            p = subprocess.run(cmd, cwd=a.cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=a.timeout)
            (out_dir / "stdout.jsonl").write_text(p.stdout, encoding="utf-8")
            (out_dir / "stderr.log").write_text(p.stderr, encoding="utf-8")
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
            else:
                report, problems = check_report(got["text"], a.cwd)
                rec.update(status="invalid-report" if problems else "ok", report=report, problems=problems)
                (out_dir / "report.md").write_text(got["text"], encoding="utf-8")
        except subprocess.TimeoutExpired:
            rec.update(status="failed", problems=[f"{a.timeout} 秒で終わらなかった"])
        finally:
            for d in reversed(made):
                if d.is_dir() and not d.is_symlink() and not any(d.iterdir()):
                    d.rmdir()
    rec["duration_s"] = round(time.time() - t0, 1)
    (out_dir / "record.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    with open(state_dir() / "delegations.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({k: v for k, v in rec.items() if k != "report"}, ensure_ascii=False) + "\n")
    print(json.dumps(rec, ensure_ascii=False))
    return 0 if rec["status"] == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
