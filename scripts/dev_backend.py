#!/usr/bin/env python3
"""Launch an isolated, local-only Open WebUI development backend.

No application module is imported until the child uvicorn process starts.
Configuration inspection therefore also works with an unsupported host Python.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_PYTHON = {(3, 11), (3, 12)}
# Application configuration, proxies, PYTHONPATH, and cloud credentials are
# deliberately not inherited. Keep only interpreter/OS/certificate essentials.
OS_ENV_KEYS = {
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TMP", "TEMP",
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TZ", "VIRTUAL_ENV",
    "SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
}


def port_number(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535") from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535")
    return port


def require_supported_python(version: tuple[int, ...] | None = None) -> None:
    version = version if version is not None else tuple(sys.version_info)
    if version[:2] not in SUPPORTED_PYTHON:
        raise RuntimeError(
            "Starting the backend requires Python 3.11 or 3.12; "
            f"current interpreter is {version[0]}.{version[1]}. "
            "Use a Python 3.11/3.12 virtual environment. "
            "--help and --print-config do not require backend dependencies."
        )


def checked_local_path(path: Path, root: Path) -> Path:
    """Do not follow a runtime symlink into another service's data directory."""
    if not path.resolve().is_relative_to(root.resolve()):
        raise RuntimeError(f"Development path escapes this repository: {path}")
    current = root.resolve()
    for part in path.relative_to(current).parts:
        current = current / part
        if current.is_symlink():
            raise RuntimeError(f"Symlinks are not allowed for development paths: {current}")
    return path


def configuration(root: Path, port: int, inherited: dict[str, str]) -> dict[str, str]:
    """Build a new environment rather than subtracting known production keys."""
    if not 1 <= port <= 65535:
        raise ValueError("port must be an integer from 1 to 65535")
    root = root.resolve()
    runtime = checked_local_path(root / ".local" / "dev", root)
    data = checked_local_path(runtime / "data", root)
    static = checked_local_path(runtime / "static", root)
    secret_file = checked_local_path(runtime / ".webui_secret_key", root)
    frontend = checked_local_path(root / ".build" / "frontend", root)
    if frontend.exists():
        if not (frontend / "index.html").is_file() or not (frontend / "static").is_dir():
            raise RuntimeError(f"Incomplete locked frontend build: {frontend}")
    else:
        frontend = checked_local_path(runtime / "no-frontend", root)
        if frontend.exists():
            raise RuntimeError(f"API-only placeholder must not exist: {frontend}")
    if not (root / "static" / "static").is_dir():
        raise RuntimeError("Missing official static/static source directory")

    env = {key: value for key, value in inherited.items() if key in OS_ENV_KEYS}
    env.update({
        "ENV": "dev",
        "DOCKER": "False",
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "WEBUI_URL": f"http://127.0.0.1:{port}",
        "DATA_DIR": str(data),
        "DATABASE_URL": f"sqlite:///{data / 'webui.db'}",
        "STATIC_DIR": str(static),
        "FRONTEND_BUILD_DIR": str(frontend),
        "FRONTEND_LOCK_FILE": str(root / "frontend.lock.json"),
        "WEBUI_SECRET_KEY_FILE": str(secret_file),
        "VECTOR_DB": "chroma",
        "CHROMA_HTTP_HOST": "",
        "STORAGE_PROVIDER": "local",
        "WEBSOCKET_MANAGER": "",
        "REDIS_URL": "",
        "WEBSOCKET_REDIS_URL": "",
        "CORS_ALLOW_ORIGIN": ";".join([
            "http://127.0.0.1:5173", "http://localhost:5173",
            "http://127.0.0.1:5175", "http://localhost:5175",
        ]),
        "PYTHONPATH": str(root / "backend"),
        "PYTHONDONTWRITEBYTECODE": "1",
        # env.py otherwise reads .env even after inherited variables are removed.
        "PYTHON_DOTENV_DISABLED": "1",
        # Do not reload remote settings previously entered in a local database.
        "ENABLE_PERSISTENT_CONFIG": "False",
        "ENABLE_OAUTH_PERSISTENT_CONFIG": "False",
        "ENABLE_OLLAMA_API": "False",
        "ENABLE_OPENAI_API": "False",
        "ENABLE_PLUGINS": "False",
        "ENABLE_VERSION_UPDATE_CHECK": "False",
        "OFFLINE_MODE": "True",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "RAG_EMBEDDING_MODEL": "",
        "RAG_EMBEDDING_MODEL_AUTO_UPDATE": "False",
        "RAG_RERANKING_MODEL": "",
        "RAG_RERANKING_MODEL_AUTO_UPDATE": "False",
        "RAG_EMBEDDING_MODEL_TRUST_REMOTE_CODE": "False",
        "RAG_RERANKING_MODEL_TRUST_REMOTE_CODE": "False",
        "WHISPER_MODEL_AUTO_UPDATE": "False",
        "USE_SLIM_DOCKER": "False",
        "USE_OLLAMA_DOCKER": "False",
        "USE_CUDA_DOCKER": "False",
        "HF_HOME": str(data / "cache" / "huggingface"),
        "SENTENCE_TRANSFORMERS_HOME": str(data / "cache" / "embedding"),
        "TORCH_HOME": str(data / "cache" / "torch"),
        "XDG_CACHE_HOME": str(data / "cache"),
        "WHISPER_MODEL_DIR": str(data / "cache" / "whisper"),
        "TIKTOKEN_CACHE_DIR": str(data / "cache" / "tiktoken"),
        "SCARF_NO_ANALYTICS": "true",
        "DO_NOT_TRACK": "true",
        "ANONYMIZED_TELEMETRY": "false",
    })
    return env


