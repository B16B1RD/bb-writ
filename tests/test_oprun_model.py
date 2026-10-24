"""op run・監督役・操作ごとのロック・担当の約束（scripts/work.py）を状態機械に写し、取りうる交互実行と途中の kill -9 を全部たどる。

審査が 1 周に 1 つずつ交互実行を見つけ、そのたびに直していたため、このモデルが写す範囲は 1 回の全探索でたどる。
範囲は担当 2・鍵 2・token 3。担当の期限切れはいつでも起こせる（実際の時間を写さない、保守的な写し方）。
過去の修正を戻した版をフラグで作り、それぞれが反例を出すことも試す（一度も落ちたことのない検査は、何も確かめていない）。
TLA+ の同じモデル（TLC）とは、照合した commit で、今の版の状態数（MODELED_STATES）と、戻した版の違反と反例の動作の列が一致した。
モデルを変えたら（動作を足す・削る・条件を変える）、状態数が変わって落ちる。同じ変更を TLA+ のモデルに入れて TLC と照らしてから、
MODELED_STATES を書き換える。

モデルが写す関数と、最後に照合した commit、その commit での各関数の本文のハッシュ（下の MODELED）。
ハッシュは、関数の本文（デコレータを含む）からコメント・先頭の docstring・空行・行末の空白を除いたものに掛ける（Python の版によらない）。
意味の同じ書式の変更（括弧・改行の位置）でも変わる。そのときも、照合した commit とハッシュを進める。
dispatch は release と op done の分岐だけを、op_step は担当の token の照合と同じ鍵の begin・done の拒否だけを写すが、
ハッシュは関数全体に掛ける（op_step は --reviewed などの写さない照合でも変わる）。locked は作業場所の排他、run_lock は鍵ごとに
別のロックのファイル、ops は鍵ごとの最後の state、alive・check_token は担当が生きているかと done を書けるかを写す。
写す関数が拒否を足すだけの変更は、モデルが許す動きを減らすだけなので、不変条件は保たれる（それでもハッシュは落ちる）。
写さない段: シグナルの中継（supervise の 2 回目の fork）、記録の形式と壊れた記録、出力のファイル、ロックを開けない・読めないとき、
op begin（op run を通さない）、integrate の排他と照合。

照合が落ちたら、変わった関数を読み、モデル（successors）が今の本文を写していることを確かめる（違えば直して全探索を通す）。
そのうえで、照合した commit とハッシュを一緒に書き換える。写す分岐に関係しない変更（dispatch の status など）でも同じにする。
"""

import ast
import hashlib
import io
import subprocess
import sys
import tokenize
import unittest
from collections import deque
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent)); import process_limit  # noqa: E402,F401  試験に上限を掛ける（直に走らせても）

WORK = Path(__file__).resolve().parents[1] / "scripts" / "work.py"

MODELED_AT = "3dc5f8d"
MODELED_STATES = 84981
MODELED = {
    "locked": "9eaa07012a88c87c",
    "ops": "19f01a5bc3cb84ad",
    "op_step": "2f1b8ddd8883b632",
    "run_lock": "fdd3edfffe9c0b10",
    "alive": "be56e37e0b082f93",
    "check_token": "6297f65bcd57f11c",
    "claim_at": "ce185fc18d5fc861",
    "run_holder": "ed7f517a536e94fd",
    "keep_lease": "1ff486dd87b26989",
    "op_run": "a0902cd64d03f8c1",
    "finish": "cac6b578315e4a7d",
    "supervise": "401e4f1a9702eb3f",
    "dispatch": "c455d546b4c92cfc",
}


def body(src: str, name: str) -> str:
    """モジュールの最上位の関数 name の本文（デコレータを含む）から、コメント・先頭の docstring・空行・行末の空白を除いたもの。"""
    node = next((n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == name), None)
    if node is None:
        raise LookupError(f"work.py に関数 {name} が無い")
    start = min([node.lineno] + [d.lineno for d in node.decorator_list])
    lines = src.splitlines()[start - 1:node.end_lineno]
    first = node.body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
        drop = set(range(first.lineno - start, first.end_lineno - start + 1))
        lines = ["" if i in drop else line for i, line in enumerate(lines)]
    text = "\n".join(lines) + "\n"
    cuts = {}  # 行ごとの、コメントが始まる列
    for t in tokenize.generate_tokens(io.StringIO(text).readline):
        if t.type == tokenize.COMMENT:
            cuts[t.start[0] - 1] = t.start[1]
    kept = [line[:cuts[i]].rstrip() if i in cuts else line.rstrip() for i, line in enumerate(text.splitlines())]
    return "\n".join(line for line in kept if line)


