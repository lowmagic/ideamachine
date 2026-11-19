<!-- lowmagic/.github code-standards/containers.md at cca8c65ba8c04cfe250b83b91ba69947f014e082. Change it there, then run make sync-standards. -->
# Container standards

Decisions for Dockerfiles and compose files, beneath the org [standards](https://github.com/lowmagic/.github/blob/main/code-standards/README.md). Python images also follow [python.md](https://github.com/lowmagic/.github/blob/main/code-standards/python.md): they install with `uv sync --frozen`, never pip.

## Dockerfile

- The base is a Debian `-slim` image whose tag names the Debian release and, for Python, the version in `.python-version`: `python:3.13-slim-trixie`. A digest after the tag is fine.
- uv comes from `COPY --from=ghcr.io/astral-sh/uv:<version> /uv /uvx /bin/`, at an exact version.
- `COPY` names the files the build needs. No `COPY . .`.
- Debian packages install in one `RUN`: `apt-get update`, then `apt-get install -y --no-install-recommends …`, then `rm -rf /var/lib/apt/lists/*`.
- Anything else the build downloads, such as a model or a tool, is pinned to a version or revision.
- The image sets a non-root `USER`. A compose file that runs it as root says why.
- `ENTRYPOINT` and `CMD` use the exec form: `["prog", "arg"]`.

## Compose

- The file is `compose.yaml`, or `compose.<purpose>.yaml` beside another, with a top-level `name:` and no `version:`.
- A third-party image's tag names at least its major version (`postgres:16`), never `latest` or nothing.
- A published port binds a named host address, such as `127.0.0.1` or one the installer picks, never all interfaces. Inside the container, the service listens on `0.0.0.0`.
- Secrets come from an env file outside the repo or a required variable (`${TOKEN:?}`). A literal credential is only for a throwaway test database.
- A long-running service has `restart: unless-stopped`. A one-shot has no restart policy.
- A long-running Python service handles SIGTERM itself, as uvicorn does, or runs with `init: true`.
- A compose file that deploys sets `logging:` on every service, to journald or to `json-file` with `max-size`.
- A database that other services need has a `healthcheck`, and they wait for it with `condition: service_healthy`.
- A bind mount the container only reads ends in `:ro`.
