"""op run・監督役・操作ごとのロック・担当の約束（scripts/work.py）を状態機械に写し、モデルが写す段の交互実行と kill -9 を全部たどる。

審査が 1 周に 1 つずつ交互実行を見つけ、そのたびに直していたため、このモデルが写す範囲は 1 回の全探索でたどる。
範囲は担当 2・鍵 2・token 3。担当の期限切れはいつでも起こせる（実際の時間を写さない、保守的な写し方）。
過去の修正を戻した版をフラグで作り、それぞれが反例を出すことも試す（一度も落ちたことのない検査は、何も確かめていない）。
同じ修正を実物の work.py の写しに戻し、その不具合を見るテストが落ちることは、tests/test_replay.py が確かめる（フラグと 1 対 1）。
TLA+ の同じモデル（TLC）とは、2026-11-15 の版（持ち主の照合が期限を見ない Issue #31 の変更）で、状態数（TLC_STATES）と、
戻した版 4 つの違反と反例の動作の数が一致した（TLC は行き詰まりの検査を外して流す。token を使い切った終わりの状態は行き詰まりになり、
このモデルも行き詰まりは見ない）。TLA+ の HolderKeepsAccess は ENABLED で書くので、Next から外しただけの動作も可能と数える
（ここでは successors で見るので、外すと破れる）。TLA+ のモデルはリポジトリに無い（最初の版は Issue #19 の付録）。
2026-11-24 の版は、コマンドの終了コード（cmd の ok・fail）・done の result（res）・照合に拒否された op run のコマンド（stray）と、
b908b17・d5923ce を戻すフラグ 3 つを足した。TLC とは照らしていない（その場で TLC を使えなかった）。代わりに、今の版の状態を、TLC と
照らした変数へ写した数（cmd の ok・fail を exited に畳み、res と stray を落とす）が TLC_STATES と同じことをテストで確かめる（足した値と
動作が、照らした版の動きを変えていない）。足したフラグの反例の動作の数は、TLC と照らしていない。足した分岐は、tests/test_replay.py の
場面（test_work.py の test_a_second_op_run_right_after_the_first_begins_is_refused・test_only_external_begin_and_the_running_done_are_refused_while_running・
test_refused_begin_does_not_run_the_command・test_failed_command_is_recorded_with_its_exit_code）が、今の work.py で通り、戻した写しで落ちる。
モデル（MODEL_PARTS の関数と、それが読む定数）を変えると、その本文のハッシュ（MODELED_MODEL）が変わって落ちる。全探索を通し、
写した数が TLC_STATES のままかを見てから、MODELED_MODEL と MODELED_STATES を書き換える。写した数が変わる変更（照らした版の動きを
変える変更）は、TLA+ のモデルに同じ変更を入れて TLC と照らしてから TLC_STATES も書き換える（TLC は CI に置かないので、照らし直したかは
テストで確かめない）。

MODELED は、モデルが写す経路の関数と、その本文のハッシュ（tests/model_tie.py）。MODELED_AT は、ハッシュを最後に進めたときに読んだ
commit の記録で、テストは確かめない（work.py と同じ commit で進めると、自分を含む commit は書けない。その親を書く）。
ハッシュは、写す関数が変わったことを知らせて、モデルを見直させるためのもの。ハッシュを進めたときにモデルと照らしたかは確かめない。
dispatch は claim・release・op done の分岐だけを、op_step は担当の token の照合と同じ鍵の begin・done の拒否だけを写すが、
ハッシュは関数全体に掛ける（op_step は --reviewed などの写さない照合でも変わる）。locked は作業場所の排他、run_lock は鍵ごとに
別のロックのファイル、ops は鍵ごとの最後の state、read・write は lease.json の token と期限、alive・check_token・held_lease は、担当が
生きているか（ほかの者が取れるか）と、持ち主の操作を通すか（期限を見ず、記録の token で照らす）・done を書けるかを写す。写す関数が拒否を足すだけの変更は、モデルが許す動きを減らすだけなので、不変条件は保たれる
（それでもハッシュは落ちる）。監督役がコマンドを起こさずに done を書く経路（起こす前に止められた、Popen の失敗。result は exit 127 か
128+N）は、StartCmd → CmdFail と cmd の値だけが違う状態に届き、不変条件は cmd が running かと、done の result が終了コードと同じかしか
見ないので写さない。
op run が切れた担当を延ばすこと（OpRunBegin の lease_alive）と、監督役が延ばすこと（KeepLease。b908b17 の延長）は写すが、どの
不変条件もそれに頼らない（どちらも消しても破れない。KeepLease は 2026-11-24 に消して全探索した。run_holder が動いている操作を数えて
claim を止める c877d78 が、切れた担当を取らせない）。消したことは MODELED_MODEL だけが捕まえる。
HolderKeepsAccess は、持ち主でない者の Renew は見るが、Release と ManualDone は見ない（持ち主でない者が手放せる・閉じられる
写し方は、MODELED_MODEL だけが捕まえる。持ち主でない者の OpRunBegin は SupKeepsLease が捕まえる。実コードの release は
test_work.py の test_expired_lease_can_be_taken_over_and_old_holder_is_stopped が守る。op done の token の照合は、Issue #31 の前からの
op_step の check_token）。
run_holder が外れたロックのファイルを消すのは、モデルでは独立した動作（Gc）で、実コードのように呼ぶたびにすぐ消すとは限らない
（実コードの動きはモデルの動きに含まれ、写しすぎは不変条件を破る側にしか働かない）。
照合に拒否された op run のコマンド（RunRefused。d5923ce を戻した版だけで起きる）は、鍵も記録も持たない 1 つの外部操作として写す。
写さない経路（ハッシュも掛けない。変えても、このテストは落ちない）: task_command（task start・resume の claim、cleanup の
担当の削除。タスクの持ち主と閉じる側の間は tests/test_task_model.py が写す。claim_at の take_over が偽の拒否（--take-over の無い
task resume が、期限が切れて MAX_LEASE_MIN 以内の担当を取らない）もそちらが写し、ここの Claim は dispatch の claim（take_over は真））・
current_lease・integrate の排他と照合、pending
（外れたロックの片付けは run_holder として写す）、main の振り分け、シグナルの中継（supervise の 2 回目の fork）、記録の形式と
壊れた記録、出力のファイル、ロックを開けない・読めないとき、op begin（op run を通さない）。

照合が落ちたら、変わった関数を読み、モデル（successors）が今の本文を写していることを確かめる（違えば直して全探索を通す）。
そのうえでハッシュと MODELED_AT を書き換える。写す分岐に関係しない変更（dispatch の status など）でも同じにする。
"""

