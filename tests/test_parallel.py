"""複数セッションの利用経路: 作業分離、保存、最新の検証済み版だけの統合。"""
import concurrent.futures
import json
import os
import shlex
import shutil
import signal
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent)); import process_limit  # noqa: E402,F401  テストに上限を掛ける（直に走らせても）
import tempfile
import time
import unittest

WORK = Path(__file__).resolve().parents[1] / 'scripts' / 'work.py'


class TestParallel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / 'repo'
        self.repo.mkdir()
        # 利用者の全体の Git 設定を読まない（全体の core.hooksPath があると、利用者の hook がテストの commit のたびに動いた）。
        # 切り離しそのものを確かめるテストは無い（外しても、全体の設定がテストの git の動きを変えない環境では落ちない）
        self.env = {**os.environ, 'BB_WRIT_STATE_DIR': str(Path(self.tmp.name) / 'state'), 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}
        self.git(self.repo, 'init', '-q', '-b', 'main')
        self.git(self.repo, 'config', 'user.name', 'Test')
        self.git(self.repo, 'config', 'user.email', 'test')
        (self.repo / 'base').write_text('base\n')
        self.git(self.repo, 'add', '.')
        self.git(self.repo, 'commit', '-qm', '初期状態')

    def git(self, at, *args):
        p = subprocess.run(['git', '-C', str(at), *args], env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.strip()

    def w(self, *args, code=0):
        p = subprocess.run([sys.executable, '-B', str(WORK), *map(str, args)], env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, code, p.stdout + p.stderr)
        return json.loads(p.stdout)

    def start(self, holder='Claude Code'):
        return self.w('task', 'start', '--dir', self.repo, '--holder', holder)

    def change(self, task, name):
        at = Path(task['dir'])
        (at / name).write_text(name + '\n')
        self.git(at, 'add', name)
        self.git(at, 'commit', '-qm', '変更')
        return self.git(at, 'rev-parse', 'HEAD')

    def integrate(self, task, expected, verified, code=0):
        return self.w('integrate', '--dir', task['dir'], '--token', task['token'],
                      '--expected', expected, '--verified', verified, code=code)

    def journal(self, **over):
        rr = next((Path(self.tmp.name) / 'state' / 'work').glob('repo-*'))
        (rr / 'integration.json').write_text(json.dumps({'dir': '/nonexistent/gone', 'expected': self.git(self.repo, 'rev-parse', 'main'),
                                                         'verified': '0' * 40, 'target': 'refs/heads/main', **over}))
        return rr / 'integration.json'

    def test_a_new_task_reports_the_tasks_nobody_closed(self):
        """片付け（task cleanup）を呼ばれなかったタスクは、次の task start と task list の出力の unclosed に出る。担当のいるタスクと、
        片付けたタスクは出ない。出すだけで、何も消さない。"""
        first = self.start()
        self.assertNotIn('unclosed', first)  # 無ければ欄も無い
        self.integrate(first, self.git(self.repo, 'rev-parse', 'main'), self.change(first, 'closed'))
        self.w('task', 'cleanup', '--dir', first['dir'], '--token', first['token'], '--saved', 'main')
        landed = self.start()
        self.integrate(landed, self.git(self.repo, 'rev-parse', 'main'), self.change(landed, 'landed'))
        squashed = self.start()
        self.change(squashed, 'squashed')
        self.git(self.repo, 'merge', '-q', '--squash', squashed['branch'])  # 統合先には、別の commit として入る
        self.git(self.repo, 'commit', '-qm', 'squash')
        unlanded = self.start()
        self.change(unlanded, 'unlanded')
        unused = self.start()
        for t in (landed, squashed, unlanded, unused):
            self.w('release', '--dir', t['dir'], '--token', t['token'])
        expired = self.start('x')
        self.change(expired, 'expired')
        self.expire(expired, 24 * 3600 + 120)  # 切れて間もないものは出ない（test_a_task_whose_owner_may_still_be_waiting_is_not_offered_for_closing）
        held = self.start()
        new = self.start()
        got = {t['task']: (t['lease'], t['commits'], t['landed']) for t in new['unclosed']}
        self.assertEqual(got, {landed['task']: ('released', 1, True), squashed['task']: ('released', 1, True),
                               unlanded['task']: ('released', 1, False), unused['task']: ('released', 0, True),
                               expired['task']: ('expired', 1, False)})
        self.assertEqual({t['task']: t['dir'] for t in new['unclosed']}[landed['task']], landed['dir'])
        listed = self.w('task', 'list', '--dir', self.repo)
        self.assertEqual({t['task'] for t in listed['unclosed']}, set(got))  # 始めたばかりの new と held は、担当がいるので出ない
        for t in (landed, squashed, unlanded, unused, expired, held, new):
            self.assertTrue(Path(t['dir']).is_dir(), t['task'])
        self.assertEqual(len(self.git(self.repo, 'branch', '--list', 'bb-writ/*').splitlines()), 7)

    def test_looking_for_unclosed_tasks_writes_nothing(self):
        """unclosed を調べるのは見るだけ。リポジトリの object も、作業場所も、状態の置き場も変えない（統合の照合は一時の置き場で行う）。"""
        diverged = self.start()
        self.change(diverged, 'side')
        self.w('release', '--dir', diverged['dir'], '--token', diverged['token'])
        (self.repo / 'main-only').write_text('x\n')  # 統合先が進んで、タスクと分かれる（照合が merge-tree を通る）
        self.git(self.repo, 'add', 'main-only')
        self.git(self.repo, 'commit', '-qm', '統合先の変更')
        record = Path(diverged['note']['path']).parent
        shutil.rmtree(record)  # 記録の置き場が無いタスク（見るだけなら、作り直さない）
        state = Path(self.tmp.name) / 'state'

        def snapshot():
            return (self.git(self.repo, 'count-objects', '-v'), self.git(self.repo, 'for-each-ref'), self.git(self.repo, 'worktree', 'list'),
                    sorted(str(x.relative_to(state)) for x in state.rglob('*') if 'worktrees' not in x.relative_to(state).parts))
        before = snapshot()
        listed = self.w('task', 'list', '--dir', self.repo)
        self.assertEqual([(t['task'], t['lease'], t['commits'], t['landed']) for t in listed['unclosed']], [(diverged['task'], 'released', 1, False)])
        self.assertEqual(snapshot(), before)
        self.assertFalse(record.exists())

    def test_a_broken_record_of_another_task_does_not_stop_a_new_task(self):
        """ほかのタスクの担当の記録（書く子が書き換えうる）が壊れていても、task start は新しいタスクの担当と token を返す。
        壊れたタスクは、確かめられないものとして unclosed に並ぶ。task list も、前のバージョンが通していた形（JSON として読める値）では止まらない。"""
        for name, lease, listable in (('深い入れ子', '[' * 100000 + ']' * 100000, False), ('float にできない期限', '{"token": "t", "expires_at": 1' + '0' * 400 + '}', True),
                                      ('JSON でない', '{', False), ('期限が文字列', '{"token": "t", "expires_at": "あした"}', True)):
            with self.subTest(name):
                other = self.start()
                self.change(other, 'f-' + other['task'])
                broken = Path(other['note']['path']).parent / 'lease.json'
                broken.write_text(lease)
                new = self.start()
                self.assertTrue(new['ok'] and new['token'], new)
                got = {t['task']: t for t in new['unclosed']}
                self.assertEqual((got[other['task']]['commits'], got[other['task']]['landed']), (1, False))
                if listable:
                    self.assertIn(other['task'], {t['task'] for t in self.w('task', 'list', '--dir', self.repo)['unclosed']})
                broken.unlink()  # 次の場面に持ち越さない
                self.w('release', '--dir', new['dir'], '--token', new['token'])
        with self.subTest('タスクの一覧の json が壊れている'):
            other = self.start()
            self.change(other, 'g-' + other['task'])
            self.w('release', '--dir', other['dir'], '--token', other['token'])
            registry = next((Path(self.tmp.name) / 'state' / 'work').glob('repo-*')) / 'tasks'
            (registry / (other['task'] + '.json')).write_text('{')
            new = self.start()
            self.assertTrue(new['ok'] and new['token'], new)
            got = {t['task']: t for t in new['unclosed']}
            self.assertEqual((got[other['task']]['lease'], got[other['task']]['commits'], got[other['task']]['landed']), ('released', None, None))

    def test_unclosed_tasks_are_found_by_the_registry_not_by_what_the_records_say(self):
        """並べるのは、タスクの一覧の識別子から決まる作業場所だけ（一覧の json の dir の値や、識別子の形でない名前のファイルは使わない）。
        landed は、checkout の HEAD でなく、そのタスクの統合先と比べる。"""
        self.git(self.repo, 'branch', 'release')
        to_release = self.w('task', 'start', '--dir', self.repo, '--holder', 'x', '--target', 'release')
        head = self.change(to_release, 'for-release')
        self.git(self.repo, 'merge', '-q', '--ff-only', head)  # checkout の HEAD（main）には入ったが、統合先（release）には入っていない
        self.w('release', '--dir', to_release['dir'], '--token', to_release['token'])
        registry = next((Path(self.tmp.name) / 'state' / 'work').glob('repo-*')) / 'tasks'
        elsewhere = Path(self.tmp.name) / 'elsewhere'
        elsewhere.mkdir()
        entry = registry / (to_release['task'] + '.json')
        entry.write_text(json.dumps({**json.loads(entry.read_text()), 'dir': str(elsewhere)}))
        (registry / 'not-a-task.json').write_text(entry.read_text())
        (Path(to_release['dir']).parent / 'not-a-task').mkdir()
        got = self.start()['unclosed']
        self.assertEqual([(t['task'], t['dir'], t['target'], t['landed']) for t in got], [(to_release['task'], to_release['dir'], 'release', False)])

    def test_a_task_in_a_submodule_works_in_the_submodule(self):
        sub = Path(self.tmp.name) / 'sub-origin'
        sub.mkdir()
        self.git(sub, 'init', '-q', '-b', 'main')
        self.git(sub, '-c', 'user.name=t', '-c', 'user.email=t', 'commit', '-q', '--allow-empty', '-m', 's')
        self.git(self.repo, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(sub), 'lib')
        self.git(self.repo, 'commit', '-qm', 'add')
        before = self.git(self.repo, 'branch', '--list')
        t = self.w('task', 'start', '--dir', self.repo / 'lib', '--holder', 'x')
        self.assertEqual(Path(t['checkout']).resolve(), (self.repo / 'lib').resolve())
        self.assertEqual(self.git(self.repo, 'branch', '--list'), before)  # 親に作らない
        self.assertIn(t['branch'], self.git(self.repo / 'lib', 'branch', '--list'))

    def test_a_task_with_an_initialised_submodule_is_kept_by_the_cleanup(self):
        """--force はサブモジュールの中の commit も消すので、片付けは止まって作業場所を残す。"""
        sub = Path(self.tmp.name) / 'sub-origin'
        sub.mkdir()
        self.git(sub, 'init', '-q', '-b', 'main')
        self.git(sub, '-c', 'user.name=t', '-c', 'user.email=t', 'commit', '-q', '--allow-empty', '-m', 's')
        self.git(self.repo, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(sub), 'lib')
        self.git(self.repo, 'commit', '-qm', 'add')
        t = self.start()
        at = Path(t['dir'])
        self.git(at, '-c', 'protocol.file.allow=always', 'submodule', 'update', '-q', '--init')
        self.w('task', 'cleanup', '--dir', t['dir'], '--token', t['token'], '--saved', 'main', code=6)
        self.assertTrue(at.exists())
        self.assertNotIn('cleanup', self.w('task', 'list', '--dir', self.repo)['tasks'][0])

    def test_a_stale_integration_record_of_a_gone_task_does_not_stop_others(self):
        a = self.start()
        head = self.change(a, 'a')
        self.journal()
        self.assertTrue(self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), head)['ok'])

    def test_a_landed_record_of_a_gone_task_is_cleared_for_another_task(self):
        a = self.start()
        head = self.change(a, 'a')
        landed = self.git(self.repo, 'rev-parse', 'main')
        self.journal(verified=landed)
        self.assertTrue(self.integrate(a, landed, head)['ok'])

    def test_a_separate_git_dir_is_refused_with_the_reason(self):
        at = Path(self.tmp.name) / 'sep'
        subprocess.run(['git', 'init', '-q', '-b', 'main', '--separate-git-dir', str(Path(self.tmp.name) / 'sep.git'), str(at)], env=self.env, check=True)
        self.git(at, '-c', 'user.name=t', '-c', 'user.email=t', 'commit', '-q', '--allow-empty', '-m', 'i')
        got = self.w('task', 'start', '--dir', at, '--holder', 'x', code=6)
        self.assertIn('separate-git-dir', got['reason'])
        self.git(at, 'worktree', 'add', '-q', '-b', 'linked', str(Path(self.tmp.name) / 'linked'))
        got = self.w('task', 'start', '--dir', Path(self.tmp.name) / 'linked', '--holder', 'x', code=6)
        self.assertIn('separate-git-dir', got['reason'])
        outer = Path(self.tmp.name) / 'outer'  # 管理ディレクトリが別のリポジトリの下にあっても、その親に作らない
        outer.mkdir()
        self.git(outer, 'init', '-q', '-b', 'main')
        self.git(outer, '-c', 'user.name=t', '-c', 'user.email=t', 'commit', '-q', '--allow-empty', '-m', 'i')
        inner = Path(self.tmp.name) / 'inner'
        subprocess.run(['git', 'init', '-q', '-b', 'main', '--separate-git-dir', str(outer / 'in.git'), str(inner)], env=self.env, check=True)
        self.git(inner, '-c', 'user.name=t', '-c', 'user.email=t', 'commit', '-q', '--allow-empty', '-m', 'i')
        self.w('task', 'start', '--dir', inner, '--holder', 'x', code=6)
        named = Path(self.tmp.name) / 'gd' / '.git'  # 管理ディレクトリの名前が .git でも、その親に作らない
        named.parent.mkdir()
        tree = Path(self.tmp.name) / 'tree'
        subprocess.run(['git', 'init', '-q', '-b', 'main', '--separate-git-dir', str(named), str(tree)], env=self.env, check=True)
        self.git(tree, '-c', 'user.name=t', '-c', 'user.email=t', 'commit', '-q', '--allow-empty', '-m', 'i')
        self.w('task', 'start', '--dir', tree, '--holder', 'x', code=6)

    def test_an_integration_record_waits_while_the_target_has_changes(self):
        a = self.start()
        head = self.change(a, 'a')
        self.journal()
        (self.repo / 'base').write_text('書きかけ\n')
        got = self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), head, code=6)
        self.assertIn('統合先', got['reason'])
        self.assertIsNotNone(self.w('task', 'list', '--dir', self.repo)['integration'])

    def test_a_landed_record_of_a_living_task_stops_others_until_it_recovers(self):
        a, b = self.start(), self.start()
        ah, bh = self.change(a, 'a'), self.change(b, 'b')
        landed = self.git(self.repo, 'rev-parse', 'main')
        self.journal(dir=str(Path(a['dir']).resolve()), verified=landed)
        self.integrate(b, landed, bh, code=6)
        self.assertIsNotNone(self.w('task', 'list', '--dir', self.repo)['integration'])
        self.git(self.repo, 'branch', 'kept', ah)
        self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'kept')  # 担当の片付けは閉じる
        self.assertIsNone(self.w('task', 'list', '--dir', self.repo)['integration'])

    def test_a_malformed_integration_record_is_refused_without_a_traceback(self):
        a = self.start()
        head = self.change(a, 'a')
        file = self.journal()
        whole = {'dir': str(Path(a['dir']).resolve()), 'verified': head, 'target': 'refs/heads/main'}
        for body in ('[]', '["x"]', '{"dir": "/"}', json.dumps(whole)):  # 最後は expected だけが欠ける
            with self.subTest(body):
                file.write_text(body)
                got = self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), head, code=6)
                self.assertIn('形が違う', got['reason'])

    def test_a_branch_that_moved_after_a_stopped_cleanup_is_kept(self):
        a = self.start()
        head = self.change(a, 'a')
        self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), head)
        reg = next(Path(self.tmp.name, 'state').rglob(a['task'] + '.json'))
        reg.write_text(json.dumps({**json.loads(reg.read_text()), 'cleanup': {'saved': 'refs/heads/main', 'commit': head}}))
        moved = self.change(a, 'later')
        self.git(self.repo, 'worktree', 'remove', a['dir'])
        self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'x', code=6)
        self.assertEqual(self.git(self.repo, 'rev-parse', a['branch']), moved)

    def test_a_task_json_naming_another_task_is_not_used(self):
        a = self.start()
        self.git(self.repo, 'branch', 'other')
        other = self.w('task', 'start', '--dir', self.repo, '--holder', 'x', '--target', 'other')
        wr = Path(a['note']['path']).parent
        record = json.loads((wr / 'task.json').read_text())
        (wr / 'task.json').write_text(json.dumps({**record, 'task': other['task']}))
        self.assertEqual(self.w('task', 'start', '--dir', a['dir'], '--holder', 'x')['target'], a['branch'])  # other を継がない

    def test_a_failing_hook_with_a_background_process_does_not_keep_the_lock(self):
        a = self.start()
        head = self.change(a, 'a')
        hook = self.repo / '.git' / 'hooks' / 'reference-transaction'
        hook.write_text('#!/bin/sh\nsleep 8 >/dev/null 2>&1 &\nexit 1\n')
        hook.chmod(0o755)
        self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), head, code=6)
        hook.unlink()
        t0 = time.monotonic()
        self.w('task', 'list', '--dir', self.repo)
        self.assertLess(time.monotonic() - t0, 5)

    def test_a_rebased_task_recovers_from_its_own_stale_record(self):
        a = self.start()
        head = self.change(a, 'a')
        self.journal(dir=str(Path(a['dir']).resolve()), verified=head)
        at = Path(a['dir'])
        self.git(at, '-c', 'user.name=t', '-c', 'user.email=t', 'commit', '-q', '--amend', '-m', '直した')
        new = self.git(at, 'rev-parse', 'HEAD')
        self.assertTrue(self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), new)['ok'])

    def test_a_record_of_this_task_does_not_stop_its_cleanup_when_nothing_landed(self):
        a = self.start()
        head = self.change(a, 'a')
        self.git(self.repo, 'branch', 'kept', head)
        self.journal(dir=str(Path(a['dir']).resolve()), verified=head)
        self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'kept')

    def test_a_failed_fast_forward_leaves_no_record(self):
        # ff を、ref を書く段の hook で失敗させる（統合先が別の commit へ進んで ff が拒まれたときと同じく、何も反映していない）
        a = self.start()
        head = self.change(a, 'a')
        expected = self.git(self.repo, 'rev-parse', 'main')
        hooks = self.repo / '.git' / 'hooks' / 'reference-transaction'
        hooks.write_text('#!/bin/sh\nexit 1\n')
        hooks.chmod(0o755)
        self.integrate(a, expected, head, code=6)
        hooks.unlink()
        self.assertEqual(self.w('task', 'list', '--dir', self.repo)['integration'], None)

    def test_integrate_waits_for_an_op_run_in_the_checkout(self):
        a = self.start()
        head = self.change(a, 'a')
        c = self.w('claim', '--dir', self.repo, '--holder', 'x')
        pidfile = Path(self.tmp.name) / 'slow.pid'
        p = subprocess.Popen([sys.executable, '-B', str(WORK), 'op', 'run', '--dir', self.repo, '--token', c['token'], '--key', 'local:slow',
                              '--', 'sh', '-c', f'echo $$ > {shlex.quote(str(pidfile))}; exec sleep 20'],
                             env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # op run を止めても、監督役（別のセッション）とコマンドは残る。コマンドを止め、監督役が終わるのを待ってから片付ける。
        # この片付けを外してもテストは落ちない（残さないことは確かめない）。pid のファイルが書かれる前にテストが落ちると、
        # コマンドを止められず、監督役は sleep 20 の終わりまで残る
        def stop():
            try:
                os.kill(int(pidfile.read_text()), signal.SIGTERM)
                p.wait(timeout=15)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                p.kill()
        self.addCleanup(stop)
        rr = next((Path(self.tmp.name) / 'state' / 'work').glob('repo-*'))
        deadline = time.time() + 15
        while time.time() < deadline and not any(rr.glob('*.lock')) and not (rr / 'ops.jsonl').exists():
            time.sleep(0.05)
        time.sleep(0.5)
        lease = json.loads((rr / 'lease.json').read_text())
        lease['expires_at'] = 1
        # 監督役は token が同じ担当の期限を 1 秒ごとに延ばす。延ばされて「生きた担当がいる」の拒否に変わらないように。
        # 時機の競争を避ける行で、外してもまず落ちない（外したことを捕まえるテストは無い）
        lease['token'] = 'x'
        (rr / 'lease.json').write_text(json.dumps(lease))
        got = self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), head, code=6)
        self.assertIn('op run', got['reason'])

    def test_a_task_started_below_a_worktree_keeps_the_target(self):
        a = self.start()
        sub = Path(a['dir']) / 'deep'
        sub.mkdir()
        b = self.w('task', 'start', '--dir', sub, '--holder', 'x')
        self.assertEqual(b['target'], 'main')

    def test_a_rewritten_target_in_the_work_record_is_not_used_by_task_start(self):
        a = self.start()
        self.git(self.repo, 'branch', 'other')
        wr = Path(a['note']['path']).parent
        record = json.loads((wr / 'task.json').read_text())
        (wr / 'task.json').write_text(json.dumps({**record, 'target': 'other'}))
        b = self.w('task', 'start', '--dir', a['dir'], '--holder', 'x')
        self.assertEqual(b['target'], 'main')
        (wr / 'task.json').write_text('[1]')
        self.w('task', 'start', '--dir', a['dir'], '--holder', 'x', '--target', 'main')

    def test_resume_finishes_a_cleanup_that_stopped_before_the_branch_was_deleted(self):
        a = self.start()
        head = self.change(a, 'a')
        self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), head)
        reg = next(Path(self.tmp.name, 'state').rglob(a['task'] + '.json'))
        reg.write_text(json.dumps({**json.loads(reg.read_text()), 'cleanup': {'saved': 'refs/heads/main', 'commit': head}}))
        self.git(self.repo, 'worktree', 'remove', a['dir'])
        got = self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'x')
        self.assertTrue(got['completed'], got)
        self.assertFalse(Path(a['dir']).exists())
        self.assertNotIn(a['branch'], self.git(self.repo, 'branch', '--list'))

    def test_a_refused_worktree_remove_leaves_the_task_as_it_was(self):
        a = self.start()
        head = self.change(a, 'a')
        self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), head)
        self.git(self.repo, 'worktree', 'lock', a['dir'])
        self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main', code=6)
        self.assertNotIn('cleanup', self.w('task', 'list', '--dir', self.repo)['tasks'][0])
        self.assertTrue(self.is_worktree_of_task(a), '作業場所が Git の作業ツリーでなくなった')
        self.git(self.repo, 'worktree', 'unlock', a['dir'])
        self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main')
        self.assertFalse(Path(a['dir']).exists())

    def registered(self, task):
        """task の作業場所が Git の worktree の一覧に在るか（作業場所が無くても引ける。道筋は symlink を解いて比べる）。"""
        d = Path(task['dir']).resolve()
        return d in [Path(x[len('worktree '):]).resolve() for x in self.git(self.repo, 'worktree', 'list', '--porcelain').splitlines()
                     if x.startswith('worktree ')]

    def assert_closed(self, task):
        """task が閉じている: 作業場所・ブランチ・タスクの記録・Git の登録・担当の記録が無い。"""
        self.assertFalse(Path(task['dir']).exists())
        self.assertEqual(self.git(self.repo, 'branch', '--list', task['branch']), '')
        self.assertEqual(self.w('task', 'list', '--dir', self.repo)['tasks'], [])
        self.assertFalse(self.registered(task))
        self.assertFalse((Path(task['note']['path']).parent / 'lease.json').exists())

    def test_cleanup_keeps_a_worktree_holding_a_directory_whose_items_it_cannot_remove(self):
        """git worktree remove は、作業場所の削除に失敗しても Git の登録を消す（戻す道具が無い）。中の項目を消せないディレクトリ
        （項目を持つのに、書き込みか中へ入る権限が無い）があると、作業場所が途中まで消えて登録だけが消え、タスクを閉じられなくなった（#77）。
        task cleanup は、削除を始める前に、その道筋を理由に出して拒み、作業場所の項目を 1 つも消さず、Git の登録とブランチを残し、
        片付けの途中の記録を書かない。権限を戻せば、同じ片付けで閉じる。"""
        self.skip_unless_permissions_hold()

        def tracked_many(d):
            for i in range(1, 9):
                (d / f'd{i}').mkdir()
                (d / f'd{i}' / 'f').write_text(f'{i}\n')
                (d / f'top{i}').write_text(f'{i}\n')

        def tracked_nested(d):
            (d / 'x' / 'y').mkdir(parents=True)
            (d / 'x' / 'y' / 'f').write_text('f\n')
        for name, tracked, untracked, locked in (
                ('a tracked directory that can be read but not written', lambda d: None, [], 'src'),
                ('an untracked read-only directory holding an empty directory', lambda d: None, ['u/s'], 'u'),
                ('one read-only tracked directory among many tracked directories and files', tracked_many, [], 'd5'),
                ('a read-only tracked directory below a writable one', tracked_nested, [], 'x/y'),
                ('the worktree itself is read-only', lambda d: None, [], '.')):
            with self.subTest(name):
                a = self.start()
                d = Path(a['dir'])
                (d / 'src').mkdir(exist_ok=True)  # 前の場面で統合済みなら、変更は無い
                (d / 'src' / 'f').write_text('f\n')
                tracked(d)
                self.git(d, 'add', '.')
                self.git(d, 'commit', '-qm', '追跡するディレクトリ', '--allow-empty')
                self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), self.git(d, 'rev-parse', 'HEAD'))
                for p in untracked:
                    (d / p).mkdir(parents=True)
                self.lock(d / locked, 0o555)
                before = sorted(map(str, d.rglob('*')))
                r = self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main', code=6)
                self.assertEqual(r['reason'], f'書き込めないディレクトリがある: {locked}。中の項目を消せないので、作業場所を保持する')
                self.assertEqual(sorted(map(str, d.rglob('*'))), before)
                self.assertTrue(self.is_worktree_of_task(a), '作業場所が Git の作業ツリーでなくなった')  # Git の登録もブランチも残る
                self.assertNotIn('cleanup', self.w('task', 'list', '--dir', self.repo)['tasks'][0])
                (d / locked).chmod(0o755)
                self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main')
                self.assert_closed(a)

    def test_cleanup_names_a_directory_whose_items_it_cannot_remove_whose_name_is_not_utf8(self):
        """中の項目を消せないディレクトリの名前が UTF-8 として読めなくても、UTF-8 の出力で落ちない形（読めないバイトは \\xe9）の道筋を
        理由に出して拒み、作業場所を Git の作業ツリーのまま残す。"""
        self.skip_unless_permissions_hold()
        a = self.start()
        d = Path(a['dir'])
        try:
            os.mkdir(os.path.join(os.fsencode(d), b'caf\xe9'))
        except OSError:
            self.skipTest('このファイルシステムは UTF-8 でない名前を作らせない')
        locked = d / os.fsdecode(b'caf\xe9')
        (locked / 'f').write_text('f\n')
        self.git(d, 'add', '.')
        self.git(d, 'commit', '-qm', 'UTF-8 でない名前')
        self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), self.git(d, 'rev-parse', 'HEAD'))
        self.lock(locked, 0o555)
        self.env['PYTHONIOENCODING'] = 'utf-8:strict'  # UTF-8 のロケールの出力と同じ（C のロケールの Python は読めないバイトをそのまま出す）
        r = self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main', code=6)
        self.assertEqual(r['reason'], '書き込めないディレクトリがある: caf\\xe9。中の項目を消せないので、作業場所を保持する')
        self.assertTrue(self.is_worktree_of_task(a), '作業場所が Git の作業ツリーでなくなった')

    def test_cleanup_still_removes_an_empty_read_only_directory_and_a_read_only_file(self):
        """空のディレクトリと読み取り専用のファイルは、親に書ければ消せるので、書き込めなくても片付けは通る。"""
        for name, committed, prepare, at, mode in (
                ('an empty read-only directory', 'with-dir', lambda d: (d / 'u').mkdir(), 'u', 0o555),
                ('a read-only tracked file', 'ro', lambda d: None, 'ro', 0o444)):
            with self.subTest(name):
                a = self.start()
                d = Path(a['dir'])
                self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), self.change(a, committed))
                prepare(d)
                self.lock(d / at, mode)
                self.assertEqual((d / at).stat().st_mode & 0o777, mode)
                self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main')
                self.assert_closed(a)

    def stopped_cleanup(self, mode=0o555, name='src', prepare=lambda d: None):
        """片付けの前の確かめの後に、作業場所の項目を消せなくなった片付け。prepare(作業場所) が足したものも一緒に追跡して統合する。
        PATH の先頭の偽の git が、git worktree remove の直前に、作業場所の追跡するディレクトリ name の権限を mode にしてから、
        本物（PATH を変える前に引いた絶対パス）を呼ぶ。
        git は作業場所を消している途中で止まり、Git の登録を消す。止まった task cleanup の出力と、タスク・保存した commit を返す。"""
        self.skip_unless_permissions_hold()
        a = self.start()
        d = Path(a['dir'])
        try:
            (d / name).mkdir()
        except OSError:
            try:
                name.encode('utf-8')
            except UnicodeEncodeError:
                self.skipTest('このファイルシステムは UTF-8 でない名前を作らせない')
            raise
        (d / name / 'f').write_text('f\n')
        prepare(d)
        self.git(d, 'add', '.')
        self.git(d, 'commit', '-qm', '追跡するディレクトリ')
        head = self.git(d, 'rev-parse', 'HEAD')
        self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), head)
        # 片付けの前は、手引きの status の見方で何も出ない（未保存の変更が無い）
        self.assertEqual(self.git(d, '-c', 'core.fsmonitor=false', '--no-optional-locks', 'status', '--porcelain', '--untracked-files=all',
                                  '--ignored=matching', '--ignore-submodules=dirty'), '')
        bin_dir = Path(self.tmp.name) / 'bin'
        bin_dir.mkdir()
        fake = bin_dir / 'git'
        fake.write_bytes(os.fsencode('#!/bin/sh\nfor last; do :; done\ncase " $* " in *" worktree remove "*) chmod ' + format(mode, 'o')
                                     + ' "$last"/' + shlex.quote(name) + ';; esac\nexec ' + shlex.quote(shutil.which('git')) + ' "$@"\n'))
        fake.chmod(0o755)
        self.addCleanup(lambda: (d / name).chmod(0o755) if os.path.lexists(d / name) else None)
        path = self.env['PATH']
        self.env['PATH'] = str(bin_dir) + os.pathsep + path
        r = self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main', code=6)
        self.env['PATH'] = path
        self.assertEqual((d / name).stat().st_mode & 0o777, mode, '偽の git が呼ばれていない')
        self.assertFalse(self.registered(a), 'git が Git の登録を残した（作りたい状態にならなかった）')
        self.assertIn('cleanup', self.w('task', 'list', '--dir', self.repo)['tasks'][0], '片付けの途中の記録が無い（作りたい状態にならなかった）')
        return a, head, r

    def test_a_cleanup_that_stopped_while_removing_the_worktree_keeps_its_record_for_task_resume(self):
        """git worktree remove が作業場所を消している途中で止まり、Git の登録だけが消えたら、片付けは始まっている。task cleanup は、
        片付けの途中の記録を消さずに残し、task resume で仕上げるよう理由に出す（記録を消して、task cleanup でも task resume でも
        閉じられなくなった。#77）。"""
        a, head, r = self.stopped_cleanup()
        self.assertTrue(r['reason'].startswith('作業場所を消している途中で止まった（'), r['reason'])
        self.assertIn(f"task resume --task {a['task']}", r['reason'])
        self.assertTrue((Path(a['dir']) / 'src' / 'f').exists())
        self.assertEqual(self.w('task', 'list', '--dir', self.repo)['tasks'][0]['cleanup'],
                         {'saved': 'refs/heads/main', 'commit': head, 'removal_stopped': True})

    def kept(self, task):
        """止まった片付けの後に残っているもの: 作業場所の残り（道筋ごとの種類・権限と中身）、状態の置き場の記録（片付けの途中の記録・
        担当・作業の記録。作業場所の置き場は残りとして見るので除く）、タスクのブランチの commit。"""
        def walk(top, skip=()):
            got = {}
            for at, dirs, files in os.walk(top):
                dirs[:] = [x for x in dirs if os.path.join(at, x) not in skip]
                for name in dirs + files:
                    f = os.path.join(at, name)
                    st = os.lstat(f)
                    got[f] = (st.st_mode, os.readlink(f) if os.path.islink(f) else None if os.path.isdir(f) else Path(f).read_bytes())
            return got
        state = self.env['BB_WRIT_STATE_DIR']
        return walk(task['dir']), walk(state, skip=(os.path.join(state, 'worktrees'),)), self.git(self.repo, 'rev-parse', task['branch'])

    def assert_cleanup_again_is_refused_and_removes_nothing(self, task, code, reason):
        """止まった片付けの後に、同じ task cleanup をもう一度呼ぶと、終了コード code と理由 reason で拒み、残り・状態の置き場の記録・
        ブランチを変えない。状態の置き場は、前に在ったものが消えも変わりもしないことだけを見る（記録の置き場が増えるのは許す）。"""
        self.assertTrue(os.path.lexists(Path(task['dir']) / 'src' / 'f'), '残りが無い（作りたい状態にならなかった）')
        tree, state, branch = self.kept(task)
        r = self.w('task', 'cleanup', '--dir', task['dir'], '--token', task['token'], '--saved', 'main', code=code)
        self.assertEqual(r, {'ok': False, 'reason': reason})
        tree2, state2, branch2 = self.kept(task)
        self.assertEqual(tree2, tree)
        self.assertEqual({k: v for k, v in state2.items() if k in state}, state)
        self.assertEqual(branch2, branch)

    def assert_cleanup_again_is_refused_whether_the_git_file_is_left_or_gone(self, gone_code, gone_reason):
        """止まった片付けの後に、同じ task cleanup をもう一度呼ぶと、残りに .git が残る形では「git のリポジトリでない: <作業場所>」
        （終了コード 2）で、.git を消した形では gone_code と gone_reason(作業場所) で拒み、どちらも何も変えない。消せない原因（書き込めない
        ディレクトリ src）を直せば、task resume で閉じる。.git が残るかは git の消す順しだいなので、git が残さなかった環境でも、片付けの前に
        在った .git と同じ中身を置いて、残る形から見る。"""
        before = []
        a, _, _ = self.stopped_cleanup(prepare=lambda d: before.append((d / '.git').read_bytes()))
        git_file = Path(a['dir']) / '.git'
        if not os.path.lexists(git_file):
            git_file.write_bytes(before[0])
        self.assert_cleanup_again_is_refused_and_removes_nothing(a, 2, f"git のリポジトリでない: {a['dir']}")
        git_file.unlink()
        self.assert_cleanup_again_is_refused_and_removes_nothing(a, gone_code, gone_reason(a['dir']))
        (Path(a['dir']) / 'src').chmod(0o755)
        got = self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'x')
        self.assertTrue(got.get('completed'), got)
        self.assert_closed(a)

    def test_task_cleanup_called_again_after_it_stopped_is_refused_and_removes_nothing(self):
        """片付けが作業場所を消している途中で止まった後に、同じ task cleanup をもう一度呼ぶと、残りが Git の作業ツリーでないので、
        残りに .git が残っていても消えていても「git のリポジトリでない: <作業場所>」（終了コード 2）で拒み、残り・状態の置き場の記録・
        ブランチを変えない。消せない原因を直せば、task resume で閉じる（playbook-parallel の「片付ける」が、この理由と終了コードを名指しする）。
        状態の置き場がどの Git リポジトリの外にも在る形なので、一時の置き場が Git リポジトリの中に在れば飛ばす。"""
        if subprocess.run(['git', '-C', self.tmp.name, 'rev-parse'], env=self.env, capture_output=True).returncode == 0:
            self.skipTest('一時の置き場が Git リポジトリの中に在り、状態の置き場をリポジトリの外に置けない')
        self.assert_cleanup_again_is_refused_whether_the_git_file_is_left_or_gone(2, lambda d: f"git のリポジトリでない: {d}")

    def test_task_cleanup_called_again_after_it_stopped_with_the_state_inside_another_repository_is_refused_and_removes_nothing(self):
        """状態の置き場が別の Git リポジトリの中に在る（ホームを Git で管理している、など）と、止まった後の同じ task cleanup は、残りに
        .git が残っていれば「git のリポジトリでない: <作業場所>」（終了コード 2）で、消えていれば、外側のリポジトリを見つけて「この場所は
        task start で作った作業場所ではない」（6）で拒む。どちらも、残り・状態の置き場の記録・ブランチを変えず、消せない原因を直せば、
        task resume で閉じる（playbook-parallel の「片付ける」が、この理由と終了コードを名指しする）。"""
        self.git(Path(self.tmp.name), 'init', '-q', '-b', 'outer')  # 状態の置き場と元の checkout を包む、別のリポジトリ
        self.assert_cleanup_again_is_refused_whether_the_git_file_is_left_or_gone(6, lambda d: 'この場所は task start で作った作業場所ではない')

    def test_task_resume_removes_what_a_stopped_cleanup_left_once_the_cause_is_fixed(self):
        """片付けの途中で、Git の登録が消えて作業場所の残りが在るとき、task resume は残りを消してから片付けを仕上げる。残りを消せなければ、
        最初に消せなかった項目で止まり、その項目の道筋を理由に出して拒み、消せないものと片付けの途中の記録とブランチを残す。
        原因を直せば、同じ task resume で閉じる。"""
        a, head, _ = self.stopped_cleanup()
        d = Path(a['dir'])
        r = self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'x', code=6)
        self.assertTrue(r['reason'].startswith('片付けの途中の残りを消せない: '), r['reason'])
        self.assertIn(str(d / 'src' / 'f') + ': ', r['reason'])  # 書き込めないディレクトリの中の、消せなかったファイル
        self.assertIn(f"もう一度 task resume --task {a['task']}", r['reason'])
        self.assertTrue((d / 'src' / 'f').exists())
        self.assertEqual(self.w('task', 'list', '--dir', self.repo)['tasks'][0]['cleanup'],
                         {'saved': 'refs/heads/main', 'commit': head, 'removal_stopped': True})
        self.assertEqual(self.git(self.repo, 'rev-parse', a['branch']), head)
        (d / 'src').chmod(0o755)
        got = self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'x')
        self.assertTrue(got.get('completed'), got)
        self.assert_closed(a)

    def test_task_resume_finishes_once_the_branch_can_be_deleted_after_it_removed_the_leftover(self):
        """task resume が片付けの途中の残りを消した後に、タスクのブランチの削除で拒まれても（元の checkout がそのブランチを
        checkout している）、片付けの途中の記録とブランチは残り、原因を直せば、同じ task resume で閉じる（残りがもう無いことで止まらない）。"""
        a, head, _ = self.stopped_cleanup()
        d = Path(a['dir'])
        (d / 'src').chmod(0o755)
        self.git(self.repo, 'checkout', '-q', a['branch'])  # git branch -D が拒む
        self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'x', code=6)
        self.assertFalse(os.path.lexists(d))  # 残りは消えている
        self.assertEqual(self.w('task', 'list', '--dir', self.repo)['tasks'][0]['cleanup'],
                         {'saved': 'refs/heads/main', 'commit': head, 'removal_stopped': True})
        self.assertEqual(self.git(self.repo, 'rev-parse', a['branch']), head)
        self.git(self.repo, 'checkout', '-q', 'main')
        got = self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'x')
        self.assertTrue(got.get('completed'), got)
        self.assert_closed(a)

    def test_task_resume_names_a_directory_it_cannot_open_in_what_a_stopped_cleanup_left(self):
        """片付けの途中の残りに、一覧できず中へも入れないディレクトリがあれば、task resume はその絶対の道筋を理由に出して拒む
        （記録の置き場を読み書きできない、の終了コード 2 にしない）。権限を戻せば、同じ task resume で閉じる。"""
        a, _, _ = self.stopped_cleanup(mode=0)
        r = self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'x', code=6)
        self.assertTrue(r['reason'].startswith('片付けの途中の残りを消せない: '), r['reason'])
        self.assertIn(os.path.join(a['dir'], 'src') + ': ', r['reason'])  # 開けなかったディレクトリそのもの
        (Path(a['dir']) / 'src').chmod(0o755)
        got = self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'x')
        self.assertTrue(got.get('completed'), got)
        self.assert_closed(a)

    def test_task_resume_names_a_directory_it_cannot_remove_whose_name_is_not_utf8(self):
        """片付けの途中の残りの、消せないディレクトリの名前が UTF-8 として読めなくても、UTF-8 の出力で落ちない形（読めないバイトは \\xe9）の
        消せなかった項目の絶対の道筋を理由に出して拒む。"""
        a, _, _ = self.stopped_cleanup(name=os.fsdecode(b'caf\xe9'))
        self.env['PYTHONIOENCODING'] = 'utf-8:strict'  # UTF-8 のロケールの出力と同じ
        r = self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'x', code=6)
        self.assertTrue(r['reason'].startswith('片付けの途中の残りを消せない: '), r['reason'])
        self.assertIn(os.path.join(a['dir'], 'caf\\xe9', 'f') + ': ', r['reason'])  # 書き込めないディレクトリの中の、消せなかったファイル

    def test_task_resume_keeps_a_registered_worktree_with_a_cleanup_mark_and_its_uncommitted_changes(self):
        """task resume が残りを消すのは、Git の登録が消えたときだけ。片付けの途中の記録があっても、登録が残る作業場所（片付けが記録を
        書いた直後に止められた形）は消さず、ふつうの再開として担当を返し、未 commit の変更・追跡外のファイル・ブランチ・登録を残す。
        登録が消えた残りの印（removal_stopped）の付いた場面は、ふつうには起きない（task cleanup がこの印を書くのは、登録が消えたのを
        見たときだけ）が、印があっても、登録の残る作業場所を消さないことを守る。"""
        for name, extra in (('a mark without removal_stopped', {}), ('a mark with removal_stopped', {'removal_stopped': True})):
            with self.subTest(name):
                a = self.start()
                committed = 'with-mark' if extra else 'without-mark'
                head = self.change(a, committed)
                self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), head)  # 照合は通る（保存先に入り、ブランチは記録の commit のまま）
                reg = next(Path(self.tmp.name, 'state').rglob(a['task'] + '.json'))
                mark = {'saved': 'refs/heads/main', 'commit': head, **extra}
                reg.write_text(json.dumps({**json.loads(reg.read_text()), 'cleanup': mark}))
                self.w('release', '--dir', a['dir'], '--token', a['token'])
                d = Path(a['dir'])
                (d / committed).write_text('書き換えた\n')
                (d / 'untracked').write_text('追跡外\n')
                self.assertTrue(self.registered(a))
                record = next(t for t in self.w('task', 'list', '--dir', self.repo)['tasks'] if t['task'] == a['task'])
                self.assertEqual(record['cleanup'], mark)
                p = subprocess.run([sys.executable, '-B', str(WORK), 'task', 'resume', '--dir', str(self.repo), '--task', a['task'], '--holder', 'x'],
                                   env=self.env, capture_output=True, text=True)
                # 消えたものを先に見る（終了コードより先に。消した後で別の理由で失敗しても、消したことを失敗の理由に出す）
                self.assertTrue((d / committed).exists() and (d / 'untracked').exists(), '登録の残る作業場所の中身を消した: ' + p.stdout + p.stderr)
                self.assertEqual((d / committed).read_text(), '書き換えた\n')
                self.assertEqual((d / 'untracked').read_text(), '追跡外\n')
                self.assertTrue(self.is_worktree_of_task(a), '作業場所が Git の作業ツリーでなくなった')  # Git の登録もブランチも残る
                self.assertEqual(self.git(self.repo, 'rev-parse', a['branch']), head)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                got = json.loads(p.stdout)
                self.assertNotIn('completed', got)  # ふつうの再開として担当を返す
                self.assertTrue(got.get('token'), got)

    def test_task_resume_keeps_an_unregistered_worktree_whose_cleanup_stopped_before_removing_anything(self):
        """片付けが git worktree remove を呼ぶ前に止まると（記録を書いた直後の Ctrl-C など）、片付けの途中の記録が残り、作業は続きうる。
        その後に Git の登録だけが消えても（作業場所が見えない間の git worktree prune）、task resume は作業場所を消さない。消すのは、
        task cleanup が、登録が消えた残りだと見て記録に書いたものだけ。この印しか無い作業場所は、今の理由で拒み、未 commit の変更・
        追跡外のファイル・印・ブランチを残す。"""
        a = self.start()
        head = self.change(a, 'a')
        self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), head)  # 照合は通る（保存先に入り、ブランチは記録の commit のまま）
        reg = next(Path(self.tmp.name, 'state').rglob(a['task'] + '.json'))
        reg.write_text(json.dumps({**json.loads(reg.read_text()), 'cleanup': {'saved': 'refs/heads/main', 'commit': head}}))
        self.w('release', '--dir', a['dir'], '--token', a['token'])
        d = Path(a['dir'])
        (d / 'a').write_text('書き換えた\n')
        (d / 'untracked').write_text('追跡外\n')
        away = d.with_name(d.name + '-away')
        d.rename(away)  # 作業場所が見えない間に、登録が消える
        self.git(self.repo, 'worktree', 'prune')
        away.rename(d)
        self.assertFalse(self.registered(a), 'Git の登録が残った（作りたい状態にならなかった）')
        self.assertEqual(self.w('task', 'list', '--dir', self.repo)['tasks'][0]['cleanup'], {'saved': 'refs/heads/main', 'commit': head})
        p = subprocess.run([sys.executable, '-B', str(WORK), 'task', 'resume', '--dir', str(self.repo), '--task', a['task'], '--holder', 'x'],
                           env=self.env, capture_output=True, text=True)
        # 消えたものを先に見る（終了コードより先に）
        self.assertTrue((d / 'a').exists() and (d / 'untracked').exists(), '片付けを始める前に止まった作業場所の中身を消した: ' + p.stdout + p.stderr)
        self.assertEqual((d / 'a').read_text(), '書き換えた\n')
        self.assertEqual((d / 'untracked').read_text(), '追跡外\n')
        self.assertEqual(self.git(self.repo, 'rev-parse', a['branch']), head)
        self.assertEqual(self.w('task', 'list', '--dir', self.repo)['tasks'][0]['cleanup'], {'saved': 'refs/heads/main', 'commit': head})
        self.assertEqual(p.returncode, 6, p.stdout + p.stderr)
        self.assertEqual(json.loads(p.stdout)['reason'], '作業場所の作成が未完了か、登録と実物が異なる。記録と git worktree list を照合する')

    def test_task_resume_keeps_an_unregistered_directory_without_a_cleanup_mark(self):
        """片付けの途中の記録が無ければ、Git の登録の無い作業場所のディレクトリを、task resume は消さず、今の理由で拒む。"""
        a = self.start()
        self.w('release', '--dir', a['dir'], '--token', a['token'])
        self.git(self.repo, 'worktree', 'remove', a['dir'])
        d = Path(a['dir'])
        d.mkdir()
        (d / 'data.txt').write_text('important\n')
        self.assertFalse(self.registered(a))
        self.assertNotIn('cleanup', self.w('task', 'list', '--dir', self.repo)['tasks'][0])
        r = self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'x', code=6)
        self.assertEqual(r['reason'], '作業場所の作成が未完了か、登録と実物が異なる。記録と git worktree list を照合する')
        self.assertEqual((d / 'data.txt').read_text(), 'important\n')

    def test_task_resume_does_not_follow_a_link_in_what_a_stopped_cleanup_left(self):
        """task resume は、片付けの途中の残りを消すとき、残りの中の symlink の先（作業場所の外）へ入らず、symlink だけを消す。
        外のディレクトリとその中のファイルは、中身ごと残る。"""
        outside = Path(self.tmp.name) / 'outside'  # 作業場所の外
        outside.mkdir()
        (outside / 'data.txt').write_text('important\n')
        a, _, _ = self.stopped_cleanup(prepare=lambda d: (d / 'link').symlink_to(outside))
        d = Path(a['dir'])
        if not os.path.lexists(d / 'link'):  # git が止まる前に消していたら（消す順は git と環境しだい）、残りに置き直す
            (d / 'link').symlink_to(outside)
        self.assertTrue(os.path.islink(d / 'link'), '残りの中に symlink が無い（作りたい状態にならなかった）')
        (d / 'src').chmod(0o755)
        p = subprocess.run([sys.executable, '-B', str(WORK), 'task', 'resume', '--dir', str(self.repo), '--task', a['task'], '--holder', 'x'],
                           env=self.env, capture_output=True, text=True)
        # 消えたものを先に見る（終了コードより先に。外を消した後で別の理由で失敗しても、外を消したことを失敗の理由に出す）
        self.assertTrue((outside / 'data.txt').exists(), '残りの中の symlink の先（作業場所の外）を消した: ' + p.stdout + p.stderr)
        self.assertEqual((outside / 'data.txt').read_text(), 'important\n')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        got = json.loads(p.stdout)
        self.assertTrue(got.get('completed'), got)
        self.assertFalse(os.path.lexists(d), '残りが消えていない')

    def assert_resume_keeps_what_a_stopped_cleanup_left(self, change):
        """片付けの途中で、Git の登録が消えて残りが在るタスクに change(タスク, 片付けの commit) を当て、task resume が change の返す
        照合の理由で拒み、残りを消さず、片付けの途中の記録を残すことを確かめる。残りは消せるものにして、拒否が照合によるものだと分かるようにする。"""
        a, head, _ = self.stopped_cleanup()
        d = Path(a['dir'])
        (d / 'src').chmod(0o755)
        why = change(a, head)
        before = sorted(map(str, d.rglob('*')))
        r = self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'x', code=6)
        self.assertEqual(r['reason'], why)
        self.assertEqual(sorted(map(str, d.rglob('*'))), before)
        self.assertIn('cleanup', self.w('task', 'list', '--dir', self.repo)['tasks'][0])

    def test_task_resume_keeps_what_a_stopped_cleanup_left_when_the_task_branch_moved(self):
        """片付けの途中の残りは、消すと戻せないので、task resume は保存先の照合を先に通す。タスクのブランチが片付けの commit から
        進んでいれば、今の照合の理由で拒み、残りを消さない。"""
        def moved(a, head):
            tree = self.git(self.repo, 'rev-parse', head + '^{tree}')
            later = self.git(self.repo, '-c', 'user.name=t', '-c', 'user.email=t', 'commit-tree', tree, '-p', head, '-m', '後の commit')
            self.git(self.repo, 'update-ref', 'refs/heads/' + a['branch'], later)
            return '片付けの途中の後にタスクのブランチが進んだ。記録と差分を照合する'
        self.assert_resume_keeps_what_a_stopped_cleanup_left(moved)

    def test_task_resume_keeps_what_a_stopped_cleanup_left_when_the_change_is_not_in_the_saved_place(self):
        """片付けの途中の記録の保存先に、片付けの commit が入っていなければ、task resume は今の照合の理由で拒み、残りを消さない。"""
        def unsaved(a, head):
            self.git(self.repo, 'branch', 'old', a['base'])
            reg = next(Path(self.tmp.name, 'state').rglob(a['task'] + '.json'))
            record = json.loads(reg.read_text())
            reg.write_text(json.dumps({**record, 'cleanup': {**record['cleanup'], 'saved': 'refs/heads/old'}}))  # 登録が消えた残りの印は残す
            return '片付けの途中の保存先 refs/heads/old に、このタスクの変更が入っていない。記録と差分を照合する'
        self.assert_resume_keeps_what_a_stopped_cleanup_left(unsaved)

    def test_a_background_process_of_a_hook_does_not_keep_the_lock(self):
        a = self.start()
        head = self.change(a, 'a')
        hook = self.repo / '.git' / 'hooks' / 'post-merge'
        hook.write_text('#!/bin/sh\nsleep 8 >/dev/null 2>&1 &\n')
        hook.chmod(0o755)
        self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), head)
        t0 = time.monotonic()
        self.w('task', 'list', '--dir', self.repo)
        self.assertLess(time.monotonic() - t0, 5)

    def test_task_start_with_a_tag_of_the_same_name_as_the_branch(self):
        self.git(self.repo, 'tag', 'main')
        self.assertEqual(self.start()['target'], 'main')

    def test_task_start_on_a_detached_head_says_so(self):
        self.git(self.repo, 'checkout', '-q', '--detach')
        got = self.w('task', 'start', '--dir', self.repo, '--holder', 'x', code=6)
        self.assertIn('--target', got['reason'])

    def test_a_mutex_link_does_not_truncate_another_file(self):
        a = self.start()
        wr = Path(a['note']['path']).parent
        victim = Path(self.tmp.name) / 'victim'
        victim.write_text('中身')
        (wr / '.mutex').unlink()
        (wr / '.mutex').symlink_to(victim)
        p = subprocess.run([sys.executable, '-B', str(WORK), 'claim', '--dir', a['dir'], '--holder', 'x'], env=self.env, capture_output=True, text=True)
        self.assertEqual(victim.read_text(), '中身')
        self.assertNotIn('Traceback', p.stderr)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)  # リンクを開かない

    def test_a_lease_expiry_written_as_text_is_refused_without_a_traceback(self):
        a = self.start()
        wr = Path(a['note']['path']).parent
        lease = json.loads((wr / 'lease.json').read_text())
        (wr / 'lease.json').write_text(json.dumps({**lease, 'expires_at': str(time.time() + 300)}))
        got = self.w('claim', '--dir', a['dir'], '--holder', 'x', code=3)
        self.assertGreater(got['expires_in_s'], 0)

    def test_a_relative_state_dir_stays_outside_the_checkout(self):
        self.env['BB_WRIT_STATE_DIR'] = 'rel-state'
        p = subprocess.run([sys.executable, '-B', str(WORK), 'task', 'start', '--dir', str(self.repo), '--holder', 'x'],
                           env=self.env, cwd=self.tmp.name, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.git(self.repo, 'status', '--short'), '')

    def test_a_target_not_merged_into_the_task_says_what_to_do(self):
        a = self.start()
        head = self.change(a, 'a')
        (self.repo / 'x').write_text('x')
        self.git(self.repo, 'add', 'x')
        self.git(self.repo, 'commit', '-qm', 'x')
        got = self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), head, code=6)
        self.assertIn('取り込', got['reason'])

    def test_simultaneous_same_host_and_mixed_hosts_have_separate_worktrees(self):
        (self.repo / 'local').write_text('手元の変更')
        for hosts in [('Claude Code', 'Claude Code'), ('Claude Code', 'Codex')]:
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                a, b = list(pool.map(self.start, hosts))
            self.assertNotEqual(a['task'], b['task'])
            self.assertNotEqual(a['dir'], b['dir'])
            self.assertNotEqual(a['branch'], b['branch'])
            self.assertFalse((Path(a['dir']) / 'local').exists())
            self.change(a, 'a')
            self.assertFalse((Path(b['dir']) / 'a').exists())
        self.assertEqual((self.repo / 'local').read_text(), '手元の変更')

    def test_resume_keeps_uncommitted_changes_and_refuses_live_holder(self):
        a = self.start()
        (Path(a['dir']) / 'unfinished').write_text('途中')
        self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'Codex', code=3)
        self.w('release', '--dir', a['dir'], '--token', a['token'])
        b = self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'Codex')
        self.assertEqual(a['dir'], b['dir'])
        self.assertNotEqual(a['token'], b['token'])
        self.assertEqual((Path(b['dir']) / 'unfinished').read_text(), '途中')
        tasks = self.w('task', 'list', '--dir', self.repo)['tasks']
        self.assertEqual([t['task'] for t in tasks], [a['task']])

    def test_task_list_does_not_expose_another_holders_token(self):
        a = self.start()
        task = self.w('task', 'list', '--dir', self.repo)['tasks'][0]
        self.assertEqual(task['lease']['holder'], 'Claude Code')
        self.assertNotIn('token', task['lease'])
        self.assertNotIn(a['token'], json.dumps(task))

    def test_resume_preserves_detached_work_and_reports_actual_branch(self):
        a = self.start()
        self.git(a['dir'], 'switch', '--detach')
        head = self.change(a, 'detached-work')
        (Path(a['dir']) / 'unfinished').write_text('途中')
        self.w('release', '--dir', a['dir'], '--token', a['token'])
        b = self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'Codex')
        self.assertIsNone(b['worktree_branch'])
        self.assertEqual(self.git(b['dir'], 'rev-parse', 'HEAD'), head)
        self.assertEqual((Path(b['dir']) / 'unfinished').read_text(), '途中')
        self.assertEqual((Path(b['dir']) / 'detached-work').read_text(), 'detached-work\n')

    def test_resume_restores_missing_worktree_without_losing_branch_commits(self):
        a = self.start()
        head = self.change(a, 'saved-on-branch')
        self.w('release', '--dir', a['dir'], '--token', a['token'])
        self.git(self.repo, 'worktree', 'remove', a['dir'])
        b = self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'Codex')
        self.assertEqual(b['dir'], a['dir'])
        self.assertEqual(self.git(b['dir'], 'rev-parse', 'HEAD'), head)
        self.assertEqual((Path(b['dir']) / 'saved-on-branch').read_text(), 'saved-on-branch\n')

    def op_after_note(self, task):
        """タスクの記録の本文を書き（最終更新は今の 1,000 秒前）、その後に op run で操作を 1 つ終える。並ぶはずの 1 行を返す。"""
        note = Path(task['note']['path'])
        note.write_text('# 作業の記録\n\n- 今の状態（ブランチ・commit・PR）: 返信は未投稿\n', encoding='utf-8')
        os.utime(note, (time.time() - 1000, time.time() - 1000))
        self.w('op', 'run', '--dir', task['dir'], '--token', task['token'], '--key', 'local:demo', '--what', 'demo', '--', 'true')
        rows = [json.loads(l) for l in (note.parent / 'ops.jsonl').read_text(encoding='utf-8').splitlines()]
        at = [e for e in rows if e['key'] == 'local:demo' and e['state'] == 'done'][-1]['at']
        return {'key': 'local:demo', 'what': 'demo', 'exit': 0, 'at': at}

    def note_written_after(self, task, row):
        """操作の後に次の一手の欄を書き足す（最終更新は done の at の 10 秒後。欄は、比べる今の状態・次の一手の中身が変わって消える）。"""
        note = Path(task['note']['path'])
        with open(note, 'a', encoding='utf-8') as fh:
            fh.write('- 次の一手: 片付ける\n')
        os.utime(note, (row['at'] + 10, row['at'] + 10))

    def test_resume_shows_the_ops_done_after_the_note(self):
        """記録の本文を書いた後に終えた操作は、担当を返した後の task resume の note.ops_after_note に出る（Issue #49）。
        本文を書き足した後は欄が無い。"""
        a = self.start()
        row = self.op_after_note(a)
        self.w('release', '--dir', a['dir'], '--token', a['token'])
        b = self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'Codex')
        self.assertEqual(b['note'].get('ops_after_note'), [row])
        self.note_written_after(a, row)
        self.w('release', '--dir', b['dir'], '--token', b['token'])
        c = self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'Codex')
        self.assertNotIn('ops_after_note', c['note'])

    def test_a_token_written_after_resume_keeps_the_ops_listed(self):
        """Issue #59 の再現手順: 引き受けた担当が「ホストと session ID」の欄に token を書き足しても（最終更新が done の後）、
        その後の status の note.ops_after_note にも操作が出る（task cleanup も同じ note を返す）。"""
        a = self.start()
        row = self.op_after_note(a)
        self.w('release', '--dir', a['dir'], '--token', a['token'])
        b = self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'Codex')
        self.assertEqual(b['note'].get('ops_after_note'), [row])
        note = Path(b['note']['path'])
        with open(note, 'a', encoding='utf-8') as fh:
            fh.write('- ホストと session ID: Codex ' + b['token'] + '\n')
        os.utime(note, (row['at'] + 10, row['at'] + 10))
        self.assertEqual(self.w('status', '--dir', b['dir'])['note'].get('ops_after_note'), [row])

    def test_cleanup_shows_the_ops_done_after_the_note(self):
        """片付けだけをするセッションも見るよう、task cleanup の note.ops_after_note にも、本文の後に終えた操作が出る。
        本文を書き足した後に片付けると、欄が無い。"""
        for rewritten in (False, True):
            with self.subTest(rewritten=rewritten):
                a = self.start()
                self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), self.change(a, f'a{rewritten}'))
                row = self.op_after_note(a)
                if rewritten:
                    self.note_written_after(a, row)
                done = self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main')
                self.assertFalse(Path(a['dir']).exists())
                if rewritten:
                    self.assertNotIn('ops_after_note', done['note'])
                else:
                    self.assertEqual(done['note'].get('ops_after_note'), [row])

    def test_resume_that_finishes_a_stopped_cleanup_shows_the_ops_and_is_not_stopped_by_a_broken_record(self):
        """片付けの途中（作業場所を消した後・タスクの記録を消す前）からの task resume は、片付けを終えた後に note を返す。
        本文の後に終えた操作は、その note.ops_after_note に出る。行の読めない ops.jsonl があっても、欄を出さずに 0 で
        completed を返す（欄は知らせで、関門でない。ここで止めると、片付けは済んだのに 1 で返り、やり直す道も無い）。"""
        for broken in (False, True):
            with self.subTest(broken=broken):
                a = self.start()
                head = self.change(a, f'a{broken}')
                self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), head)
                row = self.op_after_note(a)
                if broken:
                    with open(Path(a['note']['path']).parent / 'ops.jsonl', 'a', encoding='utf-8') as fh:
                        fh.write('{broken\n')
                reg = next(Path(self.tmp.name, 'state').rglob(a['task'] + '.json'))
                reg.write_text(json.dumps({**json.loads(reg.read_text()), 'cleanup': {'saved': 'refs/heads/main', 'commit': head}}))
                self.git(self.repo, 'worktree', 'remove', a['dir'])
                got = self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'x')
                self.assertTrue(got['completed'], got)
                self.assertFalse(Path(a['dir']).exists())
                if broken:
                    self.assertNotIn('ops_after_note', got['note'])
                else:
                    self.assertEqual(got['note'].get('ops_after_note'), [row])

    def test_an_unreadable_op_record_is_told_apart_from_a_lease_refusal(self):
        """権限で読めない記録は、担当の拒否（3）でなく、記録の置き場を読み書きできない（2）で返す（#60）。担当の拒否は 3 のまま。
        task cleanup・task resume の ops.jsonl と、integrate のタスクの記録を見る。権限が効かない環境（root など）では飛ばす。"""
        probe = Path(self.tmp.name) / 'probe'
        probe.write_text('x')
        probe.chmod(0)
        try:
            probe.read_text()
            self.skipTest('この環境では権限が効かない')
        except PermissionError:
            pass
        a = self.start()
        record = next(Path(self.env['BB_WRIT_STATE_DIR'], 'work').glob('repo-*')) / 'tasks' / (a['task'] + '.json')
        record.chmod(0)
        r = self.integrate(a, 'main', self.git(Path(a['dir']), 'rev-parse', 'HEAD'), code=2)
        self.assertIn('記録の置き場を読み書きできない', r['reason'])
        record.chmod(0o600)
        ops = next(Path(self.env['BB_WRIT_STATE_DIR'], 'work').glob('*/lease.json')).parent / 'ops.jsonl'
        ops.write_text('')
        ops.chmod(0)
        self.addCleanup(ops.chmod, 0o600)
        self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'Codex', code=3)  # 生きた担当がいる
        self.w('task', 'cleanup', '--dir', a['dir'], '--token', 'other', '--saved', 'main', code=3)  # 別の token
        r = self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main', code=2)
        self.assertIn('記録の置き場を読み書きできない', r['reason'])
        self.w('release', '--dir', a['dir'], '--token', a['token'])
        r = self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'Codex', code=2)
        self.assertIn('記録の置き場を読み書きできない', r['reason'])

    def test_resume_with_a_broken_op_record_answers_with_the_line(self):
        a = self.start()
        ops = next(Path(self.env['BB_WRIT_STATE_DIR'], 'work').glob('*/lease.json')).parent / 'ops.jsonl'
        self.w('release', '--dir', a['dir'], '--token', a['token'])
        ops.write_text('{"key": 1}\n')
        r = self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'Codex', code=1)  # 照合の拒否（6）でない
        self.assertIn('1 行目', r['reason'])

    def test_resume_refuses_a_rewritten_record(self):
        # 記録の dir・branch・base を書き換えても、そのまま git に渡さない（行き先は state の外、ブランチは別、-B のオプション注入）
        a = self.start()
        self.w('release', '--dir', a['dir'], '--token', a['token'])
        self.git(self.repo, 'worktree', 'remove', a['dir'])
        self.git(self.repo, 'branch', 'victim')
        record = next(Path(self.env['BB_WRIT_STATE_DIR'], 'work').glob('repo-*')) / 'tasks' / (a['task'] + '.json')
        outside = Path(self.tmp.name) / 'outside' / 'plugin'
        original = json.loads(record.read_text())
        for tampered in ({'dir': str(outside)}, {'dir': '-Bvictim'}, {'branch': 'victim'}, {'base': 'HEAD'}):
            record.write_text(json.dumps({**original, **tampered}))
            self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'Codex', code=6)
        self.assertFalse(outside.parent.exists())
        self.assertEqual(self.git(self.repo, 'worktree', 'list', '--porcelain').count('worktree '), 1)
        record.write_text(json.dumps(original))
        self.assertEqual(self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'Codex')['dir'], a['dir'])

    def test_rewritten_task_json_in_the_work_record_is_not_trusted(self):
        # 書く子は作業場所の記録に書けるので、そこの task.json の task・target を書き換えても、統合と片付けは親の一覧に従う
        a = self.start()
        head = self.change(a, 'feature')
        self.git(self.repo, 'branch', 'other')
        record = Path(self.w('status', '--dir', a['dir'])['note']['path']).parent / 'task.json'
        original = json.loads(record.read_text())
        record.write_text(json.dumps({**original, 'target': 'other'}))
        main = self.git(self.repo, 'rev-parse', 'main')
        self.integrate(a, main, head)
        self.assertEqual(self.git(self.repo, 'rev-parse', 'main'), head)
        self.assertEqual(self.git(self.repo, 'rev-parse', 'other'), main)
        outside = Path(self.tmp.name) / 'evil'
        record.write_text(json.dumps({**original, 'task': str(outside)}))
        self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main', code=6)
        self.assertFalse(Path(str(outside) + '.json').exists())

    def test_failed_start_without_artifacts_does_not_leave_a_task(self):
        real_git = shutil.which('git')
        bin_dir = Path(self.tmp.name) / 'bin'
        bin_dir.mkdir()
        fake = bin_dir / 'git'
        fake.write_text('#!/bin/sh\nif [ "$3" = worktree ] && [ "$4" = add ]; then exit 1; fi\nexec '
                        + shlex.quote(real_git) + ' "$@"\n')
        fake.chmod(0o755)
        self.env['PATH'] = str(bin_dir) + os.pathsep + os.environ['PATH']
        self.w('task', 'start', '--dir', self.repo, '--holder', 'Codex', code=6)
        self.assertEqual(self.w('task', 'list', '--dir', self.repo)['tasks'], [])

    def test_stale_target_requires_merge_and_reverification(self):
        a, b = self.start(), self.start('Codex')
        old = a['base']
        ah, bh = self.change(a, 'a'), self.change(b, 'b')
        self.integrate(a, old, ah)
        self.integrate(b, old, bh, code=6)
        self.assertFalse((self.repo / 'b').exists())
        self.git(b['dir'], 'merge', '--no-edit', 'main')
        new = self.git(b['dir'], 'rev-parse', 'HEAD')
        self.integrate(b, ah, bh, code=6)
        self.integrate(b, ah, new)
        self.assertTrue((self.repo / 'a').is_file())
        self.assertTrue((self.repo / 'b').is_file())

    def test_dirty_target_and_dirty_source_are_preserved(self):
        a = self.start()
        head = self.change(a, 'a')
        (self.repo / 'local').write_text('手元')
        self.integrate(a, a['base'], head, code=6)
        self.assertEqual((self.repo / 'local').read_text(), '手元')
        (self.repo / 'local').unlink()
        (Path(a['dir']) / 'unfinished').write_text('途中')
        self.integrate(a, a['base'], head, code=6)
        self.assertEqual((Path(a['dir']) / 'unfinished').read_text(), '途中')

    def test_integration_preserves_ignored_files_at_target(self):
        (self.repo / '.gitignore').write_text('local-config\n')
        self.git(self.repo, 'add', '.gitignore')
        self.git(self.repo, 'commit', '-qm', 'ローカル設定を除外')
        a = self.start()
        b = self.start('Codex')
        (self.repo / 'local-config').write_text('利用者の設定')
        (Path(a['dir']) / 'local-config').write_text('追跡する新しいファイル')
        self.git(a['dir'], 'add', '-f', 'local-config')
        self.git(a['dir'], 'commit', '-qm', '同名のファイルを追跡')
        head = self.git(a['dir'], 'rev-parse', 'HEAD')
        self.integrate(a, a['base'], head, code=6)
        self.assertEqual((self.repo / 'local-config').read_text(), '利用者の設定')
        self.assertEqual(self.git(self.repo, 'rev-parse', 'HEAD'), a['base'])
        self.assertIsNone(self.w('task', 'list', '--dir', self.repo)['integration'])
        # Git が何も変更せず拒否したので、別タスクの統合を妨げない。
        bh = self.change(b, 'safe')
        self.integrate(b, b['base'], bh)
        self.assertEqual((self.repo / 'local-config').read_text(), '利用者の設定')

    def test_cleanup_requires_saved_clean_changes_and_no_pending_ops(self):
        a = self.start()
        head = self.change(a, 'a')
        self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main', code=6)
        self.integrate(a, a['base'], head)
        self.w('op', 'begin', '--dir', a['dir'], '--token', a['token'], '--key', 'local:pending')
        self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main', code=6)
        self.w('op', 'done', '--dir', a['dir'], '--token', a['token'], '--key', 'local:pending', '--result', '照合済み')
        self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main')
        self.assertFalse(Path(a['dir']).exists())
        self.assertEqual(self.git(self.repo, 'show', 'main:a'), 'a')

    def test_cleanup_does_not_delete_commits_on_detached_task_branch(self):
        a = self.start()
        head = self.change(a, 'valuable')
        self.git(a['dir'], 'switch', '--detach', a['base'])
        self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main', code=6)
        self.assertEqual(self.git(self.repo, 'rev-parse', a['branch']), head)
        self.assertTrue(Path(a['dir']).is_dir())

    def test_cleanup_preserves_ignored_local_files(self):
        a = self.start()
        (Path(a['dir']) / '.gitignore').write_text('.env\n')
        self.git(a['dir'], 'add', '.gitignore')
        self.git(a['dir'], 'commit', '-qm', '設定を除外')
        head = self.git(a['dir'], 'rev-parse', 'HEAD')
        self.integrate(a, a['base'], head)
        (Path(a['dir']) / '.env').write_text('テスト用のローカル設定')
        self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main', code=6)
        self.assertEqual((Path(a['dir']) / '.env').read_text(), 'テスト用のローカル設定')

    def test_cleanup_does_not_count_the_claude_staging_dir(self):
        """Claude Code が設定を書くときの空の .claude/.cc-writes は、同じ場所の別のセッションも使う。task cleanup がそれを未保存の
        変更に数えたので launcher が消し、並行するセッションの設定の書き込みが拒まれた（#65）。空の .cc-writes だけの .claude と、
        .claude の中の空の .cc-writes は、無視されていてもいなくても数えない。"""
        a = self.start()
        d = Path(a['dir'])
        (d / '.gitignore').write_text('/.claude/\n/日本 語/.claude/\n/kept/.claude/.cc-writes/\n')  # 下の sub/.claude は無視しない
        (d / 'kept' / '.claude').mkdir(parents=True)
        (d / 'kept' / '.claude' / 'settings.json').write_text('{}')  # 追跡する設定の隣の置き場
        self.git(d, 'add', '.gitignore', 'kept')
        self.git(d, 'commit', '-qm', '置き場を除外')
        self.integrate(a, a['base'], self.git(d, 'rev-parse', 'HEAD'))
        for place in ('.claude', '日本 語/.claude', 'kept/.claude', 'sub/.claude'):
            (d / place / '.cc-writes').mkdir(parents=True)
        listing = self.git(d, 'status', '--porcelain', '--untracked-files=all', '--ignored=matching').splitlines()
        for line in ('!! .claude/', '!! "\\346\\227\\245\\346\\234\\254 \\350\\252\\236/.claude/"', '!! kept/.claude/.cc-writes/'):
            self.assertIn(line, listing)  # 無視するものとして見える
        self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main')
        self.assertFalse(d.exists())

    def test_cleanup_still_counts_other_ignored_places_that_hold_no_file(self):
        """数えないのは Claude Code の空の置き場だけで、ほかの無視するディレクトリは、ファイルを持たなくても数えて作業場所を元のまま残す。
        ファイルを持たないディレクトリをどれも数えずに通すと、書けない場所の下で git worktree remove が作業場所を途中まで消して止まり、
        タスクを閉じられなくなった。"""
        outside = Path(self.tmp.name) / 'outside'  # 作業場所の外の空のディレクトリ
        outside.mkdir()
        for name, ignore, dirs, files, locked in (
                ('empty', '/build/\n', ['build/a/b'], [], None),
                ('claude-alone', '/.claude/\n', ['.claude'], [], None),  # .cc-writes の無い空の .claude
                ('claude-other', '/.claude/\n', ['.claude/.cc-writes', '.claude/agents'], [], None),  # 置き場の隣のもの
                ('cc-nested', '/.claude/\n', ['.claude/.cc-writes/x'], [], None),  # 置き場の中のディレクトリ
                ('cc-file', '/.claude/\n', ['.claude/.cc-writes'], ['.claude/.cc-writes/settings.local.json.tmp'], None),
                ('cc-link', '/.claude/\n', ['.claude'], ['.claude/.cc-writes'], None),  # 外の空のディレクトリへの symlink
                ('cc-outside', '/plain/\n', ['plain/.cc-writes'], [], None),  # .claude の外の同じ名前
                ('locked-tree', '/ign/\n', ['ign/ro/x'], [], 'ign/ro'),  # 木の中のディレクトリが書けない
                ('locked-grandparent', '/top/mid/in/\n', ['top/mid/in'], [], 'top')):  # 無視するディレクトリの祖父が書けない
            with self.subTest(name):
                a = self.start()
                d = Path(a['dir'])
                (d / '.gitignore').write_text(ignore)
                self.git(d, 'add', '.gitignore')
                self.git(d, 'commit', '-qm', '置き場を除外', '--allow-empty')  # 前の場合と同じ除外のときは、変更が無い
                self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), self.git(d, 'rev-parse', 'HEAD'))
                for p in dirs:
                    (d / p).mkdir(parents=True)
                for p in files:
                    if name == 'cc-link':
                        (d / p).symlink_to(outside)
                    else:
                        (d / p).write_text('{}')
                if locked:
                    (d / locked).chmod(0o555)
                    self.addCleanup(lambda p=d / locked: p.exists() and p.chmod(0o755))
                before = sorted(map(str, d.rglob('*')))
                self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main', code=6)
                self.assertEqual(sorted(map(str, d.rglob('*'))), before)  # 作業場所の .git も残る

    def skip_unless_permissions_hold(self):
        probe = Path(self.tmp.name) / 'probe-dir'
        probe.mkdir()
        probe.chmod(0)
        try:
            os.listdir(probe)
            self.skipTest('この環境では権限が効かない')
        except PermissionError:
            pass
        finally:
            probe.chmod(0o755)

    def lock(self, path, mode):
        """path の権限を mode にし、テストの後で戻す（一時の置き場を消せるように）。"""
        path.chmod(mode)
        self.addCleanup(lambda: path.chmod(0o755) if os.path.lexists(path) else None)

    def is_worktree_of_task(self, task):
        d = Path(task['dir']).resolve()
        listed = [Path(x[len('worktree '):]).resolve() for x in self.git(self.repo, 'worktree', 'list', '--porcelain').splitlines()
                  if x.startswith('worktree ')]
        return d in listed and self.git(d, 'symbolic-ref', 'HEAD') == 'refs/heads/' + task['branch']

    def test_cleanup_keeps_a_worktree_holding_a_directory_it_cannot_open(self):
        """git status は、開けないディレクトリを出力に出さず飛ばす（標準エラーに警告を出すか、何も出さない）。task cleanup はそれを見逃し、
        git worktree remove が Git の登録を消してから削除に失敗して、タスクを閉じられなくなった（空なら中身を確かめずに消した。#68）。
        一覧できないか中へ入れないディレクトリは、空かどうかにかかわらず、その道筋を理由に出して拒み、作業場所を Git の作業ツリーの
        まま残す。権限を戻して中身を片付ければ、同じ片付けが通る。テストが作るのは自分の持つ場所だけ（ほかの利用者が持つ開けない場所を作るには
        root が要る）。"""
        self.skip_unless_permissions_hold()

        def remove(p):
            return lambda d: shutil.rmtree(d / p)
        for name, files, locked, mode, tidy in (
                ('data in a hidden directory nobody can open', ['.cache/data.txt'], '.cache', 0, remove('.cache')),
                ('an empty directory nobody can open', ['u/'], 'u', 0, remove('u')),
                ('data in a directory that can be entered but not listed', ['u/data.txt'], 'u', 0o333, remove('u')),
                ('data under a directory that can be listed but not entered', ['u/s/data.txt'], 'u', 0o444, remove('u')),
                ('data in a directory nobody can open under an untracked directory', ['t/u/data.txt'], 't/u', 0, remove('t')),
                ('data in a directory nobody can open under a tracked directory', ['src/u/data.txt'], 'src/u', 0, remove('src/u')),
                ('a tracked directory nobody can open', [], 'src', 0, lambda d: None),
                ('a change to a tracked file in a tracked directory that can be listed but not entered', ['src/keep'], 'src', 0o444,
                 lambda d: self.git(d, 'checkout', '--', 'src/keep')),
                ('data below a middle directory that can only be entered', ['x/y/z/data'], 'x/y', 0o111, remove('x')),
                ('data in a directory nobody can open next to the Claude Code staging dir', ['.claude/.cc-writes/', 'u/data.txt'], 'u', 0,
                 remove('u')),  # 置き場は status に「!! .claude/」と出るが、数えない
                ('the worktree itself can be entered but not listed', [], '.', 0o333, lambda d: None)):
            with self.subTest(name):
                a = self.start()
                d = Path(a['dir'])
                (d / 'src').mkdir(exist_ok=True)
                (d / 'src' / 'keep').write_text('keep\n')  # 追跡するディレクトリ（前の場合で統合済みなら、変更は無い）
                (d / '.gitignore').write_text('/.claude/\n')
                self.git(d, 'add', 'src', '.gitignore')
                self.git(d, 'commit', '-qm', '追跡するディレクトリ', '--allow-empty')
                self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), self.git(d, 'rev-parse', 'HEAD'))
                for p in files:
                    (d / p.rstrip('/')).parent.mkdir(parents=True, exist_ok=True)
                    if p.endswith('/'):
                        (d / p).mkdir()
                    else:
                        (d / p).write_text('important\n')
                before = sorted(map(str, d.rglob('*')))
                self.lock(d / locked, mode)
                r = self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main', code=6)
                self.assertEqual(r['reason'], f'開けないディレクトリがある: {locked}。中身を確かめられないので、作業場所を保持する')
                self.assertTrue(self.is_worktree_of_task(a), '作業場所が Git の作業ツリーでなくなった')  # Git の登録もブランチも残る
                (d / locked).chmod(0o755)
                self.assertEqual(sorted(map(str, d.rglob('*'))), before)
                tidy(d)
                self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main')
                self.assertFalse(d.exists())

    def test_cleanup_names_a_directory_it_cannot_open_inside_an_initialised_submodule(self):
        """片付けの status はサブモジュールの中の変更を見ず（--ignore-submodules=dirty）、git は何も出さない。それでも中の開けない
        ディレクトリを、その道筋を理由に出して拒み、作業場所を Git の作業ツリーのまま残す。"""
        self.skip_unless_permissions_hold()
        sub = Path(self.tmp.name) / 'sub-origin'
        sub.mkdir()
        self.git(sub, 'init', '-q', '-b', 'main')
        self.git(sub, '-c', 'user.name=t', '-c', 'user.email=t', 'commit', '-q', '--allow-empty', '-m', 's')
        self.git(self.repo, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(sub), 'lib')
        self.git(self.repo, 'commit', '-qm', 'add')
        t = self.start()
        at = Path(t['dir'])
        self.git(at, '-c', 'protocol.file.allow=always', 'submodule', 'update', '-q', '--init')
        (at / 'lib' / 'u').mkdir()
        (at / 'lib' / 'u' / 'data.txt').write_text('important\n')
        self.lock(at / 'lib' / 'u', 0)
        r = self.w('task', 'cleanup', '--dir', t['dir'], '--token', t['token'], '--saved', 'main', code=6)
        self.assertEqual(r['reason'], '開けないディレクトリがある: lib/u。中身を確かめられないので、作業場所を保持する')
        self.assertTrue(self.is_worktree_of_task(t), '作業場所が Git の作業ツリーでなくなった')

    def test_cleanup_names_a_directory_it_cannot_open_whose_name_is_not_utf8(self):
        """UTF-8 として読めない名前の開けないディレクトリでも、UTF-8 の出力で落ちない形（読めないバイトは \\xe9）の道筋を
        理由に出して拒み、作業場所を Git の作業ツリーのまま残す（読めないバイトのまま出すと、UTF-8 の出力で落ちた）。"""
        self.skip_unless_permissions_hold()
        a = self.start()
        d = Path(a['dir'])
        try:
            os.mkdir(os.path.join(os.fsencode(d), b'caf\xe9'))
        except OSError:
            self.skipTest('このファイルシステムは UTF-8 でない名前を作らせない')
        locked = d / os.fsdecode(b'caf\xe9')
        (locked / 'data.txt').write_text('important\n')
        self.lock(locked, 0)
        self.env['PYTHONIOENCODING'] = 'utf-8:strict'  # UTF-8 のロケールの出力と同じ（C のロケールの Python は読めないバイトをそのまま出す）
        r = self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main', code=6)
        self.assertEqual(r['reason'], '開けないディレクトリがある: caf\\xe9。中身を確かめられないので、作業場所を保持する')
        self.assertTrue(self.is_worktree_of_task(a), '作業場所が Git の作業ツリーでなくなった')

    def test_integrate_still_passes_with_directories_it_cannot_open(self):
        """統合はデータを消さないので、統合先の checkout と作業場所に開けないディレクトリ（root が持つ置き場など）があっても通る。"""
        self.skip_unless_permissions_hold()
        a = self.start()
        head = self.change(a, 'a')
        for at in (self.repo, Path(a['dir'])):
            (at / 'u').mkdir()
            (at / 'u' / 'data.txt').write_text('important\n')
            self.lock(at / 'u', 0)
        self.integrate(a, a['base'], head)
        self.assertEqual(self.git(self.repo, 'rev-parse', 'main'), head)
        self.assertEqual((self.repo / 'a').read_text(), 'a\n')

    def test_cleanup_does_not_follow_a_tracked_link_to_a_directory_it_cannot_open(self):
        """片付けは symlink の先へ入らない。追跡する symlink が作業場所の外の開けないディレクトリを指していても通り、先のものは残る。"""
        self.skip_unless_permissions_hold()
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir()
        (outside / 'data.txt').write_text('important\n')
        a = self.start()
        d = Path(a['dir'])
        (d / 'link').symlink_to(outside)
        self.git(d, 'add', 'link')
        self.git(d, 'commit', '-qm', '外を指す symlink')
        self.integrate(a, a['base'], self.git(d, 'rev-parse', 'HEAD'))
        self.lock(outside, 0)
        self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main')
        self.assertFalse(d.exists())
        outside.chmod(0o755)
        self.assertEqual((outside / 'data.txt').read_text(), 'important\n')

    HIDDEN = 'skip-worktree か assume-unchanged の付いた追跡ファイルに、status に出ない変更がある: {}。作業場所を保持する'

    def fresh_repo(self, name, before=None):
        """場面ごとの新しい元の checkout を作り、self.repo にする（設定と sparse-checkout は作業場所へ引き継がれ、場面の間で混ざるため）。
        追跡ファイルは a・b・src/f・other/o。before は task start の前に元の checkout へ当てる準備（設定・sparse-checkout・サブモジュール）。"""
        self.repo = Path(self.tmp.name) / name
        self.repo.mkdir()
        self.git(self.repo, 'init', '-q', '-b', 'main')
        self.git(self.repo, 'config', 'user.name', 'Test')
        self.git(self.repo, 'config', 'user.email', 'test')
        for p in ('a', 'b', 'src/f', 'other/o'):
            (self.repo / p).parent.mkdir(exist_ok=True)
            (self.repo / p).write_text(Path(p).name + '\n')
        self.git(self.repo, 'add', '.')
        self.git(self.repo, 'commit', '-qm', '初期状態')
        if before:
            before(self.repo)

    def handbook_status(self, d):
        """手引き（playbook-parallel の「片付ける」）が、何が残っているかを見るときの status。"""
        return self.git(d, '-c', 'core.fsmonitor=false', '--no-optional-locks', 'status', '--porcelain', '--untracked-files=all',
                        '--ignored=matching', '--ignore-submodules=dirty')

    def flag_of(self, d, path):
        """git ls-files -v の、path の行の先頭の字（H はフラグ無し、S は skip-worktree、h は assume-unchanged、s は両方）。"""
        return self.git(d, '-c', 'core.fsmonitor=false', 'ls-files', '-v', '--', path)[:1]

    def commit_and_integrate(self, a, name, content):
        d = Path(a['dir'])
        (d / name).write_text(content)
        self.git(d, 'add', name)
        self.git(d, 'commit', '-qm', '追跡するファイル')
        self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), self.git(d, 'rev-parse', 'HEAD'))

    def assert_cleanup_keeps_the_hidden_change(self, a, path, shown):
        """task cleanup が、隠れた変更のある path を、道筋 shown を入れた理由で拒み、作業場所・その変更・index のバイト列・フラグを元のまま残すこと。"""
        d = Path(a['dir'])
        self.assertEqual(self.handbook_status(d), '')  # 手引きの見方には出ない（準備が外れて、前からの理由で拒まれていない）
        at = os.path.join(os.fsencode(d), os.fsencode(path))

        def kept():  # 変更（中身・モード・symlink の先）
            if os.path.islink(at):
                return 'symlink', os.readlink(at)
            with open(at, 'rb') as f:
                return os.lstat(at).st_mode, f.read()
        index = Path(self.git(d, 'rev-parse', '--path-format=absolute', '--git-path', 'index'))
        index_before, flags_before, change = index.read_bytes(), self.git(d, '-c', 'core.fsmonitor=false', 'ls-files', '-v'), kept()
        r = self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main', code=6)
        self.assertEqual(r['reason'], self.HIDDEN.format(shown))
        self.assertTrue(self.is_worktree_of_task(a), '作業場所が Git の作業ツリーでなくなった')
        self.assertEqual(kept(), change, '隠れた変更が残っていない')
        self.assertTrue(index.read_bytes() == index_before, 'index のファイルが書き換わった')
        self.assertEqual(self.git(d, '-c', 'core.fsmonitor=false', 'ls-files', '-v'), flags_before)

    def test_cleanup_keeps_a_worktree_whose_flagged_tracked_file_holds_a_change_status_does_not_show(self):
        """git status は、skip-worktree か assume-unchanged の付いた追跡ファイルの変更を出さない。task cleanup はそれを見逃し、書き換えた
        ファイルごと作業場所を消した（#76）。git が付けたフラグでも同じことが起きた: core.ignoreStat=true（git が index を更新した項目に
        assume-unchanged を付ける。task start の作業場所は checkout したてなので、追跡ファイルの全部に付く）と、
        sparse.expectFilesOutsideOfPatterns=true の sparse-checkout（cone の外の実物に skip-worktree が残る）。
        フラグを外して見れば変更のあるファイルがあれば、その道筋を理由に出して拒み、作業場所・変更・index・フラグを元のまま残す。
        代表の場面（skip-worktree と assume-unchanged）では、変更を戻せば、同じ片付けが通る。"""
        def config(*pairs):
            return lambda r: [self.git(r, 'config', k, v) for k, v in pairs]

        def flag(*flags, then):
            def prepare(a, d):
                for f in flags:
                    self.git(d, 'update-index', f, 'a')
                then(d)
            return prepare

        def rewrite(d):
            (d / 'a').write_text('changed\n')

        def make_executable(d):
            p = subprocess.run(['git', '-C', str(d), 'config', '--bool', 'core.fileMode'], env=self.env, capture_output=True, text=True)
            if p.stdout.strip() == 'false':
                self.skipTest('この作業場所は core.fileMode が false で、実行ビットの変更を数えない')
            (d / 'a').chmod(0o755)

        def to_symlink(d):
            (d / 'a').unlink()
            (d / 'a').symlink_to('b')

        def to_dangling_symlink(d):
            (d / 'a').unlink()
            (d / 'a').symlink_to('missing')
            self.assertFalse(os.path.exists(d / 'a'))  # 指す先が無い

        def newline_name_rewrite(a, d):
            self.commit_and_integrate(a, 'x\ny', 'x\n')
            self.git(d, 'update-index', '--skip-worktree', 'x\ny')
            (d / 'x\ny').write_text('changed\n')

        def ignorestat_rewrite(a, d):
            self.commit_and_integrate(a, 'n', 'n\n')  # git が自分で書いたので、assume-unchanged が付く
            (d / 'n').write_text('changed\n')

        def outside_cone_rewrite(a, d):
            (d / 'other').mkdir()
            (d / 'other' / 'o').write_text('changed\n')
        sparse_expect = config(('sparse.expectFilesOutsideOfPatterns', 'true'))
        for name, before, prepare, path, tag, undo in (
                ('a change to a skip-worktree file', None, flag('--skip-worktree', then=rewrite), 'a', 'S', 'a\n'),
                ('a change to an assume-unchanged file', None, flag('--assume-unchanged', then=rewrite), 'a', 'h', 'a\n'),
                ('a change to a file with both flags', None, flag('--skip-worktree', '--assume-unchanged', then=rewrite), 'a', 's', None),
                ('a change to a skip-worktree file with a split index', config(('core.splitIndex', 'true')),
                 flag('--skip-worktree', then=rewrite), 'a', 'S', None),
                ('a change to a skip-worktree file with many files on', config(('feature.manyFiles', 'true')),
                 flag('--skip-worktree', then=rewrite), 'a', 'S', None),
                ('only the executable bit of an assume-unchanged file', None, flag('--assume-unchanged', then=make_executable), 'a', 'h', None),
                ('a skip-worktree file replaced by a symlink', None, flag('--skip-worktree', then=to_symlink), 'a', 'S', None),
                ('a skip-worktree file replaced by a symlink to nothing', None, flag('--skip-worktree', then=to_dangling_symlink), 'a', 'S', None),
                ('a change to a skip-worktree file whose name has a newline', None, newline_name_rewrite, 'x\ny', 'S', None),
                ('a change to a tracked file with core.ignoreStat on', config(('core.ignoreStat', 'true')), ignorestat_rewrite, 'n', 'h', None),
                ('a file outside the cone that differs from the index with files expected outside the patterns',
                 lambda r: (sparse_expect(r), self.git(r, 'sparse-checkout', 'set', 'src')), outside_cone_rewrite, 'other/o', 'S', None)):
            with self.subTest(name):
                self.fresh_repo('repo-' + name.replace(' ', '-'), before)
                a = self.start()
                d = Path(a['dir'])
                prepare(a, d)
                self.assertEqual(self.flag_of(d, path), tag)
                self.assert_cleanup_keeps_the_hidden_change(a, path, path)
                if undo is not None:
                    (d / path).write_text(undo)
                    self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main')
                    self.assertFalse(d.exists())

    def test_cleanup_names_a_flagged_tracked_file_whose_name_is_not_utf8(self):
        """UTF-8 として読めない名前の、フラグの付いた追跡ファイルに隠れた変更があれば、UTF-8 の出力で落ちない形（読めないバイトは \\xe9）の
        道筋を理由に出して拒み、作業場所・変更・index・フラグを元のまま残す。"""
        self.fresh_repo('repo-non-utf8')
        a = self.start()
        d = Path(a['dir'])
        name = os.fsdecode(b'caf\xe9')
        try:
            with open(os.path.join(os.fsencode(d), b'caf\xe9'), 'wb') as f:
                f.write(b'x\n')
        except OSError:
            self.skipTest('このファイルシステムは UTF-8 でない名前を作らせない')
        self.git(d, 'add', '.')
        self.git(d, 'commit', '-qm', 'UTF-8 でない名前')
        self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), self.git(d, 'rev-parse', 'HEAD'))
        self.git(d, 'update-index', '--assume-unchanged', name)
        with open(os.path.join(os.fsencode(d), b'caf\xe9'), 'wb') as f:
            f.write(b'changed\n')
        self.assertEqual(self.flag_of(d, name), 'h')
        self.env['PYTHONIOENCODING'] = 'utf-8:strict'  # UTF-8 のロケールの出力と同じ（C のロケールの Python は読めないバイトをそのまま出す）
        self.assert_cleanup_keeps_the_hidden_change(a, name, 'caf\\xe9')

    def test_cleanup_passes_when_flagged_tracked_files_hold_no_change(self):
        """フラグを外して見ても変更の無い作業場所（変更の無いフラグ付きのファイル、フラグ付きのファイルを消しただけ、未初期化のサブモジュールの
        項目、split index、core.ignoreStat=true、cone の外のファイルが無い sparse-checkout と sparse index、cone の外の実物が index と同じ
        sparse.expectFilesOutsideOfPatterns=true）は、失うものが無いので、フラグがあっても片付けが通る（フラグがあれば拒むと、
        core.ignoreStat=true のリポジトリで片付けが 1 度も通らなくなる）。"""
        def config(*pairs):
            return lambda r: [self.git(r, 'config', k, v) for k, v in pairs]

        def flag(f, then=lambda d: None):
            def prepare(a, d):
                self.git(d, 'update-index', f, 'a')
                then(d)
                return [('a', {'--skip-worktree': 'S', '--assume-unchanged': 'h'}[f])]
            return prepare

        def touch(d):
            t = os.stat(d / 'a').st_mtime + 10
            os.utime(d / 'a', (t, t))

        def submodule(r):
            sub = Path(self.tmp.name) / 'sub-origin'
            sub.mkdir()
            self.git(sub, 'init', '-q', '-b', 'main')
            self.git(sub, '-c', 'user.name=t', '-c', 'user.email=t', 'commit', '-q', '--allow-empty', '-m', 's')
            self.git(r, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(sub), 'lib')
            self.git(r, 'commit', '-qm', 'サブモジュール')

        def flag_submodule(a, d):
            self.assertTrue((d / 'lib').is_dir())  # 未初期化のサブモジュールは、空のディレクトリとして在る
            self.git(d, 'update-index', '--skip-worktree', 'lib')
            return [('lib', 'S')]

        def ignorestat(a, d):
            self.commit_and_integrate(a, 'n', 'n\n')
            return [('n', 'h'), ('a', 'h')]

        def outside_cone_absent(a, d):
            self.assertFalse(os.path.lexists(d / 'other'))
            return [('other/o', 'S')]

        def sparse_index(a, d):
            self.assertIn('other/', self.git(d, 'ls-files', '--sparse').splitlines())  # index が sparse のまま
            return outside_cone_absent(a, d)

        def outside_cone_same(a, d):
            (d / 'other').mkdir()
            (d / 'other' / 'o').write_text('o\n')
            return [('other/o', 'S')]
        cone = lambda r: self.git(r, 'sparse-checkout', 'set', 'src')  # noqa: E731
        for name, before, prepare in (
                ('an unchanged skip-worktree file', None, flag('--skip-worktree')),
                ('an assume-unchanged file only touched', None, flag('--assume-unchanged', then=touch)),
                ('an assume-unchanged file removed', None, flag('--assume-unchanged', then=lambda d: (d / 'a').unlink())),
                ('a skip-worktree entry of an uninitialised submodule', submodule, flag_submodule),
                ('an unchanged skip-worktree file with a split index', config(('core.splitIndex', 'true')), flag('--skip-worktree')),
                ('unchanged files with core.ignoreStat on', config(('core.ignoreStat', 'true')), ignorestat),
                ('a sparse checkout with nothing outside the cone', cone, outside_cone_absent),
                ('a sparse index with nothing outside the cone', lambda r: (cone(r), self.git(r, 'sparse-checkout', 'init', '--cone', '--sparse-index')),
                 sparse_index),
                ('a file outside the cone the same as the index with files expected outside the patterns',
                 lambda r: (config(('sparse.expectFilesOutsideOfPatterns', 'true'))(r), cone(r)), outside_cone_same)):
            with self.subTest(name):
                self.fresh_repo('repo-' + name.replace(' ', '-'), before)
                a = self.start()
                d = Path(a['dir'])
                for path, tag in prepare(a, d):
                    self.assertEqual(self.flag_of(d, path), tag, path)
                self.assertEqual(self.handbook_status(d), '')
                self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main')
                self.assertFalse(d.exists())

    def test_cleanup_refuses_a_flagged_file_replaced_by_a_directory_of_data_for_the_usual_reason(self):
        """フラグの付いた追跡ファイルを消して、同じ名前のディレクトリにデータを置いた作業場所は、status にそのデータが出る
        （?? a/data）ので、前からの理由（未保存の変更・未完了の操作がある）で拒む。"""
        self.fresh_repo('repo-dir-with-data')
        a = self.start()
        d = Path(a['dir'])
        self.git(d, 'update-index', '--skip-worktree', 'a')
        (d / 'a').unlink()
        (d / 'a').mkdir()
        (d / 'a' / 'data').write_text('important\n')
        self.assertEqual(self.flag_of(d, 'a'), 'S')
        self.assertEqual(self.handbook_status(d), '?? a/data')
        r = self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main', code=6)
        self.assertEqual(r['reason'], '未保存の変更・未完了の操作がある。作業場所を保持する')
        self.assertEqual((d / 'a' / 'data').read_text(), 'important\n')

    def test_integrate_still_passes_with_a_change_a_flag_hides(self):
        """統合はデータを消さないので、作業場所に skip-worktree で隠した変更があっても、前と同じく通る（片付けだけが拒む）。"""
        self.fresh_repo('repo-integrate')
        a = self.start()
        d = Path(a['dir'])
        head = self.change(a, 'c')
        self.git(d, 'update-index', '--skip-worktree', 'a')
        (d / 'a').write_text('changed\n')
        self.integrate(a, a['base'], head)
        self.assertEqual(self.git(self.repo, 'rev-parse', 'main'), head)
        self.assertEqual((d / 'a').read_text(), 'changed\n')

    def test_cleanup_does_not_run_the_index_hooks_while_it_checks_flagged_files(self):
        """片付けは、フラグを外して見るために index の写しを書くが、そのときに post-index-change の hook を走らせない（作業場所の中を指す
        core.hooksPath なら、書く子が置いたものが走る）。共有の hooks（元の checkout の .git/hooks）でも、作業場所の中の hooks でも、
        隠れた変更を拒む場面でも、通る場面でも同じ。フラグを付けた git update-index で hook が走ることを確かめ、印を空にしてから数える。"""
        mark = Path(self.tmp.name) / 'hook-ran'
        script = f'#!/bin/sh\necho ran >> {shlex.quote(str(mark))}\n'
        for where in ('common hooks', 'core.hooksPath inside the worktree'):
            for changed in (True, False):
                with self.subTest(where=where, changed=changed):
                    self.fresh_repo(f"repo-hook-{where.replace(' ', '-')}-{changed}")
                    a = self.start()
                    d = Path(a['dir'])
                    if where == 'common hooks':
                        hook = self.repo / '.git' / 'hooks' / 'post-index-change'
                        hook.write_text(script)
                        hook.chmod(0o755)
                    else:
                        (d / '.h').mkdir()
                        (d / '.h' / 'post-index-change').write_text(script)
                        (d / '.h' / 'post-index-change').chmod(0o755)
                        self.git(d, 'add', '.h')
                        self.git(d, 'commit', '-qm', 'hooks')
                        self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), self.git(d, 'rev-parse', 'HEAD'))
                        self.git(d, 'config', 'core.hooksPath', str(d / '.h'))
                    mark.write_text('')
                    self.git(d, 'update-index', '--skip-worktree', 'a')
                    self.assertNotEqual(mark.read_text(), '', 'hook が走る形になっていない')
                    if changed:
                        (d / 'a').write_text('changed\n')
                    self.assertEqual(self.handbook_status(d), '')
                    mark.write_text('')
                    self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main', code=6 if changed else 0)
                    self.assertEqual(mark.read_text(), '', '片付けの間に hook が走った')
                    self.assertEqual(d.exists(), changed)

    def stat_matching_rewrite(self, name, before, flags, tag, P=None):
        """場面ごとの新しい元の checkout（before の後に core.trustctime=false）で task start し、作業場所に追跡ファイル n を、更新時刻が
        P（ナノ秒。既定は 1000 秒前の秒の頭）のまま add・commit・統合して、flags を付け（flag_of が tag になる）、同じ大きさの別の中身に
        書き換えて、更新時刻を P に戻す（stat が index の覚えた値と同じまま、中身だけが違う）。タスクと作業場所と P を返す。"""
        def prepare(r):
            if before:
                before(r)
            self.git(r, 'config', 'core.trustctime', 'false')  # ctime は書き換えのたびに「いま」になり、戻せないので比べない
        self.fresh_repo(name, prepare)
        a = self.start()
        d = Path(a['dir'])
        at = d / 'n'
        if P is None:
            P = (int(time.time()) - 1000) * 10**9
        at.write_text('n\n')
        os.utime(at, ns=(P, P))  # add の前に（index が更新時刻 P を覚える）
        self.git(d, 'add', 'n')
        self.git(d, 'commit', '-qm', '追跡するファイル')
        self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), self.git(d, 'rev-parse', 'HEAD'))
        for f in flags:
            self.git(d, 'update-index', f, 'n')
        self.assertEqual(self.flag_of(d, 'n'), tag)
        at.write_text('X\n')  # 同じ大きさの別の中身
        os.utime(at, ns=(P, P))
        return a, d, P

    def test_cleanup_keeps_a_change_to_a_flagged_file_even_when_its_stat_matches_what_the_index_recorded(self):
        """フラグが付いている間、git はその項目の stat の記録を更新しないので、記録は当てにならない。フラグ付きのファイルを、stat が index の
        覚えた値と同じまま（同じ更新時刻・同じ大きさ）中身だけ書き換えると、フラグを外して stat で見ても変更なしに見え、片付けが作業場所ごと
        消した。素の git status が index を書き直した後（index の更新時刻だけが進み、同じ秒の項目を中身で比べる git の守りが外れる）も同じ。
        index が覚えた更新時刻の秒が 0 の項目（更新時刻 0 秒のファイルを add したもの）でも同じ。
        片付けは、フラグの付いた実物のあるファイルを、stat を信じずに中身で比べて拒む。core.trustctime=false は、テストを時計に依らなく
        するための設定（ctime は書き換えのたびに「いま」になり、戻せない）。更新時刻は待たずに、過去の秒に置いて作る。
        名前の形（改行の入った名前など）は、このテストの場面に置いていない。"""
        def config(k, v):
            return lambda r: self.git(r, 'config', k, v)
        for form, before, flags, tag in (
                ('a skip-worktree file', None, ['--skip-worktree'], 'S'),
                ('an assume-unchanged file', None, ['--assume-unchanged'], 'h'),
                ('a file with both flags', None, ['--skip-worktree', '--assume-unchanged'], 's'),
                ('a tracked file with core.ignoreStat on', config('core.ignoreStat', 'true'), [], 'h'),
                ('a skip-worktree file with a split index', config('core.splitIndex', 'true'), ['--skip-worktree'], 'S'),
                ('a skip-worktree file with many files on', config('feature.manyFiles', 'true'), ['--skip-worktree'], 'S')):
            for then in ('nothing', 'a plain git status rewrites the index'):
                with self.subTest(f'{form}, then {then}'):
                    a, d, P = self.stat_matching_rewrite(f"repo-stat-{form.replace(' ', '-')}-{then == 'nothing'}", before, flags, tag)
                    if then != 'nothing':
                        self.git(d, 'status')  # --no-optional-locks を付けない（index を書き直す）
                    self.assertEqual((os.stat(d / 'n').st_mtime_ns, os.stat(d / 'n').st_size), (P, len('n\n')),
                                     'ファイルの更新時刻か大きさが、index の覚えた値と違う')
                    self.assert_cleanup_keeps_the_hidden_change(a, 'n', 'n')
        for form, before, flags, tag in (
                ('a skip-worktree file', None, ['--skip-worktree'], 'S'),
                ('an assume-unchanged file', None, ['--assume-unchanged'], 'h'),
                ('a tracked file with core.ignoreStat on', config('core.ignoreStat', 'true'), [], 'h')):
            with self.subTest(f'{form} whose modification time the index recorded as 0 seconds, then nothing'):
                a, d, P = self.stat_matching_rewrite(f"repo-stat-zero-{form.replace(' ', '-')}", before, flags, tag, P=0)
                self.assertEqual((os.stat(d / 'n').st_mtime_ns, os.stat(d / 'n').st_size), (0, len('n\n')),
                                 'ファイルの更新時刻か大きさが、index の覚えた値と違う')
                self.assert_cleanup_keeps_the_hidden_change(a, 'n', 'n')

    def test_cleanup_still_ignores_a_change_git_does_not_see_in_a_file_with_no_flag(self):
        """フラグの無いファイルは、前と同じく、片付けの見方の status だけで判定する（守らない範囲: stat が index の覚えた値と同じまま中身だけ
        書き換えた、git が見ない形の変更は、フラグの無いファイルでは見ない）。フラグの付いたファイルを中身で比べるために、フラグの無い
        ファイルの扱いを変えない（フラグの無いファイルを、フラグの付いた追跡ファイルと名指しして拒まない）。"""
        self.fresh_repo('repo-unflagged', lambda r: self.git(r, 'config', 'core.trustctime', 'false'))
        a = self.start()
        d = Path(a['dir'])
        u = d / 'u'
        P = (int(time.time()) - 1000) * 10**9
        u.write_text('u\n')
        os.utime(u, ns=(P, P))
        self.git(d, 'add', 'u')
        self.git(d, 'commit', '-qm', '追跡するファイル')
        self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), self.git(d, 'rev-parse', 'HEAD'))
        self.git(d, 'update-index', '--skip-worktree', 'a')  # 変更の無い、フラグの付いたファイル
        u.write_text('X\n')
        os.utime(u, ns=(P, P))
        self.assertEqual((self.flag_of(d, 'u'), self.flag_of(d, 'a')), ('H', 'S'))
        self.assertEqual(self.handbook_status(d), '')
        self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main')
        self.assertFalse(d.exists())

    def test_cleanup_runs_no_filter_on_files_with_no_flag_while_it_checks_flagged_files(self):
        """片付けがフラグの付いたファイルを中身で比べる間、フラグの無い追跡ファイルの中身の比べ（clean フィルタ）を起こさない（フラグ付きの
        ファイルが 1 つあるだけで、フラグの無いファイルの全部にフィルタが走り、その required のフィルタが失敗すると片付けが拒まれた）。
        フラグ付きのファイルは、フィルタを通した中身で比べる。走ったファイルの名前は、フィルタが作業場所の外の記録に書く。index と同じ秒の
        項目は素の git status でも中身を比べるので、.bin は add の前に更新時刻を過去の秒に置く（待たずに毎回同じ結果にする）。"""
        self.fresh_repo('repo-filter-runs')
        a = self.start()
        d = Path(a['dir'])
        log = Path(self.tmp.name) / 'filter-ran'
        self.git(self.repo, 'config', 'filter.note.clean', f"""sh -c 'echo "$1" >> "$0"; cat' {shlex.quote(str(log))} %f""")
        self.git(self.repo, 'config', 'filter.note.smudge', 'cat')
        (d / '.gitattributes').write_text('*.bin filter=note\n')
        P = (int(time.time()) - 1000) * 10**9
        names = ('u0.bin', 'u1.bin', 'u2.bin', 'a.bin')
        for name in names:
            (d / name).write_text(name + '\n')
            os.utime(d / name, ns=(P, P))
        self.git(d, 'add', '.gitattributes', *names)
        self.git(d, 'commit', '-qm', 'フィルタを通すファイル')
        self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), self.git(d, 'rev-parse', 'HEAD'))
        self.git(d, 'update-index', '--skip-worktree', 'a.bin')
        ran = lambda: set(log.read_text().split()) if log.exists() else set()  # noqa: E731
        self.assertTrue(ran() >= set(names), f'add で、フィルタが .bin に走っていない（テストの準備の失敗）: {sorted(ran())}')
        log.write_text('')
        self.assertEqual(self.handbook_status(d), '')
        self.assertEqual(ran(), set(), '手引きの status の見方で、フィルタが走った（テストの準備の失敗）')
        self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main')
        self.assertFalse(d.exists())
        self.assertEqual(ran(), {'a.bin'}, f'片付けの間にフィルタが走ったファイル: {sorted(ran())}')

    def test_cleanup_keeps_a_flagged_file_whose_only_change_is_its_line_endings(self):
        """core.autocrlf=input で、フラグ付きのファイルの改行だけを LF から CRLF に書き換えた作業場所は、拒む。この書き換えは git add しても
        index が変わらないが、大きさが変わるので、フラグの無いファイルなら git status は変更と出す。フラグ付きのファイルも同じに扱う
        （stat の記録の無い項目として中身だけで比べると、index と同じ中身になって見逃す）。"""
        def config(*pairs):
            return lambda r: [self.git(r, 'config', k, v) for k, v in pairs]
        crlf = ('core.autocrlf', 'input')
        for form, before, flags, tag in (
                ('a skip-worktree file', config(crlf), ['--skip-worktree'], 'S'),
                ('an assume-unchanged file', config(crlf), ['--assume-unchanged'], 'h'),
                ('a file with both flags', config(crlf), ['--skip-worktree', '--assume-unchanged'], 's'),
                ('a tracked file with core.ignoreStat on', config(crlf, ('core.ignoreStat', 'true')), [], 'h')):
            with self.subTest(form):
                self.fresh_repo('repo-crlf-' + form.replace(' ', '-'), before)
                a = self.start()
                d = Path(a['dir'])
                (d / 'n').write_bytes(b'n\n')
                self.git(d, 'add', 'n')
                self.git(d, 'commit', '-qm', '追跡するファイル')
                self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), self.git(d, 'rev-parse', 'HEAD'))
                for f in flags:
                    self.git(d, 'update-index', f, 'n')
                self.assertEqual(self.flag_of(d, 'n'), tag)
                (d / 'n').write_bytes(b'n\r\n')
                self.assert_cleanup_keeps_the_hidden_change(a, 'n', 'n')

    def test_integrate_passes_with_a_change_a_flag_hides_in_the_checkout_it_lands_in(self):
        """統合先の checkout（元の checkout）に、フラグで隠した変更があっても、統合はデータを消さないので通り、隠した変更とフラグが残る
        （統合先は片付けの見方で見ない）。続く片付けも通る。"""
        for flag, tag in (('--skip-worktree', 'S'), ('--assume-unchanged', 'h')):
            with self.subTest(flag):
                self.fresh_repo('repo-integrate-into' + flag)
                self.git(self.repo, 'update-index', flag, 'b')
                (self.repo / 'b').write_text('hidden in the checkout\n')
                a = self.start()
                head = self.change(a, 'c')
                self.integrate(a, a['base'], head)
                self.assertEqual(self.git(self.repo, 'rev-parse', 'main'), head)
                self.assertEqual((self.repo / 'b').read_text(), 'hidden in the checkout\n')
                self.assertEqual(self.flag_of(self.repo, 'b'), tag)
                self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main')
                self.assertFalse(Path(a['dir']).exists())

    def test_cleanup_refuses_a_flagged_file_replaced_by_an_empty_directory(self):
        """フラグの付いた追跡ファイルを消して、同じ名前の空のディレクトリを置いた作業場所は、フラグを外した status に削除として出るので、
        その道筋を理由に出して拒む。ディレクトリを消せば（フラグの付いたファイルを消しただけになり）、同じ片付けが通る。"""
        self.fresh_repo('repo-empty-dir')
        a = self.start()
        d = Path(a['dir'])
        self.git(d, 'update-index', '--skip-worktree', 'a')
        (d / 'a').unlink()
        (d / 'a').mkdir()
        self.assertEqual(self.flag_of(d, 'a'), 'S')
        self.assertEqual(self.handbook_status(d), '')
        r = self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main', code=6)
        self.assertEqual(r['reason'], self.HIDDEN.format('a'))
        self.assertTrue(self.is_worktree_of_task(a), '作業場所が Git の作業ツリーでなくなった')
        (d / 'a').rmdir()
        self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main')
        self.assertFalse(d.exists())

    def test_cleanup_refuses_when_git_fails_while_it_checks_flagged_files(self):
        """フラグを外して見る git（ls-files、index の写しに当てる update-index、写しで比べる status）のどれかが失敗したら、隠れた変更を
        確かめられないので、片付けは git の文を理由に出して拒み、作業場所と変更を残す（失敗を通すと、変更を見つけないまま作業場所ごと消す）。
        PATH の先頭の偽の git が、狙った 1 つの呼び出しだけを失敗させ、印を残す（ほかは本物を呼ぶ）。比べる status は、GIT_INDEX_FILE の
        付いた status で見分ける（片付けの最初の status には付いていない）。場面の書き換えは、どれも 1 つ目の見方（フラグだけを外す）で
        決まる。2 つ目の見方（stat の記録の無い項目に入れ直す）は同じ呼び出しを通るので、2 つ目の見方だけの失敗の場面は置いていない。"""
        real = shlex.quote(shutil.which('git'))  # PATH を変える前に引く（偽の git が自分を呼び続けない）
        plain_env = dict(self.env)
        for sub, only_with_index_file in (('ls-files', False), ('update-index', False), ('status', True)):
            with self.subTest(failing=sub):
                self.fresh_repo('repo-failing-' + sub)
                a = self.start()
                d = Path(a['dir'])
                self.git(d, 'update-index', '--skip-worktree', 'a')
                (d / 'a').write_text('changed\n')
                self.assertEqual(self.flag_of(d, 'a'), 'S')
                bin_dir, mark = Path(self.tmp.name) / ('bin-' + sub), Path(self.tmp.name) / ('failed-' + sub)
                bin_dir.mkdir()
                line = f'偽の git が {sub} を失敗させた'
                hit = f'[ "$sub" = {sub} ]' + (' && [ -n "${GIT_INDEX_FILE+x}" ]' if only_with_index_file else '')
                (bin_dir / 'git').write_text(
                    '#!/bin/sh\n'
                    'sub= skip=\n'
                    'for x in "$@"; do\n'  # -C・-c の値を飛ばした、最初の語が副命令
                    '  if [ -n "$skip" ]; then skip=; continue; fi\n'
                    '  case "$x" in -C|-c) skip=1 ;; -*) ;; *) sub=$x; break ;; esac\n'
                    'done\n'
                    f'if {hit}; then echo {shlex.quote(line)} >&2; : >> {shlex.quote(str(mark))}; exit 3; fi\n'
                    f'exec {real} "$@"\n')
                (bin_dir / 'git').chmod(0o755)
                self.env = {**plain_env, 'PATH': str(bin_dir) + os.pathsep + plain_env['PATH']}
                try:
                    r = self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main', code=6)
                finally:
                    self.env = plain_env
                self.assertTrue(mark.exists(), f'偽の git が {sub} の呼び出しに当たっていない: {r}')
                self.assertIn(line, r['reason'])
                self.assertTrue(self.is_worktree_of_task(a), '作業場所が Git の作業ツリーでなくなった')
                self.assertEqual((d / 'a').read_text(), 'changed\n', '隠れた変更が残っていない')

    def test_new_task_does_not_implicitly_include_another_task(self):
        a = self.start()
        self.change(a, 'other-task')
        b = self.w('task', 'start', '--dir', a['dir'], '--holder', 'Codex')
        self.assertEqual(b['target'], 'main')
        self.assertEqual(b['base'], a['base'])
        self.assertFalse((Path(b['dir']) / 'other-task').exists())

    def test_cleanup_does_not_treat_stash_as_a_saved_branch(self):
        a = self.start()
        head = self.change(a, 'a')
        self.git(self.repo, 'update-ref', 'refs/stash', head)
        r = self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'refs/stash', code=6)
        self.assertIn('refs/stash', r['reason'])
        self.assertTrue(Path(a['dir']).exists())

    def test_cleanup_tells_a_remote_branch_apart_from_the_task_branch(self):
        a = self.start()
        head = self.change(a, 'a')
        self.integrate(a, a['base'], head)
        self.git(self.repo, 'update-ref', 'refs/remotes/origin/main', head)
        remote = self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'origin/main', code=6)
        self.assertIn('refs/remotes/origin/main', remote['reason'])
        own = self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', a['branch'], code=6)
        self.assertNotEqual(own['reason'], remote['reason'])
        self.assertTrue(Path(a['dir']).exists())

    def squash(self, task):
        self.git(self.repo, 'merge', '--squash', task['branch'])
        self.git(self.repo, 'commit', '-qm', 'squash')

    def test_cleanup_accepts_a_squash_merged_task_after_the_target_moves_on(self):
        a = self.start()
        self.change(a, 'a')
        self.change(a, 'b')
        self.squash(a)
        (self.repo / 'later').write_text('later\n')
        self.git(self.repo, 'add', 'later')
        self.git(self.repo, 'commit', '-qm', '後の変更')
        self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main')
        self.assertFalse(Path(a['dir']).exists())
        self.assertEqual(self.git(self.repo, 'branch', '--list', a['branch']), '')

    def test_cleanup_accepts_a_rebase_merged_task(self):
        a = self.start()
        self.change(a, 'a')
        self.change(a, 'b')
        (self.repo / 'earlier').write_text('earlier\n')
        self.git(self.repo, 'add', 'earlier')
        self.git(self.repo, 'commit', '-qm', '先の変更')
        self.git(self.repo, 'cherry-pick', a['base'] + '..' + a['branch'])
        self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main')
        self.assertFalse(Path(a['dir']).exists())

    def test_cleanup_refuses_a_squash_merged_task_with_later_commits(self):
        a = self.start()
        self.change(a, 'a')
        self.squash(a)
        head = self.change(a, 'not-merged')
        r = self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main', code=6)
        self.assertIn('入っていない', r['reason'])
        self.assertEqual(self.git(self.repo, 'rev-parse', a['branch']), head)
        self.assertTrue(Path(a['dir']).is_dir())

    def test_cleanup_refuses_a_squash_merged_task_reverted_at_the_target(self):
        a = self.start()
        self.change(a, 'a')
        self.squash(a)
        self.git(self.repo, 'revert', '--no-edit', 'HEAD')
        self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main', code=6)
        self.assertTrue(Path(a['dir']).is_dir())

    def test_resume_finishes_interrupted_cleanup_without_recreating_old_work(self):
        self.resume_interrupted_cleanup(lambda a, head: self.integrate(a, a['base'], head))

    def test_resume_finishes_interrupted_cleanup_of_a_squash_merged_task(self):
        self.resume_interrupted_cleanup(lambda a, head: self.squash(a))

    def resume_interrupted_cleanup(self, merge):
        a = self.start()
        head = self.change(a, 'saved')
        merge(a, head)
        real_git = shutil.which('git')
        bin_dir = Path(self.tmp.name) / 'bin'
        bin_dir.mkdir()
        fake = bin_dir / 'git'
        real = shlex.quote(real_git)
        fake.write_text('#!/bin/sh\nif [ "$3" = branch ] && [ "$4" = -D ]; then '
                        + real + ' "$@"; exit 1; fi\nexec ' + real + ' "$@"\n')
        fake.chmod(0o755)
        self.env['PATH'] = str(bin_dir) + os.pathsep + os.environ['PATH']
        self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main', code=6)
        resumed = self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'Codex')
        self.assertTrue(resumed['completed'])
        self.assertEqual(resumed['commit'], head)
        self.assertFalse(Path(a['dir']).exists())
        self.assertEqual(self.w('task', 'list', '--dir', self.repo)['tasks'], [])
        self.assertEqual((self.repo / 'saved').read_text(), 'saved\n')

    def hook(self):
        hooks = self.repo / '.git' / 'hooks'
        marker, finish = Path(self.tmp.name) / 'entered', Path(self.tmp.name) / 'finish'
        p = hooks / 'post-merge'
        p.write_text('#!' + sys.executable + '\nimport pathlib,time\n'
                     + f'pathlib.Path({str(marker)!r}).touch()\n'
                     + f'while not pathlib.Path({str(finish)!r}).exists(): time.sleep(0.02)\n')
        p.chmod(0o755)
        return marker, finish

    def integrating(self, a, head):
        p = subprocess.Popen([sys.executable, '-B', str(WORK), 'integrate', '--dir', a['dir'],
                              '--token', a['token'], '--expected', a['base'], '--verified', head],
                             env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop, p)
        return p

    def stop(self, p):
        (Path(self.tmp.name) / 'finish').touch()
        if p.poll() is None:
            p.kill()
        p.communicate(timeout=10)

    def await_file(self, path):
        until = time.monotonic() + 10
        while not path.exists() and time.monotonic() < until:
            time.sleep(0.02)
        self.assertTrue(path.exists(), str(path))

    def test_integration_holds_repository_lock_until_git_finishes_after_parent_kill(self):
        a, b = self.start(), self.start()
        ah, bh = self.change(a, 'a'), self.change(b, 'b')
        entered, finish = self.hook()
        self.addCleanup(finish.touch)
        first = self.integrating(a, ah)
        self.await_file(entered)
        second = self.integrating(b, bh)
        first.kill()  # 親だけが停止。git と hook はまだ動く。
        # 排他を子の git に渡さない退行は、この待ちの間に second が終わることでだけ見分ける（終わった後の rc と理由は同じになる）。
        # second の起動から終わりまでがこの待ちより遅い機械では見分けられない（誤って落ちることは無い）
        time.sleep(0.15)
        self.assertIsNone(second.poll(), 'git が動く間に別タスクが統合を追い越した')
        finish.touch()
        first.communicate(timeout=10)
        stdout, stderr = second.communicate(timeout=10)
        self.assertEqual(second.returncode, 6, stdout + stderr)
        self.assertIn('未完了', json.loads(stdout)['reason'])
        # 管理外の操作で統合先がさらに進んでも、反映済みの変更は照合できる。
        (self.repo / 'later').write_text('後続の変更')
        self.git(self.repo, 'add', 'later')
        self.git(self.repo, 'commit', '-qm', '後続の変更')
        later = self.git(self.repo, 'rev-parse', 'HEAD')
        self.integrate(a, a['base'], '0' * 40, code=6)
        self.integrate(a, '0' * 40, ah, code=6)
        # 前回の反映が完了したことを Git の実物から照合し、再実行しない。
        recovered = self.integrate(a, a['base'], ah)
        self.assertTrue(recovered['recovered'])
        self.assertEqual(recovered['target_commit'], later)
        self.git(b['dir'], 'merge', '--no-edit', 'main')
        new = self.git(b['dir'], 'rev-parse', 'HEAD')
        self.integrate(b, later, new)
        self.assertTrue((self.repo / 'a').exists())
        self.assertTrue((self.repo / 'b').exists())

    def expire(self, task, ago=120):
        f = Path(task['note']['path']).parent / 'lease.json'
        f.write_text(json.dumps({**json.loads(f.read_text()), 'expires_at': time.time() - ago}))

    def test_an_expired_lease_nobody_took_still_integrates_and_cleans_up(self):
        """期限が切れても、誰も取っていなければ、持ち主の integrate と task cleanup は通る（Issue #31。審査役を待つ間に切れ、
        integrate が 3 で拒否された）。"""
        for ago in (120, 24 * 3600 - 120):  # 切れて間もなくても、24 時間（担当の期限の上限）近く経っていても
            with self.subTest(ago=ago):
                a = self.start()
                head = self.change(a, f'a{ago}')
                self.expire(a, ago)
                self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), head)
                self.expire(a, ago)
                self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main')
                self.assertFalse(Path(a['dir']).exists())

    def test_a_task_whose_owner_may_still_be_waiting_is_not_offered_for_closing(self):
        """統合を終えた持ち主が子を待つ間に担当の期限が切れても、切れてから 24 時間（担当の期限の上限）を過ぎるまでは、
        ほかのセッションの task start・task list の unclosed に出さない（Issue #32。手引きどおり task resume → task cleanup で
        作業場所を消され、戻った持ち主の renew が 3 で拒否された）。過ぎたものは、止まった持ち主のタスクとして出る。
        期限の上限を超えた将来の期限（書く子が書き換えうる）は、前のとおり切れたものとして出る（待っているとみなして隠さない）。"""
        a = self.start()
        self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), self.change(a, 'a'))
        for ago, offered in ((120, False), (24 * 3600 - 120, False), (24 * 3600 + 120, True), (-(24 * 3600 + 120), True)):
            with self.subTest(ago=ago):
                self.expire(a, ago)
                b = self.start()
                listed = self.w('task', 'list', '--dir', self.repo)
                for got in (b.get('unclosed', []), listed.get('unclosed', [])):
                    self.assertEqual([(t['task'], t['lease'], t['commits'], t['landed']) for t in got if t['task'] == a['task']],
                                     [(a['task'], 'expired', 1, True)] if offered else [])
                self.w('task', 'cleanup', '--dir', b['dir'], '--token', b['token'], '--saved', 'main')
        self.w('renew', '--dir', a['dir'], '--token', a['token'])  # 誰も取っていないので、持ち主は続けられる
        self.assertTrue(Path(a['dir']).is_dir())

    def test_a_stale_list_does_not_take_the_task_of_an_owner_who_came_back(self):
        """閉じる側が、持ち主の担当が切れて 24 時間を過ぎたときに取った一覧を、持ち主が戻って延ばし、また子を待つ間に切れた後に使っても、
        task resume は担当を取らない（一覧と同じ 24 時間の段を、取る時に見る。tests/test_task_model.py の反例を実物で）。持ち主は
        続けられる。切れて 24 時間を過ぎれば、閉じる側は取って閉じられる。"""
        a = self.start()
        self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), self.change(a, 'a'))
        self.expire(a, 24 * 3600 + 120)
        stale = self.w('task', 'list', '--dir', self.repo)['unclosed']  # 閉じる側の一覧
        self.assertEqual([(t['task'], t['landed']) for t in stale], [(a['task'], True)])
        self.w('renew', '--dir', a['dir'], '--token', a['token'])  # 持ち主が戻り、また子を待つ
        for ago in (120, 24 * 3600 - 120):  # 切れて間もなくても、24 時間（担当の期限の上限）近く経っていても取らない
            with self.subTest(ago=ago):
                self.expire(a, ago)
                refused = self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'Codex', code=3)
                self.assertIn('--take-over', refused['reason'])
                self.assertTrue(Path(a['dir']).is_dir())
        self.w('renew', '--dir', a['dir'], '--token', a['token'])
        self.assertTrue(Path(a['dir']).is_dir())
        self.expire(a, 24 * 3600 + 120)
        b = self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'Codex')
        self.w('task', 'cleanup', '--dir', a['dir'], '--token', b['token'], '--saved', 'main')
        self.assertFalse(Path(a['dir']).exists())

    def test_a_session_that_took_over_a_recently_expired_task_can_close_it(self):
        """unclosed に出さないだけで、止まった持ち主の続きを task resume --take-over で引き受けたセッションは、期限が切れて
        間もなくても task cleanup で閉じられる（取り直した担当の前の期限で拒否しない）。"""
        a = self.start()
        self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), self.change(a, 'a'))
        self.expire(a)
        b = self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'Codex', '--take-over')
        self.w('task', 'cleanup', '--dir', a['dir'], '--token', b['token'], '--saved', 'main')
        self.assertFalse(Path(a['dir']).exists())

    def test_an_expired_lease_taken_by_another_stops_the_old_token(self):
        """期限の後に別の者が task resume --take-over で取ったら、元の token の integrate と task cleanup は 3 で拒否される。"""
        a = self.start()
        head = self.change(a, 'a')
        self.expire(a)
        self.w('task', 'resume', '--dir', self.repo, '--task', a['task'], '--holder', 'Codex', '--take-over')
        main = self.git(self.repo, 'rev-parse', 'main')
        self.integrate(a, main, head, code=3)
        self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main', code=3)
        self.assertEqual(self.git(self.repo, 'rev-parse', 'main'), main)
        self.assertTrue(Path(a['dir']).exists())

    def test_a_broken_lease_record_stops_integrate_and_cleanup_without_a_crash(self):
        """integrate と task cleanup も、形の違う担当の記録と空の token を、担当なしとして 3 で拒否する（落ちない）。"""
        a = self.start()
        head = self.change(a, 'a')
        f = Path(a['note']['path']).parent / 'lease.json'
        main = self.git(self.repo, 'rev-parse', 'main')
        for record, token in (([1], a['token']), ({'token': '', 'holder': 'x', 'expires_at': time.time() + 600}, '')):
            with self.subTest(record=record):
                f.write_text(json.dumps(record))
                self.w('integrate', '--dir', a['dir'], '--token', token, '--expected', main, '--verified', head, code=3)
                self.w('task', 'cleanup', '--dir', a['dir'], '--token', token, '--saved', 'main', code=3)
        self.assertEqual(self.git(self.repo, 'rev-parse', 'main'), main)
        self.assertTrue(Path(a['dir']).exists())

    def test_expired_lease_cannot_be_taken_over_during_integration(self):
        a = self.w('task', 'start', '--dir', self.repo, '--holder', 'Claude Code', '--lease', '0.05')
        ah = self.change(a, 'a')
        entered, finish = self.hook()
        self.addCleanup(finish.touch)
        first = self.integrating(a, ah)
        self.await_file(entered)
        # 期限は壁時計で判定する。WSL2 では壁時計が単調時計より数秒遅れることがあり、決まった長さの sleep では
        # 期限前に引き継ぎを試して落ちた（100 秒で 3.4 秒の遅れ、20 回中 2 回。2026-10-19）。壁時計で期限を過ぎるまで待つ
        expires_at = json.loads((Path(a['note']['path']).parent / 'lease.json').read_text())['expires_at']
        until = time.monotonic() + 30
        while time.time() < expires_at + 0.5 and time.monotonic() < until:
            time.sleep(0.05)
        self.assertGreater(time.time(), expires_at, '担当の期限が切れない')
        takeover = subprocess.Popen([sys.executable, '-B', str(WORK), 'claim', '--dir', a['dir'],
                                     '--holder', 'Codex'], env=self.env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop, takeover)
        # 統合が作業場所の排他を持たない退行は、この待ちの間に takeover が終わることでだけ見分ける。
        # takeover の起動から終わりまでがこの待ちより遅い機械では見分けられない（誤って落ちることは無い）
        time.sleep(0.15)
        self.assertIsNone(takeover.poll(), '期限切れだけで動作中の統合を引き継いだ')
        finish.touch()
        stdout, stderr = first.communicate(timeout=10)
        self.assertEqual(first.returncode, 0, stdout + stderr)
        stdout, stderr = takeover.communicate(timeout=10)
        self.assertEqual(takeover.returncode, 0, stdout + stderr)


if __name__ == '__main__':
    unittest.main()
