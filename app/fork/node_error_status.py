NODE_UNAUTHENTICATED_STATUS = 502


def node_error_status(code):
    if code == 401:
        return NODE_UNAUTHENTICATED_STATUS
    return code


def carried_http_error_status(code) -> int | None:
    if isinstance(code, bool) or not isinstance(code, int) or not 400 <= code <= 599:
        return None
    return node_error_status(int(code))
