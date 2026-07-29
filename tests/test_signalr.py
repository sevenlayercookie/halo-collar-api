from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from halo_collar import (
    AuthenticationError,
    HaloClient,
    HaloSignalRClient,
    SignalRBackpressureError,
    SignalRConnectionError,
    SignalRHub,
    SignalRProtocolError,
    StateStore,
    TokenSet,
)

RS = "\x1e"


def tokens() -> TokenSet:
    return TokenSet(
        access_token="halo-access",
        refresh_token="halo-refresh",
        expires_at=datetime.now(timezone.utc).timestamp() + 3600,
    )


class FakeWebSocket:
    def __init__(self, messages: list[str | bytes | BaseException]) -> None:
        self.messages = deque(messages)
        self.sent: list[str | bytes] = []
        self.closed = False
        self._closed = asyncio.Event()

    async def recv(self) -> str | bytes:
        if self.messages:
            value = self.messages.popleft()
            if isinstance(value, BaseException):
                raise value
            return value
        await self._closed.wait()
        raise OSError("socket closed")

    async def send(self, message: str | bytes) -> None:
        if self.closed:
            raise OSError("socket closed")
        self.sent.append(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self._closed.set()


def rest_client(tmp_path) -> HaloClient:
    return HaloClient(
        client_secret="secret",
        tokens=tokens(),
        store=StateStore(tmp_path / "state.json"),
        app_instance_id="app-instance",
        amplitude_session_id="1700000000000",
        timezone_name="UTC",
        http=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500))),
    )


def invocation(
    target: str = "HandleIoTTelemetry",
    *,
    pet_id: str = "pet-1",
    sequence_code: int = 42,
) -> str:
    return json.dumps(
        {
            "type": 1,
            "target": target,
            "arguments": [
                {
                    "petId": pet_id,
                    "petTelemetry": {"manifest": {"sequenceCode": sequence_code}},
                }
            ],
        },
        separators=(",", ":"),
    )


