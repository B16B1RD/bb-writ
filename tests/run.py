"""全試験（または引数に渡した試験の名前・モジュール）を、試験 1 つにつき 1 つのプロセスで、並べて回す。

リポジトリの根で `python3 -B tests/run.py [-j N] [名前 ...]`。並べる数の既定は os.cpu_count()。
順に回すと約 630 秒かかり、モジュールごとに並べても最も遅いモジュール（test_launch の約 425 秒）で止まるので、試験ごとに分ける。
落ちた試験の名前と出力を出し、最後に unittest と同じ形の集計（Ran N tests in S s・OK か FAILED (...)）を出す。
全部が通ったとき（飛ばした試験を含む）だけ 0 で終わる。読み込めない試験が 1 つでもあれば、回す前に止まる。
"""
import argparse
import os
import re
import subprocess
import sys
import time
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import process_limit  # noqa: F401  このプロセスと子に上限を掛ける（tests/process_limit.py）


def test_ids(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from test_ids(t)
        else:
            yield t.id()


def run_one(test_id):
    r = subprocess.run([sys.executable, "-B", "-m", "unittest", test_id], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                       errors="replace")
    # 集計は出力の末尾にある（試験が自分で出した同じ形の行と取り違えないよう、最後の一致を読む）
    ran = re.findall(r"^Ran (\d+) tests? in ", r.stdout, re.M)
    status = re.findall(r"^(?:OK|FAILED)(?: \((.*)\))?$", r.stdout, re.M)
    counts = Counter({k: int(v) for k, v in re.findall(r"([a-z ]+)=(\d+)", status[-1])} if status else {})
    if r.returncode != 0 and not counts["failures"] + counts["errors"]:
        counts["errors"] += 1  # 集計を出さずに終わった・試験が無かった（unittest は 5 で終わる）
    return test_id, r.returncode, int(ran[-1]) if ran else 0, counts, r.stdout


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-j", type=int, default=os.cpu_count(), help="同時に回す数（既定は CPU の数）")
    ap.add_argument("names", nargs="*", help="試験の名前かモジュール（例 tests.test_work）。無ければ tests の下の全部")
    args = ap.parse_args()
    sys.path.insert(0, os.getcwd())  # 名前を渡したときも、python3 -m unittest と同じく根から読む
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(args.names) if args.names else loader.discover("tests", top_level_dir=".")
    if loader.errors:
        print("\n".join(loader.errors), file=sys.stderr)
        sys.exit(f"試験を読み込めない（{len(loader.errors)} 件）")
    ids = list(test_ids(suite))
    start = time.monotonic()
    total, counts, failed = 0, Counter(), []
    with ThreadPoolExecutor(max_workers=max(1, args.j)) as pool:
        for f in as_completed([pool.submit(run_one, i) for i in ids]):
            test_id, rc, ran, c, out = f.result()
            total, counts = total + ran, counts + c
            if rc != 0:
                failed.append(test_id)
                print(f"{'=' * 70}\nFAIL: {test_id}（終了コード {rc}）\n{'-' * 70}\n{out}", flush=True)
    print(f"{'-' * 70}\nRan {total} tests in {time.monotonic() - start:.1f}s\n")
    detail = ", ".join(f"{k}={counts[k]}" for k in sorted(counts) if counts[k])
    if failed:
        print(f"落ちた試験（{len(failed)}）:\n" + "\n".join(f"  {t}" for t in sorted(failed)))
        print(f"FAILED ({detail})")
        sys.exit(1)
    print(f"OK ({detail})" if detail else "OK")


if __name__ == "__main__":
    main()
