"""タスクの持ち主と、閉じ忘れを片付けるほかのセッション（scripts/work.py の task start・integrate・renew・release・task cleanup と、
task start・task list の unclosed・task resume）を状態機械に写し、持ち主が子を待つ間の時間の経過との交互実行を全部たどる。

Issue #32 は tests/test_oprun_model.py の写す範囲の外で起きた: 統合を終えた持ち主が子を待つ間に担当の期限が切れ、ほかのセッションが
task start の unclosed に見て、playbook-parallel の「片付ける」どおり task resume → task cleanup で作業場所を消し、戻った持ち主の
renew が 3 で拒否された。直し（d7cb680）は、期限が切れて MAX_LEASE_MIN 以内の担当のタスクを unclosed に並べない（maybe_waiting）。
その直しの後も、このモデルは反例を出した: 閉じる側が、持ち主の担当が切れて MAX_LEASE_MIN を過ぎたときに取った一覧を、持ち主が戻って
延ばし、また子を待つ間に切れた後に使うと、task resume は期限の内かだけを見て担当を取り、task cleanup が作業場所を消す（実物でも再現した）。
一覧が決めたことを、取る時に決め直していなかった。直しは、--take-over の無い task resume の claim_at が、一覧と同じ maybe_waiting で、
取る時に拒否する（--take-over は、利用者にそのタスクの続きか、捨てることを頼まれたセッションが付ける）。
道具は Python の標準機能だけの、明示状態の幅優先の全探索（依存が要らず、試験に置ける）。
直しを戻した版をフラグで作り、それぞれが反例を出すことも試す。同じ直しを実物の work.py の写しに戻し、その不具合を見る試験が
落ちることは、tests/test_replay.py が確かめる（フラグと 1 対 1）。

約束（破れたときの被害）:
- OwnerNotDisplaced 持ち主が手放しておらず、担当の期限の内か、切れてから MAX_LEASE_MIN 以内なら、記録の token は持ち主のもので、
  作業場所は残っている（子を待っていただけの持ち主が、作業場所と担当を失う。Issue #32 と、その後の古い一覧）。
- WaitingNotOffered 一覧（unclosed）は、期限が切れて MAX_LEASE_MIN 以内の担当のタスクを、閉じるものとして出さない（閉じる側が、
  閉じられないタスクを並べて取りにいく。取る時の拒否が無かったときは、取って作業場所を消した。Issue #32）。
- OwnerCanAct 記録の token が持ち主のもの（誰も取り直していない）なら、期限が切れていても、持ち主は延ばし・手放し・統合し・
  （統合した後は）閉じられる（子を待っていただけの持ち主が締め出される。Issue #31 の integrate・task cleanup の経路）。

範囲（上限）: タスク 1 つ、持ち主 1、閉じる側 2（それぞれ何度でも一覧を取り直し、取った一覧をどれだけ長く持ってから使ってもよい）。
時間は、持ち主の担当の期限と、記録の期限のそれぞれを 3 段（期限の内 live・切れて MAX_LEASE_MIN 以内 recent・それより後 old）に畳む
（記録が持ち主のものの間は同じ値）。閉じる側は playbook-parallel の「片付ける」に従う: unclosed に統合先に入った（landed）タスクとして
出たものだけを、--take-over の無い task resume で取り、task cleanup で閉じる。

モデルの動作と、対応するコード（scripts/work.py）:
- Start: task_command の start（タスクの一覧に書き、add_worktree、claim_at）。
- Integrate・Cleanup（持ち主）: integrate・task_command の cleanup は current_lease で照らす。cleanup は統合先に入ったこと（contained）も見る。
- Renew・Release（持ち主）: dispatch の renew・release は held_lease と check_token で照らす。
- List（閉じる側）: unclosed_tasks（作業場所が残っていて、担当が無いか、alive でも maybe_waiting でもない）。
- Resume: task_command の resume（claim_at の take_over は偽。alive か maybe_waiting の担当がいれば 3。タスクの記録が無ければ拒否）。
- CleanupB・LostB: task_command の cleanup（current_lease。取り直されていれば 3）。
- TickA・TickRec: 時間の経過（持ち主の担当の期限、閉じる側が取った担当の期限）。

省いたもの（これらに触れる性質は、このモデルでは確かめていない）:
- op run と監督役（tests/test_oprun_model.py）。持ち主の op run が動いている間は、閉じる側の task resume が run_holder で拒否される
  （ここでは、持ち主が子を待つ間に op run は動いていないとする。動いていれば、もっと守られる）。
- 利用者に頼まれて、止まった持ち主の続きを引き受けるか、タスクを捨てるセッション（playbook-session-pickup・playbook-parallel）。
  task resume --take-over で、期限が切れて間もないタスクでも取る（取ってよい）。
- claim（作業場所を名指しして取る。書く子と、子の後の親が使い、期限の切れた担当を取る）で、閉じるタスクを取ること（手引きの片付けは
  task resume を使う）。
- 未保存の変更・終わっていない操作による task cleanup の拒否、統合先が動くこと、読めない担当の記録（unreadable）、統合の記録。
- 持ち主が自分のタスクを task resume で取り直すこと、途中で止まった task cleanup（task resume が仕上げる）。

実物との突き合わせ（2026-11-24、本物の git と work.py）: モデルの分岐は、tests/test_parallel.py の次の試験が実物で通る。一覧は、手放した・
切れて 24 時間を過ぎたタスクを出し、切れて間もないタスクを出さない（test_a_task_whose_owner_may_still_be_waiting_is_not_offered_for_closing・
test_a_new_task_reports_the_tasks_nobody_closed）、生きた担当の task resume は 3（test_resume_keeps_uncommitted_changes_and_refuses_live_holder）、
古い一覧からの task resume は、切れて間もない担当を取らず、切れて 24 時間を過ぎた担当を取って閉じる
（test_a_stale_list_does_not_take_the_task_of_an_owner_who_came_back）、切れた担当を取った後は元の token の integrate・task cleanup が 3
（test_an_expired_lease_taken_by_another_stops_the_old_token）、誰も取っていなければ持ち主の integrate・task cleanup が通る
（test_an_expired_lease_nobody_took_still_integrates_and_cleans_up）、--take-over で取った側は閉じられる
（test_a_session_that_took_over_a_recently_expired_task_can_close_it）。

対象の関数（MODELED。tests/model_tie.py のハッシュ）: alive・maybe_waiting・held_lease・check_token・current_lease・claim_at・
unclosed_tasks・task_command・integrate・dispatch。task_command・integrate・dispatch は担当の照合と作業場所の削除だけを写すが、
ハッシュは関数全体に掛ける。照合が落ちたら、変わった関数を読み、モデル（successors）が今の本文を写していることを確かめてから、
ハッシュと MODELED_AT を書き換える。
"""

