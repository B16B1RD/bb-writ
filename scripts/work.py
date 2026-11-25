#!/usr/bin/env python3
"""同じ作業場所への二重書き込みを防ぐ担当の貸し出し（lease）と、結果が分からない外部操作の記録。

  work.py claim   --dir <作業場所> [--holder <誰か>] [--lease <分>]   → token を返す。生きた担当がいるか、コマンドが終わっていない op run があれば拒否（終了コード 3）
  work.py renew   --dir <作業場所> --token <t> [--lease <分>]          → 引き継がれていれば拒否（3）。旧担当はここで止まる
  work.py release --dir <作業場所> --token <t>                         → コマンドが終わっていない op run があれば拒否（4）
  work.py status  --dir <作業場所>
  work.py op begin   --dir <作業場所> --token <t> --key <操作の鍵> --what <何をするか> [--from <場所>] [--verified <c>] [--reviewed <c>|none:<理由>] [--delta 低:<理由>] [--risk 低|中|高:<理由>] [--not-run <理由>]
                     → 同じ鍵が既にあれば拒否（4）
  work.py op run     （op begin と同じ引数） -- <コマンド>              → begin が通ったときだけ --from（無ければ作業場所）でコマンドを
                     シェルを通さず標準入力なしで実行し、終了コードと出力（stderr を含む）の末尾 20 行を done に記録する。
                     終了コードはコマンドのもの（シグナルは 128+N、起動できなければ 127、begin の拒否は begin の値、担当を失って
                     done を書けなければ 3、done の書き込みに失敗したら 1、監督役が結果を返さずに終わったら、記録に終了コードが
                     あればそれ（監督役が書いた記録の 0〜255 だけ読む）、無ければ 1 で exit は null。
                     記録（done でも begun でも）が読めれば op を添える）。出力は全体を表示する。
                     コマンドが終わるまでは、コマンドとそれが残したもの（シェルの & など）もロックを持つ。終わった後は（done を
                     書けたかに関わらず）、残したものは数えない（外部操作を & で背景に回すと、終わる前に明け渡すので、そうしない）
                     実行と記録は別のセッションの監督役が行い、コマンドが終わるまで担当を保つ。op run が止められても続く
                     （ただし、ツールの呼び出しが終わるとそこで起こしたプロセスを全部止める sandbox（Codex）では、一緒に止まる）。
                     コマンドが終わるまで（監督役が強制終了されたら、コマンドとそれが残したものが終わるまで）、その作業場所の op run と
                     外部操作の op begin、その操作の op done を拒否する（4）
  work.py op done    --dir <作業場所> --token <t> --key <操作の鍵> --result <結果>    → 照合で見つけた結果を閉じるとき
  work.py op pending --dir <作業場所>                                 → 始めたが終わりの記録が無い操作（コマンドが終わっていない op run の操作は running: true。残っている出力は output）
  work.py check   --dir <作業場所> --from <外部操作を実行する場所>      → そこで使われる GitHub のアカウントと、push 先の origin への書き込み権限（書けない・アカウントが分からなければ 5）
  work.py task start --dir <checkout> --holder <誰か> [--base <commit等>] [--target <ブランチ>] → 専用 worktree と担当を作る
  work.py task list --dir <checkout>                               → タスク、担当、未完了の統合
                     task start と task list は、担当のいない（解放済みか、期限が切れて 24 時間を過ぎた）タスクで作業場所が残っているものを
                     unclosed に並べる（切れて 24 時間以内のものは、持ち主が子を待っているだけかもしれないので並べない。commits は基点からの commit の数、landed は変更が統合先に入っているか）。片付けを呼ばれなかったタスクを見せるだけで、消さない
  work.py task resume --dir <checkout> --task <識別子> --holder <誰か> [--take-over] → 作業場所を保持して担当を取得。期限が切れて
                     24 時間以内の担当は、--take-over（利用者にそのタスクの続きか、捨てることを頼まれたとき）が無ければ拒否（3）。
                     片付けの途中で止まったタスクのうち、作業場所がもう無いものと、下の残りのものは、保存先を照合し直してから片付けを
                     仕上げる（completed: true。前の担当が生きていても）。それ以外の片付けの印（Git の登録が残る作業場所と、登録が消えた残りだと
                     記録に書いていない残り）は、仕上げない（登録が残れば、ふつうの再開として担当を返す。残っていなければ拒否（6））。
                     task cleanup が、Git の登録が消えた作業場所の残りだと記録に書いていて、いまも Git の登録が無ければ、照合の後に、
                     作業場所の道筋に在るものを消す（止まった後にそこへ書かれたものも、消して置き直したものも、確かめずに消す）。
                     消せなければ、最初に消せなかった項目の道筋を出して拒否（6）
  work.py integrate --dir <worktree> --token <t> --expected <統合先commit> --verified <検証済みcommit> → 排他・照合して fast-forward
  work.py task cleanup --dir <worktree> --token <t> --saved <別のブランチかタグ> → 変更が保存先に入っていれば片付け（squash・rebase の統合も可）。
                     項目を持つのに書き込めないディレクトリがあれば、消し始める前に、その道筋を出して拒否（6）。git worktree remove が
                     作業場所を消している途中で止まり、Git の登録だけを消したら、片付けの途中の記録に、登録が消えた残りだと書いて拒否し（6）、task resume で
                     仕上げるよう案内する

task と integrate の照合拒否は終了コード 6、担当の拒否は 3、記録の置き場を権限で読み書きできなければ 2（担当の拒否と見分ける。#60）。記録（ops.jsonl）の行が読めないか形が違えば 1 で、何行目かを示す。
統合のロックは期限では奪わず、子の Git にも渡す。
統合の記録はリポジトリ共通。中断後は前回の候補を含む統合先から完了を復元するか、新しく検証した候補で再開する。

公開する操作（鍵の種類が push・pr・merge・release）の op begin には --verified <検証した commit> が要る。作業場所の HEAD と違えば拒否する（6）。
push・pr には --reviewed も要る（初回の push も）。届ける前の審査（入口の条件 (5)・(6)）を通した commit の ID（16 進）か、
要らない理由を none:<理由> で書く。無い・形が違う・commit が見つからない・ID でなく参照に解決されたら拒否し（2）、作業場所の HEAD と違えば拒否する（6）。
HEAD と違っても、その commit が HEAD の祖先で、--delta 低:<理由>（審査の後の差分の危険度。文と試験だけで、振る舞い・主張を変えない
直し）を付ければ通す（その commit の、この作業場所を --cwd にした審査の記録と、HEAD の監査役は同じく求める。task start の作業場所（下の階層も同じ場所）では、
基点より後と確かめられない commit の審査は数えない。差分の中身と宣言の真偽は見ず、
監査役が確かめる）。--delta が中・高・形が違う・none: と並ぶ・HEAD そのものの審査と並ぶなら 2、祖先でないか基点より後と確かめられないなら（置き換え・grafts・commit-graph は外して見る）6。
push・pr には --risk 低|中|高:<理由> も要る（変更の危険度。中・高は interrogate を通すので、none: と一緒なら拒否する（2）。理由の真偽は確かめない）。
中・高で commit を書いたときは、その commit の子の記録のうち、launcher に --interrogate を付けて立てた審査役のものが無ければ拒否する（6。
条件 (5) の子の記録は、interrogate の審査の代わりにならない）。宣言の真偽（依頼文が interrogate の観点を持っていたか）は見ない。
HEAD と違うときは審査の後に変えたファイルを、ほかの拒否と通ったときは、この作業場所の記録で最後に終えた push・pr（ブランチを問わない）から変えたファイルを示す（材料で、保証ではない）。
op run・op begin を通さない git push には効かない。関門は鍵の種類（push・pr）と作業場所の HEAD で決まり、実行するコマンドと
その push 先は見ない（push を local・comment の鍵で op run すると、審査も監査役も求めない。HEAD でない ref を push しても止めない）。
commit を書いたときは、launcher の委任の記録（delegations.jsonl）に、その commit を対象に起動して ok で返った読み取り専用の子が
無ければ拒否する（6）。通ったら、その子の記録の場所を reviews に残す。審査の中身（子が十分に攻めたか）と none: の理由の真偽は確かめない。
短い往復で審査を忘れたまま、または書き手が自分の確かめを審査の代わりにして出すのを止める。
push・pr は、--reviewed（none: を含む）に関わらず、作業場所の HEAD を対象に launcher に --audit を付けて起動し ok で返った
読み取り専用の監査役（返す前の判定）の記録が無ければ拒否する（6）。監査役の記録は審査の記録にも周の数にも数えない。
監査役の記録は、判定の deliver が行と record.json の両方で yes（届けてよい）のものだけを数える。何を挙げたかは見ない。
push・pr に --not-run <理由>（子が sandbox のせいで対象の試験・検査を実行できなかった理由）を付けると、その commit を対象に
not-run で返り、子が実行の欄に実行できなかった（execution が no）と書いた記録も、審査役・--interrogate の審査役・監査役と審査の周に数える
（ブラウザの試験など、子を何回立てても ok の記録ができず、作業者が関門の外で出したため）。実行の欄の無い報告（launcher が読んだだけとして
not-run にしたもの）は数えない。理由が空か、push・pr 以外に付けたら拒否する（2）。宣言は ops.jsonl の not_run に残す。宣言の真偽
（教えれば動いた場面か）と、親が HEAD で試験を実行したかは見ず、監査役が判定する。宣言なしで記録が無いと拒否するとき、not-run の記録があれば
理由の文で宣言のしかたを示す。スキルの要求を読んでいて弱いもので代える・黙って省く（読者役を
別の目的の読み手で代える、意味の判断を役割の違う agent に見せる）まま、none: で出したため。
push・pr は、作業の記録（note.md）の目的・制約・確かめ方・指摘の扱いの欄が空か無ければ拒否する（2）。
審査が 3 周に達していたら（task start の作業場所を --cwd にして launcher で読み取り専用の子（審査役・確かめ役など）を立てて ok で返った（--not-run の宣言では、not-run で execution が no のものも）、互いに異なる commit が 3 つ）、仕組みの要否の欄も。
欄が空かどうかだけを見て、中身の真偽・書いた順・どの周の審査の指摘かは見ない。claim・renew は空の欄を note.empty で知らせる（拒否しない）。
言い直し・記録・指摘の扱いの一覧を飛ばしたまま、長いセッションで作業が進んだため。仕組みの要否は、審査の周ごとに仕組みへ直しを足し、
仕組みが目的に要るかを誰も問わないまま周が止まらなかったため（原則の文を足すだけでは、長い作業の中で判断が変わらなかった）。
検証の後に成果物が変わったら、証拠を取り直してから出す。merge は、GitHub 側でも gh pr merge --match-head-commit <同じ commit> で止める。

外部に出る操作（鍵の種類が push・pr・comment・merge・release・issue）の op begin には --from が要り、check を通す。
鍵の種類はこの 6 つか、外部に出ない操作の記録なら local（または local- で始まる名前）。ほかの種類（Push・gh-pr・push/…・tag）は、確かめずに記録されるので拒否する（2）。
gh のアカウントが分からなければ拒否する（5）。
外部操作は元の checkout（worktree なら `git rev-parse --git-common-dir` の親）から実行する。利用者のアカウントの切り替え
（gh のラッパー、git の includeIf、direnv）は、たいてい置き場所で決まるため（2026-10-17 の実測）。--from が元の checkout と違い、
そこで使われるアカウントが元の checkout のものと食い違えば拒否する。--from が元の checkout と別のリポジトリなら、そこでは確かめずに拒否する（2）。
push・merge・release は書き込み権限が無いアカウントも拒否する。comment・issue・pr は、書き込み権限が無くても
GitHub が受け付ける（公開リポジトリへのコメントや fork からの PR）ので、対象が見えれば通す。権限を見る対象は gh の既定の
リポジトリ（gh repo fork の後は upstream、GH_REPO があればそれ）で、gh 自身に解決させる。push だけは push 先の origin の最初の pushurl
（自分の fork。pushDefault・複数の pushurl は見ない）。黙って別のアカウントへ切り替えない。
push の SSH の形の URL は、git が使う ssh（GIT_SSH_COMMAND > core.sshCommand > GIT_SSH > ssh）の -G で実際のホストに直して見る。解決できなければ止める。
SSH の push は、同じ ssh の -T -o BatchMode=yes で GitHub が返す鍵のアカウントも gh のアカウントと照合する（別名や core.sshCommand で鍵を切り替える構成）。
繋がらなければ止める（known_hosts に無い ssh.github.com:443、ssh-agent の無いパスフレーズ付きの鍵も止まる）。
https の push に使われる git の credential helper のアカウントは見ない（認証情報に触れない）。IP・角括弧のホスト・/etc/hosts の別名と、リモートヘルパーの形
（persistent-https::…）は github.com とみなさない。

担当の生死はプロセスで判定しない（Codex の sandbox の中は別の pid 名前空間で、外の CLI が見えない。2026-10-17 の実測）。
op run のコマンドが終わっていないかも pid では見ず、操作ごとのロック（running-<鍵のハッシュ>.lock）の flock で見る
（ロックは監督役・コマンド・それが残したものが持ち、プロセスが死ねば OS が外す）。
環境変数 BB_WRIT_SUPERVISE_GATE は試験だけが使う（op run と監督役の中の止まり所。設定されていても 60 秒で先へ進み、コマンドには渡さない）。
claim は作業場所ごとの記録 note.md の雛形と証拠の置き場 evidence/ を作り、その場所と（書かれていれば）記録の中身を返す。後任は claim の出力だけで記録にたどり着く。
claim・status・task resume・task cleanup の note は、終えた後に今の状態・次の一手の欄を書き直していない
done の操作（ops.jsonl の記録。種類を問わない）を ops_after_note に並べる（1 行は key・what・exit・at。exit は監督役が書いた記録（by が
supervise）の result が「exit <数>:」で始まるときのその数で、それ以外（照合で op done を手で閉じた操作など）は null。result の本文は
写さない。at を数として読めない done の記録（文字列・null・真偽値）は、比べられないので並べる）。並ぶ操作が無ければ欄ごと無い。並びの順は約束しない。
欄があれば、本文の今の状態・次の一手は、並んだ操作が終わる前に書かれている（後任が済んだ操作を残件として報告したため。Issue #49）。
記録（ops.jsonl）か note.md の時刻を読めなければ、欄を出さずに進む（欄は知らせで、関門でない。renew と、片付けを終えた後の note を、
この欄のために止めない。記録が読めないことは、claim・status・op・task cleanup が、これまでどおり 0 でない終了コードで止めて知らせる
（行が読めなければ 1）。片付けの途中からの task resume は、その記録をほかに読まないので、欄が無いまま返る）。
done の行は、終えた時点の今の状態・次の一手の欄の中身のハッシュ（note_state）を持ち、今の欄と同じ間だけ並ぶ（ほかの欄の書き足し・touch では
消えない。#59）。守らない範囲: 終える前に「済み」と書いた操作も、書き直すまで並ぶ。今の状態・次の一手の欄の行を書き換えた記録（「* 今の状態:」など）は
欄と見なさず、その中身は前の欄に数える（note_gaps と同じ）。間に操作を終えずに今の状態を書き直し、前と同じ文に戻した場合は、また並ぶ。欄の中身は両端の空白だけを除いて比べるので、複数行の欄の途中の行末の
空白を消すと、書き直したと見なす。空の次の一手の行を足すと、中身を書かなくても書き直したと見なす。今と違う note_state の done が
後に複数あるときは最も遅いものを境にし、境と同じ at の操作は並べない（試験は、境の取り方と同じ at の扱いと、手で書き換えた done の行で at が
数でない行が note_state を持つときの型の検査を見ない）。note_state の無い done の行（この変更より前の記録、終えた時点の記録を読めなかった行）は、記録を最後に書いた時刻
（mtime）と比べる: 本文を変えずに mtime だけが進んだ場合は、古い今の状態が残っていても欄は出ない。時計を戻した場合。at と mtime が同じ値のときに
どちらになるか。mtime の粒が粗いファイルシステムで、操作の直後に書き足した場合は、書き足した後も、次に書くまで並ぶことがある。監督役の印（by）がある記録を
手で書き換えたときの exit（「exit <数>:」で始まらない形、255 を超える数など）。at が NaN・無限大の記録（並ぶかどうかと、出力の at が
JSON の規格に無い値になること）。renew の出力（note の path と empty だけ）には足さない。
期限内に renew しない担当は止まったとみなし、別の担当が claim できる。claim のたびに token が変わるので、
旧担当の renew・op は拒否される。出力はどれも 1 行の JSON。
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


def state_dir() -> Path:
    return Path(os.path.abspath(os.environ.get("BB_WRIT_STATE_DIR", Path.home() / ".local" / "state" / "bb-writ")))


def record_at(d: Path) -> Path:
    """作業場所 d の記録の置き場（作らない）。"""
    real = str(d.resolve())
    return state_dir() / "work" / (Path(real).name + "-" + hashlib.sha256(real.encode()).hexdigest()[:10])


def root(d: Path) -> Path:
    r = record_at(d)
    r.mkdir(parents=True, exist_ok=True)
    return r


@contextmanager
def locked(r: Path):
    fd = os.open(r / ".mutex", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        # 子の git に渡した fd は同じ排他を指す。hook の背景のプロセスが継いでも残らないよう、明示して放す（親が kill されたときは git の終わりまで保つ）
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read(p: Path):
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def write(p: Path, obj):
    tmp = p.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)  # 書く子が置いたリンクをたどらない
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


class RecordError(ValueError):
    pass


def ops(r: Path) -> dict:
    out = {}
    f = r / "ops.jsonl"
    if f.is_file():
        # 区切りは書いた側の \n だけ。splitlines は出力に入った U+2028・U+0085 などでも行を割る
        rows = f.read_bytes().split(b"\n")
        for n, line in enumerate(rows[:-1] if rows[-1] == b"" else rows, 1):
            try:
                e = json.loads(line.decode("utf-8"))
            except (ValueError, RecursionError) as x:
                raise RecordError(f"記録 {f} の {n} 行目を読めない: {x}") from None
            if not isinstance(e, dict) or not isinstance(e.get("key"), str):
                raise RecordError(f"記録 {f} の {n} 行目の形が違う（key が文字列のオブジェクトでない）")
            cur = out.setdefault(e["key"], {})
            cur.pop("by", None)
            cur.update(e)
    return out


def changed_since_publish(r: Path, d: Path, head: str) -> str:
    done = [e for e in ops(r).values() if e.get("verified") and e.get("state") == "done" and e["key"].split(":")[0] in REVIEWED]
    if not done:
        return " この作業場所に、終えた push・pr の記録が無い"
    prev = max(done, key=lambda e: e["at"])["verified"]
    rc, files, err = sh(["git", "diff", "--name-only", f"{prev}..{head}"], d)
    if rc:
        return f" 前回の push・pr（{prev}）からの差分を出せない: {err}"
    rc, _, err = sh(["git", "merge-base", "--is-ancestor", prev, head], d)
    note = {0: "", 1: "（前回の commit は HEAD の祖先でない。rebase などの後なので、取り込んだ変更も並ぶ）"}.get(rc, f"（祖先かを確かめられない: {err}）")
    return f" 前回の push・pr（{prev[:12]}）から変えたファイル{note}: " + (", ".join(files.splitlines()) or "なし")


def read_regular(f: Path) -> bytes:
    """通常のファイル（symlink の行き先を含む）の中身。開くところで止まらないよう O_NONBLOCK で開き、開いたものの種類を見る。
    無い・通常のファイルでない（ディレクトリ・FIFO・壊れた symlink）・権限で読めなければ OSError。"""
    fd = os.open(f, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as h:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"通常のファイルでない: {f}")
        return h.read()


def reviews_of(commit: str, cwd=None, not_run: bool = False) -> tuple:
    """launcher の委任の記録（delegations.jsonl）のうち、その commit を対象に起動して ok で返った読み取り専用の子の記録の out と、
    そのうち --interrogate を付けて立てた審査役の out と、--audit を付けて立てた監査役（返す前の判定）の out と、読めないものの説明（無ければ空）。
    行と、out の record.json の両方で head・status・write が合い、status・execution が行と record.json で同じものだけを数え、interrogate・audit も両方で真のものだけをその役に数える。
    監査役は審査の記録（最初の値）に数えない（push ごとに立てるので、審査の代わりにも周の数にもしない）。監査役の記録は、
    判定の deliver が行と record.json の両方で yes のものだけを数える（本文で差し戻した監査を、status の ok で通さない）。
    同じ commit に yes の記録が 1 件でもあれば、その後の no の記録では止めない（守らない）。
    head は launcher が、対象の作業ツリーが子の起動の前と終わりの両方で commit と同じとき（未 commit の変更が無いとき）だけ書く。
    審査の中身（子が十分に攻めたか、何人か、依頼文が interrogate の観点を持っていたか）は見ない。手で書き足した記録も見分けない（忘れて飛ばすのを止める）。
    cwd を渡すと、その場所を --cwd にして立てた記録だけを数える（--delta の祖先の審査を、ほかの作業場所の審査で代えないため）。
    not_run が真なら（作業者が --not-run で、子が sandbox のせいで実行できなかったと宣言した）、status が not-run で、子が実行の欄に
    実行できなかった（execution が no）と書いた記録も数える。実行の欄が無い報告（読んだだけ）は数えない。"""
    here = str(Path(cwd).resolve()) if cwd is not None else None
    done = lambda e: e.get("status") == "ok" or (not_run and e.get("status") == "not-run" and e.get("execution") == "no")
    match = lambda e: (isinstance(e, dict) and e.get("head") == commit and done(e) and e.get("write") is False
                       and isinstance(e.get("out"), str) and os.path.isabs(e["out"])
                       and (here is None or (isinstance(e.get("cwd"), str) and os.path.isabs(e["cwd"]) and str(Path(e["cwd"]).resolve()) == here)))
    f = state_dir() / "delegations.jsonl"
    found, judges, audits, broken = [], [], [], 0
    try:
        rows = read_regular(f).split(b"\n")
    except FileNotFoundError:
        if not f.is_symlink():
            rows = []
        else:
            return [], [], [], f"委任の記録 {f} を読めない: 行き先が無い"
    except OSError as x:
        return [], [], [], f"委任の記録 {f} を読めない: {x}"
    for line in rows:
        if not line.strip():
            continue
        try:
            e = json.loads(line.decode("utf-8"))
        except (ValueError, RecursionError):
            broken += 1
            continue
        if not match(e):
            continue
        try:
            rec = json.loads(read_regular(Path(e["out"]) / "record.json").decode("utf-8"))
        except (OSError, ValueError, RecursionError):
            broken += 1
            continue
        if match(rec) and rec["out"] == e["out"] and (rec.get("status"), rec.get("execution")) == (e.get("status"), e.get("execution")):
            if e.get("audit") is True or rec.get("audit") is True:
                if e.get("audit") is True and rec.get("audit") is True and e.get("deliver") == rec.get("deliver") == "yes":
                    audits.append(e["out"])
                continue
            found.append(e["out"])
            if e.get("interrogate") is True and rec.get("interrogate") is True:
                judges.append(e["out"])
    return found, judges, audits, (f"委任の記録 {f} に、読めない行か、out の record.json を読めない行が {broken} 行ある" if broken else "")


def not_run_hint(commit: str, cwd=None, role: int = 0) -> str:
    """その役（0: 審査役、1: --interrogate の審査役、2: 監査役。reviews_of の並びの番号）の記録が無いと拒否するとき（宣言があって拒否されるときは、
    数える記録が無いので空を返す）、
    その commit に、その役の子が実行できずに not-run で返った記録（execution が no）があれば、宣言のしかたを示す文。
    関門の外で出す（素の git push・gh pr create）道へ行かせないため。宣言しても通らない場面（ほかの役の not-run だけがある）では出さない。"""
    found = reviews_of(commit, cwd, not_run=True)[role]
    if not found:
        return ""
    return ("。この commit には、子が対象の試験・検査を実行できずに not-run で返った記録がある"
            f"（{found[0]}）。"
            "sandbox のせいで子が実行できない（headless Chrome・Node の子プロセス・127.0.0.1 など。写し方を教えれば動くものは除く）なら、"
            "関門の外で出さず、親が HEAD で同じ試験を実行して証拠（コマンド・終了コード・出力）を作業の記録に書き、"
            "--not-run <子が実行できなかった理由> を付けて op run する（監査役はその証拠を判定する。`playbook-deliver`）")


def task_place(d: Path) -> tuple:
    """task start で作った作業場所（親のタスクの一覧に dir があるもの）に d が入っていれば、(その作業場所, 基点)。ほかは (None, None)。
    同じリポジトリ（common dir が同じもの）の下の階層も同じ作業場所と見る（下の階層を --dir にして基点の確かめを外せた）。
    どのタスクの一覧を見るかは common dir で、包含はパスで決める（下の階層に .git のファイルを置くと、git の最上位がそこに変わった）。
    作業場所の下に置いた別のリポジトリ（clone）は、別のリポジトリとして見る。パスは resolve した綴りのまま比べる
    （大文字小文字を区別しないファイルシステムで、綴りの違う --dir は別の場所として見る。守らない）。作業場所の task.json は書く子が書き換えうるので読まない。
    記録の置き場は作らない。"""
    c = checkout_or_none(d)
    if not c or not (record_at(c) / "tasks").is_dir():
        return None, None
    real = d.resolve()
    for f in (record_at(c) / "tasks").glob("*.json"):
        try:
            t = read(f)
            place = Path(t["dir"]).resolve() if isinstance(t, dict) else None
            if place is not None and (real == place or place in real.parents):
                return place, t.get("base")
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None, None


def is_ancestor(older: str, newer: str, d: Path) -> tuple:
    """git merge-base --is-ancestor の (終了コード, エラー)。置き換え（git replace）・grafts（info/grafts・GIT_GRAFT_FILE）・
    commit-graph は、履歴に無い親を見せられるので、外して object の本当の親で確かめる。"""
    rc, _, err = sh(["git", "-c", "core.commitGraph=false", "-c", "advice.graftFileDeprecated=false",
                     "merge-base", "--is-ancestor", older, newer], d,
                    env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1", "GIT_GRAFT_FILE": os.devnull})
    return rc, err


def review_rounds(d: Path, not_run: bool = False) -> int:
    """審査の周の数。委任の記録のうち、この作業場所（その下の階層を含む）を --cwd にして ok で返った読み取り専用の子の head（launcher が書く完全な ID）の、
    互いに異なるものの数（タスクの基点そのものの審査は除く）。審査役も条件 (5) の確かめ役も数え、監査役（--audit）は数えない。amend・rebase で前の周の commit が
    履歴から外れても数える。別の場所（ほかのタスク、写し）を --cwd にした審査は数えない。
    task start で作った作業場所（親のタスクの一覧に dir があるもの）でなければ数えない。記録は行だけを見る（手で書いた行も数える）。
    作業場所の task.json は書く子が書き換えうるので読まない。手で書いた行の cwd が解けない（NUL・symlink のループ）ときは、
    traceback で止まる（launcher は resolve した cwd を書く。手で書いた記録は確かめない）。
    not_run が真なら（--not-run の宣言）、not-run で execution が no の子も周に数える（数えないと、審査がすべて not-run になる
    リポジトリで、3 周の関門が働かない）。"""
    top, base = task_place(d)
    if base is None:
        return 0
    try:
        rows = read_regular(state_dir() / "delegations.jsonl").split(b"\n")
    except OSError:
        return 0
    heads = set()
    for line in rows:
        try:
            e = json.loads(line.decode("utf-8")) if line.strip() else None
        except (ValueError, RecursionError):
            continue
        if (isinstance(e, dict) and isinstance(e.get("head"), str) and re.fullmatch(r"[0-9a-f]{40,64}", e["head"])
                and (e.get("status") == "ok" or (not_run and e.get("status") == "not-run" and e.get("execution") == "no"))
                and e.get("write") is False and e.get("audit") is not True and isinstance(e.get("cwd"), str)
                and e["head"] != base and (Path(e["cwd"]).resolve() == top or top in Path(e["cwd"]).resolve().parents)):
            heads.add(e["head"])
    return len(heads)


def pending(r: Path) -> list:
    held = run_holder(r)
    return [{**e, **({"running": True} if e["key"] == held else {}),
             **({"output": str(run_log(r, e["key"]))} if run_log(r, e["key"]).is_file() else {})}
            for e in ops(r).values() if e.get("state") == "begun"]


NOTE = """# 作業の記録