import hashlib
import sys
import unittest
from collections import deque
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent)); import process_limit  # noqa: E402,F401  テストに上限を掛ける（直に走らせても）
from model_tie import body, drift, fingerprint  # noqa: E402

WORK = Path(__file__).resolve().parents[1] / "scripts" / "work.py"

MODELED_AT = "53be7d3"
TLC_STATES = 84981  # TLC と照らした版の状態数（TLC_FIELDS に写した数）
MODELED_STATES = 222085
MODELED_MODEL = "0bad1c49061cf3a4"
MODELED = {
    "read": "8372930316b7ee94",
    "write": "00c93a6aa7ceb1e8",
    "locked": "0a22de1033650c4d",
    "ops": "19f01a5bc3cb84ad",
    "op_step": "1550ba5e3d424b54",
    "run_lock": "fdd3edfffe9c0b10",
    "alive": "f1f9574a8ad75670",
    "check_token": "6297f65bcd57f11c",
    "held_lease": "2e230a09eab2bcdd",
    "claim_at": "d13a239d32440d08",
    "run_holder": "ed7f517a536e94fd",
    "keep_lease": "1ff486dd87b26989",
    "op_run": "ec713203b25a8d89",
    "finish": "cac6b578315e4a7d",
    "supervise": "126b7e2bbc8b5c5a",
    "dispatch": "7b2ce7ae7c39483f",
}


MODEL_PARTS = ("init", "held_key", "successors", "violated")  # このテストのモデル。変えたら、TLC と照らした変数へ写した数を見直す


def model_fingerprint() -> str:
    src = Path(__file__).read_text(encoding="utf-8")
    return hashlib.sha256("\n".join(body(src, n) for n in MODEL_PARTS).encode("utf-8")).hexdigest()[:16]


