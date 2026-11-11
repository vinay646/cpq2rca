"""Salesforce OAuth 2.0 web server flow with PKCE, for the "Connect Salesforce org"
button in tester mode.

Configuration (environment variables):
    SF_OAUTH_CLIENT_ID       consumer key of the Connected App / External Client App (turns tester mode on)
    SF_OAUTH_CLIENT_SECRET   consumer secret (only if the app requires a secret for the web server flow)
    SF_OAUTH_CALLBACK_URL    full callback URL registered on the app, e.g.
                             https://cpq.example.com/oauth/callback (default: derived from the request)
    CPQ2RCA_ALLOWED_USERS    optional comma-separated allowlist: usernames/emails ("ana@acme.com"),
                             email domains ("@acme.com") or org IDs ("00D...")

Security notes:
  * Only Salesforce login hosts are accepted (login/test.salesforce.com and *.my.salesforce.com),
    so the token request (which may carry the client secret) can never go to another host.
  * State and PKCE verifier are single-use and kept in the signed session.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

import requests

LOGIN_HOSTS = {"production": "login.salesforce.com", "sandbox": "test.salesforce.com"}
_MY_DOMAIN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?(?:\.sandbox|\.develop|\.scratch)?\.my\.salesforce\.com$")
_ID_PATH = re.compile(r"^/id/(00D[A-Za-z0-9]{12,15})/(005[A-Za-z0-9]{12,15})/?$")
SCOPES = "api refresh_token"


class OAuthError(Exception):
    """Shown to the user; never contains tokens or secrets."""


@dataclass(frozen=True)
class OAuthConfig:
    client_id: str
    client_secret: str | None
    callback_url: str | None
    allowed: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "OAuthConfig | None":
        cid = os.environ.get("SF_OAUTH_CLIENT_ID")
        if not cid:
            return None
        allowed = tuple(a.strip().lower() for a in os.environ.get("CPQ2RCA_ALLOWED_USERS", "").split(",") if a.strip())
        return cls(cid, os.environ.get("SF_OAUTH_CLIENT_SECRET") or None,
                   os.environ.get("SF_OAUTH_CALLBACK_URL") or None, allowed)


def login_host(env: str, custom: str | None = None) -> str:
    """Validated Salesforce login host for 'production', 'sandbox' or a My Domain."""
    if env in LOGIN_HOSTS:
        return LOGIN_HOSTS[env]
    host = (custom or "").strip().lower()
    host = re.sub(r"^https?://", "", host).split("/")[0]
    if not _MY_DOMAIN.fullmatch(host):
        raise OAuthError("Enter your My Domain login host, for example acme.my.salesforce.com "
                         "or acme--uat.sandbox.my.salesforce.com.")
    return host


def is_salesforce_url(url: str | None) -> bool:
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (host == "salesforce.com" or host.endswith(".salesforce.com"))


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(cfg: OAuthConfig, host: str, redirect_uri: str, state: str, challenge: str) -> str:
    q = {"response_type": "code", "client_id": cfg.client_id, "redirect_uri": redirect_uri,
         "scope": SCOPES, "state": state, "code_challenge": challenge,
         "code_challenge_method": "S256", "prompt": "login consent"}
    return f"https://{host}/services/oauth2/authorize?{urlencode(q)}"


def _token_request(host: str, data: dict) -> dict:
    try:
        r = requests.post(f"https://{host}/services/oauth2/token", data=data, timeout=30)
    except requests.RequestException as e:
        raise OAuthError(f"Could not reach Salesforce ({e.__class__.__name__}).") from e
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200:
        desc = body.get("error_description") or body.get("error") or f"HTTP {r.status_code}"
        raise OAuthError(f"Salesforce refused the login: {desc}")
    return body


def exchange_code(cfg: OAuthConfig, host: str, code: str, redirect_uri: str, verifier: str) -> dict:
    """Swap the authorization code for tokens and identify the user.

    Returns {access_token, refresh_token, instance_url, login_host, org_id, user_id,
    username, display_name, org_name}.
    """
    data = {"grant_type": "authorization_code", "code": code, "client_id": cfg.client_id,
            "redirect_uri": redirect_uri, "code_verifier": verifier}
    if cfg.client_secret:
        data["client_secret"] = cfg.client_secret
    body = _token_request(host, data)
    instance_url, id_url = body.get("instance_url"), body.get("id")
    if not body.get("access_token") or not is_salesforce_url(instance_url) or not is_salesforce_url(id_url):
        raise OAuthError("Salesforce returned an unexpected login response.")
    m = _ID_PATH.match(urlsplit(id_url).path)
    if not m:
        raise OAuthError("Salesforce returned an unexpected identity.")
    conn = {"access_token": body["access_token"], "refresh_token": body.get("refresh_token"),
            "instance_url": instance_url.rstrip("/"), "login_host": host,
            "org_id": m.group(1)[:15], "user_id": m.group(2)[:15],
            "username": "", "display_name": "", "org_name": ""}
    try:  # best effort: names for display and the allowlist
        r = requests.get(id_url, headers={"Authorization": f"Bearer {body['access_token']}"}, timeout=30)
        if r.status_code == 200:
            ident = r.json()
            conn["username"] = ident.get("username") or ""
            conn["display_name"] = ident.get("display_name") or conn["username"]
            conn["email"] = ident.get("email") or ""
    except (requests.RequestException, ValueError):
        pass
    return conn


def is_allowed(cfg: OAuthConfig, conn: dict) -> bool:
    if not cfg.allowed:
        return True
    names = {(conn.get("username") or "").lower(), (conn.get("email") or "").lower()} - {""}
    org = conn.get("org_id", "").lower()
    for rule in cfg.allowed:
        if rule.startswith("00d") and org and rule[:15] == org[:15]:
            return True
        if rule.startswith("@") and any(n.endswith(rule) for n in names):
            return True
        if rule in names:
            return True
    return False


def refresh(cfg: OAuthConfig, conn: dict) -> str:
    """New access token from the refresh token (used when a run outlives the session)."""
    if not conn.get("refresh_token"):
        raise OAuthError("Session expired and no refresh token was granted; connect the org again.")
    data = {"grant_type": "refresh_token", "refresh_token": conn["refresh_token"], "client_id": cfg.client_id}
    if cfg.client_secret:
        data["client_secret"] = cfg.client_secret
    body = _token_request(conn["login_host"], data)
    conn["access_token"] = body["access_token"]
    return body["access_token"]


def revoke(conn: dict) -> None:
    token = conn.get("refresh_token") or conn.get("access_token")
    if not token:
        return
    try:
        requests.post(f"https://{conn['login_host']}/services/oauth2/revoke", data={"token": token}, timeout=15)
    except requests.RequestException:
        pass


def owner_key(conn: dict) -> str:
    """Stable identity of a tester: Salesforce org + user (15-character IDs)."""
    return f"{conn['org_id'][:15]}/{conn['user_id'][:15]}"
