"""The public MCP listener (web.PublicHandler) with OAuth in front (im.oauth): the whole sign-in as claude.ai and
ChatGPT do it, and what's refused. Ported from Lattice's tests/test_public_mcp.py (lattice 6def765)."""

import base64
import hashlib
import http.client
import json
import re
import secrets
import threading
import urllib.parse
from http.server import ThreadingHTTPServer

import pytest

from im import oauth, web

TOKEN = "test-token-0123456789abcdef"
BASE = "https://im-mcp.example.com"
CLAUDE_IP, CHATGPT_IP = "160.79.105.17", "203.0.113.77"
CLAUDE, CHATGPT = "https://claude.ai/api/mcp/auth_callback", "https://chatgpt.com/connector_platform_oauth_redirect"
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}


@pytest.fixture
def ranges(tmp_path):
    """ChatGPT's published ranges, as install/nas.sh writes them: one network a line."""
    path = tmp_path / "chatgpt-ranges.txt"
    path.write_text("203.0.113.0/24\n198.51.100.0/24\n")
    return path


@pytest.fixture
def public(monkeypatch, ranges):
    """The public listener on a loopback port, as `im web` opens it with IM_PUBLIC_URL set: its port."""
    monkeypatch.setenv("IM_WEB_TOKEN", TOKEN)
    monkeypatch.setenv("IM_PUBLIC_URL", BASE)
    monkeypatch.setenv("IM_MCP_ALLOW_FILE", str(ranges))
    monkeypatch.delenv("IM_MCP_ALLOW", raising=False)
    oauth._failures.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.PublicHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    thread.join()
    server.server_close()


def call(port, path, method="GET", ip=CLAUDE_IP, json_body=None, form=None, headers=None):
    """(status, headers, body) of one request, as Cloudflare passes it on (CF-Connecting-IP), redirects not followed."""
    body, kind = None, None
    if json_body is not None:
        body, kind = json.dumps(json_body), "application/json"
    elif form is not None:
        body, kind = urllib.parse.urlencode(form), "application/x-www-form-urlencoded"
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, body=body, headers={**({"Content-Type": kind} if kind else {}),
                                                **({"CF-Connecting-IP": ip} if ip else {}), **(headers or {})})
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read()


def sign_in(port, writes=True, callback=CLAUDE, ip=CLAUDE_IP, scope=f"{oauth.READ} {oauth.WRITE}"):
    """Register, consent and trade the code, as the client does. Returns the token response."""
    status, _, body = call(port, "/register", "POST", ip=ip, json_body={
        "redirect_uris": [callback], "client_name": "Claude", "token_endpoint_auth_method": "none"})
    assert status == 201, body
    client_id = json.loads(body)["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    q = urllib.parse.urlencode({"response_type": "code", "client_id": client_id, "redirect_uri": callback,
                                "code_challenge": challenge, "code_challenge_method": "S256", "state": "st8",
                                "scope": scope, "resource": BASE + "/mcp"})
    status, headers, page = call(port, f"/authorize?{q}", ip="192.0.2.9")   # my browser, anywhere
    assert status == 200 and headers["X-Frame-Options"] == "DENY"
    assert urllib.parse.urlsplit(callback).hostname.encode() in page and b'name="writes"' in page
    request = re.search(rb'name="request" value="([^"]+)"', page).group(1).decode()
    form = {"request": request, "token": TOKEN, **({"writes": "on"} if writes else {})}
    status, headers, _ = call(port, "/authorize", "POST", ip="192.0.2.9", form=form)
    assert status == 303
    back = urllib.parse.urlsplit(headers["Location"])
    params = dict(urllib.parse.parse_qsl(back.query))
    assert f"{back.scheme}://{back.netloc}{back.path}" == callback and params["state"] == "st8"
    assert params["iss"] == BASE   # RFC 9207: what lets ChatGPT use its stable callback
    status, _, body = call(port, "/token", "POST", ip=ip, form={
        "grant_type": "authorization_code", "code": params["code"], "redirect_uri": callback,
        "client_id": client_id, "code_verifier": verifier, "resource": BASE + "/mcp"})
    assert status == 200, body
    return json.loads(body) | {"client_id": client_id, "_code": params["code"], "_verifier": verifier}


