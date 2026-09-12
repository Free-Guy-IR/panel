def register_fork_link_handlers(builder) -> None:
    builder.protocol_handlers["mtproto"] = builder._build_mtproto
