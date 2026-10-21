"""Stdlib tests: no Open WebUI dependencies, network, or real database needed."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dev_backend.py"
SPEC = importlib.util.spec_from_file_location("dev_backend", SCRIPT)
dev = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dev)


class DevelopmentBackendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / "static" / "static").mkdir(parents=True)

    def config(self, inherited=None, port=8081):
        return dev.configuration(self.root, port, inherited or {})

    def test_storage_and_frontend_are_isolated_without_creating_runtime(self):
        config = self.config()
        runtime = self.root / ".local" / "dev"
        self.assertEqual(config["DATA_DIR"], str(runtime / "data"))
        self.assertEqual(config["STATIC_DIR"], str(runtime / "static"))
        self.assertEqual(config["DATABASE_URL"], f"sqlite:///{runtime / 'data' / 'webui.db'}")
        self.assertEqual(config["FRONTEND_BUILD_DIR"], str(runtime / "no-frontend"))
        self.assertEqual(config["FRONTEND_LOCK_FILE"], str(self.root / "frontend.lock.json"))
        self.assertEqual(config["VECTOR_DB"], "chroma")
        self.assertEqual(config["STORAGE_PROVIDER"], "local")
        self.assertFalse(runtime.exists())

    def test_production_environment_is_not_inherited(self):
        inherited = {
            "PATH": "/safe/python/bin", "HOME": "/home/developer",
            "VIRTUAL_ENV": "/safe/venv", "SSL_CERT_FILE": "/safe/ca.pem",
            "DATABASE_URL": "postgresql://production:secret@prod/db",
            "DATABASE_HOST": "prod", "DATA_DIR": "/production/data",
            "WEBUI_SECRET_KEY": "production-secret", "REDIS_URL": "redis://prod",
            "CHROMA_HTTP_HOST": "production-chroma", "S3_BUCKET_NAME": "prod",
            "AWS_ACCESS_KEY_ID": "production-access", "HF_TOKEN": "production-token",
            "OPENAI_API_KEY": "production-key", "OLLAMA_BASE_URL": "http://prod",
            "WEBUI_ADMIN_EMAIL": "admin@prod", "WEBUI_ADMIN_PASSWORD": "secret",
            "HTTP_PROXY": "http://prod-proxy", "PYTHONPATH": "/production/modules",
            "ENV_FILE": "/production/.env", "PYTHON_DOTENV_DISABLED": "0",
            "ENABLE_PERSISTENT_CONFIG": "True", "RAG_EMBEDDING_MODEL": "remote-model",
        }
        config = self.config(inherited)
        for key in ("PATH", "HOME", "VIRTUAL_ENV", "SSL_CERT_FILE"):
            self.assertEqual(config[key], inherited[key])
        for key in ("DATABASE_HOST", "S3_BUCKET_NAME", "AWS_ACCESS_KEY_ID", "HF_TOKEN",
                    "OPENAI_API_KEY", "OLLAMA_BASE_URL", "WEBUI_ADMIN_EMAIL",
                    "WEBUI_ADMIN_PASSWORD", "HTTP_PROXY", "ENV_FILE", "WEBUI_SECRET_KEY"):
            self.assertNotIn(key, config)
        self.assertEqual(config["CHROMA_HTTP_HOST"], "")
        self.assertEqual(config["REDIS_URL"], "")
        self.assertEqual(config["PYTHONPATH"], str(self.root / "backend"))
        self.assertEqual(config["PYTHON_DOTENV_DISABLED"], "1")
        self.assertEqual(config["ENABLE_PERSISTENT_CONFIG"], "False")
        self.assertEqual(config["RAG_EMBEDDING_MODEL"], "")
        self.assertEqual(config["OFFLINE_MODE"], "True")
        self.assertNotIn("production", json.dumps(config))

    def test_print_config_on_unsupported_python_does_not_read_or_create_secret(self):
        inherited = {"WEBUI_SECRET_KEY": "never-show-this", "DATABASE_URL": "postgresql://prod"}
        output = io.StringIO()
        with patch.object(dev, "REPO_ROOT", self.root), patch.dict(os.environ, inherited, clear=True), \
                patch.object(dev, "require_supported_python", side_effect=AssertionError("must not run")), \
                patch.object(dev, "prepare_runtime", side_effect=AssertionError("must not run")), \
                contextlib.redirect_stdout(output):
            self.assertEqual(dev.main(["--print-config", "--port", "8099"]), 0)
        displayed = json.loads(output.getvalue())
        self.assertEqual(displayed["PORT"], "8099")
        self.assertEqual(displayed["HOST"], "127.0.0.1")
        self.assertNotIn("never-show-this", output.getvalue())
        self.assertNotIn("postgresql://prod", output.getvalue())
        self.assertFalse((self.root / ".local").exists())

    def test_secret_is_generated_once_in_dedicated_runtime(self):
        config = self.config()
        first = dev.prepare_runtime(self.root, config)
        second = dev.prepare_runtime(self.root, config)
        self.assertEqual(first, second)
        self.assertGreaterEqual(len(first), 32)
        key_file = self.root / ".local" / "dev" / ".webui_secret_key"
        self.assertEqual(key_file.read_text().strip(), first)
        self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)
        self.assertTrue(Path(config["STATIC_DIR"]).is_dir())
        self.assertFalse(Path(config["FRONTEND_BUILD_DIR"]).exists())
        self.assertNotIn("WEBUI_SECRET_KEY", config)

    def test_runtime_symlinks_cannot_target_another_service(self):
        external = self.root.parent / "other-service"
        (self.root / ".local").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "escapes"):
            self.config()

    def test_nested_storage_symlink_is_rejected_before_secret_creation(self):
        config = self.config()
        data = Path(config["DATA_DIR"])
        data.mkdir(parents=True)
        (data / "uploads").symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "Symlinks"):
            dev.prepare_runtime(self.root, config)
        self.assertFalse(Path(config["WEBUI_SECRET_KEY_FILE"]).exists())

    def test_internal_parent_symlink_cannot_redirect_runtime_into_source(self):
        source = self.root / "backend"
        source.mkdir()
        (self.root / ".local").symlink_to(source, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "Symlinks"):
            self.config()
        self.assertFalse((source / "dev").exists())

    def test_frontend_is_only_used_when_complete(self):
        frontend = self.root / ".build" / "frontend"
        frontend.mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError, "Incomplete"):
            self.config()
        (frontend / "index.html").write_text("<html></html>")
        (frontend / "static").mkdir()
        self.assertEqual(self.config()["FRONTEND_BUILD_DIR"], str(frontend))

    def test_api_only_placeholder_cannot_be_a_stale_frontend(self):
        (self.root / ".local" / "dev" / "no-frontend").mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError, "must not exist"):
            self.config()

    def test_ports_are_validated(self):
        for value in ("0", "65536", "-1", "http", "8081.0"):
            with self.subTest(value=value), self.assertRaises(Exception):
                dev.port_number(value)
        self.assertEqual(dev.port_number("8081"), 8081)
        with self.assertRaises(ValueError):
            self.config(port=0)

    def test_only_supported_python_starts_backend(self):
        for version in ((3, 11, 0), (3, 12, 8)):
            dev.require_supported_python(version)
        for version in ((3, 10, 9), (3, 13, 0), (3, 14, 0)):
            with self.subTest(version=version), self.assertRaisesRegex(RuntimeError, "Python 3.11 or 3.12"):
                dev.require_supported_python(version)

    def test_missing_uvicorn_reports_setup_without_creating_runtime(self):
        stderr = io.StringIO()
        with patch.object(dev, "REPO_ROOT", self.root), \
                patch.object(dev, "require_supported_python"), \
                patch.object(dev.importlib.util, "find_spec", return_value=None), \
                contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as error:
            dev.main([])
        self.assertEqual(error.exception.code, 1)
        self.assertIn("uvicorn is not installed", stderr.getvalue())
        self.assertFalse((self.root / ".local").exists())

    def test_launch_passes_sanitized_environment_and_local_secret(self):
        inherited = {"WEBUI_SECRET_KEY": "production-secret", "OPENAI_API_KEY": "production-key"}
        with patch.object(dev, "REPO_ROOT", self.root), patch.dict(os.environ, inherited, clear=True), \
                patch.object(dev, "require_supported_python"), \
                patch.object(dev.importlib.util, "find_spec", return_value=object()), \
                patch.object(dev.os, "chdir"), patch.object(dev.os, "execve") as execute:
            self.assertEqual(dev.main(["--no-reload"]), 0)
        interpreter, command, environment = execute.call_args.args
        self.assertEqual(interpreter, dev.sys.executable)
        self.assertEqual(environment["HOST"], "127.0.0.1")
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotEqual(environment["WEBUI_SECRET_KEY"], "production-secret")
        self.assertEqual(environment["WEBUI_SECRET_KEY"], Path(environment["WEBUI_SECRET_KEY_FILE"]).read_text().strip())
        self.assertNotIn("--reload", command)

    def test_reload_only_watches_application_source(self):
        command = dev.launch_command(self.root, 8081)
        self.assertEqual(command[command.index("--host") + 1], "127.0.0.1")
        self.assertEqual(command[command.index("--reload-dir") + 1], str(self.root / "backend" / "open_webui"))
        self.assertEqual(command.count("--reload-dir"), 1)
        self.assertNotIn("--reload", dev.launch_command(self.root, 8081, reload=False))


if __name__ == "__main__":
    unittest.main()
