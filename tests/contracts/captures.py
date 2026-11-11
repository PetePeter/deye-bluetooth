"""Reuse the raw fixtures already exercised by the model decode tests."""
from __future__ import annotations

from tests.fixtures.three_phase import decoded_words as three_phase_words
from tests.test_single_phase import CONFIG as SINGLE_PHASE_CONFIG
from tests.test_single_phase import TELEMETRY as SINGLE_PHASE_TELEMETRY
from tests.test_string import CONFIG as STRING_CONFIG
from tests.test_string import TELEMETRY as STRING_TELEMETRY


MODEL_CAPTURES = {
    "three_phase": three_phase_words(),
    "single_phase": {**SINGLE_PHASE_TELEMETRY, **SINGLE_PHASE_CONFIG},
    "string": {**STRING_TELEMETRY, **STRING_CONFIG},
}
