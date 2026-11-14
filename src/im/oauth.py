"""OAuth 2.1 for the public MCP listener (docs/design/m5-lenses-and-mcp.md, slice 2): Idea Machine is its own small
authorization server, as Lattice was (lattice 6def765, src/lattice/oauth.py).

claude.ai, the Claude apps and ChatGPT reach MCP servers from their own clouds and can't send a bearer header of my
choosing, so they sign in the standard MCP way:

1. /mcp answers 401 with a pointer to the protected resource metadata (RFC 9728), which names this server.
2. The client reads the authorization server metadata (RFC 8414) and registers itself (RFC 7591).
3. My browser opens /authorize: I enter the web token, choose read-only or read and write, and go back to the client
   with a code (PKCE S256 only), and `iss` (RFC 9207) on every answer, errors included.
4. The client trades the code for an access token (1 hour) and a refresh token (30 days, rotated on every use).

Nothing is stored. Clients, requests, codes and tokens are signed with a key derived from IM_WEB_TOKEN, so they
survive restarts, and changing the token revokes everything (as it logs every device out). Process memory holds only
what must happen once: codes already used, and each refresh token family's latest generation, so a refresh token used
twice revokes its family. After a restart that memory is empty and the next refresh is trusted.

Redirects go only to Claude's hosted callbacks and ChatGPT's stable one. Demo mode is the app's setting, so there is
one resource, /mcp.

The listener's settings: IM_PUBLIC_URL (https://host, no path: where the clients reach it, and the issuer; unset, no
public listener), IM_MCP_ALLOW (the networks whose requests may reach anything but the sign-in page, by the address
Cloudflare reports; default Anthropic's outbound range), and IM_MCP_ALLOW_FILE (more of them, one per line: ChatGPT's
published ranges, which install/nas.sh fetches on every deploy).
"""

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import threading
import time
from pathlib import Path

REDIRECTS = {"https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback",
             "https://chatgpt.com/connector_platform_oauth_redirect"}
READ, WRITE = "ideamachine:read", "ideamachine:write"
MCP = "/mcp"
ACCESS_TTL, REFRESH_TTL, CODE_TTL, REQUEST_TTL = 3600, 30 * 24 * 3600, 300, 900
LOGIN_FAILURES_PER_HOUR = 10
# Anthropic's outbound range for MCP calls to external servers (platform.claude.com/docs/en/api/ip-addresses).
ANTHROPIC_OUTBOUND = "160.79.104.0/21"


class OAuthError(Exception):
    def __init__(self, error: str, description: str, status: int = 400):
        super().__init__(description)
        self.error, self.description, self.status = error, description, status


class Redirect(Exception):
    """An error to send back to the (validated) redirect URI."""

    def __init__(self, uri: str, state: str | None, error: str, description: str):
        super().__init__(description)
        self.uri, self.state, self.error, self.description = uri, state, error, description


# --- The listener's settings -----------------------------------------------------------------------------------

def public_url() -> str | None:
    """IM_PUBLIC_URL, checked; None when it's unset (no public listener)."""
    url = os.environ.get("IM_PUBLIC_URL", "").strip().rstrip("/")
    if not url:
        return None
    if not url.startswith("https://") or url.count("/") != 2:
        raise SystemExit("IM_PUBLIC_URL must be https://<host>, with no path (e.g. https://im-mcp.example.com)")
    return url


_allow_cache: dict = {}


def mcp_allow() -> list:
    """The networks allowed past the sign-in page: IM_MCP_ALLOW, and IM_MCP_ALLOW_FILE's lines (read again when the
    file changes). A malformed entry stops the server."""
    names = [n.strip() for n in os.environ.get("IM_MCP_ALLOW", ANTHROPIC_OUTBOUND).split(",") if n.strip()]
    path = os.environ.get("IM_MCP_ALLOW_FILE", "")
    if path:
        try:
            stamp = os.stat(path).st_mtime_ns
        except OSError:
            stamp = None
        if _allow_cache.get("key") != (path, stamp):
            lines = Path(path).read_text().split() if stamp is not None else []
            _allow_cache.update(key=(path, stamp), lines=lines)
        names += _allow_cache["lines"]
    try:
        return [ipaddress.ip_network(n) for n in names]
    except ValueError as e:
        raise SystemExit(f"IM_MCP_ALLOW or IM_MCP_ALLOW_FILE: {e}") from None


