"""Static contract for config-page Merge controls.

API round-trip tests prove persistence.  These assertions pin the browser-side
load/save wiring so a future template edit cannot silently drop the checkbox or
turn an empty numeric field into JSON null again.
"""

import pytest

from web import routes


@pytest.fixture
def config_html():
    routes.app.config["TESTING"] = True
    with routes.app.test_client() as client:
        with client.session_transaction() as session:
            session["logged_in"] = True
        response = client.get("/config")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_merge_checkbox_load_and_save_wiring(config_html):
    assert 'type="checkbox" name="merge_enabled"' in config_html
    assert "input.checked = !!data[key]" in config_html
    assert 'data.merge_enabled = !!(mergeEnabled && mergeEnabled.checked)' in config_html


@pytest.mark.parametrize(
    "name",
    ["merge_min_shares", "merge_advantage_min_usd"],
)
def test_merge_numeric_controls_are_required_non_negative_and_finite(
    config_html, name
):
    assert f'name="{name}"' in config_html
    field = config_html.split(f'name="{name}"', 1)[1].split(">", 1)[0]
    assert 'min="0"' in field
    assert "required" in field
    assert "Number.isFinite(data[key])" in config_html
    assert "data[key] < 0" in config_html
