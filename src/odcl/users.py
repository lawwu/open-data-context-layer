"""Users: who may read and write the catalog, and how a caller proves who they are.

A user has a name (what revisions record as the actor) and one role:

  reader    reads everything
  editor    also annotates: projects and their uses, privacy fields, created_by, references, copies, documents relied on
  importer  also imports: whole batches (`Catalog.apply`), builds, model versions, query history, OpenLineage events
  admin     also manages users and their tokens

A caller proves who they are with an API token (`odcl_...`, shown once, kept only as a digest) or, in the browser,
with a session from single sign-on (`odcl.sso`). Whoever connects to the database directly, without a token, is
trusted as before: holding the database's credentials is the gate there, and the actor is `--as`, `$ODCL_USER` or
the login name. That is how the first admin is made, and how importers that run centrally sign their work.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

from .store import CatalogError, require, stamp, text

ROLES = ("reader", "editor", "importer", "admin")
NAME = re.compile(r"^[a-z0-9_][a-z0-9_.\-]{0,99}$")
TOKEN_PREFIX = "odcl_"


def rank(role: str) -> int:
    return ROLES.index(role)


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def public(row: dict) -> dict:
    """What anyone allowed to see a user sees: never a token or a digest."""
    return {"name": row["name"], "display_name": row["display_name"], "email": row["email"], "role": row["role"],
            "single_sign_on": row["oidc_subject"] is not None, "disabled": row["disabled_at"] is not None,
            "last_sign_in_at": stamp(row["last_sign_in_at"]) if row["last_sign_in_at"] else None, "created_at": stamp(row["created_at"]),
            "created_by": row["created_by"]}


def _name(value) -> str:
    name = str(value or "").strip().lower()
    require(NAME.match(name), "name", "A user name is letters, digits, _ . or - (at most 100)")
    return name


def _role(value) -> str:
    require(value in ROLES, "role", f"A role is one of: {', '.join(ROLES)}")
    return value


def _get(db, name: str) -> dict:
    row = db.execute("SELECT * FROM users WHERE name = %s", (name,)).fetchone()
    require(row, "not_found", f"No user named {name}")
    return row


# ---- who is calling ------------------------------------------------------------------------------------------
def authenticate(db, token: str) -> dict:
    """The user an API token belongs to. Fails for a token that is not current, a disabled user, or (with single
    sign-on set up) someone who signs in through it but has not for ODCL_OIDC_TOKEN_IDLE_DAYS."""
    require(isinstance(token, str) and token.startswith(TOKEN_PREFIX), "unauthorized", "That is not an odcl API token")
    row = db.execute("SELECT u.*, t.id AS token_id FROM user_tokens t JOIN users u ON u.name = t.user_name "
                     "WHERE t.digest = %s AND t.revoked_at IS NULL", (digest(token),)).fetchone()
    require(row, "unauthorized", "That token is not a current odcl API token")
    require(row["disabled_at"] is None, "unauthorized", f"{row['name']} is disabled")
    from .sso import settings
    sso = settings()
    if sso and sso.token_idle_days and row["oidc_subject"] is not None:
        last = row["last_sign_in_at"]
        require(last and last > datetime.now(timezone.utc) - timedelta(days=sso.token_idle_days), "unauthorized",
                f"This token stopped working: {row['name']} has not signed in for {sso.token_idle_days} days. Sign in in the browser and it works again")
    db.execute("UPDATE user_tokens SET last_used_at = now() WHERE id = %s AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute')",
               (row["token_id"],))
    return public(row)


def start_session(db, name: str, hours: float) -> str:
    """A browser session for a user who just signed in. The cookie holds the secret; the table, its digest."""
    secret = secrets.token_urlsafe(32)
    db.execute("DELETE FROM sessions WHERE expires_at <= now()")
    db.execute("INSERT INTO sessions (digest, user_name, expires_at) VALUES (%s, %s, now() + %s)", (digest(secret), name, timedelta(hours=hours)))
    return secret


def session_user(db, secret: str | None) -> dict | None:
    if not secret:
        return None
    row = db.execute("SELECT u.* FROM sessions s JOIN users u ON u.name = s.user_name WHERE s.digest = %s AND s.expires_at > now() "
                     "AND u.disabled_at IS NULL", (digest(secret),)).fetchone()
    return public(row) if row else None


def end_session(db, secret: str | None) -> None:
    if secret:
        db.execute("DELETE FROM sessions WHERE digest = %s", (digest(secret),))


def whoami(catalog) -> dict:
    """The signed-in user, or, for a direct database connection, the actor its writes are recorded under."""
    return catalog.user or {"name": catalog.actor, "role": None, "direct": True}


# ---- managing users ------------------------------------------------------------------------------------------
def listing(catalog) -> list[dict]:
    catalog.allow("admin")
    rows = catalog.db.execute("SELECT u.*, (SELECT count(*) FROM user_tokens t WHERE t.user_name = u.name AND t.revoked_at IS NULL) AS tokens "
                              "FROM users u ORDER BY u.name").fetchall()
    return [{**public(r), "tokens": r["tokens"]} for r in rows]


def add(catalog, name: str, role: str, display_name: str | None = None, email: str | None = None) -> dict:
    """Make a user: a person, or a service identity for importers that run from CI or a scheduler."""
    catalog.allow("admin")
    name, role = _name(name), _role(role)
    display_name, email = text(display_name, "display_name", 200, optional=True), text(email, "email", 320, optional=True)
    with catalog.db.transaction():
        require(not catalog.db.execute("SELECT 1 FROM users WHERE name = %s", (name,)).fetchone(), "exists", f"There is already a user named {name}")
        row = catalog.db.execute("INSERT INTO users (name, display_name, email, role, created_by) VALUES (%s, %s, %s, %s, %s) RETURNING *",
                                 (name, display_name, email and email.lower(), role, catalog.actor)).fetchone()
    return public(row)


def update(catalog, name: str, role: str | None = None, disabled: bool | None = None) -> dict:
    """Change a user's role, or disable them (which ends their sessions; their tokens stop working) or enable them again."""
    catalog.allow("admin")
    name = _name(name)
    require(role is not None or disabled is not None, "update", "Say a role, or whether the user is disabled")
    require(not (catalog.user and catalog.user["name"] == name), "self", "Ask another admin to change your own role or disable you")
    with catalog.db.transaction():
        _get(catalog.db, name)
        if role is not None:
            catalog.db.execute("UPDATE users SET role = %s WHERE name = %s", (_role(role), name))
        if disabled is not None:
            catalog.db.execute("UPDATE users SET disabled_at = CASE WHEN %s THEN COALESCE(disabled_at, now()) END WHERE name = %s", (disabled, name))
            if disabled:
                catalog.db.execute("DELETE FROM sessions WHERE user_name = %s", (name,))
        return public(_get(catalog.db, name))


