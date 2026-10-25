"""Accepted session work remains observable until I/O and occupancy have settled."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.leases import ResourceLeases
from lab_control.application.sessions import SessionManager
from lab_control.demo.instrument import SimulatedInstrument
from lab_control.domain.instruments import DeviceCommand
from lab_control.storage.repository import Repository


def block_worker(worker):
    entered, release = threading.Event(), threading.Event()

    def wait():
        entered.set()
        assert release.wait(5)

    blocker = worker.submit(wait)
    assert entered.wait(3)
    return release, blocker


@pytest.mark.parametrize("operation", ["status", "apply", "idle_read"])
def test_accepted_operation_cannot_cancel_occupancy_or_io(tmp_path, monkeypatch, operation):
    repository = Repository(tmp_path)
    sessions = SessionManager(ResourceLeases(), repository, SimulatedInstrument)
    sessions.connect("test", ConnectionConfig("mock://admission", "rigol.dg5504pro", "mock")).result(3)
    session = sessions.sessions["test"]
    adapter = session.adapter
    release, blocker = block_worker(session.worker)
    calls, closed = [], []
    method = {"status": "read_status", "apply": "apply", "idle_read": "read_settings"}[operation]
    original = getattr(adapter, method)

    def observe(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(adapter, method, observe)
    monkeypatch.setattr(adapter, "close", lambda: closed.append(True))
    cancelled = False
    try:
        if operation == "idle_read":
            future = sessions.try_submit_idle("test", "read_settings", (("frequency",), 1), quiet=True)
        else:
            argument = DeviceCommand("frequency", {"value": 1000}) if operation == "apply" else None
            future = sessions.submit("test", operation, argument)
        assert future is not None
        assert session.pending == 1
        assert session.control_pending == int(operation == "apply")
        cancelled = future.cancel()
        release.set()
        blocker.result(3)
        if not cancelled:
            future.result(3)
        assert session.pending == 0
        assert session.control_pending == 0
        assert not cancelled
        assert future.done() and not future.cancelled()
        assert len(calls) == 1
        sessions.close()
        assert not sessions.sessions
        assert closed == [True]
        assert sessions.leases.owner("mock://admission") is None
    finally:
        release.set()
        blocker.result(3)
        if cancelled:  # Let the regression fail against the old implementation without leaking its worker.
            session.pending = session.control_pending = 0
        sessions.close()
        repository.close()


def test_accepted_connect_cannot_cancel_reserved_session(tmp_path, monkeypatch):
    repository = Repository(tmp_path)
    sessions = SessionManager(ResourceLeases(), repository, SimulatedInstrument)
    workers = []

    def delayed_worker(*args, **kwargs):
        worker = ThreadPoolExecutor(*args, **kwargs)
        release, blocker = block_worker(worker)
        workers.append((release, blocker))
        return worker

    monkeypatch.setattr("lab_control.application.sessions.ThreadPoolExecutor", delayed_worker)
    cancelled = False
    try:
        future = sessions.connect("test", ConnectionConfig("mock://connecting", "rigol.dg5504pro", "mock"))
        release, blocker = workers[0]
        cancelled = future.cancel()
        release.set()
        blocker.result(3)
        assert not cancelled
        identity = future.result(3)
        assert future.done() and not future.cancelled()
        assert sessions.sessions["test"].identity == identity
        assert sessions.sessions["test"].pending == 0
        sessions.close()
        assert not sessions.sessions
        assert sessions.leases.owner("mock://connecting") is None
    finally:
        if workers:
            workers[0][0].set()
            workers[0][1].result(3)
        if cancelled:
            sessions.sessions["test"].state = "error"
        sessions.close()
        repository.close()
