"""loralarm — Python alarm service (migration target per server/chirpstack/…-migration-spec.md §7).

First milestone: parse ChirpStack MQTT uplink events and evaluate alarms.
Ingest/decoding stays in ChirpStack; this package consumes the decoded `object`.
"""

__version__ = "0.1.0"
