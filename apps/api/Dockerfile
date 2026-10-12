FROM python:3.13-slim

WORKDIR /app

# Install uv
# poppler renders PDF pages to PNG thumbnails (slide-template previews);
# tesseract is the KB pipeline's OCR fallback for image-only PDF pages.
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv

COPY pyproject.toml .
RUN uv sync --no-dev --no-install-project

ENV PATH="/app/.venv/bin:$PATH"

# Best-effort: bake the Presidio spaCy model so PII flagging works out of the box.
# NON-FATAL — behind a TLS-intercepting corporate proxy the GitHub model download
# fails (CERTIFICATE_VERIFY_FAILED), so we must not make the build depend on it.
# When the model is absent the app degrades gracefully (PII flag no-ops; the
# runtime checks `is_package` before loading, so it never tries to download/hang).
# Placed before the app COPY so the layer caches across code changes.
RUN python -m spacy download en_core_web_lg \
    || echo "spaCy model not baked (offline/proxy) — PII flagging will no-op until provisioned"

COPY . .
RUN uv sync --no-dev

# Run as a non-root user, and make what the app writes (/app/data) owned by
# group 0 and group-writable: OpenShift runs containers under a random UID that
# is always in group 0, so this one image runs on Docker and OpenShift alike.
# HOME points somewhere writable for libraries that cache under ~.
RUN mkdir -p /app/data/datasets \
    && chgrp -R 0 /app/data && chmod -R g=u /app/data
ENV HOME=/tmp
USER 1001

EXPOSE 8001

CMD ["uvicorn", "sila.main:app", "--host", "0.0.0.0", "--port", "8001"]
