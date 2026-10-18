#!/usr/bin/env python3
"""別系統の CLI（Claude Code・Codex）を bb-writ 専用のログインで起動し、報告を回収して検査する（plan-v3 4.2 の launcher）。

使い方:
  python3 launch.py --host codex|claude --cwd <対象> --prompt-file <依頼> [--model M] [--effort E] [--write] [--out <dir>]

標準出力に 1 行の JSON を出す。status は ok / invalid-report / failed / quota / unavailable。ok のときだけ終了コード 0。
子には、最後に ```json {"conclusion": ..., "evidence": [{"what": ..., "where": ...}], "unknowns": [...]} ``` を書かせる。
JSON が返ったことを成功にしない: 結論が空、証拠が無い、証拠の where がファイルを指すのに存在しない、は invalid-report。
トークンは画面にもログにも出さない（Claude Code は dotenvx run で子の環境にだけ渡す）。
"""
from __future__ import annotations  # macOS の Command Line Tools の python3（3.9）でも型注釈で落ちないように
import argparse
import json
import os
import re
import shutil
import secrets
import subprocess
import sys
import time
from pathlib import Path

DEFAULTS = {"claude": ("sonnet", "medium"), "codex": ("gpt-6-sol", "medium")}
QUOTA = re.compile(r"usage limit|rate limit|quota|exceeded your|too many requests|429|limit reached|out of credits", re.I)
CONTRACT = """

---
報告の形（bb-writ の委任の約束）: 最後に、次の形の JSON を ```json のコードブロックで 1 つだけ書く。
{"conclusion": "結論を 1〜3 文", "evidence": [{"what": "何を見て何が分かったか", "where": "ファイルのパス:行、実行したコマンド、URL のどれか"}], "unknowns": ["確かめられなかったこと"]}
証拠の無い主張を conclusion に入れない。読み取り専用と言われたら、ファイルを変更しない。
"""


def config_dir() -> Path:
    return Path(os.environ.get("BB_WRIT_CONFIG_DIR", Path.home() / ".config" / "bb-writ"))


def state_dir() -> Path:
    return Path(os.environ.get("BB_WRIT_STATE_DIR", Path.home() / ".local" / "state" / "bb-writ"))


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
        perm = ["--permission-mode", "auto", "--add-dir", str(state_dir())] if a.write else [
            "--permission-mode", "dontAsk", "--allowedTools", "Read", "Grep", "Glob",
            "Bash(git log:*)", "Bash(git diff:*)", "Bash(git show:*)", "Bash(git status:*)", "Bash(ls:*)"]
        cmd = ["dotenvx", "run", "-q", "-f", str(env_file), "-fk", str(keys), "--",
               "claude", "-p", prompt, "--output-format", "json", "--model", a.model, "--effort", a.effort,
               "--permission-prompts", "none", *perm]
        return cmd, env, None
    if not shutil.which("codex"):
        return [], env, "codex が PATH に無い"
    home = cfg / "codex-home"
    if not (home / "auth.json").is_file():
        return [], env, f"専用ログインが無い: {home}/auth.json（CODEX_HOME={home} codex login）"
    env["CODEX_HOME"] = str(home)
    mode = ["--approve-for-me", "-c", "sandbox_workspace_write.network_access=true",
            "--add-dir", str(state_dir())] if a.write else ["-s", "read-only"]
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
    ap.add_argument("--host", choices=("claude", "codex"), required=True)
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
        try:
            p = subprocess.run(cmd, cwd=a.cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=a.timeout)
            (out_dir / "stdout.jsonl").write_text(p.stdout, encoding="utf-8")
            (out_dir / "stderr.log").write_text(p.stderr, encoding="utf-8")
            got = parse_claude(p.stdout) if a.host == "claude" else parse_codex(p.stdout)
            rec.update(exit_code=p.returncode, session=got["session"], model_effective=got["model_effective"], usage=got["usage"])
            if QUOTA.search(p.stderr[-4000:] + " ".join(got["errors"])):
                rec.update(status="quota", problems=["利用枠の上限に当たった（別料金の API へは切り替えない）"])
            elif p.returncode != 0 or got.get("is_error") or not got["text"]:
                tail = [l for l in p.stderr.strip().splitlines() if "injected env" not in l and "Reading additional input" not in l][-5:]
                rec.update(status="failed", problems=[f"終了コード {p.returncode}"] + got["errors"] + tail)
            else:
                report, problems = check_report(got["text"], a.cwd)
                rec.update(status="invalid-report" if problems else "ok", report=report, problems=problems)
                (out_dir / "report.md").write_text(got["text"], encoding="utf-8")
        except subprocess.TimeoutExpired:
            rec.update(status="failed", problems=[f"{a.timeout} 秒で終わらなかった"])
    rec["duration_s"] = round(time.time() - t0, 1)
    (out_dir / "record.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    with open(state_dir() / "delegations.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({k: v for k, v in rec.items() if k != "report"}, ensure_ascii=False) + "\n")
    print(json.dumps(rec, ensure_ascii=False))
    return 0 if rec["status"] == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
