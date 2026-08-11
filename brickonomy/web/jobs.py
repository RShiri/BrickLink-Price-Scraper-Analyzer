"""In-process background refresh job — one at a time, polled by the UI."""
import threading
import time
from collections import deque

_lock = threading.Lock()
_state = {
    "running": False,
    "scope": None,
    "current_item": None,
    "done": 0,
    "total": 0,
    "errors": [],
    "log": deque(maxlen=40),
    "finished_at": None,
}


def status():
    with _lock:
        s = dict(_state)
        s["log"] = list(s["log"])
        s["errors"] = list(s["errors"])
        return s


def _log_line(msg):
    with _lock:
        _state["log"].append(msg)


def _progress(done, total, current_item, errors):
    with _lock:
        _state.update(done=done, total=total, current_item=current_item,
                      errors=[f"{i} · {s}: {e}" for i, s, e in errors])


def start(scope="portfolio", item_id=None, force=False):
    """Start a refresh in a background thread. Returns False if one is running."""
    from ..refresh import run_refresh

    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, scope=item_id or scope, current_item=None,
                      done=0, total=0, errors=[], finished_at=None)
        _state["log"].clear()

    def worker():
        try:
            run_refresh(scope=scope, item_id=item_id, force=force,
                        progress=_progress, log=_log_line)
        except Exception as exc:
            _log_line(f"✘ refresh crashed: {type(exc).__name__}: {exc}")
        finally:
            with _lock:
                _state["running"] = False
                _state["current_item"] = None
                _state["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    threading.Thread(target=worker, name="brickonomy-refresh", daemon=True).start()
    return True
