"""Entrypoint: ``python -m sila.worker`` runs the KB document-processing loop."""

import asyncio

from sila.worker.runner import run

if __name__ == "__main__":
    asyncio.run(run())
