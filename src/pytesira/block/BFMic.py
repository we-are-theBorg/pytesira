#!/usr/bin/env python3
"""
BFMic (Biamp Parle Beamforming Microphone) DSP block.

Confirmed attributes via live probe on TesiraForteX (FW 5.6.1.2):
  get numChannels          -> int (2) - two independent tracking channels
  get audioSources {ch}   -> [{azimuth: float, intensity: float}, ...] (4 beams per channel)
  get segmentsActive {ch} -> [bool, bool, bool, bool] (4 zone segments per channel)
  get lobeData {ch}       -> [{azimuth, intensity, elevation}, ...] (firmware 4.11.2+)
  get mute {ch}           -> bool / set mute {ch} {true|false}
  get level {ch}          -> float (dB -100..+8) / set level {ch} {value}
  get minLevel {ch}       -> float / get maxLevel {ch} -> float
  label                   -> NOT supported (auto-generated)

Channels 1..numChannels each have independent beamforming tracking. Each
channel is typically configured in Biamp DSP designer to cover a separate
spatial zone (e.g., channel 1 = table side, channel 2 = presenter side).

Subscriptions fire before +OK (early-fire); timeout is expected and handled.
"""

from threading import Event
from queue import Queue
from pytesira.block.block import Block
from pytesira.util.ttp_response import TTPResponse
from pytesira.util.types import TTPResponseType
from pytesira.util.channel import Channel
import logging

_DEFAULT_RATE_MS = 500
ACTIVE_THRESHOLD = 0.5


def _safe_float(val, default: float = 0.0) -> float:
    """Convert val to float, stripping any trailing TTP dict braces."""
    try:
        return float(str(val).strip().rstrip("}").strip())
    except (ValueError, TypeError):
        return default