# ---- 状態機械 ----
# 状態は次の組。鍵ごとの値は KEYS の順の組。
#   lease_tok: lease.json の token（0 は無い）  lease_alive: 期限内か  next_tok: 次に払い出す token
#   tok_of: 担当ごとの、最後に受け取った token  rec: 鍵ごとの ops.jsonl の state（none・begun・done）
#   lock_file: 鍵ごとのロックのファイルがあるか  holders: 鍵ごとの、そのロックの flock を持つもの（oprun・sup・cmd・left）
#   oprun: 鍵ごとの op run（none・spawning・spawned・dead・failed）  sup: 鍵ごとの監督役（none・started・waiting・wrote・exited・killed）
#   sup_tok: 鍵ごとの、監督役が受け取った token  cmd: 鍵ごとのコマンド（none・running・ok・fail。ok と fail は終わった後の終了コード）
#   left: 鍵ごとの、コマンドが残したプロセス（credential daemon など）が生きているか
#   res: 鍵ごとの、done の記録の result（-・ok・fail・hand。hand は監督役でない者が書いたもの）
#   stray: 照合に拒否された op run が走らせたコマンドの数（0 か 1。今のコードでは走らない）

AGENTS = ("a1", "a2")
KEYS = ("k1", "k2")
MAX_TOK = 3
LIMIT = 1_000_000  # 状態の数の上限（超えたら、たどり切れていないので落とす）
ALIVE = {"started", "waiting", "wrote"}  # 監督役が生きている段
EXITED = ("ok", "fail")  # 終わったコマンドの終了コード
FIELDS = ("lease_tok", "lease_alive", "next_tok", "tok_of", "rec", "lock_file", "holders", "oprun", "sup", "sup_tok", "cmd", "left",
          "res", "stray")
TLC_FIELDS = FIELDS[:12]  # TLC と照らした版の変数（cmd の終わりは exited の 1 つ）

# 今のコードと、過去の修正を 1 つずつ戻した版。True が今のコード
NOW = {"cmd_holds_lock": True, "done_lock_free": True, "claim_checks_run": True, "holder_by_token": True,
       "run_blocks_ops": True, "run_after_check": True, "records_exit": True}
REVERTED = {
    # d1b5462: コマンド（とそれが残したもの）にもロックの fd を渡す（supervise の Popen の pass_fds）
    "cmd_holds_lock": "AtMostOneCmd",
    # 5c1f45a: 鍵が done のロックは数えない（run_holder）
    "done_lock_free": "BlockedOnlyWhileWorking",
    # c877d78: op run のコマンドが動いている間は担当を取らせない（claim_at の run_holder）
    "claim_checks_run": "SupKeepsLease",
    # Issue #31: 持ち主の操作は期限を見ず、記録の token で照らす（held_lease）。戻すと、誰も取っていないのに持ち主が手放せない
    "holder_by_token": "HolderKeepsAccess",
    # b908b17: op run のコマンドが動いている間は、同じ作業場所の op run と、その鍵の手での op done を拒否する（op_run と dispatch の run_holder）
    "run_blocks_ops": "SupKeepsLease",
    # d5923ce: 照合を通ったときだけコマンドを走らせる（op_run が begin の拒否で返る）。戻すと、begin とコマンドを手で並べたのと同じ
    "run_after_check": "AtMostOneCmd",
    # d5923ce: done の result は、コマンドの実際の終了コード（supervise の exit {rc}）。戻すと、打った文字（ok）が残る
    "records_exit": "RecordIsTheExit",
}


def init():
    per = lambda v: tuple(v for _ in KEYS)  # noqa: E731
    return (0, False, 1, tuple(0 for _ in AGENTS), per("none"), per(False), per(frozenset()), per("none"), per("none"),
            per(0), per("none"), per(False), per("-"), 0)


def upd(t, i, v):
    return t[:i] + (v,) + t[i + 1:]


def held_key(s, k, f):
    """run_holder がその鍵を数えるか: ファイルがあり、誰かが flock を持ち、（今のコードでは）鍵が done でない。"""
    _, _, _, _, rec, lock_file, holders, *_ = s
    return lock_file[k] and bool(holders[k]) and (not f["done_lock_free"] or rec[k] != "done")


