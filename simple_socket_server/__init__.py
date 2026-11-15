"""
Simple TCP socket server built on select().

The server is single-threaded: run() polls the sockets and calls the handlers
registered for its events. No third-party networking code is involved, the only
runtime dependency is the event bus used to dispatch those events.

Copyright (c) 2023–2026 webtoucher
Distributed under the BSD 3-Clause license. See LICENSE.md for more info.
"""

import select
import socket
import sys
import threading
import time
import weakref

from typing import Dict, List, Optional, Set, Tuple

from event_bus import EventBus

__all__ = ["SimpleSocketServer", "SimpleSocketServerException"]

__version__ = "2.2.0"

#: Peer address as returned by accept(): (host, port).
Address = Tuple[str, int]

#: Size of a single recv() chunk; a message event carries one such chunk.
_RECV_SIZE = 1024

#: How long select() waits for socket activity, in seconds.
_POLL_INTERVAL = 0.1

#: Default client limit. select() refuses a descriptor set larger than
#: FD_SETSIZE by raising ValueError, and that limit is 512 on Windows and
#: 1024 elsewhere, so the default stays below both.
_DEFAULT_MAX_CLIENTS = 500

#: Highest descriptor number that select() accepts on POSIX systems: a socket
#: above it fails the poll with ValueError. Windows limits the number of
#: descriptors instead of their numbers, and max_clients covers that case.
_SELECT_FD_LIMIT: Optional[int] = None if sys.platform == "win32" else 1024


class SimpleSocketServerException(socket.error):
    """Raised when the listening socket cannot be created, bound or enabled."""


