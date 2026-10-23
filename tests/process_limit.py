"""試験のプロセスとその子に、利用者の今のスレッド数と元の上限の中ほどの上限を掛ける（Linux）。

試験が書いた偽の git が PATH で自分自身を呼び続け、利用者全体のプロセスの上限（ulimit -u）を埋めて、同時に動いていた
Claude Code を落とした（2026-10-23）。上限は利用者ごとの数で数えるので、試験の側だけを低くすれば、試験が同じ誤りをしても
増えるのは試験の分だけで止まり、ほかのプロセスには元の上限までの余裕が残る。余白を +500 にすると、試験の間に同時に動く
Claude Code・Codex がスレッドを増やしただけで、試験が fork できずに落ちた（同日）。余白は今の数と元の上限の間の半分（少なくとも 500）。
利用者がすでに上限の近くにいて、それより下げられないときは掛けない。
tests/__init__.py と、各試験のモジュールの先頭から読む（python3 tests/test_launch.py のように直に走らせると、
__init__.py は読まれず上限が掛からなかった）。何度読んでも、下げるだけなので同じになる。
"""
import os
import sys

if sys.platform.startswith("linux"):
    import resource

    def _user_tasks() -> int:
        """利用者のスレッドの数（上限はスレッドも数える）。"""
        uid, n = os.getuid(), 0
        for pid in os.listdir("/proc"):
            if pid.isdigit():
                try:
                    if os.stat(f"/proc/{pid}").st_uid == uid:
                        n += len(os.listdir(f"/proc/{pid}/task"))
                except OSError:  # 走査の途中で終わったプロセス
                    continue
        return n

    _soft, _hard = resource.getrlimit(resource.RLIMIT_NPROC)
    _tasks = _user_tasks()
    _ceiling = _hard if _soft == resource.RLIM_INFINITY else _soft
    _limit = _tasks + max(500, (_ceiling - _tasks) // 2) if _ceiling != resource.RLIM_INFINITY else _tasks + 4096
    if os.getuid() != 0 and (_soft == resource.RLIM_INFINITY or _limit < _soft):  # root には効かないので掛けない
        resource.setrlimit(resource.RLIMIT_NPROC, (_limit, _hard))
