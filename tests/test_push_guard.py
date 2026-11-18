"""入口を呼んだセッションで、関門（work.py op run）を通さない外へ出すコマンドを拒否する hook（scripts/push_guard.py）。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent)); import process_limit  # noqa: E402,F401  試験に上限を掛ける（直に走らせても）

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "push_guard.py"
HOOKS = ROOT / "hooks" / "hooks.json"

# 入口を呼んだ印の、会話記録の行（2026-11-18、Claude Code 2.1.327 の記録の形）
BY_SKILL_TOOL = {"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "tool_use", "id": "toolu_1", "name": "Skill", "input": {"skill": "bb-writ:bb-writ"}}]}}
BY_SLASH = {"type": "user", "message": {"role": "user", "content":
    "<command-message>bb-writ:bb-writ</command-message>\n<command-name>/bb-writ:bb-writ</command-name>\n<command-args>直して</command-args>"}}
OTHER_SKILL = {"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "tool_use", "id": "toolu_2", "name": "Skill", "input": {"skill": "bb-writ:playbook-docs-skill"}}]}}
MENTION_ONLY = {"type": "user", "message": {"role": "user", "content": "bb-writ:bb-writ の使い方を教えて"}}

OUTWARD = ["git push origin main", "GH_CONFIG_DIR=x git -C . push -q origin HEAD:main", "cd repo && git -c user.name=w push",
           "git status && git push", "env GIT_TERMINAL_PROMPT=0 git push", "gh pr create --fill", "gh pr merge 3 --squash",
           "gh release create v1 --notes-file n.md"]
NOT_OUTWARD = ["python3 /p/scripts/work.py op run --dir . --key push:origin/main@abc -- git push origin main",
               'git commit -m "docs: git push の説明を直す"', 'echo "次に git push する"', "git status", "gh pr view 3",
               "git pushx", "gh issue create --title t"]


class TestPushGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def transcript(self, *rows) -> str:
        p = Path(self.tmp.name) / "session.jsonl"
        p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows) + '{"broken\n')
        return str(p)

    def run_guard(self, command, transcript, tool="Bash", raw=None):
        stdin = raw if raw is not None else json.dumps({"session_id": "s", "transcript_path": transcript, "cwd": self.tmp.name,
                                                        "hook_event_name": "PreToolUse", "tool_name": tool,
                                                        "tool_input": {"command": command}})
        p = subprocess.run([sys.executable, "-B", str(GUARD)], input=stdin, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)  # 拒否も JSON で返す。終了コードで止めない（ほかの失敗と区別する）
        return json.loads(p.stdout)["hookSpecificOutput"] if p.stdout.strip() else None

    def test_outward_commands_are_denied_in_a_session_that_used_the_entry(self):
        for entry in (BY_SKILL_TOOL, BY_SLASH):
            t = self.transcript(OTHER_SKILL, entry)
            for c in OUTWARD:
                with self.subTest(entry=entry["type"], command=c):
                    out = self.run_guard(c, t)
                    self.assertIsNotNone(out, "外へ出すコマンドが通った")
                    self.assertEqual((out["hookEventName"], out["permissionDecision"]), ("PreToolUse", "deny"))
                    self.assertIn("work.py op run", out["permissionDecisionReason"])  # 関門へ戻す道を示す

    def test_other_commands_pass_in_a_session_that_used_the_entry(self):
        t = self.transcript(BY_SKILL_TOOL)
        for c in NOT_OUTWARD:
            with self.subTest(command=c):
                self.assertIsNone(self.run_guard(c, t))

    def test_sessions_that_did_not_use_the_entry_are_not_touched(self):
        for rows in ((), (OTHER_SKILL,), (MENTION_ONLY,)):
            t = self.transcript(*rows)
            with self.subTest(rows=len(rows)):
                self.assertIsNone(self.run_guard("git push origin main", t))

    def test_a_grok_build_call_passes(self):
        # Grok Build は tool_name に run_terminal_command を渡し、会話記録も別の形。判定できないので通す
        t = self.transcript(BY_SKILL_TOOL)
        self.assertIsNone(self.run_guard("git push origin main", t, tool="run_terminal_command"))

    def test_input_it_cannot_judge_passes(self):
        t = self.transcript(BY_SKILL_TOOL)
        for name, raw in (("会話記録が無い", json.dumps({"tool_name": "Bash", "transcript_path": str(Path(self.tmp.name) / "none"),
                                                        "tool_input": {"command": "git push"}})),
                          ("会話記録の欄が無い", json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push"}})),
                          ("JSON でない", "git push"), ("空", ""),
                          ("Bash でない", json.dumps({"tool_name": "Edit", "transcript_path": t, "tool_input": {"command": "git push"}})),
                          ("コマンドが文字列でない", json.dumps({"tool_name": "Bash", "transcript_path": t, "tool_input": {"command": ["git", "push"]}}))):
            with self.subTest(name):
                self.assertIsNone(self.run_guard(None, None, raw=raw))

    def test_the_hook_runs_the_guard_for_each_outward_command(self):
        cfg = json.loads(HOOKS.read_text(encoding="utf-8"))
        entries = cfg["hooks"]["PreToolUse"]
        self.assertEqual([e["matcher"] for e in entries], ["Bash"])
        # 判定は push_guard.py の中だけ。if の欄（Claude Code だけが解す）は置かない（Grok Build は handler を飛ばしてエラーを出す）。
        # -B: プラグインの置き場に __pycache__ を作らない
        self.assertEqual([{k: v for k, v in h.items() if k != "timeout"} for h in entries[0]["hooks"]],
                         [{"type": "command", "command": 'python3 -B "${CLAUDE_PLUGIN_ROOT}/scripts/push_guard.py"'}])
        self.assertTrue(GUARD.is_file())


if __name__ == "__main__":
    unittest.main()
