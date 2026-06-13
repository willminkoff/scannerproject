"""Per-block voice-activity scoring for analog audio.

Phase 1 prototype: lives outside the chirp daemon as a pure numpy module
so we can validate it offline against real audio captures before wiring
it into the GR flowgraph.

The scorer reads N samples at a time, returns a 0-100 voice-likeness
score plus the underlying metrics so we can correlate to ground truth.
"""
from __future__ import annotations

import numpy as np
from dataclasses import dataclass


@dataclass
class VADScore:
    rms_dbfs: float
    env_std_db: float
    spectral_flatness: float
    zcr_hz: float
    env_score: float
    flat_score: float
    zcr_score: float
    composite: float  # 0-100


class AudioVAD:
    """Voice-activity scorer with rolling envelope window.

    Call ``score(block)`` with one block of audio at a time.
    """

    BLOCK_SAMPLES_DEFAULT = 800  # 50 ms at 16 kHz

    def __init__(
        self,
        audio_rate: float = 16000.0,
        block_samples: int = BLOCK_SAMPLES_DEFAULT,
        env_window_blocks: int = 20,  # 1 s rolling window
    ):
        self.audio_rate = float(audio_rate)
        self.block_samples = int(block_samples)
        self.env_window_blocks = int(env_window_blocks)
        self._env_history: list[float] = []

    def score(self, block: np.ndarray) -> VADScore:
        if len(block) != self.block_samples:
            # accept variable-size blocks (compute scores at whatever
            # resolution caller provides). dont assert.
            pass
        # 1. RMS in dBFS
        rms = float(np.sqrt(np.mean(block.astype(np.float64) ** 2)) + 1e-12)
        rms_db = 20.0 * np.log10(rms + 1e-9)
        # 2. Rolling envelope std
        self._env_history.append(rms_db)
        if len(self._env_history) > self.env_window_blocks:
            self._env_history.pop(0)
        env_std = float(np.std(self._env_history)) if len(self._env_history) >= 4 else 0.0
        # 3. Spectral flatness (Wiener entropy)
        win = block * np.hamming(len(block))
        spec = np.abs(np.fft.rfft(win)) + 1e-9
        spec_mean = float(np.mean(spec))
        spec_geo = float(np.exp(np.mean(np.log(spec))))
        flatness = spec_geo / spec_mean
        # 4. Zero-crossing rate in Hz
        signs = np.sign(block)
        zc = float(np.sum(np.abs(np.diff(signs)))) / 2.0
        zcr_hz = zc * (self.audio_rate / len(block))

        # Composite voice score:
        # - env_score: 0 dB std => 0, 4 dB std => 100 (voice modulates 5-15 dB)
        env_score = max(0.0, min(100.0, (env_std - 0.5) * 30.0))
        # - flat_score: low flatness = structured = voice-like
        flat_score = max(0.0, min(100.0, (1.0 - flatness) * 150.0))
        # - zcr_score: voice has F0 80-300 Hz => harmonics drive ZCR into 200-3000 Hz range
        if 200.0 <= zcr_hz <= 2500.0:
            zcr_score = 80.0
        elif 100.0 <= zcr_hz < 200.0 or 2500.0 < zcr_hz <= 4000.0:
            zcr_score = 40.0
        else:
            zcr_score = 10.0
        # Weighted composite. Envelope is the strongest discriminator.
        composite = env_score * 0.5 + flat_score * 0.3 + zcr_score * 0.2

        return VADScore(
            rms_dbfs=round(rms_db, 1),
            env_std_db=round(env_std, 2),
            spectral_flatness=round(flatness, 4),
            zcr_hz=round(zcr_hz, 1),
            env_score=round(env_score, 0),
            flat_score=round(flat_score, 0),
            zcr_score=round(zcr_score, 0),
            composite=round(composite, 0),
        )
