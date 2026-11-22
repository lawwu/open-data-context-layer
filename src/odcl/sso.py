"""Single sign-on for `odcl serve` through any OpenID Connect provider (Microsoft Entra ID, Okta, Google Workspace,
Auth0, Keycloak...), as html-server does it: the authorization code flow with PKCE, a state and a nonce; the provider
found by discovery; its ID token checked (signature against the provider's keys, issuer, audience, expiry, nonce).
Nothing of the provider's is kept: the server reads who signed in, then runs its own session (a cookie, and its
digest in `sessions`). It is on when ODCL_OIDC_ISSUER, ODCL_OIDC_CLIENT_ID and ODCL_OIDC_CLIENT_SECRET are set, and
needs the `sso` extra (PyJWT).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .store import CatalogError
from .users import ROLES, rank

LOGIN_COOKIE = "odcl_oidc"      # the sign-in under way: state, PKCE verifier, nonce, where to go after
SESSION_COOKIE = "odcl_session"
LOGIN_MINUTES = 10
CALLBACK = "/auth/callback"


@dataclass(frozen=True)
class Settings:
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str = ""            # default: this server's /auth/callback, as the browser reached it
    post_logout_redirect_uri: str = ""  # default: this server's /
    scopes: str = "openid profile email"
    session_hours: float = 12
    token_idle_days: float = 30       # a single sign-on user's API tokens stop working after this long without signing in (0: never)
    default_role: str = "reader"      # the role of someone signing in for the first time, unless the next two say more
    admin_emails: tuple[str, ...] = ()
    role_claim: str = ""              # e.g. roles
    role_values: tuple[tuple[str, str], ...] = ()  # (claim value, role), e.g. (("Odcl.Editor", "editor"), ("Odcl.Admin", "admin"))
    allow_insecure_http: bool = False  # a provider on plain http: local testing only


def _list(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").replace(";", ",").split(",") if v.strip()]


def _number(env, name: str, default: float) -> float:
    value = env.get(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        raise CatalogError("sso", f"{name} is a number") from None


def settings(env=None) -> Settings | None:
    """Single sign-on's settings from the environment, or None when it is not set up."""
    env = os.environ if env is None else env
    required = ("ODCL_OIDC_ISSUER", "ODCL_OIDC_CLIENT_ID", "ODCL_OIDC_CLIENT_SECRET")
    present = [n for n in required if env.get(n)]
    if not present:
        return None
    if len(present) < len(required):
        raise CatalogError("sso", f"Single sign-on is half set up: set {', '.join(n for n in required if n not in present)}, or unset all three")
    values = []
    for pair in _list(env.get("ODCL_OIDC_ROLE_VALUES")):
        value, _, role = pair.partition("=")
        if role.strip() not in ROLES or not value.strip():
            raise CatalogError("sso", f"ODCL_OIDC_ROLE_VALUES pairs a claim value with a role, like Odcl.Editor=editor; {pair!r} is not one")
        values.append((value.strip(), role.strip()))
    default = env.get("ODCL_OIDC_DEFAULT_ROLE") or "reader"
    if default not in ROLES or default == "admin":
        raise CatalogError("sso", "ODCL_OIDC_DEFAULT_ROLE is reader, editor or importer")
    return Settings(issuer=env["ODCL_OIDC_ISSUER"].rstrip("/"), client_id=env["ODCL_OIDC_CLIENT_ID"], client_secret=env["ODCL_OIDC_CLIENT_SECRET"],
                    redirect_uri=env.get("ODCL_OIDC_REDIRECT_URI", ""), post_logout_redirect_uri=env.get("ODCL_OIDC_POST_LOGOUT_REDIRECT_URI", ""),
                    scopes=" ".join(_list(env.get("ODCL_OIDC_SCOPES", "").replace(" ", ","))) or "openid profile email",
                    session_hours=_number(env, "ODCL_OIDC_SESSION_HOURS", 12) or 12, token_idle_days=_number(env, "ODCL_OIDC_TOKEN_IDLE_DAYS", 30),
                    default_role=default, admin_emails=tuple(e.lower() for e in _list(env.get("ODCL_OIDC_ADMIN_EMAILS"))),
                    role_claim=env.get("ODCL_OIDC_ROLE_CLAIM", ""), role_values=tuple(values),
                    allow_insecure_http=env.get("ODCL_OIDC_ALLOW_INSECURE_HTTP") == "true")


