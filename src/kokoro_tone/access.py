"""Two roles, one allowlist file, and operation ids as the unit (ADR-0016).

The service has exactly one permission question: may this caller decide a
pronunciation, or only propose one? That has two answers, so there are two
roles. A caller with an admin key may call every operation; a caller with a
default key may call only the operation ids named in `[default].allow`.

The authorisation unit is the OpenAPI `operationId`, not the path, because
`GET` and `DELETE` on one path are different operations. `routes.py` sets the
id on every route and the dependency reads it from the matched route, so the
slug has one source of truth.

`PRONUNCIATION_WRITE_KEY` is gone with this module. A second secret could not
say *which* operations it unlocked, which is the distinction ADR-0016 needs.
"""

from __future__ import annotations

import secrets
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .log import warning

Role = Literal["default", "admin"]
ROLE_DEFAULT: Role = "default"
ROLE_ADMIN: Role = "admin"

# One entry per operation the HTTP surface exposes. This is the validation set
# for a policy file: a slug outside it is a typo and refuses to start. Adding a
# route means adding its operation id here, which is also the deploy-order
# constraint ADR-0016 accepts.
OPERATIONS = frozenset(
    {
        "compileScript",
        "createPronunciation",
        "createSpeech",
        "deletePronunciation",
        "getHealth",
        "listModels",
        "listPronunciations",
        "listVoices",
        "retrievePronunciationAudio",
        "updatePronunciation",
    }
)

# ADR-0017: a proposal exists so that a different key makes the decision. The
# deciding operations are therefore never in the built-in default policy, and an
# allowlist cannot grant them either: `Access.allows` is bypassed only by the
# admin role, but a file that names an admin-only slug is rejected at load.
ADMIN_ONLY_SLUGS = frozenset(
    {"deletePronunciation", "retrievePronunciationAudio", "updatePronunciation"}
)

# Public is a property of the contract, not a deployment choice: the compose
# healthcheck calls /health with no credential and the docs must stay readable.
PUBLIC_SLUGS = frozenset({"getHealth", "openapi", "swagger_ui_html", "redoc_html"})

KNOWN_SLUGS = OPERATIONS | PUBLIC_SLUGS

# The built-in policy, used when no file exists. It names every operation a
# default key may call, so an upgrade that adds keys but no `rbac.toml` does not
# take a route away from an existing caller. A route added later is NOT added
# here automatically; it is admin-only until an allowlist names it.
DEFAULT_ALLOW: tuple[str, ...] = tuple(sorted(OPERATIONS - ADMIN_ONLY_SLUGS))


class PolicyError(RuntimeError):
    """A policy file that must stop the process rather than narrow or widen access."""


@dataclass(frozen=True)
class Policy:
    allow: frozenset[str]
    source: str | None = None


def _builtin() -> Policy:
    return Policy(frozenset(DEFAULT_ALLOW))


def load_policy(path: str) -> Policy:
    """Read `rbac.toml`, or return the built-in defaults when there is no file.

    A missing file is normal: the built-in policy is the whole policy for a
    single-operator deployment, and the path is a default rather than a
    requirement. Everything else - a syntax error, an unknown version, role
    table or slug - raises `PolicyError`, because a typo must not silently
    change who may write.
    """
    if not path:
        return _builtin()
    candidate = Path(path)
    try:
        text = candidate.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _builtin()
    except OSError as exc:
        raise PolicyError(f"{path}: cannot read the RBAC policy: {exc}") from exc

    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError(f"{path}: {exc}") from exc

    unknown_top = sorted(set(raw) - {"version", "default"})
    if unknown_top:
        raise PolicyError(
            f"{path}: unknown role table or key {unknown_top[0]!r}; "
            "only 'default' is configurable and admin is defined by having a key"
        )

    version = raw.get("version")
    if version != 1:
        raise PolicyError(f"{path}: unsupported version {version!r}; expected version = 1")

    table = raw.get("default", {})
    if not isinstance(table, dict):
        raise PolicyError(f"{path}: [default] must be a table")
    unknown_keys = sorted(set(table) - {"allow"})
    if unknown_keys:
        raise PolicyError(f"{path}: unknown key {unknown_keys[0]!r} in [default]")

    allow = table.get("allow", [])
    if not isinstance(allow, list) or not all(isinstance(slug, str) for slug in allow):
        raise PolicyError(f"{path}: [default].allow must be a list of operation ids")
    unknown = [slug for slug in allow if slug not in KNOWN_SLUGS]
    if unknown:
        raise PolicyError(f"{path}: unknown operation id {unknown[0]!r} in [default].allow")
    admin_only = [slug for slug in allow if slug in ADMIN_ONLY_SLUGS]
    if admin_only:
        raise PolicyError(
            f"{path}: {admin_only[0]!r} is an admin-only operation and cannot be granted "
            "to a default key"
        )

    return Policy(frozenset(allow), str(candidate))


def template() -> str:
    """The example policy, generated from `DEFAULT_ALLOW` so the two cannot drift."""
    lines = [
        "# Generated from src/kokoro_tone/access.py: DEFAULT_ALLOW.",
        "# Copy to /etc/kokoro-tone/rbac.toml and remove the ids a default key",
        "# should not call. An admin key may call every operation, listed or not.",
        "# The deciding operations (update/delete/audio) are admin-only and must",
        "# not be listed here.",
        "",
        "version = 1",
        "",
        "[default]",
        "allow = [",
    ]
    lines.extend(f'  "{slug}",' for slug in DEFAULT_ALLOW)
    lines.extend(["]", ""])
    return "\n".join(lines)


@dataclass(frozen=True)
class Access:
    """The credentials and the policy, resolved once at startup."""

    admin_keys: tuple[str, ...]
    default_keys: tuple[str, ...]
    policy: Policy

    @property
    def enabled(self) -> bool:
        return bool(self.admin_keys or self.default_keys)

    def match(self, presented: str) -> Role | None:
        """The role a token holds, or None. No early exit on either list."""
        admin = False
        default = False
        for key in self.admin_keys:
            admin |= secrets.compare_digest(presented, key)
        for key in self.default_keys:
            default |= secrets.compare_digest(presented, key)
        if admin:
            return ROLE_ADMIN
        if default:
            return ROLE_DEFAULT
        return None

    def allows(self, role: Role, slug: str) -> bool:
        if slug in PUBLIC_SLUGS:
            return True
        if slug in ADMIN_ONLY_SLUGS:
            return role == ROLE_ADMIN
        if role == ROLE_ADMIN:
            return True
        return slug in self.policy.allow


def load_access(
    *,
    admin_keys: tuple[str, ...],
    default_keys: tuple[str, ...],
    rbac_path: str,
) -> Access:
    """Build the resolved access object, warning if the file cannot matter."""
    policy = load_policy(rbac_path)
    access = Access(admin_keys=admin_keys, default_keys=default_keys, policy=policy)
    if policy.source and not access.enabled:
        warning(
            f"KOKORO_TONE_RBAC={policy.source} is inert: no API keys are configured, "
            "so authentication and authorisation are disabled"
        )
    return access
