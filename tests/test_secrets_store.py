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
    monkeypatch.setattr(secrets_store, "_backend", lambda: fake)
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
    monkeypatch.setattr(secrets_store, "_backend", lambda: None)
    assert secrets_store.available() is False
    assert secrets_store.save_password("h", "a@x", "source", "pw") is False
    assert secrets_store.get_password("h", "a@x", "source") is None
    secrets_store.delete_password("h", "a@x", "source")  # must not raise


def test_backend_errors_never_propagate(monkeypatch):
    class Boom:
        def set_password(self, *a): raise RuntimeError("keychain locked")
        def get_password(self, *a): raise RuntimeError("keychain locked")
        def delete_password(self, *a): raise RuntimeError("keychain locked")

    monkeypatch.setattr(secrets_store, "_backend", lambda: Boom())
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
    # force the keyring path (the macOS `security` backend would otherwise win)
    monkeypatch.setattr(secrets_store._MacSecurity, "available",
                        staticmethod(lambda: False))
    assert secrets_store.available() is False


def test_macos_security_backend_round_trip(monkeypatch):
    # The macOS backend shells out to `security`; drive it with a fake CLI so
    # the test never touches the real keychain.
    import sys as _sys

    from email_export_import import secrets_store as ss

    store = {}

    class FakeCompleted:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    def fake_security(*args, input_text=None):
        cmd = args[0]
        # parse -a <acct> -s <svc>
        acct = args[args.index("-a") + 1]
        svc = args[args.index("-s") + 1]
        if cmd == "add-generic-password":
            store[(svc, acct)] = args[args.index("-w") + 1]
            return FakeCompleted(0)
        if cmd == "find-generic-password":
            v = store.get((svc, acct))
            return FakeCompleted(0, out=v + "\n") if v is not None else FakeCompleted(44)
        if cmd == "delete-generic-password":
            store.pop((svc, acct), None)
            return FakeCompleted(0)
        return FakeCompleted(1, err="unknown")

    monkeypatch.setattr(ss, "_security", fake_security)
    monkeypatch.setattr(ss._MacSecurity, "available", staticmethod(lambda: True))
    monkeypatch.setattr(ss, "sys", type("S", (), {"platform": "darwin"}))

    assert ss.available() is True
    assert ss.save_password("h", "a@x", "source", "pw123")
    assert ss.get_password("h", "a@x", "source") == "pw123"
    ss.delete_password("h", "a@x", "source")
    assert ss.get_password("h", "a@x", "source") is None