def successors(s, f):
    """(動作, 次の状態) を返す。動作は (名前, 担当か鍵の番号…)。Claim だけは (名前, 担当, 払い出す token)。"""
    lease_tok, lease_alive, next_tok, tok_of, rec, lock_file, holders, oprun, sup, sup_tok, cmd, left, res, stray = s
    mutex = "wrote" in sup  # 作業場所の排他（locked(r)）。監督役が done を書いてからロックのファイルを消すまで持つ
    held = any(held_key(s, k, f) for k in range(len(KEYS)))
    blocked = held and f["run_blocks_ops"]  # op_run・dispatch の run_holder による拒否

    def mk(**kw):
        d = dict(zip(FIELDS, s))
        d.update(kw)
        return tuple(d[n] for n in FIELDS)
    if lease_alive:  # 時間が過ぎて担当が切れる（keep_lease の延長が間に合わない場合を含む）
        yield ("Expire",), mk(lease_alive=False)
    for a in range(len(AGENTS)):
        live = (lease_alive or f["holder_by_token"]) and lease_tok != 0 and tok_of[a] == lease_tok  # held_lease・check_token
        if not mutex and not lease_alive and next_tok <= MAX_TOK and (not f["claim_checks_run"] or not held):  # claim_at
            yield ("Claim", a, next_tok), mk(lease_tok=next_tok, lease_alive=True, next_tok=next_tok + 1, tok_of=upd(tok_of, a, next_tok))
        if not mutex and live and not held:  # dispatch の release
            yield ("Release", a), mk(lease_tok=0, lease_alive=False)
        if not mutex and live and not lease_alive:  # dispatch の renew。生きた担当の renew は、モデルでは状態を変えない
            yield ("Renew", a), mk(lease_alive=True)
        for k in range(len(KEYS)):
            if not mutex and live and not blocked and rec[k] == "none":  # op_run の排他の中: begun を書き、ロックを作って持ち、担当を延ばす
                yield ("OpRunBegin", a, k), mk(rec=upd(rec, k, "begun"), lock_file=upd(lock_file, k, True),
                                               holders=upd(holders, k, frozenset({"oprun"})), oprun=upd(oprun, k, "spawning"),
                                               sup_tok=upd(sup_tok, k, tok_of[a]), lease_alive=True)
                # begun を書いた後、ロックのファイルを作る前に kill -9 される（排他は OS が外す）。作った後の死は OpRunKilled と同じ
                yield ("OpRunKilledInBegin", a, k), mk(rec=upd(rec, k, "begun"), oprun=upd(oprun, k, "dead"))
            elif not mutex and not f["run_after_check"] and not stray:  # 戻した版: 照合に拒否されても、コマンドを走らせる
                yield ("RunRefused", a, k), mk(stray=1)
            if not mutex and live and rec[k] == "begun" and not (held_key(s, k, f) and f["run_blocks_ops"]):  # dispatch の op done（照合で閉じる）
                yield ("ManualDone", a, k), mk(rec=upd(rec, k, "done"), res=upd(res, k, "hand"))
    if stray:
        yield ("StrayExit",), mk(stray=0)
    for k in range(len(KEYS)):
        h = holders[k]
        hold = f["cmd_holds_lock"] and lock_file[k]  # コマンドとそれが残したものが、ロックの fd を受け継ぐか
        if oprun[k] == "spawning":
            yield ("OpRunKilled", k), mk(oprun=upd(oprun, k, "dead"), holders=upd(holders, k, h - {"oprun"}))
            yield ("Spawn", k), mk(oprun=upd(oprun, k, "spawned"), sup=upd(sup, k, "started"),
                                   holders=upd(holders, k, (h - {"oprun"}) | {"sup"}))
            if not mutex:  # 監督役を起こせない（OSError）。排他の中で done（exit 127）を書き、ロックのファイルを消し、fd を閉じる
                mine = lease_tok == sup_tok[k]
                yield ("SpawnFails", k), mk(oprun=upd(oprun, k, "failed"), rec=upd(rec, k, "done") if mine else rec,
                                            res=upd(res, k, "hand") if mine else res,
                                            lock_file=upd(lock_file, k, False), holders=upd(holders, k, h - {"oprun"}))
        if sup[k] == "started":
            yield ("StartCmd", k), mk(sup=upd(sup, k, "waiting"), cmd=upd(cmd, k, "running"),
                                      holders=upd(holders, k, h | {"cmd"} if hold else h))
        if cmd[k] == "running" and not left[k]:
            yield ("Leftover", k), mk(left=upd(left, k, True), holders=upd(holders, k, h | {"left"} if hold else h))
        if cmd[k] == "running":
            yield ("CmdExit", k), mk(cmd=upd(cmd, k, "ok"), holders=upd(holders, k, h - {"cmd"}))
            yield ("CmdFail", k), mk(cmd=upd(cmd, k, "fail"), holders=upd(holders, k, h - {"cmd"}))
        if left[k]:
            yield ("LeftExit", k), mk(left=upd(left, k, False), holders=upd(holders, k, h - {"left"}))
        if not mutex and sup[k] == "waiting" and cmd[k] in EXITED:  # supervise の op_step done。token が違えば書かない
            mine = lease_tok == sup_tok[k]
            yield ("WriteDone", k), mk(sup=upd(sup, k, "wrote"), rec=upd(rec, k, "done") if mine else rec,
                                       res=upd(res, k, cmd[k] if f["records_exit"] else "ok") if mine else res)
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
    lease_tok, lease_alive, next_tok, tok_of, rec, lock_file, holders, oprun, sup, sup_tok, cmd, left, res, stray = s
    n = range(len(KEYS))
    # 同じ作業場所で、外部操作が同時に 2 つ走らない（後任が終わっていない操作をやり直さない）
    if sum(c == "running" for c in cmd) + stray > 1:
        return "AtMostOneCmd"
    # 監督役が書いた done の result は、そのコマンドの実際の終了コード（失敗した push を ok と記録しない）
    if any(rec[k] == "done" and res[k] in EXITED and res[k] != cmd[k] for k in n):
        return "RecordIsTheExit"
    # 監督役が生きている間、担当はその監督役の token のまま（done を書ける）
    if any(sup[k] in ("started", "waiting") and lease_tok != sup_tok[k] for k in n):
        return "SupKeepsLease"
    # 作業場所が止まるのは、終わっていない外部操作がまだ続いているかもしれない間だけ（排他の間は誰も run_holder を呼べない）
    if "wrote" not in sup and any(held_key(s, k, f) for k in n) and not any(
            rec[k] != "done" and (cmd[k] == "running" or left[k] or sup[k] in ALIVE or oprun[k] == "spawning") for k in n):
        return "BlockedOnlyWhileWorking"
    # 記録の token が自分のもの（誰も取り直さず、手放してもいない）なら、排他と動いている操作が無いとき、期限が切れていても、
    # 持ち主は手放せ・切れた担当を延ばせ・まだ無い鍵で op run を始め・始めたままの鍵を閉じられる。持ち主でない者は延ばせない（Issue #31）
    if "wrote" not in sup and not any(held_key(s, k, f) for k in n) and lease_tok != 0:
        steps = {step for step, _ in successors(s, f)}
        for a in range(len(AGENTS)):
            if tok_of[a] != lease_tok:
                if ("Renew", a) in steps:
                    return "HolderKeepsAccess"
                continue
            need = {("Release", a)} | ({("Renew", a)} if not lease_alive else set())
            need |= {("OpRunBegin", a, k) for k in n if rec[k] == "none"} | {("ManualDone", a, k) for k in n if rec[k] == "begun"}
            if not need <= steps:
                return "HolderKeepsAccess"
    return None


