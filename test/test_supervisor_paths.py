"""Portable real-process coverage for virtual-environment launcher identity."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import venv

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "package" / "editor"))
from infernux_mcp.launcher_paths import python_launcher_path


class PythonLauncherTests(unittest.TestCase):
    def test_empty_launcher_does_not_become_current_directory(self):
        self.assertEqual(python_launcher_path(""), "")

    def test_relative_launcher_is_normalized_without_dereferencing(self):
        path = Path("venv") / "bin" / ".." / "bin" / "python"
        self.assertEqual(python_launcher_path(path), os.path.abspath("venv/bin/python"))

    @unittest.skipIf(os.name == "nt", "POSIX venv launcher symlink regression")
    def test_symlink_launcher_keeps_real_child_inside_virtual_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "environment"
            venv.EnvBuilder(with_pip=False, symlinks=True).create(root)
            executable = root / "bin" / "python"
            self.assertTrue(executable.is_symlink())
            chosen = python_launcher_path(executable)
            self.assertNotEqual(chosen, str(executable.resolve()))
            probe = subprocess.run(
                [chosen, "-I", "-c", "import json,sys;print(json.dumps([sys.prefix,sys.base_prefix]))"],
                text=True, capture_output=True, check=True,
            )
            prefix, base = json.loads(probe.stdout)
            self.assertEqual(Path(prefix).resolve(), root.resolve())
            self.assertNotEqual(prefix, base)


if __name__ == "__main__":
    unittest.main()
