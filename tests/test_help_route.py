"""The user guide stays behind the normal authentication gate."""

import pytest

from web import routes


@pytest.fixture
def client():
    routes.app.config["TESTING"] = True
    with routes.app.test_client() as test_client:
        with test_client.session_transaction() as session:
            session["logged_in"] = True
        yield test_client


def test_help_page_renders_merge_first_guidance(client):
    response = client.get("/help")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Merge 安全边界" in body
    assert "不提供网页自更新" in body


def test_help_requires_login():
    routes.app.config["TESTING"] = True
    with routes.app.test_client() as client:
        assert client.get("/help").status_code in (301, 302)
