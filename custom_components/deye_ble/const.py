"""Constants for the Deye BLE integration."""
from __future__ import annotations

DOMAIN = "deye_ble"

CONF_LOGGER_SN = "logger_sn"
CONF_ADDRESS = "address"
CONF_DRY_RUN = "dry_run"
CONF_REASSERT = "reassert"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_KEEPALIVE = "keepalive"

DEFAULT_DRY_RUN = True   # writes blocked until user explicitly enables
DEFAULT_REASSERT = False  # local-wins reassert is opt-in
# Persistent BLE connection: reuse one link instead of reconnecting per poll.
# ON for a crisp 5 s cadence (a reconnect costs ~3.3 s, stretching the cycle to
# ~8.3 s). The inverted logger shares one processor between BLE and its Wi-Fi
# cloud upload, and a permanently held link starved the cloud feed on
# 2026-10-26 16:24:57 — so the Cloud Window below must give it a real gap.
DEFAULT_KEEPALIVE = True

DEFAULT_SCAN_INTERVAL = 5             # seconds — fast telemetry poll (user-configurable)
MIN_SCAN_INTERVAL = 3                # floor: below this the logger cannot answer in time
MAX_SCAN_INTERVAL = 600
CONFIG_READ_INTERVAL = 900           # seconds â€” work_mode + max_sell re-read

# How many consecutive BLE poll failures to ride out before surfacing the
# entities as unavailable. Below this, the last good values are kept so a brief
# BLE hiccup doesn't flip every sensor to "unknown". A genuine outage still
# surfaces once the count is reached.
# --- Polling / cloud cross-check defaults ------------------------------------
# Coordinator initialises one field per constant below. The Cloud Window is
# implemented in coordinator._async_update_data: while fully-local mode is off,
# BLE polling pauses for CLOUD_WINDOW_DURATION every CLOUD_SYNC_INTERVAL seconds
# so the logger can push to DeyeCloud.
SLOW_TELEMETRY_INTERVAL = 300         # seconds between slow-changing reads
DEFAULT_CLOUD_SYNC_INTERVAL = 300     # pause BLE every 5 min for the cloud upload
DEFAULT_CLOUD_WINDOW_DURATION = 30    # seconds of BLE silence so the logger can upload
DEFAULT_FULLY_LOCAL = False           # pause BLE so the cloud feed keeps updating
DEFAULT_BLE_ENABLED = True            # BLE polling on by default

MAX_POLL_FAILURES = 10

# BLE writes occasionally time out on a flaky link. Each write is retried up to
# this many times (the read-back verify confirms it landed) before the failure
# surfaces to the caller. WRITE_RETRY_BACKOFF is the base inter-attempt delay in
# seconds, scaled by the attempt number so the link gets progressively longer to
# settle.
MAX_WRITE_ATTEMPTS = 3
WRITE_RETRY_BACKOFF = 2.0

# --- Inverter clock sync ----------------------------------------------------
# The RTC commits its staged registers on the Time Sync bit's on->off edge.

# Gap between the "Time Sync on" write and the clock-register writes.
# EVIDENCE IS THIN AND SAID SO HONESTLY: one rehearsal (2026-08-13) failed to
# latch with no gap and succeeded with a 1 s gap â€” one observation each way. The
# hypothesis is that the clock words otherwise land before the flag takes
# effect, so the falling edge commits stale values. Unconfirmed, cheap to keep.
CLOCK_SYNC_SETTLE = 1.0

# --- Reading back a commit ---------------------------------------------------
# THE RULE THAT COST US A DESIGN: change-detection cannot establish freshness on
# a self-ticking register. The RTC advances every second, so consecutive reads
# differ whether or not our write landed â€” a probe on 2026-08-15 produced 14
# distinct frames across 68 s in which the commit had NOT taken. "Wait until the
# frame changes" measures the passage of time and calls it evidence of a write.
#
# So the read-back is judged on CONTENT versus INTENT (the drift tolerance), and
# the constants below exist only to make sure the frame being judged is not a
# cached pre-write one.

# Settle after clearing the flag, before the read-back. Short, because the
# commit is effectively instantaneous: probes on 2026-08-15 saw the new value in
# the first frame back, at t+0.14 s and t+0.24 s across two runs.
#
# NO HORIZON, deliberately. An earlier design waited 20 s here to outlast the
# logger's read cache, on the theory that a cached PRE-write frame could be
# judged and report success over a corrupted RTC. The cache is real at rest â€”
# 12 byte-identical reads while the clock ticked â€” but across 32 post-commit
# frames in two runs it has never been observed surviving a write. A 20 s wait
# per attempt to defend against something never seen is ceremony, and ceremony
# that looks like rigour is worse than none.
CLOCK_COMMIT_SETTLE = 2.0

# The longest read-cache refresh gap observed at rest (9.1 s), rounded up. Used
# ONLY as the staleness margin on the drift threshold: a reading can be this old,
# so a decision to SKIP a write must clear the threshold by this much before we
# trust it. Erring toward writing is right while the write is only risky when
# the clock is actually wrong.
CLOCK_CACHE_MAX_AGE = 10.0

# What a verify concluded. "Wrong" and "unverified" are deliberately different:
# a distinct, decoded, out-of-tolerance frame is EVIDENCE the RTC is bad, while a
# timeout is an ABSENCE of evidence. Only the first justifies pushing harder.
VERIFY_OK = "verified"
VERIFY_WRONG = "wrong"
VERIFY_UNVERIFIED = "unverified"

# Giving up with the clock verified WRONG is the worst available outcome â€” it
# leaves the inverter on a bad date (2074 was observed live on 2026-08-14). So
# that case keeps trying on a slower, BOUNDED extension. Bounded because
# unlimited retries against a live inverter are their own hazard.
CLOCK_KNOWN_BAD_ATTEMPTS = 3
CLOCK_KNOWN_BAD_BACKOFF = 20.0
# 180 s, not 120: preserving three extension attempts matters more than a short
# worst case, because a single failed attempt is ordinary on this transport
# (three faults in five commits, 2026-08-14/15). LINKED TO THE CANCELLATION
# SHIELDING in _enable_time_sync â€” a longer held link is only tolerable because a
# cancellation cannot now abort the Time Sync re-enable. Do not raise this
# further without checking that still holds.
CLOCK_KNOWN_BAD_CEILING = 180.0

# Published sync outcomes. clock_wrong is separate from failed on purpose: a
# clock left at 2074 reported as a generic failure is the silent-failure shape
# this whole feature keeps producing.
SYNC_OK = "ok"
SYNC_SKIPPED = "skipped"
SYNC_FAILED = "failed"
SYNC_CLOCK_WRONG = "clock_wrong"
SYNC_RESULTS = [SYNC_OK, SYNC_SKIPPED, SYNC_FAILED, SYNC_CLOCK_WRONG]

# The write is not reliable first time â€” a year byte of 0x1A once landed as 0x7A
# (year 2122). The readback is verified against a freshly re-derived now and the
# whole sequence retried, with the target recomputed every attempt.
CLOCK_SYNC_ATTEMPTS = 3

# Accept window for that verify, in seconds. Wide enough to absorb the sequence's
# own elapsed time and BLE latency; far too tight to admit a corrupted field.
CLOCK_SYNC_TOLERANCE = 90

DEVICE_NAME = "Deye Inverter (BLE)"  # coexistence with deyecloud "Deye Inverter"

# Re-export for convenience
from .registers import WORK_MODE_LABELS  # noqa: F401

