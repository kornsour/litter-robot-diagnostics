import pytest

from lr4_diagnostics import auth


class FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.values.pop((service, username), None)


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    value = FakeKeyring()
    monkeypatch.setattr(auth, "keyring", value)
    return value


def test_store_password_remembers_default_username(
    fake_keyring: FakeKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WHISKER_USERNAME", raising=False)

    auth.store_password("owner@example.com", "secret")

    assert auth.resolve_username(None) == "owner@example.com"
    assert fake_keyring.get_password(auth.SERVICE, "owner@example.com") == "secret"


def test_explicit_username_takes_precedence(
    fake_keyring: FakeKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    del fake_keyring
    monkeypatch.setenv("WHISKER_USERNAME", "environment@example.com")

    assert auth.resolve_username("explicit@example.com") == "explicit@example.com"


def test_clear_credentials_removes_matching_default_username(
    fake_keyring: FakeKeyring,
) -> None:
    auth.store_password("owner@example.com", "secret")
    auth.save_token("owner@example.com", {"access_token": "token"})

    auth.clear_credentials("owner@example.com")

    assert fake_keyring.values == {}


def test_missing_username_has_actionable_error(
    fake_keyring: FakeKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    del fake_keyring
    monkeypatch.delenv("WHISKER_USERNAME", raising=False)

    with pytest.raises(auth.CredentialError, match="auth store"):
        auth.resolve_username(None)