- 目的:
- 制約:
- できたと分かる確かめ方（述語）:
- 指摘の扱い（審査の指摘ごとに、対応する・記録のみ・退ける と理由を、直す前に）:
- 仕組みの要否（審査が 3 周に達したら、直しを書く前に、目的の値で仕組みあり・なし・別の形を比べて）:
- 決めたことと根拠:
- 済んだことと証拠:
- 今の状態（ブランチ・commit・PR）:
- 次の一手:
- ホストと session ID:
"""
FIELDS = re.findall(r"^- (.+?):$", NOTE, re.M)
REQUIRED = FIELDS[:3]
TRIAGE = FIELDS[3]
PREMISE = FIELDS[4]
PREMISE_ROUNDS = 3  # skills/interrogate の 5 が前提を疑う「3 周目」と同じ数


def field_line(name: str):
    """欄の名前の行。雛形の括弧の中（説明）は省いてよい（「- 指摘の扱い: …」も欄）。別の文言の括弧は受けない。
    雛形の行を書き換えた欄の行（「* 目的:」「- 目的：」、別の文言の括弧の「- 制約（未定）:」など）は欄と見なさず、前の欄の中身に数える。そのため、書き換えた行の前の
    空の欄が埋まって見えることは守らない（雛形は note が書き、書き手は「:」の後に書き足す。2026-11 に手元の作業の記録（比べるための
    候補の記録を除く 202 件）の欄らしい行 1,831 行を数えて、記号や「:」を書き換えた欄の行は 0 行だった（記号の無い「目的:」の 5 行は、
    雛形を使わない記録か、雛形の欄の中身の行だった）。別の文言の括弧の行は 14 行あり、多くは欄の中の項目
    （「- 指摘の扱い（審査 1 回目）: …」）で、中身に数えるのが正しい。揺れを読み分ける解析器を足すと、審査の周ごとに新しい穴が出た）。"""
    short, paren = name.split("（")[0], name[len(name.split("（")[0]):]
    return re.compile(rf"- {re.escape(short)}(?:{re.escape(paren)})?:" if paren else rf"- {re.escape(short)}:")


FIELD_LINES = [field_line(f) for f in FIELDS]
HEADING = re.compile(r"#+(\s|$)")


def note_gaps(body: str, publish: bool, rounds: int = 0) -> list:
    """記録の必須の欄のうち、空か無いものの名前。欄の中身は、名前の行の ":" の後から、次の雛形の欄か見出しの行の前まで
    （表や字下げした行も中身に数える）。中身の真偽・書いた順・どの審査の指摘かは見ない（忘れて飛ばすのを止める）。記号だけの行
    （区切り線「---」、空の「-」、HTML のコメント）も中身に数えるので、それだけの欄は空と見なさない（手元の記録の欄の中身に 0 行）。
    同じ欄が 2 回あれば最初だけを見る。push・pr の前（publish）は指摘の扱いの欄も、審査の周が PREMISE_ROUNDS に達した commit では仕組みの要否の欄も。"""
    lines, gaps = body.splitlines(), []
    for name in REQUIRED + ([TRIAGE] if publish else []) + ([PREMISE] if publish and rounds >= PREMISE_ROUNDS else []):
        found = field_texts(lines, name)
        if not found or not found[0].strip():
            gaps.append(name)
    return gaps


def field_texts(lines: list, name: str) -> list:
    pat, found = field_line(name), []
    for at, l in enumerate(lines):
        m = pat.match(l)
        if not m:
            continue
        text = [l[m.end():]]
        for nxt in lines[at + 1:]:
            if HEADING.match(nxt) or any(p.match(nxt) for p in FIELD_LINES):
                break
            text.append(nxt)
        found.append("\n".join(text))
    return found


STATE_FIELDS = [f for f in FIELDS if f.startswith(("今の状態", "次の一手"))]


def note_state(body: str) -> str:
    """今の状態・次の一手の欄の中身（出てくるものすべて。欄の範囲は note_gaps と同じ）の SHA-256 の先頭 16 桁。
    done の行に残し、今の記録と比べる（ほかの欄の書き足し・touch では変わらない。#59）。"""
    lines = body.splitlines()
    parts = [name + "\n" + text.strip() for name in STATE_FIELDS for text in field_texts(lines, name)]
    return hashlib.sha256("\n\0".join(parts).encode("utf-8")).hexdigest()[:16]