def mcp(port, access, message, ip=CLAUDE_IP):
    status, _, body = call(port, "/mcp", "POST", ip=ip, json_body=message,
                           headers={"Authorization": f"Bearer {access}"})
    return status, json.loads(body) if body else None


def test_discovery_starts_from_a_401(public):
    status, headers, _ = call(public, "/mcp", "POST", json_body=PING)
    assert status == 401
    assert headers["WWW-Authenticate"].startswith(
        f'Bearer resource_metadata="{BASE}/.well-known/oauth-protected-resource"')
    prm = json.loads(call(public, "/.well-known/oauth-protected-resource")[2])
    assert prm["resource"] == BASE + "/mcp" and prm["authorization_servers"] == [BASE]
    asm = json.loads(call(public, "/.well-known/oauth-authorization-server")[2])
    assert asm["issuer"] == BASE and asm["code_challenge_methods_supported"] == ["S256"]
    assert asm["authorization_response_iss_parameter_supported"] is True
    assert asm["registration_endpoint"] == BASE + "/register" and "none" in asm["token_endpoint_auth_methods_supported"]


def test_claude_signs_in_and_a_code_works_once(public):
    t = sign_in(public)
    assert t["token_type"] == "Bearer" and t["scope"] == f"{oauth.READ} {oauth.WRITE}"
    assert mcp(public, t["access_token"], PING) == (200, {"jsonrpc": "2.0", "id": 1, "result": {}})
    status, _, body = call(public, "/token", "POST", form={
        "grant_type": "authorization_code", "code": t["_code"], "redirect_uri": CLAUDE, "client_id": t["client_id"],
        "code_verifier": t["_verifier"]})
    assert status == 400 and json.loads(body)["error"] == "invalid_grant"


def test_chatgpt_signs_in_from_its_published_ranges(public):
    t = sign_in(public, callback=CHATGPT, ip=CHATGPT_IP)
    assert mcp(public, t["access_token"], PING, ip=CHATGPT_IP)[0] == 200


def test_a_read_only_grant_has_no_write_tools(public, seeded_lattice, web_dsn, monkeypatch):
    monkeypatch.setattr(web.Handler, "dsn", web_dsn)
    t = sign_in(public, writes=False)
    assert t["scope"] == oauth.READ
    tools = {x["name"] for x in mcp(public, t["access_token"], {"jsonrpc": "2.0", "id": 2,
                                                               "method": "tools/list"})[1]["result"]["tools"]}
    assert "search_ideas" in tools and not tools & {"star_idea", "link_ideas", "note_idea", "add_idea"}
    _, body = mcp(public, t["access_token"], {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                              "params": {"name": "star_idea", "arguments": {"idea_id": "x"}}})
    assert body["error"]["code"] == -32602
    full = sign_in(public)
    _, body = mcp(public, full["access_token"], {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                                 "params": {"name": "list_themes", "arguments": {}}})
    assert body["result"]["structuredContent"]["themes"]   # tools work through it


def test_refresh_tokens_rotate_and_a_reused_one_revokes_the_family(public):
    t = sign_in(public)
    refresh = {"grant_type": "refresh_token", "client_id": t["client_id"]}
    status, _, body = call(public, "/token", "POST", form=refresh | {"refresh_token": t["refresh_token"]})
    assert status == 200
    second = json.loads(body)
    assert second["refresh_token"] != t["refresh_token"] and mcp(public, second["access_token"], PING)[0] == 200
    for old in (t["refresh_token"], second["refresh_token"]):   # the first again: refused, and the family with it
        status, _, body = call(public, "/token", "POST", form=refresh | {"refresh_token": old})
        assert status == 400 and json.loads(body)["error"] == "invalid_grant"


