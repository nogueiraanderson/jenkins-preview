"""The client's wiring: gates fire before any network I/O, ambiguous reads refuse.

The gate predicates have unit tests in test_gates.py. These prove the wiring
around them: `_request` consults a gate for every write BEFORE opening a
connection, and the read helpers treat unexpected statuses as refusals rather
than as absence. A gate that runs after the connection opens is no gate.
"""

import pytest

from jenkins_preview.client import Jenkins
from jenkins_preview.errors import Fail


class _Response:
    def __init__(self, status: int, body: str = "") -> None:
        self.status = status
        self._body = body.encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class RecordingOpener:
    """Serves scripted (status, body) responses and records every URL opened."""

    def __init__(self, *responses: tuple[int, str]) -> None:
        self.responses = list(responses)
        self.opened: list[str] = []

    def open(self, request, timeout=None) -> _Response:
        self.opened.append(request.full_url)
        status, body = self.responses.pop(0)
        return _Response(status, body)


@pytest.fixture
def opener() -> RecordingOpener:
    return RecordingOpener((200, "{}"))


@pytest.fixture
def jenkins(opener: RecordingOpener) -> Jenkins:
    client = Jenkins("https://jenkins.test", "alice", "token")
    client._opener = opener
    return client


def test_a_write_outside_previews_never_reaches_the_wire(jenkins, opener) -> None:
    with pytest.raises(Fail, match="outside the previews folder"):
        jenkins.post("/job/production/doDelete")
    assert opener.opened == [], "the gate must fire before any connection opens"


def test_a_view_write_with_an_unsafe_name_never_reaches_the_wire(jenkins, opener) -> None:
    with pytest.raises(Fail, match="unsafe view name"):
        jenkins.delete_view("a|b")
    assert opener.opened == []


def test_a_gated_write_inside_previews_reaches_the_wire(jenkins, opener) -> None:
    status, _ = jenkins.post("/job/previews/createItem?name=preview-alice-x")
    assert status == 200
    assert opener.opened == ["https://jenkins.test/job/previews/createItem?name=preview-alice-x"]


def test_a_read_is_not_gated(jenkins, opener) -> None:
    jenkins.get_text("/job/anything-at-all/config.xml")
    assert len(opener.opened) == 1


def test_get_json_treats_404_as_absence(jenkins, opener) -> None:
    opener.responses = [(404, "")]
    assert jenkins.get_json("/job/previews/job/gone") == {}


def test_exists_fails_closed_on_403(jenkins, opener) -> None:
    """A 403 must refuse, not report absence: rollback would otherwise claim a
    resource removed while it is still there."""
    opener.responses = [(403, "")]
    with pytest.raises(Fail, match="403"):
        jenkins.exists("/job/previews/job/preview-alice-x")


def test_view_description_fails_closed_on_500(jenkins, opener) -> None:
    opener.responses = [(500, "")]
    with pytest.raises(Fail, match="HTTP 500"):
        jenkins.view_description("preview-alice-x")


def test_expect_names_the_proxy_on_a_redirect(jenkins) -> None:
    with pytest.raises(Fail, match="authentication proxy"):
        jenkins.expect(302, "/job/previews/createItem", (200,))


def test_main_refuses_every_credential_flag(capsys) -> None:
    """Gate G10's CLI half: a credential flag dies before argparse ever runs."""
    from jenkins_preview.cli import CREDENTIAL_FLAGS, main

    for flag in CREDENTIAL_FLAGS:
        for argv in ([flag, "x", "list"], [f"{flag}=x", "list"]):
            assert main(argv) == 2
            assert "refusing a credential on the command line" in capsys.readouterr().err


def test_credentials_refuse_a_marker_breaking_user_id(monkeypatch, capsys) -> None:
    """The ownership marker is whitespace-delimited key=value text. A user id
    carrying whitespace or '=' could displace marker fields (owner=x id=evil),
    silently defeating rollback, so it is refused at credential load."""
    from jenkins_preview.cli import credentials
    from jenkins_preview.errors import Fail as FailError

    monkeypatch.setenv("JENKINS_URL", "https://jenkins.test")
    monkeypatch.setenv("JENKINS_TOKEN", "token")
    for user in ("Satya Bodapati", "eve id=deadbeef", "tab\tuser"):
        monkeypatch.setenv("JENKINS_USER", user)
        with pytest.raises(FailError, match="whitespace or '='"):
            credentials()


class _RaisingOpener:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def open(self, request, timeout=None):
        raise self.exc


def test_a_mid_body_timeout_becomes_a_refusal_not_a_traceback() -> None:
    client = Jenkins("https://jenkins.test", "alice", "token")
    client._opener = _RaisingOpener(TimeoutError())
    with pytest.raises(Fail, match="timed out"):
        client.get_text("/job/previews/job/x/config.xml")


def test_a_200_with_an_html_body_names_the_proxy(jenkins, opener) -> None:
    opener.responses = [(200, "<html>SSO login</html>")]
    with pytest.raises(Fail, match="non-JSON body"):
        jenkins.get_json("/job/previews/job/x")