def note_state_of(r: Path):
    try:
        return note_state(note_body(r / "note.md"))
    except (RecordError, OSError):
        return None


def note_body(f: Path) -> str:
    """記録の中身。無ければ空。UTF-8 でない字は置き換えて読む（欄が空かを見るだけなので、止めない。置き換えか無視かは結果を変えない）。
    先頭の BOM は外す（残すと、見出しの無い記録の最初の欄の行が欄に見えず、書いた欄が無いと言われる）。
    通常のファイルでない（ディレクトリ・壊れた symlink など）か、読めなければ RecordError。"""
    if not f.is_file() and (f.exists() or f.is_symlink()):
        raise RecordError(f"作業の記録 {f} が通常のファイルでない")
    try:
        return f.read_text(encoding="utf-8-sig", errors="replace") if f.is_file() else ""
    except OSError as e:
        raise RecordError(f"作業の記録 {f} を読めない: {e}") from None


def ops_after_note(r: Path, f: Path, body: str) -> list:
    """done の操作（種類を問わない）のうち、終えた後に記録の今の状態・次の一手の欄を書き直していないもの。並びは ops の順のまま。
    done の行に note_state（終えた時点の欄の中身のハッシュ）があれば、今の欄と同じときだけ並べる（ほかの欄の書き足しでは消えない。#59）。
    ただし、今と違う note_state を持つ done が後にあれば、それより前に終えた操作は並べない（その間に欄を書き直した。今の状態が
    前と同じ文に戻ったとき、済んだ操作をまた並べないため）。無い行（この変更より前の記録、終えた時点の記録を読めなかった行）は、
    記録を最後に書いた時刻（mtime）より後に done になったものを並べ、at を数として読めない行は比べられないので並べる（黙って落とさない）。
    mtime を読めなければ OSError、記録が読めなければ RecordError。"""
    written, now = f.stat().st_mtime, note_state(body)
    num = lambda at: isinstance(at, (int, float)) and not isinstance(at, bool)
    done = [e for e in ops(r).values() if e.get("state") == "done"]
    rewritten = max((e["at"] for e in done if isinstance(e.get("note_state"), str) and e["note_state"] != now and num(e.get("at"))),
                    default=None)
    rows = []
    for e in done:
        at, state = e.get("at"), e.get("note_state")
        comparable = num(at)
        if isinstance(state, str):
            if state != now or (comparable and rewritten is not None and at <= rewritten):
                continue
        elif comparable and at <= written:
            continue
        result = e.get("result")
        m = re.match(r"exit ([0-9]{1,3}):", result) if isinstance(result, str) and e.get("by") == "supervise" else None
        rows.append({"key": e["key"], "what": e.get("what"), "exit": int(m.group(1)) if m else None, "at": at})
    return rows


def note(r: Path) -> dict:
    f = r / "note.md"
    if not f.exists() and not f.is_symlink():
        try:
            f.write_text(NOTE, encoding="utf-8")
        except OSError as e:
            raise RecordError(f"作業の記録 {f} を作れない: {e}") from None
    body = note_body(f)
    try:
        after = ops_after_note(r, f, body)
    except (RecordError, OSError):
        after = []
    ev = r / "evidence"
    ev.mkdir(exist_ok=True)
    gaps = note_gaps(body, False)
    return {"path": str(f), "filled": body.strip() != NOTE.strip(), "body": body if body.strip() != NOTE.strip() else None,
            "evidence_dir": str(ev), **({"empty": gaps} if gaps else {}), **({"ops_after_note": after} if after else {})}


EXTERNAL = {"push", "pr", "comment", "merge", "release", "issue"}
PUBLISH = {"push", "pr", "merge", "release"}
REVIEWED = {"push", "pr"}
NEEDS_WRITE = {"push", "merge", "release"}
WRITE_OK = {"ADMIN", "MAINTAIN", "WRITE"}
NO_GITHUB_REMOTE = r"none of the git remotes configured for this repository point to a known GitHub host|no git remotes found"  # gh 2.102.0


def sh(cmd, cwd: Path, env=None):
    try:
        p = subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, env=env)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def checkout_of(d: Path) -> Path:
    c = checkout_or_none(d)
    if not c:
        raise SystemExit(out({"ok": False, "reason": f"git のリポジトリでない: {d}"}, 2))
    return c


def checkout_or_none(d: Path):
    rc, common, _ = sh(["git", "rev-parse", "--git-common-dir"], d)
    if rc:
        return None
    c = Path(common)
    c = (c if c.is_absolute() else d / c).resolve()
    if c.name == ".git":
        return c.parent
    # サブモジュール（.git/modules/x）の管理ディレクトリの親は checkout でない。core.worktree が元の checkout を指す
    rc, at, _ = sh(["git", "config", "--file", str(c / "config"), "core.worktree"], d)
    return (c / at).resolve() if rc == 0 and at else c.parent


def ssh_command(at: Path) -> list:
    """git が push に使う ssh。GIT_SSH_COMMAND > core.sshCommand（どちらもシェルで解釈）> GIT_SSH（プログラム）> ssh
    （git 2.43 の実測。core.sshCommand で鍵を切り替える構成もある）。"""
    cmd = os.environ.get("GIT_SSH_COMMAND")
    if not cmd:
        rc, cmd, _ = sh(["git", "config", "--get", "core.sshCommand"], at)
        cmd = cmd if rc == 0 else None
    return ["sh", "-c", cmd + ' "$@"', "sh"] if cmd else [os.environ.get("GIT_SSH") or "ssh"]


def github_repo(url: str, at: Path):
    """push 先の URL を、gh が受け取る github.com/OWNER/REPO と、SSH なら git と同じ ssh の接続先の引数にする。
    github.com でなければ (None, None)。
    SSH の別名（~/.ssh/config の Host）は ssh -G で実際のホスト名に直す。gh は既定のリポジトリの解決では
    別名を直すが、引数に渡した URL は直さず、別名のホストへ接続しに行って失敗する（2026-10-20 の実測）。
    別名を直せなければ GitHub かどうか分からないので、ValueError で止める（飛ばすと確かめずに通る）。"""
    m = re.match(r"(?:([a-z+]+)://)?(?:([^@/]+)@)?([^/:]+)(?::(\d+))?[:/]/?([^/]+/[^/]+?)(?:\.git)?/?$", url, re.I)
    if not m:
        return None, None
    scheme, user, host, port, name = m.groups()
    dest = None
    if scheme is None or "ssh" in scheme.lower():  # git と同じく user@host で渡す（ssh の設定の Match user が効く）
        dest = [*(["-p", port] if port and scheme else []), "--", f"{user}@{host}" if user else host]
        rc, conf, err = sh([*ssh_command(at), "-G", *dest], at)
        opts = dict(l.split(" ", 1) for l in reversed(conf.splitlines()) if " " in l) if rc == 0 else {}
        host = opts.get("hostname")
        if not host:
            raise ValueError(f"ssh -G で {url} のホストを解決できない: {err[:200]}")
        if not is_github(host) and opts.get("proxycommand", "none") != "none":  # 接続先は ProxyCommand が決める（ProxyJump は踏み台だけ）
            raise ValueError(f"ssh の設定の ProxyCommand が接続先を決めるので、GitHub か分からない: {opts['proxycommand'][:200]}")
    return (f"github.com/{name}", dest) if is_github(host) else (None, None)


def is_github(host: str) -> bool:
    host = host.lower().rstrip(".")  # 末尾の点は同じホスト（github.com.）
    return host == "github.com" or host.endswith(".github.com")


def key_login(dest: list, at: Path):
    """SSH の push で使われる鍵の GitHub のアカウント。GitHub は ssh -T に「Hi <login>!」を返す（終了コードは 1。deploy key は owner/repo）。"""
    _, o, e = sh([*ssh_command(at), "-T", "-o", "BatchMode=yes", *dest], at)
    m = re.search(r"Hi ([^!\s]+)!", o + "\n" + e)
    return (m.group(1), None) if m else (None, (e or o)[:300])


def account(at: Path, push: bool = False, key: bool = True) -> dict:
    """at で外部操作をしたときに使われる GitHub のアカウントと、対象のリポジトリへの権限（gh を at で実行して観測する）。
    push は push 先の origin の pushurl を見て、SSH なら鍵のアカウント（key_login）も見る。
    ほかは gh が操作する既定のリポジトリ（gh repo fork の後は upstream、GH_REPO があればそれ）を見る。既定の解決は gh 自身に
    させる。gh は別名を PATH の ssh -G <host> で直し（user を付けず、git の ssh も見ない）、失敗すれば名前のまま判定するので、
    真似るとずれる（2026-10-20 の実測）。gh が GitHub の remote を見つけられなければ、gh の操作も失敗するので飛ばす。"""
    dest = None
    if push:
        rc, url, _ = sh(["git", "remote", "get-url", "--push", "origin"], at)
        try:
            target, dest = (None, None) if rc else github_repo(url, at)
        except ValueError as e:
            return {"at": str(at), "github": True, "login": None, "repo": None, "permission": None, "error": str(e)}
        if not target:
            return {"at": str(at), "github": False, "remote": url or None}
    else:
        target = os.environ.get("GH_REPO")  # gh repo view は引数が無いと GH_REPO を見ない（issue・pr は見る）
    rc, login, err = sh(["gh", "api", "user", "--jq", ".login"], at)
    login = login if rc == 0 else ""  # 4xx の本文（JSON）は --jq を通らず stdout に出る（gh 2.102.0）
    rc, repo, err2 = sh(["gh", "repo", "view", *([target] if target else []), "--json", "nameWithOwner,viewerPermission",
                         "--jq", '.nameWithOwner + " " + .viewerPermission'], at)
    if rc and not target and re.search(NO_GITHUB_REMOTE, err2):
        return {"at": str(at), "github": False, "remote": None, "gh": err2[:200]}
    name, _, perm = repo.partition(" ") if rc == 0 else (None, None, None)
    res = {"at": str(at), "github": True, "login": login or None, "repo": name, "permission": perm or None,
           "error": None if login and rc == 0 else (err or err2)[:300]}
    if push and dest and key:
        res["key_login"], res["key_error"] = key_login(dest, at)
    return res


def check(d: Path, frm: Path, kind: str = "push") -> tuple[dict, int]:
    c = checkout_of(d)
    if checkout_or_none(frm) != c:
        return {"checkout": str(c), "ok": False, "reason": f"--from {frm} は元の checkout {c} と同じリポジトリでない。外部操作は元の checkout から実行する"}, 2
    write, push = kind in NEEDS_WRITE, kind == "push"
    here = account(frm, push)
    res = {"checkout": str(c), "from": here}
    if not here["github"]:
        return {**res, "ok": True, "skipped": ("origin の pushurl が github.com でない" if push else "gh が GitHub の remote を見つけられない") + "ので確かめを飛ばした"}, 0
    if not here["login"]:  # gh api user だけが失敗する token がある
        return {**res, "ok": False, "reason": f"{frm} で gh のアカウントが分からない。gh auth status を確かめる"
                + (f"（エラー: {here['error']}）" if here.get("error") else "")}, 5
    if not here["permission"] or (write and here["permission"] not in WRITE_OK):
        return {**res, "ok": False, "reason": f"{frm} で使われるアカウント {here['login']} は {here['repo'] or 'origin'} に{'書け' if write else 'アクセスでき'}ない（権限: {here['permission'] or '見えない'}）。"
                f"外部操作は元の checkout {c} から実行する。そこでも{'書け' if write else 'アクセスでき'}なければ、利用者のアカウントの設定を確かめる"
                + (f"（エラー: {here['error']}）" if here.get("error") else "")}, 5
    if "key_login" in here and not here["key_login"]:
        return {**res, "ok": False, "reason": f"SSH で GitHub に繋がらず、push に使われる鍵のアカウントが分からない（ssh: {here['key_error']}）。"
                "git の push も同じ ssh で繋ぐので、ssh の設定（鍵・known_hosts）を確かめる"}, 5
    if "key_login" in here and here["key_login"].lower() != (here["login"] or "").lower():
        return {**res, "ok": False, "reason": f"SSH の push に使われる鍵のアカウント {here['key_login']} が gh のアカウント {here['login']} と違う。"
                "ssh の設定（別名の IdentityFile・core.sshCommand）で、gh と同じアカウントの鍵を使う"}, 5
    if frm.resolve() != c:
        base = account(c, push, key=False)
        res["checkout_account"] = base
        if base.get("login") != here["login"]:
            return {**res, "ok": False, "reason": f"{frm} のアカウント {here['login']} と元の checkout {c} のアカウント {base.get('login')} が食い違う。"
                    f"外部操作は元の checkout から実行する"}, 5
    if kind == "release":
        why = ci_not_passed(d, frm, here["repo"])
        if why:
            return {**res, "ok": False, "reason": why}, 6
    return {**res, "ok": True}, 0