# ---- the provider ---------------------------------------------------------------------------------------------
_discovered: dict = {}
_lock = threading.Lock()


def _fetch(url: str, s: Settings, data: dict | None = None, auth: str | None = None) -> dict:
    if not (url.startswith("https://") or (s.allow_insecure_http and url.startswith("http://"))):
        raise CatalogError("sso", f"The provider's address is not https: {url}")
    headers = {"Accept": "application/json", **({"Authorization": auth} if auth else {})}
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    request = urllib.request.Request(url, data=body, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - the scheme is checked above
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        raise CatalogError("sso", f"The provider answered {e.code} from {url}") from None
    except (OSError, ValueError) as e:
        raise CatalogError("sso", f"Could not reach the provider at {url}: {e}") from None


def provider(s: Settings) -> dict:
    """The provider's metadata, by discovery, fetched once per issuer and client; and a client for its signing keys."""
    key = (s.issuer, s.client_id)
    with _lock:
        if key not in _discovered:
            import jwt
            meta = _fetch(f"{s.issuer}/.well-known/openid-configuration", s)
            if meta.get("issuer", "").rstrip("/") != s.issuer:
                raise CatalogError("sso", f"The provider at {s.issuer} says its issuer is {meta.get('issuer')}")
            for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                if not isinstance(meta.get(field), str):
                    raise CatalogError("sso", f"The provider's metadata has no {field}")
            _fetch(meta["jwks_uri"], s)  # its scheme is checked like every other address of the provider's
            _discovered[key] = {**meta, "keys": jwt.PyJWKClient(meta["jwks_uri"], cache_keys=True)}
        return _discovered[key]


def forget_provider() -> None:
    """Forget every provider's metadata (tests point the same settings at a fresh fake provider)."""
    with _lock:
        _discovered.clear()


# ---- signing in -----------------------------------------------------------------------------------------------
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# The sign-in cookie is signed, so a cookie planted from a sibling subdomain is refused. The key is
# $ODCL_SESSION_SECRET, else made when the server starts: a restart in the middle of a sign-in asks for it again.
_KEY = (os.environ.get("ODCL_SESSION_SECRET") or "").encode() or secrets.token_bytes(32)
_MAC = 43  # the length of an HMAC-SHA256 in unpadded base64


def _sign(value: str) -> str:
    return value + _b64(hmac.new(_KEY, value.encode(), hashlib.sha256).digest())


def _signed(raw: str | None) -> str | None:
    if not raw or len(raw) <= _MAC:
        return None
    value, mac = raw[:-_MAC], raw[-_MAC:]
    return value if hmac.compare_digest(mac, _b64(hmac.new(_KEY, value.encode(), hashlib.sha256).digest())) else None


def redirect_uri(s: Settings, origin: str) -> str:
    return s.redirect_uri or f"{origin}{CALLBACK}"


def safe_next(value: str | None) -> str:
    """Only a path on this server: never another site (an open redirect), nor the sign-in's own addresses."""
    v = str(value or "")
    if not v.startswith("/") or v.startswith("//") or v.startswith("/\\") or any(ord(c) < 32 for c in v) or v.startswith("/auth/"):
        return "/"
    return v[:2000]


def cookie(name: str, value: str, *, max_age: int, path: str = "/", secure: bool) -> str:
    return f"{name}={value}; Path={path}; Max-Age={max_age}; HttpOnly; SameSite=Lax{'; Secure' if secure else ''}"


def read_cookie(header: str | None, name: str) -> str | None:
    for part in (header or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name and value and all(c.isalnum() or c in "-_" for c in value):
            return value
    return None


def start_login(s: Settings, origin: str, next_path: str | None) -> tuple[str, str]:
    """The provider's sign-in address, and the cookie that holds this sign-in's secrets."""
    meta = provider(s)
    verifier, state, nonce = secrets.token_urlsafe(48), secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    query = {"client_id": s.client_id, "redirect_uri": redirect_uri(s, origin), "response_type": "code", "scope": s.scopes,
             "state": state, "nonce": nonce, "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"}
    endpoint = meta["authorization_endpoint"]
    url = endpoint + ("&" if "?" in endpoint else "?") + urllib.parse.urlencode(query)
    value = _sign(_b64(json.dumps({"state": state, "verifier": verifier, "nonce": nonce, "next": safe_next(next_path)}).encode()))
    return url, cookie(LOGIN_COOKIE, value, max_age=LOGIN_MINUTES * 60, path="/auth", secure=origin.startswith("https:"))


def clear_login_cookie(secure: bool) -> str:
    return cookie(LOGIN_COOKIE, "", max_age=0, path="/auth", secure=secure)


def finish_login(s: Settings, origin: str, query: dict, cookie_header: str | None) -> dict:
    """Back from the provider: check its answer against this browser's sign-in (state, PKCE verifier, nonce) and the
    ID token, and say who signed in. Raises CatalogError("sign_in") when the sign-in did not start in this browser."""
    import jwt
    raw = _signed(read_cookie(cookie_header, LOGIN_COOKIE))
    try:
        started = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))) if raw else None
    except (ValueError, UnicodeDecodeError):
        started = None  # tampered: as if missing
    if not isinstance(started, dict) or not all(isinstance(started.get(k), str) for k in ("state", "verifier", "nonce")):
        raise CatalogError("sign_in", "This sign-in did not start in this browser")
    if not secrets.compare_digest(str(query.get("state", "")), started["state"]) or not query.get("code"):
        raise CatalogError("sign_in", "This sign-in did not start in this browser")
    meta = provider(s)
    form = {"grant_type": "authorization_code", "code": query["code"], "redirect_uri": redirect_uri(s, origin), "code_verifier": started["verifier"]}
    methods = meta.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    if "client_secret_basic" in methods:
        quote = lambda v: urllib.parse.quote(v, safe="")
        auth = "Basic " + base64.b64encode(f"{quote(s.client_id)}:{quote(s.client_secret)}".encode()).decode()
    else:
        form |= {"client_id": s.client_id, "client_secret": s.client_secret}
        auth = None
    tokens = _fetch(meta["token_endpoint"], s, form, auth)
    id_token = tokens.get("id_token")
    if not isinstance(id_token, str):
        raise CatalogError("sso", "The provider sent no ID token")
    algorithms = [a for a in meta.get("id_token_signing_alg_values_supported") or ["RS256"] if a not in ("none", "HS256", "HS384", "HS512")]
    try:
        key = meta["keys"].get_signing_key_from_jwt(id_token).key
        claims = jwt.decode(id_token, key, algorithms=algorithms, audience=s.client_id, issuer=meta["issuer"],
                            options={"require": ["exp", "iat", "iss", "aud", "sub"]}, leeway=60)
    except jwt.PyJWTError as e:
        raise CatalogError("sso", f"The provider's ID token does not check out: {e}") from None
    if not secrets.compare_digest(str(claims.get("nonce", "")), started["nonce"]):
        raise CatalogError("sso", "The provider's ID token is for another sign-in (its nonce differs)")
    if isinstance(claims.get("aud"), list) and len(claims["aud"]) > 1 and claims.get("azp") != s.client_id:
        raise CatalogError("sso", "The provider's ID token was issued to another client")
    text = lambda v: v if isinstance(v, str) and v else None
    return {"issuer": claims["iss"], "sub": str(claims["sub"]), "email": text(claims.get("email")), "email_verified": claims.get("email_verified"),
            "username": text(claims.get("preferred_username")), "display_name": text(claims.get("name")), "claims": claims,
            "next": safe_next(started.get("next"))}  # checked again: the cookie came back from the browser


def first_role(s: Settings, who: dict) -> str:
    """The role someone gets when they first sign in: admin by a verified email in ODCL_OIDC_ADMIN_EMAILS (never one
    the provider says is unverified), else the highest role their ODCL_OIDC_ROLE_CLAIM values map to, else the default."""
    from .users import verified_email
    if (email := verified_email(who)) and email in s.admin_emails:
        return "admin"
    said = who.get("claims", {}).get(s.role_claim) if s.role_claim else None
    said = {str(v) for v in (said if isinstance(said, list) else [said] if said is not None else [])}
    roles = [role for value, role in s.role_values if value in said]
    return max(roles + [s.default_role], key=rank)


def logout_url(s: Settings, origin: str) -> str:
    """Where to send someone signing out: the provider's own sign-out, if it has one, then back here."""
    back = s.post_logout_redirect_uri or f"{origin}/"
    try:
        end = provider(s).get("end_session_endpoint")
    except CatalogError:
        return back  # the provider is unreachable: signed out here all the same
    if not isinstance(end, str):
        return back
    return end + ("&" if "?" in end else "?") + urllib.parse.urlencode({"client_id": s.client_id, "post_logout_redirect_uri": back})
