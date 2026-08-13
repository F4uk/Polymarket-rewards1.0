"""UI contract for the Merge / Relayer authorization area on the config page."""

import web.routes as routes


def _client_logged_in():
    routes.app.config["TESTING"] = True
    client = routes.app.test_client()
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    return client


def _config_html():
    return _client_logged_in().get("/config").get_data(as_text=True)


def test_config_page_has_relayer_section():
    html = _config_html()
    assert "Merge / Relayer 授权" in html
    assert "id=\"relayer-config-section\"" in html


def test_credential_fields_are_password_type():
    html = _config_html()
    for field in ("relayer-api-key", "relayer-secret", "relayer-passphrase"):
        assert f'<input type="password" id="{field}"' in html


def test_no_saved_secret_injected_into_html():
    html = _config_html()
    for needle in (
        "test-builder-key",
        "test-secret-not-real",
        "test-passphrase-not-real",
    ):
        # Server-rendered HTML must never contain a saved secret.
        assert needle not in html


def test_saved_marker_is_js_driven_only():
    html = _config_html()
    # The masked marker is produced client-side after /api/relayer-config
    # (which never returns secrets); it is not server-rendered as a value.
    assert "•••••• 已保存" in html  # JS template string exists
    assert 'value="•••••• 已保存"' not in html


def test_save_test_delete_controls_present():
    html = _config_html()
    assert "saveRelayerConfig()" in html
    assert "retestRelayerConfig()" in html
    assert "deleteRelayerConfig()" in html
    assert "保存并测试" in html
    assert "重新测试" in html
    assert "删除本地凭据" in html


def test_status_and_source_areas_present():
    html = _config_html()
    assert 'id="relayer-status"' in html
    assert 'id="relayer-source"' in html
    assert 'id="relayer-test-results"' in html
    assert 'id="relayer-url-display"' in html


def test_wallet_table_distinguishes_merge_states():
    html = _config_html()
    assert "'Merge 已关闭（模板）'" in html
    assert "'Merge 可用'" in html
    assert "'Merge 不可用 · '" in html
    assert "Deposit Wallet 尚未部署，需要单独初始化" in html


def test_ui_explains_first_time_only():
    html = _config_html()
    assert "配置一次" in html
    assert "polymarket.com/settings" in html
