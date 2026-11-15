"""Tests for the simple_socket_server package."""

import importlib.metadata
import os
import re
import socket
import struct
import threading
import time

import pytest

import simple_socket_server
from simple_socket_server import SimpleSocketServer, SimpleSocketServerException

EVENT_NAMES = ("start", "connect", "message", "disconnect", "error", "stop")

LOCAL_HOST = "127.0.0.1"


def recv_exactly(sock, size, timeout=10.0):
    """Read exactly size bytes, failing the test instead of hanging forever."""
    sock.settimeout(timeout)
    data = b""
    while len(data) < size:
        chunk = sock.recv(min(size - len(data), 65536))
        if not chunk:
            raise AssertionError(
                "connection closed after %d of %d bytes" % (len(data), size)
            )
        data += chunk
    return data


class RunningServer:
    """A server executing in its own thread, with every event recorded."""

    def __init__(self, handlers=None, **run_kwargs):
        self.server = SimpleSocketServer()
        self.handlers = dict(handlers or {})
        self.run_kwargs = {"host": LOCAL_HOST, "port": 0, "max_conn": 5}
        self.run_kwargs.update(run_kwargs)
        self.events = []
        self.error = None
        self._lock = threading.Lock()
        self._started = threading.Event()
        self._finished = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _record(self, name):
        def handler(*args):
            with self._lock:
                self.events.append((name, args))
            if name == "start":
                self._started.set()

        return handler

    def _run(self):
        try:
            self.server.run(**self.run_kwargs)
        except BaseException as error:  # noqa: BLE001 - kept for the assertions
            self.error = error
        finally:
            self._finished.set()

    def start(self):
        for name in EVENT_NAMES:
            self.server.on(name)(self._record(name))
        for name, handler in self.handlers.items():
            self.server.on(name)(handler)
        self.thread.start()
        if not self._started.wait(10):
            raise AssertionError("the server did not report the start event")
        return self

    @property
    def address(self):
        return self.server.host, self.server.port

    def connect(self, timeout=10.0):
        return socket.create_connection(self.address, timeout=timeout)

    def events_of(self, name):
        with self._lock:
            return [args for event, args in self.events if event == name]

    def wait_for(self, name, count=1, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.events_of(name)
            if len(found) >= count:
                return found
            time.sleep(0.01)
        raise AssertionError(
            "event %r did not arrive %d time(s), recorded: %r"
            % (name, count, [event for event, _ in self.events])
        )

    def stop(self):
        self.server.stop()
        assert self._finished.wait(10), "run() did not return after stop()"
        self.thread.join(10)
        assert not self.thread.is_alive()


@pytest.fixture
def running_server():
    server = RunningServer().start()
    try:
        yield server
    finally:
        if server.server.is_running:
            server.stop()


@pytest.fixture
def echo_server():
    state = {}

    def on_message(sock, _peer, data):
        state["server"].send(sock, b"echo:" + data)

    def on_connect(sock, _peer):
        state["server"].send(sock, b"hello\r\n")

    server = RunningServer({"message": on_message, "connect": on_connect})
    state["server"] = server.server
    server.start()
    try:
        yield server
    finally:
        if server.server.is_running:
            server.stop()


def test_start_reports_the_bound_address(running_server):
    host, port = running_server.events_of("start")[0]
    assert host == LOCAL_HOST
    assert port > 0
    assert running_server.server.port == port
    assert running_server.server.host == host
    assert running_server.server.is_running


def test_connect_event_carries_the_client_address(running_server):
    client = running_server.connect()
    try:
        (sock, peer), = running_server.wait_for("connect")
        assert isinstance(sock, socket.socket)
        assert peer == client.getsockname()[:2]
    finally:
        client.close()


def test_message_round_trip_and_ordering(echo_server):
    client = echo_server.connect()
    try:
        assert recv_exactly(client, len(b"hello\r\n")) == b"hello\r\n"
        client.sendall(b"ping")
        assert recv_exactly(client, len(b"echo:ping")) == b"echo:ping"
        client.sendall(b"pong")
        assert recv_exactly(client, len(b"echo:pong")) == b"echo:pong"
    finally:
        client.close()

    messages = [data for _sock, _peer, data in echo_server.events_of("message")]
    assert messages == [b"ping", b"pong"]


def test_large_message_is_delivered_in_full():
    payload = bytes(range(256)) * 1024  # 256 KiB, far above the socket buffer

    def on_message(sock, _peer, _data):
        state["server"].send(sock, payload)

    state = {}
    server = RunningServer({"message": on_message})
    state["server"] = server.server
    server.start()
    try:
        client = server.connect()
        try:
            client.sendall(b"go")
            assert recv_exactly(client, len(payload)) == payload
        finally:
            client.close()
    finally:
        server.stop()


def test_disconnect_when_the_client_closes_gracefully(running_server):
    client = running_server.connect()
    running_server.wait_for("connect")
    client.close()

    ((_sock, peer),) = running_server.wait_for("disconnect")
    assert peer[0] == LOCAL_HOST
    assert running_server.server.is_running, "the server keeps serving"


def test_disconnect_when_the_connection_is_reset(running_server):
    client = running_server.connect()
    running_server.wait_for("connect")
    # Windows LINGER is two shorts, POSIX is two ints; a zero linger sends RST.
    linger = struct.pack("hh", 1, 0) if os.name == "nt" else struct.pack("ii", 1, 0)
    client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
    client.close()

    running_server.wait_for("disconnect")
    assert running_server.server.is_running


def test_server_keeps_accepting_after_a_disconnect(running_server):
    first = running_server.connect()
    running_server.wait_for("connect")
    first.close()
    running_server.wait_for("disconnect")

    second = running_server.connect()
    try:
        running_server.wait_for("connect", count=2)
        server_addresses = [peer for _sock, peer in running_server.events_of("connect")]
        assert server_addresses[0] != server_addresses[1]
    finally:
        second.close()


def test_send_after_disconnect_raises_value_error(running_server):
    client = running_server.connect()
    ((sock, _peer),) = running_server.wait_for("connect")
    client.close()
    running_server.wait_for("disconnect")

    with pytest.raises(ValueError):
        running_server.server.send(sock, b"late")


def test_send_to_a_foreign_socket_raises_value_error(running_server):
    stranger = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(ValueError):
            running_server.server.send(stranger, b"hello")
    finally:
        stranger.close()


def test_empty_message_is_ignored(running_server):
    client = running_server.connect()
    try:
        ((sock, _peer),) = running_server.wait_for("connect")
        running_server.server.send(sock, b"")
        client.settimeout(0.5)
        with pytest.raises(socket.timeout):
            client.recv(1)
    finally:
        client.close()


def test_stop_closes_every_socket_and_emits_stop(running_server):
    client = running_server.connect()
    running_server.wait_for("connect")
    host, port = running_server.address

    running_server.stop()

    assert running_server.events_of("stop") == [(host, port)]
    assert not running_server.server.is_running
    assert client.recv(1) == b"", "the client sees the connection closed"
    client.close()

    names = [event for event, _args in running_server.events]
    assert names.index("disconnect") < names.index("stop")


def test_stop_is_idempotent_and_safe_before_run():
    server = SimpleSocketServer()
    server.stop()
    server.stop()
    assert not server.is_running


def test_run_cannot_be_started_twice(running_server):
    with pytest.raises(RuntimeError):
        running_server.server.run(host=LOCAL_HOST, port=0)


def test_port_is_released_after_stop():
    first = RunningServer().start()
    _host, port = first.address
    first.stop()

    second = RunningServer(port=port).start()
    try:
        assert second.address == (LOCAL_HOST, port)
        client = second.connect()
        client.close()
    finally:
        second.stop()


def test_unbindable_address_raises_server_exception():
    server = SimpleSocketServer()
    with pytest.raises(SimpleSocketServerException):
        server.run(host="203.0.113.1", port=0)
    assert not server.is_running


def test_two_servers_are_independent():
    state = {}

    def on_first(sock, _peer, data):
        state["first"].send(sock, b"first:" + data)

    def on_second(sock, _peer, data):
        state["second"].send(sock, b"second:" + data)

    first = RunningServer({"message": on_first})
    second = RunningServer({"message": on_second})
    state["first"] = first.server
    state["second"] = second.server
    first.start()
    second.start()
    try:
        assert first.address != second.address
        first_client = first.connect()
        second_client = second.connect()
        try:
            first_client.sendall(b"x")
            second_client.sendall(b"x")
            assert recv_exactly(first_client, len(b"first:x")) == b"first:x"
            assert recv_exactly(second_client, len(b"second:x")) == b"second:x"
        finally:
            first_client.close()
            second_client.close()
    finally:
        first.stop()
        second.stop()


def test_instances_are_not_shared():
    assert SimpleSocketServer() is not SimpleSocketServer()


def test_public_api_is_explicit():
    for name in simple_socket_server.__all__:
        assert not name.startswith("_"), "%s is not a public name" % name
        assert hasattr(simple_socket_server, name), "%s is in __all__ but missing" % name
    assert re.match(r"^\d+\.\d+(\.\d+)?$", simple_socket_server.__version__)
    assert issubclass(SimpleSocketServerException, OSError)


def test_declared_version_matches_the_package_metadata():
    try:
        installed = importlib.metadata.version("simple-socket-server")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("the package is not installed, metadata is unavailable")
    assert installed == simple_socket_server.__version__


@pytest.mark.skipif(
    os.name == "nt",
    reason="SO_REUSEADDR lets Windows bind a port that is already listening",
)
def test_taken_port_raises_server_exception():
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind((LOCAL_HOST, 0))
    blocker.listen(1)
    try:
        server = SimpleSocketServer()
        with pytest.raises(SimpleSocketServerException):
            server.run(host=LOCAL_HOST, port=blocker.getsockname()[1])
        assert not server.is_running
    finally:
        blocker.close()


def test_client_limit_is_reported_through_the_error_event():
    server = RunningServer(max_clients=1).start()
    try:
        first = server.connect()
        second = server.connect()
        try:
            (_sock, _peer, error), = server.wait_for("error")
            assert isinstance(error, ConnectionAbortedError)
            assert server.thread.is_alive(), "one excessive client does not stop the loop"
            second.settimeout(10)
            assert second.recv(1) == b"", "the excessive client is closed at once"
            assert len(server.events_of("connect")) == 1
        finally:
            first.close()
            second.close()
    finally:
        server.stop()


def half_close_client(server, request, timeout=10.0):
    """Send a request, close the write half and read the reply until EOF."""
    client = server.connect()
    try:
        client.sendall(request)
        client.shutdown(socket.SHUT_WR)
        client.settimeout(timeout)
        reply = b""
        while True:
            chunk = client.recv(65536)
            if not chunk:
                return reply
            reply += chunk
    finally:
        client.close()


def test_reply_survives_a_half_close_of_the_client():
    def on_message(sock, _peer, data):
        state["server"].send(sock, b"echo:" + data)

    state = {}
    server = RunningServer({"message": on_message})
    state["server"] = server.server
    server.start()
    try:
        # The reply used to be dropped whenever the EOF arrived before the flush.
        for attempt in range(10):
            reply = half_close_client(server, b"ping")
            assert reply == b"echo:ping", "attempt %d lost the reply" % attempt
    finally:
        server.stop()


def test_large_reply_survives_a_half_close_of_the_client():
    payload = bytes(range(256)) * 1024  # 256 KiB, far above the socket buffer

    def on_message(sock, _peer, _data):
        state["server"].send(sock, payload)

    state = {}
    server = RunningServer({"message": on_message})
    state["server"] = server.server
    server.start()
    try:
        assert half_close_client(server, b"go", timeout=30) == payload
    finally:
        server.stop()


def test_disconnect_delivers_the_queue_before_it_closes():
    def on_message(sock, _peer, _data):
        state["server"].send(sock, b"bye")
        state["server"].disconnect(sock)

    state = {}
    server = RunningServer({"message": on_message})
    state["server"] = server.server
    server.start()
    try:
        assert half_close_client(server, b"go") == b"bye"
        server.wait_for("disconnect")
    finally:
        server.stop()


def test_disconnect_rejects_a_foreign_socket(running_server):
    stranger = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(ValueError):
            running_server.server.disconnect(stranger)
    finally:
        stranger.close()


def test_closing_the_socket_inside_a_handler_keeps_the_server_alive():
    def on_message(sock, _peer, _data):
        sock.close()

    server = RunningServer({"message": on_message}).start()
    try:
        victim = server.connect()
        server.wait_for("connect")
        victim.sendall(b"x")
        server.wait_for("disconnect")

        assert server.thread.is_alive(), "run() survived a socket closed by a handler"
        assert server.error is None, server.error

        second = server.connect()
        try:
            server.wait_for("connect", count=2)
            assert server.thread.is_alive()
        finally:
            victim.close()
            second.close()
    finally:
        server.stop()


def test_stop_closes_everything_even_if_a_disconnect_handler_raises():
    def bad_disconnect(_sock, _peer):
        raise RuntimeError("boom on disconnect")

    server = RunningServer({"disconnect": bad_disconnect}).start()
    first = server.connect()
    second = server.connect()
    server.wait_for("connect", count=2)

    server.stop()

    assert server.events_of("stop"), "the stop event arrives despite the failure"
    assert isinstance(server.error, RuntimeError), "the handler failure is not swallowed"
    first.settimeout(10)
    second.settimeout(10)
    assert first.recv(1) == b"", "every client is closed"
    assert second.recv(1) == b"", "every client is closed, not just the first one"
    first.close()
    second.close()
    with pytest.raises(OSError):
        socket.create_connection(server.address, timeout=2)
