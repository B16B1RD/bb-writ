"""複数セッションの利用経路: 作業分離、保存、最新の検証済み版だけの統合。"""
import concurrent.futures
import json
import os
import shlex
import shutil
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent)); import process_limit  # noqa: E402,F401  試験に上限を掛ける（直に走らせても）
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
        self.env = {**os.environ, 'BB_WRIT_STATE_DIR': str(Path(self.tmp.name) / 'state')}
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
        subprocess.run(['git', 'init', '-q', '-b', 'main', '--separate-git-dir', str(Path(self.tmp.name) / 'sep.git'), str(at)], check=True)
        self.git(at, '-c', 'user.name=t', '-c', 'user.email=t', 'commit', '-q', '--allow-empty', '-m', 'i')
        got = self.w('task', 'start', '--dir', at, '--holder', 'x', code=6)
        self.assertIn('separate-git-dir', got['reason'])
        outer = Path(self.tmp.name) / 'outer'  # 管理ディレクトリが別のリポジトリの下にあっても、その親に作らない
        outer.mkdir()
        self.git(outer, 'init', '-q', '-b', 'main')
        self.git(outer, '-c', 'user.name=t', '-c', 'user.email=t', 'commit', '-q', '--allow-empty', '-m', 'i')
        inner = Path(self.tmp.name) / 'inner'
        subprocess.run(['git', 'init', '-q', '-b', 'main', '--separate-git-dir', str(outer / 'in.git'), str(inner)], check=True)
        self.git(inner, '-c', 'user.name=t', '-c', 'user.email=t', 'commit', '-q', '--allow-empty', '-m', 'i')
        self.w('task', 'start', '--dir', inner, '--holder', 'x', code=6)
        named = Path(self.tmp.name) / 'gd' / '.git'  # 管理ディレクトリの名前が .git でも、その親に作らない
        named.parent.mkdir()
        tree = Path(self.tmp.name) / 'tree'
        subprocess.run(['git', 'init', '-q', '-b', 'main', '--separate-git-dir', str(named), str(tree)], check=True)
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
        t0 = time.time()
        self.w('task', 'list', '--dir', self.repo)
        self.assertLess(time.time() - t0, 5)

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

    def test_a_failed_fast_forward_after_the_target_moved_leaves_no_record(self):
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
        p = subprocess.Popen([sys.executable, '-B', str(WORK), 'op', 'run', '--dir', self.repo, '--token', c['token'], '--key', 'local:slow',
                              '--', 'sleep', '20'], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(p.kill)
        rr = next((Path(self.tmp.name) / 'state' / 'work').glob('repo-*'))
        deadline = time.time() + 15
        while time.time() < deadline and not any(rr.glob('*.lock')) and not (rr / 'ops.jsonl').exists():
            time.sleep(0.05)
        time.sleep(0.5)
        lease = json.loads((rr / 'lease.json').read_text())
        lease['expires_at'] = 1
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

    def test_a_background_process_of_a_hook_does_not_keep_the_lock(self):
        a = self.start()
        head = self.change(a, 'a')
        hook = self.repo / '.git' / 'hooks' / 'post-merge'
        hook.write_text('#!/bin/sh\nsleep 8 >/dev/null 2>&1 &\n')
        hook.chmod(0o755)
        self.integrate(a, self.git(self.repo, 'rev-parse', 'main'), head)
        t0 = time.time()
        self.w('task', 'list', '--dir', self.repo)
        self.assertLess(time.time() - t0, 5)

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
        (Path(a['dir']) / '.env').write_text('試験用のローカル設定')
        self.w('task', 'cleanup', '--dir', a['dir'], '--token', a['token'], '--saved', 'main', code=6)
        self.assertEqual((Path(a['dir']) / '.env').read_text(), '試験用のローカル設定')

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
        time.sleep(0.15)
        self.assertIsNone(takeover.poll(), '期限切れだけで動作中の統合を引き継いだ')
        finish.touch()
        stdout, stderr = first.communicate(timeout=10)
        self.assertEqual(first.returncode, 0, stdout + stderr)
        stdout, stderr = takeover.communicate(timeout=10)
        self.assertEqual(takeover.returncode, 0, stdout + stderr)


if __name__ == '__main__':
    unittest.main()
