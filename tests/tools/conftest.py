"""Shared fixtures for tools/analysis test suite."""
import numpy as np
import pandas as pd
import pytest


def _make_ohlcv(n=300, start=100.0, trend=0.0, seed=42, freq="B"):
    """Deterministic synthetic OHLCV frame, optionally trending, for indicator tests."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(loc=trend, scale=1.0, size=n)
    close = start + np.cumsum(steps)
    close = np.clip(close, 1.0, None)
    high = close + np.abs(rng.normal(0.5, 0.2, n))
    low = close - np.abs(rng.normal(0.5, 0.2, n))
    open_ = close + rng.normal(0, 0.2, n)
    volume = rng.integers(100_000, 1_000_000, n).astype(float)

    idx = pd.date_range("2024-01-01", periods=n, freq=freq)
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume},
        index=idx,
    )


@pytest.fixture
def ohlcv_df():
    return _make_ohlcv()


@pytest.fixture
def uptrend_df():
    return _make_ohlcv(trend=0.3, seed=1)


@pytest.fixture
def downtrend_df():
    return _make_ohlcv(trend=-0.3, seed=2)


@pytest.fixture
def make_ohlcv():
    return _make_ohlcv
