"""実際に起きた不具合ごとに、scripts/work.py の写しに直しを戻す差分を当て、その不具合を見る実物の試験（場面。偽の CLI・本物の git・
kill）が、戻した写しで落ち、戻さない写しで通ることを確かめる。

モデル（tests/test_oprun_model.py・tests/test_task_model.py）は、同じ直しを戻したフラグ（REVERTED）で、全部の順序から反例を出す。
ただしモデルが実物に結ばれているのは、写す関数の本文のハッシュ（MODELED）だけなので、直しを戻してハッシュだけを進めると、モデルの
試験は通り続ける。この試験は、その迂回を実物で落とす:
- 戻す差分は、今の work.py の文と、戻した文の組。今の文が work.py にちょうど 1 つ無ければ落ちる（直しが戻されたか、書き方が変わった）。
  書き方を変えたときは、新しい書き方に合わせて戻す差分を書き直し、その差分で場面が落ちることを、この試験で確かめる。
- 場面の試験を消すか弱めて、戻した写しで通るようになったら落ちる（直しを確かめる試験が無くなった）。
- 直しが何か所かにあれば、箇所ごとに戻す差分と場面を書き、1 か所ずつ戻す（どの 1 か所を戻しても、その箇所の場面が全部落ちる）。
  箇所の分け方が足りているか（直しの全部の箇所を挙げたか）は確かめない。
- モデルの戻したフラグと、この表の行は対応する: どのフラグも、ちょうど 1 つの行が名指しする（フラグだけを足すと落ちる）。どの行も、
  1 つ以上のフラグを名指しする（フラグを名指ししない行、モデルに無いフラグやほかの行と同じフラグを名指しする行を足すと落ちる）。1 つの行が 2 つのモデルのフラグを名指ししてよい（#31）。
場面は、tests/ のモジュールの WORK を写しに差し替えて、別のプロセスで走らせる。戻さない写しでも同じ場面を走らせ、写しで動かすこと
そのものが場面を落とさないことを確かめる（戻した写しだけを見ると、関係のない理由で落ちても通ってしまう）。

b908b17 の延長（keep_lease）は表に無い: モデルでは、消しても不変条件が破れない（c877d78 の claim の拒否が、切れた担当を取らせない）。
延長そのものは test_work.py の test_the_lease_is_kept_while_a_long_command_runs などが見る。
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent)); import process_limit  # noqa: E402,F401  試験に上限を掛ける（直に走らせても）
import test_oprun_model  # noqa: E402
import test_task_model  # noqa: E402

TESTS = Path(__file__).resolve().parent
WORK = TESTS.parent / "scripts" / "work.py"
TIMEOUT = 600  # 1 回の起動の上限（秒）。場面が止まる退行で、試験全体を止めない
MODELS = {"test_oprun_model": test_oprun_model, "test_task_model": test_task_model}

# 不具合ごとに: 直しを戻したモデルのフラグと、直しの箇所ごとの、戻す差分（今の文, 戻した文）と、戻した写しで落ちる場面。
# 直しが何か所かにあれば、箇所ごとに分ける（どれか 1 か所だけを戻しても、場面が落ちる）。
OPRUN, WORKT, PAR = "test_work.TestOpRun.", "test_work.TestWork.", "test_parallel.TestParallel."
REPLAYS = {
    "d1b5462": {  # 監督役が kill -9 されると、コマンドがロックを持たず、後任が終わっていない操作をやり直せた
        "models": [("test_oprun_model", "cmd_holds_lock")],
        "sites": {"supervise": {"revert": [('pass_fds=[spec["lock_fd"]],', 'pass_fds=[],')],
                                "scenarios": [OPRUN + "test_killing_the_supervisor_with_sigkill_does_not_let_the_op_run_twice"]}},
    },
    "5c1f45a": {  # done を書いた直後に監督役が殺されると、残ったロックで作業場所が止まった
        "models": [("test_oprun_model", "done_lock_free")],
        "sites": {"run_holder": {"revert": [('if held is None and done.get(text, {}).get("state") != "done":', 'if held is None:')],
                                 "scenarios": [OPRUN + "test_a_lock_left_after_done_is_not_counted"]}},
    },
    "c877d78": {  # 手放した後や期限切れの後に別の担当が取り、終わっていない操作をやり直せた
        "models": [("test_oprun_model", "claim_checks_run")],
        "sites": {"claim_at": {"revert": [("    held = run_holder(r)\n    if held:  # 担当の期限が切れても", "    held = None\n    if held:  # 担当の期限が切れても")],
                               "scenarios": [OPRUN + "test_no_one_can_claim_while_the_command_runs_even_after_release_or_expiry"]}},
    },
    "b908b17": {  # 動いている op run の間に、同じ作業場所の op run と、外部操作の op begin と、その鍵の手での op done が通った
        "models": [("test_oprun_model", "run_blocks_ops")],
        "sites": {
            "op_run": {"revert": [("            held = run_holder(r)  # 照合と記録の前に見る。", "            held = None  # 照合と記録の前に見る。")],
                       "scenarios": [OPRUN + "test_a_second_op_run_right_after_the_first_begins_is_refused"]},
            "dispatch-done": {"revert": [('(a.action == "done" and a.key == held) or ', 'False or ')],
                              "scenarios": [OPRUN + "test_only_external_begin_and_the_running_done_are_refused_while_running"]},
            "dispatch-begin": {"revert": [(' or (a.action == "begin" and (a.key or "").split(":")[0] in EXTERNAL))', ')')],
                               "scenarios": [OPRUN + "test_only_external_begin_and_the_running_done_are_refused_while_running"]},
        },
    },
    "d5923ce-run": {  # op begin が拒否されても、手で並べたコマンド（git push）が走った
        "models": [("test_oprun_model", "run_after_check")],
        "sites": {"op_run": {"revert": [('        if code:\n            return out(res, code)\n        gate("after-begin")',
                                         '        if code:\n            subprocess.run(argv, cwd=str(a.frm or a.dir), stdout=sys.stderr)\n'
                                         '            return out(res, code)\n        gate("after-begin")')],
                             "scenarios": [OPRUN + "test_refused_begin_does_not_run_the_command"]}},
    },
    "d5923ce-exit": {  # done の結果が打った文字で、失敗した push が ok と記録された
        "models": [("test_oprun_model", "records_exit")],
        "sites": {"supervise": {"revert": [('result=f"exit {rc}: " + output, by="supervise")', 'result="exit 0: " + output, by="supervise")')],
                                "scenarios": [OPRUN + "test_failed_command_is_recorded_with_its_exit_code"]}},
    },
    "#31": {  # 担当が子を待つ間に切れ、誰も取っていないのに、持ち主の op run・renew・release・integrate・task cleanup が拒否された
        "models": [("test_oprun_model", "holder_by_token"), ("test_task_model", "holder_by_token")],
        "sites": {
            "held_lease": {"revert": [("    return lease if isinstance(lease, dict) and isinstance(token, str) and token else None",
                                       "    return lease if isinstance(lease, dict) and isinstance(token, str) and token and alive(lease, time.time()) else None")],
                           "scenarios": [WORKT + "test_an_expired_lease_nobody_took_stays_with_its_holder",
                                         PAR + "test_an_expired_lease_nobody_took_still_integrates_and_cleans_up",
                                         OPRUN + "test_an_expired_lease_nobody_took_still_runs_and_is_extended"]},
            "op_run": {"revert": [('res, code = op_step(a, r, held_lease(lease, a.token), now, "begin")',
                                   'res, code = op_step(a, r, lease if alive(lease, now) else None, now, "begin")')],
                       "scenarios": [OPRUN + "test_an_expired_lease_nobody_took_still_runs_and_is_extended"]},
            "op_run-extend": {"revert": [('if not alive(lease, now) or float(lease["expires_at"]) < now + 20 * 60:',
                                          'if float(lease["expires_at"]) < now + 20 * 60:')],
                              "scenarios": [OPRUN + "test_a_broken_expiry_does_not_crash_op_run_or_leave_its_lock"]},
            "renew-release": {"revert": [("            mine = held_lease(lease, a.token)", "            mine = live")],
                              "scenarios": [WORKT + "test_an_expired_lease_nobody_took_stays_with_its_holder"]},
            "op-begin-done": {"revert": [("res, code = op_step(a, r, held_lease(lease, a.token), now, a.action)",
                                          "res, code = op_step(a, r, live, now, a.action)")],
                              "scenarios": [WORKT + "test_an_expired_lease_nobody_took_stays_with_its_holder"]},
            "current_lease": {"revert": [("    lease = held_lease(read(r / 'lease.json'), token)",
                                          "    lease = read(r / 'lease.json')\n    lease = lease if alive(lease, time.time()) else None")],
                              "scenarios": [PAR + "test_an_expired_lease_nobody_took_still_integrates_and_cleans_up"]},
        },
    },
    "#32": {  # 子を待つ持ち主のタスクが、ほかのセッションの unclosed に出て、作業場所を消された
        "models": [("test_task_model", "waiting_not_offered")],
        "sites": {"unclosed_tasks": {"revert": [("            if alive(lease, now) or maybe_waiting(lease, now):", "            if alive(lease, now):")],
                                     "scenarios": [PAR + "test_a_task_whose_owner_may_still_be_waiting_is_not_offered_for_closing"]}},
    },
    "stale-list": {  # #32 の直しの後、閉じる側が古い一覧から task resume し、戻って延ばした後にまた切れた持ち主の作業場所を消した
        "models": [("test_task_model", "resume_refuses_waiting")],
        "sites": {
            "claim_at": {"revert": [("    if not take_over and maybe_waiting(old, now):", "    if not take_over and False:")],
                         "scenarios": [PAR + "test_a_stale_list_does_not_take_the_task_of_an_owner_who_came_back"]},
            "task_command": {"revert": [("claim_at(wr, dest, a.holder, a.lease, take_over=a.take_over)", "claim_at(wr, dest, a.holder, a.lease)")],
                             "scenarios": [PAR + "test_a_stale_list_does_not_take_the_task_of_an_owner_who_came_back"]},
        },
    },
}

RUNNER = r"""
import importlib, json, sys, unittest
from pathlib import Path
tests, work, names = sys.argv[1], Path(sys.argv[2]), sys.argv[3:]
sys.path.insert(0, tests)
suite = unittest.TestSuite()
for name in names:
    module, rest = name.split(".", 1)
    mod = importlib.import_module(module)
    mod.WORK = work
    suite.addTests(unittest.defaultTestLoader.loadTestsFromName(rest, mod))
