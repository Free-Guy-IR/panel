from app.fork.registry import get_subscription_format
from app.models.user import UsersResponseWithInbounds


def build_fork_subscription_config(config_format: str, client_templates=None):
    extra = get_subscription_format(config_format)
    if extra is None:
        return None
    return extra(client_templates)


async def generate_openvpn_files(user: UsersResponseWithInbounds) -> dict[str, str]:
    from app.fork.subscription.openvpn import OpenVPNConfiguration
    from app.settings import subscription_settings
    from app.subscription.client_templates import subscription_client_templates
    from app.subscription.share import (
        get_effective_custom_variables,
        process_inbounds_and_tags,
        setup_format_variables,
    )

    client_templates = await subscription_client_templates()
    conf = OpenVPNConfiguration()
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
    return conf._files()