def source_ok(address: str | None) -> bool:
    """The client address Cloudflare reports, in an allowed network. Missing means it didn't come through the
    tunnel."""
    try:
        ip = ipaddress.ip_address((address or "").strip())
    except ValueError:
        return False
    return any(ip in net for net in mcp_allow())


# --- Signing -----------------------------------------------------------------------------------------------------

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _key() -> bytes:
    from im import web

    return hmac.new(web._token().encode(), b"ideamachine-oauth-v1", hashlib.sha256).digest()


def sign(typ: str, ttl: int | None, **claims) -> str:
    body = {"typ": typ, **claims, **({"exp": int(time.time()) + ttl} if ttl else {})}
    payload = _b64(json.dumps(body, separators=(",", ":")).encode())
    return payload + "." + _b64(hmac.new(_key(), payload.encode(), hashlib.sha256).digest())


def verify(typ: str, value: str) -> dict | None:
    """The claims of a value this server signed with this type and that hasn't expired; else None."""
    payload, _, mac = str(value or "").partition(".")
    try:
        if not hmac.compare_digest(_unb64(mac), hmac.new(_key(), payload.encode(), hashlib.sha256).digest()):
            return None
        claims = json.loads(_unb64(payload))
    except (ValueError, TypeError):
        return None
    if claims.get("typ") != typ or claims.get("exp", float("inf")) < time.time():
        return None
    return claims


def _cid(client_id: str) -> str:
    return hashlib.sha256(client_id.encode()).hexdigest()[:32]


# --- What has to happen once --------------------------------------------------------------------------------------

_lock = threading.Lock()
_used_codes: dict = {}      # code nonce -> expiry
_families: dict = {}        # refresh family -> latest generation, or None once revoked
_failures: list = []        # times of wrong tokens at /authorize


def _spend_code(nonce: str, exp: int) -> bool:
    with _lock:
        now = time.time()
        for n in [n for n, e in _used_codes.items() if e < now]:
            del _used_codes[n]
        if nonce in _used_codes:
            return False
        _used_codes[nonce] = exp
        return True


def login_allowed() -> bool:
    with _lock:
        _failures[:] = [t for t in _failures if t > time.time() - 3600]
        return len(_failures) < LOGIN_FAILURES_PER_HOUR


def login_failed() -> None:
    with _lock:
        _failures.append(time.time())


# --- Metadata --------------------------------------------------------------------------------------------------------

def protected_resource(base: str) -> dict:
    return {"resource": base + MCP, "authorization_servers": [base], "scopes_supported": [READ, WRITE],
            "bearer_methods_supported": ["header"], "resource_name": "Idea Machine"}


def authorization_server(base: str) -> dict:
    return {"issuer": base, "authorization_endpoint": base + "/authorize", "token_endpoint": base + "/token",
            "registration_endpoint": base + "/register", "scopes_supported": [READ, WRITE],
            "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
            "authorization_response_iss_parameter_supported": True}


def challenge_header(base: str, error: str | None = None) -> str:
    return (f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource", scope="{READ} {WRITE}"'
            + (f', error="{error}"' if error else ""))


# --- Registration (RFC 7591) ---------------------------------------------------------------------------------------

