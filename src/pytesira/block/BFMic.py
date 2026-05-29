#!/usr/bin/env python3
"""
BFMic (Biamp Parlé Beamforming Microphone) DSP block.

Confirmed attributes via live probe on TesiraForteX (FW 5.6.1.2):
  get numChannels         → int (2)
  get audioSources {ch}  → [{azimuth: float, intensity: float}, ...] (4 beams)
  get segmentsActive {ch} → [bool, bool, bool, bool] (4 zones)
  get mute {ch}          → bool
  set mute {ch} {true|false}
  get level {ch}         → float (dB, range -100..+8)
  set level {ch} {value}
  get minLevel {ch}      → float (-100.0)
  get maxLevel {ch}      → float (8.0)
  label                  → NOT supported (auto-generated)

Subscriptions use an explicit rate parameter (300 ms default).
BFMic sends the first subscription notification before +OK, so
_sync_command will timeout on subscribe commands — this is handled
by catching and ignoring the exception after adding to routing table.
"""
from threading import Event
from queue import Queue
from pytesira.block.block import Block
from pytesira.util.ttp_response import TTPResponse
from pytesira.util.types import TTPResponseType
from pytesira.util.channel import Channel
import logging

_DEFAULT_RATE_MS = 300
ACTIVE_THRESHOLD = 0.5


