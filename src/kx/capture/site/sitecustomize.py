"""Put on PYTHONPATH by kx exec: every Python process of the run gets kx.capture's import hook, so what it logs to
wandb or MLflow is recorded in the run. Cheap: kx itself is imported only when the code imports one of them. The
sitecustomize this one hides (a distribution's) still runs, after it."""
import importlib.machinery
import importlib.util
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_NAMES = ("wandb", "mlflow")


class _Hook:
    def find_spec(self, name, path=None, target=None):
        if name not in _NAMES or not os.environ.get("KX_RUN_ID"):
            return None
        sys.meta_path.remove(self)
        try:
            try:
                import kx.capture as capture
            except ImportError:  # an interpreter without kx installed: the sources this file is part of
                sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(_here))))
                import kx.capture as capture
            capture.install()  # its own hook takes the next imports; this one it answers here
            return capture.Finder().find_spec(name, path, target)
        except Exception as e:  # noqa: BLE001 - never stop the run's code from starting
            print(f"kx: no capture of {name} ({type(e).__name__}: {e})", file=sys.stderr)
        return None


if os.environ.get("KX_CAPTURE", "").lower() not in ("off", "0", "false", "no"):
    sys.meta_path.insert(0, _Hook())

_other = importlib.machinery.PathFinder.find_spec("sitecustomize", [p for p in sys.path if os.path.abspath(p or ".") != _here])
if _other is not None and _other.loader is not None:
    _module = importlib.util.module_from_spec(_other)
    _other.loader.exec_module(_module)
