from pathlib import Path

from jinja2 import Environment
import pytest

from india_states import TALLY_STATES


ROOT = Path(__file__).resolve().parents[1]
ENV = Environment()


def render(fragment, **context):
    return ENV.from_string(fragment).render(**context)


def checkout_state_field():
    source = (ROOT / "templates" / "checkout.html").read_text()
    return source.split('<label for="ck_state">State</label>', 1)[1].split(
        '<small class="owNcrHint"', 1)[0]


def profile_state_field(element_id):
    source = (ROOT / "templates" / "profile.html").read_text()
    zipcode_id = element_id.replace("-state", "-zip")
    start = source.index('<label>State</label>', source.index(f'id="{zipcode_id}"'))
    marker = "</div>\n        </div>" if element_id.startswith("new-") else "</div>\n    </div>"
    end = source.index(marker, start)
    return source[start:end]


def test_checkout_state_select_is_canonical_and_required_on_india():
    rendered = render(
        checkout_state_field(),
        is_india=True,
        india_states=TALLY_STATES,
        prefill_state="Chhattisgarh",
        prefill={"state": "Chattisgarh"},
    )
    assert '<select id="ck_state" name="customer_state" required>' in rendered
    assert 'value="Chhattisgarh" selected' in rendered
    assert rendered.count("<option value=") == 37


def test_checkout_keeps_region_text_input_on_global_site():
    rendered = render(
        checkout_state_field(),
        is_india=False,
        india_states=TALLY_STATES,
        prefill_state="",
        prefill={"state": "Île-de-France"},
    )
    assert '<input type="text" id="ck_state" name="customer_state"' in rendered
    assert "Île-de-France" in rendered
    assert "<select" not in rendered


@pytest.mark.parametrize("element_id", ("new-addr-state", "edit-addr-state"))
def test_profile_address_fields_switch_only_for_india(element_id):
    fragment = profile_state_field(element_id)
    india = render(fragment, is_india=True, india_states=TALLY_STATES)
    global_site = render(fragment, is_india=False, india_states=TALLY_STATES)
    assert f'<select name="state" id="{element_id}" required>' in india
    assert india.count("<option value=") == 37
    assert '<input type="text" name="state"' in global_site
    assert "<select" not in global_site


def test_checkout_and_profile_autofill_use_python_lookup_json():
    checkout = (ROOT / "templates" / "checkout.html").read_text()
    profile = (ROOT / "templates" / "profile.html").read_text()
    for source in (checkout, profile):
        assert "var INDIA_STATE_LOOKUP = {{ india_state_lookup|tojson }};" in source
        assert "function canonicalState(value)" in source
        assert "field.tagName === 'SELECT' ? canonicalState(value)" in source
    assert "setStateValue('ck_state', radio.dataset.state || '')" in checkout
    assert "setStateValue('new-addr-state', data[0].PostOffice[0].State)" in profile
    assert "if (stateId) setStateValue(stateId, state)" in profile
