"""chirp.cmd.schema — UDP JSON command/response validators.

Placeholder; populated in Phase 1. See SDR_DEMOD_DESIGN_2026-06-03.md Section 5
for the command list, error codes, and event stream definitions.
"""

# Protocol version. Daemon advertises this via get_status.
# Breaking changes bump this; non-breaking additions do not.
PROTOCOL_VERSION = 1
