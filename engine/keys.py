"""Provider credential routing.

The single place credentials are read from the environment. Every other module
goes through ProviderKeyring, so that:

  - no module outside this one calls os.environ for a secret;
  - a missing key raises rather than yielding an empty string, so an
    unconfigured provider can never be mistaken for a configured one;
  - a key printed by accident, including inside a traceback, is redacted.

This matters more than usual for a trading system: the engine talks to several
providers over its life, and a key that leaks into a log is a credential that
has to be rotated.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final

# Load .env once at import so the keyring and the gateway see the same
# environment. Without this, an early call to keyring() reports every provider
# as unconfigured even though .env is populated, because config.py's loader has
# not run yet.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:      # pragma: no cover - dotenv is a hard dependency, but a
    pass               # missing .env must not prevent the engine starting.

# Logical provider name -> environment variable that holds its key.
# Adding a provider is a config change, not a code change.
PROVIDER_ENV_VARS: Final[dict[str, str]] = {
    "xai": "XAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "groq": "GROQ_API_KEY",
    "telegram": "TELEGRAM_BOT_TOKEN",
    "smtp": "SMTP_PASSWORD",
    "agentrouter": "AGENTROUTER_API_KEY",
    "infoway": "INFOWAY_API_KEY",
    "anchor": "ANCHOR_API_KEY",
}

# The vendor SDK reads ANCHOR_API_KEY. The old name remains a fallback only.
DEPRECATED_ALIAS: Final[dict[str, str]] = {
    "anchor": "ANCHORBROWSER_API_KEY",
}
DEPRECATION_NOTE: Final[str] = "deprecated duplicate, remove it from .env"

# Markers that identify an obvious placeholder rather than a real credential.
_PLACEHOLDER_PREFIXES = ("YOUR_", "<", "REPLACE_", "CHANGEME")


def _usable(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    return not text.upper().startswith(_PLACEHOLDER_PREFIXES)


class KeyNotConfigured(LookupError):
    """Raised when a provider is used but no key is configured for it.

    The message names the variable, never the value.
    """

    def __init__(self, provider: str, env_var: str):
        self.provider = provider
        self.env_var = env_var
        super().__init__(
            f"No key configured for provider {provider!r}: set {env_var} in the "
            f"environment (.env). The key is not set."
        )


@dataclass(frozen=True)
class Keyring:
    """Reads and redacts provider credentials.

    Construct with the environment mapping, defaulting to os.environ. Tests
    pass their own mapping so they never touch the real environment.
    """

    env: dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.env is None:
            object.__setattr__(self, "env", dict(os.environ))

    # -- introspection ---------------------------------------------------

    def providers(self) -> list[str]:
        """Every known provider name, whether or not it is configured."""
        return sorted(PROVIDER_ENV_VARS)

    def env_var_for(self, provider: str) -> str:
        try:
            return PROVIDER_ENV_VARS[provider]
        except KeyError:
            raise KeyError(
                f"Unknown provider {provider!r}. Known providers: "
                f"{self.providers()}"
            ) from None

    def _stored(self, env_var: str) -> str:
        return str(self.env.get(env_var, ""))

    def _configured_value(self, provider: str) -> str | None:
        """The canonical value, or the deprecated alias when that is the only real key."""
        primary = self._stored(self.env_var_for(provider))
        if _usable(primary):
            return primary.strip()
        alias = DEPRECATED_ALIAS.get(provider)
        if alias and _usable(self._stored(alias)):
            return self._stored(alias).strip()
        return None

    def is_configured(self, provider: str) -> bool:
        """True when a real key is present for this provider."""
        return self._configured_value(provider) is not None

    def missing(self) -> list[str]:
        """Providers with no key configured, for a startup readiness report."""
        return [p for p in self.providers() if not self.is_configured(p)]

    # -- access ----------------------------------------------------------

    def require(self, provider: str) -> str:
        """The key for `provider`.

        Raises KeyNotConfigured when absent, so a caller cannot accidentally
        send an empty credential and receive a confusing auth error.
        """
        env_var = self.env_var_for(provider)
        value = self._configured_value(provider)
        if value is None:
            raise KeyNotConfigured(provider, env_var)
        return value

    def get(self, provider: str, default: str | None = None) -> str | None:
        """The key for `provider`, or `default` when not configured."""
        try:
            return self.require(provider)
        except KeyNotConfigured:
            return default

    # -- redaction -------------------------------------------------------

    def describe(self, provider: str) -> str:
        """Redacted description, safe for logs and exceptions.

        A deprecated alias that is present is named as a duplicate. The value
        is never included.
        """
        env_var = self.env_var_for(provider)
        alias = DEPRECATED_ALIAS.get(provider, "")
        note = ""
        if alias and self._stored(alias).strip():
            note = f" {DEPRECATION_NOTE}"
        value = self._configured_value(provider)
        if value is None:
            return f"{provider} (env {env_var}): ***NOT CONFIGURED***{note}"
        return (
            f"{provider} (env {env_var}): ***REDACTED*** "
            f"(set, len={len(value)}){note}"
        )

    def describe_all(self) -> list[str]:
        return [self.describe(p) for p in self.providers()]

    def __repr__(self) -> str:
        return f"Keyring(providers={self.providers()}, keys=***REDACTED***)"

    def __str__(self) -> str:
        return self.__repr__()


# Process-wide default. Tests construct their own Keyring instead of patching
# this, so no test can accidentally read a real key.
_default: Keyring | None = None


def keyring() -> Keyring:
    """The shared keyring for this process."""
    global _default
    if _default is None:
        _default = Keyring()
    return _default


def reset_keyring() -> None:
    """Drop the cached keyring. Used by tests and after a config reload."""
    global _default
    _default = None