def register(meta: dict) -> dict:
    uris = meta.get("redirect_uris")
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
        raise OAuthError("invalid_redirect_uri", "redirect_uris is required")
    if not set(uris) <= REDIRECTS:
        raise OAuthError("invalid_redirect_uri", "only Claude's and ChatGPT's hosted callbacks can sign in here")
    if meta.get("token_endpoint_auth_method", "none") != "none":
        raise OAuthError("invalid_client_metadata", "only public clients (token_endpoint_auth_method none)")
    name = str(meta.get("client_name") or "an MCP client")[:80]
    client_id = sign("client", None, uris=sorted(set(uris)), name=name, n=secrets.token_hex(4))
    return {"client_id": client_id, "client_id_issued_at": int(time.time()), "client_name": name,
            "redirect_uris": sorted(set(uris)), "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}


def client(client_id: str) -> dict:
    found = verify("client", client_id)
    if found is None:
        raise OAuthError("invalid_client", "unknown client; remove the connector and add it again", 401)
    return found


# --- Authorization -------------------------------------------------------------------------------------------------

def _resource_ok(base: str, value) -> bool:
    return value in (None, "", base + MCP)


def check_request(base: str, q: dict) -> tuple[dict, str]:
    """Validate /authorize's query. Returns (the client, a signed request for the consent form). Errors about the
    client or redirect URI must be shown, not redirected (the redirect URI can't be trusted yet)."""
    c = client(q.get("client_id", ""))
    redirect = q.get("redirect_uri", "")
    if redirect not in c["uris"]:
        raise OAuthError("invalid_request", "redirect_uri isn't one this client registered")
    if q.get("response_type") != "code":
        raise Redirect(redirect, q.get("state"), "unsupported_response_type", "only response_type=code")
    if q.get("code_challenge_method") != "S256" or not 43 <= len(q.get("code_challenge", "")) <= 128:
        raise Redirect(redirect, q.get("state"), "invalid_request", "PKCE with S256 is required")
    if not _resource_ok(base, q.get("resource")):
        raise Redirect(redirect, q.get("state"), "invalid_target", "unknown resource")
    scopes = set((q.get("scope") or f"{READ} {WRITE}").split()) & {READ, WRITE} or {READ}
    req = sign("request", REQUEST_TTL, cid=_cid(q["client_id"]), redirect=redirect, challenge=q["code_challenge"],
               state=q.get("state"), scopes=sorted(scopes | {READ}))
    return c, req


def grant(request: str, allow_writes: bool) -> dict:
    """After I've entered the token: the code, for the redirect. Raises OAuthError if the form is stale or forged."""
    r = verify("request", request)
    if r is None:
        raise OAuthError("invalid_request", "this sign-in page expired; start again from the app")
    scopes = [s for s in r["scopes"] if s == READ or (s == WRITE and allow_writes)]
    code = sign("code", CODE_TTL, cid=r["cid"], redirect=r["redirect"], challenge=r["challenge"], scopes=scopes,
                n=secrets.token_hex(8))
    return {"redirect": r["redirect"], "state": r["state"], "code": code}


# --- Tokens ----------------------------------------------------------------------------------------------------------

def _tokens(cid: str, scopes: list, family: str, gen: int) -> dict:
    with _lock:
        _families[family] = gen
    return {"access_token": sign("access", ACCESS_TTL, cid=cid, scopes=scopes, res=MCP),
            "token_type": "Bearer", "expires_in": ACCESS_TTL, "scope": " ".join(scopes),
            "refresh_token": sign("refresh", REFRESH_TTL, cid=cid, scopes=scopes, fam=family, gen=gen, res=MCP)}


def token(base: str, form: dict) -> dict:
    grant_type = form.get("grant_type")
    client_id = form.get("client_id", "")
    client(client_id)
    cid = _cid(client_id)
    if not _resource_ok(base, form.get("resource")):
        raise OAuthError("invalid_target", "unknown resource")
    if grant_type == "authorization_code":
        code = verify("code", form.get("code", ""))
        if code is None or code["cid"] != cid or code["redirect"] != form.get("redirect_uri"):
            raise OAuthError("invalid_grant", "the code is invalid, expired, or not this client's")
        verifier = form.get("code_verifier", "")
        if not hmac.compare_digest(_b64(hashlib.sha256(verifier.encode()).digest()), code["challenge"]):
            raise OAuthError("invalid_grant", "code_verifier doesn't match the code_challenge")
        if not _spend_code(code["n"], code["exp"]):
            raise OAuthError("invalid_grant", "the code was already used")
        return _tokens(cid, code["scopes"], secrets.token_hex(8), 1)
    if grant_type == "refresh_token":
        r = verify("refresh", form.get("refresh_token", ""))
        if r is None or r["cid"] != cid:
            raise OAuthError("invalid_grant", "the refresh token is invalid, expired, or not this client's")
        with _lock:
            latest = _families.get(r["fam"], 0)
            if latest is None or latest > r["gen"]:   # revoked, or an old token used again: revoke the family
                _families[r["fam"]] = None
                raise OAuthError("invalid_grant", "the refresh token was already used")
        return _tokens(cid, r["scopes"], r["fam"], r["gen"] + 1)
    raise OAuthError("unsupported_grant_type", "authorization_code or refresh_token")


def access(header: str | None) -> dict | None:
    """The claims of a valid access token for /mcp in an Authorization header, else None."""
    scheme, _, value = (header or "").partition(" ")
    claims = verify("access", value.strip()) if scheme.lower() == "bearer" else None
    return claims if claims and claims.get("res") == MCP else None
