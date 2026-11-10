"""子の一時の置き場の片付け・最初の起動・続きの起動（scripts/launch.py）を状態機械に写し、モデルが写す段の交互実行と kill -9 を全部たどる。

審査が 3 周、同じ処理の順序と止め方（排他の後の古い記録、途中で止まった片付けの後の続き、排他の取り方）を 1 つずつ見つけたので、
このモデルが写す範囲は 1 回の全探索でたどる。道具は Python の標準機能だけの、明示状態の幅優先の全探索（依存が要らず、試験に置ける）。

約束（破れたときの被害）:
- A1 子は、消し終えた置き場では動かない（続きの子が、前の起動で写した対象を全部失ったまま続きを始める）。記録の行が何であっても。
- B  片付けは、子が動いている置き場を消さない（動いている子の写しが消える）。記録の行が何であっても。
- A2 子は、片付けが消し始めた置き場（途中で止まって半分になったもの）では動かない（写しの一部を失ったまま続きを始める）。正直な範囲だけ。
- C  片付けが消し始める時点で、その置き場は、いまの記録といまの時刻でも対象である（7 日以内に使われた置き場が消える）。正直な範囲だけ。
- H  半分になった置き場は、いつも対象である（対象でなくなると、次の片付けが残りを消さず、続きが半分の置き場から始まる）。正直な範囲だけ。
- E  最初の起動は、置き場を作ってから排他を取るまでの間に、片付けに置き場を消されて失敗しない。正直な範囲だけ。
- L  誰も動いていない状態（途中で死んだ片付け・続きの後でも）から片付けを 1 回走らせると、対象の置き場は無くなる（残り続けない）。

正直な範囲: 委任の記録（delegations.jsonl）の行は launcher が書いたものだけ。時計は戻らない。無くなったタスクの作業場所は、同じ道筋に
作り直されない。その外（偽の行・時計の巻き戻し・作業場所の作り直し）では A2・C・H・E が破れることを、このモデルが反例で示す
（test_outside_the_honest_range_the_model_finds_the_gaps_the_text_admits。守らない範囲として sweep_leftovers・place_due の説明に書いてある）。

範囲（上限）: 置き場 1 つ。期限 D は 1 刻み。時計は 0〜2（もうある置き場の場面）か 0〜1（最初の起動の場面。作ってから排他を取るまでに
期限が過ぎることは写さない）。続きの起動 2・片付けだけの起動 2（もうある置き場の場面）、最初の起動 1・続き 1・片付け 2（最初の起動の場面）。
kill -9 は、動いているどれにも、どの段の間にも。偽の行は 2 回まで、時計の巻き戻しは 1 回まで（正直な範囲の外を調べるときだけ）。
記録の行の集まりは、片付けの判定が見るもの（行が無いか、最も新しい at と、対象がどれもタスクの作業場所か）に畳む。

モデルの動作と、対応するコード（scripts/launch.py）:
- 置き場の排他 hold_place の 3 段: 開く（os.open）→ 排他（fcntl.flock）→ まだその名前にあるか（samestat）。
- 最初の起動（main の「最初の起動は、作った直後のここで取る」と codex_read_only の scratch.mkdir、grok_home の mkdtemp）:
  作る → hold_place → 子 → 記録の行を書く（main の終わり。at は起動の終わりの時刻）→ 終わる（排他は、プロセスが終わると OS が放す）。
  hold_place に失敗した最初の起動は、子を起こさず、置き場を指す行を書いて終わる（読み取り専用の Codex。記録の scratch は排他の前に入る。
  Grok の最初の起動は、失敗しても置き場を指す行を書かないが、モデルは Codex の形で写す。正直な範囲では、この段に届かない）。
  対象（タスクの作業場所）が無くなっていれば、子は起きず、launcher は置き場を指す失敗の行を書いて終わる。
- 続き（main の --resume の「続きが使う一時の置き場の排他を、ここで取って終わりまで持つ」）: hold_place → place_due（記録を読む → いまの
  時刻で決める）→ 子 → 行を書く → 終わる。
- 片付け（sweep_leftovers）: 時刻を決める（main が渡す now。片付けの間は変わらない）→ 置き場を並べる（leftover_places）→ 記録を読む
  （leftover_uses）→ 決める（leftover_due）→ hold_place → 記録を読み直す → 決め直す → 消す（remove_tree。モデルでは「半分」と「無い」の 2 段）
  → 排他を放す。
- 外で起こること: 時計が進む、タスクの作業場所が片付けられる（task cleanup）、kill -9（持っていた排他は OS が放すものとして写す）。

省いたもの（これらに触れる性質は、このモデルでは確かめていない）:
- 状態の置き場の中の名前の付け替え（改名・symlink への差し替え）。remove_tree が fd から降りること、symlink の置き場。試験は
  tests/test_launch.py の test_a_directory_swapped_for_a_link_during_the_sweep_is_not_followed など。排他の後の付け替えは守らない範囲。
- 置き場が 2 つ以上あるとき（片付けは置き場ごとに独立に決める。排他を取れない環境で全部をやめる分岐は写さない）。Grok の続きが、前の home の
  排他を持ったまま新しい home を作ること（2 つの置き場の、それぞれ 1 つの場面として写る）。
- ディレクトリの flock が効かない環境、読めない権限の置き場、使えない時刻（未来・NaN）、名前の日時を現地時刻で解くこと。
- 続きの起動のほかの照合（同じ session の排他 hold_session、空の残り、権限）。モデルは同じ置き場の続きを 2 つ同時に許すので、実物より広い。
- 続きの launcher 自身の片付け（自分が持つ置き場は、自分の別の fd の排他に当たって飛ばす。OS の flock の振る舞いに拠る）。
- record.json を書いてから delegations.jsonl に足すまでの間の kill（続きは record.json から、片付けは delegations.jsonl から読む）。
- 子が置き場の中でしたこと、SIGTERM の中継、時間切れ。

実物との突き合わせ（2026-11-10、偽の CLI で scripts/launch.py を動かした）: モデルの分岐は、それぞれ tests/test_launch.py の次の試験が
実物で通る。使っていない対象の置き場を消す（test_the_launcher_removes_its_leftovers_after_the_retention・
test_places_of_a_cleaned_up_task_go_without_waiting）、ほかの launcher が持つ置き場を飛ばす
（test_a_place_in_use_is_kept_whatever_the_records_say）、排他の後に記録を読み直す
（test_a_place_used_again_while_the_sweep_was_deciding_is_kept）、続きの拒否 3 つ（無い: test_a_resume_is_refused_once_its_place_is_gone、
対象: test_a_place_due_for_removal_cannot_be_continued_even_before_it_is_removed、使用中:
test_a_resume_waits_while_another_launcher_holds_its_place）、開いた後に消された置き場（test_a_place_swept_just_before_a_resume_takes_it_is_not_used）。
試験に無い場面は、片付けを 1 つ消したところで kill -9 して半分の置き場を作り、続きを起動して比べた（1 回きりの確かめで、試験には残して
いない）: 正直な範囲（作業場所が無くなった置き場・8 日前の置き場）では、続きは拒否され、次の片付けが残りを消した。作業場所を同じ道筋に
作り直す・いまの時刻の偽の行を足す・時計を 8 日戻す、のそれぞれでは、続きの子が半分の置き場で起きた（どれもモデルの予想どおり）。
違いは 1 つ出て、モデルを直した: 対象の作業場所が無いまま続きの照合を通った場合（行を消す偽の行）、モデルは子を起こしたが、実物は
子を起こせずに失敗の行を書いて終わった（今は、そう写してある）。

外しても、このモデルの試験が落ちない判定（2026-11-10 に注入して確かめた）: 続きが、対象の無くなった作業場所の置き場を拒む判定（place_due の
作業場所の有無）は、このモデルの約束には要らない。対象が無ければ子は起きないので、A2 は破れない（拒否の理由を、分かる文で先に出すための
判定で、tests/test_launch.py の test_a_place_due_for_removal_cannot_be_continued_even_before_it_is_removed が実物で見る）。対象が無いと子が
起きない分岐（nochild）を外すと、モデルは実物より多くの動き（実物では起きない子）を許すだけで、正直な範囲の合否は変わらない。

対象の関数: hold_place・leftover_places・leftover_uses・leftover_due・place_due・sweep_leftovers・remove_tree・grok_home と、main の
--resume の置き場の照合・最初の起動の排他・記録を書く段。これらを変えたら、このモデル（step）が今の本文を写しているかを読み直す
（機械では検出しない）。
"""

