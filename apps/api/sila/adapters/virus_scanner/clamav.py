"""ClamAV virus scanner via clamd TCP socket.

Swap point (Rule 5): replace with ClamAVHttpAdapter or a cloud scanning service
without changing any caller.

Protocol: INSTREAM command — streams file content, receives OK or FOUND verdict.
"""

import asyncio
import logging
import struct

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 4096
_INSTREAM_CMD = b"nINSTREAM\n"


class ClamAVAdapter:
    def __init__(self, host: str, port: int, timeout: float = 30.0) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout

    async def scan(self, content: bytes) -> bool:
        """Stream file to clamd via INSTREAM. Returns True=clean, False=infected.

        Raises RuntimeError if ClamAV is unreachable or returns an unexpected response.
        O(n) where n = file size (network transfer bounded by document_max_mb config).
        """
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=self._timeout,
            )
        except (TimeoutError, OSError) as exc:
            raise RuntimeError(f"ClamAV unreachable at {self._host}:{self._port}") from exc

        try:
            writer.write(_INSTREAM_CMD)
            # Stream file in chunks prefixed with 4-byte big-endian length
            for i in range(0, len(content), _CHUNK_SIZE):
                chunk = content[i : i + _CHUNK_SIZE]
                writer.write(struct.pack("!I", len(chunk)) + chunk)
            writer.write(struct.pack("!I", 0))  # terminator
            await writer.drain()

            response = await asyncio.wait_for(reader.read(1024), timeout=self._timeout)
        finally:
            writer.close()
            await writer.wait_closed()

        verdict = response.decode("utf-8", errors="replace").strip()
        logger.debug("ClamAV verdict: %s", verdict)

        if verdict.endswith("OK"):
            return True
        if "FOUND" in verdict:
            logger.warning("Malware detected by ClamAV: %s", verdict)
            return False
        raise RuntimeError(f"Unexpected ClamAV response: {verdict!r}")
