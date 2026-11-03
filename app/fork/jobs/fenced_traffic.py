from sqlalchemy import BigInteger, bindparam, func, update
from sqlalchemy.dialects.postgresql import ARRAY

from app.db.models import User


def fenced_user_traffic_updates(dialect: str, billable: list[dict], batch_size: int) -> list[tuple]:
    if not billable:
        return []

    if dialect == "postgresql":
        source = (
            func.unnest(
                bindparam("uids", type_=ARRAY(BigInteger())),
                bindparam("epochs", type_=ARRAY(BigInteger())),
                bindparam("traffic_values", type_=ARRAY(BigInteger())),
            )
            .table_valued("uid", "epoch", "value")
            .render_derived(name="usage_source")
        )
        stmt = (
            update(User)
            .where(User.id == source.c.uid, User.usage_epoch == source.c.epoch)
            .values(used_traffic=User.used_traffic + source.c.value)
            .execution_options(synchronize_session=False)
        )
        ordered = sorted(billable, key=lambda item: int(item["uid"]))
        return [
            (
                stmt,
                {
                    "uids": [int(item["uid"]) for item in ordered],
                    "epochs": [int(item["epoch"]) for item in ordered],
                    "traffic_values": [int(item["value"]) for item in ordered],
                },
            )
        ]

    stmt = (
        update(User)
        .where(User.id == bindparam("uid"), User.usage_epoch == bindparam("epoch"))
        .values(used_traffic=User.used_traffic + bindparam("value"))
        .execution_options(synchronize_session=False)
    )
    size = max(batch_size, 1)
    return [(stmt, billable[index : index + size]) for index in range(0, len(billable), size)]