CI_OK = {"success", "skipped", "neutral"}


def ci_not_passed(d: Path, frm: Path, repo: str):
    """出す commit の GitHub Actions の実行が、全部終わって通ったか。通っていなければ理由を返す。
    push の直後に Release を出し、macOS の job が時間切れで打ち切られた（cancelled）のを 3 回見ないまま「済んだ」と報告したため。
    守らない範囲: 実行が 1 つも無ければ通す（CI の無いリポジトリ。push の直後で、実行がまだ作られていない数秒も通る）。
    Actions 以外の CI（外部のサービスの check）は見ない。その commit の実行を全部見るので、同じ commit で手動に起こして
    失敗した実行・別の workflow の失敗も止める（理由と URL を出すので、その実行を再実行して通すか、要らない実行なら利用者に聞く）。"""
    head = commit_of(d, "HEAD")
    rc, o, e = sh(["gh", "run", "list", "-R", repo, "--commit", head, "--limit", "100", "--json", "name,status,conclusion,url"], frm)
    try:
        runs = json.loads(o) if rc == 0 else None
    except ValueError:
        runs = None
    if not isinstance(runs, list):
        return f"commit {head[:12]} の CI の実行を読めない（gh run list: {(e or o)[:200]}）。読めるようにしてから出す"
    if [r for r in runs if r.get("status") != "completed"]:
        return (f"commit {head[:12]} の CI が終わっていない（" + "、".join(f"{r.get('name')}: {r.get('status')}" for r in runs if r.get("status") != "completed")
                + f"）。gh run watch --exit-status <id> -R {repo} で終わるまで待ち、結果を確かめてから出す")
    bad = [r for r in runs if r.get("conclusion") not in CI_OK]
    if bad:
        return (f"commit {head[:12]} の CI が通っていない（" + "、".join(f"{r.get('name')}: {r.get('conclusion')} {r.get('url')}" for r in bad)
                + "）。cancelled は時間切れなどで打ち切られたもので、失敗と同じに扱う。ログで原因を直し、CI が通った commit で出す")
    return None


def commit_of(d: Path, rev: str):
    rc, sha, _ = sh(["git", "rev-parse", "--verify", "--quiet", rev + "^{commit}"], d)
    return sha if rc == 0 else None


def out(obj, code=0):
    print(json.dumps(obj, ensure_ascii=False))
    return code


MAX_LEASE_MIN = 24 * 60


def alive(lease, now):
    """期限内の担当か。書く子は作業場所の記録に書けるので、上限を超える期限や壊れた値の担当は切れたものとみなす
    （期限を遠い将来にされると、親が担当を取り直せなくなる）。"""
    try:
        return bool(lease) and now < float(lease['expires_at']) <= now + MAX_LEASE_MIN * 60
    except (KeyError, TypeError, ValueError):
        return False


def maybe_waiting(lease, now):
    """期限が切れてから MAX_LEASE_MIN 以内の担当か。持ち主は子を待っているだけかもしれない（担当の生死はプロセスで判定できないので、
    時間だけで決める）。誰も取り直していなければ持ち主の操作は通る（held_lease）ので、ほかのセッションに閉じるタスクとして見せない（Issue #32）。"""
    try:
        return bool(lease) and now - MAX_LEASE_MIN * 60 < float(lease['expires_at']) <= now
    except (KeyError, TypeError, ValueError):
        return False


def lease_minutes(v):
    m = float(v)
    if not 0 <= m <= MAX_LEASE_MIN:
        raise argparse.ArgumentTypeError(f'担当の期限は 0 以上 {MAX_LEASE_MIN} 分以下')
    return m


def held_lease(lease, token):
    """持ち主の操作（renew・release・op・integrate・task cleanup）が照らす担当。期限は見ない。期限は、止まった持ち主の担当を
    ほかの者が取り直せるようにするためのもので、誰も取り直していなければ記録の token は持ち主のままなので、子を待っていただけの
    持ち主を締め出さない（Issue #31）。取り直されると token が変わり、release すると記録が消えるので、どちらの後も元の token は通らない。
    形の違う記録と空の token は、担当なしとして拒否させる（期限の値は読まない。壊れていても落ちない）。"""
    return lease if isinstance(lease, dict) and isinstance(token, str) and token else None


def check_token(lease, token):
    if not lease or lease.get("token") != token:
        return "この token の担当ではない（引き継がれたか、解放された）。作業を止め、status で今の担当を確かめる"
    return None


def pending_while(r: Path, held: str) -> list:
    try:
        return pending(r)
    except RecordError as e:
        raise RecordError(f"{e}。op run の外部操作がまだ動いている: {held}。終わってから直す") from None


def claim_at(r, d, holder, minutes, take_over=True):
    """take_over が偽（task resume の既定）なら、unclosed_tasks と同じく、期限が切れて MAX_LEASE_MIN 以内の担当も取らない。
    一覧でなく取る時に決めるので、古い一覧から、戻って延ばした後にまた切れた持ち主の担当を取らない。"""
    now = time.time()
    old = read(r / 'lease.json')
    if alive(old, now):
        return {'ok': False, 'reason': '生きた担当がいる。二重に書かない',
                'holder': old['holder'], 'expires_in_s': round(float(old['expires_at']) - now)}, 3
    if not take_over and maybe_waiting(old, now):
        return {'ok': False, 'reason': '担当の期限が切れて 24 時間以内。持ち主は子を待っているだけかもしれないので、閉じ忘れの片付けでは取らない。'
                '利用者にこのタスクの続きか、捨てることを頼まれたときだけ --take-over で取る', 'holder': old.get('holder')}, 3
    held = run_holder(r)
    if held:  # 担当の期限が切れても（手放されても）、op run のコマンドが動いている間は取らせない。取ると、終わっていない操作をやり直せる
        return {'ok': False, 'reason': f'op run の外部操作がまだ動いている: {held}。終わるのを待つ（終われば監督役が done を書く）',
                'holder': old and old.get('holder'), 'pending_ops': pending_while(r, held)}, 3
    waiting = pending(r)
    n = note(r)
    new = {'token': secrets.token_hex(8), 'holder': holder, 'dir': str(d.resolve()),
           'claimed_at': now, 'expires_at': now + minutes * 60,
           'previous': old and {'holder': old.get('holder'), 'expired_at': old.get('expires_at')}}
    write(r / 'lease.json', new)
    return {'ok': True, 'token': new['token'], 'previous': new['previous'],
            'pending_ops': waiting, 'note': n}, 0


def git(d, *args, fds=()):
    p = subprocess.run(['git', '-C', str(d), *args], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, pass_fds=fds)
    if p.returncode:
        raise ValueError(p.stderr.strip() or p.stdout.strip() or 'git が失敗した')
    return p.stdout.strip()


def contained(repo, commit, ref, look_only=False):
    """commit の変更が ref に入っているか。祖先でなくても（squash・rebase で統合した後に統合先が進んでも）、
    commit を ref へマージした結果が ref のツリーのままなら入っている。衝突・取り消し・未統合の commit は入っていない。
    git merge-tree --write-tree は、マージした結果の object をリポジトリに書く。look_only（見るだけの呼び出し）では、
    一時の置き場（tempfile の決める場所。TMPDIR）に書かせ、リポジトリの object は読むだけにする。"""
    if sh(['git', 'merge-base', '--is-ancestor', commit, ref], repo)[0] == 0:
        return True
    cmd = ['git', '-C', str(repo), 'merge-tree', '--write-tree', ref, commit]
    if look_only:
        objects = git(repo, 'rev-parse', '--path-format=absolute', '--git-path', 'objects')
        with tempfile.TemporaryDirectory() as scratch:
            env = {**os.environ, 'GIT_OBJECT_DIRECTORY': scratch,
                   'GIT_ALTERNATE_OBJECT_DIRECTORIES': os.pathsep.join(x for x in (objects, os.environ.get('GIT_ALTERNATE_OBJECT_DIRECTORIES')) if x)}
            p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env)
    else:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if p.returncode not in (0, 1) or not p.stdout:
        raise ValueError('変更が保存先に入っているかを確かめられない: ' + (p.stderr.strip() or 'git が失敗した')
                         + '（git merge-tree --write-tree を使う。Git が 2.38 より古いと、ここで止まる）')
    return p.returncode == 0 and p.stdout.split()[0] == git(repo, 'rev-parse', ref + '^{tree}')


# 書く子が書けた作業場所で動かす git に付ける -c（launch.py の QUIET_GIT と同じ）。git status・ls-files・index の写しを書く update-index・
# worktree remove が設定から走らせる core.fsmonitor を外す（コマンド行の -c は include・config.worktree の値より強く、中で起こす git にも伝わる。git 2.43 で測った。#64）。
# filter は外さない（どこで定めたものも。LFS などの required の filter を外すと status が壊れる。守らない範囲は launch.py の QUIET_GIT の注釈）。
QUIET = ['-c', 'core.fsmonitor=false']


def claude_staging_only(p):
    """p が、Claude Code が設定を書くときの空の置き場か。中が空の .cc-writes 1 つだけの .claude と、.claude の中の空の .cc-writes。
    同じ場所の別のセッションも使うので、片付けのために消させない（launcher が消して、並行するセッションの設定の書き込みが拒まれた。#65）。"""
    p = os.fsdecode(os.path.normpath(p))  # git status はディレクトリを「名前/」で出す
    cc = p if os.path.basename(p) == '.cc-writes' else os.path.join(p, '.cc-writes')
    claude = os.path.dirname(cc)
    try:
        return (os.path.basename(claude) == '.claude' and not os.path.islink(claude) and not os.path.islink(cc)
                and (cc == p or os.listdir(claude) == ['.cc-writes']) and os.listdir(cc) == [])
    except OSError:
        return False


def unopened_dir(d):
    """d の下で、一覧できないか中へ入れない（中の項目を調べられない）ディレクトリのどれか 1 つの、d からの相対の道筋（d そのものなら「.」）。無ければ None。
    権限のビットは読まず、一覧と、中の「.」の lstat を試して確かめる。symlink の先へは入らない。
    再帰しない（os.walk で歩いた版は、Python 3.9 で 1100 段の木を歩いて RecursionError で落ちた）。これを確かめる試験は無い
    （その深さの木は、開けるファイルの数の上限が 1024 以下の環境では、git 自身が歩けず、片付けが通らない）。"""
    stack = ['']
    while stack:
        rel = stack.pop()
        at = os.path.join(d, rel)
        try:
            os.lstat(os.path.join(at, '.'))
            with os.scandir(at) as entries:
                stack.extend(os.path.join(rel, e.name) for e in entries if e.is_dir(follow_symlinks=False))
        except OSError:
            return rel or '.'
    return None


def unremovable_dir(d):
    """d の下で、項目を持つのに、書き込みか中へ入る権限が無い（中の項目を消せない）ディレクトリのどれか 1 つの、d からの相対の道筋
    （d そのものなら「.」）。無ければ None。git worktree remove は、作業場所を消し切れなくても Git の登録を消すので（戻す道具が無い。#77）、
    消し始める前に見る。空のディレクトリと読み取り専用のファイルは数えない（親に書ければ消せる）。
    権限のビットは読まず os.access で確かめる（持ち主が違うディレクトリも同じ条件で見える）。symlink の先へは入らない。
    見るのは d の木の中だけ（d の親に書けず d そのものを消せない場合は見ない。そのときは git worktree remove が止まり、task resume で
    仕上げる形になる）。再帰しない（unopened_dir と同じ理由）。一覧できない・中へ入れないディレクトリは、先に unopened_dir が拒んでいる。
    権限のほかの理由で消せないもの（変更できないファイル、消している間に書き足すプロセス）は見えない（task_command の cleanup の注釈）。"""
    stack = ['']
    while stack:
        rel = stack.pop()
        at = os.path.join(d, rel)
        with os.scandir(at) as entries:
            found = list(entries)
        if found and not os.access(at, os.W_OK | os.X_OK):
            return rel or '.'
        stack.extend(os.path.join(rel, e.name) for e in found if e.is_dir(follow_symlinks=False))
    return None


class RemoveFailed(Exception):
    """remove_tree が最初に消せなかった項目の道筋 path と、OS の誤り error。OSError にしない（OSError を上げると、3.12 からの
    shutil.rmtree は外側の段でもう一度受け口を呼び、道筋が親のディレクトリに替わる）。"""

    def __init__(self, path, error):
        super().__init__(path, error)
        self.path, self.error = path, error


def remove_tree(d):
    """d を消す（symlink の先へは入らない）。最初に消せなかった項目で止まり、RemoveFailed を上げる。受け口の引数は、
    3.12 からは onexc（3 つ目は例外）、その前は onerror（3 つ目は sys.exc_info()）。どちらも 2 つ目が、その項目の道筋。"""
    def failed(function, path, info):
        raise RemoveFailed(path, info if isinstance(info, BaseException) else info[1])
    shutil.rmtree(d, **({'onexc': failed} if sys.version_info >= (3, 12) else {'onerror': failed}))


