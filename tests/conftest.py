"""Shared fixtures. Every test runs with non-loopback network access blocked."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from typing import Any

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


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)


@pytest.fixture
def make_row() -> Callable[..., ResultRow]:
    def _make(**overrides: Any) -> ResultRow:
        return ResultRow(**row_values(**overrides))

    return _make