def tlc_view(s):
    """TLC と照らした版の変数への写像。終わったコマンドの ok・fail を exited に畳み、res と stray を落とす。"""
    d = dict(zip(FIELDS, s))
    d["cmd"] = tuple("exited" if c in EXITED else c for c in d["cmd"])
    return tuple(d[n] for n in TLC_FIELDS)


def check(f, seen=None):
    """幅優先で全部たどる。(破れた不変条件か None, 数えた状態の数, 最短の反例の動作の列)。seen を渡すと、たどった状態を足す。"""
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
                if len(parent) > LIMIT:
                    raise RuntimeError(f"状態の数が上限 {LIMIT} を超えた（たどり切れていない）")
    if seen is not None:
        seen.update(parent)
    return None, len(parent), []


STEPS = {  # work.py の言葉で。文の言い回しはテストしない（番号付きの手順で出ることだけを試す）
    "Expire": "担当の期限が切れる（延長が間に合わない）",
    "Claim": "{a} が担当を取る（claim_at。token {t}）",
    "Release": "{a} が担当を手放す（dispatch の release）",
    "Renew": "{a} が切れた担当を延ばす（dispatch の renew。記録の token が同じ）",
    "OpRunBegin": "{a} が鍵 {k} で op run を始める（op_run の排他の中で begun を書き、ロックのファイルを作って flock を持つ）",
    "ManualDone": "{a} が鍵 {k} を op done で閉じる（dispatch の done）",
    "OpRunKilledInBegin": "{a} が鍵 {k} で op run を始め、begun を書いた後、ロックのファイルを作る前に kill -9 される",
    "OpRunKilled": "鍵 {k} の op run が、監督役を起こす前に kill -9 される",
    "SpawnFails": "鍵 {k} の op run が監督役を起こせず、done（exit 127）を書いてロックのファイルを消す",
    "Spawn": "鍵 {k} の監督役が起動し、ロックの fd を受け継ぐ（op run は fd を閉じる）",
    "StartCmd": "鍵 {k} の監督役がコマンドを起こす（supervise の Popen）",
    "Leftover": "鍵 {k} のコマンドが、プロセスを残す（credential daemon など）",
    "CmdExit": "鍵 {k} のコマンドが終わる（終了コード 0）",
    "CmdFail": "鍵 {k} のコマンドが失敗して終わる（0 でない終了コード）",
    "RunRefused": "{a} の鍵 {k} の op run が照合に拒否されるが、コマンドは走る",
    "StrayExit": "照合に拒否された op run のコマンドが終わる",
    "LeftExit": "鍵 {k} のコマンドが残したプロセスが終わる",
    "WriteDone": "鍵 {k} の監督役が排他を取り、done を書く（担当の token が違えば書かない）",
    "Finish": "鍵 {k} の監督役がロックのファイルを消し（finish）、fd を閉じて終わる",
    "SupKilled": "鍵 {k} の監督役が kill -9 される",
    "KeepLease": "鍵 {k} の監督役が、切れた担当を延ばす（keep_lease。token が同じ）",
    "Gc": "run_holder が、誰も持たない鍵 {k} のロックのファイルを消す",
}
MEANING = {
    "AtMostOneCmd": "同じ作業場所で、外部操作が同時に 2 つ走った",
    "RecordIsTheExit": "監督役が書いた done の result が、コマンドの実際の終了コードと違う",
    "SupKeepsLease": "監督役が生きているのに、担当がその監督役の token でなくなった（done を書けない）",
    "BlockedOnlyWhileWorking": "終わっていない外部操作が無いのに、作業場所が止まった（run_holder が数え続ける）",
    "HolderKeepsAccess": "誰も取り直していないのに、記録の token の持ち主が手放す・延ばす・op run を始める・閉じるのどれかをできない"
                         "（期限切れで締め出された）か、持ち主でない者が担当を延ばせる",
}


