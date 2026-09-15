"""
Feature engineering for the anomaly detector.

Core idea: a $4B protocol and a $1B protocol are both "normal" at their own
scale. What signals anomaly is DEVIATION from a protocol's own recent
history — so every feature here is a rolling z-score or percent-change,
computed per protocol, never compared across protocols directly.
"""

import pandas as pd
import numpy as np

ROLLING_WINDOW = 24  # hours — trailing day as the "baseline" for z-scores


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    df: one protocol's snapshots, sorted oldest->newest, with columns:
        tvl_usd, volume_24h_usd, tx_count_24h, unique_wallets_24h,
        liquidity_depth, utilization_rate, snapshot_at
    Returns df with added feature columns. Rows without a full rolling
    window (the first ROLLING_WINDOW hours) are dropped — there's no
    reliable baseline yet for those.
    """
    df = df.sort_values("snapshot_at").reset_index(drop=True)

    def zscore(series: pd.Series) -> pd.Series:
        roll_mean = series.rolling(ROLLING_WINDOW).mean()
        roll_std = series.rolling(ROLLING_WINDOW).std().replace(0, np.nan)
        return (series - roll_mean) / roll_std

    df["tvl_pct_change_1h"] = df["tvl_usd"].pct_change()
    df["tvl_zscore_24h"] = zscore(df["tvl_usd"])

    df["volume_zscore_24h"] = zscore(df["volume_24h_usd"])
    df["volume_pct_change_1h"] = df["volume_24h_usd"].pct_change()

    df["tx_zscore_24h"] = zscore(df["tx_count_24h"])
    df["wallets_zscore_24h"] = zscore(df["unique_wallets_24h"])

    df["liquidity_pct_change_1h"] = df["liquidity_depth"].pct_change()
    df["liquidity_zscore_24h"] = zscore(df["liquidity_depth"])

    # volume/liquidity ratio — our proxy for price-deviation-style pressure
    df["vol_liq_ratio"] = df["volume_24h_usd"] / df["liquidity_depth"].replace(0, np.nan)
    df["vol_liq_ratio_zscore_24h"] = zscore(df["vol_liq_ratio"])

    # tx-per-wallet — spikes when a few whales dominate, drops when volume
    # is spread across many actors (helps distinguish WHALE vs TX_SPIKE)
    df["tx_per_wallet"] = df["tx_count_24h"] / df["unique_wallets_24h"].replace(0, np.nan)
    df["tx_per_wallet_zscore_24h"] = zscore(df["tx_per_wallet"])

    if (df["category"] == "LENDING").any():
        df["utilization_change_1h"] = df["utilization_rate"].diff()
    else:
        df["utilization_change_1h"] = 0.0

    df = df.iloc[ROLLING_WINDOW:].reset_index(drop=True)  # drop rows without full baseline
    df = df.fillna(0.0)
    return df


FEATURE_COLUMNS = [
    "tvl_pct_change_1h", "tvl_zscore_24h",
    "volume_zscore_24h", "volume_pct_change_1h",
    "tx_zscore_24h", "wallets_zscore_24h",
    "liquidity_pct_change_1h", "liquidity_zscore_24h",
    "vol_liq_ratio_zscore_24h",
    "tx_per_wallet_zscore_24h",
    "utilization_change_1h",
]
