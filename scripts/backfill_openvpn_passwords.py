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
