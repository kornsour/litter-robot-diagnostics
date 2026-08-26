import io
import logging
from pathlib import Path

from lr4_diagnostics.cli import _configure_logging, _line_buffer, main
from lr4_diagnostics.store import EventStore


def test_a_redirected_stdout_gets_line_buffered(tmp_path: Path) -> None:
    """The armed banner must not wait for process exit to reach the log.

    Redirecting to a file is exactly the case that block-buffers, and it is how
    the watchdog runs both in the background locally and under Lambda.
    """
    with (tmp_path / "out.log").open("w") as stream:
        assert not stream.line_buffering
        _line_buffer(stream)
        assert stream.line_buffering


def test_the_banner_reaches_the_file_before_the_process_would_exit(tmp_path: Path) -> None:
    path = tmp_path / "out.log"
    with path.open("w") as stream:
        _line_buffer(stream)
        print("ARMED - commands will be sent", file=stream)
        # Deliberately read before the `with` closes the stream: an unflushed
        # buffer is the bug, so reading after the implicit close proves nothing.
        assert "ARMED" in path.read_text()


def test_line_buffer_ignores_a_stream_it_cannot_reconfigure() -> None:
    """`capsys` and friends swap in substitutes with no `reconfigure`."""
    _line_buffer(io.StringIO())


def test_verbose_does_not_let_botocore_log_request_bodies() -> None:
    """`--verbose` once printed raw Cognito refresh and ID tokens to the console.

    botocore logs whole request and response bodies at DEBUG, and the auth
    exchange carries live credentials in both directions.
    """
    _configure_logging(verbose=True)
    for name in ("botocore.endpoint", "botocore.parsers", "urllib3.connectionpool"):
        assert not logging.getLogger(name).isEnabledFor(logging.DEBUG), name


def test_verbose_still_enables_our_own_debug_logging() -> None:
    _configure_logging(verbose=True)
    assert logging.getLogger("lr4_diagnostics.autoreset").isEnabledFor(logging.DEBUG)


def test_without_verbose_our_logging_stops_at_info() -> None:
    _configure_logging(verbose=False)
    watchdog = logging.getLogger("lr4_diagnostics.autoreset")
    assert watchdog.isEnabledFor(logging.INFO)
    assert not watchdog.isEnabledFor(logging.DEBUG)


def test_summary_prints_counts(tmp_path: Path, capsys: object) -> None:
    database = tmp_path / "capture.sqlite"
    with EventStore(database) as store:
        store.add(
            observed_at="2026-07-25T12:00:00+00:00",
            source="state",
            robot_id="lr4-test",
            payload={"robotCycleState": "CYCLE_STATE_CAT_DETECT"},
        )

    assert main(["summary", "--database", str(database), "--limit", "1"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert '"state": 1' in output
    assert "CYCLE_STATE_CAT_DETECT" in output
