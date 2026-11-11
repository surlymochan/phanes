"""Static regression checks for test fixtures (also run by unittest discover)."""
import ast
import os
import shlex
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


_REPO = "/__isolation_checkout__"


def _constant_path(node):
    """Fold path syntax without evaluating any test code."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return {"ROOT": _REPO, "REPO_ROOT": _REPO, "SCRIPT": _REPO + "/tools/script.py",
                "__file__": _REPO + "/tools/tests/test.py", "HOME": str(Path.home())}.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.Add)):
        left, right = _constant_path(node.left), _constant_path(node.right)
        if left is not None and right is not None:
            return os.path.join(left, right) if isinstance(node.op, ast.Div) else left + right
    if isinstance(node, ast.JoinedStr):
        parts = [_constant_path(part.value if isinstance(part, ast.FormattedValue) else part)
                 for part in node.values]
        if all(part is not None for part in parts):
            return "".join(parts)
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        value = _constant_path(node.value)
        if value is not None:
            return os.path.dirname(value)
    if isinstance(node, ast.Call):
        name = _call_name(node)
        if name in ("Path", "str", "fspath", "resolve", "absolute", "expanduser"):
            value = _constant_path(node.args[0]) if node.args else (
                _constant_path(node.func.value) if isinstance(node.func, ast.Attribute) else None)
            return os.path.expanduser(value) if value is not None else None
        if name == "join":
            parts = [_constant_path(arg) for arg in node.args]
            if parts and all(part is not None for part in parts):
                return os.path.join(*parts)
        if name in ("dirname", "realpath", "abspath", "normpath") and node.args:
            value = _constant_path(node.args[0])
            if value is not None:
                return os.path.dirname(value) if name == "dirname" else os.path.normpath(value)
        if name == "gettempdir":
            return tempfile.gettempdir()
        if name == "home":
            return str(Path.home())
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute):
        if (node.value.attr == "parents" and isinstance(node.slice, ast.Constant) and
                isinstance(node.slice.value, int) and 0 <= node.slice.value < 20):
            value = _constant_path(node.value.value)
            if value is not None:
                for _ in range(node.slice.value + 1):
                    value = os.path.dirname(value)
                return value
        if node.value.attr == "environ" and _constant_path(node.slice) == "HOME":
            return str(Path.home())
    return None


def _call_name(node):
    return (node.func.attr if isinstance(node.func, ast.Attribute)
            else node.func.id if isinstance(node.func, ast.Name) else "")


def _shared_root(value):
    if value is None:
        return False
    path = os.path.normpath(os.path.expanduser(value))
    return (path in (".tmp", ".swrm-tmp", "/tmp", os.path.normpath(tempfile.gettempdir()),
                     str(Path.home()), "$HOME", "${HOME}", _REPO, str(ROOT))
            or path.startswith((_REPO + "/", str(ROOT) + "/")))


def _rm_targets(words):
    """Return recursive force-removal operands from a constant shell argv."""
    for index, word in enumerate(words):
        if os.path.basename(word) in ("sh", "bash", "dash"):
            for offset in range(index + 1, len(words) - 1):
                if words[offset] == "-c":
                    try:
                        return _rm_targets(shlex.split(words[offset + 1]))
                    except ValueError:
                        return []
        if os.path.basename(word) != "rm":
            continue
        tail = words[index + 1:]
        flags = "".join(arg.lstrip("-") for arg in tail if arg.startswith("-"))
        if ("r" in flags or "R" in flags or "recursive" in flags) and "f" in flags:
            return [arg for arg in tail if not arg.startswith("-")]
    return []


def violations(source):
    """Reject shared fixture roots and fixed listeners in executable syntax."""
    compile(source, '<test fixture>', 'exec')
    tree = ast.parse(source)
    errors = []
    for node in ast.walk(tree):
        # Retain detection of repository expressions beyond the folding subset.
        if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) and
                isinstance(node.right, ast.Constant) and
                node.right.value in (".tmp", ".swrm-tmp")):
            names = {part.id for part in ast.walk(node.left) if isinstance(part, ast.Name)}
            if names & {"ROOT", "REPO_ROOT", "SCRIPT", "__file__"}:
                errors.append((node.lineno, 'shared repository scratch root'))
        if isinstance(node, (ast.BinOp, ast.Call)):
            value = _constant_path(node)
            if value and value.startswith(_REPO + "/") and any(
                    part in (".tmp", ".swrm-tmp") for part in Path(value).parts):
                errors.append((node.lineno, 'shared repository scratch root'))
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name in ('bind', 'HTTPServer', 'ThreadingHTTPServer', 'TCPServer') and node.args:
            address = node.args[0]
            if isinstance(address, ast.Tuple) and len(address.elts) == 2:
                port = address.elts[1]
                if isinstance(port, ast.Constant) and isinstance(port.value, int) and port.value != 0:
                    errors.append((node.lineno, 'fixed listening port'))
        if name == 'bind' and node.args:
            target = _constant_path(node.args[0])
            if target and target.startswith(('/tmp/', '/run/')):
                errors.append((node.lineno, 'fixed socket path'))
        if name == 'rmtree' and node.args and _shared_root(_constant_path(node.args[0])):
            errors.append((node.lineno, 'deleting shared temporary root'))
        if name in ('run', 'call', 'check_call', 'check_output', 'Popen', 'system', 'sh') and node.args:
            argument = node.args[0]
            if isinstance(argument, (ast.List, ast.Tuple)):
                words = [_constant_path(item) for item in argument.elts]
            else:
                command = _constant_path(argument)
                try:
                    words = shlex.split(command) if command is not None else []
                except ValueError:
                    words = []
            if all(word is not None for word in words) and any(
                    _shared_root(target) for target in _rm_targets(words)):
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
                'root = Path(__file__).resolve().parents[2] / ".swrm-tmp"',
                'root = ROOT / ".tmp" / "shared"',
                'shutil.rmtree(".swrm-tmp")',
                'shutil.rmtree(os.path.join(ROOT, ".swrm-tmp"))',
                'shutil.rmtree("/tmp/")',
                'shutil.rmtree(Path(ROOT) / ".swrm-tmp")',
                'shutil.rmtree(ROOT / "fixed-fixture")',
                'shutil.rmtree(Path(__file__).resolve().parents[2] / "fixed-fixture")',
                'shutil.rmtree(ROOT + "/fixed-fixture")',
                'shutil.rmtree(f"{ROOT}/fixed-fixture")',
                'shutil.rmtree(Path.home())',
                'shutil.rmtree(HOME)',
                'shutil.rmtree(os.environ["HOME"])',
                'subprocess.run(["rm", "-rf", os.path.join(ROOT, "fixed")])',
                'os.system("rm -rf /tmp/")',
                'subprocess.run(f"rm -rf {ROOT}/fixed", shell=True)',
                'subprocess.run("rm -rf " + "/tmp/", shell=True)',
                'subprocess.run(["rm", "-rf", Path(ROOT) / ".swrm-tmp"])',
                'subprocess.run(["rm", "-rf", tempfile.gettempdir()])',
                'os.system("rm -rf $HOME")',
                'subprocess.run(["bash", "-c", "rm -rf /tmp/"])',
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
shutil.rmtree(tempfile.mkdtemp(dir=tempfile.gettempdir()))
subprocess.run(["rm", "-rf", root.name])
sock.bind(("127.0.0.1", 0))
self.assertEqual(result, repo / ".swrm-tmp")
'''), [])


if __name__ == "__main__":
    unittest.main()