def test_pkce_and_redirects_are_enforced(public):
    status, _, body = call(public, "/register", "POST", json_body={"redirect_uris": ["https://evil.example/cb"]})
    assert status == 400 and json.loads(body)["error"] == "invalid_redirect_uri"
    client_id = json.loads(call(public, "/register", "POST", json_body={"redirect_uris": [CLAUDE]})[2])["client_id"]
    base_q = {"response_type": "code", "client_id": client_id, "redirect_uri": CLAUDE, "state": "s"}
    # No PKCE: sent back to the client with an error, and the issuer.
    status, headers, _ = call(public, "/authorize?" + urllib.parse.urlencode(base_q), ip=None)
    assert status == 303 and "error=invalid_request" in headers["Location"]
    assert dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(headers["Location"]).query))["iss"] == BASE
    # A redirect the client didn't register, or a forged client: shown, never redirected.
    for q in [base_q | {"redirect_uri": "https://evil.example/cb"}, base_q | {"client_id": client_id[:-2] + "xx"}]:
        assert call(public, "/authorize?" + urllib.parse.urlencode(q), ip=None)[0] in (400, 401)


def test_wrong_tokens_are_capped(public):
    client_id = json.loads(call(public, "/register", "POST", json_body={"redirect_uris": [CLAUDE]})[2])["client_id"]
    q = urllib.parse.urlencode({"response_type": "code", "client_id": client_id, "redirect_uri": CLAUDE,
                                "code_challenge": "x" * 43, "code_challenge_method": "S256"})
    page = call(public, f"/authorize?{q}", ip=None)[2]
    request = re.search(rb'name="request" value="([^"]+)"', page).group(1).decode()
    statuses = [call(public, "/authorize", "POST", ip=None, form={"request": request, "token": "wrong"})[0]
                for _ in range(oauth.LOGIN_FAILURES_PER_HOUR + 1)]
    assert statuses[:-1] == [401] * oauth.LOGIN_FAILURES_PER_HOUR and statuses[-1] == 429
    assert call(public, "/authorize", "POST", ip=None, form={"request": request, "token": TOKEN})[0] == 429


def test_only_the_allowed_networks_reach_anything_but_the_sign_in_page(public):
    t = sign_in(public)
    auth_header = {"Authorization": f"Bearer {t['access_token']}"}
    for ip in ("8.8.8.8", None, "not an ip"):
        for path, method in [("/mcp", "POST"), ("/token", "POST"), ("/register", "POST"),
                             ("/.well-known/oauth-authorization-server", "GET")]:
            got = call(public, path, method, ip=ip, json_body={} if method == "POST" else None, headers=auth_header)
            assert (got[0], got[2]) == (404, b""), (path, ip)
    for path in ("/", "/login", "/sky", "/demo", "/idea/x"):   # nothing of the web view
        assert call(public, path)[0] == 404, path
    assert mcp(public, TOKEN, PING)[0] == 401   # the tailnet's bearer token means nothing here


def test_changing_the_token_revokes_every_grant(public, monkeypatch):
    t = sign_in(public)
    monkeypatch.setenv("IM_WEB_TOKEN", TOKEN + "-rotated")
    assert mcp(public, t["access_token"], PING)[0] == 401


def test_settings_are_checked(monkeypatch, ranges):
    monkeypatch.setenv("IM_MCP_ALLOW", "10.0.0.0/8, 2607:6bc0::/48")
    monkeypatch.delenv("IM_MCP_ALLOW_FILE", raising=False)
    assert oauth.source_ok("10.1.2.3") and oauth.source_ok("2607:6bc0::1") and not oauth.source_ok(CLAUDE_IP)
    monkeypatch.setenv("IM_MCP_ALLOW_FILE", str(ranges))
    assert oauth.source_ok(CHATGPT_IP)
    ranges.write_text("198.51.100.0/24\n")   # a refreshed list is read again
    assert not oauth.source_ok(CHATGPT_IP) and oauth.source_ok("198.51.100.1")
    monkeypatch.setenv("IM_MCP_ALLOW", "nonsense")
    with pytest.raises(SystemExit):
        oauth.mcp_allow()
    for bad in ["http://im.example.com", "https://im.example.com/mcp", "im.example.com"]:
        monkeypatch.setenv("IM_PUBLIC_URL", bad)
        with pytest.raises(SystemExit):
            oauth.public_url()
    monkeypatch.setenv("IM_PUBLIC_URL", "https://im.example.com/")
    assert oauth.public_url() == "https://im.example.com"
    monkeypatch.delenv("IM_PUBLIC_URL")
    assert oauth.public_url() is None
