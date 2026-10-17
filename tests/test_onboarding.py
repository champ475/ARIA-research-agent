"""UX-01: the first-run card names three capabilities, the main limit, the
trial count and Telegram; example chips cover the domains."""

from tests.conftest import import_app_without_side_effects
from ui import chat as chat_ui

app = import_app_without_side_effects()


def test_card_for_a_new_trial_user():
    card = chat_ui.onboarding_card({"used_today": 2, "byo": False, "telegram": False})
    assert card.count("\n1. ") == 1 and "\n2. " in card and "\n3. " in card
    assert "doesn't give personal buy/sell advice" in card
    assert "**3 of 5** free questions left today" in card
    assert "Connect Telegram" in card


def test_card_for_a_byo_user_with_telegram():
    card = chat_ui.onboarding_card({"used_today": 0, "byo": True, "telegram": True})
    assert "no daily limit" in card and "Telegram is connected" in card


def test_chips_and_capabilities_cover_every_domain():
    chips = " ".join(chat_ui.CHAT_DEFAULTS).lower()
    assert "analyse" in chips and "alert" in chips and "elss" in chips and "plan" in chips
    assert "can't" in chat_ui.CAPABILITIES_MD and "SEBI" in chat_ui.CAPABILITIES_MD