def prepare_runtime(root: Path, env: dict[str, str]) -> str:
    runtime = checked_local_path(root.resolve() / ".local" / "dev", root.resolve())
    # Reject existing symlinks, including nested upload/vector/cache objects,
    # before starting application migrations or storage initialization.
    if runtime.exists():
        for path in [runtime, *runtime.rglob("*")]:
            if path.is_symlink():
                raise RuntimeError(f"Symlinks are not allowed in development runtime: {path}")
    for name in ("DATA_DIR", "STATIC_DIR"):
        checked_local_path(Path(env[name]), root).mkdir(parents=True, exist_ok=True)
    key_file = checked_local_path(Path(env["WEBUI_SECRET_KEY_FILE"]), root)
    try:
        descriptor = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if not key_file.is_file() or key_file.is_symlink():
            raise RuntimeError(f"Invalid development secret file: {key_file}") from None
        key = key_file.read_text(encoding="utf-8").strip()
        if len(key) < 32:
            raise RuntimeError("Existing development secret is invalid; use a dedicated local key")
        key_file.chmod(0o600)
        return key
    key = secrets.token_urlsafe(48)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(key + "\n")
    return key


def launch_command(root: Path, port: int, reload: bool = True) -> list[str]:
    command = [
        sys.executable, "-m", "uvicorn", "open_webui.main:app",
        "--host", "127.0.0.1", "--port", str(port),
        "--forwarded-allow-ips", "127.0.0.1", "--workers", "1",
    ]
    if reload:
        command.extend(["--reload", "--reload-dir", str(root / "backend" / "open_webui")])
    return command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=port_number, default=8081)
    parser.add_argument("--print-config", action="store_true", help="Show safe settings without creating files")
    parser.add_argument("--no-reload", action="store_true", help="Run one process without watching source files")
    args = parser.parse_args(argv)
    try:
        env = configuration(REPO_ROOT, args.port, dict(os.environ))
        if args.print_config:
            # Do not dump the inherited OS environment or load/create the key.
            shown = {key: value for key, value in env.items() if key not in OS_ENV_KEYS}
            shown["WEBUI_SECRET_KEY"] = "<loaded locally on start; never displayed>"
            print(json.dumps(shown, indent=2, sort_keys=True))
            return 0
        require_supported_python()
        if importlib.util.find_spec("uvicorn") is None:
            raise RuntimeError(
                "uvicorn is not installed in this Python environment. "
                "Activate a Python 3.11/3.12 virtual environment and install "
                "backend/requirements.txt before starting; this script installs nothing."
            )
        env["WEBUI_SECRET_KEY"] = prepare_runtime(REPO_ROOT, env)
        os.chdir(REPO_ROOT / "backend")
        os.execve(sys.executable, launch_command(REPO_ROOT, args.port, not args.no_reload), env)
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"Development backend not started: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
