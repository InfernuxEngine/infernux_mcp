"""The editor plugin launches the independent MCP process."""
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread
from infernux.lifecycle import InxPreload


class InfernuxMCPPreload(InxPreload):
    def preload(self, context):
        if context.runtime:
            return
        context.own_python_library("editor/infernux_mcp")
        root = str(Path(__file__).parent.parent)
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join([root, *sys.path])
        log_path = Path(context.project_root) / "Cache" / "Automation" / "mcp.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("ab")
        context.add_cleanup(log.close)
        process = subprocess.Popen([sys.executable, "-m", "infernux_mcp", "--project", context.project_root,
                                    "--transport", "http"], env=environment, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=log,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.process = process
        self._stopping = False
        self._descriptor = str(log_path.with_name("mcp.json"))
        context.add_cleanup(self._stop)
        def observe_exit():
            code = process.wait()
            if not self._stopping:
                context.fail(f"MCP service exited ({code}); see {log_path}")
        Thread(target=observe_exit, name="InfernuxMCPExit", daemon=True).start()

    def _stop(self):
        self._stopping = True
        process = self.process
        if process.poll() is None:
            process.terminate()
            process.wait()
        descriptor = Path(self._descriptor)
        if descriptor.exists():
            import json
            if json.loads(descriptor.read_text(encoding="utf-8")).get("pid") == process.pid:
                descriptor.unlink()
