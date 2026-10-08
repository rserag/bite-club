"""App-owned Sign in with ChatGPT credentials; never borrow a Codex auth session."""

import argparse
import asyncio
import base64
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
import time
import uuid
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
from pydantic import ConfigDict, Field, SecretStr

from nutrition_bot.domain.ai import AiUnavailable
from nutrition_bot.domain.food import FrozenModel

ISSUER: Literal["https://auth.openai.com"] = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
AUTHORIZE_URL = ISSUER + "/api/accounts/authorize"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
JWKS_URL = ISSUER + "/.well-known/jwks.json"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"


class ChatGPTCredentials(FrozenModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    issuer: Literal["https://auth.openai.com"] = ISSUER
    subject: str = Field(min_length=1, max_length=256)
    email: str | None = Field(default=None, max_length=320)
    client_id: str = Field(min_length=1, max_length=256)
    ext_agent_host_id: str = Field(pattern=r"^urn:uuid:[a-f0-9-]{36}$")
    id_token: SecretStr
    access_token: SecretStr
    refresh_token: SecretStr | None = None
    token_type: Literal["Bearer"]
    scopes: tuple[str, ...]
    expires_at: float = Field(allow_inf_nan=False)
    earliest_refresh_at: float = Field(default=0, allow_inf_nan=False)
    saved_at: float = Field(allow_inf_nan=False)

    def inference_permitted(self) -> bool:
        return (
            self.client_id != "dynamic_agent_client"
            and "chatgpt.tokens.use.direct" in self.scopes
            and "resource.invoke" in self.scopes
        )


def _private_parent(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    value = path.parent.lstat()
    if not stat.S_ISDIR(value.st_mode) or value.st_uid != os.getuid() or value.st_mode & 0o077:
        raise AiUnavailable(
            "The ChatGPT credential directory must be private and owner-controlled."
        )


def _read_private(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        value = os.fstat(descriptor)
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_uid != os.getuid()
            or value.st_mode & 0o077
            or value.st_nlink != 1
            or value.st_size > 65_536
        ):
            raise AiUnavailable("The ChatGPT credential file must have owner-only permissions.")
        with os.fdopen(descriptor, "rb", closefd=False) as file:
            return file.read(65_537)
    finally:
        os.close(descriptor)


def _write_private(path: Path, data: bytes) -> None:
    _private_parent(path)
    if path.is_symlink():
        raise AiUnavailable("The ChatGPT credential path cannot be a symlink.")
    descriptor, name = tempfile.mkstemp(prefix=".chatgpt-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_credentials(path: Path) -> ChatGPTCredentials:
    try:
        value = ChatGPTCredentials.model_validate_json(_read_private(path))
        if value.client_id == "dynamic_agent_client":
            raise ValueError
        return value
    except (OSError, ValueError):
        raise AiUnavailable(
            "ChatGPT sign-in is unavailable. Sign in again with Bite Club."
        ) from None


def save_credentials(path: Path, credentials: ChatGPTCredentials) -> None:
    value = credentials.model_dump(mode="json")
    for secret_field in ("access_token", "refresh_token", "id_token"):
        secret = getattr(credentials, secret_field)
        value[secret_field] = secret.get_secret_value() if secret is not None else None
    _write_private(path, json.dumps(value, separators=(",", ":")).encode())


def stable_host_id(path: Path) -> str:
    if path.exists():
        try:
            result = _read_private(path).decode().strip()
            if not result.startswith("urn:uuid:") or str(uuid.UUID(result[9:])) != result[9:]:
                raise ValueError
            return result
        except (OSError, ValueError):
            raise AiUnavailable("The saved ChatGPT host identity is invalid.") from None
    result = "urn:uuid:" + str(uuid.uuid4())
    _write_private(path, result.encode())
    return result


@contextmanager
def credential_lock(path: Path) -> Iterator[None]:
    """Nonblocking process lock serializes refreshes of one rotating token session."""
    _private_parent(path)
    descriptor = os.open(
        path.with_suffix(path.suffix + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        value = os.fstat(descriptor)
        if value.st_uid != os.getuid() or value.st_mode & 0o077 or not stat.S_ISREG(value.st_mode):
            raise AiUnavailable("The ChatGPT session lock is not owner-controlled.")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AiUnavailable(
                "ChatGPT credentials are being renewed; try again shortly."
            ) from None
        yield
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class PendingSignIn:
    host_id: str
    redirect_uri: str
    state: str = field(repr=False)
    nonce: str = field(repr=False)
    verifier: str = field(repr=False)
    selected: ChatGPTCredentials | None = field(default=None, repr=False)

    @classmethod
    def create(
        cls, host_id: str, port: int, selected: ChatGPTCredentials | None = None
    ) -> "PendingSignIn":
        return cls(
            host_id,
            f"http://127.0.0.1:{port}/auth/callback",
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(64),
            selected,
        )

    @property
    def authorization_url(self) -> str:
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        params = dict(
            client_id=self.selected.client_id if self.selected else "dynamic_agent_client",
            ext_agent_host_id=self.host_id,
            response_type="code",
            redirect_uri=self.redirect_uri,
            scope=SCOPES,
            resource=RESOURCE,
            state=self.state,
            nonce=self.nonce,
            code_challenge_method="S256",
            code_challenge=challenge,
        )
        if self.selected:
            params["id_token_hint"] = self.selected.id_token.get_secret_value()
        else:
            params["agent_name_hint"] = "Bite Club"
        return AUTHORIZE_URL + "?" + urlencode(params)

    def callback(self, path: str) -> tuple[str, str]:
        if len(path) > 8192 or urlsplit(path).path != "/auth/callback":
            raise AiUnavailable("The ChatGPT sign-in callback is invalid.")
        values = parse_qs(urlsplit(path).query, keep_blank_values=True, strict_parsing=True)
        if any(len(parts) != 1 for parts in values.values()):
            raise AiUnavailable("The ChatGPT sign-in callback is invalid.")
        if not hmac.compare_digest(values.get("state", [""])[0].encode(), self.state.encode()):
            raise AiUnavailable("The ChatGPT sign-in state did not match.")
        if "error" in values:
            raise AiUnavailable(
                "ChatGPT sign-in was declined; existing credentials were preserved."
            )
        code = values.get("code", [""])[0]
        issued = values.get("client_id", [self.selected.client_id if self.selected else ""])[0]
        if (
            not 1 <= len(code) <= 4096
            or not 1 <= len(issued) <= 256
            or issued == "dynamic_agent_client"
        ):
            raise AiUnavailable("ChatGPT registration did not return its issued client identifier.")
        if self.selected and issued != self.selected.client_id:
            raise AiUnavailable("ChatGPT registration does not match the selected account.")
        return code, issued


async def _bounded_json(
    client: httpx.AsyncClient, method: str, url: str, *, data: dict[str, str] | None = None
) -> dict[str, Any]:
    try:
        async with (
            asyncio.timeout(20),
            client.stream(method, url, data=data, timeout=20) as response,
        ):
            if response.status_code != 200:
                raise ValueError
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > 65_536:
                    raise ValueError
        value = json.loads(content)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (httpx.HTTPError, ValueError, TimeoutError):
        raise AiUnavailable(
            "ChatGPT authorization returned an unavailable or invalid response."
        ) from None


async def verify_id_token(
    client: httpx.AsyncClient, token: str, *, client_id: str, nonce: str | None
) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise ValueError
        keys = (await _bounded_json(client, "GET", JWKS_URL))["keys"]
        if not isinstance(keys, list) or len(keys) > 50:
            raise ValueError
        if not all(isinstance(key, dict) for key in keys):
            raise ValueError
        matching = [
            key for key in keys if key.get("kid") == header["kid"] and key.get("kty") == "RSA"
        ]
        if len(matching) != 1:
            raise ValueError
        key = jwt.PyJWK.from_dict(matching[0], algorithm="RS256")
        claims: dict[str, Any] = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            issuer=ISSUER,
            audience=client_id,
            options={"require": ["iss", "sub", "aud", "exp", "iat"]},
        )
        if not isinstance(claims["sub"], str) or not 1 <= len(claims["sub"]) <= 256:
            raise ValueError
        if nonce is not None and (
            not isinstance(claims.get("nonce"), str)
            or not hmac.compare_digest(claims["nonce"], nonce)
        ):
            raise ValueError
        return claims
    except (httpx.HTTPError, ValueError, KeyError, TypeError, jwt.PyJWTError):
        raise AiUnavailable(
            "ChatGPT identity validation failed; no credentials were activated."
        ) from None


async def _token_response(client: httpx.AsyncClient, data: dict[str, str]) -> dict[str, Any]:
    return await _bounded_json(client, "POST", TOKEN_URL, data=data)


def _credentials(
    value: dict[str, Any],
    *,
    claims: dict[str, Any],
    client_id: str,
    host_id: str,
    previous: ChatGPTCredentials | None = None,
) -> ChatGPTCredentials:
    now = time.time()
    scope = value.get("scope")
    expires = value.get("expires_in")
    if (
        not isinstance(scope, str)
        or type(expires) is not int
        or not 1 <= expires <= 7200
        or not isinstance(value.get("token_type"), str)
        or value["token_type"].casefold() != "bearer"
        or not isinstance(value.get("access_token"), str)
        or not value["access_token"]
    ):
        raise AiUnavailable("ChatGPT returned an invalid credential response.")
    if previous and claims["sub"] != previous.subject:
        raise AiUnavailable("ChatGPT identity changed; the selected account was preserved.")
    earliest = value.get("earliest_refresh_at", now)
    if not isinstance(earliest, int | float) or isinstance(earliest, bool):
        earliest = now
    try:
        return ChatGPTCredentials(
            subject=claims["sub"],
            email=claims.get("email"),
            client_id=client_id,
            ext_agent_host_id=host_id,
            id_token=value.get("id_token") or (previous.id_token if previous else ""),
            access_token=value["access_token"],
            refresh_token=value.get("refresh_token"),
            token_type="Bearer",
            scopes=tuple(scope.split()),
            expires_at=now + expires,
            earliest_refresh_at=float(earliest),
            saved_at=now,
        )
    except ValueError:
        raise AiUnavailable("ChatGPT returned an invalid credential response.") from None


async def finish_sign_in(
    client: httpx.AsyncClient, pending: PendingSignIn, callback_path: str, path: Path
) -> ChatGPTCredentials:
    code, issued = pending.callback(callback_path)
    value = await _token_response(
        client,
        dict(
            grant_type="authorization_code",
            client_id=issued,
            code=code,
            code_verifier=pending.verifier,
            redirect_uri=pending.redirect_uri,
            resource=RESOURCE,
        ),
    )
    token = value.get("id_token")
    if not isinstance(token, str) or not token:
        raise AiUnavailable("ChatGPT did not return the identity token needed for validation.")
    claims = await verify_id_token(client, token, client_id=issued, nonce=pending.nonce)
    result = _credentials(
        value, claims=claims, client_id=issued, host_id=pending.host_id, previous=pending.selected
    )
    with credential_lock(path):
        save_credentials(path, result)
    return result


async def access_credentials(client: httpx.AsyncClient, path: Path) -> ChatGPTCredentials:
    with credential_lock(path):
        current = load_credentials(path)
        if not current.inference_permitted():
            raise AiUnavailable("This ChatGPT sign-in did not grant plan usage permission.")
        now = time.time()
        if current.expires_at > now + 60:
            return current
        if not current.refresh_token or now < current.earliest_refresh_at:
            raise AiUnavailable("ChatGPT sign-in needs renewal; sign in again with Bite Club.")
        refresh_token = current.refresh_token.get_secret_value()

        async def renew() -> ChatGPTCredentials:
            value = await _token_response(
                client,
                dict(
                    grant_type="refresh_token",
                    client_id=current.client_id,
                    refresh_token=refresh_token,
                    resource=RESOURCE,
                ),
            )
            token = value.get("id_token")
            claims = (
                await verify_id_token(client, token, client_id=current.client_id, nonce=None)
                if isinstance(token, str) and token
                else {"sub": current.subject, "email": current.email}
            )
            # Rotation must provide a replacement. Never keep using an old refresh token.
            if not isinstance(value.get("refresh_token"), str) or not value["refresh_token"]:
                raise AiUnavailable("ChatGPT renewal did not return a replacement session token.")
            replacement = _credentials(
                value,
                claims=claims,
                client_id=current.client_id,
                host_id=current.ext_agent_host_id,
                previous=current,
            )
            if not replacement.inference_permitted():
                raise AiUnavailable("ChatGPT plan permission is no longer available.")
            save_credentials(path, replacement)
            return replacement

        # Once a rotating refresh starts, cancellation cannot discard a received
        # replacement while validation is still awaiting JWKS. Retain the file
        # lock, join bounded validation/persistence, then propagate cancellation.
        renewal = asyncio.create_task(renew())
        try:
            return await asyncio.shield(renewal)
        except asyncio.CancelledError:
            while not renewal.done():
                try:
                    await asyncio.shield(renewal)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            # Retrieve any bounded validation failure while preserving the
            # caller's cancellation. Invalid replacements are never activated.
            renewal.exception()
            raise


def import_credentials(source: Path, destination: Path, host_id_path: Path) -> None:
    """A VM has its own stable host identity; importing cannot overwrite it with a laptop ID."""
    host_id = stable_host_id(host_id_path)
    current = load_credentials(source)
    with credential_lock(destination):
        save_credentials(destination, current.model_copy(update={"ext_agent_host_id": host_id}))


def sign_in_cli(path: Path, host_path: Path, *, open_browser: bool) -> None:
    _private_parent(path)
    host_id = stable_host_id(host_path)
    selected = load_credentials(path) if path.exists() else None
    callbacks: list[str] = []
    pending: PendingSignIn | None = None

    class Handler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(3)

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            assert pending is not None
            try:
                pending.callback(self.path)
            except (AiUnavailable, ValueError):
                self.send_response(400)
                body = b"Sign-in callback was rejected. Return to Bite Club."
            else:
                callbacks.append(self.path)
                self.send_response(200)
                body = b"Continue with ChatGPT received. You may return to Bite Club."
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        server.timeout = 1
        pending = PendingSignIn.create(host_id, server.server_port, selected)
        # Never print an old ID-token login hint. Browser navigation alone receives it.
        public_url = pending.authorization_url
        if selected:
            from urllib.parse import urlunsplit

            parts = urlsplit(public_url)
            safe = parse_qs(parts.query)
            safe.pop("id_token_hint", None)
            public_url = urlunsplit(
                (
                    parts.scheme,
                    parts.netloc,
                    parts.path,
                    urlencode({key: values[0] for key, values in safe.items()}),
                    "",
                )
            )
        print("Continue with ChatGPT:", public_url, flush=True)
        if open_browser:
            webbrowser.open(pending.authorization_url)
        deadline = time.monotonic() + 300
        while not callbacks and time.monotonic() < deadline:
            server.handle_request()
    if not callbacks:
        raise AiUnavailable("ChatGPT sign-in timed out; the previous account was preserved.")

    async def finish() -> None:
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
            assert pending is not None
            result = await finish_sign_in(client, pending, callbacks[0], path)
            print(
                "ChatGPT sign-in saved privately. Plan usage permission:",
                result.inference_permitted(),
            )

    asyncio.run(finish())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bite Club app-owned Continue with ChatGPT sign-in"
    )
    parser.add_argument("operation", choices=("signin", "import", "host-id"))
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--host-id", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()
    host_path = args.host_id or args.credentials.with_name("chatgpt-host-id")
    try:
        if args.operation == "signin":
            sign_in_cli(args.credentials, host_path, open_browser=args.open_browser)
        elif args.operation == "host-id":
            stable_host_id(host_path)
            print("Bite Club ChatGPT host identity saved privately.")
        elif args.source:
            import_credentials(args.source, args.credentials, host_path)
            print("ChatGPT credentials imported with this host's own identity.")
        else:
            parser.error("--source is required for import")
    except (AiUnavailable, ValueError, OSError):
        raise SystemExit(
            "ChatGPT setup could not finish safely. Existing credentials were preserved."
        ) from None


if __name__ == "__main__":
    main()
