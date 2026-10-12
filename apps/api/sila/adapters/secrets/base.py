from typing import Protocol


class SecretsProvider(Protocol):
    def get(self, key: str) -> str: ...
