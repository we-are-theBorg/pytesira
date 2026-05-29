#!/usr/bin/env python3
"""
Parle Beamtracking DSP block

Supports Biamp Parle microphone arrays (TC-M, TCM-X, etc.) which expose
real-time beam-steering data over TTP subscriptions.

TTP attributes used:
  subscribe audioSources   1 <token> <rate_ms>  — list of {azimuth, intensity} per beam
  subscribe segmentsActive 1 <token> <rate_ms>  — active coarse zone segments
  get lobeData                                   — {azimuth, elevation} per beam (FW 4.11.2+)

Azimuth is 0-360 degrees measured counter-clockwise from the Biamp logo.
Intensity is 0.0-1.0; values >= ACTIVE_THRESHOLD indicate an active talker.
"""
from threading import Event
from queue import Queue
from pytesira.block.block import Block
from pytesira.util.ttp_response import TTPResponse
from pytesira.util.types import TTPResponseType
import logging

# Default subscription rate (ms).  Lower = more responsive but more SSH traffic.
_DEFAULT_RATE_MS = 500

# Beams at or above this intensity are considered active talkers.
ACTIVE_THRESHOLD = 0.5


class ParleBeamtracking(Block):
    """
    Parle Beamtracking DSP block.

    Wraps Biamp Parle ceiling microphone arrays as first-class pytesira block
    objects.  Beam steering data arrives via TTP subscriptions; elevation data
    is queried once on startup from the DSP.
    """

    VERSION = "0.1.0"

    # =================================================================================================================

    def __init__(
        self,
        block_id: str,
        exit_flag: Event,
        connected_flag: Event,
        command_queue: Queue,
        subscriptions: dict,
        init_helper: str | None = None,
    ) -> None:

        # Logger must be set before super().__init__ (Block base checks for it)
        self._logger = logging.getLogger(f"{__name__}.{block_id}")

        super().__init__(
            block_id,
            exit_flag,
            connected_flag,
            command_queue,
            subscriptions,
            init_helper,
        )

        # Live beam data (updated by audioSources subscription callbacks)
        self.beams: list[dict] = []        # [{azimuth: float, intensity: float}, ...]
        # Per-beam elevation angles populated once from lobeData (FW 4.11.2+)
        self.elevations: list[float] = []
        # Coarse zone segments currently active (updated by segmentsActive callbacks)
        self.segments_active: list = []

        # One-time elevation query — silently ignored on older firmware
        self._query_lobe_data()

        # Block-map caching is not useful for real-time beam data
        self._init_helper = {}

    # =================================================================================================================
    # Subscription management
    # =================================================================================================================

    def subscribe(self) -> None:
        """Called once by the DSP after __init__ to set up TTP subscriptions."""
        self._register_parle_subscriptions()

    def _register_base_subscriptions(self) -> list[TTPResponse]:
        """
        Called periodically by the DSP device-data refresh loop to re-subscribe
        after a connection interruption.  Returns TTPResponse list so the DSP
        reconnect-detection logic can check subscription status.
        """
        return self._register_parle_subscriptions()

    def _register_parle_subscriptions(self) -> list[TTPResponse]:
        """Register (or re-register) all Parle TTP subscriptions."""
        results = []
        for sub_type in ("audioSources", "segmentsActive"):
            try:
                res = self._register_subscription(
                    subscribe_type=sub_type,
                    channel=1,
                    rate_ms=_DEFAULT_RATE_MS,
                )
                results.append(res)
            except Exception as exc:
                self._logger.warning(f"subscription failed ({sub_type}): {exc}")
        return results

    # =================================================================================================================
    # Subscription callback
    # =================================================================================================================

    def subscription_callback(self, response: TTPResponse) -> None:
        """Route incoming subscription notifications to the correct handler."""
        sub_type = response.subscription_type
        if sub_type == "audioSources":
            self._handle_audio_sources(response.value)
        elif sub_type == "segmentsActive":
            val = response.value
            self.segments_active = val if isinstance(val, list) else [val]
        else:
            self._logger.debug(f"unhandled subscription type: {sub_type}")

        # Invoke any registered external callbacks (e.g. Home Assistant coordinator)
        super().subscription_callback(response)

    def _handle_audio_sources(self, value) -> None:
        """Parse audioSources payload into self.beams."""
        items = value if isinstance(value, list) else [value]
        self.beams = [
            {
                "azimuth": float(item.get("azimuth", 0)),
                "intensity": float(item.get("intensity", 0)),
            }
            for item in items
            if isinstance(item, dict)
        ]

    # =================================================================================================================
    # Startup query
    # =================================================================================================================

    def _query_lobe_data(self) -> None:
        """Query per-beam elevation via lobeData (Tesira FW 4.11.2+)."""
        try:
            resp = self._sync_command(f'"{self._block_id}" get lobeData')
            if resp.type == TTPResponseType.CMD_OK_VALUE and isinstance(resp.value, list):
                self.elevations = [
                    float(item.get("elevation", 0)) if isinstance(item, dict) else 0.0
                    for item in resp.value
                ]
                self._logger.debug(
                    f"lobeData: {len(self.elevations)} elevation value(s) loaded"
                )
        except Exception:
            pass  # older firmware — elevation stays as empty list

    def export_init_helper(self) -> dict:
        """Block-map caching is not meaningful for real-time beam data."""
        return {"version": self.VERSION, "helper": {}}

    # =================================================================================================================
    # Computed read-only properties
    # =================================================================================================================

    @property
    def active_beams(self) -> list[dict]:
        """Beams whose intensity is at or above ACTIVE_THRESHOLD."""
        return [b for b in self.beams if b["intensity"] >= ACTIVE_THRESHOLD]

    @property
    def talker_count(self) -> int:
        """Number of beams currently above the active-talker threshold."""
        return len(self.active_beams)

    @property
    def primary_beam(self) -> dict | None:
        """Highest-intensity active beam, or None when no active beams exist."""
        active = self.active_beams
        return max(active, key=lambda b: b["intensity"]) if active else None

    @property
    def primary_azimuth(self) -> float | None:
        b = self.primary_beam
        return b["azimuth"] if b else None

    @property
    def primary_intensity(self) -> float | None:
        b = self.primary_beam
        return b["intensity"] if b else None

    @property
    def primary_elevation(self) -> float | None:
        """Elevation of the primary beam, or None if FW < 4.11.2 or no active beam."""
        if not self.elevations or not self.beams:
            return None
        primary = self.primary_beam
        if primary is None:
            return None
        try:
            return self.elevations[self.beams.index(primary)]
        except (ValueError, IndexError):
            return None