import sys
import unittest
from collections import deque
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent)); import process_limit  # noqa: E402,F401  試験に上限を掛ける（直に走らせても）

D = 1  # 期限（刻み）。実物は LEFTOVER_DAYS 日
TOP = {"existing": 2, "fresh": D}  # 時計の上限。fresh は、作ってから排他を取るまでに期限が過ぎることを写さない
LIMIT = 3_000_000  # 状態の数の上限（超えたら、たどり切れていないので落とす）
UNSET = -1
NO_ROWS = 0


def rows_with(rows: int, at: int, cwd: str) -> int:
    """記録の行の集まりに 1 行足す。集まりは、片付けの判定が見るものだけに畳む: 行が無い（0）か、最も新しい at と、対象がどれもタスクの作業場所か。"""
    if rows == NO_ROWS:
        return 1 + at * 2 + (cwd == "W")
    newest, all_tasks = (rows - 1) // 2, (rows - 1) % 2 == 1
    return 1 + max(newest, at) * 2 + (all_tasks and cwd == "W")


def due(rows: int, stamp: int, now: int, tree: str) -> bool:
    """leftover_due: 記録の対象が、どれも、もう無いタスクの作業場所か、最後に使われてから D を過ぎたか。最後に使われた時刻は、行の at と名前の日時の新しいほう。"""
    if rows == NO_ROWS:
        return now - stamp > D
    newest, all_tasks = (rows - 1) // 2, (rows - 1) % 2 == 1
    return (all_tasks and tree == "gone") or now - max(newest, stamp) > D


