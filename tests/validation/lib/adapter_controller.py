# tests/validation/lib/adapter_controller.py
"""
Day 6 integration tests - Mock Adapter container controller.

Purpose:
    Start/stop/restart individual Adapter containers via docker CLI,
    and override environment variables (e.g. SMS_SUCCESS_RATE) before restart.

Why this module exists:
    - TS-0002 (isolation): docker stop am-sms-adapter to simulate failure
    - TS-0003 (retry):     restart with SMS_SUCCESS_RATE=0.70 for partial failure
    - TS-0005 (DLQ):       restart with SUCCESS_RATE=0.0 to force all failures

Approach:
    - Uses subprocess to call `docker` CLI (already on host per Day 2 env)
    - On Windows/Git Bash, prefixes with MSYS_NO_PATHCONV=1 where needed
    - All operations are synchronous with timeouts
"""

import os
import subprocess
import time
import logging
import platform
from typing import Optional


# Known Adapter container names (matches docker-compose.adapters.yml)
KNOWN_ADAPTERS = {
    "SMS":   "am-sms-adapter",
    "MMS":   "am-mms-adapter",
    "RCS":   "am-rcs-adapter",
    "FAX":   "am-fax-adapter",
    "EMAIL": "am-email-adapter",
}

# Default timeout per docker command (seconds)
DOCKER_CMD_TIMEOUT = 30

# Detect Windows (Git Bash) - MSYS_NO_PATHCONV=1 avoids path mangling
IS_WINDOWS = platform.system() == "Windows"


log = logging.getLogger("adapter_controller")


class AdapterControlError(Exception):
    """Raised when docker CLI returns non-zero or times out."""