import sys
import unittest
from collections import deque
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent)); import process_limit  # noqa: E402,F401  試験に上限を掛ける（直に走らせても）
from model_tie import drift  # noqa: E402

WORK = Path(__file__).resolve().parents[1] / "scripts" / "work.py"

MODELED_AT = "2f5e310"
MODELED = {
    "alive": "f1f9574a8ad75670",
    "maybe_waiting": "b5093f786995e028",
    "held_lease": "2e230a09eab2bcdd",
    "check_token": "6297f65bcd57f11c",
    "current_lease": "647f2e51dedbbfd9",
    "claim_at": "d13a239d32440d08",
    "unclosed_tasks": "c1b08c620e752898",
    "task_command": "1be11aadcb159544",
    "integrate": "be70176842d82a73",
    "dispatch": "7b2ce7ae7c39483f",
}

LATER = {"live": "recent", "recent": "old"}  # 期限の段の進み方
PHASE = {"live": "期限の内", "recent": "切れて 24 時間以内", "old": "切れて 24 時間を過ぎた"}
CLOSERS = ("b1", "b2")
OWNER = 1  # 持ち主の token。閉じる側 i の token は OWNER + 1 + i
IDLE = ("idle", False, False)
LIMIT = 100_000  # 状態の数の上限（超えたら、たどり切れていないので落とす）

# 今のコードと、直しを 1 つずつ戻した版。True が今のコード
NOW = {"waiting_not_offered": True, "holder_by_token": True, "resume_refuses_waiting": True}
REVERTED = {
    # Issue #32（d7cb680）: 期限が切れて MAX_LEASE_MIN 以内の担当のタスクは unclosed に並べない（unclosed_tasks の maybe_waiting）
    "waiting_not_offered": "WaitingNotOffered",
    # Issue #31（9f11eab）: 持ち主の操作は期限を見ず、記録の token で照らす（held_lease。integrate・task cleanup は current_lease から）
    "holder_by_token": "OwnerCanAct",
    # #32 の直しの後の古い一覧: --take-over の無い task resume は、一覧と同じ maybe_waiting の担当を、取る時に拒否する（claim_at）
    "resume_refuses_waiting": "OwnerNotDisplaced",
}

# 状態は (place, landed, tok, phase, owner, mine, closers)。
#   place: 作業場所とタスクの記録があるか  landed: 変更が統合先に入ったか  tok: lease.json の token（0 は無い）
#   phase: lease.json の期限の段  owner: 持ち主（none・holds・left）  mine: 持ち主の担当の期限の段
#   closers: 閉じる側ごとの (stage, offered, waiting)。stage は idle・listed・took。offered は、一覧に閉じるタスクとして出たか。
#            waiting は、出たときに担当の期限が切れて MAX_LEASE_MIN 以内だったか（WaitingNotOffered を見るための記録。動作は読まない）


