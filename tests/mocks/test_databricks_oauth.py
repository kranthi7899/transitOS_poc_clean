"""Credential-cache boundaries without connecting or requiring the optional driver."""
import stat
from types import SimpleNamespace

import pytest

from transitos.mocks.databricks_oauth import LocalOAuthCache


def test_cache_is_private_and_replaces_atomically(tmp_path):
    path = tmp_path / 'private' / 'oauth.json'
    cache = LocalOAuthCache(path)
    cache.persist('example', SimpleNamespace(access_token='synthetic', refresh_token='synthetic'))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    cache.persist('example', SimpleNamespace(access_token='new-synthetic', refresh_token='synthetic'))
    assert list(path.parent.iterdir()) == [path]


def test_cache_refuses_broad_permissions_and_wrong_host(tmp_path):
    path = tmp_path / 'oauth.json'
    cache = LocalOAuthCache(path)
    assert cache.read('example') is None
    cache.persist('example', SimpleNamespace(access_token='synthetic', refresh_token='synthetic'))
    with pytest.raises(ValueError, match='hostname'):
        cache.read('other')
    path.chmod(0o644)
    with pytest.raises(ValueError, match='owner-only'):
        cache.read('example')