result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
print(json.dumps({"ran": result.testsRun, "bad": sorted({t.id().split(" ")[0] for t, _ in result.failures + result.errors})}))
"""


def reverted(src: str, revert: list) -> str:
    """src に戻す差分を当てた文。今の文がちょうど 1 つ無ければ LookupError。"""
    for old, new in revert:
        if src.count(old) != 1:
            raise LookupError(f"直しの文が scripts/work.py にちょうど 1 つ無い（{src.count(old)} か所）: {old!r}。直しが戻されたか、"
                              "書き方が変わった。今の書き方に合わせて、tests/test_replay.py の戻す差分を書き直す")
        src = src.replace(old, new)
    return src


class TestReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def copy(self, name: str, src: str) -> Path:
        work = Path(self.tmp.name) / name / "scripts" / "work.py"
        work.parent.mkdir(parents=True)
        work.write_text(src, encoding="utf-8")
        compile(src, str(work), "exec")  # 戻した写しが Python として読める（読めずに落ちるのは、直しを確かめたことにならない）
        return work

    def run_scenarios(self, work: Path, names: list) -> dict:
        p = subprocess.Popen([sys.executable, "-B", "-c", RUNNER, str(TESTS), str(work), *names], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, errors="replace", start_new_session=True)
        try:
            out, err = p.communicate(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.communicate()
            self.fail(f"場面が {TIMEOUT} 秒で終わらない（{work}）: {names}")
        lines = out.strip().splitlines()
        self.assertTrue(lines, err[-3000:])
        res = json.loads(lines[-1])
        self.assertEqual(res["ran"], len(names), f"場面の試験が見つからないか、数が違う: {names}\n{err[-3000:]}")
        res["log"] = err
        return res

    def test_every_reverted_flag_of_the_models_has_a_replay(self):
        flags = {(name, flag) for name, model in MODELS.items() for flag in model.REVERTED}
        replayed = [pair for replay in REPLAYS.values() for pair in replay["models"]]
        self.assertEqual([bug for bug, replay in REPLAYS.items() if not replay["models"]], [],
                         "モデルのフラグを名指ししない行がある（直しを足したら、モデルのフラグも足す）")
        self.assertEqual(len(replayed), len(set(replayed)), "同じフラグを 2 つの行が名指ししている")
        self.assertEqual(set(replayed), flags, "モデルの戻したフラグと、この表の行が対応しない（直しを足したら、モデルのフラグと、"
                         "実物に戻す差分と場面の両方を足す）")
        self.assertEqual([bug for bug, replay in REPLAYS.items() if not replay["sites"]
                          or any(not site["revert"] or not site["scenarios"] for site in replay["sites"].values())], [],
                         "戻す差分か場面の無い箇所がある")

    def test_the_scenarios_pass_on_an_unchanged_copy(self):
        names = sorted({n for replay in REPLAYS.values() for site in replay["sites"].values() for n in site["scenarios"]})
        res = self.run_scenarios(self.copy("unchanged", WORK.read_text(encoding="utf-8")), names)
        self.assertEqual(res["bad"], [], f"戻さない写しで場面が落ちた（写しで動かすことそのものが場面を落とす）:\n{res['log'][-3000:]}")

    def test_each_reverted_fix_fails_its_scenarios(self):
        src = WORK.read_text(encoding="utf-8")
        for bug, replay in REPLAYS.items():
            for name, site in replay["sites"].items():
                with self.subTest(bug=bug, site=name):
                    work = self.copy(f"{bug.strip('#')}-{name}", reverted(src, site["revert"]))
                    res = self.run_scenarios(work, site["scenarios"])
                    self.assertEqual(res["bad"], sorted(site["scenarios"]), f"直しの 1 か所（{bug} の {name}）を戻した写しで、通ってしまった場面が"
                                     f"ある（その箇所を確かめる試験が無くなった）:\n{res['log'][-3000:]}")

    def test_a_missing_fix_is_reported(self):
        with self.assertRaises(LookupError):
            reverted(WORK.read_text(encoding="utf-8").replace('pass_fds=[spec["lock_fd"]],', 'pass_fds=[],'),
                     REPLAYS["d1b5462"]["sites"]["supervise"]["revert"])


if __name__ == "__main__":
    unittest.main()
