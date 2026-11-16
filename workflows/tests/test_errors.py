"""Shared error contract and test isolation guards."""

import os

import errors


def test_upstream_error_is_runtime_error():
    exc = errors.UpstreamError("reddit 503")
    assert isinstance(exc, RuntimeError)
    assert str(exc) == "reddit 503"


def test_heartbeat_seen_path_is_isolated_from_live_data():
    path = os.environ["HEARTBEAT_SEEN_PATH"]
    assert not path.startswith("/data/")