def _run_docker(args: list, timeout: int = DOCKER_CMD_TIMEOUT) -> subprocess.CompletedProcess:
    """
    Execute `docker <args...>` synchronously.

    Returns CompletedProcess with stdout/stderr captured.
    Raises AdapterControlError on failure.
    """
    cmd = ["docker"] + args
    env = os.environ.copy()
    if IS_WINDOWS:
        env["MSYS_NO_PATHCONV"] = "1"

    log.debug(f"exec: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise AdapterControlError(f"docker command timed out after {timeout}s: {' '.join(cmd)}")
    except FileNotFoundError:
        raise AdapterControlError("docker CLI not found on PATH")

    if result.returncode != 0:
        raise AdapterControlError(
            f"docker failed (rc={result.returncode}): {result.stderr.strip() or result.stdout.strip()}"
        )
    return result


class AdapterController:
    """
    High-level control over Mock Adapter containers.
    """

    @staticmethod
    def resolve_container(channel_or_name: str) -> str:
        """
        Accept either a channel shortcut ("SMS") or a full container name
        ("am-sms-adapter") and return the container name.
        """
        key = channel_or_name.upper()
        if key in KNOWN_ADAPTERS:
            return KNOWN_ADAPTERS[key]
        return channel_or_name  # assume raw container name

    # ─────────────────────────────────────────────────────────
    # Status queries
    # ─────────────────────────────────────────────────────────
    def is_running(self, channel_or_name: str) -> bool:
        """True if the container exists and is currently running."""
        container = self.resolve_container(channel_or_name)
        try:
            result = _run_docker(
                ["inspect", "-f", "{{.State.Running}}", container],
                timeout=5,
            )
            return result.stdout.strip().lower() == "true"
        except AdapterControlError:
            return False

    def get_status(self, channel_or_name: str) -> str:
        """Return container status string ('running', 'exited', 'not found')."""
        container = self.resolve_container(channel_or_name)
        try:
            result = _run_docker(
                ["inspect", "-f", "{{.State.Status}}", container],
                timeout=5,
            )
            return result.stdout.strip()
        except AdapterControlError:
            return "not found"

    def list_all(self) -> dict:
        """Return {channel: status} for all known adapters."""
        return {ch: self.get_status(name) for ch, name in KNOWN_ADAPTERS.items()}

    # ─────────────────────────────────────────────────────────
    # Stop / Start / Restart (TS-0002, TS-0003)
    # ─────────────────────────────────────────────────────────
    def stop(self, channel_or_name: str, wait_sec: int = 10) -> None:
        """
        Gracefully stop the container. Used in TS-0002 to simulate failure.
        """
        container = self.resolve_container(channel_or_name)
        log.info(f"Stopping container: {container}")
        _run_docker(["stop", "-t", str(wait_sec), container], timeout=wait_sec + 10)

    def start(self, channel_or_name: str) -> None:
        """Start a stopped container (reuses existing config)."""
        container = self.resolve_container(channel_or_name)
        log.info(f"Starting container: {container}")
        _run_docker(["start", container])

    def restart(self, channel_or_name: str, wait_sec: int = 10) -> None:
        """Restart the container (stop + start)."""
        container = self.resolve_container(channel_or_name)
        log.info(f"Restarting container: {container}")
        _run_docker(["restart", "-t", str(wait_sec), container], timeout=wait_sec + 10)

    # ─────────────────────────────────────────────────────────
    # Environment variable override + restart (TS-0003, TS-0005)
    # ─────────────────────────────────────────────────────────
    def restart_with_env(
        self,
        channel_or_name: str,
        env_overrides: dict,
        wait_ready_sec: int = 15,
    ) -> None:
        """
        Restart the container with environment variable overrides.

        This is non-trivial: docker doesn't let you change env vars of an existing
        container. The cleanest way is to `docker container rm` + `docker compose up`.
        For Day 6, we use a simpler approach: `docker compose up -d --no-deps \
        -e KEY=VAL <service>` is unreliable because compose needs the full YAML.

        Alternative (used here): we leverage that the container's entrypoint reads
        env at startup. We use `docker run --rm --env KEY=VAL` for a parallel
        instance OR simply restart with env passed via compose file mutation.

        For simplicity and scenario isolation, this method:
          1. Writes a temporary override env file
          2. Calls `docker compose -f ... -f override up -d --force-recreate <service>`

        If this fails on the user's env, fallback to manual env var setting in
        the scenario script (see TS-0003 implementation notes).

        NOTE: This method is best-effort. If it fails, raise a clear error so
        the test scenario can print a remediation hint.
        """
        container = self.resolve_container(channel_or_name)
        env_str = ", ".join(f"{k}={v}" for k, v in env_overrides.items())
        log.info(f"Restarting {container} with env overrides: {env_str}")
        log.warning(
            "restart_with_env is best-effort on docker-compose-managed containers. "
            "If this fails, set env vars in docker-compose.adapters.yml and re-up manually."
        )

        # Strategy 1: docker compose up with env override (requires compose file path)
        # We let the caller provide the compose file via COMPOSE_FILE env var.
        compose_file = os.getenv(
            "COMPOSE_ADAPTERS_FILE",
            "poc/docker/docker-compose.adapters.yml",
        )
        if not os.path.exists(compose_file):
            raise AdapterControlError(
                f"compose file not found: {compose_file} (set COMPOSE_ADAPTERS_FILE env var)"
            )

        # Build env flags to pass to docker compose
        env = os.environ.copy()
        for k, v in env_overrides.items():
            env[k] = str(v)
        if IS_WINDOWS:
            env["MSYS_NO_PATHCONV"] = "1"

        # Service name in compose file is lowercase channel (sms, mms, ...)
        channel_key = channel_or_name.upper()
        service_name = None
        for ch, cname in KNOWN_ADAPTERS.items():
            if ch == channel_key or cname == channel_or_name:
                service_name = f"{ch.lower()}-adapter"
                break
        if not service_name:
            raise AdapterControlError(f"Cannot resolve compose service name for {channel_or_name}")

        cmd = [
            "docker", "compose",
            "-f", compose_file,
            "up", "-d", "--force-recreate", "--no-deps",
            service_name,
        ]
        log.info(f"exec: {' '.join(cmd)}  (env overrides applied)")
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=60, env=env,
            )
        except subprocess.TimeoutExpired:
            raise AdapterControlError("docker compose up timed out after 60s")

        if result.returncode != 0:
            raise AdapterControlError(
                f"docker compose up failed: {result.stderr.strip() or result.stdout.strip()}"
            )

        # Wait for container to become healthy
        log.info(f"Waiting up to {wait_ready_sec}s for {container} to become ready...")
        for _ in range(wait_ready_sec):
            if self.is_running(container):
                time.sleep(1)  # small buffer for app init
                return
            time.sleep(1)
        raise AdapterControlError(f"{container} did not become ready within {wait_ready_sec}s")

    # ─────────────────────────────────────────────────────────
    # Context manager for guaranteed restore (stop -> test -> start)
    # ─────────────────────────────────────────────────────────
    def stopped(self, channel_or_name: str):
        """
        Context manager that stops the container on entry, starts it on exit.
        Guarantees restoration even if the test scenario fails.

        Usage:
            with controller.stopped("SMS"):
                # run test with SMS down
                ...
            # SMS is back up here
        """
        return _StoppedContext(self, channel_or_name)


class _StoppedContext:
    def __init__(self, controller: AdapterController, channel_or_name: str):
        self.controller = controller
        self.channel_or_name = channel_or_name
        self.was_running = False

    def __enter__(self):
        self.was_running = self.controller.is_running(self.channel_or_name)
        if self.was_running:
            self.controller.stop(self.channel_or_name)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.was_running:
            try:
                self.controller.start(self.channel_or_name)
                # small buffer for Adapter to re-subscribe to Kafka
                time.sleep(3)
            except Exception as e:
                log.error(f"Failed to restart {self.channel_or_name}: {e}")
        return False  # don't suppress exceptions


if __name__ == "__main__":
    # Self-check: print status of all 5 adapters
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ctrl = AdapterController()
    print("[AdapterController] current status:")
    for ch, status in ctrl.list_all().items():
        print(f"  {ch:6s} : {status}")