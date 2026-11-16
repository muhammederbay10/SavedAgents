"""Runs the API as a real uvicorn process on the loopback interface.

For tests that must see what an actual start writes to stdout and stderr. The
process is started with the options from the image's start command, gets only
the environment passed in, and runs in an empty directory so it never reads a
developer's `.env`.
"""

import http.client
import json
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
LOOPBACK_HOST = "127.0.0.1"
STARTUP_TIMEOUT_SECONDS = 15.0
SHUTDOWN_TIMEOUT_SECONDS = 10.0
REQUEST_TIMEOUT_SECONDS = 5.0
READINESS_POLL_SECONDS = 0.05
OUTPUT_FILE_NAME = "api-output.log"
DOCKERFILE_NAME = "Dockerfile"
DOCKERFILE_COMMAND_PREFIX = "CMD "


@dataclass(frozen=True)
class ApiResponse:
    """What one request to the running API returned, and who sent it."""

    status: int
    client_address: str


@dataclass(frozen=True)
class RunningApi:
    """Handle to a started API process."""

    port: int

    def get(self, path: str, headers: dict[str, str] | None = None) -> ApiResponse:
        connection = http.client.HTTPConnection(
            LOOPBACK_HOST, self.port, timeout=REQUEST_TIMEOUT_SECONDS
        )
        try:
            connection.request("GET", path, headers=headers or {})
            client_host, client_port = connection.sock.getsockname()
            response = connection.getresponse()
            response.read()
            return ApiResponse(
                status=response.status, client_address=f"{client_host}:{client_port}"
            )
        finally:
            connection.close()


def find_free_port() -> int:
    with socket.socket() as probe:
        probe.bind((LOOPBACK_HOST, 0))
        port: int = probe.getsockname()[1]
        return port


def image_start_command() -> list[str]:
    """Return the API image's start command, so tests start the API the way the image does."""
    for line in (REPOSITORY_ROOT / DOCKERFILE_NAME).read_text().splitlines():
        if line.startswith(DOCKERFILE_COMMAND_PREFIX):
            command: list[str] = json.loads(line.removeprefix(DOCKERFILE_COMMAND_PREFIX))
            return command
    raise LookupError(f"{DOCKERFILE_NAME} has no exec-form CMD line")


def with_option(command: list[str], option: str, value: str) -> list[str]:
    value_index = command.index(option) + 1
    return [*command[:value_index], value, *command[value_index + 1 :]]


def uvicorn_command(port: int) -> list[str]:
    """Build the image's start command for this machine: same options, loopback address."""
    executable, *options = image_start_command()
    assert executable == "uvicorn", "the image is expected to start uvicorn directly"
    local_options = with_option(with_option(options, "--host", LOOPBACK_HOST), "--port", str(port))
    return [sys.executable, "-m", "uvicorn", *local_options]


def process_environment(settings_environment: dict[str, str]) -> dict[str, str]:
    return {"PYTHONPATH": str(REPOSITORY_ROOT), **settings_environment}


def run_api_until_exit(settings_environment: dict[str, str], working_directory: Path) -> str:
    """Start the API, wait for it to exit by itself, and return everything it printed."""
    completed = subprocess.run(
        uvicorn_command(find_free_port()),
        env=process_environment(settings_environment),
        cwd=working_directory,
        capture_output=True,
        text=True,
        timeout=STARTUP_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode != 0, "the API was expected to refuse to start"
    return completed.stdout + completed.stderr


def _wait_until_ready(api: RunningApi, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"the API exited during startup with code {process.returncode}")
        try:
            api.get("/health")
        except OSError:
            time.sleep(READINESS_POLL_SECONDS)
        else:
            return
    raise TimeoutError("the API did not answer /health in time")


def _stop(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


@contextmanager
def running_api(
    settings_environment: dict[str, str], working_directory: Path
) -> Iterator[RunningApi]:
    """Start the API, yield a handle once `/health` answers, and stop it afterwards.

    Read the output with `read_output` after the `with` block, when the process
    has exited and flushed its logs.
    """
    api = RunningApi(port=find_free_port())
    with (working_directory / OUTPUT_FILE_NAME).open("wb") as output_file:
        process = subprocess.Popen(
            uvicorn_command(api.port),
            env=process_environment(settings_environment),
            cwd=working_directory,
            stdout=output_file,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_until_ready(api, process)
            yield api
        finally:
            _stop(process)


def read_output(working_directory: Path) -> str:
    return (working_directory / OUTPUT_FILE_NAME).read_text()
