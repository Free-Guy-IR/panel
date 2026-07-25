"""Backfill a persisted Hysteria2 password for users created before sing-box support.

``ProxyTable.hysteria2`` (app/models/proxy.py) fills a missing key with
``Hysteria2Settings(password=random_password())`` via Pydantic's
default_factory. That default is only ever generated in-memory, for whichever
request happens to read it - it is never written back to
``User.proxy_settings``. For any user whose proxy_settings predates this
protocol being added, every subscription fetch mints a brand new password,
so the value the client sees never matches what any node was ever told to
accept.

This script is idempotent: it only touches users where the "hysteria2" key
is genuinely absent, and only adds that key - no other field is read or
modified. Safe to re-run; a second run is a no-op once every user has the
key. Run once after upgrading a pre-existing install to a panel build that
supports the sing-box/Hysteria2 core, from inside the panel container:

    docker exec <panel-container> python3 scripts/backfill_hysteria2_passwords.py

After running, any node serving Hysteria2 to previously-affected users needs
a reconnect (POST /api/node/{id}/reconnect) so it picks up the newly
persisted passwords - this script only touches the database, not running
node processes.
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.db import GetDB
from app.db.models import User
from app.utils.system import random_password


async def main() -> None:
    fixed = 0
    async with GetDB() as db:
        result = await db.execute(select(User))
        users = result.scalars().all()
        for user in users:
            settings = user.proxy_settings or {}
            if "hysteria2" not in settings:
                settings = dict(settings)
                settings["hysteria2"] = {"password": random_password()}
                user.proxy_settings = settings
                flag_modified(user, "proxy_settings")
                fixed += 1
                if fixed % 300 == 0:
                    await db.commit()
                    print(f"committed {fixed} so far...")
        await db.commit()
    print(f"done. total users fixed: {fixed}")


if __name__ == "__main__":
    asyncio.run(main())
