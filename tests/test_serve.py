from pathlib import Path

from jevloop.serve import DASHBOARD_DIR, LOG_DIR, Handler


def _translate(path: str) -> Path:
    handler = Handler.__new__(Handler)  # translate_path needs no socket state
    return Path(handler.translate_path(path))


def test_dashboard_files_are_served():
    assert _translate("/") == (DASHBOARD_DIR / "index.html").resolve()
    assert _translate("/wall.html") == (DASHBOARD_DIR / "wall.html").resolve()


def test_log_files_come_from_the_log_dir():
    assert _translate("/latest.json") == LOG_DIR / "latest.json"
    assert _translate("/log.jsonl?x=1") == LOG_DIR / "log.jsonl"


def test_path_traversal_cannot_leave_the_dashboard_dir():
    for path in ("/../.env", "/../jevloop/limits.py", "/../../.jev-loop/ca-bundle.pem"):
        resolved = _translate(path)
        assert resolved.name == "__not_found__"
        assert resolved.parent == DASHBOARD_DIR
