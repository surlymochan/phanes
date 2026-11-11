"""Static regression checks for test fixtures (also run by unittest discover)."""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def violations(source):
    """Reject shared repository scratch allocations and fixed listening ports.

    Inspect executable expressions, not documentation or assertion strings. A
    scratch repository's own .swrm-tmp remains valid test input.
    """
    compile(source, '<test fixture>', 'exec')
    tree = ast.parse(source)
    errors = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            if isinstance(node.right, ast.Constant) and node.right.value in ('.tmp', '.swrm-tmp'):
                names = {n.id for n in ast.walk(node.left) if isinstance(n, ast.Name)}
                if names & {'ROOT', 'REPO_ROOT', 'SCRIPT', '__file__'}:
                    errors.append((node.lineno, 'shared repository scratch root'))
        if isinstance(node, ast.Call):
            name = (node.func.attr if isinstance(node.func, ast.Attribute)
                    else node.func.id if isinstance(node.func, ast.Name) else '')
            if name in ('bind', 'HTTPServer', 'ThreadingHTTPServer', 'TCPServer') and node.args:
                address = node.args[0]
                if isinstance(address, ast.Tuple) and len(address.elts) == 2:
                    port = address.elts[1]
                    if isinstance(port, ast.Constant) and isinstance(port.value, int) and port.value != 0:
                        errors.append((node.lineno, 'fixed listening port'))
            if name == 'bind' and node.args:
                target = node.args[0]
                if isinstance(target, ast.Constant) and isinstance(target.value, str):
                    if target.value.startswith(('/tmp/', '/run/')):
                        errors.append((node.lineno, 'fixed socket path'))
            if name == 'rmtree' and node.args:
                target = node.args[0]
                if (isinstance(target, ast.Constant) and target.value in
                        ('.tmp', '.swrm-tmp', '/tmp')) or (
                        isinstance(target, ast.Call) and isinstance(target.func, ast.Attribute)
                        and target.func.attr == 'gettempdir'):
                    errors.append((node.lineno, 'deleting shared temporary root'))
    return errors


class IsolationGuardTests(unittest.TestCase):
    def test_test_sources_use_isolated_resources(self):
        errors = []
        for directory in ('swrm/tests', 'tools/tests'):
            for path in sorted((ROOT / directory).rglob('*.py')):
                for line, reason in violations(path.read_text(encoding='utf-8')):
                    errors.append('%s:%s: %s' % (path.relative_to(ROOT), line, reason))
        self.assertEqual(errors, [])

    def test_deliberate_violations_are_detected(self):
        for source in (
                'root = ROOT / ".swrm-tmp"',
                'root = ROOT / ".tmp" / "shared"',
                'shutil.rmtree(".swrm-tmp")',
                'shutil.rmtree(tempfile.gettempdir())',
                'sock.bind(("127.0.0.1", 8765))',
                'sock.bind("/tmp/shared.sock")',
                'HTTPServer(("localhost", 8765), Handler)',
                'http.HTTPServer(("localhost", 8765), Handler)'):
            with self.subTest(source=source):
                self.assertTrue(violations(source))

    def test_private_resources_and_contract_strings_are_allowed(self):
        self.assertEqual(violations('''
root = tempfile.TemporaryDirectory()
shutil.rmtree(root.name)
sock.bind(("127.0.0.1", 0))
self.assertEqual(result, repo / ".swrm-tmp")
'''), [])
