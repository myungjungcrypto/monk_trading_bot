from backend.bot.engine import (
    EXECUTION_ALERT_ONLY,
    EXECUTION_LIVE,
    EXECUTION_PAPER,
    EXECUTION_VARIATIONAL_BROWSER,
    BotConfig,
    BotEngine,
)


def test_variational_browser_execution_mode_is_virtual():
    bot = BotEngine(
        exchanges={},
        config=BotConfig(execution_mode=EXECUTION_VARIATIONAL_BROWSER),
    )

    assert bot.execution_mode == EXECUTION_VARIATIONAL_BROWSER
    assert bot._uses_virtual_positions
    assert bot._virtual_exchange_name == "variational_browser"
    assert bot.variational_bridge is not None


def test_known_execution_modes_are_preserved():
    assert BotEngine._normalize_execution_mode(EXECUTION_ALERT_ONLY, False) == EXECUTION_ALERT_ONLY
    assert BotEngine._normalize_execution_mode(EXECUTION_PAPER, False) == EXECUTION_PAPER
    assert BotEngine._normalize_execution_mode(EXECUTION_LIVE, False) == EXECUTION_LIVE
    assert BotEngine._normalize_execution_mode(EXECUTION_VARIATIONAL_BROWSER, False) == EXECUTION_VARIATIONAL_BROWSER
