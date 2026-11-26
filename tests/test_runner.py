"""並べて回す tests/run.py: 小さな偽のテストの包みで、終了コード・落ちたテストの名前と出力・集計を見る。"""

import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent)); import process_limit  # noqa: E402,F401  テストに上限を掛ける（直に走らせても）

RUN = Path(__file__).resolve().parent / "run.py"


def run_on(files):
    """一時ディレクトリに tests の包み（files の名前と中身）を作り、その根で run.py を回す。"""
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "tests").mkdir()
        (Path(d) / "tests" / "__init__.py").write_text("")
        for name, src in files.items():
            (Path(d) / "tests" / name).write_text(textwrap.dedent(src))
        return subprocess.run([sys.executable, "-B", str(RUN), "-j", "2"], cwd=d, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=120)


PASS_AND_SKIP = """
    import unittest
    class T(unittest.TestCase):
        def test_pass(self):
            pass
        @unittest.skip("飛ばす")
        def test_skip(self):
            pass
"""


class TestRunner(unittest.TestCase):
    def test_a_failure_makes_it_fail_and_shows_the_test_and_its_output(self):
        r = run_on({"test_a.py": PASS_AND_SKIP, "test_b.py": """
            import unittest
            class U(unittest.TestCase):
                def test_fail(self):
                    print("出力の印-7f3a")
                    self.assertEqual(1, 2)
        """})
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("tests.test_b.U.test_fail", r.stdout)
        self.assertIn("出力の印-7f3a", r.stdout)
        self.assertRegex(r.stdout, r"(?m)^Ran 3 tests in ")
        self.assertRegex(r.stdout, r"(?m)^FAILED \(failures=1, skipped=1\)$")

    def test_all_passing_with_a_skip_exits_zero(self):
        r = run_on({"test_a.py": PASS_AND_SKIP})
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertRegex(r.stdout, r"(?m)^Ran 2 tests in ")
        self.assertRegex(r.stdout, r"(?m)^OK \(skipped=1\)$")

    def test_a_module_that_cannot_be_imported_fails(self):
        r = run_on({"test_a.py": PASS_AND_SKIP, "test_c.py": "import no_such_module_for_the_runner_test\n"})
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("no_such_module_for_the_runner_test", r.stdout)
        self.assertFalse(re.search(r"(?m)^OK", r.stdout), r.stdout)


if __name__ == "__main__":
    unittest.main()
