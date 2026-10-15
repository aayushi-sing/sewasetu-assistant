# -*- coding: utf-8 -*-
"""Bearer-token check for the MCP servers: the portal is the authorization server;
each MCP server is just a resource server that verifies the portal's JWT."""
import os
from datetime import datetime

import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken

import core

ISSUER = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
JWKS_URL = os.environ.get("PORTAL_JWKS_URL", ISSUER + "/.well-known/jwks.json")
_jwks = PyJWKClient(JWKS_URL, cache_keys=True, lifespan=3600)


class PortalTokenVerifier:
    """Accepts a token only if: signed by the portal, unexpired, issued by the portal,
    minted for THIS server (aud == resource url), carrying the right scope and role,
    and not signed out since it was issued."""

    def __init__(self, resource_url, scope, role, subject_prefix):
        self.resource_url = resource_url.rstrip("/")
        self.scope, self.role, self.prefix = scope, role, subject_prefix

    async def verify_token(self, token):
        try:
            key = _jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"], issuer=ISSUER,
                                audience=[self.resource_url, self.resource_url + "/"])
        except Exception:
            return None
        if claims.get("kind", "access") != "access":
            return None
        if self.scope not in str(claims.get("scope", "")).split() or claims.get("role") != self.role:
            return None
        sub = claims.get("sub", "")
        if not sub.startswith(self.prefix):
            return None
        out = core.signed_out_after(sub)
        if out and claims.get("iat", 0) <= int(out.timestamp()):
            return None
        return AccessToken(token=token, client_id=claims.get("client_id", ""),
                           scopes=[self.scope], expires_at=claims.get("exp"),
                           resource=self.resource_url, subject=sub, claims=claims)
