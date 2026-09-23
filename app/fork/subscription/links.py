from app.fork.subscription.helpers import UnissuedSecretError


def _refuse_empty_links_when_mtproto_was_unissued(builder) -> None:
    render = builder.render

    def render_or_refuse():
        output = render()
        if not output and "mtproto" in builder.unissued_secret_protocols:
            raise UnissuedSecretError("mtproto")
        return output

    builder.render = render_or_refuse


def register_fork_link_handlers(builder) -> None:
    builder.protocol_handlers["mtproto"] = builder._build_mtproto
    _refuse_empty_links_when_mtproto_was_unissued(builder)
