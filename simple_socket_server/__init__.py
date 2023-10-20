# coding=utf8
"""
Simple TCP socket server with select
Copyright (c) 2023 webtoucher
Distributed under the BSD 3-Clause license. See LICENSE for more info.
"""

import queue
import select
import socket
import time

from event_bus import EventBus

class SimpleSocketServerException(socket.error):
    def __init__(self, message, error):
        super().__init__(message, error)

class SimpleSocketServer(EventBus):
    def __init__(self):
        super().__init__()
        self.__host = None
        self.__port = None
        self.__max_conn = None
        self.__inputs = []
        self.__outputs = []
        self.__messages = {}
        self.__clients = {}
        self.__initialized = False

    def run(self, host='0.0.0.0', port=6666, max_conn=5):
        self.__host = host
        self.__port = port
        self.__max_conn = max_conn
        self.__inputs = []
        self.__outputs = []
        self.__messages = {}
        self.__clients = {}
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

    def send(self, sock, message):
        if sock in self.__messages:
            self.__messages[sock].put(message)
            if sock not in self.__outputs:
                self.__outputs.append(sock)

    def sendall(self, message):
        for sock in self.__messages:
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
        self.server_socket.bind((self.__host, self.__port))
        self.server_socket.listen(self.__max_conn)
        self.__inputs.append(self.server_socket)
        self.__initialized = True
        self.emit('start', self.__host, self.__port)

    def __read_socket(self, sockets_to_read: list):
        for sock in sockets_to_read:
            if sock is self.server_socket:
                self.__server_socket(sock)
            else:
                self.__receive_message(sock)

    def __server_socket(self, server_socket):
        client_socket, client_address = server_socket.accept()
        self.__clients[client_socket] = client_address
        client_socket.setblocking(0)
        self.__inputs.append(client_socket)
        self.__messages[client_socket] = queue.Queue()
        self.emit('connect', client_socket, client_address)

    def __receive_message(self, sock):
        try:
            data_from_client = sock.recv(1024)
            if data_from_client:
                self.emit('message', sock, self.__clients[sock], data_from_client)
        except ConnectionResetError:
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
            except BrokenPipeError as err:
                self.emit('error', sock, self.__clients[sock], err)
                pass
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
                self.__clients = {}
                self.__initialized = False

    def __delete_socket_connection(self, sock):
        if sock in self.__inputs:
            self.__inputs.remove(sock)
        self.__messages.pop(sock, None)
        if sock in self.__outputs:
            self.__outputs.remove(sock)
        self.emit('disconnect', sock, self.__clients[sock])
        sock.close()


if __name__ == '__main__':
    pass
    
