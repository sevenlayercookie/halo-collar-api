"""Async, receive-only client for Halo's observed SignalR hubs."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from .client import HaloClient
from .errors import (
    AuthenticationError,
    LoginRequiredError,
    SignalRBackpressureError,
    SignalRConnectionError,
    SignalRNegotiationError,
    SignalRProtocolError,
)
from .models import TokenSet

SIGNALR_BASE_URL = "https://halo-prod-sockets-app.azurewebsites.net"
RECORD_SEPARATOR = "\x1e"
_HANDSHAKE = '{"protocol":"json","version":1}' + RECORD_SEPARATOR
_PING = '{"type":6}' + RECORD_SEPARATOR
_END = object()
_LOGGER = logging.getLogger(__name__)


class SignalRHub(str, Enum):
    """Halo hubs observed in official app traffic."""

    TELEMETRY = "TelemetryHub"
    NOTIFICATIONS = "NotificationHub"

    @classmethod
    def parse(cls, value: SignalRHub | str) -> SignalRHub:
        if isinstance(value, cls):
            return value
        normalized = value.replace("-", "").replace("_", "").casefold()
        for item in cls:
            if normalized in {item.name.replace("_", "").casefold(), item.value.casefold()}:
                return item
        choices = ", ".join(item.value for item in cls)
        raise ValueError(f"Unknown SignalR hub {value!r}. Choose one of: {choices}")


@dataclass(slots=True)
class SignalREvent:
    """One server-to-client SignalR invocation."""

    hub: SignalRHub
    target: str
    arguments: list[Any]
    raw: dict[str, Any]

    @property
    def sequence_code(self) -> Any | None:
        """Return Halo's ordering marker when this is an IoT telemetry event."""

        for argument in self.arguments:
            if not isinstance(argument, dict):
                continue
            pet_telemetry = argument.get("petTelemetry")
            if not isinstance(pet_telemetry, dict):
                continue
            manifest = pet_telemetry.get("manifest")
            if isinstance(manifest, dict) and "sequenceCode" in manifest:
                return manifest["sequenceCode"]
        return None

    @property
    def pet_id(self) -> str | None:
        """Return the pet identifier carried by a telemetry event, if present."""

        for argument in self.arguments:
            if isinstance(argument, dict) and argument.get("petId") is not None:
                return str(argument["petId"])
        return None


class _WebSocket(Protocol):
    async def recv(self) -> str | bytes: ...

    async def send(self, message: str | bytes) -> None: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


_Connector = Callable[..., Awaitable[_WebSocket]]


@dataclass(frozen=True, slots=True)
class _NegotiatedConnection:
    url: str
    access_token: str


@dataclass(slots=True)
class _OpenConnection:
    websocket: _WebSocket
    reader: _RecordReader


class _RecordReader:
    """Split SignalR's record-separated JSON while retaining partial records."""

    def __init__(self, max_record_size: int) -> None:
        self.max_record_size = max_record_size
        self._buffer = ""
        self._records: deque[str] = deque()

    async def next(self, websocket: _WebSocket, timeout: float) -> str:
        while not self._records:
            raw = await asyncio.wait_for(websocket.recv(), timeout=timeout)
            if isinstance(raw, bytes):
                try:
                    raw = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise SignalRProtocolError(
                        "Halo sent a SignalR message that was not UTF-8 JSON."
                    ) from exc
            if not isinstance(raw, str):
                raise SignalRProtocolError("Halo sent an unsupported SignalR message type.")
            self._buffer += raw
            parts = self._buffer.split(RECORD_SEPARATOR)
            self._buffer = parts.pop()
            for part in parts:
                if part:
                    if len(part.encode("utf-8")) > self.max_record_size:
                        raise SignalRProtocolError(
                            "A Halo SignalR record exceeded the size limit."
                        )
                    self._records.append(part)
            if len(self._buffer.encode("utf-8")) > self.max_record_size:
                raise SignalRProtocolError("A Halo SignalR record exceeded the size limit.")
        return self._records.popleft()


