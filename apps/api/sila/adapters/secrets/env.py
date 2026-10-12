import os

from sila.adapters.secrets.base import SecretsProvider


class EnvSecretsAdapter:
    """Reads secrets from environment variables. Swap for VaultAdapter in production."""

    def get(self, key: str) -> str:
        value = os.environ.get(key)
        if value is None:
            raise KeyError(f"Secret not found in environment: {key}")
        return value


_: SecretsProvider = EnvSecretsAdapter()