class BFMic(Block):
    """
    Biamp Parle Beamforming Microphone -- TTP block type BFMic.

    Two independent beamforming channels (numChannels=2), each tracking
    talkers in a separate spatial zone.
    """

    VERSION = "0.2.0"

    def __init__(
        self,
        block_id: str,
        exit_flag: Event,
        connected_flag: Event,
        command_queue: Queue,
        subscriptions: dict,
        init_helper: str | None = None,
    ) -> None:
        self._logger = logging.getLogger(f"{__name__}.{block_id}")
        super().__init__(
            block_id, exit_flag, connected_flag, command_queue, subscriptions, init_helper,
        )

        # Per-channel beamtracking data -- keyed by channel index (1-based)
        self.channel_beams: dict[int, list[dict]] = {}
        self.channel_segments_active: dict[int, list] = {}
        self.channel_elevations: dict[int, list[float]] = {}

        try:
            if init_helper is not None:
                self._load_init_helper(init_helper)
            else:
                raise ValueError("no helper")
        except Exception as e:
            self._logger.debug(f"loading from init helper failed: {e}, querying DSP")
            self._query_attributes()

        self._query_lobe_data()

        self._init_helper = {"channels": {}}
        for idx, ch in self.channels.items():
            self._init_helper["channels"][int(idx)] = ch.schema

    # ------------------------------------------------------------------
    # Subscription lifecycle
    # ------------------------------------------------------------------

    def subscribe(self) -> None:
        self._subscribe_mute_level()
        self._subscribe_beamtracking()

    def _register_base_subscriptions(self) -> list[TTPResponse]:
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
                self._logger.debug(f"{sub_type} subscribe timed out (normal): {exc}")
        return results

    def _subscribe_beamtracking(self) -> list[TTPResponse]:
        results = []
        for ch_idx in range(1, len(self.channels) + 1):
            for sub_type in ("audioSources", "segmentsActive"):
                try:
                    r = self._register_subscription(
                        subscribe_type=sub_type,
                        channel=ch_idx,
                        rate_ms=_DEFAULT_RATE_MS,
                    )
                    results.append(r)
                except Exception as exc:
                    self._logger.debug(
                        f"{sub_type} ch{ch_idx} subscribe timed out (normal for BFMic): {exc}"
                    )
        return results

    # ------------------------------------------------------------------
    # Subscription callback
    # ------------------------------------------------------------------

    def subscription_callback(self, response: TTPResponse) -> None:
        try:
            sub_type = response.subscription_type

            if sub_type == "mutes":
                items = response.value if isinstance(response.value, list) else [response.value]
                for i, mute in enumerate(items):
                    idx = i + 1
                    if idx in self.channels:
                        self.channels[idx]._muted(bool(mute))

            elif sub_type == "levels":
                items = response.value if isinstance(response.value, list) else [response.value]
                for i, level in enumerate(items):
                    idx = i + 1
                    if idx in self.channels:
                        self.channels[idx]._level(_safe_float(level))

            elif sub_type == "audioSources":
                try:
                    ch = int(response.subscription_channel_id)
                except (ValueError, AttributeError):
                    ch = 1
                self._handle_audio_sources(ch, response.value)

            elif sub_type == "segmentsActive":
                try:
                    ch = int(response.subscription_channel_id)
                except (ValueError, AttributeError):
                    ch = 1
                val = response.value
                self.channel_segments_active[ch] = val if isinstance(val, list) else [val]

        except Exception as exc:
            self._logger.warning(
                f"subscription_callback error ({response.subscription_type}): {exc}"
            )

        super().subscription_callback(response)

    def _handle_audio_sources(self, channel: int, value) -> None:
        items = value if isinstance(value, list) else [value]
        beams = []
        for item in items:
            try:
                if isinstance(item, dict):
                    beams.append({
                        "azimuth":   _safe_float(item.get("azimuth", 0)),
                        "intensity": _safe_float(item.get("intensity", 0)),
                    })
            except Exception as exc:
                self._logger.debug(f"beam parse error: {exc!r} item={item!r}")
        self.channel_beams[channel] = beams

    # ------------------------------------------------------------------
    # Channel change callbacks
    # ------------------------------------------------------------------

    def _channel_change_callback(
        self, data_type: str, channel_index: int, new_value
    ) -> TTPResponse | None:
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
            self._sync_command(f'"{self._block_id}" get numChannels').value
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
                        f'"{self._block_id}" get minLevel {i}'
                    ).value,
                    "max_level": self._sync_command(
                        f'"{self._block_id}" get maxLevel {i}'
                    ).value,
                    "muted": self._sync_command(
                        f'"{self._block_id}" get mute {i}'
                    ).value,
                    "level": self._sync_command(
                        f'"{self._block_id}" get level {i}'
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
        for ch in range(1, len(self.channels) + 1):
            try:
                resp = self._sync_command(f'"{self._block_id}" get lobeData {ch}')
                if resp.type == TTPResponseType.CMD_OK_VALUE and isinstance(resp.value, list):
                    self.channel_elevations[ch] = [
                        _safe_float(item.get("elevation", 0)) if isinstance(item, dict) else 0.0
                        for item in resp.value
                    ]
            except Exception:
                pass

    def export_init_helper(self) -> dict:
        return {"version": self.VERSION, "helper": self._init_helper}

    # ------------------------------------------------------------------
    # Per-channel computed methods
    # ------------------------------------------------------------------

    def beams_for_channel(self, ch: int) -> list[dict]:
        return self.channel_beams.get(ch, [])

    def active_beams_for_channel(self, ch: int) -> list[dict]:
        return [b for b in self.beams_for_channel(ch) if b["intensity"] >= ACTIVE_THRESHOLD]

    def talker_count_for_channel(self, ch: int) -> int:
        return len(self.active_beams_for_channel(ch))

    def primary_beam_for_channel(self, ch: int) -> dict | None:
        active = self.active_beams_for_channel(ch)
        return max(active, key=lambda b: b["intensity"]) if active else None

    def primary_azimuth_for_channel(self, ch: int) -> float | None:
        b = self.primary_beam_for_channel(ch)
        return b["azimuth"] if b else None

    def primary_intensity_for_channel(self, ch: int) -> float | None:
        b = self.primary_beam_for_channel(ch)
        return b["intensity"] if b else None

    def primary_elevation_for_channel(self, ch: int) -> float | None:
        elevs = self.channel_elevations.get(ch, [])
        beams = self.beams_for_channel(ch)
        primary = self.primary_beam_for_channel(ch)
        if not elevs or not beams or primary is None:
            return None
        try:
            return elevs[beams.index(primary)]
        except (ValueError, IndexError):
            return None

    # ------------------------------------------------------------------
    # Backward-compatible block-level properties (alias to channel 1)
    # ------------------------------------------------------------------

    @property
    def beams(self) -> list[dict]:
        return self.channel_beams.get(1, [])

    @property
    def segments_active(self) -> list:
        return self.channel_segments_active.get(1, [])

    @property
    def elevations(self) -> list[float]:
        return self.channel_elevations.get(1, [])

    @property
    def active_beams(self) -> list[dict]:
        return self.active_beams_for_channel(1)

    @property
    def talker_count(self) -> int:
        return self.talker_count_for_channel(1)

    @property
    def primary_beam(self) -> dict | None:
        return self.primary_beam_for_channel(1)

    @property
    def primary_azimuth(self) -> float | None:
        return self.primary_azimuth_for_channel(1)

    @property
    def primary_intensity(self) -> float | None:
        return self.primary_intensity_for_channel(1)

    @property
    def primary_elevation(self) -> float | None:
        return self.primary_elevation_for_channel(1)