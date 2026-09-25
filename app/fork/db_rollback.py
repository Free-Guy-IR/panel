import argparse
import sys
from argparse import Namespace
from collections.abc import Callable, Iterable
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.environment import EnvironmentContext
from alembic.script import ScriptDirectory
from alembic.util import CommandError

ROOT = Path(__file__).resolve().parents[2]


class RollbackError(RuntimeError):
    pass


def default_config(url: str | None = None, x_arguments: Iterable[str] = ()) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "app" / "db" / "migrations"))
    if url:
        config.set_main_option("sqlalchemy.url", url)
    x_arguments = list(x_arguments)
    if x_arguments:
        config.cmd_opts = Namespace(x=x_arguments)
    return config


def current_heads(config: Config) -> tuple[str, ...]:
    script = ScriptDirectory.from_config(config)
    found: list[str] = []

    def capture(_revision, context):
        found.extend(context.get_current_heads())
        return []

    with EnvironmentContext(config, script, fn=capture, dont_mutate=True):
        script.run_env()
    return tuple(sorted(found))


def _known(script: ScriptDirectory, revision: str) -> bool:
    try:
        return script.get_revision(revision) is not None
    except CommandError:
        return False


def _lineage(script: ScriptDirectory, revisions: Iterable[str]) -> set[str]:
    lineage: set[str] = set()
    for revision in revisions:
        lineage.update(rev.revision for rev in script.iterate_revisions(revision, "base"))
    return lineage


def revisions_to_remove(script: ScriptDirectory, heads: Iterable[str], target: str) -> set[str]:
    heads = tuple(heads)
    if not _known(script, target):
        raise RollbackError(f"unknown target revision {target}")
    unknown = [head for head in heads if not _known(script, head)]
    if unknown:
        raise RollbackError(f"the database is at {', '.join(unknown)}, which this release does not know")
    applied = _lineage(script, heads)
    if target not in applied:
        raise RollbackError(f"the database at {', '.join(heads) or 'base'} is not at or above {target}")
    return applied - _lineage(script, (target,))


def _next_step(script: ScriptDirectory, heads: tuple[str, ...], target: str, removals: set[str]) -> str:
    if any(head != target and target in _lineage(script, (head,)) for head in heads):
        return target
    side_heads = sorted(head for head in heads if head in removals)
    if not side_heads:
        raise RollbackError(f"cannot reduce {', '.join(heads)} to {target}")
    return f"{side_heads[0]}@-1"


def rollback(
    config: Config, target: str, *, dry_run: bool = False, echo: Callable[[str], None] = print
) -> tuple[str, ...]:
    script = ScriptDirectory.from_config(config)
    heads = current_heads(config)
    removals = revisions_to_remove(script, heads, target)
    echo(f"current: {', '.join(heads)}")
    echo(f"target: {target}")
    echo(f"revisions to remove: {', '.join(sorted(removals)) or 'none'}")
    if dry_run:
        return heads
    while removals:
        step = _next_step(script, heads, target, removals)
        echo(f"alembic downgrade {step}")
        command.downgrade(config, step)
        heads = current_heads(config)
        remaining = revisions_to_remove(script, heads, target)
        if not remaining < removals:
            raise RollbackError(f"downgrade {step} made no progress; the database is at {', '.join(heads)}")
        removals = remaining
    if heads != (target,):
        raise RollbackError(f"the database is at {', '.join(heads)}, not exactly {target}")
    echo(f"done: the database is at {target}")
    return heads


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.fork.db_rollback")
    parser.add_argument("--to", required=True, dest="target")
    parser.add_argument("--url", default=None)
    parser.add_argument("-x", action="append", default=[], dest="x_arguments")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        rollback(default_config(args.url, args.x_arguments), args.target, dry_run=args.dry_run)
    except RollbackError as exc:
        print(f"rollback refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
