"""Bing result links are unwrapped to the real URL."""

import base64

from harvey.agents.scout import Scout


def _wrapped(url: str) -> str:
    token = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f"https://www.bing.com/ck/a?!&&p=abc123&ptn=3&ver=2&u=a1{token}&ntb=1"


def test_click_tracking_link_is_unwrapped():
    assert Scout._bing_target(_wrapped("https://www.example.com/about?x=1")) == \
        "https://www.example.com/about?x=1"


def test_plain_link_is_left_alone():
    assert Scout._bing_target("https://example.com/") == "https://example.com/"


def test_undecodable_link_is_left_alone():
    bad = "https://www.bing.com/ck/a?!&&p=abc&u=a1%%%%"
    assert Scout._bing_target(bad) == bad


def test_non_http_target_is_left_alone():
    link = _wrapped("javascript:alert(1)")
    assert Scout._bing_target(link) == link
