from __future__ import annotations
import numpy as np
import pandas as pd


def rw_kalman(prices, frac_period=0.5, tune_len=30, damping=0.08):
    """
    Random Walk Kalman. Measurement = fractional EMA.
    Returns: states (1D array), gains (1D array), innovations (1D array)
    """
    alpha = min(2.0 / (frac_period + 1.0), 1.0)
    state = float(prices[0]); P = 2.0; fema_v = float(prices[0])
    residuals, innovations, states, gains = [], [], [], []
    for price in prices:
        fema_v = fema_v + alpha * (price - fema_v)
        residuals.append(price - state)
        innovations.append(fema_v - state)
        if len(residuals) >= tune_len:
            R = max(float(np.var(residuals[-tune_len:])), 1e-6)
            Q = max(float(np.var(innovations[-tune_len:])) * damping, 1e-8)
        else:
            R, Q = 2.0, 0.1
        K = P / (P + R) if (P + R) > 0 else 0.0
        state = state + K * (fema_v - state)
        P = (1.0 - K) * P + Q
        states.append(state); gains.append(K)
    return np.array(states), np.array(gains), np.array(residuals)


def cv_kalman(prices, phi=0.9, tune_len=30, q_p=None, q_v=None):
    """
    Constant Velocity Kalman. State = [price, velocity].
    F = [[1, 1], [0, phi]]. H = [1, 0]. Self-tunes R from residuals.
    Returns: states (Nx2), gains (Nx2), innovations (1D array)
    """
    n = len(prices)
    F = np.array([[1.0, 1.0], [0.0, phi]])
    H = np.array([[1.0, 0.0]])
    s = np.array([float(prices[0]), 0.0])
    P = np.eye(2) * 2.0
    R = np.array([[2.0]])
    residuals = []
    states = np.zeros((n, 2))
    gains  = np.zeros((n, 2))
    innov  = np.zeros(n)

    for i, price in enumerate(prices):
        z = np.array([[float(price)]])
        s_pred = F @ s
        P_pred = F @ P @ F.T
        if i >= tune_len:
            vel_var = max(float(np.var(states[max(0,i-tune_len):i, 1])), 1e-8)
            prc_var = max(float(np.var(prices[max(0,i-tune_len):i])) * 0.001, 1e-8)
        else:
            vel_var, prc_var = 0.001, 0.01
        Q_dyn = np.diag([prc_var, vel_var])
        P_pred = P_pred + Q_dyn
        y = z - H @ s_pred
        S = H @ P_pred @ H.T + R
        K = P_pred @ H.T @ np.linalg.inv(S)
        s = s_pred + (K @ y).flatten()
        P = (np.eye(2) - K @ H) @ P_pred
        residuals.append(float(y[0, 0]))
        if len(residuals) >= tune_len:
            R[0, 0] = max(float(np.var(residuals[-tune_len:])), 1e-6)
        states[i] = s
        gains[i]  = K.flatten()
        innov[i]  = float(y[0, 0])

    return states, gains, innov


