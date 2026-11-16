import io
import json
import logging

from app.logging_config import configure_logging


def read_log_lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


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


def test_uvicorn_logs_go_through_the_json_handler() -> None:
    uvicorn_logger = logging.getLogger("uvicorn.error")
    uvicorn_logger.addHandler(logging.NullHandler())
    uvicorn_logger.propagate = False
    stream = io.StringIO()

    configure_logging("INFO", stream=stream)
    uvicorn_logger.info("server_started")

    (entry,) = read_log_lines(stream)
    assert entry["logger"] == "uvicorn.error"
    assert uvicorn_logger.handlers == []


def test_configuring_twice_does_not_duplicate_log_lines() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    configure_logging("INFO", stream=stream)

    logging.getLogger("app.pipeline").info("once")

    assert len(read_log_lines(stream)) == 1