def start(scene: str, cwd: str = "W"):
    """初めの状態。existing: 前の起動が終わって、行が 1 つある置き場。fresh: まだ無い置き場（最初の起動が作る）。
    状態は (時計, 置き場, 名前の日時, 記録の行, 作業場所, 続きから起動できる記録があるか, 偽の行の数, 巻き戻しの数, 動くもの)。
    動くものは (種類, 段, 排他を持つか, 手元の値 2 つ) で、並べ替えて持つ（同じ種類の 2 つは入れ替えても同じ状態）。"""
    if scene == "existing":
        actors = (("R", "idle", False, UNSET, UNSET),) * 2 + (("S", "idle", False, UNSET, UNSET),) * 2
        return (0, "whole", 0, rows_with(NO_ROWS, 0, cwd), "exists", True, 0, 0, actors)
    actors = (("F", "idle", False, UNSET, UNSET), ("R", "idle", False, UNSET, UNSET)) + (("S", "idle", False, UNSET, UNSET),) * 2
    return (0, "unborn", UNSET, NO_ROWS, "exists", False, 0, 0, actors)


def step(s, scene: str, cwd: str, env: frozenset, broken: frozenset):
    """状態 s から 1 段で行ける (動作の名前, 次の状態, その段で破れた約束) を全部返す。
    env は、正直な範囲の外の動作（"recreate" 作業場所の作り直し・"forge" 偽の行・"back" 時計の巻き戻し）。
    broken は、コードの判定を 1 か所外した版（過去の直しを戻した版）。"""
    clock, place, stamp, rows, tree, resumable, forged, backs, actors = s
    locked = any(a[2] for a in actors)
    out = []

    def put(name, bad=(), i=None, actor=None, **kw):
        v = dict(clock=clock, place=place, stamp=stamp, rows=rows, tree=tree, resumable=resumable, forged=forged, backs=backs)
        v.update(kw)
        new = actors if i is None else tuple(sorted(actors[:i] + (actor,) + actors[i + 1:]))
        out.append((name, (v["clock"], v["place"], v["stamp"], v["rows"], v["tree"], v["resumable"], v["forged"], v["backs"], new), tuple(bad)))

    def running_elsewhere(i):
        return any(a[1] == "run" for j, a in enumerate(actors) if j != i)

    def child(i, kind):  # 子を起こす。置き場が欠けていれば、約束が破れている
        if cwd == "W" and tree == "gone":  # 対象が無ければ、子は起きない（launcher は失敗の行を書いて終わる。行は置き場を指す）
            put(f"{kind}.nochild", rows=rows_with(rows, clock, cwd), i=i, actor=(kind, "wrote", actors[i][2], UNSET, UNSET))
            return
        put(f"{kind}.child", i=i, actor=(kind, "run", actors[i][2], UNSET, UNSET),
            bad=[p for p, hit in (("A1", place == "none"), ("A2", place != "whole")) if hit])

    # 外で起こること
    if clock < TOP[scene]:
        put("tick", clock=clock + 1)
    if cwd == "W" and tree == "exists":
        put("cleanup", tree="gone")  # task cleanup が作業場所を消す
    if "recreate" in env and tree == "gone":
        put("recreate", tree="exists")
    if "back" in env and backs < 1 and clock > 0:
        put("back", clock=clock - 1, backs=backs + 1)
    if "forge" in env and forged < 2:
        put("forge-drop", rows=NO_ROWS, forged=forged + 1)
        put("forge-new", rows=rows_with(rows, clock, "X"), forged=forged + 1)
        put("forge-old", rows=rows_with(rows, 0, "W"), forged=forged + 1)
    seen_actors = set()
    for i, a in enumerate(actors):
        if a in seen_actors:  # 同じ状態の 2 つは、どちらが動いても同じ
            continue
        seen_actors.add(a)
        kind, pc, holds, x, y = a
        if pc not in ("idle", "end"):  # kill -9。持っていた排他は OS が放す
            put(f"{kind}.kill", i=i, actor=(kind, "end", False, UNSET, UNSET))

        # hold_place の 3 段（開く → 排他 → まだその名前にあるか）。F・R・S で同じ。失敗の行き先だけが違う
        lost = (kind, "failing" if kind == "F" else "end", False, UNSET, UNSET)
        if pc == "open":  # os.open: 無ければ gone
            put(f"{kind}.open", i=i, actor=(kind, "opened", False, x, y) if place in ("whole", "half") else lost)
        elif pc == "opened":  # flock: ほかが持っていれば busy（消し終えた置き場の inode にも、持ち主が放すまで掛かっている）
            if not locked:
                put(f"{kind}.flock", i=i, actor=(kind, "locked", True, x, y))
            elif kind == "S" and "sweep-ignores-busy" in broken:
                put(f"{kind}.flock!", i=i, actor=(kind, "locked", False, x, y))
            else:
                put(f"{kind}.busy", i=i, actor=lost)
        elif pc == "locked":  # samestat: 開いてから排他を取るまでの間に消し終えられていたら gone
            if place == "none" and "no-samestat" not in broken:
                put(f"{kind}.gone", i=i, actor=lost)
            else:
                put(f"{kind}.held", i=i, actor=(kind, "held", holds, x, y))

        elif kind == "F":
            if pc == "idle" and place == "unborn":  # scratch.mkdir・mkdtemp。名前の日時は、いまの時刻
                put("F.mkdir", place="whole", stamp=clock, i=i,
                    actor=("F", "held" if "first-launch-holds-nothing" in broken else "open", False, UNSET, UNSET))
            elif pc == "held":
                child(i, "F")
            elif pc == "run":  # 子が終わり、記録の行を書く（at は、いまの時刻）。使った置き場が欠けていれば、使っている間に消された
                put("F.record", rows=rows_with(rows, clock, cwd), resumable=True, i=i, actor=("F", "wrote", holds, UNSET, UNSET),
                    bad=("C",) if place != "whole" else ())
            elif pc == "wrote":
                put("F.exit", i=i, actor=("F", "end", False, UNSET, UNSET))
            elif pc == "failing":  # 排他を取れなかった最初の起動は、子を起こさず、置き場を指す行を書いて終わる（続きからは起動できない記録）
                put("F.failed", rows=rows_with(rows, clock, cwd), i=i, actor=("F", "end", False, UNSET, UNSET), bad=("E",))

        elif kind == "R":
            if pc == "idle" and resumable:
                if "due-before-lock" in broken:  # 戻した版: 対象かを、排他の前に決める
                    put("R.early", i=i, actor=("R", "end", False, UNSET, UNSET) if due(rows, stamp, clock, tree) else ("R", "open", False, 1, UNSET))
                else:
                    put("R.start", i=i, actor=("R", "open", False, UNSET, UNSET))
            elif pc == "held":
                if x == 1 or "no-due-check" in broken:
                    child(i, "R")
                else:  # place_due の 1 段目: 記録を読む
                    put("R.read", i=i, actor=("R", "read", holds, UNSET, rows))
            elif pc == "read":  # place_due の 2 段目: いまの時刻と、いまの作業場所の有無で決める
                if due(y, stamp, clock, tree):
                    put("R.due", i=i, actor=("R", "end", False, UNSET, UNSET))
                else:
                    child(i, "R")
            elif pc == "run":
                if "record-after-unlock" in broken:  # 戻した版: 排他を放してから行を書く
                    put("R.unlock", i=i, actor=("R", "late", False, UNSET, UNSET))
                else:
                    put("R.record", rows=rows_with(rows, clock, cwd), i=i, actor=("R", "wrote", holds, UNSET, UNSET),
                        bad=("C",) if place != "whole" else ())
            elif pc == "late":
                put("R.record", rows=rows_with(rows, clock, cwd), i=i, actor=("R", "end", False, UNSET, UNSET),
                    bad=("C",) if place != "whole" else ())
            elif pc == "wrote":
                put("R.exit", i=i, actor=("R", "end", False, UNSET, UNSET))

        elif kind == "S":  # x は片付けの時刻（now）、y は読んだ記録
            if pc == "idle":  # main が time.time() を渡す。片付けの間は変わらない
                put("S.start", i=i, actor=("S", "timed", False, clock, UNSET))
            elif pc == "timed":  # leftover_places: 実のディレクトリだけ（半分になった置き場も並ぶ）
                listed = place in ("whole", "half") and not (place == "half" and "half-not-listed" in broken)
                put("S.list", i=i, actor=("S", "listed", False, x, UNSET) if listed else ("S", "end", False, UNSET, UNSET))
            elif pc == "listed":  # leftover_uses
                put("S.read", i=i, actor=("S", "snap", False, x, rows))
            elif pc == "snap":  # leftover_due（1 回目）
                put("S.decide", i=i, actor=("S", "open", False, x, y) if due(y, stamp, x, tree) else ("S", "end", False, UNSET, UNSET))
            elif pc == "held":
                if "no-reread" in broken:  # 戻した版: 排他の前に読んだ記録のまま消す
                    put("S.stale", i=i, actor=("S", "sure", holds, x, UNSET))
                else:  # 排他を取った後に、記録を読み直す
                    put("S.reread", i=i, actor=("S", "again", holds, x, rows))
            elif pc == "again":  # leftover_due（2 回目）
                put("S.again", i=i, actor=("S", "sure", holds, x, UNSET) if due(y, stamp, x, tree) else ("S", "end", False, UNSET, UNSET))
            elif pc == "sure":  # remove_tree の 1 段目（中身を消し始める）
                bad = [p for p, hit in (("B", running_elsewhere(i)), ("C", place == "whole" and not due(rows, stamp, clock, tree))) if hit]
                put("S.remove1", place="half" if place in ("whole", "half") else place, i=i, actor=("S", "half", holds, UNSET, UNSET), bad=bad)
            elif pc == "half":  # remove_tree の 2 段目（置き場そのものを消す）
                put("S.remove2", place="none" if place in ("whole", "half") else place, i=i, actor=("S", "gone", holds, UNSET, UNSET),
                    bad=("B",) if running_elsewhere(i) else ())
            elif pc == "gone":  # os.close(lock)
                put("S.unlock", i=i, actor=("S", "end", False, UNSET, UNSET))
    return out


