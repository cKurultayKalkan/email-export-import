"""Password storage wrapper: round-trips through a fake keyring, and fails
closed (never raises) when no backend is present."""
import sys
import types

import pytest

from email_export_import import secrets_store


class _FakeKeyring:
    """Minimal in-memory stand-in for the keyring module."""

    def __init__(self):
        self._store = {}
        self._backend = object()

    def get_keyring(self):
        return self._backend

    def set_password(self, service, key, pw):
        self._store[(service, key)] = pw

    def get_password(self, service, key):
        return self._store.get((service, key))

    def delete_password(self, service, key):
        self._store.pop((service, key), None)


@pytest.fixture
def fake_backend(monkeypatch):
    fake = _FakeKeyring()
    # _keyring() checks isinstance(kr, fail.Keyring); give it a fail module
    # whose Keyring the fake backend is NOT an instance of.
    fail_mod = types.ModuleType("keyring.backends.fail")
    fail_mod.Keyring = type("FailKeyring", (), {})
    monkeypatch.setattr(secrets_store, "_keyring", lambda: fake)
    return fake


def test_save_get_delete_round_trip(fake_backend):
    assert secrets_store.save_password("h", "a@x", "source", "pw123")
    assert secrets_store.get_password("h", "a@x", "source") == "pw123"
    # role scopes the secret: destination password for the same address differs
    secrets_store.save_password("h", "a@x", "dest", "other")
    assert secrets_store.get_password("h", "a@x", "source") == "pw123"
    assert secrets_store.get_password("h", "a@x", "dest") == "other"
    secrets_store.delete_password("h", "a@x", "source")
    assert secrets_store.get_password("h", "a@x", "source") is None


def test_no_backend_fails_closed(monkeypatch):
    monkeypatch.setattr(secrets_store, "_keyring", lambda: None)
    assert secrets_store.available() is False
    assert secrets_store.save_password("h", "a@x", "source", "pw") is False
    assert secrets_store.get_password("h", "a@x", "source") is None
    secrets_store.delete_password("h", "a@x", "source")  # must not raise


def test_backend_errors_never_propagate(monkeypatch):
    class Boom:
        def set_password(self, *a): raise RuntimeError("keychain locked")
        def get_password(self, *a): raise RuntimeError("keychain locked")
        def delete_password(self, *a): raise RuntimeError("keychain locked")

    monkeypatch.setattr(secrets_store, "_keyring", lambda: Boom())
    assert secrets_store.save_password("h", "a@x", "source", "pw") is False
    assert secrets_store.get_password("h", "a@x", "source") is None
    secrets_store.delete_password("h", "a@x", "source")  # must not raise


def test_available_false_for_fail_backend(monkeypatch):
    # A real keyring present but with only the "fail" backend must read as
    # unavailable, so the app keeps prompting instead of pretending to store.
    fail_mod = types.ModuleType("keyring.backends.fail")

    class FailKeyring:
        pass

    fail_mod.Keyring = FailKeyring
    backends_mod = types.ModuleType("keyring.backends")
    backends_mod.fail = fail_mod
    kr_mod = types.ModuleType("keyring")
    kr_mod.get_keyring = lambda: FailKeyring()
    kr_mod.backends = backends_mod

    monkeypatch.setitem(sys.modules, "keyring", kr_mod)
    monkeypatch.setitem(sys.modules, "keyring.backends", backends_mod)
    monkeypatch.setitem(sys.modules, "keyring.backends.fail", fail_mod)
    assert secrets_store.available() is False
