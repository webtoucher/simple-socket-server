# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.2.0] - 2026-11-15

### Added

- `max_pending` argument of `run()`: a cap of the outbound queue of one client, in
  bytes. A slow reader that grows past the cap is reported through `error` and
  closed instead of growing the memory of the server without a bound.
- `timeout` argument of `disconnect()`: how long the queue may still be flushed, in
  seconds. A client that stopped reading used to keep the connection forever,
  because the queue never drained; once the timeout expires the socket is closed
  with whatever is left unsent.
- Tests for the new behaviour: 200 clients connecting at once, `send()` and
  `disconnect()` from another thread, the latency of such a reply, the thread every
  handler runs in, a repeated `disconnect()`, a rejected descriptor number and a
  client dropped by `max_pending`.

### Fixed

- `disconnect()` called from another thread could stop the whole server with
  `OSError` (`Bad file descriptor` / `WSAENOTSOCK`): the foreign thread closed the
  socket between the snapshot of the poll set and `select()`. Sockets are closed
  only by the thread of `run()` now, so a foreign `disconnect()` just marks the
  client and the poll thread finishes it. As a side effect, `disconnect` is emitted
  in the thread of `run()`, as the documentation promises.
- The default backlog of `listen()` was 5, so a burst of simultaneous connections
  lost a part of them: 185 of 200 clients were served. It is `socket.SOMAXCONN`
  now, and all 200 clients of the test get their reply.
- A reply queued from another thread waited for the next poll: the median was
  100 ms, the whole poll interval. An empty queue is written straight to the socket
  now, and only the unwritten tail is queued, so the median drops below a
  millisecond.
- `disconnect()` for a client that is already closed raised `ValueError`, which a
  race with the peer or with the poll thread makes unavoidable. It is idempotent
  now; an unknown socket still raises.
- On POSIX a descriptor number of 1024 or more made `select()` fail with
  `ValueError`, and the number is not a count, so `max_clients` did not cover it: a
  process holding many files stopped the server. An accepted client above the
  limit is closed with `error`, and a listening socket above it fails `run()` with
  `SimpleSocketServerException` instead of a bare `ValueError`.

### Changed

- `max_conn` defaults to `socket.SOMAXCONN` instead of 5. It is still the backlog
  of `listen()`, not a limit of served clients; `max_clients` limits those.
- Documentation: the thread rules of `send()` and `disconnect()`, the idempotent
  `disconnect()`, `max_pending` and the descriptor limit of `select()` are spelled
  out. It also says that `max_pending` counts only the bytes still waiting to be
  written and that `stop()` does not flush the queues.
- Version numbers carry three components from this release on: `2.2.0` instead of
  `2.2`.

## [2.1] - 2026-11-15

### Added

- `disconnect(sock)`: a graceful way to drop a client from a handler or from
  another thread. The socket stops being read, the replies already queued for it
  are delivered, and only then it is closed and `disconnect` is emitted.
- `max_clients` argument of `run()` (500 by default): a connection above the limit
  is accepted, closed at once and reported through the `error` event.
- Tests for the new behaviour: a client half-close, a queued reply delivered before
  the close, a socket closed inside a handler, a failing `disconnect` handler
  during `stop()`, the client limit and a port that is already taken.

### Fixed

- `select()` could kill the server with `ValueError`. A socket closed behind the
  server's back left the poll set with a dead descriptor (`file descriptor cannot
  be a negative integer`), and an oversized client set failed the same way
  (`filedescriptor out of range`). Dead sockets are dropped before the poll,
  failures of `select()` are reported through `error` and stop the server instead
  of escaping `run()`, and the number of clients is capped below `FD_SETSIZE`.
- The reply to a client that half-closed the connection (`sendall()` followed by
  `shutdown(SHUT_WR)`, the way `nc -N` behaves) was thrown away. A connection that
  ended its input keeps being written to until its queue is drained.
- `stop()` could leave clients open and the port listening when a `disconnect`
  handler raised. The listening socket is closed first, every client is closed in
  its own `try`, and `stop` is emitted in any case; the first handler failure is
  re-raised afterwards.
- `send()` called from another thread mutated the poll state without a lock. The
  queues and the poll sets are guarded by a lock now, and handlers are never called
  under it.
- Urgent (out of band) data disconnected the client without an `error` event. Only
  the listening socket is passed to the exceptional set of `select()`.
- A `BlockingIOError` of `accept()` was reported as an `error` event. It is ignored
  now, any other `accept()` failure pauses before the next attempt, so a descriptor
  shortage does not spin the loop.

### Changed

- A `disconnect` handler that raises no longer aborts the cleanup: `run()` closes
  every socket and emits `stop` before the exception surfaces.
- `error` for a failed `select()` or a broken listening socket reports the listening
  socket and `peer=None`; for a rejected connection it reports the client socket
  and its address.
- Concurrent clients are limited to 500 by default. A larger `max_clients` can
  exceed the descriptor limit of `select()` (512 on Windows), which stops the server
  with an `error` event.
- The version test reads the package metadata instead of a hard-coded version.
- Documentation: `max_conn` is described as the `listen()` backlog and `max_clients`
  as the limit of concurrent clients; the thread safety of `send()` and
  `disconnect()`, the unbounded outbound queue, the `select()` descriptor limit and
  the rule that a handler must not raise are spelled out.

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