def fingerprint(src: str, name: str) -> str:
    return hashlib.sha256(body(src, name).encode("utf-8")).hexdigest()[:16]


# ---- 状態機械 ----
# 状態は次の組。鍵ごとの値は KEYS の順の組。
#   lease_tok: lease.json の token（0 は無い）  lease_alive: 期限内か  next_tok: 次に払い出す token
#   tok_of: 担当ごとの、最後に受け取った token  rec: 鍵ごとの ops.jsonl の state（none・begun・done）
#   lock_file: 鍵ごとのロックのファイルがあるか  holders: 鍵ごとの、そのロックの flock を持つもの（oprun・sup・cmd・left）
#   oprun: 鍵ごとの op run（none・spawning・spawned・dead・failed）  sup: 鍵ごとの監督役（none・started・waiting・wrote・exited・killed）
#   sup_tok: 鍵ごとの、監督役が受け取った token  cmd: 鍵ごとのコマンド（none・running・exited）
#   left: 鍵ごとの、コマンドが残したプロセス（credential daemon など）が生きているか

AGENTS = ("a1", "a2")
KEYS = ("k1", "k2")
MAX_TOK = 3
ALIVE = {"started", "waiting", "wrote"}  # 監督役が生きている段
FIELDS = ("lease_tok", "lease_alive", "next_tok", "tok_of", "rec", "lock_file", "holders", "oprun", "sup", "sup_tok", "cmd", "left")

# 今のコードと、過去の修正を 1 つずつ戻した版。True が今のコード
NOW = {"cmd_holds_lock": True, "done_lock_free": True, "claim_checks_run": True}
REVERTED = {
    # d1b5462: コマンド（とそれが残したもの）にもロックの fd を渡す（supervise の Popen の pass_fds）
    "cmd_holds_lock": "AtMostOneCmd",
    # 5c1f45a: 鍵が done のロックは数えない（run_holder）
    "done_lock_free": "BlockedOnlyWhileWorking",
    # c877d78: op run のコマンドが動いている間は担当を取らせない（claim_at の run_holder）
    "claim_checks_run": "SupKeepsLease",
}


def init():
    per = lambda v: tuple(v for _ in KEYS)  # noqa: E731
    return (0, False, 1, tuple(0 for _ in AGENTS), per("none"), per(False), per(frozenset()), per("none"), per("none"),
            per(0), per("none"), per(False))


def upd(t, i, v):
    return t[:i] + (v,) + t[i + 1:]


def held_key(s, k, f):
    """run_holder がその鍵を数えるか: ファイルがあり、誰かが flock を持ち、（今のコードでは）鍵が done でない。"""
    _, _, _, _, rec, lock_file, holders, *_ = s
    return lock_file[k] and bool(holders[k]) and (not f["done_lock_free"] or rec[k] != "done")