def clean(d, ignored=False, child=False):
    """作業ツリーの根 d に、未保存の変更が無いか。命令を走らせない形の status（QUIET、hooks を走らせない --no-optional-locks）で見る。
    child（書く子が書けた作業場所）では、入れ子のリポジトリの設定を読まないよう、サブモジュールの中に入らない
    （--ignore-submodules=dirty。サブモジュールが別の commit を指す変更は数える）。
    無視するものの行のうち、Claude Code の空の置き場（claude_staging_only）だけは数えない。
    ignored（片付けの見方）では、status に何も残らなくても、一覧できないか中へ入れないディレクトリ（unopened_dir）が 1 つでもあれば、
    空でも、中身を確かめられないので、その道筋（UTF-8 として読めないバイトは \\xe9 の形）を入れた理由（ValueError）で拒む（git status はそこを飛ばし、標準エラーに警告を
    出すか、何も出さないだけなので、片付けが git worktree remove まで進んだ。#68）。git の警告の文は読まない。統合の見方は数えない
    （データを消さず、利用者の checkout に root の持つ置き場などがありうる）。
    ignored では、さらに、skip-worktree か assume-unchanged の付いた、作業場所に実物のある追跡ファイルを、index の写しの上でフラグを
    外して比べ（hidden_change。stat の記録を信じない見方も含む）、変更があれば、その道筋を入れた理由（ValueError）で拒む（git はその
    変更を status に出さないので、片付けが書き換えた中身ごと消した。#76）。フラグは、人が付けるほか、core.ignoreStat=true では git が
    index を更新した項目に付け（task start の作業場所は checkout したてなので、追跡ファイルの全部に付く）、
    sparse.expectFilesOutsideOfPatterns=true の sparse-checkout では cone の外の実物に残る。フラグがあるだけでは拒まない
    （core.ignoreStat=true の作業場所で、片付けが通らなくなる）。統合の見方は見ない。
    フラグの付いた追跡ファイルを消して、同じ名前の空のディレクトリを置いた作業場所は拒む（フラグを外した status に削除として出る）。
    数えないもの: 開ける、無視しない空のディレクトリ・FIFO やソケットだけのディレクトリ（status に出ず、保存するデータが無い）、
    フラグの付いた追跡ファイルを消しただけのもの（実物が無く、失うデータが無い）。"""
    p = subprocess.run(['git', '-C', str(d), *QUIET, '--no-optional-locks', 'status', '--porcelain', '-z', '--untracked-files=all',
                        *(['--ignore-submodules=dirty'] if child else []), *(['--ignored=matching'] if ignored else [])],
                       stdin=subprocess.DEVNULL, capture_output=True)
    if p.returncode:
        raise ValueError(os.fsdecode(p.stderr).strip() or 'git が失敗した')
    # 1 欄は「XY 道筋」（道筋はリポジトリの根から、引用しない）。改名の元の道筋の欄は、改名の欄で数え終わっている
    if not all(e[:3] == b'!! ' and claude_staging_only(os.path.join(os.fsencode(d), e[3:])) for e in p.stdout.split(b'\0') if e):
        return False
    # status に Claude Code の空の置き場しか出ないので、歩く木に無視する大きな置き場は無い（初期化したサブモジュールの中は除く。その中の無視するものも歩く）
    rel = unopened_dir(d) if ignored else None
    if rel is not None:
        # UTF-8 の出力で落ちないよう、UTF-8 として読めないバイトは \xe9 の形にする
        raise ValueError(f"開けないディレクトリがある: {os.fsencode(rel).decode('utf-8', 'backslashreplace')}。中身を確かめられないので、作業場所を保持する")
    hidden = hidden_change(d, child) if ignored else None
    if hidden is not None:
        raise ValueError('skip-worktree か assume-unchanged の付いた追跡ファイルに、status に出ない変更がある: '
                         f"{hidden.decode('utf-8', 'backslashreplace')}。作業場所を保持する")
    return True


def hidden_change(d, child):
    """作業ツリーの根 d で、skip-worktree か assume-unchanged の付いた、作業場所に実物のある追跡ファイルのうち、変更のあるものの
    どれか 1 つの、根からの道筋（バイト列）。無ければ None。index の写しの上で、2 つの見方の status（clean と同じく命令を走らせない
    形で、追跡外・無視するものは見ない）を順に流し、先に出た最初の道筋を返す（status に出た道筋を絞らないので、確かめの間に
    書き換わった、フラグの無いファイルの道筋が出ることがある）。
    見方 1: それらのフラグだけを外す（index の覚えた stat と実物が食い違い、git が変更と見るものが出る。git add しても index が
    変わらない書き換えでも、大きさが変わっていれば出る）。
    見方 2: それらの項目を stat の記録の無い項目として入れ直し（update-index --index-info）、フラグを外す。git は stat の記録の無い
    項目の実物の中身を比べるので、index の覚えた stat が実物と同じでも、中身が違えば出る（フラグが付いている間、git は項目の stat の
    記録を更新しないので、記録は当てにならない）。
    フラグの無い項目は、写しの上でも元のままにする。元の index とフラグは書き換えない。写しを書く update-index は hooks を走らせない
    （post-index-change が走った）。
    守らない範囲: フラグの無いファイルは、前と同じく、clean の片付けの見方の status だけで判定する（git が見ない形の変更は見ない）。
    git が変更に数えないものは見つけない: core.fileMode=false での実行ビットだけの変更。改行の変換やフィルタを通すと index と同じ
    中身になり、大きさも変わらない書き換え（フラグの無いファイルでも、git status は出さない）。"""
    p = subprocess.run(['git', '-C', str(d), *QUIET, 'ls-files', '-v', '-s', '-z'], stdin=subprocess.DEVNULL, capture_output=True)
    if p.returncode:
        raise ValueError(os.fsdecode(p.stderr).strip() or 'git が失敗した')
    # 1 欄は「字 SP モード SP オブジェクト SP 段 TAB 道筋」。S・s は skip-worktree、h・s は assume-unchanged（s は両方）。
    # 字と空白を除いた残りが、そのまま update-index --index-info の 1 欄になる
    flagged = [e[2:] for e in p.stdout.split(b'\0') if e[:1] in (b'S', b's', b'h')
               and os.path.lexists(os.path.join(os.fsencode(d), e.partition(b'\t')[2]))]
    if not flagged:
        return None
    paths = [e.partition(b'\t')[2] for e in flagged]
    index = git(d, 'rev-parse', '--path-format=absolute', '--git-path', 'index')
    with tempfile.TemporaryDirectory() as scratch:
        env = {**os.environ, 'GIT_INDEX_FILE': os.path.join(scratch, 'index')}
        with open(index, 'rb') as src, open(env['GIT_INDEX_FILE'], 'wb') as dst:
            dst.write(src.read())

        def update(*args, entries):
            p = subprocess.run(['git', '-C', str(d), *QUIET, '-c', 'core.hooksPath=/dev/null', 'update-index', *args],
                               input=b''.join(e + b'\0' for e in entries), capture_output=True, env=env)
            if p.returncode:
                raise ValueError(os.fsdecode(p.stderr).strip() or 'git が失敗した')

        def changed():
            p = subprocess.run(['git', '-C', str(d), *QUIET, '--no-optional-locks', 'status', '--porcelain', '-z', '--untracked-files=no',
                                *(['--ignore-submodules=dirty'] if child else [])], stdin=subprocess.DEVNULL, capture_output=True, env=env)
            if p.returncode:
                raise ValueError(os.fsdecode(p.stderr).strip() or 'git が失敗した')
            # 1 欄は「XY 道筋」
            return next((e[3:] for e in p.stdout.split(b'\0') if e), None)
        # 見方 1。1 回の呼び出しに 2 つのフラグを書くと、assume-unchanged しか外れない
        for flag in ('--no-skip-worktree', '--no-assume-unchanged'):
            update(flag, '-z', '--stdin', entries=paths)
        found = changed()
        if found is None:
            # 見方 2。core.ignoreStat=true では、git が入れ直した項目に assume-unchanged をまた付けるので、外し直す
            update('-z', '--index-info', entries=flagged)
            update('--no-assume-unchanged', '-z', '--stdin', entries=paths)
            found = changed()
    return found


class LeaseRefused(Exception):
    pass


def current_lease(r, token):
    lease = held_lease(read(r / 'lease.json'), token)
    why = check_token(lease, token)
    if why:
        raise LeaseRefused(why)
    return lease


def tasks_at(repo):
    r = root(repo) / 'tasks'
    r.mkdir(exist_ok=True)
    return r


def registered_task(d):
    record = read(root(d) / 'task.json')
    ident = str(record.get('task')) if isinstance(record, dict) else ''
    task = read(tasks_at(checkout_of(d)) / (ident + '.json')) if re.fullmatch('[a-f0-9]{16}', ident) else None
    return task if task and Path(task['dir']).resolve() == d.resolve() else None


def read_journal(rr):
    journal = read(rr / 'integration.json')
    if journal is not None and not (isinstance(journal, dict) and all(isinstance(journal.get(k), str) for k in ('dir', 'expected', 'verified', 'target'))):
        raise ValueError(f"統合の記録の形が違う: {rr / 'integration.json'}")
    return journal


def settle_integration(rr, repo, own=None):
    """統合の記録を実物で閉じ、閉じられなければその記録を返す。統合先に書きかけが無ければ、ff は反映し終えたか、始まらなかったかのどちらか。
    始まらなかった記録と、作業場所が消えた記録は要らない（リポジトリ全体が止まらない）。反映し終えた記録は、担当が完了を受け取るので、own の作業場所か
    担当の作業場所が消えたときだけ閉じる。"""
    journal = read_journal(rr)
    if not journal or not clean(repo):
        return journal
    landed = sh(['git', 'merge-base', '--is-ancestor', journal['verified'], journal['target']], repo)[0] == 0
    if landed and journal['dir'] != own and Path(journal['dir']).exists():
        return journal
    (rr / 'integration.json').unlink()
    return None


def task_at(d):
    task = registered_task(d)
    if not task:
        raise ValueError('この場所は task start で作った作業場所ではない')
    return task


def task_dir(rr, ident):
    return rr.parent.parent / 'worktrees' / rr.name / ident


def worktrees(repo):
    result, entry = [], {}
    for field in git(repo, 'worktree', 'list', '--porcelain', '-z').split('\0'):
        if not field:
            if entry:
                result.append(entry)
                entry = {}
        else:
            k, _, v = field.partition(' ')
            entry[k] = v
    return result


def unclosed_tasks(repo, rr, registry):
    """担当のいない（解放済みか、期限が切れて MAX_LEASE_MIN を過ぎた）タスクのうち、作業場所が残っているもの。期限が切れて間もない
    タスクは、持ち主が子を待っているだけかもしれないので並べない（maybe_waiting）。作業場所は task cleanup と、片付けの途中で止まったタスクを仕上げる task resume だけが消すので、
    呼ばれなかったタスクは残り続ける。それを、次の task start と task list で見えるようにする。見るだけで、何も消さず、
    リポジトリにも状態の置き場にも何も書かない（統合の照合の object は一時の置き場に書かせ、記録の置き場は作らずに読む。
    一時の置き場は tempfile の決める場所で、TMPDIR を状態の置き場やリポジトリの中に向けた場合は、そこに書いて消す）。
    commits は、基点から HEAD までの commit の数（0 なら、まだ何も commit していないタスク）。landed は、HEAD の変更が統合先に
    入っているか（task cleanup の保存先の照合と同じ contained。squash・rebase で入ったものも真）。確かめられなければ None。
    未 commit の変更・無視されるファイル・終わっていない操作は見ない（片付けるときに task cleanup が見る。大きい作業場所の
    git status を、task start のたびに回さない）。ほかのタスクの記録は書く子が書き換えうるので、読めない・形の違う値は、
    そのタスクを「確かめられない」として並べるだけにし、ここからは例外を出さない（task start を止めない）。task list は、
    担当の記録が JSON として読めないタスクがあると、前からのとおり、ここへ来る前に理由を出して止まる（壊れた記録を黙って通さない。
    JSON として読めても形の違う記録（文字列だけ・深い入れ子）では、理由を出さずに例外で落ちる。これも前からの振る舞い）。"""
    found, now = [], time.time()
    for p in sorted(registry.glob('*.json')):
        if not re.fullmatch('[a-f0-9]{16}', p.stem):
            continue
        dest = task_dir(rr, p.stem)
        if not dest.is_dir():
            continue
        entry = {'task': p.stem, 'dir': str(dest), 'target': None, 'lease': 'unreadable', 'commits': None, 'landed': None}
        try:
            lease = read(record_at(dest) / 'lease.json')
            if alive(lease, now) or maybe_waiting(lease, now):
                continue
            entry['lease'] = 'released' if lease is None else 'expired'
        except Exception:
            pass
        try:
            task = read(p)
            if isinstance(task, dict) and re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', str(task.get('base'))) and isinstance(task.get('target'), str):
                entry['target'] = task['target']
                head = git(dest, 'rev-parse', '--verify', 'HEAD')
                entry['commits'] = int(git(dest, 'rev-list', '--count', task['base'] + '..' + head))
                entry['landed'] = contained(repo, head, 'refs/heads/' + task['target'], look_only=True)
        except Exception:
            pass
        found.append(entry)
    return found


