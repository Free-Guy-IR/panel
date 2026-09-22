from app.fork.registry import get_subscription_format
from app.models.user import UsersResponseWithInbounds


def build_fork_subscription_config(config_format: str, client_templates=None):
    extra = get_subscription_format(config_format)
    if extra is None:
        return None
    return extra(client_templates)


async def _collect_page_components(user: UsersResponseWithInbounds, conf) -> None:
    from app.subscription.share import (
        get_effective_custom_variables,
        process_inbounds_and_tags,
        setup_format_variables,
        subscription_client_templates,
        subscription_settings,
    )

    client_templates = await subscription_client_templates()
    sub_settings = await subscription_settings()
    custom_variables = get_effective_custom_variables(user, sub_settings.custom_variables)
    format_variables = setup_format_variables(user, sub_settings.custom_variables)

    await process_inbounds_and_tags(
        user,
        format_variables,
        conf,
        client_templates,
        randomize_order=sub_settings.randomize_order,
        custom_variables=custom_variables,
    )


async def generate_l2tp_details(user: UsersResponseWithInbounds) -> list[dict]:
    from app.fork.subscription.l2tp import L2TPConfiguration

    conf = L2TPConfiguration()
    await _collect_page_components(user, conf)
    return list(conf.details)


async def generate_openvpn_files(user: UsersResponseWithInbounds) -> dict[str, str]:
    from app.fork.subscription.openvpn import OpenVPNConfiguration

    conf = OpenVPNConfiguration(refuse_unissued_secret=False)
    await _collect_page_components(user, conf)
    return conf._files()