def successors(s, f):
    """(動作, 次の状態) を返す。動作は (名前, 担当か鍵の番号…)。Claim だけは (名前, 担当, 払い出す token)。"""
    lease_tok, lease_alive, next_tok, tok_of, rec, lock_file, holders, oprun, sup, sup_tok, cmd, left = s
    mutex = "wrote" in sup  # 作業場所の排他（locked(r)）。監督役が done を書いてからロックのファイルを消すまで持つ
    held = any(held_key(s, k, f) for k in range(len(KEYS)))

    def mk(**kw):
        d = dict(zip(FIELDS, s))
        d.update(kw)
        return tuple(d[n] for n in FIELDS)
    if lease_alive:  # 時間が過ぎて担当が切れる（keep_lease の延長が間に合わない場合を含む）
        yield ("Expire",), mk(lease_alive=False)
    for a in range(len(AGENTS)):
        live = lease_alive and lease_tok != 0 and tok_of[a] == lease_tok
        if not mutex and not lease_alive and next_tok <= MAX_TOK and (not f["claim_checks_run"] or not held):  # claim_at
            yield ("Claim", a, next_tok), mk(lease_tok=next_tok, lease_alive=True, next_tok=next_tok + 1, tok_of=upd(tok_of, a, next_tok))
        if not mutex and live and not held:  # dispatch の release
            yield ("Release", a), mk(lease_tok=0, lease_alive=False)
        for k in range(len(KEYS)):
            if not mutex and live and not held and rec[k] == "none":  # op_run の排他の中: begun を書き、ロックを作って持つ
                yield ("OpRunBegin", a, k), mk(rec=upd(rec, k, "begun"), lock_file=upd(lock_file, k, True),
                                               holders=upd(holders, k, frozenset({"oprun"})), oprun=upd(oprun, k, "spawning"),
                                               sup_tok=upd(sup_tok, k, tok_of[a]))
                # begun を書いた後、ロックのファイルを作る前に kill -9 される（排他は OS が外す）。作った後の死は OpRunKilled と同じ
                yield ("OpRunKilledInBegin", a, k), mk(rec=upd(rec, k, "begun"), oprun=upd(oprun, k, "dead"))
            if not mutex and live and rec[k] == "begun" and not held_key(s, k, f):  # dispatch の op done（照合で閉じる）
                yield ("ManualDone", a, k), mk(rec=upd(rec, k, "done"))
    for k in range(len(KEYS)):
        h = holders[k]
        hold = f["cmd_holds_lock"] and lock_file[k]  # コマンドとそれが残したものが、ロックの fd を受け継ぐか
        if oprun[k] == "spawning":
            yield ("OpRunKilled", k), mk(oprun=upd(oprun, k, "dead"), holders=upd(holders, k, h - {"oprun"}))
            yield ("Spawn", k), mk(oprun=upd(oprun, k, "spawned"), sup=upd(sup, k, "started"),
                                   holders=upd(holders, k, (h - {"oprun"}) | {"sup"}))
            if not mutex:  # 監督役を起こせない（OSError）。排他の中で done（exit 127）を書き、ロックのファイルを消し、fd を閉じる
                yield ("SpawnFails", k), mk(oprun=upd(oprun, k, "failed"), rec=upd(rec, k, "done") if lease_tok == sup_tok[k] else rec,
                                            lock_file=upd(lock_file, k, False), holders=upd(holders, k, h - {"oprun"}))
        if sup[k] == "started":
            yield ("StartCmd", k), mk(sup=upd(sup, k, "waiting"), cmd=upd(cmd, k, "running"),
                                      holders=upd(holders, k, h | {"cmd"} if hold else h))
        if cmd[k] == "running" and not left[k]:
            yield ("Leftover", k), mk(left=upd(left, k, True), holders=upd(holders, k, h | {"left"} if hold else h))
        if cmd[k] == "running":
            yield ("CmdExit", k), mk(cmd=upd(cmd, k, "exited"), holders=upd(holders, k, h - {"cmd"}))
        if left[k]:
            yield ("LeftExit", k), mk(left=upd(left, k, False), holders=upd(holders, k, h - {"left"}))
        if not mutex and sup[k] == "waiting" and cmd[k] == "exited":  # supervise の op_step done。token が違えば書かない
            yield ("WriteDone", k), mk(sup=upd(sup, k, "wrote"),
                                       rec=upd(rec, k, "done") if lease_tok == sup_tok[k] else rec)
        if sup[k] == "wrote":  # finish でファイルを消し、fd を閉じて終わる
            yield ("Finish", k), mk(sup=upd(sup, k, "exited"), lock_file=upd(lock_file, k, False), holders=upd(holders, k, frozenset()))
        if sup[k] in ALIVE:  # どの段でも（排他も OS が外す）
            yield ("SupKilled", k), mk(sup=upd(sup, k, "killed"), holders=upd(holders, k, h - {"sup"}))
        if not mutex and sup[k] == "waiting" and lease_tok == sup_tok[k] and not lease_alive:  # keep_lease: token が同じなら延ばす
            yield ("KeepLease", k), mk(lease_alive=True)
        if not mutex and lock_file[k] and not h:  # run_holder が外れたロックのファイルを消す
            yield ("Gc", k), mk(lock_file=upd(lock_file, k, False))


def violated(s, f):
    """破れた不変条件の名前（無ければ None）。"""
    lease_tok, lease_alive, next_tok, tok_of, rec, lock_file, holders, oprun, sup, sup_tok, cmd, left = s
    n = range(len(KEYS))
    # 同じ作業場所で、外部操作が同時に 2 つ走らない（後任が終わっていない操作をやり直さない）
    if sum(c == "running" for c in cmd) > 1:
        return "AtMostOneCmd"
    # 監督役が生きている間、担当はその監督役の token のまま（done を書ける）
    if any(sup[k] in ("started", "waiting") and lease_tok != sup_tok[k] for k in n):
        return "SupKeepsLease"
    # 作業場所が止まるのは、終わっていない外部操作がまだ続いているかもしれない間だけ（排他の間は誰も run_holder を呼べない）
    if "wrote" not in sup and any(held_key(s, k, f) for k in n) and not any(
            rec[k] != "done" and (cmd[k] == "running" or left[k] or sup[k] in ALIVE or oprun[k] == "spawning") for k in n):
        return "BlockedOnlyWhileWorking"
    return None


