"""Opt-in development OAuth cache shared by the owner's local product processes."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class LocalOAuthCache:
    def __init__(self, path):
        self.path = Path(path)

    def persist(self, hostname, token):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump({'hostname': hostname, 'access_token': token.access_token,
                           'refresh_token': token.refresh_token}, stream)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def read(self, hostname):
        try:
            if self.path.stat().st_mode & 0o077:
                raise ValueError('OAuth cache must be owner-only')
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return None
        if data['hostname'] != hostname:
            raise ValueError('OAuth cache hostname mismatch')
        from databricks.sql.experimental.oauth_persistence import OAuthToken

        return OAuthToken(data['access_token'], data['refresh_token'])
