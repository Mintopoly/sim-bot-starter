"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from simbot import Client


@pytest.fixture
def api_key() -> str:
    return "sim_" + "a" * 64


@pytest.fixture
def base_url() -> str:
    return "https://test.example.com"


@pytest.fixture
def client(api_key: str, base_url: str) -> Client:
    return Client(api_key=api_key, base_url=base_url, retries=0)
