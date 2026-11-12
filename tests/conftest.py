"""Shared fixtures. Every test runs with non-loopback network access blocked."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from relay.schemas import ResultRow
from tests.factories import row_values

_real_connect = socket.socket.connect


def _is_loopback(host: object) -> bool:
    if host == "localhost":
        return True
    if not isinstance(host, str):
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _guarded_connect(self: socket.socket, address: Any) -> Any:
    host = address[0] if isinstance(address, tuple) else address
    if not _is_loopback(host):
        raise RuntimeError(f"network access is blocked in tests (attempted {host!r})")
    return _real_connect(self, address)


def _check_httpx_host(request: httpx.Request) -> None:
    if not _is_loopback(request.url.host):
        raise RuntimeError(f"network access is blocked in tests (attempted {request.url.host!r})")


_real_async_handle = httpx.AsyncHTTPTransport.handle_async_request
_real_sync_handle = httpx.HTTPTransport.handle_request


async def _guarded_async_handle(
    self: httpx.AsyncHTTPTransport, request: httpx.Request
) -> httpx.Response:
    _check_httpx_host(request)
    return await _real_async_handle(self, request)


def _guarded_sync_handle(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
    _check_httpx_host(request)
    return _real_sync_handle(self, request)


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    # socket.connect alone does not cover asyncio's Windows proactor sockets, so the
    # real httpx transports are guarded as well. MockTransport is unaffected.
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _guarded_async_handle)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _guarded_sync_handle)


@pytest.fixture
def make_row() -> Callable[..., ResultRow]:
    def _make(**overrides: Any) -> ResultRow:
        return ResultRow(**row_values(**overrides))

    return _make
