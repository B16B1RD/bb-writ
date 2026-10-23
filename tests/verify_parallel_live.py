#!/usr/bin/env python3
"""専用ログインの実 CLI で並行開発を確認する。CI の単体試験には含めない。

実行: python3 -B tests/verify_parallel_live.py --out <証拠のディレクトリ>
作成する試験用 Git リポジトリでのみ commit し、外部には push しない。
"""
import argparse
import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bb_launch', ROOT / 'scripts/launch.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def run(cmd, env=None, at=None):
    p = subprocess.run(list(map(str, cmd)), cwd=at, env=env, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(p.stdout + p.stderr)
    return p.stdout.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', type=Path)
    ap.add_argument('--check', action='store_true')
    args = ap.parse_args()
    if args.check:
        cfg = launcher.config_dir()
        print(json.dumps({'python': sys.version.split()[0],
                          'tools': {n: bool(shutil.which(n)) for n in ('git', 'claude', 'codex', 'dotenvx')},
                          'login_files': {n: (cfg / n).is_file() for n in ('.env', '.env.keys', 'codex-home/auth.json')}}, indent=2))
        return
    if args.out is None:
        ap.error('--out が必要（点検だけなら --check）')
    evidence = args.out.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    # launcher 自身にも同じ fixture の状態ディレクトリを使わせる。
    os.environ['BB_WRIT_STATE_DIR'] = str(evidence / 'state')
    env = dict(os.environ)
    repo = evidence / 'repo'
    repo.mkdir()
    run(['git', 'init', '-q', '-b', 'main', repo])
    # この fixture の実効名義を、最初の commit より前に固定する。
    run(['git', '-C', repo, 'config', 'user.name', 'Parallel verification'])
    run(['git', '-C', repo, 'config', 'user.email', 'verification'])
    (repo / 'README.md').write_text('並行開発の使い捨て試験リポジトリ。外部操作なし。\n')
    run(['git', '-C', repo, 'add', 'README.md'])
    run(['git', '-C', repo, 'commit', '-qm', 'test: 並行検証の初期状態'])
    results = []

    def work(*words):
        return json.loads(run([sys.executable, '-B', ROOT / 'scripts/work.py', *words], env=env))

    def session(host, name):
        instructions = f'''この試験では開発中の bb-writ を使う。スキルの正本は {ROOT}/skills/ 以下。キャッシュの旧版を使わない。
bb-writ の入口と playbook-feature、playbook-parallel を読み、次の依頼を実行する。
「{name}.txt を作り、内容を {name} の一行にする」。
独立した新規タスクとして作業場所を自動確保し、その中で変更・検証・commit する。
work.py は {ROOT}/scripts/work.py。元の checkout は {repo}。
この試験の状態は BB_WRIT_STATE_DIR={evidence / 'state'} にある。同じ値を全 work.py 呼出しに渡す。
他のセッションも別タスクを実行している。元の checkout には書かない。
試験終了時点では統合と片付けは行わず、作業場所・ブランチ・検証した commit を報告して担当を release する。
委任と外部操作は禁止。既存の利用者設定、認証情報、他リポジトリは読まない。
最後に JSON コードブロックで conclusion、evidence（what と where）、unknowns を報告する。
'''
        if host == 'claude':
            instructions = instructions[instructions.index('「' ):]
            prompt = '/bb-writ:bb-writ ' + instructions
        else:
            prompt = instructions
        a = SimpleNamespace(host=host, write=True, cwd=repo,
                            model='sonnet' if host == 'claude' else 'gpt-6-sol', effort='medium')
        cmd, child_env, why = launcher.command(a, prompt, evidence / 'scratch')  # 読み取り専用の Codex にだけ使う一時ディレクトリ
        if why:
            raise RuntimeError(why)
        # このセッションは子ではなく利用者の親セッションの代わり（自分で task start・commit する）なので、子に掛ける制限
        # （作業場所の記録だけの付与、Claude Code の --restricted と sandbox）を外し、状態ディレクトリ全体を渡す。
        cmd[cmd.index('--add-dir') + 1] = str(evidence / 'state')
        for flag in ('--restricted',):
            if flag in cmd:
                cmd.remove(flag)
        for flag in ('--tools', '--settings'):
            if flag in cmd:
                del cmd[cmd.index(flag):cmd.index(flag) + 2]
        child_env['BB_WRIT_STATE_DIR'] = str(evidence / 'state')
        if host == 'claude':
            cmd[cmd.index('--output-format') + 1] = 'stream-json'
            cmd.extend(['--verbose', '--max-turns', '35'])
        p = subprocess.Popen(cmd, cwd=repo, env=child_env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            stdout, stderr = p.communicate(timeout=600)
        except BaseException:
            os.killpg(p.pid, signal.SIGTERM)
            p.communicate(timeout=15)
            raise
        (evidence / (name + '.jsonl')).write_text(stdout)
        (evidence / (name + '.stderr')).write_text(stderr)
        got = launcher.parse_claude(stdout) if host == 'claude' else launcher.parse_codex(stdout)
        report, problems = launcher.check_report(got['text'], repo)
        tool_calls = []
        if host == 'claude':
            for line in stdout.splitlines():
                event = json.loads(line)
                message = event.get('message') or {}
                for block in message.get('content') or []:
                    if isinstance(block, dict) and block.get('type') == 'tool_use':
                        tool_calls.append({'tool': block.get('name'), 'skill': (block.get('input') or {}).get('skill')})
            if not tool_calls or tool_calls[0] != {'tool': 'Skill', 'skill': 'bb-writ:playbook-feature'}:
                problems.append('最初のツールで feature 手引きを使わなかった')
        result = {'host': host, 'name': name, 'session': got['session'], 'exit_code': p.returncode,
                  'model_effective': got['model_effective'], 'problems': problems, 'report': report,
                  'tool_calls': tool_calls}
        (evidence / (name + '.result.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2))
        if p.returncode or problems:
            raise RuntimeError(f'{name}: exit={p.returncode}, problems={problems}')
        return result

    try:
        for hosts in [('claude', 'claude'), ('claude', 'codex')]:
            prefix = str(len(results))
            names = [prefix + '-a', prefix + '-b']
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                futures = [pool.submit(session, h, n) for h, n in zip(hosts, names)]
                pair = [f.result() for f in futures]
            tasks = work('task', 'list', '--dir', str(repo))['tasks']
            chosen = []
            for name in names:
                found = [t for t in tasks if (Path(t['dir']) / (name + '.txt')).exists()]
                assert len(found) == 1, f'{name}: 成果物が専用の作業場所に無い'
                task = found[0]
                assert (Path(task['dir']) / (name + '.txt')).read_text().strip() == name
                assert not (repo / (name + '.txt')).exists(), '元の checkout に書かれた'
                assert not run(['git', '-C', task['dir'], 'status', '--porcelain']), '未 commit の成果物'
                chosen.append(task)
            assert chosen[0]['dir'] != chosen[1]['dir'], '作業場所が共有された'
            for task in chosen:
                resumed = work('task', 'resume', '--dir', str(repo), '--task', task['task'], '--holder', 'verification')
                expected = run(['git', '-C', repo, 'rev-parse', 'main'])
                run(['git', '-C', task['dir'], 'merge', '--no-edit', expected])
                # 最新の組み合わせで、先行タスクを含む成果物を検査する。
                for n in names:
                    f = Path(task['dir']) / (n + '.txt')
                    if f.exists():
                        assert f.read_text().strip() == n
                verified = run(['git', '-C', task['dir'], 'rev-parse', 'HEAD'])
                work('integrate', '--dir', task['dir'], '--token', resumed['token'], '--expected', expected, '--verified', verified)
                work('task', 'cleanup', '--dir', task['dir'], '--token', resumed['token'], '--saved', 'main')
            for n in names:
                assert (repo / (n + '.txt')).read_text().strip() == n
            assert not work('task', 'list', '--dir', str(repo))['tasks'], '片付け後にタスク登録が残った'
            results.extend(pair)
            print(json.dumps({'hosts': hosts, 'result': 'passed', 'sessions': [r['session'] for r in pair]}), flush=True)
        # 読むだけの依頼では、専用 worktree を作らず investigation を使う。
        a = SimpleNamespace(host='claude', write=False, cwd=repo, model='sonnet', effort='medium')
        prompt = '/bb-writ:bb-writ README.md を読んで説明して。変更はしないで。委任も不要。'
        cmd, child_env, why = launcher.command(a, prompt + launcher.CONTRACT, evidence / 'scratch')
        if why:
            raise RuntimeError(why)
        child_env['BB_WRIT_STATE_DIR'] = str(evidence / 'state')
        cmd[cmd.index('--output-format') + 1] = 'stream-json'
        cmd.extend(['--verbose', '--max-turns', '8'])
        p = subprocess.run(cmd, cwd=repo, env=child_env, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=180)
        (evidence / 'read-only.jsonl').write_text(p.stdout)
        (evidence / 'read-only.stderr').write_text(p.stderr)
        skills = []
        for line in p.stdout.splitlines():
            event = json.loads(line)
            for block in (event.get('message') or {}).get('content') or []:
                if isinstance(block, dict) and block.get('type') == 'tool_use' and block.get('name') == 'Skill':
                    skills.append((block.get('input') or {}).get('skill'))
        assert p.returncode == 0, '読むだけの CLI セッションが失敗'
        assert 'bb-writ:playbook-investigation' in skills, f'調査の手引きが使われなかった: {skills}'
        assert not work('task', 'list', '--dir', str(repo))['tasks'], '読むだけの依頼でタスクが作成された'
        (evidence / 'read-only.result.json').write_text(json.dumps({'status': 'passed', 'skills': skills}))
        (evidence / 'result.json').write_text(json.dumps({'status': 'passed', 'sessions': results}, ensure_ascii=False, indent=2))
    except BaseException as e:
        (evidence / 'failure.txt').write_text(str(e))
        raise
    finally:
        # 失敗した場合も未統合の成果物と再開記録を残す。成功時は空の一覧になる。
        (evidence / 'remaining-tasks.json').write_text(json.dumps(work('task', 'list', '--dir', str(repo)), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