class BFMic(Block):
    """
    Biamp Parlé Beamforming Microphone — TTP block type 'BFMic'.

    Exposes:
    - Per-channel mute and level control (2 channels)
    - Real-time beam azimuth/intensity tracking via audioSources subscription
    - Zone segment state via segmentsActive subscription
    - Per-beam elevation from lobeData query (FW 4.11.2+)
    """

    VERSION = "0.1.0"

    def __init__(
        self,
        block_id: str,
        exit_flag: Event,
        connected_flag: Event,
        command_queue: Queue,
        subscriptions: dict,
        init_helper: str | None = None,
    ) -> None:

        # Logger must be set before super().__init__
        self._logger = logging.getLogger(f"{__name__}.{block_id}")

        super().__init__(
            block_id,
            exit_flag,
            connected_flag,
            command_queue,
            subscriptions,
            init_helper,
        )

        # Beam tracking data — updated by audioSources subscription
        self.beams: list[dict] = []         # [{azimuth: float, intensity: float}, ...]
        self.segments_active: list = []     # list of bools from segmentsActive
        self.elevations: list[float] = []   # per-beam elevation (FW 4.11.2+)

        # Load attributes (channels, level/mute ranges)
        try:
            if init_helper is not None:
                self._load_init_helper(init_helper)
            else:
                raise ValueError("no helper")
        except Exception as e:
            self._logger.debug(f"loading from init helper failed: {e}, querying DSP")
            self._query_attributes()

        # One-time elevation query (silently skipped on older firmware)
        self._query_lobe_data()

        # Build init helper for block map caching
        self._init_helper = {"channels": {}}
        for idx, ch in self.channels.items():
            self._init_helper["channels"][int(idx)] = ch.schema

    # ------------------------------------------------------------------
    # Subscription lifecycle
    # ------------------------------------------------------------------

    def subscribe(self) -> None:
        """Called once by the DSP after __init__ to register TTP subscriptions."""
        self._subscribe_mute_level()
        self._subscribe_beamtracking()

    def _register_base_subscriptions(self) -> list[TTPResponse]:
        """
        Called by the DSP device-data refresh loop on reconnect.
        Returns list of TTPResponse for reconnect-detection logic.
        """
        results = []
        results += self._subscribe_mute_level()
        results += self._subscribe_beamtracking()
        return results

    def _subscribe_mute_level(self) -> list[TTPResponse]:
        results = []
        for sub_type in ("mutes", "levels"):
            try:
                r = self._register_subscription(subscribe_type=sub_type, channel=None)
                results.append(r)
            except Exception as exc:
                self._logger.warning(f"{sub_type} subscription failed: {exc}")
        return results

    def _subscribe_beamtracking(self) -> list[TTPResponse]:
        """
        Subscribe to audioSources and segmentsActive.

        BFMic sends the first subscription notification before +OK, causing
        _sync_command to time out. We add to routing BEFORE sending, catch
        the timeout, and continue — data will flow correctly regardless.
        """
        results = []
        for sub_type in ("audioSources", "segmentsActive"):
            try:
                r = self._register_subscription(
                    subscribe_type=sub_type,
                    channel=1,
                    rate_ms=_DEFAULT_RATE_MS,
                )
                results.append(r)
            except Exception as exc:
                # Timeout is expected — subscription is active even if +OK didn't arrive
                self._logger.debug(
                    f"{sub_type} subscribe command timed out (normal for BFMic): {exc}"
                )
        return results

    # ------------------------------------------------------------------
    # Subscription callback
    # ------------------------------------------------------------------

    def subscription_callback(self, response: TTPResponse) -> None:
        sub_type = response.subscription_type

        if sub_type == "mutes":
            items = response.value if isinstance(response.value, list) else [response.value]
            for i, mute in enumerate(items):
                idx = i + 1
                if idx in self.channels:
                    self.channels[idx]._muted(bool(mute))
            self._logger.debug(f"mutes: {response.value}")

        elif sub_type == "levels":
            items = response.value if isinstance(response.value, list) else [response.value]
            for i, level in enumerate(items):
                idx = i + 1
                if idx in self.channels:
                    self.channels[idx]._level(float(level))
            self._logger.debug(f"levels: {response.value}")

        elif sub_type == "audioSources":
            self._handle_audio_sources(response.value)

        elif sub_type == "segmentsActive":
            val = response.value
            self.segments_active = val if isinstance(val, list) else [val]
            self._logger.debug(f"segmentsActive: {self.segments_active}")

        else:
            self._logger.debug(f"unhandled subscription: {sub_type}")

        super().subscription_callback(response)

    def _handle_audio_sources(self, value) -> None:
        items = value if isinstance(value, list) else [value]
        self.beams = [
            {
                "azimuth": float(item.get("azimuth", 0)),
                "intensity": float(item.get("intensity", 0)),
            }
            for item in items
            if isinstance(item, dict)
        ]
        self._logger.debug(f"beams: {len(self.beams)} ({self.talker_count} active)")

    # ------------------------------------------------------------------
    # Channel change callbacks (from Channel.muted / Channel.level setters)
    # ------------------------------------------------------------------

    def _channel_change_callback(
        self, data_type: str, channel_index: int, new_value
    ) -> TTPResponse | None:
        from pytesira.util.types import TTPResponseType
        if data_type == "muted":
            cmd_res = self._sync_command(
                f'"{self._block_id}" set mute {channel_index} {str(new_value).lower()}'
            )
            if cmd_res.type != TTPResponseType.CMD_OK:
                raise ValueError(cmd_res.value)
            return cmd_res
        elif data_type == "level":
            cmd_res = self._sync_command(
                f'"{self._block_id}" set level {channel_index} {new_value}'
            )
            if cmd_res.type != TTPResponseType.CMD_OK:
                raise ValueError(cmd_res.value)
            return cmd_res
        else:
            self._logger.warning(f"unhandled channel change: {data_type}")
            return None

    # ------------------------------------------------------------------
    # Attribute queries
    # ------------------------------------------------------------------

    def _query_attributes(self) -> None:
        num_channels = int(
            self._sync_command(f"{self._block_id} get numChannels").value
        )
        self.channels = {}
        for i in range(1, num_channels + 1):
            self.channels[i] = Channel(
                self._block_id,
                i,
                self._channel_change_callback,
                {
                    "label": f"{self._block_id}_ch{i}",
                    "min_level": self._sync_command(
                        f"{self._block_id} get minLevel {i}"
                    ).value,
                    "max_level": self._sync_command(
                        f"{self._block_id} get maxLevel {i}"
                    ).value,
                    "muted": self._sync_command(
                        f"{self._block_id} get mute {i}"
                    ).value,
                    "level": self._sync_command(
                        f"{self._block_id} get level {i}"
                    ).value,
                },
            )

    def _load_init_helper(self, init_helper: dict) -> None:
        self.channels = {}
        for i, d in init_helper["channels"].items():
            self.channels[int(i)] = Channel(
                self._block_id, int(i), self._channel_change_callback, d
            )

    def _query_lobe_data(self) -> None:
        try:
            resp = self._sync_command(f'"{self._block_id}" get lobeData')
            if resp.type == TTPResponseType.CMD_OK_VALUE and isinstance(resp.value, list):
                self.elevations = [
                    float(item.get("elevation", 0)) if isinstance(item, dict) else 0.0
                    for item in resp.value
                ]
        except Exception:
            pass

    def export_init_helper(self) -> dict:
        return {"version": self.VERSION, "helper": self._init_helper}

    # ------------------------------------------------------------------
    # Computed properties
    # ------------------------------------------------------------------

    @property
    def active_beams(self) -> list[dict]:
        return [b for b in self.beams if b["intensity"] >= ACTIVE_THRESHOLD]

    @property
    def talker_count(self) -> int:
        return len(self.active_beams)

    @property
    def primary_beam(self) -> dict | None:
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
        if not self.elevations or not self.beams:
            return None
        primary = self.primary_beam
        if primary is None:
            return None
        try:
            return self.elevations[self.beams.index(primary)]
        except (ValueError, IndexError):
            return None
