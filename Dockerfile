# syntax=docker/dockerfile:1
# Reuse the matching official runtime dependencies; our Python source and the
# verified DIY frontend are complete replacements, not overlays of old files.
FROM ghcr.io/open-webui/open-webui:v0.12.2@sha256:10e6a78ba2d172493460a9e8a92c496aaa7a64dc4d26d622a2e95e0c736081c9

ARG BUILD_HASH=local-development
ARG FRONTEND_COMMIT=unknown
LABEL org.opencontainers.image.title="Open WebUI Backend" \
      org.opencontainers.image.source="https://github.com/Mike666wq/open-webui-backend" \
      org.opencontainers.image.revision="${BUILD_HASH}" \
      io.openwebui.upstream.version="0.12.2" \
      io.openwebui.frontend.commit="${FRONTEND_COMMIT}"

WORKDIR /app/backend
RUN rm -rf /app/backend/open_webui /app/build
COPY backend/open_webui/ /app/backend/open_webui/
COPY backend/start.sh backend/requirements.txt backend/requirements-slim.txt /app/backend/
COPY package.json CHANGELOG.md backend.upstream.json frontend.lock.json /app/
COPY LICENSE LICENSE_HISTORY LICENSE_NOTICE CONTRIBUTOR_LICENSE_AGREEMENT /app/licenses/
COPY .build/frontend/ /app/build/
COPY scripts/frontend_archive.py /app/backend-build/frontend_archive.py

ENV FRONTEND_BUILD_DIR=/app/build \
    FRONTEND_LOCK_FILE=/app/frontend.lock.json \
    WEBUI_BUILD_HASH=${BUILD_HASH}

# Recheck all bytes inside a manually invoked build, not just its manifest.
RUN python -c 'import json,pathlib,sys; sys.path.insert(0,"/app/backend-build"); from frontend_archive import verify_directory; root=pathlib.Path("/app"); lock=json.loads((root/"frontend.lock.json").read_text()); print(verify_directory(root/"build",lock)); assert (root/"build/static").is_dir()'

EXPOSE 8080
CMD ["bash", "start.sh"]