def make_token(catalog, name: str | None = None, label: str | None = None) -> dict:
    """A new API token: your own, or (an admin) anyone's, such as a service user's. The token is in this answer only."""
    if name is None or (catalog.user and _name(name) == catalog.user["name"]):
        require(catalog.user, "unauthorized", "Sign in to make your own token, or name the user")
        name = catalog.user["name"]
    else:
        catalog.allow("admin")
        name = _name(name)
    label = text(label, "label", 200, optional=True)
    user = _get(catalog.db, name)
    require(user["disabled_at"] is None, "disabled", f"{name} is disabled")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    token_id = "tok_" + secrets.token_hex(6)
    catalog.db.execute("INSERT INTO user_tokens (id, user_name, digest, label, created_by) VALUES (%s, %s, %s, %s, %s)",
                       (token_id, name, digest(token), label, catalog.actor))
    return {"id": token_id, "user": name, "label": label, "token": token}


def tokens(catalog, name: str | None = None) -> list[dict]:
    """A user's current tokens (their ids and labels, never the tokens): your own, or (an admin) anyone's."""
    if name is None:
        require(catalog.user, "unauthorized", "Sign in to list your own tokens, or name the user")
        name = catalog.user["name"]
    elif not (catalog.user and _name(name) == catalog.user["name"]):
        catalog.allow("admin")
    rows = catalog.db.execute("SELECT id, label, created_at, created_by, last_used_at FROM user_tokens WHERE user_name = %s AND revoked_at IS NULL "
                              "ORDER BY created_at, id", (_name(name),)).fetchall()
    return [{"id": r["id"], "label": r["label"], "created_at": stamp(r["created_at"]), "created_by": r["created_by"],
             "last_used_at": stamp(r["last_used_at"]) if r["last_used_at"] else None} for r in rows]


