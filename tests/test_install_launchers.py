"""Non-destructive installer smoke tests using temporary HOME directories."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class InstallerTest(unittest.TestCase):
    def _run(self, script, *, is_termux=False):
        with tempfile.TemporaryDirectory() as work:
            home = Path(work) / "home"
            home.mkdir()
            prefix = Path(work) / "usr"
            env = dict(os.environ)
            env["HOME"] = str(home)
            env["MONICO_HOME"] = str(home / ".monico-bridge")
            env["PREFIX"] = str(prefix) if is_termux else env.get("PREFIX", "")
            env.pop("MONICO_SERVER", None)
            env.pop("MONICO_TOKEN", None)
            proc = subprocess.run(
                ["bash", str(ROOT / script)],
                cwd=str(ROOT), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            launch_dir = prefix / "bin" if is_termux else home / ".local" / "bin"
            names = ("monico-connect", "monico-cmd", "monico-model")
            if not is_termux:
                names += ("monico-server", "monico-pair")
            for name in names:
                path = launch_dir / name
                self.assertTrue(path.is_file(), name)
                first = path.open().readline().strip()
                self.assertTrue(first.startswith("#!/"), first)
                interpreter = first[2:].split()[0]
                if not is_termux:
                    # /usr/bin/env bash is valid for normal Linux.
                    self.assertTrue(os.access(interpreter, os.X_OK), interpreter)
                else:
                    # /usr/bin/env often does not exist on Termux.
                    self.assertNotIn("/usr/bin/env", first)
                    self.assertTrue(os.access(interpreter, os.X_OK), interpreter)
                self.assertTrue(os.access(str(path), os.X_OK), name)

    def test_linux_installer_creates_all_launchers(self):
        self._run("install-linux.sh")

    def test_termux_installer_writes_portable_shell_launchers(self):
        self._run("install-termux.sh", is_termux=True)


if __name__ == "__main__":
    unittest.main()