def check(f):
    """幅優先で全部たどる。(破れた不変条件か None, 数えた状態の数, 最短の反例の動作の列)。"""
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
            return bad, len(parent), trace[::-1]
        for step, t in successors(s, f):
            if t not in parent:
                parent[t] = (s, step)
                q.append(t)
    return None, len(parent), []


STEPS = {  # work.py の言葉で
    "Expire": "担当の期限が切れる（延長が間に合わない）",
    "Claim": "{a} が担当を取る（claim_at。token {t}）",
    "Release": "{a} が担当を手放す（dispatch の release）",
    "OpRunBegin": "{a} が鍵 {k} で op run を始める（op_run の排他の中で begun を書き、ロックのファイルを作って flock を持つ）",
    "ManualDone": "{a} が鍵 {k} を op done で閉じる（dispatch の done）",
    "OpRunKilledInBegin": "{a} が鍵 {k} で op run を始め、begun を書いた後、ロックのファイルを作る前に kill -9 される",
    "OpRunKilled": "鍵 {k} の op run が、監督役を起こす前に kill -9 される",
    "SpawnFails": "鍵 {k} の op run が監督役を起こせず、done（exit 127）を書いてロックのファイルを消す",
    "Spawn": "鍵 {k} の監督役が起動し、ロックの fd を受け継ぐ（op run は fd を閉じる）",
    "StartCmd": "鍵 {k} の監督役がコマンドを起こす（supervise の Popen）",
    "Leftover": "鍵 {k} のコマンドが、プロセスを残す（credential daemon など）",
    "CmdExit": "鍵 {k} のコマンドが終わる",
    "LeftExit": "鍵 {k} のコマンドが残したプロセスが終わる",
    "WriteDone": "鍵 {k} の監督役が排他を取り、done を書く（担当の token が違えば書かない）",
    "Finish": "鍵 {k} の監督役がロックのファイルを消し（finish）、fd を閉じて終わる",
    "SupKilled": "鍵 {k} の監督役が kill -9 される",
    "KeepLease": "鍵 {k} の監督役が、切れた担当を延ばす（keep_lease。token が同じ）",
    "Gc": "run_holder が、誰も持たない鍵 {k} のロックのファイルを消す",
}
MEANING = {
    "AtMostOneCmd": "同じ作業場所で、外部操作が同時に 2 つ走った",
    "SupKeepsLease": "監督役が生きているのに、担当がその監督役の token でなくなった（done を書けない）",
    "BlockedOnlyWhileWorking": "終わっていない外部操作が無いのに、作業場所が止まった（run_holder が数え続ける）",
}


def story(bad, trace):
    """反例を、番号付きの手順の文にする。"""
    rows = []
    for i, (name, *args) in enumerate(trace, 1):
        by_agent = name in ("Claim", "Release", "OpRunBegin", "OpRunKilledInBegin", "ManualDone")  # 動作の引数は (担当, token)・(担当, 鍵)・(担当,)・(鍵,)
        who = {"a": AGENTS[args[0]]} if by_agent else {}
        if name == "Claim":
            who["t"] = args[1]
        elif len(args) > by_agent:
            who["k"] = KEYS[args[-1]]
        rows.append(f"{i}. " + STEPS[name].format(**who))
    return f"{bad} が破れた: {MEANING[bad]}\n" + "\n".join(rows)


