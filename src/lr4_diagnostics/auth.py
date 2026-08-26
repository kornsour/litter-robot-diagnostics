"""Credential and token storage backed by the operating-system keyring."""

from __future__ import annotations

import json
import os
from getpass import getpass
from typing import Any

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

SERVICE = "litter-robot-diagnostics"
DEFAULT_USERNAME_KEY = "default-username"


class CredentialError(RuntimeError):
    """Raised when required credentials cannot be resolved safely."""


def resolve_username(explicit: str | None) -> str:
    """Resolve a username without persisting it outside the keyring."""
    username = explicit or os.getenv("WHISKER_USERNAME")
    if not username:
        try:
            username = keyring.get_password(SERVICE, DEFAULT_USERNAME_KEY)
        except KeyringError:
            username = None
    if not username:
        raise CredentialError(
            "A Whisker username is required. Run `lr4-diagnostics auth store --username "
            "you@example.com` first, or use --username or WHISKER_USERNAME."
        )
    return username


def resolve_password(username: str, *, allow_prompt: bool = True) -> str:
    """Load a Whisker password from the environment, keyring, or a silent prompt."""
    if password := os.getenv("WHISKER_PASSWORD"):
        return password
    try:
        if password := keyring.get_password(SERVICE, username):
            return password
    except KeyringError as exc:
        if not allow_prompt:
            raise CredentialError("The OS keyring is unavailable.") from exc
    if allow_prompt:
        password = getpass(f"Whisker password for {username}: ")
        if password:
            return password
    raise CredentialError("No Whisker password was found. Run `lr4-diagnostics auth store` first.")


def store_password(username: str, password: str | None = None) -> None:
    """Prompt for and store a Whisker password in the OS keyring."""
    value = password if password is not None else getpass(f"Whisker password for {username}: ")
    if not value:
        raise CredentialError("Password cannot be empty.")
    try:
        keyring.set_password(SERVICE, username, value)
        keyring.set_password(SERVICE, DEFAULT_USERNAME_KEY, username)
    except KeyringError as exc:
        raise CredentialError("Unable to store the password in the OS keyring.") from exc


def clear_credentials(username: str) -> None:
    """Remove the stored password and API token for a Whisker account."""
    for key in (username, _token_key(username)):
        try:
            keyring.delete_password(SERVICE, key)
        except KeyringError, PasswordDeleteError:
            continue
    try:
        if keyring.get_password(SERVICE, DEFAULT_USERNAME_KEY) == username:
            keyring.delete_password(SERVICE, DEFAULT_USERNAME_KEY)
    except KeyringError, PasswordDeleteError:
        pass


def load_token(username: str) -> dict[str, Any] | None:
    """Load a previously issued API token from the OS keyring."""
    try:
        raw = keyring.get_password(SERVICE, _token_key(username))
    except KeyringError:
        return None
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def save_token(username: str, token: dict[str, Any] | None) -> None:
    """Persist refreshed API tokens without writing them to project files."""
    key = _token_key(username)
    try:
        if token is None:
            keyring.delete_password(SERVICE, key)
        else:
            keyring.set_password(SERVICE, key, json.dumps(token))
    except KeyringError, PasswordDeleteError:
        return


def _token_key(username: str) -> str:
    return f"{username}:api-token"
