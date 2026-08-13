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
    assert "只暂停已成交 token/side" in body
    assert "只让单边 residual 进入离场逻辑" in body
    assert "对侧 reward BUY" in body and "本侧 maker SELL" in body
    assert "FOK 补对侧 + Merge" in body
    assert "默认 1" in body
    assert "默认 0.01 USD" in body
    assert "默认 0，即关闭" in body
    assert "POLY_1271 Deposit Wallet（Type3）" in body
    assert "Type1/Type2 仍可正常做市" in body
    assert "不提供网页自更新" in body


def test_help_requires_login():
    routes.app.config["TESTING"] = True
    with routes.app.test_client() as client:
        assert client.get("/help").status_code in (301, 302)