class _ServerClosed(Exception):
    def __init__(self, message: str | None, allow_reconnect: bool) -> None:
        super().__init__(message or "Halo closed the SignalR connection.")
        self.allow_reconnect = allow_reconnect


class _UnauthorizedNegotiation(Exception):
    pass


class _EventIterator:
    """Single-consumer iterator without async-generator shutdown side effects."""

    def __init__(self, client: HaloSignalRClient) -> None:
        self._client = client
        self._finished = False

    def __aiter__(self) -> _EventIterator:
        return self

    async def __anext__(self) -> SignalREvent:
        if self._finished:
            raise StopAsyncIteration
        await self._client.start()
        assert self._client._queue is not None
        item = await self._client._queue.get()
        if item is _END:
            self._finished = True
            self._client._consumer_active = False
            error = self._client._terminal_error
            await self._client.close()
            if error is not None:
                raise error
            raise StopAsyncIteration
        assert isinstance(item, SignalREvent)
        return item

    async def aclose(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._client._consumer_active = False
        await self._client.close()


async def _default_connector(
    url: str,
    *,
    headers: dict[str, str],
    open_timeout: float,
    close_timeout: float,
    max_size: int,
) -> _WebSocket:
    return await websocket_connect(
        url,
        additional_headers=headers,
        open_timeout=open_timeout,
        close_timeout=close_timeout,
        max_size=max_size,
        ping_interval=20,
        ping_timeout=20,
    )


class HaloSignalRClient:
    """Maintain one async stream of server-to-client events from a Halo hub.

    The existing :class:`HaloClient` remains responsible for OAuth refresh and
    local token storage. This client performs Halo's negotiation, follows the
    Azure SignalR redirect, opens the WebSocket, and repeats the entire chain
    after a transient disconnect.

    Reading runs in a background task so a briefly busy consumer does not stop
    heartbeats. The queue is bounded; sustained backpressure closes the stream
    with :class:`SignalRBackpressureError` instead of silently dropping location
    updates or growing memory without limit.
    """

    def __init__(
        self,
        halo: HaloClient,
        *,
        hub: SignalRHub | str = SignalRHub.TELEMETRY,
        base_url: str = SIGNALR_BASE_URL,
        http: httpx.AsyncClient | None = None,
        connector: _Connector | None = None,
        queue_size: int = 256,
        max_reconnect_attempts: int | None = 8,
        reconnect_delay: float = 0.5,
        max_reconnect_delay: float = 15.0,
        handshake_timeout: float = 10.0,
        server_timeout: float = 45.0,
        signalr_ping_interval: float = 15.0,
        max_message_size: int = 1024 * 1024,
        max_negotiation_redirects: int = 5,
    ) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be at least 1.")
        if max_reconnect_attempts is not None and max_reconnect_attempts < 0:
            raise ValueError("max_reconnect_attempts must be zero or greater.")
        for value, name in (
            (reconnect_delay, "reconnect_delay"),
            (max_reconnect_delay, "max_reconnect_delay"),
            (handshake_timeout, "handshake_timeout"),
            (server_timeout, "server_timeout"),
            (signalr_ping_interval, "signalr_ping_interval"),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero.")
        if max_message_size < 1:
            raise ValueError("max_message_size must be at least 1.")
        if max_negotiation_redirects < 1:
            raise ValueError("max_negotiation_redirects must be at least 1.")

        self.halo = halo
        self.hub = SignalRHub.parse(hub)
        self.base_url = base_url.rstrip("/")
        self.queue_size = queue_size
        self.max_reconnect_attempts = max_reconnect_attempts
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay
        self.handshake_timeout = handshake_timeout
        self.server_timeout = server_timeout
        self.signalr_ping_interval = signalr_ping_interval
        self.max_message_size = max_message_size
        self.max_negotiation_redirects = max_negotiation_redirects

        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=False,
        )
        self._connector = connector or _default_connector
        self._queue: asyncio.Queue[SignalREvent | object] | None = None
        self._task: asyncio.Task[None] | None = None
        self._close_event: asyncio.Event | None = None
        self._connected_event: asyncio.Event | None = None
        self._websocket: _WebSocket | None = None
        self._closed = False
        self._consumer_active = False
        self._terminal_error: BaseException | None = None

    @property
    def is_connected(self) -> bool:
        return self._connected_event is not None and self._connected_event.is_set()

    async def __aenter__(self) -> HaloSignalRClient:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def __aiter__(self) -> AsyncIterator[SignalREvent]:
        return self.events()

    async def start(self) -> None:
        """Start connecting in the background without waiting for the socket."""

        if self._closed:
            raise RuntimeError("A closed HaloSignalRClient cannot be restarted.")
        if self._task is not None:
            return
        self._queue = asyncio.Queue(maxsize=self.queue_size)
        self._close_event = asyncio.Event()
        self._connected_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._run(),
            name=f"halo-signalr-{self.hub.value}",
        )

    async def wait_connected(self, timeout: float | None = None) -> None:
        """Wait until the handshake succeeds, propagating an early terminal error."""

        await self.start()
        assert self._connected_event is not None
        assert self._task is not None
        connected = asyncio.create_task(self._connected_event.wait())
        try:
            done, _ = await asyncio.wait(
                {connected, self._task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise TimeoutError("Timed out waiting for Halo SignalR to connect.")
            if self._task in done:
                await self._task
                if self._terminal_error is not None:
                    raise self._terminal_error
                raise SignalRConnectionError(
                    "Halo SignalR stopped before completing its handshake."
                )
            await connected
        finally:
            if not connected.done():
                connected.cancel()
                with suppress(asyncio.CancelledError):
                    await connected

    def events(self) -> AsyncIterator[SignalREvent]:
        """Return the stream's sole live-event iterator."""

        if self._consumer_active:
            raise RuntimeError("HaloSignalRClient supports one event consumer.")
        self._consumer_active = True
        return _EventIterator(self)

    async def close(self) -> None:
        """Stop reconnecting, close the socket, and release owned HTTP resources."""

        if self._closed:
            return
        self._closed = True
        if self._close_event is not None:
            self._close_event.set()
        if self._websocket is not None:
            with suppress(Exception):
                await self._websocket.close()
        if self._task is not None and self._task is not asyncio.current_task():
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        elif self._owns_http:
            await self._http.aclose()

    async def _run(self) -> None:
        failures = 0
        terminal_error: BaseException | None = None
        try:
            while not self._closed:
                try:
                    connection = await self._open_connection()
                    self._websocket = connection.websocket
                    failures = 0
                    await self._consume(connection)
                    if not self._closed:
                        raise SignalRConnectionError(
                            "Halo closed the SignalR WebSocket without a close message."
                        )
                except asyncio.CancelledError:
                    raise
                except (
                    AuthenticationError,
                    SignalRBackpressureError,
                    SignalRProtocolError,
                ) as exc:
                    terminal_error = exc
                    break
                except _ServerClosed as exc:
                    if not exc.allow_reconnect:
                        terminal_error = SignalRConnectionError(str(exc))
                        break
                    failures += 1
                    terminal_error = exc
                except (
                    ConnectionClosed,
                    OSError,
                    TimeoutError,
                    asyncio.TimeoutError,
                    WebSocketException,
                    httpx.TransportError,
                    SignalRConnectionError,
                    SignalRNegotiationError,
                ) as exc:
                    failures += 1
                    terminal_error = exc
                finally:
                    if self._connected_event is not None:
                        self._connected_event.clear()
                    if self._websocket is not None:
                        with suppress(Exception):
                            await self._websocket.close()
                        self._websocket = None

                if self._closed:
                    terminal_error = None
                    break
                if (
                    self.max_reconnect_attempts is not None
                    and failures > self.max_reconnect_attempts
                ):
                    terminal_error = SignalRConnectionError(
                        f"Halo SignalR failed after {failures} connection attempts."
                    )
                    break
                delay = min(
                    self.reconnect_delay * (2 ** max(failures - 1, 0)),
                    self.max_reconnect_delay,
                )
                assert self._close_event is not None
                with suppress(TimeoutError, asyncio.TimeoutError):
                    await asyncio.wait_for(self._close_event.wait(), timeout=delay)
        except asyncio.CancelledError:
            terminal_error = None
            raise
        finally:
            self._terminal_error = terminal_error
            if self._owns_http:
                await self._http.aclose()
            self._finish_queue()

    async def _open_connection(self) -> _OpenConnection:
        tokens = await self._refresh_login()
        first_headers = {
            "Accept": "application/json",
            "Authorization": f"{tokens.token_type} {tokens.access_token}",
            "Halo-Client": self.halo.halo_client_header,
            "Halo-Amplitude-SessionId": self.halo.amplitude_session_id,
        }
        try:
            negotiated = await self._negotiate(
                f"{self.base_url}/{self.hub.value}",
                first_headers,
            )
        except _UnauthorizedNegotiation:
            tokens = await self._refresh_login(force=True)
            first_headers["Authorization"] = f"{tokens.token_type} {tokens.access_token}"
            try:
                negotiated = await self._negotiate(
                    f"{self.base_url}/{self.hub.value}",
                    first_headers,
                )
            except _UnauthorizedNegotiation as exc:
                raise LoginRequiredError(
                    "Halo rejected SignalR authentication after refreshing the login."
                ) from exc

        websocket = await self._connector(
            negotiated.url,
            headers={"Authorization": f"Bearer {negotiated.access_token}"},
            open_timeout=self.handshake_timeout,
            close_timeout=self.handshake_timeout,
            max_size=self.max_message_size,
        )
        reader = _RecordReader(self.max_message_size)
        try:
            await websocket.send(_HANDSHAKE)
            response = await reader.next(websocket, self.handshake_timeout)
            value = _decode_record(response)
            if value.get("error"):
                raise SignalRProtocolError("Halo rejected the SignalR protocol handshake.")
            if "type" in value:
                raise SignalRProtocolError(
                    "Halo sent an event before completing the SignalR handshake."
                )
        except BaseException:
            with suppress(Exception):
                await websocket.close()
            raise
        if self._connected_event is not None:
            self._connected_event.set()
        return _OpenConnection(websocket, reader)

    async def _refresh_login(self, *, force: bool = False) -> TokenSet:
        tokens = self.halo.tokens
        if not force and tokens is not None and not tokens.is_expired:
            return tokens
        return await asyncio.to_thread(self.halo.refresh_login, force=force)

    async def _negotiate(
        self,
        base_url: str,
        headers: dict[str, str],
    ) -> _NegotiatedConnection:
        current_url = base_url
        current_headers = headers.copy()
        access_token: str | None = None
        for _ in range(self.max_negotiation_redirects):
            try:
                response = await self._http.post(
                    _negotiate_url(current_url),
                    headers=current_headers,
                )
            except httpx.HTTPError as exc:
                raise SignalRNegotiationError(
                    "Could not reach Halo's SignalR negotiation service."
                ) from exc
            if response.status_code == 401:
                raise _UnauthorizedNegotiation
            if response.is_error:
                raise SignalRNegotiationError(
                    f"Halo SignalR negotiation failed with HTTP {response.status_code}."
                )
            try:
                value = response.json()
            except (json.JSONDecodeError, ValueError) as exc:
                raise SignalRNegotiationError(
                    "Halo returned a SignalR negotiation response that was not JSON."
                ) from exc
            if not isinstance(value, dict):
                raise SignalRNegotiationError(
                    "Halo returned an unexpected SignalR negotiation response."
                )
            error = value.get("error")
            if error:
                raise SignalRNegotiationError("Halo rejected the SignalR negotiation.")

            redirect_url = value.get("url")
            redirect_token = value.get("accessToken")
            if isinstance(redirect_url, str) and redirect_url:
                if redirect_token is not None and not isinstance(redirect_token, str):
                    raise SignalRNegotiationError(
                        "Halo returned an invalid SignalR redirect token."
                    )
                access_token = redirect_token or access_token
                if not access_token:
                    raise SignalRNegotiationError(
                        "Halo's SignalR redirect did not include an access token."
                    )
                current_url = redirect_url
                current_headers = {
                    "Accept": "application/json",
                    "Authorization": f"Bearer {access_token}",
                }
                continue

            connection_token = value.get("connectionToken") or value.get("connectionId")
            if isinstance(connection_token, str) and connection_token and access_token:
                return _NegotiatedConnection(
                    _websocket_url(current_url, connection_token),
                    access_token,
                )
            raise SignalRNegotiationError(
                "Halo's SignalR negotiation response contained no connection token."
            )
        raise SignalRNegotiationError("Halo returned too many SignalR negotiation redirects.")

    async def _consume(self, connection: _OpenConnection) -> None:
        loop = asyncio.get_running_loop()
        last_received = loop.time()
        next_ping = loop.time() + self.signalr_ping_interval
        while not self._closed:
            now = loop.time()
            remaining_server_time = self.server_timeout - (now - last_received)
            if remaining_server_time <= 0:
                raise TimeoutError("Halo SignalR stopped sending data.")
            timeout = max(0.001, min(next_ping - now, remaining_server_time))
            try:
                record = await connection.reader.next(connection.websocket, timeout)
            except (TimeoutError, asyncio.TimeoutError):
                now = loop.time()
                if now - last_received >= self.server_timeout:
                    raise TimeoutError("Halo SignalR stopped sending data.") from None
                if now >= next_ping:
                    await connection.websocket.send(_PING)
                    next_ping = now + self.signalr_ping_interval
                continue

            last_received = loop.time()
            event = self._event_from_record(record)
            if event is not None:
                self._queue_event(event)

    def _event_from_record(self, record: str) -> SignalREvent | None:
        value = _decode_record(record)
        message_type = value.get("type")
        if message_type == 1:
            target = value.get("target")
            arguments = value.get("arguments")
            if not isinstance(target, str) or not isinstance(arguments, list):
                raise SignalRProtocolError("Halo sent a malformed SignalR invocation.")
            return SignalREvent(
                hub=self.hub,
                target=target,
                arguments=arguments,
                raw=value,
            )
        if message_type == 6:
            return None
        if message_type == 7:
            error = value.get("error")
            raise _ServerClosed(
                str(error) if error else None,
                bool(value.get("allowReconnect", False)),
            )
        if isinstance(message_type, int):
            _LOGGER.debug("Ignoring unsupported SignalR message type %s.", message_type)
            return None
        raise SignalRProtocolError("Halo sent a SignalR message without a numeric type.")

    def _queue_event(self, event: SignalREvent) -> None:
        assert self._queue is not None
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull as exc:
            raise SignalRBackpressureError(
                f"The Halo SignalR event queue reached its {self.queue_size}-event limit."
            ) from exc

    def _finish_queue(self) -> None:
        if self._queue is None:
            return
        if self._queue.full():
            with suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
        with suppress(asyncio.QueueFull):
            self._queue.put_nowait(_END)


def _decode_record(record: str) -> dict[str, Any]:
    try:
        value = json.loads(record)
    except (json.JSONDecodeError, ValueError) as exc:
        raise SignalRProtocolError("Halo sent a SignalR record that was not valid JSON.") from exc
    if not isinstance(value, dict):
        raise SignalRProtocolError("Halo sent a SignalR record that was not an object.")
    return value


def _negotiate_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SignalRNegotiationError("Halo returned an invalid SignalR negotiation URL.")
    path = f"{parsed.path.rstrip('/')}/negotiate"
    query = [(key, value) for key, value in parse_qsl(parsed.query) if key != "negotiateVersion"]
    query.append(("negotiateVersion", "1"))
    return urlunsplit((parsed.scheme, parsed.netloc, path, urlencode(query), ""))


def _websocket_url(base_url: str, connection_token: str) -> str:
    parsed = urlsplit(base_url)
    schemes = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}
    scheme = schemes.get(parsed.scheme)
    if scheme is None or not parsed.netloc:
        raise SignalRNegotiationError("Halo returned an invalid SignalR WebSocket URL.")
    query = [(key, value) for key, value in parse_qsl(parsed.query) if key != "id"]
    query.append(("id", connection_token))
    return urlunsplit((scheme, parsed.netloc, parsed.path, urlencode(query), ""))
