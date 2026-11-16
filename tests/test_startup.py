"""Checks on what a real API process prints. All inputs are synthetic."""

import json
from pathlib import Path

import pytest

from tests.api_process import read_output, run_api_until_exit, running_api
from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL

VALID_ENVIRONMENT = {
    "LOG_LEVEL": "INFO",
    "DATABASE_URL": TEST_DATABASE_URL,
    "REDIS_URL": TEST_REDIS_URL,
}
SYNTHETIC_PASSWORD = "synthetic-pw-9f2c"
SYNTHETIC_QUERY_TOKEN = "synthetic-query-token-41d7"
# Kept short on purpose: Pydantic shortens long rejected values, which would hide
# the password by accident and let these tests pass without the fix.
MALFORMED_URLS = {
    "DATABASE_URL": f"postgresql://u:{SYNTHETIC_PASSWORD}@h:not-a-port/d",
    "REDIS_URL": f"redis://u:{SYNTHETIC_PASSWORD}@h:not-a-port/0",
}
WEBSOCKET_UPGRADE_HEADERS = {
    "Connection": "Upgrade",
    "Upgrade": "websocket",
    "Sec-WebSocket-Version": "13",
    # The sample handshake key from RFC 6455, not a secret.
    "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
}


def parse_log_lines(output: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.splitlines()]


@pytest.mark.parametrize("variable", sorted(MALFORMED_URLS))
def test_startup_with_malformed_url_names_the_field_but_prints_no_credentials(
    tmp_path: Path, variable: str
) -> None:
    environment = {**VALID_ENVIRONMENT, variable: MALFORMED_URLS[variable]}

    output = run_api_until_exit(environment, tmp_path)

    assert variable.lower() in output
    assert SYNTHETIC_PASSWORD not in output
    assert "input_value" not in output


def test_request_logs_contain_no_query_values_or_client_addresses(tmp_path: Path) -> None:
    with running_api(VALID_ENVIRONMENT, tmp_path) as api:
        response = api.get(f"/health?token={SYNTHETIC_QUERY_TOKEN}")
    output = read_output(tmp_path)

    assert response.status == 200
    assert "app_created" in output
    assert SYNTHETIC_QUERY_TOKEN not in output
    assert response.client_address not in output
    assert all(entry["logger"] != "uvicorn.access" for entry in parse_log_lines(output))


def test_websocket_upgrade_logs_contain_no_query_values_or_client_addresses(
    tmp_path: Path,
) -> None:
    with running_api(VALID_ENVIRONMENT, tmp_path) as api:
        response = api.get(
            f"/health?token={SYNTHETIC_QUERY_TOKEN}", headers=WEBSOCKET_UPGRADE_HEADERS
        )
    output = read_output(tmp_path)

    assert "app_created" in output
    assert SYNTHETIC_QUERY_TOKEN not in output
    assert response.client_address not in output


def test_error_level_keeps_info_records_out_of_a_real_server_log(tmp_path: Path) -> None:
    with running_api({**VALID_ENVIRONMENT, "LOG_LEVEL": "ERROR"}, tmp_path) as api:
        response = api.get("/health")
    output = read_output(tmp_path)

    assert response.status == 200
    assert [entry for entry in parse_log_lines(output) if entry["level"] != "ERROR"] == []
