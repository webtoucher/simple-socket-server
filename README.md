# Simple TCP socket server with select

![License](https://img.shields.io/badge/License-BSD%203--Clause-green)
[![Downloads](https://img.shields.io/pypi/dm/simple-socket-server.svg?color=orange)](https://pypi.python.org/pypi/simple-socket-server)
[![Latest Version](https://img.shields.io/pypi/v/simple-socket-server.svg)](https://pypi.python.org/pypi/simple-socket-server)
[![Supported Python versions](https://img.shields.io/pypi/pyversions/simple-socket-server.svg)](https://pypi.python.org/pypi/simple-socket-server)
[![tests](https://github.com/webtoucher/simple-socket-server/actions/workflows/tests.yml/badge.svg)](https://github.com/webtoucher/simple-socket-server/actions/workflows/tests.yml)

A TCP server on a single `select()` loop with no networking framework underneath:
you subscribe handlers to events and the server calls them. Type hints are shipped
with the package, the only runtime dependency is the event bus that dispatches
those events.

## Installation

Install it with pip:

```shell
$ pip install simple-socket-server
```

Or you can add it as dependency in requirements.txt file of your python application:

```
simple-socket-server~=2.0
```

## Usage

Easy way to understand how it works is testing socket server via telnet terminal:

```python
from socket import socket
from simple_socket_server import SimpleSocketServer

socket_server = SimpleSocketServer()


@socket_server.on('start')
def on_start(host, port):
    print('Listening on %s:%s' % (host, port))


@socket_server.on('connect')
def on_connect(sock: socket, peer):
    print('New connection from %s:%s' % peer)
    socket_server.send(sock, b'What is your name?\r\n')


@socket_server.on('disconnect')
def on_disconnect(_sock, peer):
    print('Connection from %s:%s is closed' % peer)


@socket_server.on('message')
def on_message(sock: socket, peer, message: bytes):
    print('Incoming data from %s:%s' % peer, message)
    socket_server.send(sock, b'Hi, ' + message)


socket_server.run(host='0.0.0.0', port=5000)
```

Then you can connect to server:

```shell
telnet 127.0.0.1 5000
```

`run()` blocks, so the server stops when someone asks it to. That may be a signal
handler, a timer or an event handler:

```python
@another_server.on('message')
def on_message(sock, peer, message):
    if message.strip() == b'quit':
        another_server.stop()
```

A port of `0` lets the operating system pick a free port; the real address comes
back in the `start` event and in the `host` and `port` properties:

```python
socket_server.run(host='127.0.0.1', port=0)
# ... inside the start handler:
print(socket_server.host, socket_server.port)
```

## Events

Handlers are called from the thread that runs `run()`, one at a time.

| Event        | Arguments               | When it fires                                                    |
|--------------|-------------------------|------------------------------------------------------------------|
| `start`      | `host, port`            | the listening socket is ready                                    |
| `connect`    | `sock, peer`            | a client has been accepted                                       |
| `message`    | `sock, peer, data`      | a chunk of bytes has been received                               |
| `disconnect` | `sock, peer`            | the connection is gone; the socket is closed right after         |
| `error`      | `sock, peer, error`     | a socket error occurred; `disconnect` follows                    |
| `stop`       | `host, port`            | `run()` has returned and every socket is closed                  |

`peer` is an `(host, port)` tuple, `data` is `bytes`, `error` is an `OSError`.

## API

| Member                                | Description                                                          |
|---------------------------------------|----------------------------------------------------------------------|
| `run(host='0.0.0.0', port=6666, max_conn=5)` | serve connections until `stop()` is called                    |
| `stop()`                              | ask `run()` to return; safe from a handler or another thread          |
| `send(sock, message)`                 | queue `bytes` for a client; `ValueError` if it is not a client        |
| `is_running`                          | `True` while `run()` is serving connections                           |
| `host`, `port`                        | the address the listening socket is bound to                          |
| `on(event)`, `emit(...)`              | inherited from `event_bus.EventBus`                                   |

Notes:

- The server is single-threaded and handlers are called synchronously, so a slow
  handler delays every other connection. Hand off long work to your own thread.
- `message` carries one raw `recv()` chunk of up to 1024 bytes. There is no
  framing: the protocol you put on top has to delimit its own messages.
- `send()` never blocks and never truncates: because client sockets are
  non-blocking, the bytes are buffered and the unwritten tail is retried on the
  next poll, so a message larger than the socket buffer arrives in full.
- Do not use the socket after `disconnect` or `error`: it is closed.
- An exception raised by a handler propagates out of `run()`. The socket state is
  still cleaned up before the call returns.

## Compatibility

- Python **3.9 and newer**, including free-threaded builds: CI runs every version
  from 3.9 to 3.15 plus the free-threaded `3.13t`.
- One runtime dependency: [`event-bus`](https://pypi.org/project/event-bus/).
- Type hints are shipped with the package (`py.typed`, PEP 561), so mypy and
  pyright see real signatures instead of `Any`.
- Any server instance is independent: several servers may run in one process, each
  in its own thread.

## Development

```shell
$ python -m venv .venv
$ .venv\Scripts\activate          # Windows; on POSIX: source .venv/bin/activate
$ pip install -e ".[dev]"          # installs the package and pytest
$ python -m pytest
```

The test suite covers:

- the lifecycle: `start`, `stop`, `is_running`, socket cleanup and port reuse,
- the message flow: `connect`, echo round trips, ordering and large payloads,
- disconnects: graceful close by the peer, an abrupt reset and a send to a
  connection that is already gone,
- error handling: an unknown socket for `send()` and a port that is already taken,
- independence of two servers running in one process.

Project layout:

```
simple_socket_server/__init__.py  the whole library
tests/                            test suite
pyproject.toml                    metadata, build config and pytest settings
.github/workflows/                CI: tests on Python 3.9-3.15 and 3.13t, release pipeline
```

To build and verify a release:

```shell
$ pip install build twine
$ python -m build
$ python -m twine check dist/*
```

## Changelog

See [CHANGELOG.md](CHANGELOG.md). GitHub Releases are generated from the matching
entry of that file, so the history is available both in git and on the releases
page.

## License

Distributed under the BSD 3-Clause license. See [LICENSE.md](LICENSE.md) for details.