def story(bad, trace):
    """反例を、番号付きの手順の文にする。"""
    rows = []
    for i, (name, *args) in enumerate(trace, 1):
        by_agent = name in ("Claim", "Release", "Renew", "OpRunBegin", "OpRunKilledInBegin", "ManualDone", "RunRefused")  # 動作の引数は (担当, token)・(担当, 鍵)・(担当,)・(鍵,)・()
        who = {"a": AGENTS[args[0]]} if by_agent else {}
        if name == "Claim":
            who["t"] = args[1]
        elif len(args) > by_agent:
            who["k"] = KEYS[args[-1]]
        rows.append(f"{i}. " + STEPS[name].format(**who))
    return f"{bad} が破れた: {MEANING[bad]}\n" + "\n".join(rows)


class TestOpRunModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seen = set()
        cls.now = check(NOW, cls.seen)  # 今の版の全探索は 1 回だけ（下のテストが同じ結果を使う）

    def test_the_modeled_code_keeps_the_invariants_in_every_interleaving(self):
        bad, n, trace = self.now
        self.assertIsNone(bad, f"{n} 状態をたどって反例。scripts/work.py か、モデルの写し方を直す:\n" + (story(bad, trace) if bad else ""))

    def test_the_model_is_the_one_checked_against_tlc(self):
        self.assertEqual(len({tlc_view(s) for s in self.seen}), TLC_STATES,
                         "TLC と照らした変数へ写した状態の数が、TLC と照らした数と違う（足した値や動作が、照らした版の動きを変えた）。"
                         "意図した変更なら、TLA+ のモデルに同じ変更を入れて TLC と照らしてから TLC_STATES を書き換える（tests/test_oprun_model.py の冒頭）")
        self.assertEqual(self.now[1], MODELED_STATES, "モデルの状態数が、見直した数と違う。モデルを変えたなら、写した数を見てから "
                         "MODELED_STATES を書き換える（tests/test_oprun_model.py の冒頭）")
        self.assertEqual(model_fingerprint(), MODELED_MODEL,
                         f"モデル（{'・'.join(MODEL_PARTS)}）が、見直したものと違う。全探索と写した数を見てから "
                         "MODELED_MODEL と MODELED_STATES を書き換える（tests/test_oprun_model.py の冒頭）")

    def test_each_reverted_fix_gives_a_counterexample(self):
        self.assertEqual(set(REVERTED), set(NOW), "今のコードのフラグごとに、戻した版の行が要る（行を消すと、その修正を確かめなくなる）")
        for flag, inv in REVERTED.items():
            with self.subTest(reverted=flag):
                bad, _, trace = check({**NOW, flag: False})
                self.assertEqual(bad, inv, f"修正を戻した版（{flag}）が {inv} の反例を出さない。モデルが修正の効き目を写していない"
                                 + (f"。出た反例:\n{story(bad, trace)}" if bad else ""))
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
        changed = drift(WORK.read_text(encoding="utf-8"), MODELED)
        if changed:
            self.fail(f"scripts/work.py のモデルが写す関数が、照合した commit {MODELED_AT} から変わった。"
                      "モデルを見直して、照合した commit（MODELED_AT）とハッシュを進める（tests/test_oprun_model.py の冒頭）:\n" + "\n".join(changed))


