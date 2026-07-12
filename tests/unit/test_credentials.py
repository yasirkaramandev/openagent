from openagent.core.models import CredentialRef, CredentialType
from openagent.credentials.store import CredentialStore


def test_env_credential_resolves(monkeypatch):
    monkeypatch.setenv("MY_TEST_KEY", "secret-value")
    store = CredentialStore()
    ref = CredentialRef(type=CredentialType.ENV, env_var="MY_TEST_KEY")
    assert store.resolve(ref) == "secret-value"
    assert store.available(ref) is True


def test_env_credential_missing():
    store = CredentialStore()
    ref = CredentialRef(type=CredentialType.ENV, env_var="DEFINITELY_NOT_SET_123")
    assert store.resolve(ref) is None
    assert store.available(ref) is False


def test_session_credential_roundtrip():
    store = CredentialStore()
    ref = CredentialRef(type=CredentialType.SESSION, account="openai/main")
    store.set_secret(ref, "in-memory-secret")
    assert store.resolve(ref) == "in-memory-secret"
    store.delete_secret(ref)
    assert store.resolve(ref) is None


def test_none_credential():
    store = CredentialStore()
    assert store.resolve(CredentialRef(type=CredentialType.NONE)) is None


def test_keychain_roundtrip_via_memory_backend():
    store = CredentialStore()
    ref = CredentialRef(type=CredentialType.KEYCHAIN, account="provider/test")
    store.set_secret(ref, "kc-secret")
    assert store.resolve(ref) == "kc-secret"
    store.delete_secret(ref)
    assert store.resolve(ref) is None


def test_keychain_resolve_survives_backend_failure(monkeypatch):
    """On a headless host with no usable keyring backend, resolving a keychain ref returns None
    instead of raising — so a run without a stored key still proceeds (no crash)."""
    import openagent.credentials.store as store_mod

    class _Boom:
        def get_password(self, *a, **k):
            raise RuntimeError("no keyring backend available")

    monkeypatch.setattr(store_mod, "keyring", _Boom())
    store = CredentialStore()
    ref = CredentialRef(type=CredentialType.KEYCHAIN, account="provider/x")
    assert store.resolve(ref) is None


def test_external_command_credential():
    store = CredentialStore()
    ref = CredentialRef(type=CredentialType.EXTERNAL_COMMAND, command=["printf", "cmd-secret"])
    assert store.resolve(ref) == "cmd-secret"
