# coding=utf8
"""
Simple TCP socket server with select
Copyright (c) 2023 webtoucher
Distributed under the BSD 3-Clause license. See LICENSE for more info.
"""

from abc import ABCMeta
import socket
import select
import queue
import time


class Singleton(ABCMeta):
    """Metaclass for singleton."""

    def __call__(cls, *args, **kwargs):
        if not hasattr(cls, '_instance'):
            cls._instance = super().__call__(*args, **kwargs)
        return cls._instance


class SimpleSocketServerException(socket.error):
    """New class for socket.error."""

    def __init__(self, message, error):
        super().__init__(message, error)


class SimpleSocketServer(object, metaclass=Singleton):
    def __init__(self, ip='0.0.0.0', port=6666, max_conn=5):
        """Init socket class."""
        self.__ip = ip
        self.__port = port
        self.__max_conn = max_conn
        self.__inputs = []
        self.__outputs = []
        self.__messages = {}
        self.__listeners = {
            'connect': [],
            'disconnect': [],
            'message': [],
        }
        self.__initialized = False

    def run(self):
        self.__inputs = []
        self.__outputs = []
        self.__messages = {}
        """Run socket server."""
        while True:
            if self.__initialized:
                sock_to_read, sock_to_write, sock_errors = select.select(
                    self.__inputs,
                    self.__outputs,
                    self.__inputs,
                    0.1,
                )
                self.__read_socket(sock_to_read)
                self.__write_socket(sock_to_write)
                self.__exception_socket(sock_errors)
                time.sleep(0.1)
            else:
                self.__initialize()

    @property
    def on_connect(self):
        def decorator(func):
            self.add_listener('connect', func)
            return func

        return decorator

    @property
    def on_disconnect(self):
        def decorator(func):
            self.add_listener('disconnect', func)
            return func

        return decorator

    @property
    def on_message(self):
        def decorator(func):
            self.add_listener('message', func)
            return func

        return decorator

    def add_listener(self, name, func):
        if not self.__listeners[name]:
            self.__listeners.update({name: []})
        if func not in self.__listeners[name]:
            self.__listeners[name].append(func)

    def remove_listener(self, name, func):
        if self.__listeners[name] and func in self.__listeners[name]:
            self.__listeners[name].remove(func)

    def trigger(self, name, *args):
        if not self.__listeners[name]:
            return
        for func in self.__listeners[name]:
            func(*args)

    def send(self, sock, message):
        if self.__messages[sock]:
            self.__messages[sock].put(message)
            if sock not in self.__outputs:
                self.__outputs.append(sock)

    def __initialize(self):
        self.server_socket = socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
        )
        server_fd = self.server_socket.fileno()
        if server_fd < 0:
            self.__initialized = False
            raise SimpleSocketServerException(
                'Error with creating sockets',
                'server_fd < 0',
            )
        self.server_socket.setblocking(False)
        self.server_socket.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_REUSEADDR,
            1,
        )
        self.server_socket.bind((self.__ip, self.__port))
        self.server_socket.listen(self.__max_conn)
        self.__inputs.append(self.server_socket)
        self.__initialized = True

    def __read_socket(self, sockets_to_read: list):
        for sock in sockets_to_read:
            if sock is self.server_socket:
                self.__server_socket(sock)
            else:
                self.__receive_message(sock)

    def __server_socket(self, server_socket):
        client_socket, client_address = server_socket.accept()
        client_socket.setblocking(0)
        self.__inputs.append(client_socket)
        self.__messages[client_socket] = queue.Queue()
        self.trigger('connect', client_socket)

    def __receive_message(self, sock):
        data_from_client = None
        try:
            data_from_client = sock.recv(1024)
        except ConnectionResetError:
            self.__delete_socket_connection(sock)
        if data_from_client:
            self.trigger('message', sock, data_from_client)
        else:
            self.__delete_socket_connection(sock)

    def __write_socket(self, socket_to_write):
        for sock in socket_to_write:
            echo_message = ''.encode()
            try:
                if sock.fileno() > 0:
                    echo_message = self.__messages[sock].get_nowait()
                else:
                    self.__delete_socket_connection(sock)
                    continue
            except queue.Empty:
                self.__outputs.remove(sock)
            try:
                if echo_message:
                    sock.send(echo_message)
            except ConnectionResetError:
                self.__delete_socket_connection(sock)

    def __exception_socket(self, socket_errors):
        for sock in socket_errors:
            self.__delete_socket_connection(sock)
            print('trying to delete server socket')
            if sock is self.server_socket:
                self.__inputs = []
                self.__outputs = []
                self.__messages = {}
                self.__initialized = False

    def __delete_socket_connection(self, sock):
        if sock in self.__inputs:
            self.__inputs.remove(sock)
        self.__messages.pop(sock, None)
        if sock in self.__outputs:
            self.__outputs.remove(sock)
        self.trigger('disconnect', sock)
        sock.close()


if __name__ == '__main__':
    pass
