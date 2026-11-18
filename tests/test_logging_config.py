import io
import json
import logging

import httpx
import uvicorn

from app.logging_config import configure_logging


def read_log_lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def apply_uvicorn_logging_configuration() -> None:
    """Let uvicorn install its own handlers and levels, as it does before loading the app."""
    uvicorn.Config("app.main:create_app", factory=True)


def test_log_record_is_written_as_one_json_object_with_extra_fields() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)

    logging.getLogger("app.pipeline").info("item_indexed", extra={"item_id": "abc", "step": 4})

    (entry,) = read_log_lines(stream)
    assert entry["level"] == "INFO"
    assert entry["logger"] == "app.pipeline"
    assert entry["message"] == "item_indexed"
    assert entry["item_id"] == "abc"
    assert entry["step"] == 4
    assert isinstance(entry["timestamp"], str)
    assert entry["timestamp"].endswith("+00:00")


def test_records_below_the_configured_level_are_dropped() -> None:
    stream = io.StringIO()
    configure_logging("WARNING", stream=stream)

    logger = logging.getLogger("app.pipeline")
    logger.info("ignored")
    logger.warning("kept")

    assert [entry["message"] for entry in read_log_lines(stream)] == ["kept"]


def test_configured_level_also_applies_to_loggers_that_set_their_own_level() -> None:
    library_logger = logging.getLogger("some_library")
    library_logger.setLevel(logging.DEBUG)
    stream = io.StringIO()
    configure_logging("ERROR", stream=stream)

    library_logger.info("ignored")
    library_logger.error("kept")

    assert [entry["message"] for entry in read_log_lines(stream)] == ["kept"]


def test_exception_traceback_is_included() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)

    try:
        raise ValueError("resolver returned no candidates")
    except ValueError:
        logging.getLogger("app.pipeline").exception("resolve_failed")

    (entry,) = read_log_lines(stream)
    assert entry["message"] == "resolve_failed"
    assert "ValueError: resolver returned no candidates" in str(entry["exception"])


def test_uvicorn_server_logs_go_through_the_json_handler() -> None:
    apply_uvicorn_logging_configuration()
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)

    logging.getLogger("uvicorn.error").info("server_started")

    (entry,) = read_log_lines(stream)
    assert entry["logger"] == "uvicorn.error"
    assert entry["message"] == "server_started"


def test_error_level_drops_uvicorn_info_records_and_keeps_error_records() -> None:
    apply_uvicorn_logging_configuration()
    stream = io.StringIO()
    configure_logging("ERROR", stream=stream)

    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).info("ignored")
        logging.getLogger(name).error("kept")

    assert [(entry["logger"], entry["message"]) for entry in read_log_lines(stream)] == [
        ("uvicorn", "kept"),
        ("uvicorn.error", "kept"),
    ]


def test_uvicorn_access_records_are_never_written() -> None:
    apply_uvicorn_logging_configuration()
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)

    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d',
        "203.0.113.7:51000",
        "GET",
        "/health?token=synthetic-query-token-41d7",
        "1.1",
        200,
    )

    assert stream.getvalue() == ""
    # Uvicorn skips building access records at all when this is false.
    assert not logging.getLogger("uvicorn.access").hasHandlers()


def test_configuring_twice_does_not_duplicate_log_lines() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    configure_logging("INFO", stream=stream)

    logging.getLogger("app.pipeline").info("once")

    assert len(read_log_lines(stream)) == 1


async def test_http_request_lines_with_full_urls_are_not_written_below_warning() -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)
    signed_link = "https://cdn.example.test/video.mp4?signature=SYNTHETIC_SIGNATURE"
    transport = httpx.MockTransport(lambda request: httpx.Response(200))

    async with httpx.AsyncClient(transport=transport) as client:
        await client.get(signed_link)
    logging.getLogger("httpx").warning("kept")

    assert "SYNTHETIC_SIGNATURE" not in stream.getvalue()
    assert [entry["message"] for entry in read_log_lines(stream)] == ["kept"]
