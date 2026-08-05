from agent_hub.settings import Settings, get_settings


def resolve_database_url(configured_url: str | None, settings: Settings | None = None) -> str:
    if configured_url:
        return configured_url
    return (settings or get_settings()).database_url_value()
