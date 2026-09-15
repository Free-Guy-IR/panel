import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

ROOT = os.environ.get("PANEL_ROOT", "/root/dev/panel")
LIVE_DB = os.path.join(ROOT, "db.sqlite3")
MIGRATION = os.environ.get("MIGRATION_PATH") or os.path.join(ROOT, "app/db/migrations/versions/f6c4d0e2a183_content_filter_backfill_sniffing_owners.py")
SHAPE = {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": False}
failures = []


def check(label, got, want):
    ok = got == want
    print("  %-62s %s" % (label, "OK" if ok else "FAIL got=%r want=%r" % (got, want)))
    if not ok:
        failures.append(label)


def load_migration():
    sys.path.insert(0, ROOT)
    spec = importlib.util.spec_from_file_location("backfill", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rows(path):
    db = sqlite3.connect(path)
    out = {
        (r[0], r[1]): (json.loads(r[2]) if r[2] else None)
        for r in db.execute("SELECT core_id, inbound_tag, original FROM content_filter_sniffing_overrides")
    }
    db.close()
    return out


def ledger_exists(path):
    db = sqlite3.connect(path)
    n = db.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='content_filter_sniffing_repairs'"
    ).fetchone()[0]
    db.close()
    return n == 1


def fresh_copy():
    work = tempfile.mkdtemp()
    path = os.path.join(work, "db.sqlite3")
    src = sqlite3.connect("file:%s?mode=ro" % LIVE_DB, uri=True)
    dst = sqlite3.connect(path)
    src.backup(dst)
    src.close()
    dst.execute("DELETE FROM content_filter_sniffing_overrides")
    dst.execute("DROP TABLE IF EXISTS content_filter_sniffing_repairs")
    dst.commit()
    dst.close()
    return work, path


def seed(path, entries):
    db = sqlite3.connect(path)
    for core_id, tag, original in entries:
        db.execute(
            "INSERT INTO content_filter_sniffing_overrides (core_id, inbound_tag, original, installed_at) "
            "VALUES (?, ?, ?, '2026-09-15 00:00:00')",
            (core_id, tag, json.dumps(original) if original is not None else None),
        )
    db.commit()
    db.close()


def run(path, fn):
    engine = sa.create_engine("sqlite:///" + path)
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            fn()
    engine.dispose()


migration = load_migration()

print("=== A. no mapping: the migration must touch nothing ===")
work, path = fresh_copy()
seed(path, [(1, "Shadowsocks TCP", SHAPE), (1, "Unrelated Legit", None)])
os.environ.pop(migration.MAPPING_ENV, None)
before = rows(path)
run(path, migration.upgrade)
check("rows unchanged with an empty mapping", rows(path), before)
check("ledger table created but empty is fine", ledger_exists(path), True)
run(path, migration.downgrade)
check("downgrade with empty ledger leaves rows unchanged", rows(path), before)
check("ledger table removed", ledger_exists(path), False)
shutil.rmtree(work)

print("\n=== B. operator sniffing EQUAL to our shape, NOT in the mapping, is left alone ===")
work, path = fresh_copy()
seed(path, [(1, "Shadowsocks Open", SHAPE)])
os.environ[migration.MAPPING_ENV] = "1:Shadowsocks TCP"
run(path, migration.upgrade)
check("unmapped row with our exact shape keeps its original", rows(path)[(1, "Shadowsocks Open")], SHAPE)
run(path, migration.downgrade)
check("still intact after downgrade", rows(path)[(1, "Shadowsocks Open")], SHAPE)
shutil.rmtree(work)

print("\n=== C. a verified legacy tag with NO row is inserted, and exactly reversed ===")
work, path = fresh_copy()
seed(path, [(1, "Unrelated Legit", None)])
os.environ[migration.MAPPING_ENV] = "1:Shadowsocks TCP"
run(path, migration.upgrade)
check("mapped tag now owned with original NULL", rows(path).get((1, "Shadowsocks TCP"), "ABSENT"), None)
check("unrelated NULL-original row survives upgrade", rows(path).get((1, "Unrelated Legit"), "ABSENT"), None)
run(path, migration.downgrade)
check("downgrade removes only the row the migration inserted", (1, "Shadowsocks TCP") in rows(path), False)
check("unrelated NULL-original row SURVIVES downgrade", rows(path).get((1, "Unrelated Legit"), "ABSENT"), None)
shutil.rmtree(work)

print("\n=== D. a verified legacy tag whose row self-recorded our shape is rewritten, and restored on downgrade ===")
work, path = fresh_copy()
seed(path, [(1, "Shadowsocks TCP", SHAPE), (1, "Unrelated Legit", None)])
os.environ[migration.MAPPING_ENV] = "1:Shadowsocks TCP"
run(path, migration.upgrade)
check("mapped self-recorded row rewritten to NULL", rows(path)[(1, "Shadowsocks TCP")], None)
run(path, migration.downgrade)
check("downgrade restores the previous recorded value exactly", rows(path)[(1, "Shadowsocks TCP")], SHAPE)
check("unrelated NULL-original row untouched throughout", rows(path).get((1, "Unrelated Legit"), "ABSENT"), None)
shutil.rmtree(work)

print("\n=== E. a mapped row whose original is genuinely NULL already is not double-ledgered ===")
work, path = fresh_copy()
seed(path, [(1, "Shadowsocks TCP", None)])
os.environ[migration.MAPPING_ENV] = "1:Shadowsocks TCP"
run(path, migration.upgrade)
db = sqlite3.connect(path)
n = db.execute("SELECT count(*) FROM content_filter_sniffing_repairs").fetchone()[0]
db.close()
check("no ledger entry for a row that needed nothing", n, 0)
run(path, migration.downgrade)
check("row still present after downgrade", rows(path).get((1, "Shadowsocks TCP"), "ABSENT"), None)
shutil.rmtree(work)

print("\n=== F. a mapping naming a core that does not exist is ignored ===")
work, path = fresh_copy()
os.environ[migration.MAPPING_ENV] = "999:Ghost"
run(path, migration.upgrade)
check("no row created for an unknown core", (999, "Ghost") in rows(path), False)
run(path, migration.downgrade)
shutil.rmtree(work)

print()
if failures:
    print("FAILED %d check(s): %s" % (len(failures), failures))
    sys.exit(1)
print("ALL migration CHECKS PASSED")