def init():
    return (False, False, 0, "live", "none", "live", tuple(IDLE for _ in CLOSERS))


def upd(t, i, v):
    return t[:i] + (v,) + t[i + 1:]


def successors(s, f):
    """(動作, 次の状態) の列。動作は (名前, 引数…)。"""
    place, landed, tok, phase, owner, mine, closers = s
    out = []

    def put(step, **kw):
        d = dict(place=place, landed=landed, tok=tok, phase=phase, owner=owner, mine=mine, closers=closers)
        d.update(kw)
        out.append((step, (d["place"], d["landed"], d["tok"], d["phase"], d["owner"], d["mine"], d["closers"])))

    def held(t):  # held_lease・check_token（current_lease も同じ）。今のコードは期限を見ない
        return tok == t and (f["holder_by_token"] or phase == "live")
    waiting = tok != 0 and phase == "recent"  # maybe_waiting
    # 時間。閉じる側が一覧を持っている間も進む
    if owner != "none" and mine in LATER:
        put(("TickA", LATER[mine]), mine=LATER[mine], phase=LATER[mine] if tok == OWNER else phase)
    if tok > OWNER and phase in LATER:
        put(("TickRec", LATER[phase]), phase=LATER[phase])
    # 持ち主
    if owner == "none":
        put(("Start",), place=True, tok=OWNER, phase="live", owner="holds", mine="live")
    elif owner == "holds" and held(OWNER):
        if not landed:
            put(("Integrate",), landed=True)
        put(("Renew",), phase="live", mine="live")
        put(("Release",), tok=0, owner="left")
        if landed:
            put(("Cleanup",), place=False, tok=0, owner="left")
    # 閉じる側
    for i, (stage, offered, _) in enumerate(closers):
        me = OWNER + 1 + i
        if stage == "idle" and place:  # unclosed_tasks: 担当が無いか、alive でも maybe_waiting でもない
            shown = landed and (tok == 0 or (phase != "live" and not (waiting and f["waiting_not_offered"])))
            put(("List", i, shown), closers=upd(closers, i, ("listed", shown, shown and waiting)))
        elif stage == "listed":
            # task resume の claim_at（take_over は偽）。一覧を取ったときでなく、今の担当で決める
            if offered and place and (tok == 0 or (phase != "live" and not (waiting and f["resume_refuses_waiting"]))):
                put(("Resume", i), tok=me, phase="live", closers=upd(closers, i, ("took", False, False)))
            else:
                put(("Pass", i), closers=upd(closers, i, IDLE))
        elif stage == "took":
            if held(me) and landed:  # task cleanup（一覧に出た landed のタスクだけを取るので、統合先に入っている）
                put(("CleanupB", i), place=False, tok=0, closers=upd(closers, i, IDLE))
            else:
                put(("LostB", i), closers=upd(closers, i, IDLE))
    return out


def violated(s, f):
    """破れた約束の名前（無ければ None）。"""
    place, landed, tok, phase, owner, mine, closers = s
    if owner == "holds" and mine != "old" and (tok != OWNER or not place):
        return "OwnerNotDisplaced"
    if any(waiting for _, _, waiting in closers):
        return "WaitingNotOffered"
    if owner == "holds" and tok == OWNER:
        steps = {step for step, _ in successors(s, f)}
        if not {("Renew",), ("Release",), ("Cleanup",) if landed else ("Integrate",)} <= steps:
            return "OwnerCanAct"
    return None


def check(f):
    """幅優先で全部たどる。(破れた約束か None, たどった状態（数と中身）, 最短の反例の動作の列)。"""
    s0 = init()
    parent = {s0: None}
    q = deque([s0])
    while q:
        s = q.popleft()
        bad = violated(s, f)
        if bad:
            trace = []
            while parent[s] is not None:
                s, step = parent[s]
                trace.append(step)
            return bad, parent, trace[::-1]
        for step, t in successors(s, f):
            if t not in parent:
                parent[t] = (s, step)
                q.append(t)
                if len(parent) > LIMIT:
                    raise RuntimeError(f"状態の数が上限 {LIMIT} を超えた（たどり切れていない）")
    return None, parent, []


