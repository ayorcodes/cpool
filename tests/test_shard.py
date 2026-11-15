"""Auth/probe boundary regressions are not exercised by the existing CLI tests."""
import io
import urllib.error
import urllib.request
from email.message import Message
from http.client import HTTPResponse
from unittest.mock import MagicMock

import pytest

from cpool import shard


@pytest.mark.parametrize("status,body,expected", [
    (200, b'{"shard": 1}', True),
    (200, b'{"shard": true}', True),
    (200, b'{"shard": 0}', False),
    (200, b'{}', False),
    (201, b'{"shard": 1}', False),
    (200, b'not json', False),
    (200, b'[]', False),
])
def test_relay_capability_requires_success_and_shard(monkeypatch, status, body, expected):
    def open_request(request, timeout):
        assert request.full_url == "http://localhost:4001/_relay/health"
        assert request.get_method() == "GET"
        assert request.headers == {}
        assert timeout == 1.0
        response = MagicMock(spec=HTTPResponse)
        response.__enter__.return_value = response
        response.status = status
        response.read.return_value = body
        return response

    monkeypatch.setattr(urllib.request, "urlopen", open_request)
    assert shard.relay_supports_shard("http://localhost:4001/") is expected


@pytest.mark.parametrize("error", [urllib.error.URLError("offline"), TimeoutError(), ValueError("bad URL")])
def test_relay_capability_errors_fall_back(monkeypatch, error):
    def fail(request, timeout):
        assert timeout == 0.1
        raise error

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    assert shard.relay_supports_shard("http://localhost:4001", timeout=0.1) is False


@pytest.mark.parametrize("primary,fallback,expected", [
    (None, None, None), (None, "fallback", "fallback"),
    ("primary", "fallback", "primary"), ("", "fallback", "fallback"),
])
def test_proxy_key_precedence(monkeypatch, primary, fallback, expected):
    for name, value in (("CPOOL_SHARD_KEY", primary), ("CLI_PROXY_API_KEY", fallback)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert shard.key() == expected


@pytest.mark.parametrize("status", [200, 201, 204, 401, 500])
def test_probe_requires_http_200(monkeypatch, status):
    def open_request(request, timeout):
        assert request.full_url == "http://localhost:8317/v1/models"
        assert request.get_method() == "GET"
        assert request.get_header("X-api-key") == "secret"
        assert timeout == 2.0
        response = MagicMock(spec=HTTPResponse)
        response.__enter__.return_value = response
        response.status = status
        return response

    monkeypatch.setattr(urllib.request, "urlopen", open_request)
    assert shard.probe("http://localhost:8317/", "secret") is (status == 200)


@pytest.mark.parametrize("error", [
    urllib.error.URLError("offline"), TimeoutError(),
    urllib.error.HTTPError("http://localhost", 401, "unauthorized", Message(), io.BytesIO()),
    ValueError("invalid URL"),
])
def test_probe_errors_are_unreachable(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    assert shard.probe("http://localhost:8317", None, timeout=0.1) is False
