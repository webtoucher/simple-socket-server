# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0] - 2026-11-15

### Added

- `SimpleSocketServer.stop()`: `run()` returns on request, closes every socket and
  emits `stop(host, port)`. It only raises a flag, so it is safe to call from an
  event handler or from another thread.
- `is_running`, `host` and `port` properties. A port of `0` is resolved by the
  system, and the `start` event plus both properties report the real address.
- `simple_socket_server.__version__`, an explicit `__all__` and a public `Address`
  alias for a peer address.
- `py.typed` marker (PEP 561) and type hints on every public name, so mypy and
  pyright see real signatures instead of `Any`.
- `tests/`: pytest suite covering the lifecycle, event order, echo round trips,
  graceful and abrupt disconnects, large messages, error handling and port reuse.
- GitHub Actions workflows: tests on Python 3.9-3.15 and 3.13t, plus a tag-driven
  release pipeline publishing to PyPI and creating a GitHub Release.
- `CHANGELOG.md` and `.gitattributes` with `* text=auto eol=lf`.

### Fixed

- A peer closing the connection cleanly (`recv()` returning `b""`) was ignored, so
  the socket stayed in the `select()` set forever: the loop spun on an idle file
  descriptor and `disconnect` never fired.
- `send()` raised `KeyError` for a socket that was not a client of the server; it
  raises `ValueError` now, and an empty message is a no-op.
- `send()` dropped data on a non-blocking socket: a single `send()` call can accept
  only part of the buffer. The unwritten tail is buffered and retried, so a message
  larger than the socket buffer arrives in full.
- Closed sockets were never removed from the client table, so a long-lived server
  leaked memory with every reconnection.
- The `disconnect` event could be emitted twice for one socket, and a socket already
  dropped earlier in the same poll was used again.
- The state reset on the server-socket error path left the previous listening
  socket open, and `run()` reused a stale initialized flag.
- `SimpleSocketServerException` was raised with two positional arguments on a check
  that could not trigger; bind and listen failures are reported now with the address.
- A `select()` failure is reported through the `error` event and stops the server
  instead of escaping `run()` as an unhandled exception.

### Changed

- Packaging moved from `setup.py` to `pyproject.toml` (PEP 621, PEP 639);
  metadata, build configuration and pytest settings live in one file.
- Errors that end a connection are reported through the `error` event and followed
  by `disconnect`; `message` is never emitted for a reset connection.
- `run()` no longer sleeps after `select()`: the poll interval is the `select()`
  timeout alone, so the latency of a response is halved.
- The listening socket no longer tries to restart itself after an error; a broken
  listening socket stops the server with an `error` event.
- `stop` is emitted when `run()` returns, after every socket is closed.
- Documentation: README rewritten with the event table, the API table, the
  framing and threading caveats, compatibility and development instructions.
- Metadata: the `HTTP Servers` topic is replaced by `System :: Networking` and
  `Software Development :: Libraries`, `Requires-Python` is declared, and project
  URLs point to the changelog and issue tracker.

### Removed

- Python 2 leftovers: the `coding=utf8` header and the empty `__main__` block.
- `setup.py` and `requirements.txt`: dependencies are declared in `pyproject.toml`.
- The debug `print()` call on the server-socket error path.

### Breaking

- The `SimpleSocketServer` class is no longer a singleton: every instance is an
  independent server, so two servers can run in one process.
- Python 3.9 or newer is required.
- `send()` raises `ValueError` instead of `KeyError` for an unknown socket.
- `SimpleSocketServerException` wraps the underlying `OSError` (`raise ... from`),
  so `except OSError` keeps working but the arguments differ.

## [1.8] - 2023-02-10

- Fixed getting the peer address after a disconnect.

## [1.7] - 2023-02-06

- `EventBus.__init__` is called from the server constructor.

## [1.6] - 2023-02-06

- Replaced the event emitter with the `event-bus` library.

## [1.5] - 2023-02-05

- Replaced the bundled event emitter.

## [1.4] - 2023-02-04

- `BrokenPipeError` on send is reported instead of escaping `run()`.

## [1.3] - 2023-02-03

- `ConnectionResetError` ends the connection instead of raising from the handler.

## [1.2] - 2023-02-02

- Removed the Flask integration.

## [1.1] - 2023-02-01

- `start` event with the listening address.

## [1.0] - 2023-01-24

- First published release: `select()`-based TCP server with `connect`, `message`
  and `disconnect` events.