class SimpleSocketServer(EventBus):
    """Single-threaded TCP server dispatching events through an event bus.

    Events, in the order they may arrive:

    ``start(host, port)``
        The listening socket is ready.
    ``connect(sock, peer)``
        A client has been accepted.
    ``message(sock, peer, data)``
        A chunk of bytes has been received.
    ``disconnect(sock, peer)``
        The connection is closed: the peer closed it, reset it, a send failed
        or disconnect() was called. Queued replies are delivered before it.
    ``error(sock, peer, error)``
        A socket error occurred; the connection is closed right after, or the
        server itself stops when the listening socket or select() fails.
    ``stop(host, port)``
        run() has returned and every socket is closed.

    Handlers are always called from the thread that runs run(), one at a time,
    so a slow handler delays every other connection. **Handlers must not raise**:
    an exception propagates out of run() and stops the server for every client.
    The sockets are still closed before run() returns.

    send() and disconnect() may be called from any thread: the structures they
    touch are guarded by a lock, and the sockets are closed only by the thread
    that runs run(), so no handler ever runs outside it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.__host = "0.0.0.0"
        self.__port = 6666
        self.__max_conn = socket.SOMAXCONN
        self.__max_clients = _DEFAULT_MAX_CLIENTS
        self.__max_pending: Optional[int] = None
        self.__server_socket: Optional[socket.socket] = None
        self.__inputs: List[socket.socket] = []
        self.__outputs: List[socket.socket] = []
        self.__pending: Dict[socket.socket, bytearray] = {}
        self.__clients: Dict[socket.socket, Address] = {}
        self.__closing: Set[socket.socket] = set()
        self.__deadlines: Dict[socket.socket, float] = {}
        self.__failures: Dict[socket.socket, BaseException] = {}
        #: Sockets the server has closed already, so disconnect() stays quiet for
        #: them. Weak references keep this from pinning dead sockets.
        self.__retired: "weakref.WeakSet[socket.socket]" = weakref.WeakSet()
        self.__runner: Optional[int] = None
        self.__lock = threading.Lock()
        self.__stop = threading.Event()

    @property
    def is_running(self) -> bool:
        """True while run() is serving connections."""
        return self.__server_socket is not None and not self.__stop.is_set()

    @property
    def host(self) -> str:
        """Host the listening socket is bound to."""
        return self.__host

    @property
    def port(self) -> int:
        """Port the listening socket is bound to."""
        return self.__port

    def run(
        self,
        host: str = "0.0.0.0",
        port: int = 6666,
        max_conn: int = socket.SOMAXCONN,
        max_clients: int = _DEFAULT_MAX_CLIENTS,
        max_pending: Optional[int] = None,
    ) -> None:
        """Serve connections until stop() is called.

        The call blocks the calling thread. Once it returns, every client socket
        and the listening socket are closed. A port of 0 makes the operating
        system pick a free port; the actual address is reported by the start
        event and by the host and port properties.

        max_conn is the backlog of the listening socket: how many connections the
        system may queue before accept(). It defaults to socket.SOMAXCONN.

        max_clients is the number of clients the server keeps at once; a
        connection above it is accepted and closed immediately with an error
        event.

        max_pending caps the outbound queue of one client, in bytes. When a
        client stops reading and its queue grows past the cap, the server
        reports error on it and closes it. None means no cap.
        """
        if self.is_running:
            raise RuntimeError("the server is already running")
        self.__host = host
        self.__port = port
        self.__max_conn = max_conn
        self.__max_clients = max_clients
        self.__max_pending = max_pending
        self.__stop.clear()
        self.__initialize()
        self.__runner = threading.get_ident()
        try:
            while not self.__stop.is_set():
                self.__select()
        finally:
            try:
                self.__shutdown()
            finally:
                self.__runner = None

    def stop(self) -> None:
        """Ask run() to return.

        It only raises a flag, so it is safe to call from an event handler or
        from another thread. run() finishes the current poll and then closes
        every socket.
        """
        self.__stop.set()

    def send(self, sock: socket.socket, message: bytes) -> None:
        """Queue message for delivery to a client of this server.

        The socket is non-blocking, so the bytes are buffered and the unwritten
        tail is retried on the next poll; a message larger than the socket
        buffer is delivered in full. Empty messages are ignored.

        Safe to call from another thread. An empty queue is written straight to
        the socket, so a reply from another thread does not wait for the next
        poll; whatever the socket does not take at once is queued.

        When max_pending is set and the queue of a client grows past it, the
        server reports error on that client and drops the queue.
        """
        if not message:
            return
        with self.__lock:
            pending = self.__pending.get(sock)
            if pending is None:
                raise ValueError("the socket is not a client of this server")
            failure: Optional[BaseException] = None
            sent = 0
            if not pending:
                try:
                    sent = sock.send(message)
                except BlockingIOError:
                    sent = 0
                except OSError as error:
                    # Reported by the poll thread: handlers run only there.
                    failure = error
            if sent < len(message):
                pending.extend(message[sent:])
                if sock not in self.__outputs:
                    self.__outputs.append(sock)
            if (
                failure is None
                and self.__max_pending is not None
                and len(pending) > self.__max_pending
            ):
                failure = ConnectionAbortedError(
                    "the outbound queue is over %d bytes" % self.__max_pending
                )
            if failure is not None:
                self.__failures[sock] = failure

    def disconnect(
        self,
        sock: socket.socket,
        timeout: Optional[float] = None,
    ) -> None:
        """Close a client connection gracefully.

        The socket stops being read, the replies already queued for it are
        delivered first, and only then it is closed and the disconnect event is
        emitted. Safe to call from an event handler or from another thread: the
        socket is closed by the thread of run(), never by the caller.

        timeout is how long the queue may still be flushed, in seconds. When it
        expires, the socket is closed with whatever is left unsent, so a client
        that stopped reading does not keep the connection forever. None waits
        for the queue without a limit.

        Calling it again for a client that is closed already does nothing, so a
        race with the peer is harmless; a socket the server never served raises
        ValueError.

        Use it instead of sock.close(): closing a socket behind the server's
        back leaves the select() set with a dead descriptor.
        """
        with self.__lock:
            known = sock in self.__clients
            retired = sock in self.__retired
        if not known:
            if retired:
                return
            raise ValueError("the socket is not a client of this server")
        self.__start_closing(sock, timeout)

    def __initialize(self) -> None:
        server_socket = socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
        )
        descriptor = server_socket.fileno()
        if _SELECT_FD_LIMIT is not None and descriptor >= _SELECT_FD_LIMIT:
            server_socket.close()
            raise SimpleSocketServerException(
                "cannot poll descriptor %d: select() accepts numbers below %d"
                % (descriptor, _SELECT_FD_LIMIT)
            )
        try:
            server_socket.setblocking(False)
            server_socket.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_REUSEADDR,
                1,
            )
            server_socket.bind((self.__host, self.__port))
            server_socket.listen(self.__max_conn)
        except OSError as error:
            server_socket.close()
            raise SimpleSocketServerException(
                "cannot listen on %s:%s" % (self.__host, self.__port)
            ) from error
        self.__server_socket = server_socket
        self.__inputs.append(server_socket)
        # A port of 0 is resolved by the system, so report the real address.
        self.__host, self.__port = server_socket.getsockname()[:2]
        self.emit("start", self.__host, self.__port)

    def __select(self) -> None:
        self.__settle()
        # Handlers may call disconnect() and send() from another thread, so the
        # sequences handed to select() are snapshots taken under the lock.
        with self.__lock:
            sock_to_read = list(self.__inputs)
            sock_to_write = list(self.__outputs)
        server_socket = self.__server_socket
        sock_to_check = [server_socket] if server_socket is not None else []
        try:
            sock_to_read, sock_to_write, sock_errors = select.select(
                sock_to_read,
                sock_to_write,
                sock_to_check,
                _POLL_INTERVAL,
            )
        except (OSError, ValueError) as error:
            # select() broke: an unusable listening socket, or a descriptor set
            # larger than the platform allows. The poll set cannot be trusted.
            self.emit("error", server_socket, None, error)
            self.stop()
            return
        for sock in sock_errors:
            # Only the listening socket is polled here: urgent (out of band)
            # data on a client connection is not a connection error.
            if sock is server_socket:
                self.stop()
        for sock in sock_to_read:
            if sock is server_socket:
                self.__accept()
            else:
                self.__receive(sock)
        for sock in sock_to_write:
            self.__flush(sock)

    def __settle(self) -> None:
        """Finish the work queued by other threads before the poll."""
        for sock in list(self.__clients):
            if sock.fileno() < 0:
                # The application closed it behind the server's back.
                self.__close(sock)
        with self.__lock:
            drained = [
                sock
                for sock in self.__closing
                if sock in self.__clients and not self.__pending.get(sock)
            ]
            now = time.monotonic()
            expired = [
                sock
                for sock, deadline in self.__deadlines.items()
                if deadline <= now and sock in self.__clients
            ]
            failures = list(self.__failures.items())
            self.__failures.clear()
        for sock in drained:
            self.__close(sock)
        for sock in expired:
            # The peer did not read its replies in time: drop the rest.
            self.__close(sock)
        for sock, failure in failures:
            if sock not in self.__clients:
                continue
            self.emit("error", sock, self.__clients.get(sock), failure)
            self.__close(sock)

    def __accept(self) -> None:
        server_socket = self.__server_socket
        if server_socket is None:
            return
        try:
            client_socket, client_address = server_socket.accept()
        except BlockingIOError:
            # The connection died in the backlog before accept() reached it.
            return
        except OSError as error:
            self.emit("error", server_socket, None, error)
            # Out of descriptors (EMFILE) would spin the loop without a pause.
            time.sleep(_POLL_INTERVAL)
            return
        client_socket.setblocking(False)
        if _SELECT_FD_LIMIT is not None and client_socket.fileno() >= _SELECT_FD_LIMIT:
            # select() fails on the descriptor number, not on the count.
            client_socket.close()
            self.emit(
                "error",
                client_socket,
                client_address,
                ConnectionAbortedError(
                    "the descriptor number is too large for select()"
                ),
            )
            return
        if len(self.__clients) >= self.__max_clients:
            client_socket.close()
            self.emit(
                "error",
                client_socket,
                client_address,
                ConnectionAbortedError("the client limit is reached"),
            )
            return
        with self.__lock:
            self.__clients[client_socket] = client_address
            self.__pending[client_socket] = bytearray()
            self.__inputs.append(client_socket)
        self.emit("connect", client_socket, client_address)

    def __receive(self, sock: socket.socket) -> None:
        with self.__lock:
            peer = self.__clients.get(sock)
            closing = sock in self.__closing
        if peer is None or closing:
            # Dropped during this poll, or closing: nothing more to read.
            return
        try:
            data = sock.recv(_RECV_SIZE)
        except BlockingIOError:
            # select() woke us up for nothing; try again on the next poll.
            return
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.__close(sock)
            return
        except OSError as error:
            self.emit("error", sock, peer, error)
            self.__close(sock)
            return
        if not data:
            # An empty chunk means the peer closed its end of the connection.
            # Replies queued for it are still delivered before the socket goes.
            self.__start_closing(sock)
            return
        self.emit("message", sock, peer, data)

    def __start_closing(
        self,
        sock: socket.socket,
        timeout: Optional[float] = None,
    ) -> None:
        """Stop reading a connection and close it once its queue is drained."""
        with self.__lock:
            if sock not in self.__clients:
                return
            self.__closing.add(sock)
            if sock in self.__inputs:
                self.__inputs.remove(sock)
            drained = not self.__pending.get(sock)
            if not drained:
                if sock not in self.__outputs:
                    self.__outputs.append(sock)
                if timeout is not None:
                    self.__deadlines[sock] = time.monotonic() + timeout
        if drained and self.__in_poll_thread():
            # Only the poll thread closes sockets: a close from another thread
            # would leave its snapshot of the poll set with a dead descriptor.
            self.__close(sock)

    def __in_poll_thread(self) -> bool:
        """True when the caller may close sockets itself."""
        return self.__runner is None or threading.get_ident() == self.__runner

    def __flush(self, sock: socket.socket) -> None:
        failure: Optional[OSError] = None
        drained = False
        closing = False
        with self.__lock:
            pending = self.__pending.get(sock)
            if pending is None:
                if sock in self.__outputs:
                    self.__outputs.remove(sock)
                return
            try:
                with memoryview(pending) as view:
                    sent = sock.send(view)
            except BlockingIOError:
                # The kernel buffer is full again; the tail stays queued.
                return
            except OSError as error:
                failure = error
            else:
                del pending[:sent]
                drained = not pending
                if drained and sock in self.__outputs:
                    self.__outputs.remove(sock)
            closing = drained and sock in self.__closing
        if failure is not None:
            self.emit("error", sock, self.__clients.get(sock), failure)
            self.__close(sock)
            return
        if closing:
            self.__close(sock)

    def __close(self, sock: socket.socket) -> None:
        """Drop a client connection and close it. Safe to call twice."""
        with self.__lock:
            peer = self.__clients.pop(sock, None)
            if sock in self.__inputs:
                self.__inputs.remove(sock)
            if sock in self.__outputs:
                self.__outputs.remove(sock)
            self.__pending.pop(sock, None)
            self.__closing.discard(sock)
            self.__deadlines.pop(sock, None)
            self.__failures.pop(sock, None)
            self.__retired.add(sock)
        if peer is None:
            sock.close()
            return
        try:
            self.emit("disconnect", sock, peer)
        finally:
            sock.close()

    def __shutdown(self) -> None:
        """Close everything, no matter what the handlers do.

        The listening socket goes first, so the port stops accepting; then every
        client is closed even if a disconnect handler raises, and stop is
        emitted in any case. The first handler failure is re-raised afterwards
        unless run() is already unwinding because of another error.
        """
        failure: Optional[BaseException] = None
        server_socket, self.__server_socket = self.__server_socket, None
        if server_socket is not None:
            with self.__lock:
                if server_socket in self.__inputs:
                    self.__inputs.remove(server_socket)
            server_socket.close()
        for sock in list(self.__clients):
            try:
                self.__close(sock)
            except Exception as error:  # noqa: BLE001 - cleanup must finish
                if failure is None:
                    failure = error
        for sock in list(self.__outputs):
            sock.close()
        self.__outputs.clear()
        try:
            self.emit("stop", self.__host, self.__port)
        except Exception as error:  # noqa: BLE001 - cleanup must finish
            if failure is None:
                failure = error
        if failure is not None and sys.exc_info()[0] is None:
            raise failure