def task_command(a):
    repo = checkout_of(a.dir)
    rr = root(repo)
    with locked(rr) as rf:
        registry = tasks_at(repo)
        if a.action == 'list':
            tasks = []
            for p in sorted(registry.glob('*.json')):
                task = read(p)
                lease = read(record_at(Path(task['dir'])) / 'lease.json')
                tasks.append({**task, 'lease': {k: v for k, v in lease.items() if k != 'token'} if lease else None})
            rest = unclosed_tasks(repo, rr, registry)
            return out({'ok': True, 'tasks': tasks, 'integration': read(rr / 'integration.json'), **({'unclosed': rest} if rest else {})})
        if a.action == 'start':
            top = Path(git(a.dir, 'rev-parse', '--show-toplevel')).resolve()
            common = Path(git(a.dir, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
            main_checkout = Path(git(a.dir, 'rev-parse', '--absolute-git-dir')).resolve() == common
            if sh(['git', 'config', '--file', str(common / 'config'), 'core.worktree'], a.dir)[0] and (
                    common != (repo / '.git').resolve() or (main_checkout and top != repo)):
                # 管理ディレクトリから checkout へ戻る道が無く、作業場所から一覧を引けない（名前が .git の管理ディレクトリに手で足した linked worktree は見分けない）
                raise ValueError('--separate-git-dir の checkout にはタスクの作業場所を作れない')
            target = a.target or (registered_task(top) or {}).get('target')
            if not target:
                rc, full, _ = sh(['git', 'symbolic-ref', '--quiet', 'HEAD'], a.dir)  # --short はタグと同じ名前で heads/ を付ける
                if rc:
                    raise ValueError('HEAD がブランチにない。--target で統合先を指定する')
                target = full.removeprefix('refs/heads/')
            git(repo, 'show-ref', '--verify', 'refs/heads/' + target)
            base = git(repo, 'rev-parse', '--verify', (a.base or 'refs/heads/' + target) + '^{commit}')
            rest = unclosed_tasks(repo, rr, registry)
            ident = secrets.token_hex(8)
            dest = task_dir(rr, ident)
            task = {'task': ident, 'dir': str(dest), 'branch': 'bb-writ/' + ident,
                    'base': base, 'target': target, 'checkout': str(repo)}
            write(registry / (ident + '.json'), task)
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                git(repo, 'worktree', 'add', '-b', task['branch'], '--', str(dest), base, fds=(rf,))
            except (ValueError, OSError):
                if not dest.exists() and not commit_of(repo, 'refs/heads/' + task['branch']) and not any(
                        Path(w['worktree']).resolve() == dest.resolve() for w in worktrees(repo)):
                    (registry / (ident + '.json')).unlink()
                raise
            wr = root(dest)
            with locked(wr):
                write(wr / 'task.json', task)
                res, code = claim_at(wr, dest, a.holder, a.lease)
            return out({**task, **res, **({'unclosed': rest} if rest else {})}, code)
        if a.action == 'resume':
            if not re.fullmatch('[a-f0-9]{16}', a.task):
                raise ValueError('task list の task 識別子を指定する')
            task = read(registry / (a.task + '.json'))
            if not task:
                raise ValueError('そのタスクの記録が無い')
            dest = task_dir(rr, a.task)
            if (task.get('task'), Path(str(task.get('dir'))).resolve(), task.get('branch')) != (a.task, dest.resolve(), 'bb-writ/' + a.task) or \
                    not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', str(task.get('base'))):
                raise ValueError('タスクの記録が task start の作った値と合わない。記録を照合する')
            cleanup = task.get('cleanup')
            # task cleanup の git worktree remove が作業場所を消している途中で止まり、Git の登録だけが消えた残り（#77）。task cleanup が
            # そう見て記録に書いたもの（removal_stopped）だけ。git worktree remove を呼ぶ前に止まった片付けの印は、作業が続きうるので数えない
            leftover = bool(cleanup) and cleanup.get('removal_stopped') is True and dest.exists() and not any(
                Path(w['worktree']).resolve() == dest.resolve() for w in worktrees(repo))
            if cleanup and (leftover or not dest.exists()):
                if not contained(repo, cleanup['commit'], cleanup['saved']):
                    raise ValueError(f"片付けの途中の保存先 {cleanup['saved']} に、このタスクの変更が入っていない。記録と差分を照合する")
                branch = commit_of(repo, 'refs/heads/' + task['branch'])
                if branch and branch != cleanup['commit']:
                    raise ValueError('片付けの途中の後にタスクのブランチが進んだ。記録と差分を照合する')
                if leftover:
                    # 消すと戻せないので、上の照合の後に消す。removal_stopped は、task cleanup が、未保存の変更が無いと確かめた作業場所を
                    # 消している途中で、Git の登録が消えたのを見た印。いまこの道筋に在るものが、そのときの残りかは確かめない。止まった後に
                    # 書かれたものも、残りを消して置き直したもの（同じ道筋に作り直した作業場所の登録が、また消えた形）も消す（守らない範囲）。
                    # 最初に消せなかった項目で止まる（どこまで消えるかは、読む順しだい）
                    try:
                        remove_tree(dest)
                    except RemoveFailed as e:  # OSError で素通しにしない（main は PermissionError を、記録の置き場を読み書きできない（2）と読む）
                        where = os.fsencode(e.path).decode('utf-8', 'backslashreplace')
                        raise ValueError(f"片付けの途中の残りを消せない: {where}: {e.error.strerror or e.error}。"
                                         f"原因を直してから、もう一度 task resume --task {task['task']}") from e.error
                if branch:
                    git(repo, 'branch', '-D', task['branch'], fds=(rf,))
                wr = root(dest)
                with locked(wr):
                    (wr / 'lease.json').unlink(missing_ok=True)
                    (registry / (task['task'] + '.json')).unlink()
                return out({'ok': True, 'completed': True, **cleanup, 'note': note(wr)})
            trees = worktrees(repo)
            registered = any(Path(w['worktree']).resolve() == dest.resolve() for w in trees)
            if not dest.exists() and not registered:
                if any(w.get('branch') == 'refs/heads/' + task['branch'] or
                       Path(w['worktree']).resolve() == dest.resolve() for w in trees):
                    raise ValueError('別の場所か不完全な worktree 登録が残る。先に実物を照合する')
                dest.parent.mkdir(parents=True, exist_ok=True)
                branch = commit_of(repo, 'refs/heads/' + task['branch'])
                git(repo, 'worktree', 'add', *([] if branch else ['-b', task['branch']]),
                    '--', str(dest), task['branch'] if branch else task['base'], fds=(rf,))
                registered = True
            if not dest.is_dir() or not registered:
                raise ValueError('作業場所の作成が未完了か、登録と実物が異なる。記録と git worktree list を照合する')
            actual_branch = next(w.get('branch') for w in worktrees(repo) if Path(w['worktree']).resolve() == dest.resolve())
            git(dest, 'rev-parse', '--verify', 'HEAD')
            wr = root(dest)
            with locked(wr):
                write(wr / 'task.json', task)
                res, code = claim_at(wr, dest, a.holder, a.lease, take_over=a.take_over)
            return out({**task, **res, 'worktree_branch': actual_branch}, code)
        task = task_at(a.dir)
        wr = root(a.dir)
        with locked(wr) as wf:
            current_lease(wr, a.token)
            if git(a.dir, 'symbolic-ref', '--quiet', 'HEAD') != 'refs/heads/' + task['branch']:
                raise ValueError('タスクのブランチが変更されている。元のブランチに戻して保存先を照合する')
            journal = settle_integration(rr, repo, own=str(a.dir.resolve()))
            if not clean(a.dir, ignored=True, child=True) or pending(wr) or (journal and journal['dir'] == str(a.dir.resolve())):
                raise ValueError('未保存の変更・未完了の操作がある。作業場所を保持する')
            rel = unremovable_dir(a.dir)
            if rel is not None:
                # git worktree remove は、作業場所を消し切れなくても Git の登録を消す。消し始める前に拒む（#77）
                raise ValueError(f"書き込めないディレクトリがある: {os.fsencode(rel).decode('utf-8', 'backslashreplace')}。中の項目を消せないので、作業場所を保持する")
            ref = git(repo, 'rev-parse', '--symbolic-full-name', a.saved)
            if not ref.startswith(('refs/heads/', 'refs/tags/')):
                raise ValueError(f'保存先 {a.saved} の解決先 {ref or "（名前の付いた ref でない）"} は、ローカルのブランチかタグでない。'
                                 'ローカルのブランチかタグを指定する')
            if ref == 'refs/heads/' + task['branch']:
                raise ValueError('タスク自身のブランチは片付けで削除するので、保存先にならない。統合先のブランチかタグを指定する')
            head = git(a.dir, 'rev-parse', 'HEAD')
            if not contained(repo, head, ref):
                raise ValueError(f'保存先 {ref} に、このタスクの変更が入っていない。統合してから片付ける')
            write(registry / (task['task'] + '.json'), {**task, 'cleanup': {'saved': ref, 'commit': head}})
            try:
                # 中で status を起こす。子が commit した hooks（core.hooksPath が作業場所の中の置き場のとき）も走らせない（-c は中の status に伝わる）
                git(repo, *QUIET, '-c', 'core.hooksPath=/dev/null', 'worktree', 'remove', str(a.dir.resolve()), fds=(rf, wf))
            except ValueError as e:
                # 片付けが始まったかは、作業場所が残っているかでなく、Git の登録が残っているかで分かる。git は、作業場所を消し切れなくても
                # （変更できないファイル、消している間に書き足すプロセスなど、上の確かめで見えない理由）登録を消し、戻す道具が無い（#77）
                # 登録が消えたのを見て記録に書く前に止まると（下の git worktree list の失敗、このプロセスへのシグナル）、removal_stopped の無い
                # 印だけが残り、task resume は残りを消さない（守らない範囲。残りが無くなれば、task resume が仕上げる）
                registered = any(Path(w['worktree']).resolve() == a.dir.resolve() for w in worktrees(repo))
                if registered and a.dir.exists():  # 登録が残っている（lock、初期化したサブモジュール、status の拒否など）。片付けの途中の記録は残さない。--force で通さない（中身を失う）
                    write(registry / (task['task'] + '.json'), task)
                elif a.dir.exists():  # 片付けの途中の記録に、登録が消えた残りだと書く。残りは task resume が消して仕上げる
                    write(registry / (task['task'] + '.json'), {**task, 'cleanup': {'saved': ref, 'commit': head, 'removal_stopped': True}})
                    raise ValueError(f"作業場所を消している途中で止まった（{e}）。Git の登録は消えた。未保存の変更は無かった。"
                                     f"消せない原因を直してから、task resume --task {task['task']} で片付けを仕上げる") from e
                raise
            git(repo, 'branch', '-D', task['branch'], fds=(rf, wf))
            (wr / 'lease.json').unlink()
            (registry / (task['task'] + '.json')).unlink()
            return out({'ok': True, 'saved': ref, 'commit': head, 'note': note(wr)})


def integrate(a):
    repo = checkout_of(a.dir)
    rr, wr = root(repo), root(a.dir)
    if rr == wr:
        raise ValueError('統合は task start で作った作業場所から実行する')
    # 全コマンドで repository → worktree の順に取り、検証後の反映を直列にする。
    with locked(rr) as rf, locked(wr) as wf:
        current_lease(wr, a.token)
        task = task_at(a.dir)
        ref = 'refs/heads/' + task['target']
        candidates = [w for w in worktrees(repo) if w.get('branch') == ref]
        if len(candidates) != 1:
            raise ValueError('統合先ブランチを checkout した作業場所が 1 つ必要')
        dest = Path(candidates[0]['worktree'])
        if dest.resolve() != repo.resolve():
            raise ValueError('ローカル統合先は元の checkout に限る。対象側の PR 経路などで統合する')
        target_lease = read(rr / 'lease.json')
        if alive(target_lease, time.time()):  # この拒否を外す退行を落とす試験は無い（守らない範囲）
            raise ValueError('統合先の checkout に生きた担当がいる。書き換えず待つ')
        held = run_holder(rr)
        if held:
            raise ValueError(f'統合先の checkout で op run の外部操作がまだ動いている: {held}。終わるのを待つ')
        if git(a.dir, 'symbolic-ref', '--quiet', 'HEAD') != 'refs/heads/' + task['branch']:
            raise ValueError('タスクのブランチが変更されている')
        journal_file = rr / 'integration.json'
        journal = read_journal(rr)
        head, target = git(a.dir, 'rev-parse', 'HEAD'), git(repo, 'rev-parse', ref)
        if journal and journal['dir'] == str(a.dir.resolve()) and a.verified == journal['verified'] and a.expected == journal['expected'] and clean(dest) \
                and subprocess.run(['git', '-C', str(repo), 'merge-base', '--is-ancestor', journal['verified'], target], capture_output=True).returncode == 0:
            journal_file.unlink()
            return out({'ok': True, 'recovered': True, 'commit': journal['verified'], 'target_commit': target})
        if settle_integration(rr, repo):
            raise ValueError('別の統合が未完了か、統合先に未 commit の変更がある。task list の integration から担当と作業場所を調べ、先に照合する')
        if head != a.verified or target != a.expected:
            raise ValueError('検証した commit または統合先が変わった。最新の統合先を取り込み、再検証する')
        if not clean(a.dir, child=True) or not clean(dest):
            raise ValueError('作業場所か統合先に未 commit の変更がある。変更は保持する')
        if sh(['git', 'merge-base', '--is-ancestor', target, head], repo)[0]:
            raise ValueError('統合先が作業場所に入っていない。最新の統合先を取り込み、再検証する')
        write(journal_file, {'dir': str(a.dir.resolve()), 'expected': target, 'verified': head, 'target': ref})
        try:
            git(dest, 'merge', '--ff-only', '--no-overwrite-ignore', head, fds=(rf, wf))
        except (ValueError, OSError):
            settle_integration(rr, repo)
            raise
        if git(repo, 'rev-parse', ref) != head or not clean(dest):
            raise ValueError('統合後の実物が検証した commit と一致しない。記録と差分を照合する')
        journal_file.unlink()
        return out({'ok': True, 'commit': head, 'target': ref})


def op_step(a, r: Path, live, now: float, action: str):
    """op の begin・done を 1 件記録する。(結果, 終了コード) を返す。呼ぶ側が r の排他を持つ。"""
    why = check_token(live, a.token)
    if why:
        return ({"ok": False, "reason": why}, 3)
    if not a.key:
        return ({"ok": False, "reason": "--key が要る"}, 2)
    known = ops(r).get(a.key)
    acct = None
    kind = a.key.split(":")[0] if ":" in a.key else None
    if action == "begin" and len(a.key.encode("utf-8")) > 1024:  # ロックの中身の鍵を読み切れるように
        return ({"ok": False, "reason": "鍵の長さは 1024 バイトまで"}, 2)
    if action == "begin" and kind not in EXTERNAL and kind != "local" and not (kind or "").startswith("local-"):
        return ({"ok": False, "reason": f"鍵 {a.key!r} の種類が分からない。鍵は <種類:対象> で、種類は外部操作なら "
                    "push・pr・comment・merge・release・issue のどれか（小文字）、外部に出ない操作の記録なら local（または local- で始まる名前）"}, 2)
    if action == "begin" and kind in EXTERNAL:
        if not a.frm:
            return ({"ok": False, "reason": f"外部操作には --from <実行する場所> が要る。元の checkout は {checkout_of(a.dir)}"}, 2)
        res, code = check(a.dir, a.frm, a.key.split(":")[0])
        if code:
            return (res, code)
        acct = res["from"]
    verified = None
    if action == "begin" and a.key.split(":")[0] in PUBLISH:
        head = commit_of(a.dir, "HEAD")
        if not head:
            return ({"ok": False, "reason": f"作業場所 {a.dir} の HEAD を解決できない"}, 2)
        if not a.verified:
            return ({"ok": False, "reason": f"公開する操作には --verified <検証した commit> が要る。作業場所の HEAD は {head}"}, 2)
        verified = commit_of(a.dir, a.verified)
        if verified != head:
            return ({"ok": False, "reason": f"検証した commit {verified or a.verified} と作業場所の HEAD {head} が違う。"
                        "検証の後に成果物が変わった。今の HEAD で証拠を取り直してから出す"}, 6)
    reviewed, reviews, judges, audit, delta, not_run = None, None, [], None, None, None
    if action == "begin" and getattr(a, "not_run", None) is not None:
        if a.key.split(":")[0] not in REVIEWED:
            return ({"ok": False, "reason": "--not-run は、push・pr の関門で、子が sandbox のせいで実行できなかった記録を数える宣言。ほかの操作には付けない"}, 2)
        if not a.not_run.strip():
            return ({"ok": False, "reason": "--not-run の後に、子が対象の試験・検査を実行できなかった理由を書く（headless Chrome が子の sandbox で起動しない、など）"}, 2)
        not_run = a.not_run.strip()
    if action == "begin" and a.key.split(":")[0] in REVIEWED:
        if not a.reviewed:
            return ({"ok": False, "reason": "push・pr には --reviewed <届ける前の審査を通した commit> か --reviewed none:<要らない理由> が要る。"
                        "試験・検査を足した・変えたなら、入口の条件 (5)・(6) をこの commit で通す。" + changed_since_publish(r, a.dir, head)}, 2)
        if a.reviewed.startswith("none:"):
            if not a.reviewed[5:].strip():
                return ({"ok": False, "reason": "--reviewed none: の後に、審査が要らない理由を書く。" + changed_since_publish(r, a.dir, head)}, 2)
            reviewed = "none: " + a.reviewed[5:].strip()
            if a.delta is not None:
                return ({"ok": False, "reason": "--delta は、審査を通した commit（--reviewed <commit>）から HEAD までの差分の宣言。none: とは並べない"}, 2)
        else:
            if not re.fullmatch(r"[0-9a-f]{7,64}", a.reviewed):
                return ({"ok": False, "reason": f"--reviewed {a.reviewed!r} は commit の ID（16 進）でも none:<理由> でもない。"
                            + changed_since_publish(r, a.dir, head)}, 2)
            reviewed = commit_of(a.dir, a.reviewed)
            if not reviewed:
                return ({"ok": False, "reason": f"--reviewed の commit {a.reviewed} が見つからない。" + changed_since_publish(r, a.dir, head)}, 2)
            if not reviewed.startswith(a.reviewed):  # 16 進だけの名前のブランチ・タグは、git が ID より先に解決する
                return ({"ok": False, "reason": f"--reviewed {a.reviewed} は ID でなく参照（{reviewed[:12]}）に解決された。完全な ID を書く。"
                            + changed_since_publish(r, a.dir, head)}, 2)
            if reviewed == head and a.delta is not None:
                return ({"ok": False, "reason": "--delta は、審査を通した commit が HEAD と違うときの、その後の差分の宣言。HEAD を審査したなら付けない"}, 2)
            if reviewed != head:
                rc, files, err = sh(["git", "diff", "--name-only", f"{reviewed}..{head}"], a.dir)
                after = (", ".join(files.splitlines()) or "なし") if rc == 0 else f"（差分を出せない: {err}）"
                if a.delta is None:
                    return ({"ok": False, "reason": f"審査を通した commit {reviewed} と作業場所の HEAD {head} が違う。"
                                f"審査の後に変えた。直した commit で審査を通し直してから出すか、審査の後の差分の危険度が低（文と試験だけで、"
                                f"振る舞い・主張を変えない）なら --delta 低:<理由> を付ける（宣言は HEAD の監査役が確かめる。"
                                f"`interrogate` の 5）。審査の後に変えたファイル: {after}"}, 6)
                d = re.fullmatch(r"(低|中|高):(.*)", a.delta, re.S)
                if not d or not d.group(2).strip():
                    return ({"ok": False, "reason": "--delta は 低:<理由> の形で、審査を通した commit から HEAD までの差分の危険度を書く"}, 2)
                if d.group(1) != "低":
                    return ({"ok": False, "reason": f"審査の後の差分の危険度が{d.group(1)}なら、HEAD で審査を通してから出す（--reviewed に HEAD の commit）。"
                                f"審査の後に変えたファイル: {after}"}, 2)
                rc, err = is_ancestor(reviewed, head, a.dir)
                if rc:
                    return ({"ok": False, "reason": f"審査を通した commit {reviewed} は HEAD {head} の祖先でない"
                                + ("（rebase・amend などで履歴から外れたか、確かめられない" + (f": {err}" if err else "") + "）" if rc == 1 else f"（確かめられない: {err}）")
                                + "。HEAD で審査を通してから出す"}, 6)
                base = task_place(Path(a.dir))[1]
                if base is not None and (reviewed == base or is_ancestor(base, reviewed, a.dir)[0] != 0):
                    return ({"ok": False, "reason": f"審査を通した commit {reviewed} は、このタスクの基点 {base} より後と確かめられない"
                                "（基点か、その前か、別の系統）。基点の審査はタスクの変更を見ていないので、HEAD で審査を通してから出す"}, 6)
                delta = "低: " + d.group(2).strip()
            found, judges, _, unread = reviews_of(reviewed, a.dir if delta else None, bool(not_run))
            if not found:
                return ({"ok": False, "reason": f"commit {reviewed} を対象にした審査役の記録が無い"
                            + ("（--delta では、この作業場所を --cwd にして立てたものだけを数える）" if delta else "")
                            + "。launcher（launch.py、読み取り専用）で、"
                            "この commit を checkout した未 commit の変更が無い場所を --cwd にして審査役を立て、status が ok で返った記録が要る"
                            "（Agent ツールなど launcher を通らない子は記録に残らない）。審査が要らない変更なら none:<理由>"
                            + not_run_hint(reviewed, a.dir if delta else None)
                            + (f"。{unread}" if unread else "")}, 6)
            reviews = found
        risk = re.fullmatch(r"(低|中|高):(.*)", a.risk or "", re.S)
        if not risk or not risk.group(2).strip():
            return ({"ok": False, "reason": "push・pr には --risk 低|中|高:<理由> が要る。危険度は書いた人でなく変更の中身で決める（interrogate の 5）。"
                        "文書だけ・小さな追加は低、普通のコードは中、認証・課金・外部操作・データの削除・皆が使う土台は高"}, 2)
        if risk.group(1) != "低" and reviewed.startswith("none:"):
            return ({"ok": False, "reason": f"危険度が{risk.group(1)}の変更は interrogate を通す（入口の条件 (6)）。--reviewed に、審査を通した commit を書く"}, 2)
        if risk.group(1) != "低" and not judges:
            return ({"ok": False, "reason": f"危険度が{risk.group(1)}の変更は interrogate を通す（入口の条件 (6)）。commit {reviewed} の記録に、"
                        "launcher に --interrogate を付けて立てた審査役のものが無い（条件 (5) の子の記録は代わりにならない）。"
                        "interrogate の 3 の依頼文で、この commit の審査役を --interrogate を付けて立てる"
                        + not_run_hint(reviewed, a.dir if delta else None, 1)}, 6)
        risk = risk.group(1) + ": " + risk.group(2).strip()
        _, _, audits, unread = reviews_of(head, not_run=bool(not_run))
        if not audits:
            return ({"ok": False, "reason": f"作業場所の HEAD {head} を対象にした監査役（返す前の判定）の記録が無い。--reviewed none: でも要る"
                        "（none: は interrogate を省く宣言で、子を省く宣言でない）。launcher（launch.py）に --audit を付け、この commit を "
                        "checkout した未 commit の変更が無い場所を --cwd にして、`playbook-verify` の「返す前の判定」の依頼文で立て、"
                        "status が ok で返り、届けてよい（deliver: yes）と判定した記録が要る（差し戻しの deliver: no の記録と、deliver の無い"
                        "更新前の launcher の記録は数えない。差し戻しなら挙がった項目を閉じてから立て直す）。指摘は、作業の記録の指摘の扱いの欄に書いてから出す。"
                        "launcher が unavailable で返るなら（専用ログインが無いなど）、その problems の理由に従い、"
                        "bb-writ の README の「任意機能の準備」で専用ログインを用意する"
                        + not_run_hint(head, role=2)
                        + (f"。{unread}" if unread else "")}, 6)
        audit = audits
        f = r / "note.md"
        gaps = note_gaps(note_body(f), True, review_rounds(Path(a.dir), bool(not_run)) if reviews else 0)
        if gaps:
            return ({"ok": False, "note": {"path": str(f), "empty": gaps}, "reason": f"作業の記録 {f} の欄が空か無い: {'、'.join(gaps)}。目的・制約・確かめ方は担当を取った直後に、"
                        "審査役・監査役の指摘の扱いは直す前に、審査が 3 周に達したら仕組みの要否も、その欄へ書く（`playbook-deliver` の「記録を残す」と入口の条件 (6)、"
                        "`principle-attack-the-premise`。"
                        "欄の名前の行が無ければ、雛形の行を足す）"}, 2)
    if action == "begin" and known:
        return ({"ok": False, "reason": "同じ鍵の操作が既にある。外部の状態を照合してから進める（やり直さない）", "op": known}, 4)
    if action == "done" and not known:
        return ({"ok": False, "reason": "begin の記録が無い"}, 2)
    if action == "done" and known.get("state") == "done":
        return ({"ok": False, "reason": "この操作は既に終わりを記録している", "op": known}, 4)
    e = {"key": a.key, "state": "begun" if action == "begin" else "done", "at": now, "holder": live["holder"]}
    e.update({"what": a.what} if action == "begin" else {"result": a.result})
    if action == "done":
        e["by"] = getattr(a, "by", None)
        state = note_state_of(r)
        if state:
            e["note_state"] = state
    if acct:
        e.update({"from": acct["at"], "account": acct.get("login")})
    if verified:
        e["verified"] = verified
    if reviewed:
        e["reviewed"] = reviewed
    if reviews:
        e["reviews"] = reviews
    if audit:
        e["audits"] = audit
    if reviewed:
        e["risk"] = risk
    if delta:
        e["delta"] = delta
    if not_run:
        e["not_run"] = not_run
    with open(r / "ops.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")
    res = {"ok": True, "op": ops(r)[a.key]}
    if reviewed:
        res["changed"] = changed_since_publish(r, a.dir, head)
    return res, 0


def run_lock(r: Path, key: str) -> Path:
    return r / f"running-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}.lock"


def run_holder(r: Path):
    """コマンドがまだ終わっていない（監督役が見届けていない）op run があれば、その操作の鍵を返す（作業場所に 1 つ）。
    無ければ None。呼ぶ側が r の排他を持つ。
    ロックの flock で見る。プロセスが死ねば OS が外すので、pid やプロセスの開始時刻を照らさない。
    監督役はコマンドが終わったらロックのファイルを消すので、コマンドが残したもの（git の credential daemon など）が
    消えたファイルを持っていても数えない。監督役が強制終了されて消されなかったロックは、コマンドとそれが残したものが
    終わるまで数える（外部操作を続けているかもしれない）。ただし鍵が done のロック（done を書いた直後に監督役が殺された）は
    数えない。そのファイルは、持つ者が居なくなったときに消す（それまで、呼ぶたびに記録を読む）。
    開けない・読めないロックは消さない（鍵を始めた記録から引き、done なら数えず、引けなければファイル名で数える）。"""
    held = None
    done = None

    def records():
        try:
            return {k: e for k, e in ops(r).items() if isinstance(k, str) and isinstance(e, dict)}
        except Exception:
            return {}
    for f in sorted(r.glob("running*.lock")):
        fd = None
        try:
            fd = os.open(f, os.O_RDONLY)
            text = os.read(fd, 4096).decode("utf-8", "replace")  # 鍵は 1024 バイトまで（op_step が拒否する）
        except FileNotFoundError:
            continue
        except OSError:
            if fd is not None:
                os.close(fd)
            done = records() if done is None else done
            if any(e.get("state") == "done" and run_lock(r, k).name == f.name for k, e in done.items()):
                continue
            keys = [k for k, e in done.items() if e.get("state") == "begun" and run_lock(r, k).name == f.name]
            held = held or (keys[0] if keys else f.name)
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                done = records() if done is None else done
                if held is None and done.get(text, {}).get("state") != "done":
                    held = text or "?"
                continue
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
        finally:
            os.close(fd)
    return held


STOP_SIGNALS = {signal.SIGTERM, signal.SIGHUP, signal.SIGINT}


def run_log(r: Path, key: str) -> Path:
    return r / f"run-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}.log"


MAX_TAIL = 8 * 1024


def tail(f: Path, lines: int = 20, budget: int = MAX_TAIL) -> str:
    """出力の末尾の lines 行を、記録（ops.jsonl に書く JSON の値）にして budget バイトまで返す（budget は先頭を省いた印の 24 バイトより大きいこと）。
    出力の末尾の ASCII の空白（改行を含む。全角の空白などは残す）は記録しない。最後の意味のある文字の前の 64 KiB だけを読む
    （そこを探して後ろから 64 KiB ずつ遡る。16 MiB 遡っても無ければ空を返す）。行の中の空白は変えない。"""
    if not f.is_file():
        return ""
    with open(f, "rb") as fh:
        end = fh.seek(0, os.SEEK_END)
        for _ in range(256):
            start = max(0, end - 64 * 1024)
            fh.seek(start)
            block = fh.read(end - start)
            if block.strip():
                end = start + len(block.rstrip())
                break
            if start == 0:
                return ""
            end = start
        else:
            return ""
        start = max(0, end - 64 * 1024)
        partial = False
        if start > 0:
            fh.seek(start - 1)
            partial = fh.read(1) != b"\n"
        fh.seek(start)
        data = fh.read(end - start)
        if partial:  # 文字の途中から読まない（UTF-8 の継続バイトを飛ばす）
            k = 0
            while k < min(3, len(data)) and 0x80 <= data[k] <= 0xBF:
                k += 1
            data = data[k:]
        rows = data.split(b"\n")  # 行は改行だけで数える（\r の進捗表示で行を割らない）
    text = b"\n".join(rows[-lines:]).decode("utf-8", "replace")
    cut = partial and len(rows) <= lines

    def size(s):
        return len(json.dumps(("（先頭を省いた）" if cut else "") + s, ensure_ascii=False).encode()) - 2
    if size(text) > budget:
        cut, lo, hi = True, 0, len(text)
        while lo < hi:
            mid = (lo + hi) // 2
            if size(text[mid:]) <= budget:
                hi = mid
            else:
                lo = mid + 1
        text = text[lo:]
    return ("（先頭を省いた）" if cut else "") + text


def show(fh):
    for chunk in iter(lambda: fh.read(64 * 1024), b""):
        sys.stderr.buffer.write(chunk)
    sys.stderr.buffer.flush()


def keep_lease(r: Path, token: str):
    with locked(r):
        lease = read(r / "lease.json")
        now = time.time()
        if lease and lease.get("token") == token and float(lease["expires_at"]) < now + 19 * 60:
            lease["expires_at"] = now + 20 * 60
            write(r / "lease.json", lease)


def op_run(a, r: Path, argv: list) -> int:
    """begin の照合を通ったときだけ、argv を --from（無ければ作業場所）で実行し、終了コードと出力の末尾を done に記録する。
    begin とコマンドを手で並べると、begin が拒否されてもコマンドが走り、done には打った文字が残ったため。
    実行と記録は、別のセッションに起こした監督役（supervise）が行う。この op run が止められても、監督役はコマンドの終わりまで
    担当を保ち、done を書く。20 分を超えるコマンドの間に担当が切れ、後任が終わっていない操作をやり直したため。"""
    if not argv:
        return out({"ok": False, "reason": "op run には、-- の後に実行するコマンドが要る（例: -- git push origin <ブランチ>）"}, 2)
    fd = None
    try:
        with locked(r):
            lease = read(r / "lease.json")
            now = time.time()
            held = run_holder(r)  # 照合と記録の前に見る。拒否した操作の begun を残さない
            if held:
                res, code = {"ok": False, "reason": f"この作業場所で、op run の外部操作がまだ動いている: {held}。"
                             "終わるのを待つ（終われば監督役が done を書く）"}, 4
            else:
                res, code = op_step(a, r, held_lease(lease, a.token), now, "begin")
            if not code:
                fd = os.open(run_lock(r, a.key), os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.write(fd, a.key.encode("utf-8"))
                if not alive(lease, now) or float(lease["expires_at"]) < now + 20 * 60:
                    lease["expires_at"] = now + 20 * 60
                    write(r / "lease.json", lease)
        if code:
            return out(res, code)
        gate("after-begin")
        spec = json.dumps({"dir": str(a.dir), "token": a.token, "key": a.key, "cwd": str(a.frm or a.dir), "lock_fd": fd})
        # 出力のファイルは先に作って開いておく。消すのは監督役だけ（done を書けたら消す）で、消されても開いたファイルから表示できる
        # 監督役がシグナルの扱いを登録するまでの間に止められないよう、塞いだまま起こす（監督役が登録してから外す）
        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
        errf = shown = None
        try:
            run_log(r, a.key).write_bytes(b"")
            shown = open(run_log(r, a.key), "rb")
            errf = open(r / "supervise.err", "ab")
            sup = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "_supervise", spec, "--", *argv],
                                   pass_fds=[fd], start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=errf)
        except OSError as e:
            a.result = f"exit 127: 起動の準備ができない（出力のファイルか監督役。コマンドは走っていない）: {e}"
            with locked(r):
                res, _ = op_step(a, r, read(r / "lease.json"), time.time(), "done")
                run_lock(r, a.key).unlink(missing_ok=True)
                try:
                    run_log(r, a.key).unlink(missing_ok=True)
                except OSError:
                    pass
            if shown:
                shown.close()
            return out({"ok": False, "exit": 127, "op": res.get("op"), "reason": a.result}, 127)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
            if errf:
                errf.close()
    finally:
        if fd is not None:
            os.close(fd)  # ロックは監督役が持ち続ける
    lines = sup.communicate()[0].decode("utf-8", "replace").strip().split("\n")  # 結果の JSON は 1 行。splitlines は出力の U+2028 などでも割る
    with shown:
        try:
            show(shown)
        except OSError as e:
            sys.stderr.write(f"（出力を読めない: {e!r}）\n")
    rec, unreadable = {}, ""
    if len(lines) < 2:
        try:
            rec = ops(r).get(a.key, {})
        except RecordError as e:
            unreadable = f"{e}。その行を直してから、"
        except Exception as e:
            unreadable = f"記録を読めない: {e!r}。読めるようにしてから、"
    if len(lines) < 2 and rec.get("state") == "done":
        result = rec.get("result")
        m = re.match(r"exit ([0-9]{1,3}):", result) if isinstance(result, str) and rec.get("by") == "supervise" else None
        if not m or int(m.group(1)) > 255:
            return out({"ok": False, "exit": None, "op": rec, "reason": "監督役が結果を返さずに終わった。記録の result で判断する"}, 1)
        rc = int(m.group(1))
        return out({"ok": rc == 0, "exit": rc, "op": rec, "reason": "監督役が結果を返さずに終わったが、結果は記録済み"}, rc)
    if len(lines) < 2:
        return out({"ok": False, "exit": None, **({"op": rec} if rec else {}),
                    "reason": f"監督役が結果を返さずに終わった。{r / 'supervise.err'} を読み、{unreadable}"
                    "op pending の running が消えるのを待ってから、外部の状態で照合する"}, 1)
    print(lines[1])
    return int(lines[0])


def finish(r: Path, spec: dict):
    """コマンドが終わったので、ロックのファイルを消す。done を書けたかに関わらず消す
    （コマンドが終わった後に二重に走るおそれは無い。done が無ければ begun が残り、照合で閉じる）。
    コマンドが残したものは、消えたファイルのロックを持つだけになり、次の操作を止めない。"""
    run_lock(r, spec["key"]).unlink(missing_ok=True)


def gate(name: str):
    """試験だけが使う止まり所。BB_WRIT_SUPERVISE_GATE のディレクトリに at-<name>（中身は pid）を書き、<name> ができるまで待つ。
    監督役の中の数 ms の窓（fork の後・コマンドを起こす前）に、試験がシグナルを送れるようにする。"""
    d = os.environ.get("BB_WRIT_SUPERVISE_GATE")
    if d and Path(d).is_dir():
        try:
            tmp = Path(d) / f".at-{name}.{os.getpid()}"
            tmp.write_text(str(os.getpid()))
            os.replace(tmp, Path(d) / f"at-{name}")
        except OSError:
            return
        deadline = time.time() + 60
        while not (Path(d) / name).exists() and time.time() < deadline:
            time.sleep(0.02)


def supervise(spec: dict, argv: list) -> int:
    # 2 回目の fork。op run の子孫の木から外れ、木ごと止めるハーネスに巻き込まれない。保留中のシグナルは fork の子に
    # 受け継がれないので、中継（親）が fork の後に見て、パイプで子に渡す。子はそれを受け取るまで起こさない。
    # ここが「起こす前」の決め所で、これより後に中継へ届いたものは、起こした後に op run を止めたのと同じ扱い（コマンドは続く）
    rfd, wfd = os.pipe()
    if os.fork():
        os.close(rfd)
        gate("relay")
        os.write(wfd, (",".join(str(int(s)) for s in signal.sigpending() & STOP_SIGNALS) or "-").encode())
        os._exit(0)
    os.close(wfd)
    gate("relay-told")
    told = b""
    while chunk := os.read(rfd, 64):
        told += chunk
    os.close(rfd)
    told = told.decode()
    if told != "-" and not all(x.isdigit() for x in told.split(",")):  # 中継が渡す前に殺された。止めとみなし、起こさない
        early = {signal.SIGKILL}
    else:
        early = {signal.Signals(int(x)) for x in told.split(",") if x.isdigit()}
    d = Path(spec["dir"])
    r = root(d)
    p = None
    caught = []

    def forward(sig, _frame):
        caught.append(sig)
        if p is not None and p.poll() is None:
            p.send_signal(sig)
    for sig in STOP_SIGNALS:
        signal.signal(sig, forward)
    # op run が塞いで起こした。塞いだまま、起こす前に届いたかを見る。起こした後で外すので、その間に届いたものもコマンドに伝わる
    gate("before-check")
    early |= signal.sigpending() & STOP_SIGNALS
    if early:
        caught.extend(sorted(early))
    warned = set()

    def warn(msg):
        if msg not in warned:
            warned.add(msg)
            sys.stderr.write(f"{spec['key']}: {msg}\n")
            sys.stderr.flush()
    if caught:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, STOP_SIGNALS)
        rc, output = 128 + caught[0], "起動の前に止められた（コマンドは走っていない）"
    else:
        try:
            gate("before-start")
            with open(run_log(r, spec["key"]), "ab") as logf:
                # 出力はファイルへ。op run が止められても、パイプの相手を失って書けなくならない
                p = subprocess.Popen(argv, cwd=spec["cwd"], stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                                     env={**{k: v for k, v in os.environ.items() if k != "BB_WRIT_SUPERVISE_GATE"}, "GIT_TERMINAL_PROMPT": "0"},
                                     pass_fds=[spec["lock_fd"]],
                                     preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_UNBLOCK, STOP_SIGNALS))
        except OSError as e:
            rc, output = 127, f"起動できない（コマンドは走っていない）: {e}"
        else:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, STOP_SIGNALS)
            while True:
                try:
                    p.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    try:
                        keep_lease(r, spec["token"])
                    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
                        warn(f"担当の期限を延ばせない: {e!r}")
            rc = p.returncode if p.returncode >= 0 else 128 - p.returncode
            try:
                output = tail(run_log(r, spec["key"]), budget=MAX_TAIL - len(f"exit {rc}: ".encode()))
            except OSError as e:
                output = f"（出力を読めない: {e!r}）"
    a = argparse.Namespace(key=spec["key"], token=spec["token"], dir=d, frm=None, verified=None, reviewed=None, what="",
                           result=f"exit {rc}: " + output, by="supervise")
    gate("before-done")
    code = None
    try:
        with locked(r):
            res, code = op_step(a, r, read(r / "lease.json"), time.time(), "done")
            if not code:
                try:
                    run_log(r, spec["key"]).unlink(missing_ok=True)
                except OSError as e:
                    warn(f"出力のファイルを消せない: {e!r}")
            finish(r, spec)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        if code is not None:
            warn(f"ロックのファイルを消せない: {e!r}")
        else:
            warn(f"done を書けない: {e!r}")
            res, code = {"ok": False, "reason": f"done を書けない: {e!r}"}, 1
            try:
                finish(r, spec)
            except OSError:
                pass
    gate("before-print")
    os.close(spec["lock_fd"])
    if code:
        res, rc = {**res, "exit": rc, "output": output[-2000:]}, code
    else:
        res = {"ok": rc == 0, "exit": rc, "op": res["op"]}
    try:
        print(rc if rc < 256 else 1)
        print(json.dumps(res, ensure_ascii=False), flush=True)
    except BrokenPipeError:  # op run が止められた。結果は記録にある。終わり際の flush でも出さない
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser('task')
    ts = t.add_subparsers(dest='action', required=True)
    for name in ('start', 'resume', 'list', 'cleanup'):
        s = ts.add_parser(name)
        s.add_argument('--dir', type=Path, required=True)
        if name in ('start', 'resume'):
            s.add_argument('--holder', required=True)
            s.add_argument('--lease', type=lease_minutes, default=20)
        if name == 'start':
            s.add_argument('--base')
            s.add_argument('--target')
        if name == 'resume':
            s.add_argument('--task', required=True)
            s.add_argument('--take-over', dest='take_over', action='store_true',
                           help='期限が切れて 24 時間以内の担当も取る（利用者にこのタスクの続きか、捨てることを頼まれたときだけ。閉じ忘れの片付けでは付けない）')
        if name == 'cleanup':
            s.add_argument('--token', required=True)
            s.add_argument('--saved', required=True)
    i = sub.add_parser('integrate')
    i.add_argument('--dir', type=Path, required=True)
    i.add_argument('--token', required=True)
    i.add_argument('--expected', required=True, help='検証時の統合先の完全な commit ID')
    i.add_argument('--verified', required=True, help='検証した作業場所の完全な commit ID')
    for name in ("claim", "renew", "release", "status"):
        s = sub.add_parser(name)
        s.add_argument("--dir", type=Path, required=True)
        if name in ("renew", "release"):
            s.add_argument("--token", required=True)
        if name in ("claim", "renew"):
            s.add_argument("--lease", type=lease_minutes, default=20, help="分")
        if name == "claim":
            s.add_argument("--holder", default=os.environ.get("USER", "?"))
    o = sub.add_parser("op")
    o.add_argument("action", choices=("begin", "done", "pending", "run"))
    o.add_argument("--dir", type=Path, required=True)
    o.add_argument("--token")
    o.add_argument("--key")
    o.add_argument("--what", default="")
    o.add_argument("--result", default="")
    o.add_argument("--from", dest="frm", type=Path, help="外部操作を実行する場所（push・pr・comment・merge・release・issue で必須）")
    o.add_argument("--verified", help="検証した commit（push・pr・merge・release で必須。作業場所の HEAD と一致させる）")
    o.add_argument("--reviewed", help="届ける前の審査を通した commit か none:<理由>（push・pr で必須。commit は作業場所の HEAD と一致させるか、HEAD の祖先にして --delta を付け、その commit を対象に launcher で ok で返った読み取り専用の子（審査役・確かめ役など。監査役は除く）の記録が要る。--risk が中・高なら --interrogate を付けた審査役の記録。どちらでも、HEAD を対象に --audit を付けて立て、届けてよい（deliver: yes）と判定した監査役の記録も要る）")
    o.add_argument("--delta", help="審査を通した commit（--reviewed）から HEAD までの差分の危険度と理由 低:<理由>（push・pr で、--reviewed の commit が HEAD の祖先のときだけ。中・高なら HEAD で審査を通す）")
    o.add_argument("--not-run", dest="not_run", help="子が sandbox のせいで対象の試験・検査を実行できなかった理由（push・pr だけ。付けると、"
                   "その commit を対象に not-run で返り、実行の欄に実行できなかった（no）と書いた子の記録も、審査役・--interrogate の審査役・監査役と審査の周に数える。"
                   "実行の欄の無い報告は数えない。親が HEAD で実行した試験の証拠を作業の記録に書き、監査役に判定させる）")
    o.add_argument("--risk", help="変更の危険度と理由 低|中|高:<理由>（push・pr で必須。中・高は --reviewed none: と一緒に使えない）")
    c = sub.add_parser("check")
    c.add_argument("--dir", type=Path, required=True)
    c.add_argument("--from", dest="frm", type=Path, required=True)
    argv, run_argv = sys.argv[1:], []
    if argv[:1] == ["_supervise"] and "--" in argv:
        return supervise(json.loads(argv[1]), argv[argv.index("--") + 1:])
    if argv[:2] == ["op", "run"] and "--" in argv:
        i = argv.index("--")
        argv, run_argv = argv[:i], argv[i + 1:]
    a = ap.parse_args(argv)
    if a.cmd in ('task', 'integrate'):
        try:
            return task_command(a) if a.cmd == 'task' else integrate(a)
        except LeaseRefused as e:
            return out({'ok': False, 'reason': str(e)}, 3)
        except PermissionError as e:
            return out({'ok': False, 'reason': f"記録の置き場を読み書きできない: {e}"}, 2)
        except RecordError as e:
            return out({'ok': False, 'reason': str(e)}, 1)
        except (ValueError, OSError) as e:
            return out({'ok': False, 'reason': str(e)}, 6)
    if a.cmd == "check":
        res, code = check(a.dir, a.frm)
        return out(res, code)
    r = root(a.dir)
    try:
        return dispatch(a, r, run_argv)
    except RecordError as e:
        return out({"ok": False, "reason": str(e)}, 1)