def sweep_alone(s, scene, cwd, broken):
    """誰も動いていない状態 s から、片付けを 1 回、最後まで走らせた後の置き場。"""
    s = s[:8] + ((("S", "idle", False, UNSET, UNSET),),)
    while s[8][0][1] != "end":
        nexts = [n for name, n, _ in step(s, scene, cwd, frozenset(), broken) if name.startswith("S.") and name != "S.kill"]
        assert len(nexts) == 1, nexts
        s = nexts[0]
    return s[1]


def explore(scene: str, cwd: str = "W", env=(), broken=()):
    """全部の状態をたどり、(状態の数, {破れた約束: 反例の動作の列}) を返す。反例は、約束ごとに最初に見つけた、最も短いもの。"""
    env, broken = frozenset(env), frozenset(broken)
    first = start(scene, cwd)
    came = {first: None}
    todo = deque([first])
    found = {}

    def trace(s, last=None):
        names = [] if last is None else [last]
        while came[s] is not None:
            s, name = came[s]
            names.append(name)
        return names[::-1]

    while todo:
        s = todo.popleft()
        clock, place, stamp, rows, tree = s[:5]
        if place == "half" and not due(rows, stamp, clock, tree):
            found.setdefault("H", trace(s))
        if "L" not in found and all(a[1] in ("idle", "end") for a in s[8]) and place in ("whole", "half") \
                and due(rows, stamp, clock, tree) and sweep_alone(s, scene, cwd, broken) != "none":
            found["L"] = trace(s, "片付けを 1 回走らせても残る")
        for name, n, bad in step(s, scene, cwd, env, broken):
            for p in bad:
                found.setdefault(p, trace(s, name))
            if n not in came:
                came[n] = (s, name)
                todo.append(n)
                if len(came) > LIMIT:
                    raise RuntimeError(f"状態の数が上限 {LIMIT} を超えた（たどり切れていない）")
    return len(came), found


