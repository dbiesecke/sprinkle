import sys
import types


progress_module = types.ModuleType("progress")
bar_module = types.ModuleType("progress.bar")
daemons_module = types.ModuleType("daemons")
prefab_module = types.ModuleType("daemons.prefab")
run_module = types.ModuleType("daemons.prefab.run")
filelock_module = types.ModuleType("filelock")


class DummyBar(object):
    def __init__(self, *args, **kwargs):
        self.message = ""

    def next(self):
        return None

    def finish(self):
        return None


bar_module.Bar = DummyBar
run_module.RunDaemon = object
filelock_module.Timeout = Exception
filelock_module.FileLock = lambda *args, **kwargs: None

sys.modules.setdefault("progress", progress_module)
sys.modules.setdefault("progress.bar", bar_module)
sys.modules.setdefault("daemons", daemons_module)
sys.modules.setdefault("daemons.prefab", prefab_module)
sys.modules.setdefault("daemons.prefab.run", run_module)
sys.modules.setdefault("filelock", filelock_module)
