"""試験の包み。試験のプロセスとその子に、利用者の今のプロセス数 +500 の上限を掛ける（Linux）。

試験が書いた偽の git が PATH で自分自身を呼び続け、利用者全体のプロセスの上限（ulimit -u）を埋めて、同時に動いていた
Claude Code を落とした（2026-10-23）。上限は利用者ごとの数で数えるので、試験の側だけを低くすれば、試験が同じ誤りをしても
増えるのは試験の分だけで止まり、ほかのプロセスには元の上限までの余裕が残る。
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
    _limit = _user_tasks() + 500
    if os.getuid() != 0 and (_soft == resource.RLIM_INFINITY or _limit < _soft):  # root には効かないので掛けない
        resource.setrlimit(resource.RLIMIT_NPROC, (_limit, _hard))