class TestOpRunModel(unittest.TestCase):
    def test_the_modeled_code_keeps_the_invariants_in_every_interleaving(self):
        bad, n, trace = check(NOW)
        self.assertIsNone(bad, f"{n} 状態目で反例。scripts/work.py か、モデルの写し方を直す:\n" + (story(bad, trace) if bad else ""))

    def test_the_model_is_the_one_checked_against_tlc(self):
        self.assertEqual(check(NOW)[1], MODELED_STATES, "モデルの状態数が、TLC と照らした数と違う。モデルを変えたなら、同じ変更を "
                         "TLA+ のモデルに入れて TLC と照らしてから MODELED_STATES を書き換える（tests/test_oprun_model.py の冒頭）")

    def test_each_reverted_fix_gives_a_counterexample(self):
        for flag, inv in REVERTED.items():
            with self.subTest(reverted=flag):
                bad, _, trace = check({**NOW, flag: False})
                self.assertEqual(bad, inv, f"修正を戻した版（{flag}）が {inv} の反例を出さない。モデルが修正の効き目を写していない")
                text = story(bad, trace)
                self.assertRegex(text, r"^\w+ が破れた: .+\n1\. ")
                self.assertEqual(len(text.splitlines()), len(trace) + 1)

    def test_the_story_names_each_step_in_work_py_terms(self):
        _, _, trace = check({**NOW, "cmd_holds_lock": False})
        text = story("AtMostOneCmd", trace)
        self.assertIn("kill -9", text)
        self.assertIn("鍵 k1", text)
        self.assertIn("a1 が担当を取る（claim_at。token 1）", text)

    def test_the_model_was_checked_against_the_current_code(self):
        src = WORK.read_text(encoding="utf-8")
        drift = [f"{name}: 照合した {old} / 今 {fingerprint(src, name)}" for name, old in MODELED.items() if fingerprint(src, name) != old]
        if drift:
            self.fail(f"scripts/work.py のモデルが写す関数が、照合した commit {MODELED_AT} から変わった。"
                      "モデルを見直して、照合した commit とハッシュを進める（tests/test_oprun_model.py の冒頭）:\n" + "\n".join(drift))


    def test_the_hashes_are_those_of_the_checked_commit(self):
        root = WORK.parents[1]
        try:
            shallow = subprocess.run(["git", "rev-parse", "--is-shallow-repository"], cwd=root, capture_output=True, text=True, check=True)
        except (OSError, subprocess.CalledProcessError) as e:
            self.skipTest(f"git の履歴を読めない: {e}")
        if shallow.stdout.strip() == "true":  # CI の actions/checkout は履歴を 1 つだけ取る
            self.skipTest("浅い clone なので、照合した commit を読めない")
        old = subprocess.run(["git", "show", f"{MODELED_AT}:scripts/work.py"], cwd=root, capture_output=True, text=True)
        self.assertEqual(old.returncode, 0, f"照合した commit {MODELED_AT} の scripts/work.py を読めない: {old.stderr.strip()}")
        self.assertEqual({name: fingerprint(old.stdout, name) for name in MODELED}, MODELED,
                         f"MODELED のハッシュが、照合した commit {MODELED_AT} の本文のものでない。照合した commit とハッシュを一緒に進める")


class TestFingerprint(unittest.TestCase):
    """照合が、モデルとコードのずれを捕まえ、写さない変更では落ちないか。今の work.py を写した文字列を変えて確かめる。"""

    def setUp(self):
        self.src = WORK.read_text(encoding="utf-8")

    def changed(self, old, new):
        self.assertEqual(self.src.count(old), 1, f"work.py に {old!r} がちょうど 1 つ無い（試験の前提が古い）")
        mutated = self.src.replace(old, new)
        return [name for name in MODELED if fingerprint(mutated, name) != fingerprint(self.src, name)]

    def test_moving_a_line_out_of_the_mutex_is_caught(self):
        # supervise で、done とロックのファイルの削除の間に排他を割る
        self.assertEqual(self.changed("                    warn(f\"出力のファイルを消せない: {e!r}\")\n            finish(r, spec)\n",
                                      "                    warn(f\"出力のファイルを消せない: {e!r}\")\n        finish(r, spec)\n"), ["supervise"])

    def test_passing_the_lock_to_the_command_is_caught(self):
        self.assertEqual(self.changed("pass_fds=[spec[\"lock_fd\"]],", "pass_fds=[],"), ["supervise"])

    def test_a_comment_or_docstring_change_is_not_caught(self):
        self.assertEqual(self.changed("# 2 回目の fork。", "# 二回目の fork。"), [])
        self.assertEqual(self.changed("    \"\"\"コマンドが終わったので、ロックのファイルを消す。", "    \"\"\"コマンドが終わったら、ロックのファイルを消す。"), [])

    def test_a_change_outside_the_modeled_functions_is_not_caught(self):
        self.assertEqual(self.changed("def tail(f: Path, lines: int = 20, budget: int = MAX_TAIL) -> str:",
                                      "def tail(f: Path, lines: int = 30, budget: int = MAX_TAIL) -> str:"), [])

    def test_a_missing_function_is_reported(self):
        with self.assertRaises(LookupError):
            fingerprint(self.src.replace("def keep_lease(", "def keep_lease2("), "keep_lease")


if __name__ == "__main__":
    unittest.main()
