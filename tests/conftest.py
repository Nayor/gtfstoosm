"""Shared test setup."""

import pytest
import requests


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never reach a real server; a test that needs requests replaces them."""

    def refuse(*args, **kwargs):
        raise AssertionError(f"unexpected network access: {args[:1]}")

    monkeypatch.setattr(requests, "get", refuse)
    monkeypatch.setattr(requests, "post", refuse)