def negotiation_transport(
    *,
    redirects: list[tuple[str, str, str]] | None = None,
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    requests: list[httpx.Request] = []
    states = deque(
        redirects
        or [
            (
                "https://azure.example/client/?hub=telemetry",
                "azure-access",
                "connection-token",
            )
        ]
    )
    active: tuple[str, str, str] | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active
        requests.append(request)
        if request.url.host == "sockets.example":
            active = states.popleft()
            redirect_url, access_token, _ = active
            return httpx.Response(
                200,
                json={"url": redirect_url, "accessToken": access_token},
            )
        if request.url.host == "azure.example":
            assert active is not None
            _, access_token, connection_token = active
            assert request.headers["Authorization"] == f"Bearer {access_token}"
            return httpx.Response(
                200,
                json={
                    "connectionId": f"display-{connection_token}",
                    "connectionToken": connection_token,
                    "negotiateVersion": 1,
                    "availableTransports": [
                        {"transport": "WebSockets", "transferFormats": ["Text", "Binary"]}
                    ],
                },
            )
        raise AssertionError(request.url)

    return httpx.MockTransport(handler), requests


def test_two_stage_negotiation_and_event_delivery(tmp_path) -> None:
    async def scenario() -> None:
        transport, requests = negotiation_transport()
        websocket = FakeWebSocket(
            [
                "{}"
                + RS
                + invocation()
                + RS
                + json.dumps({"type": 6})
                + RS,
            ]
        )
        connections: list[tuple[str, dict[str, Any]]] = []

        async def connector(url: str, **kwargs: Any) -> FakeWebSocket:
            connections.append((url, kwargs))
            return websocket

        async with httpx.AsyncClient(transport=transport) as http:
            stream = HaloSignalRClient(
                rest_client(tmp_path),
                base_url="https://sockets.example",
                http=http,
                connector=connector,
                reconnect_delay=0.001,
            )
            iterator = stream.events()
            event = await asyncio.wait_for(anext(iterator), timeout=1)
            assert event.hub is SignalRHub.TELEMETRY
            assert event.target == "HandleIoTTelemetry"
            assert event.pet_id == "pet-1"
            assert event.sequence_code == 42
            assert websocket.sent[0] == '{"protocol":"json","version":1}' + RS

            assert [request.url.path for request in requests] == [
                "/TelemetryHub/negotiate",
                "/client/negotiate",
            ]
            assert parse_qs(requests[0].url.query.decode()) == {"negotiateVersion": ["1"]}
            assert requests[0].headers["Authorization"] == "Bearer halo-access"
            assert "clientId=halo.app.ios" in requests[0].headers["Halo-Client"]
            assert requests[0].headers["Halo-Amplitude-SessionId"] == "1700000000000"
            assert parse_qs(requests[1].url.query.decode()) == {
                "hub": ["telemetry"],
                "negotiateVersion": ["1"],
            }

            websocket_url, options = connections[0]
            assert websocket_url.startswith("wss://azure.example/client/?")
            assert parse_qs(urlsplit_query(websocket_url)) == {
                "hub": ["telemetry"],
                "id": ["connection-token"],
            }
            assert options["headers"] == {"Authorization": "Bearer azure-access"}
            await iterator.aclose()

    asyncio.run(scenario())


def test_reconnect_repeats_both_negotiations_and_rotates_azure_token(tmp_path) -> None:
    async def scenario() -> None:
        transport, requests = negotiation_transport(
            redirects=[
                (
                    "https://azure.example/client/?hub=telemetry",
                    "azure-one",
                    "connection-one",
                ),
                (
                    "https://azure.example/client/?hub=telemetry",
                    "azure-two",
                    "connection-two",
                ),
            ]
        )
        sockets = deque(
            [
                FakeWebSocket(["{}" + RS + invocation(sequence_code=1) + RS, OSError("lost")]),
                FakeWebSocket(["{}" + RS + invocation(sequence_code=2) + RS]),
            ]
        )
        connections: list[tuple[str, dict[str, Any]]] = []

        async def connector(url: str, **kwargs: Any) -> FakeWebSocket:
            connections.append((url, kwargs))
            return sockets.popleft()

        async with httpx.AsyncClient(transport=transport) as http:
            stream = HaloSignalRClient(
                rest_client(tmp_path),
                base_url="https://sockets.example",
                http=http,
                connector=connector,
                reconnect_delay=0.001,
                max_reconnect_delay=0.001,
            )
            iterator = stream.events()
            first = await asyncio.wait_for(anext(iterator), timeout=1)
            second = await asyncio.wait_for(anext(iterator), timeout=1)
            assert [first.sequence_code, second.sequence_code] == [1, 2]
            assert [request.url.host for request in requests] == [
                "sockets.example",
                "azure.example",
                "sockets.example",
                "azure.example",
            ]
            assert parse_qs(urlsplit_query(connections[0][0]))["id"] == ["connection-one"]
            assert parse_qs(urlsplit_query(connections[1][0]))["id"] == ["connection-two"]
            assert connections[0][1]["headers"]["Authorization"] == "Bearer azure-one"
            assert connections[1][1]["headers"]["Authorization"] == "Bearer azure-two"
            await iterator.aclose()

    asyncio.run(scenario())


def test_server_close_with_allow_reconnect_negotiates_again(tmp_path) -> None:
    async def scenario() -> None:
        transport, requests = negotiation_transport(
            redirects=[
                (
                    "https://azure.example/client/?hub=telemetry",
                    "azure-one",
                    "connection-one",
                ),
                (
                    "https://azure.example/client/?hub=telemetry",
                    "azure-two",
                    "connection-two",
                ),
            ]
        )
        close = json.dumps({"type": 7, "allowReconnect": True})
        sockets = deque(
            [
                FakeWebSocket(["{}" + RS + invocation(sequence_code=1) + RS + close + RS]),
                FakeWebSocket(["{}" + RS + invocation(sequence_code=2) + RS]),
            ]
        )
        connections: list[str] = []

        async def connector(url: str, **_: Any) -> FakeWebSocket:
            connections.append(url)
            return sockets.popleft()

        async with httpx.AsyncClient(transport=transport) as http:
            stream = HaloSignalRClient(
                rest_client(tmp_path),
                base_url="https://sockets.example",
                http=http,
                connector=connector,
                reconnect_delay=0.001,
                max_reconnect_delay=0.001,
            )
            iterator = stream.events()
            first = await asyncio.wait_for(anext(iterator), timeout=1)
            second = await asyncio.wait_for(anext(iterator), timeout=1)
            assert [first.sequence_code, second.sequence_code] == [1, 2]
            assert len(requests) == 4
            assert len(connections) == 2
            await iterator.aclose()

    asyncio.run(scenario())


def test_signalr_ping_runs_while_the_consumer_is_idle(tmp_path) -> None:
    async def scenario() -> None:
        transport, _ = negotiation_transport()
        websocket = FakeWebSocket(["{}" + RS])

        async def connector(url: str, **kwargs: Any) -> FakeWebSocket:
            return websocket

        async with httpx.AsyncClient(transport=transport) as http:
            stream = HaloSignalRClient(
                rest_client(tmp_path),
                base_url="https://sockets.example",
                http=http,
                connector=connector,
                signalr_ping_interval=0.01,
                server_timeout=1,
            )
            await stream.wait_connected(timeout=1)
            await asyncio.sleep(0.035)
            assert '{"type":6}' + RS in websocket.sent
            await stream.close()

    asyncio.run(scenario())


def test_bad_handshake_is_terminal_and_does_not_reconnect(tmp_path) -> None:
    async def scenario() -> None:
        transport, requests = negotiation_transport()
        websocket = FakeWebSocket([json.dumps({"error": "unsupported"}) + RS])
        connection_count = 0

        async def connector(url: str, **kwargs: Any) -> FakeWebSocket:
            nonlocal connection_count
            connection_count += 1
            return websocket

        async with httpx.AsyncClient(transport=transport) as http:
            stream = HaloSignalRClient(
                rest_client(tmp_path),
                base_url="https://sockets.example",
                http=http,
                connector=connector,
            )
            with pytest.raises(SignalRProtocolError, match="rejected"):
                await stream.wait_connected(timeout=1)
            assert connection_count == 1
            assert len(requests) == 2
            await stream.close()

    asyncio.run(scenario())


def test_backpressure_fails_instead_of_dropping_events(tmp_path) -> None:
    async def scenario() -> None:
        transport, _ = negotiation_transport()
        websocket = FakeWebSocket(
            [
                "{}" + RS + invocation(sequence_code=1) + RS + invocation(sequence_code=2) + RS,
            ]
        )

        async def connector(url: str, **kwargs: Any) -> FakeWebSocket:
            return websocket

        async with httpx.AsyncClient(transport=transport) as http:
            stream = HaloSignalRClient(
                rest_client(tmp_path),
                base_url="https://sockets.example",
                http=http,
                connector=connector,
                queue_size=1,
            )
            await stream.start()
            assert stream._task is not None
            await asyncio.wait_for(stream._task, timeout=1)
            iterator = stream.events()
            with pytest.raises(SignalRBackpressureError, match="1-event limit"):
                await anext(iterator)
            await iterator.aclose()

    asyncio.run(scenario())


def test_notification_hub_and_split_records(tmp_path) -> None:
    async def scenario() -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.host == "sockets.example":
                return httpx.Response(
                    200,
                    json={
                        "url": "https://azure.example/client/?hub=notifications",
                        "accessToken": "azure",
                    },
                )
            return httpx.Response(
                200,
                json={"connectionToken": "notification-connection"},
            )

        message = invocation("HandleDataStateChanged")
        websocket = FakeWebSocket(["{" + "}" + RS + message[:12], message[12:] + RS])

        async def connector(url: str, **kwargs: Any) -> FakeWebSocket:
            return websocket

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            stream = HaloSignalRClient(
                rest_client(tmp_path),
                hub="notifications",
                base_url="https://sockets.example",
                http=http,
                connector=connector,
            )
            iterator = stream.events()
            event = await asyncio.wait_for(anext(iterator), timeout=1)
            assert event.hub is SignalRHub.NOTIFICATIONS
            assert event.target == "HandleDataStateChanged"
            assert requests[0].url.path == "/NotificationHub/negotiate"
            await iterator.aclose()

    asyncio.run(scenario())


def test_negotiation_errors_do_not_expose_response_credentials(tmp_path) -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                500,
                json={"accessToken": "must-not-leak", "error": "private-error-body"},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            stream = HaloSignalRClient(
                rest_client(tmp_path),
                base_url="https://sockets.example",
                http=http,
                max_reconnect_attempts=0,
                reconnect_delay=0.001,
            )
            iterator = stream.events()
            with pytest.raises(SignalRConnectionError) as raised:
                await asyncio.wait_for(anext(iterator), timeout=1)
            assert "must-not-leak" not in str(raised.value)
            assert "private-error-body" not in str(raised.value)
            await iterator.aclose()

    asyncio.run(scenario())


def test_authentication_failure_is_delivered_by_the_iterator(tmp_path) -> None:
    async def scenario() -> None:
        stream = HaloSignalRClient(
            rest_client(tmp_path),
            base_url="https://sockets.example",
            max_reconnect_attempts=0,
        )

        async def fail_refresh(*, force: bool = False) -> TokenSet:
            raise AuthenticationError("authentication unavailable")

        stream._refresh_login = fail_refresh  # type: ignore[method-assign]
        iterator = stream.events()
        with pytest.raises(AuthenticationError, match="unavailable"):
            await asyncio.wait_for(anext(iterator), timeout=1)
        await iterator.aclose()

    asyncio.run(scenario())


def urlsplit_query(url: str) -> str:
    return httpx.URL(url).query.decode()
