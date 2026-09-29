import hashlib
from pathlib import Path

from alembic.script import ScriptDirectory

MIGRATIONS = Path(__file__).resolve().parents[1] / "app" / "db" / "migrations"
RELEASE_ROLLBACK_TARGET = "e5a9c3f1b7d2"
GRAPH_SIZE = 181
GRAPH_FINGERPRINT = "d79312d7f29e86ea99c3c6f540ad366673bbe6c109d4c576c2e3e7551b63e040"


def test_the_rollback_image_has_exactly_the_graph_the_release_rolls_back_to():
    script = ScriptDirectory(str(MIGRATIONS))
    lines = []
    for revision in script.iterate_revisions("heads", "base"):
        down = revision.down_revision
        down = ",".join(sorted(down)) if isinstance(down, tuple) else (down or "")
        lines.append(f"{revision.revision}<{down}")
    lines.sort()

    assert script.get_heads() == [RELEASE_ROLLBACK_TARGET]
    assert (len(lines), hashlib.sha256("\n".join(lines).encode()).hexdigest()) == (GRAPH_SIZE, GRAPH_FINGERPRINT)