class TestFingerprint(unittest.TestCase):
    """照合が、モデルとコードのずれを捕まえ、写さない変更では落ちないか。今の work.py を写した文字列を変えて確かめる。"""

    def setUp(self):
        self.src = WORK.read_text(encoding="utf-8")

    def changed(self, old, new):
        self.assertEqual(self.src.count(old), 1, f"work.py に {old!r} がちょうど 1 つ無い（テストの前提が古い）")
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

    def test_code_after_the_docstring_on_its_line_is_caught(self):
        self.assertEqual(self.changed("    \"\"\"コマンドが終わったので、ロックのファイルを消す。", "    \"\"\"コマンドが終わったので、ロックのファイルを消す。\"\"\"; return\n    \"\"\""),
                         ["finish"])

    def test_a_constant_the_modeled_functions_read_is_caught(self):
        self.assertEqual(self.changed("MAX_LEASE_MIN = 24 * 60", "MAX_LEASE_MIN = 0"), ["alive"])

    def test_a_missing_function_is_reported(self):
        with self.assertRaises(LookupError):
            fingerprint(self.src.replace("def keep_lease(", "def keep_lease2("), "keep_lease")

    def test_a_second_binding_of_a_modeled_name_is_reported(self):
        tail = "\n\nif __name__ == \"__main__\":"
        self.assertEqual(self.src.count(tail), 1)
        for name, extra in (("claim_at", "def claim_at(r, d, holder, minutes):\n    return None"),
                            ("finish", "finish = lambda r, spec: None"), ("write", "from os import write"),
                            ("run_holder", "if True:\n    run_holder = lambda r: None")):
            with self.subTest(extra=extra), self.assertRaises(LookupError):
                fingerprint(self.src.replace(tail, "\n\n" + extra + tail), name)


if __name__ == "__main__":
    unittest.main()
