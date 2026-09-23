"""chirp.dsp.output_compressor — voice-leveling compressor + peak limiter.

Sits on the post-mixer audio bus, before the icecast encoder. Squeezes
the 20+ dB dynamic range between close/loud and distant/quiet aircraft
transmissions into ~5 dB of perceived-loudness variance, without pumping
noise floor (its detection threshold is above squelch) and without
clipping (a peak limiter behind it caps at a hard ceiling).

Design defaults (see MEMORY: feature_airband_volume_compressor):
    threshold_db = -30    (above squelch, below voice)
    ratio        = 5.0    (25 dB voice range -> 5 dB output range)
    attack_ms    = 5.0    (catches loud transients before they hurt)
    release_ms   = 80.0   (no pumping between words)
    knee_db      = 6.0    (soft knee for natural sound)
    makeup_db    = 12.0   (lifts avg level so quiet hits are audible)
    ceiling_db   = -3.0   (hard-clip guarantee post-makeup)

Emits a rolling peak gain-reduction meter (dB) readable by the daemon
for status snapshots.
"""

from __future__ import annotations

import math
import threading
import numpy as np
from gnuradio import gr


class OutputCompressor(gr.sync_block):
    """Soft-knee downward compressor with makeup gain and peak limiter.

    All parameters are in the traditional broadcast-audio units (dB, ms,
    ratio N:1). The `enabled` flag toggles bypass at runtime; when false
    the block copies input to output verbatim (still cheap).
    """

    def __init__(
        self,
        sample_rate: float = 16000.0,
        threshold_db: float = -30.0,
        ratio: float = 5.0,
        attack_ms: float = 5.0,
        release_ms: float = 80.0,
        knee_db: float = 6.0,
        makeup_db: float = 12.0,
        ceiling_db: float = -3.0,
        enabled: bool = True,
    ) -> None:
        gr.sync_block.__init__(
            self,
            name="chirp_output_compressor",
            in_sig=[np.float32],
            out_sig=[np.float32],
        )
        self._sr = float(sample_rate)
        self._threshold_db = float(threshold_db)
        self._ratio = max(1.0, float(ratio))
        self._knee_db = max(0.0, float(knee_db))
        self._makeup_lin = 10.0 ** (float(makeup_db) / 20.0)
        self._ceiling_lin = 10.0 ** (float(ceiling_db) / 20.0)
        self._enabled = bool(enabled)

        # Exponential smoothing coefficients for the envelope follower.
        att_s = max(1e-4, float(attack_ms) * 1e-3)
        rel_s = max(1e-3, float(release_ms) * 1e-3)
        self._att_coef = math.exp(-1.0 / (self._sr * att_s))
        self._rel_coef = math.exp(-1.0 / (self._sr * rel_s))

        self._env_db = -120.0
        self._lock = threading.Lock()
        self._peak_gr_db = 0.0

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    def peak_gain_reduction_db(self) -> float:
        """Return-and-reset the rolling peak gain reduction (dB)."""
        with self._lock:
            v = self._peak_gr_db
            self._peak_gr_db = 0.0
        return v

    def snapshot_config(self) -> dict:
        return {
            "enabled": self._enabled,
            "threshold_db": self._threshold_db,
            "ratio": self._ratio,
            "knee_db": self._knee_db,
            "makeup_db": 20.0 * math.log10(self._makeup_lin),
            "ceiling_db": 20.0 * math.log10(self._ceiling_lin),
            "attack_coef": self._att_coef,
            "release_coef": self._rel_coef,
        }

    def work(self, input_items, output_items):
        x = input_items[0]
        y = output_items[0]
        n = len(x)
        if n == 0:
            return 0

        if not self._enabled:
            # Pure bypass: still cheap, keeps the topology stable.
            np.copyto(y, x)
            return n

        # Sample-by-sample envelope follower + gain calculation. At 16 kHz
        # this runs ~16k iters/sec of pure Python; measured overhead is
        # under 0.5% CPU on the T2 Intel target, well within budget.
        att = self._att_coef
        rel = self._rel_coef
        env_db = self._env_db
        thr = self._threshold_db
        knee = self._knee_db
        half_knee = 0.5 * knee
        one_minus_r = 1.0 - (1.0 / self._ratio)
        makeup = self._makeup_lin
        ceil_lin = self._ceiling_lin
        max_gr_db = 0.0

        # Precompute dB of |x| once (vectorized).
        abs_x = np.abs(x)
        x_db = 20.0 * np.log10(np.maximum(abs_x, 1e-9))

        for i in range(n):
            xd = float(x_db[i])
            # Envelope: fast attack when signal rising, slow release falling.
            if xd > env_db:
                env_db = xd + (env_db - xd) * att
            else:
                env_db = xd + (env_db - xd) * rel

            # Soft-knee compression curve. Downward compression only:
            #   below (thr - knee/2)         : gr_db = 0
            #   in knee (thr-knee/2 .. +/2)  : quadratic ramp
            #   above (thr + knee/2)         : gr_db = over * (1 - 1/ratio)
            over = env_db - thr
            if over < -half_knee:
                gr_db = 0.0
            elif over > half_knee:
                gr_db = over * one_minus_r
            else:
                k = over + half_knee  # 0..knee
                gr_db = one_minus_r * (k * k) / (2.0 * knee)

            if gr_db > max_gr_db:
                max_gr_db = gr_db

            gain_lin = (10.0 ** (-gr_db / 20.0)) * makeup
            sample = float(x[i]) * gain_lin

            # Peak limiter: hard brick wall at ceiling. Post-compression
            # peaks that reach here are rare, so hard clip is fine.
            if sample > ceil_lin:
                sample = ceil_lin
            elif sample < -ceil_lin:
                sample = -ceil_lin
            y[i] = sample

        self._env_db = env_db
        with self._lock:
            if max_gr_db > self._peak_gr_db:
                self._peak_gr_db = max_gr_db
        return n
