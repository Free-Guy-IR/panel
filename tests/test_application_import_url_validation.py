import pytest
from pydantic import ValidationError

from app.models.settings import IMPORT_URL_SCHEMES, Application

SUB_PAGE_APP_SCHEMES = {
    "https",
    "http",
    "happ",
    "v2rayng",
    "v2box",
    "v2raytun",
    "streisand",
    "sing-box",
    "hiddify",
    "karing",
    "clash",
    "clashmeta",
    "flclash",
    "koala-clash",
    "stash",
    "foxray",
    "shadowrocket",
    "sub",
    "sn",
    "husi",
    "incy",
    "loon",
    "surge",
    "quantumult-x",
    "npvtsub",
}

ACCEPTED = [
    "",
    "v2rayng://install-config?url={url}",
    "v2rayng://install-sub?url={url}",
    "streisand://import/{url}",
    "sing-box://import-remote-profile?url={url}",
    "happ://add/{url}",
    "HAPP://add/{url}",
    "hiddify://import/{url}",
    "clash://install-config?url={url}",
    "shadowrocket://add/sub://{url}",
    "quantumult-x:///add-resource?remote-resource={url}",
    "https://example.com/import?sub={url}",
]

REJECTED = [
    "\x01javascript:alert(1)//{url}",
    "javascript:alert(1)//{url}",
    "JavaScript://x%0Aalert(1)//{url}",
    "java\tscript:alert(1)//{url}",
    "data:text/html,{url}",
    "vbscript:msgbox(1)//{url}",
    "file:///etc/passwd?{url}",
    "ftp://example.com/{url}",
    " happ://add/{url}",
    "happ://add/{url} ",
    "happ://add/\u00a0{url}",
    "happ://add/\u2028{url}",
    "happ://add/\u200b{url}",
    "happ://add/\u202e{url}",
    "happ://add/\x85{url}",
    "happ://add/\x7f{url}",
    "//evil.example/{url}",
    "1happ://add/{url}",
    "happ//add/{url}",
    "happ://add/",
    "happ://add/" + "a" * 250 + "{url}",
]


def _app(import_url: str) -> Application:
    return Application(name="client", platform="android", download_links=[], import_url=import_url)


def test_the_allowed_schemes_are_exactly_the_sub_pages_list():
    assert set(IMPORT_URL_SCHEMES) == SUB_PAGE_APP_SCHEMES


@pytest.mark.parametrize("import_url", ACCEPTED)
def test_real_client_import_links_are_accepted(import_url):
    assert _app(import_url).import_url == import_url


@pytest.mark.parametrize("import_url", REJECTED, ids=[repr(value)[:40] for value in REJECTED])
def test_hostile_or_malformed_import_links_are_refused(import_url):
    with pytest.raises(ValidationError):
        _app(import_url)
