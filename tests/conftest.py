"""Shared test fixtures."""

import pytest
import pytest_socket


@pytest.fixture
def internet() -> None:
    """Allow real network access (only for the opt-in live tests).

    Home Assistant's test plugin blocks sockets and allows only 127.0.0.1 for every test; this lifts
    both restrictions for the requesting test (they are set up again before the next one).
    """
    pytest_socket.enable_socket()
    pytest_socket._remove_restrictions()
