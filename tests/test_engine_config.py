import asyncio

from backend.bot.engine import (
    EXECUTION_ALERT_ONLY,
    EXECUTION_LIVE,
    EXECUTION_PAPER,
    EXECUTION_VARIATIONAL_BROWSER,
    BotConfig,
    BotEngine,
)
from backend.bot.risk_manager import RiskConfig
from backend.bot.signal import MultiTFConfig


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


def test_runtime_config_reload_updates_signal_and_risk():
    bot = BotEngine(
        exchanges={},
        config=BotConfig(
            execution_mode=EXECUTION_VARIATIONAL_BROWSER,
            trading_mode="swing",
            signal_config=MultiTFConfig.swing(),
            risk_config=RiskConfig(take_profit_pct=0.8, stop_loss_pct=-3.0),
        ),
    )
    bot.signal_engine._spread_5m.extend([float(i) for i in range(20)])

    signal_cfg = MultiTFConfig.position()
    signal_cfg.entry_zscore = 2.8
    signal_cfg.divergence_threshold_pct = 1.2
    risk_cfg = RiskConfig(
        take_profit_pct=1.1,
        stop_loss_pct=-2.2,
        zscore_exit_min_pnl_pct=0.15,
    )

    asyncio.run(
        bot._apply_runtime_config(
            BotConfig(
                position_size_usd=250,
                leverage=2,
                execution_mode=EXECUTION_LIVE,
                primary_exchange="backpack",
                trading_mode="position",
                signal_config=signal_cfg,
                risk_config=risk_cfg,
            )
        )
    )

    assert bot.execution_mode == EXECUTION_VARIATIONAL_BROWSER
    assert bot.config.execution_mode == EXECUTION_VARIATIONAL_BROWSER
    assert bot.config.primary_exchange == "lighter"
    assert bot.config.position_size_usd == 250
    assert bot.config.leverage == 2
    assert bot.signal_engine.config.entry_zscore == 2.8
    assert bot.signal_engine.config.divergence_threshold_pct == 1.2
    assert bot.risk_manager.config.take_profit_pct == 1.1
    assert bot.risk_manager.config.stop_loss_pct == -2.2
    assert bot.risk_manager.config.zscore_exit_min_pnl_pct == 0.15
    assert not bot.risk_manager.config.averaging_enabled
    assert not bot.risk_manager.config.size_reduction_enabled
    assert list(bot.signal_engine._spread_5m)[-1] == 19.0
