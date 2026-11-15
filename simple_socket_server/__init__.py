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
import threading

from typing import Dict, List, Optional, Tuple

from event_bus import EventBus

__all__ = ["SimpleSocketServer", "SimpleSocketServerException"]

__version__ = "2.0"

#: Peer address as returned by accept(): (host, port).
Address = Tuple[str, int]

#: Size of a single recv() chunk; a message event carries one such chunk.
_RECV_SIZE = 1024

#: How long select() waits for socket activity, in seconds.
_POLL_INTERVAL = 0.1


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
        The connection is gone: the peer closed it, reset it or a send failed.
    ``error(sock, peer, error)``
        A socket error occurred; the connection is closed right after.
    ``stop(host, port)``
        run() has returned and every socket is closed.

    Handlers are called from the thread that runs run(), one at a time, so a
    slow handler delays every other connection.
    """

    def __init__(self) -> None:
        super().__init__()
        self.__host = "0.0.0.0"
        self.__port = 6666
        self.__max_conn = 5
        self.__server_socket: Optional[socket.socket] = None
        self.__inputs: List[socket.socket] = []
        self.__outputs: List[socket.socket] = []
        self.__pending: Dict[socket.socket, bytearray] = {}
        self.__clients: Dict[socket.socket, Address] = {}
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

    def run(self, host: str = "0.0.0.0", port: int = 6666, max_conn: int = 5) -> None:
        """Serve connections until stop() is called.

        The call blocks the calling thread. Once it returns, every client socket
        and the listening socket are closed. A port of 0 makes the operating
        system pick a free port; the actual address is reported by the start
        event and by the host and port properties.
        """
        if self.is_running:
            raise RuntimeError("the server is already running")
        self.__host = host
        self.__port = port
        self.__max_conn = max_conn
        self.__stop.clear()
        self.__initialize()
        try:
            while not self.__stop.is_set():
                self.__select()
        finally:
            self.__shutdown()

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
        """
        if sock not in self.__pending:
            raise ValueError("the socket is not a client of this server")
        if not message:
            return
        self.__pending[sock].extend(message)
        if sock not in self.__outputs:
            self.__outputs.append(sock)

    def __initialize(self) -> None:
        server_socket = socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
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
        try:
            sock_to_read, sock_to_write, sock_errors = select.select(
                self.__inputs,
                self.__outputs,
                self.__inputs,
                _POLL_INTERVAL,
            )
        except OSError as error:
            # select() itself failed, so the listening socket is unusable.
            self.emit("error", None, None, error)
            self.stop()
            return
        for sock in sock_errors:
            if sock is self.__server_socket:
                self.stop()
            else:
                self.__close(sock)
        for sock in sock_to_read:
            if sock is self.__server_socket:
                self.__accept()
            else:
                self.__receive(sock)
        for sock in sock_to_write:
            self.__flush(sock)

    def __accept(self) -> None:
        try:
            client_socket, client_address = self.__server_socket.accept()
        except OSError as error:
            self.emit("error", self.__server_socket, None, error)
            return
        client_socket.setblocking(False)
        self.__clients[client_socket] = client_address
        self.__pending[client_socket] = bytearray()
        self.__inputs.append(client_socket)
        self.emit("connect", client_socket, client_address)

    def __receive(self, sock: socket.socket) -> None:
        peer = self.__clients.get(sock)
        if peer is None:
            # The socket was already dropped during this poll.
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
            self.__close(sock)
            return
        self.emit("message", sock, peer, data)

    def __flush(self, sock: socket.socket) -> None:
        pending = self.__pending.get(sock)
        if pending is None:
            if sock in self.__outputs:
                self.__outputs.remove(sock)
            return
        try:
            sent = sock.send(pending)
        except BlockingIOError:
            # The kernel buffer is full again; the tail stays queued.
            return
        except OSError as error:
            self.emit("error", sock, self.__clients.get(sock), error)
            self.__close(sock)
            return
        del pending[:sent]
        if not pending and sock in self.__outputs:
            self.__outputs.remove(sock)

    def __close(self, sock: socket.socket) -> None:
        """Close a client connection and forget it. Safe to call twice."""
        peer = self.__clients.pop(sock, None)
        if sock in self.__inputs:
            self.__inputs.remove(sock)
        if sock in self.__outputs:
            self.__outputs.remove(sock)
        self.__pending.pop(sock, None)
        if peer is None:
            sock.close()
            return
        try:
            self.emit("disconnect", sock, peer)
        finally:
            sock.close()

    def __shutdown(self) -> None:
        for sock in list(self.__clients):
            self.__close(sock)
        server_socket, self.__server_socket = self.__server_socket, None
        if server_socket is not None:
            if server_socket in self.__inputs:
                self.__inputs.remove(server_socket)
            server_socket.close()
        self.emit("stop", self.__host, self.__port)