def extract_signals(prices, kalman_line, gain, innovations, velocity=None, tune_len=30, slope_len=5):
    n = len(prices)
    prices = np.asarray(prices, dtype=float)

    slope = np.zeros(n)
    for i in range(slope_len, n):
        slope[i] = kalman_line[i] - kalman_line[i - slope_len]

    innov_std = np.array([
        max(np.std(innovations[max(0, i-tune_len):i+1]), 1e-8) if i > 0 else 1.0
        for i in range(n)
    ])
    z_score = innovations / innov_std

    gain_scalar = gain if gain.ndim == 1 else gain[:, 0]
    gain_thresh = np.percentile(gain_scalar[tune_len:], 40) if n > tune_len else 0.1

    cross_signals = np.zeros(n, dtype=int)
    prev_above = prices[0] > kalman_line[0]
    for i in range(tune_len + 1, n):
        above = prices[i] > kalman_line[i]
        if above and not prev_above and slope[i] > 0:
            cross_signals[i] = 1
        elif not above and prev_above and slope[i] < 0:
            cross_signals[i] = -1
        prev_above = above

    vel_signals = np.zeros(n, dtype=int)
    if velocity is not None:
        for i in range(1, n):
            if velocity[i-1] <= 0 and velocity[i] > 0:
                vel_signals[i] = 1
            elif velocity[i-1] >= 0 and velocity[i] < 0:
                vel_signals[i] = -1

    last = -1
    cur_bias = 'bullish' if slope[last] > 0 else ('bearish' if slope[last] < 0 else 'neutral')
    cur_regime = 'trending' if gain_scalar[last] > gain_thresh else 'ranging'
    cur_z = float(z_score[last])
    stretched = 'long' if cur_z > 2 else ('short' if cur_z < -2 else None)

    cross_idxs = np.where(cross_signals != 0)[0]
    last_cross_idx   = int(cross_idxs[-1])  if len(cross_idxs) else None
    last_cross_sig   = int(cross_signals[last_cross_idx]) if last_cross_idx is not None else None
    last_cross_age   = int(n - 1 - last_cross_idx) if last_cross_idx is not None else None
    last_cross_price = float(prices[last_cross_idx]) if last_cross_idx is not None else None

    vel_idxs = np.where(vel_signals != 0)[0]
    last_vel_idx = int(vel_idxs[-1])     if len(vel_idxs) else None
    last_vel_sig = int(vel_signals[last_vel_idx]) if last_vel_idx is not None else None
    last_vel_age = int(n - 1 - last_vel_idx) if last_vel_idx is not None else None

    return {
        'kalman_line':   kalman_line.tolist(),
        'velocity':      velocity.tolist() if velocity is not None else None,
        'gain':          gain_scalar.tolist(),
        'z_score':       z_score.tolist(),
        'slope':         slope.tolist(),
        'cross_signals': cross_signals.tolist(),
        'vel_signals':   vel_signals.tolist() if velocity is not None else None,
        'bias':          cur_bias,
        'regime':        cur_regime,
        'z_now':         round(cur_z, 3),
        'stretched':     stretched,
        'gain_now':      round(float(gain_scalar[-1]), 4),
        'signal':        'buy' if last_cross_sig == 1 else ('sell' if last_cross_sig == -1 else None),
        'signal_price':  last_cross_price,
        'signal_age_bars': last_cross_age,
        'vel_signal':    'buy' if last_vel_sig == 1 else ('sell' if last_vel_sig == -1 else None),
        'vel_signal_age_bars': last_vel_age,
        'vel_lead_bars': None,
    }


def compute_both(df, timeframe='1d'):
    """
    Run both RW and CV on a OHLCV DataFrame.
    df must have columns: Close, High, Low (any case).
    Handles MultiIndex columns from yfinance single-ticker downloads.
    """
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]
    else:
        df.columns = [c.lower() if isinstance(c, str) else str(c[0]).lower() for c in df.columns]
    closes  = df['close'].values.astype(float)
    highs   = df['high'].values.astype(float)
    lows    = df['low'].values.astype(float)
    typical = (highs + lows + closes) / 3.0

    input_rw = typical if timeframe in ('1h', '15m', '30m') else closes

    rw_states, rw_gains, rw_innov = rw_kalman(input_rw)
    cv_states, cv_gains, cv_innov = cv_kalman(closes)

    rw_sig = extract_signals(closes, rw_states, rw_gains, rw_innov)
    cv_sig = extract_signals(closes, cv_states[:, 0], cv_gains, cv_innov,
                             velocity=cv_states[:, 1])

    if cv_sig['vel_signal_age_bars'] is not None and cv_sig['signal_age_bars'] is not None:
        cv_sig['vel_lead_bars'] = cv_sig['signal_age_bars'] - cv_sig['vel_signal_age_bars']

    return {'rw': rw_sig, 'cv': cv_sig, 'n_bars': len(closes)}