STEPS = {  # work.py の言葉で
    "Start": "持ち主がタスクを始める（task start。作業場所を作り、claim_at で担当を取る）",
    "Integrate": "持ち主が統合する（integrate。current_lease で照らす）",
    "Renew": "持ち主が担当を延ばす（renew。held_lease で照らす）",
    "Release": "持ち主が担当を手放す（release）",
    "Cleanup": "持ち主が task cleanup で閉じる",
    "TickA": "時間が過ぎ、持ち主の担当の期限が「{p}」になる",
    "TickRec": "時間が過ぎ、閉じる側が取った担当の期限が「{p}」になる",
    "List": "{b} が task start・task list の unclosed を見る（unclosed_tasks）。このタスクは閉じるものとして{o}",
    "Resume": "{b} が task resume で担当を取る（claim_at。--take-over は付けない）",
    "Pass": "{b} はこのタスクを閉じない（一覧に出ていない・生きた担当か、切れて 24 時間以内の担当がいて task resume が 3・タスクの記録が無い）",
    "CleanupB": "{b} が task cleanup で作業場所を消す",
    "LostB": "{b} の担当が取り直されていて、task cleanup が 3 で拒否される",
}
MEANING = {
    "OwnerNotDisplaced": "持ち主が手放しておらず、担当の期限が切れてから 24 時間以内なのに、ほかのセッションが担当を取ったか、作業場所を消した",
    "WaitingNotOffered": "持ち主が子を待っているだけかもしれない（担当の期限が切れて 24 時間以内の）タスクを、一覧が閉じるものとして出した",
    "OwnerCanAct": "誰も取り直していないのに、持ち主が延ばす・手放す・統合する・閉じるのどれかをできない（期限切れで締め出された）",
}


def story(bad, trace):
    """反例を、番号付きの手順の文にする。"""
    rows = []
    for i, (name, *args) in enumerate(trace, 1):
        if name in ("TickA", "TickRec"):
            who = {"p": PHASE[args[0]]}
        elif args:
            who = {"b": CLOSERS[args[0]], "o": "出る" if args[1:] == [True] else "出ない"}
        else:
            who = {}
        rows.append(f"{i}. " + STEPS[name].format(**who))
    return f"{bad} が破れた: {MEANING[bad]}\n" + "\n".join(rows)


class TestTaskModel(unittest.TestCase):
    def test_the_modeled_code_keeps_the_promises_in_every_interleaving(self):
        bad, seen, trace = check(NOW)
        self.assertIsNone(bad, f"{len(seen)} 状態をたどって反例。scripts/work.py か、モデルの写し方を直す:\n" + (story(bad, trace) if bad else ""))
        self.assertGreater(len(seen), 100)  # たどった範囲が、空でないこと
        # 閉じる側が一覧を持つ間に、持ち主が戻って延ばし、また切れて 24 時間以内になった状態にも届く（一覧を持つ長さを狭めて通していない）
        self.assertTrue(any(s[2:5] == (OWNER, "recent", "holds") and ("listed", True, False) in s[6] for s in seen))

    def test_each_reverted_fix_gives_a_counterexample(self):
        self.assertEqual(set(REVERTED), set(NOW), "今のコードのフラグごとに、戻した版の行が要る（行を消すと、その直しを確かめなくなる）")
        for flag, promise in REVERTED.items():
            with self.subTest(reverted=flag):
                bad, _, trace = check({**NOW, flag: False})
                self.assertEqual(bad, promise, f"直しを戻した版（{flag}）が {promise} の反例を出さない。モデルが直しの効き目を写していない"
                                 + (f"。出た反例:\n{story(bad, trace)}" if bad else ""))
                self.assertEqual(len(story(bad, trace).splitlines()), len(trace) + 1)

    def test_the_story_of_issue_32_names_each_step_in_work_py_terms(self):
        _, _, trace = check({**NOW, "waiting_not_offered": False})
        text = story("WaitingNotOffered", trace)
        self.assertIn("「切れて 24 時間以内」", text)
        self.assertIn("unclosed を見る（unclosed_tasks）。このタスクは閉じるものとして出る", text)

    def test_without_the_check_when_taking_a_stale_list_takes_a_waiting_owners_task(self):
        """task resume が取る時に maybe_waiting を見ないと（Issue #32 の直しの後の版）、閉じる側は、持ち主の担当が切れて 24 時間を過ぎた
        ときに取った一覧から、戻って延ばし、また切れた持ち主の担当を取る（実物の再現: tests/test_parallel.py の
        test_a_stale_list_does_not_take_the_task_of_an_owner_who_came_back）。"""
        bad, _, trace = check({**NOW, "resume_refuses_waiting": False})
        self.assertEqual(bad, "OwnerNotDisplaced")
        names = [step[0] for step in trace]
        listed = names.index("List")
        self.assertEqual(trace[listed][2], True)
        self.assertIn("Renew", names[listed:names.index("Resume")])

    def test_the_model_was_checked_against_the_current_code(self):
        changed = drift(WORK.read_text(encoding="utf-8"), MODELED)
        if changed:
            self.fail(f"scripts/work.py のモデルが写す関数が、照合した commit {MODELED_AT} から変わった。"
                      "モデルを見直して、照合した commit（MODELED_AT）とハッシュを進める（tests/test_task_model.py の冒頭）:\n" + "\n".join(changed))


if __name__ == "__main__":
    unittest.main()
