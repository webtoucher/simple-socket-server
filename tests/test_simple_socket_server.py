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
        self.run_kwargs = {"host": LOCAL_HOST, "port": 0}
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


def test_disconnect_is_idempotent():
    server = RunningServer().start()
    try:
        client = server.connect()
        ((sock, _peer),) = server.wait_for("connect")
        server.server.disconnect(sock)
        server.server.disconnect(sock)  # the second call is silent
        server.wait_for("disconnect")
        client.settimeout(10)
        assert client.recv(1) == b""

        server.server.disconnect(sock)
        assert len(server.events_of("disconnect")) == 1
    finally:
        client.close()
        server.stop()


def test_disconnect_from_another_thread_keeps_the_server_alive():
    clients = []
    handler_threads = []

    def on_connect(sock, _peer):
        clients.append(sock)

    def on_disconnect(_sock, _peer):
        handler_threads.append(threading.get_ident())

    server = RunningServer({"connect": on_connect, "disconnect": on_disconnect})
    server.start()
    poller = server.thread.ident
    stop_churn = threading.Event()

    def churn():
        while not stop_churn.is_set():
            try:
                client = socket.create_connection(server.address, timeout=2)
                time.sleep(0.005)
                client.close()
            except OSError:
                return
            # A short pause keeps the test from exhausting the ephemeral ports
            # of the machine, which would make the following tests flaky.
            time.sleep(0.01)

    workers = [threading.Thread(target=churn, daemon=True) for _ in range(6)]
    for worker in workers:
        worker.start()
    try:
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline and server.thread.is_alive():
            for sock in list(clients):
                try:
                    server.server.disconnect(sock)
                except ValueError:
                    pass
            time.sleep(0.001)
    finally:
        stop_churn.set()
        for worker in workers:
            worker.join(5)

    try:
        assert server.thread.is_alive(), "run() survives a foreign-thread disconnect"
        assert server.error is None, server.error
        assert server.events_of("error") == []
        assert handler_threads, "some connections were closed"
        for ident in handler_threads:
            assert ident == poller, "disconnect is emitted in the thread of run()"
    finally:
        server.stop()


def test_send_from_another_thread_does_not_wait_for_the_poll():
    ready = []
    server = RunningServer({"connect": lambda sock, _peer: ready.append(sock)}).start()
    try:
        client = server.connect()
        server.wait_for("connect")
        sock = ready[0]
        client.settimeout(10)
        timings = []
        for _ in range(20):
            started = time.perf_counter()
            server.server.send(sock, b"ping")
            assert recv_exactly(client, 4, timeout=10) == b"ping"
            timings.append(time.perf_counter() - started)
        timings.sort()
        median = timings[len(timings) // 2]
        assert median < 0.010, "median delivery took %.1f ms" % (median * 1000)
    finally:
        client.close()
        server.stop()


def test_a_descriptor_above_the_select_limit_is_rejected(monkeypatch):
    server = RunningServer().start()
    try:
        monkeypatch.setattr(simple_socket_server, "_SELECT_FD_LIMIT", 0)
        client = server.connect()
        try:
            (_sock, _peer, error), = server.wait_for("error")
            assert isinstance(error, ConnectionAbortedError)
            assert server.thread.is_alive(), "one rejected client does not stop the loop"
            assert server.events_of("connect") == []
            client.settimeout(10)
            assert client.recv(1) == b""
        finally:
            client.close()
    finally:
        server.stop()


def test_a_listening_descriptor_above_the_select_limit_fails_loudly(monkeypatch):
    monkeypatch.setattr(simple_socket_server, "_SELECT_FD_LIMIT", 0)
    server = SimpleSocketServer()
    with pytest.raises(SimpleSocketServerException):
        server.run(host=LOCAL_HOST, port=0)
    assert not server.is_running


def test_a_slow_client_is_dropped_when_the_queue_limit_is_reached():
    ready = []
    server = RunningServer(
        {"connect": lambda sock, _peer: ready.append(sock)},
        max_pending=4096,
    ).start()
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Small buffers on both ends: otherwise the kernel swallows megabytes of
        # replies for a client that never reads, and the queue never grows.
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        client.settimeout(30)
        client.connect(server.address)
        ((sock, _peer),) = server.wait_for("connect")
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)

        payload = b"x" * 65536
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not server.events_of("error"):
            try:
                server.server.send(sock, payload)
            except ValueError:
                break

        (_sock, _peer, error), = server.wait_for("error")
        assert isinstance(error, ConnectionAbortedError)
        assert server.thread.is_alive(), "a slow client does not stop the loop"
        server.wait_for("disconnect")
        while client.recv(65536):
            pass
    finally:
        client.close()
        server.stop()


def test_disconnect_gives_up_when_the_client_does_not_read():
    ready = []
    server = RunningServer({"connect": lambda sock, _peer: ready.append(sock)}).start()
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        client.settimeout(30)
        client.connect(server.address)
        ((sock, _peer),) = server.wait_for("connect")
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)

        # A client that never reads leaves the queue growing: the loop fills it
        # past the socket buffers, and without a timeout it would never drain.
        chunk = b"x" * (1024 * 1024)
        for _ in range(64):
            server.server.send(sock, chunk)
        server.server.disconnect(sock, timeout=0.2)

        started = time.monotonic()
        server.wait_for("disconnect", timeout=5)
        assert time.monotonic() - started < 5, "the drain gave up in time"
        assert server.thread.is_alive()
        assert server.error is None, server.error
    finally:
        client.close()
        server.stop()


def test_two_hundred_clients_are_accepted_with_the_default_backlog():
    clients = 200

    def on_message(sock, _peer, _data):
        state["server"].send(sock, b"ok")

    state = {}
    server = RunningServer({"message": on_message})
    state["server"] = server.server
    server.start()
    barrier = threading.Barrier(clients)
    replies = []
    lock = threading.Lock()

    def talk():
        barrier.wait(30)
        try:
            client = server.connect(timeout=20)
            try:
                client.sendall(b"x")
                reply = recv_exactly(client, 2, timeout=20)
            finally:
                client.close()
        except OSError as error:
            reply = error
        with lock:
            replies.append(reply)

    workers = [threading.Thread(target=talk) for _ in range(clients)]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(60)
        failures = [reply for reply in replies if reply != b"ok"]
        assert not failures, "%d of %d clients got no reply: %r" % (
            len(failures), clients, failures[:3])
    finally:
        server.stop()


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