def dispatch(a, r: Path, run_argv):
    if a.cmd == "op" and a.action == "run":
        return op_run(a, r, run_argv)
    with locked(r):
        now = time.time()
        lease = read(r / "lease.json")
        live = lease if alive(lease, now) else None
        if a.cmd == "status":
            return out({"dir": str(a.dir.resolve()), "holder": live and live["holder"], "expires_in_s": live and round(live["expires_at"] - now),
                        "stale": bool(lease and not live), "pending_ops": pending(r), "note": note(r)})
        if a.cmd == "claim":
            res, code = claim_at(r, a.dir, a.holder, a.lease)
            return out(res, code)
        if a.cmd in ("renew", "release"):
            mine = held_lease(lease, a.token)
            why = check_token(mine, a.token)
            if why:
                return out({"ok": False, "reason": why, "holder": live and live["holder"]}, 3)
            held = run_holder(r) if a.cmd == "release" else None
            if held:
                return out({"ok": False, "reason": f"op run の外部操作がまだ動いている: {held}。終わるのを待ってから手放す"}, 4)
            if a.cmd == "renew":
                n = note(r)
                mine["expires_at"] = now + a.lease * 60
                write(r / "lease.json", mine)
                return out({"ok": True, **({"note": {"path": n["path"], "empty": n["empty"]}} if "empty" in n else {})})
            (r / "lease.json").unlink()
            return out({"ok": True})
        if a.action == "pending":
            return out({"pending_ops": pending(r)})
        held = run_holder(r)
        if held and ((a.action == "done" and a.key == held) or (a.action == "begin" and (a.key or "").split(":")[0] in EXTERNAL)):
            return out({"ok": False, "reason": f"この作業場所で、op run の外部操作がまだ動いている: {held}。終わるのを待つ（終われば監督役が done を書く）"}, 4)
        res, code = op_step(a, r, held_lease(lease, a.token), now, a.action)
        if a.action == "done" and (not code or (code == 4 and res.get("op", {}).get("state") == "done")):
            try:
                run_log(r, a.key).unlink(missing_ok=True)
            except OSError as e:
                res = {**res, "warning": f"出力のファイルを消せない: {e!r}"}
        return out(res, code)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except OSError as e:
        sys.exit(out({"ok": False, "reason": f"記録の置き場を読み書きできない: {e}"}, 2))
