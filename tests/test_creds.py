"""Credential resolution: env first, then the YAML file, per field, no prompts."""

from pathlib import Path

import pytest

from jenkins_preview.creds import credentials_path, resolve
from jenkins_preview.errors import Fail


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """No ambient credentials: a developer's real env or file must not leak in."""
    for env in ("JENKINS_URL", "JENKINS_USER", "JENKINS_TOKEN", "JENKINS_SERVER"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


def write_file(tmp_path, text: str) -> Path:
    path = tmp_path / "xdg" / "jenkins-preview" / "credentials.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)
    return path


ONE_SERVER = """
servers:
  ps3:
    url: https://ps3.example.com
    user: alice
    token: filetoken
"""

TWO_SERVERS = """
current: ps3
servers:
  ps3: {url: "https://ps3.example.com", user: alice, token: pstoken}
  pxb: {url: "https://pxb.example.com", user: alice, token: pxbtoken}
"""


def set_env(monkeypatch, url="https://env.example.com", user="envuser", token="envtoken"):
    monkeypatch.setenv("JENKINS_URL", url)
    monkeypatch.setenv("JENKINS_USER", user)
    monkeypatch.setenv("JENKINS_TOKEN", token)


def test_env_alone_resolves(monkeypatch):
    set_env(monkeypatch)
    values, sources = resolve()
    assert values == {"url": "https://env.example.com", "user": "envuser", "token": "envtoken"}
    assert set(sources.values()) == {"env"}


def test_file_alone_resolves(tmp_path):
    write_file(tmp_path, ONE_SERVER)
    values, sources = resolve()
    assert values["url"] == "https://ps3.example.com"
    assert values["token"] == "filetoken"
    assert sources["token"] == "credentials.yaml (ps3)"


def test_each_env_field_beats_its_file_counterpart(tmp_path, monkeypatch):
    """url and user can live in the file while the token rides the env."""
    write_file(tmp_path, ONE_SERVER)
    monkeypatch.setenv("JENKINS_TOKEN", "envtoken")
    values, sources = resolve()
    assert values["token"] == "envtoken"
    assert sources["token"] == "env"
    assert values["url"] == "https://ps3.example.com"
    assert sources["url"] == "credentials.yaml (ps3)"


def test_current_picks_the_server(tmp_path):
    write_file(tmp_path, TWO_SERVERS)
    values, _ = resolve()
    assert values["url"] == "https://ps3.example.com"


def test_jenkins_server_env_overrides_current(tmp_path, monkeypatch):
    write_file(tmp_path, TWO_SERVERS)
    monkeypatch.setenv("JENKINS_SERVER", "pxb")
    values, sources = resolve()
    assert values["url"] == "https://pxb.example.com"
    assert sources["token"] == "credentials.yaml (pxb)"


def test_a_single_server_needs_no_current(tmp_path):
    write_file(tmp_path, ONE_SERVER)
    assert resolve()[0]["url"] == "https://ps3.example.com"


def test_many_servers_without_current_die_listing_them(tmp_path):
    write_file(tmp_path, TWO_SERVERS.replace("current: ps3\n", ""))
    with pytest.raises(Fail, match="no 'current'") as caught:
        resolve()
    assert "ps3" in str(caught.value) and "pxb" in str(caught.value)


def test_an_unknown_server_dies_listing_the_known(tmp_path, monkeypatch):
    write_file(tmp_path, TWO_SERVERS)
    monkeypatch.setenv("JENKINS_SERVER", "nope")
    with pytest.raises(Fail, match="'nope' is not in"):
        resolve()


def test_a_group_readable_file_is_refused(tmp_path):
    path = write_file(tmp_path, ONE_SERVER)
    path.chmod(0o640)
    with pytest.raises(Fail, match="readable by group or others"):
        resolve()


def test_broken_yaml_dies_with_the_path(tmp_path):
    write_file(tmp_path, "servers: [broken")
    with pytest.raises(Fail, match="not valid YAML"):
        resolve()


def test_a_file_without_servers_is_refused(tmp_path):
    write_file(tmp_path, "current: ps3\n")
    with pytest.raises(Fail, match="at least one named server"):
        resolve()


def test_a_missing_field_names_both_fixes(tmp_path):
    write_file(tmp_path, "servers:\n  ps3: {url: 'https://x', user: alice}\n")
    with pytest.raises(Fail, match="JENKINS_TOKEN is not set") as caught:
        resolve()
    assert str(credentials_path()) in str(caught.value)


def test_a_marker_breaking_user_is_refused(monkeypatch):
    set_env(monkeypatch, user="alice bob")
    with pytest.raises(Fail, match="must not contain whitespace"):
        resolve()


def test_lenient_mode_reports_instead_of_dying(tmp_path):
    values, sources = resolve(require=False)
    assert values == {"url": None, "user": None, "token": None}
    assert set(sources.values()) == {"missing"}