def revoke(catalog, token_id: str) -> dict:
    """Stop a token working: your own, or (an admin) anyone's."""
    if catalog.user is None:
        catalog.allow("admin")  # a direct connection may; an anonymous caller of the HTTP API may not
    row = catalog.db.execute("SELECT user_name FROM user_tokens WHERE id = %s AND revoked_at IS NULL", (token_id,)).fetchone()
    admin = catalog.user is None or catalog.user["role"] == "admin"
    # someone else's token is "not found" to anyone but an admin, so the answer says nothing about whose it is
    require(row and (admin or row["user_name"] == catalog.user["name"]), "not_found", f"No current token {token_id} of yours")
    catalog.db.execute("UPDATE user_tokens SET revoked_at = now() WHERE id = %s", (token_id,))
    return {"id": token_id, "user": row["user_name"], "revoked": True}


# ---- single sign-on --------------------------------------------------------------------------------------------
def verified_email(who: dict) -> str | None:
    """The email someone signed in with, only when the provider says it is verified: `email_verified` true, or
    Microsoft Entra's `xms_edov` (its domain owner verified it). A provider that says nothing is not taken at its word:
    Entra sends no `email_verified`, and its `email` can be set by users and other tenants."""
    email = (who.get("email") or "").lower() or None
    said = lambda v: v is True or (isinstance(v, str) and v.lower() == "true")  # noqa: E731
    claims = who.get("claims") or {}
    return email if email and (said(who.get("email_verified")) or said(claims.get("xms_edov"))) else None


def any_users(db) -> bool:
    return db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def handle(*candidates: str | None) -> str:
    """A user name from what the provider says: its username, else the email's local part (ann.lee@example.com -> ann.lee)."""
    for value in candidates:
        if value:
            name = re.sub(r"[^a-z0-9_.\-]+", "-", value.strip().lower().split("@", 1)[0]).strip("-.")[:90]
            if name and NAME.match(name):
                return name
    return "user"


def sign_in(db, who: dict, role: str) -> dict:
    """The user someone who signed in through single sign-on is: found by the provider's issuer and subject, which
    never change. The first sign-in makes the user with `role` (decided by the caller from the provider's claims);
    later sign-ins never change the role, so a provider's claims changing cannot silently promote anyone. A user an
    admin made beforehand with the same email is taken over, only when the provider says that email is verified."""
    with db.transaction():
        row = db.execute("SELECT * FROM users WHERE oidc_issuer = %s AND oidc_subject = %s", (who["issuer"], who["sub"])).fetchone()
        email = (who.get("email") or "").lower() or None
        verified = verified_email(who)
        if row is None and verified:
            row = db.execute("SELECT * FROM users WHERE email = %s AND oidc_subject IS NULL ORDER BY name LIMIT 1", (verified,)).fetchone()
            if row:
                db.execute("UPDATE users SET oidc_issuer = %s, oidc_subject = %s WHERE name = %s", (who["issuer"], who["sub"], row["name"]))
        if row is None:
            base = handle(who.get("username"), email, who.get("display_name"))
            name, n = base, 1
            while db.execute("SELECT 1 FROM users WHERE name = %s", (name,)).fetchone():
                n += 1
                name = f"{base}-{n}"
            row = db.execute("INSERT INTO users (name, display_name, email, role, oidc_issuer, oidc_subject, created_by) "
                             "VALUES (%s, %s, %s, %s, %s, %s, 'single sign-on') RETURNING *",
                             (name, (who.get("display_name") or "")[:200] or None, email, _role(role), who["issuer"], who["sub"])).fetchone()
        if row["disabled_at"] is not None:
            raise CatalogError("forbidden", f"{row['name']} is disabled; ask an admin")
        db.execute("UPDATE users SET last_sign_in_at = now(), email = COALESCE(%s, email), display_name = COALESCE(%s, display_name) WHERE name = %s",
                   (email, (who.get("display_name") or "")[:200] or None, row["name"]))
        return public(_get(db, row["name"]))