class TestSweepModel(unittest.TestCase):
    def test_the_modeled_code_keeps_the_promises_in_every_interleaving(self):
        """正直な範囲では、どの順序でも、どこで kill -9 しても、約束はどれも破れない。"""
        for scene, cwd in (("existing", "W"), ("existing", "X"), ("fresh", "W")):
            with self.subTest(scene=scene, cwd=cwd):
                states, found = explore(scene, cwd)
                self.assertEqual(found, {})
                self.assertGreater(states, 5_000)  # たどった範囲が、空でないこと

    def test_the_promises_about_any_records_hold_outside_the_honest_range(self):
        """記録の行を書き換えても（消す・いまの時刻の行を足す・無くなった作業場所を指す古い行を足す）、作業場所を作り直しても、時計を戻しても、
        子は消し終えた置き場で動かず、片付けは子が動いている置き場を消さず、誰も動いていなければ片付け 1 回で対象の置き場が無くなる。"""
        for scene, cwd, env in (("existing", "W", "forge"), ("existing", "X", "forge"), ("fresh", "W", "forge"),
                                ("existing", "W", "recreate"), ("existing", "W", "back")):
            with self.subTest(scene=scene, cwd=cwd, env=env):
                _, found = explore(scene, cwd, env=(env,))
                self.assertEqual(sorted(set(found) & {"A1", "B", "L"}), [])

    def test_each_reverted_guard_gives_a_counterexample(self):
        """コードの判定を 1 か所外した版は、その判定が守る約束を破る（一度も落ちたことのない約束は、何も確かめていない）。
        外さない版は、同じ条件でその約束を破らない。"""
        whole = {}
        for broken, scene, env, promise in (
                ("no-due-check", "existing", (), "A2"),          # 続きが、排他の後に対象かを見ない
                ("due-before-lock", "existing", (), "A2"),       # 続きが、対象かを排他の前に見る
                ("no-reread", "existing", (), "C"),              # 片付けが、排他の後に記録を読み直さない
                ("record-after-unlock", "existing", (), "C"),    # 続きが、排他を放してから行を書く
                ("sweep-ignores-busy", "existing", (), "B"),     # 片付けが、ほかの launcher の排他を無視する
                ("half-not-listed", "existing", (), "L"),        # 片付けが、半分になった置き場を並べない
                # 次の 2 つは、正直な範囲では約束を破らない（対象かの判定が先に止める・作ったばかりの置き場は対象にならない）。
                # 守るのは範囲の外なので、その条件で見る
                ("no-samestat", "existing", ("recreate",), "A1"),              # 排他の後に、まだその名前にあるかを見ない
                ("first-launch-holds-nothing", "fresh", ("forge",), "B")):     # 最初の起動が、排他を持たずに子を起こす
            with self.subTest(broken=broken):
                _, found = explore(scene, "W", env=env, broken=(broken,))
                self.assertIn(promise, found)
                if (scene, env) not in whole:
                    whole[scene, env] = explore(scene, "W", env=env)[1]
                self.assertNotIn(promise, whole[scene, env])

    def test_outside_the_honest_range_the_model_finds_the_gaps_the_text_admits(self):
        """正直な範囲の外では、文に書いた守らない範囲が、反例として出る（出なければ、モデルが粗い）: 片付けが途中で止まって半分になった
        置き場は、作業場所を同じ道筋に作り直す・いまの時刻の偽の行を足す・時計を戻すと、対象でなくなり、続きの子がそこから始まる。
        偽の行があると、最初の起動は、作ってから排他を取るまでの間に置き場を消されて失敗する。"""
        for env in ("recreate", "forge", "back"):
            with self.subTest(env=env):
                _, found = explore("existing", "W", env=(env,))
                self.assertLessEqual({"A2", "H"}, set(found))
        _, found = explore("fresh", "W", env=("forge",))
        self.assertIn("E", found)


if __name__ == "__main__":
    unittest.main()
