import io
import json

import pytest

from scorn import update


@pytest.mark.parametrize(
    "latest, current, expected",
    [
        ("0.2.0", "0.1.0", True),
        ("0.1.1", "0.1.0", True),
        ("1.0", "0.9.9", True),
        ("0.1.0", "0.1.0", False),
        ("0.1", "0.1.0", False),
        ("0.0.9", "0.1.0", False),
        ("v0.2.0", "0.1.0", True),
        ("0.2.0", "v0.1.0", True),
        ("0.2.0", "0.0.0+unknown", False),
        ("0.2.0", "0.1.0rc1", False),
        ("0.2.0-beta", "0.1.0", False),
        ("garbage", "0.1.0", False),
        ("0.2.0", "", False),
    ],
)
def test_is_newer(latest, current, expected):
    assert update.is_newer(latest, current) is expected


def test_latest_release_returns_none_when_request_fails(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(update.urllib.request, "urlopen", boom)
    assert update.latest_release() is None


def test_latest_release_parses_response(monkeypatch):
    body = json.dumps({"tag_name": "v1.2.3", "html_url": "https://github.com/x/y/releases/tag/v1.2.3"}).encode()
    seen = {}

    def fake_urlopen(request, timeout):
        seen["headers"] = dict(request.header_items())
        return io.BytesIO(body)

    monkeypatch.setattr(update.urllib.request, "urlopen", fake_urlopen)
    assert update.latest_release() == {"version": "1.2.3", "url": "https://github.com/x/y/releases/tag/v1.2.3"}
    assert seen["headers"]["Accept"] == "application/vnd.github+json"
    assert seen["headers"]["User-agent"].startswith("scorn/")


def test_latest_release_returns_none_for_bad_json(monkeypatch):
    monkeypatch.setattr(update.urllib.request, "urlopen", lambda request, timeout: io.BytesIO(b"not json"))
    assert update.latest_release() is None
