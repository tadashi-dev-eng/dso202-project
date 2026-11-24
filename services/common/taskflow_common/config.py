"""Environment-driven configuration.

All settings come from environment variables that the Helm library chart
fills from a ConfigMap (non-secret) and Secrets (credentials). Nothing is
read from files baked into the image, so one image runs in every
environment.
"""

import os


def env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def env_int(name: str, default: int) -> int:
    return int(env(name, str(default)))


def env_float(name: str, default: float) -> float:
    return float(env(name, str(default)))


SERVICE_NAME = env("SERVICE_NAME", "unknown-service")
SERVICE_VERSION = env("SERVICE_VERSION", "dev")
POD_NAME = env("POD_NAME", "local")
POD_IP = env("POD_IP", "127.0.0.1")
NAMESPACE = env("POD_NAMESPACE", "default")
PORT = env_int("PORT", 8000)
LOG_LEVEL = env("LOG_LEVEL", "INFO")


def service_url(name: str) -> str:
    """In-cluster base URL of another service.

    ``<NAME>_URL`` overrides it (e.g. ``LIST_SERVICE_URL``); otherwise the
    Service DNS name in the same namespace is used. The gateway prefers the
    etcd registry and only falls back to this.
    """
    override = env(name.upper().replace("-", "_") + "_URL")
    return override or f"http://{name}:8000"
