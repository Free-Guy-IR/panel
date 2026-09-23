"""Backfill a persisted OpenVPN password for users created before OpenVPN support.

``ProxyTable.openvpn`` (app/models/proxy.py) fills a missing key with
``OpenVPNSettings(password=random_password())`` via Pydantic's
default_factory. That default is only ever generated in-memory, for whichever
request happens to read it - it is never written back to
``User.proxy_settings``. For any user whose proxy_settings predates this
protocol being added, every subscription fetch mints a brand new password,
so the value the client sees never matches what any node was ever told to
accept. This is the exact same bug class hit (and fixed) for hysteria2 -
see scripts/backfill_hysteria2_passwords.py - built in from the start this
time instead of discovered after users report broken connections.

This script is idempotent: it only touches users where the "openvpn" key
is genuinely absent, and only adds that key - no other field is read or
modified. Safe to re-run; a second run is a no-op once every user has the
key. Run once after upgrading a pre-existing install to a panel build that
supports the OpenVPN core, from inside the panel container:

    docker exec <panel-container> python3 scripts/backfill_openvpn_passwords.py

After running, any node serving OpenVPN to previously-affected users needs
a reconnect (POST /api/node/{id}/reconnect) so it picks up the newly
persisted passwords - this script only touches the database, not running
node processes.
"""

import asyncio

from sqlalchemy import select

from app.db import GetDB
from app.db.models import User
from app.fork.proxy_secrets.backfill import fill_missing_sections
from app.fork.proxy_secrets.entitled import ENTITLED_SPEC_BY_FIELD, EntitledSecretField
from app.utils.system import random_password

SPEC = ENTITLED_SPEC_BY_FIELD[EntitledSecretField.openvpn_password]
CHUNK = 500


async def main() -> None:
    async with GetDB() as db:
        user_ids = (await db.execute(select(User.id).order_by(User.id))).scalars().all()
    fixed = 0
    for start in range(0, len(user_ids), CHUNK):
        async with GetDB() as db:
            fixed += await fill_missing_sections(db, SPEC, user_ids[start : start + CHUNK], random_password)
            await db.commit()
        print(f"committed {fixed} so far...")
    print(f"done. total users fixed: {fixed}")


if __name__ == "__main__":
    asyncio.run(main())
