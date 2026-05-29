#!/usr/bin/env python3
"""
AVBPOEAmp (AVB Power-over-Ethernet Amplifier) DSP block.

Confirmed attributes via live probe on TesiraForteX (FW 5.6.1.2):
  get numChannels     → int (2)
  get levels          → [float, float]   all-channel levels (no-channel query)
  get level {ch}      → float (dB, -100..0 — maxLevel=0 dB, unity)
  set level {ch}      → float
  get mute {ch}       → bool
  set mute {ch}       → bool
  get invert {ch}     → bool
  set invert {ch}     → bool
  get minLevel {ch}   → float (-100.0)
  get maxLevel {ch}   → float (0.0)
  label               → NOT supported (auto-generated)

Subscriptions (mutes, levels) both fire before +OK — same early-fire pattern
as BFMic and AudioMeter. Timeout on subscribe command is expected and caught;
routing token is pre-registered so data flows correctly.

maxLevel of 0 dB reflects the amplifier's unity-gain ceiling — this block
controls the DSP-side signal level feeding the Parlé POE speaker amplifier,
not a variable-gain power amp stage.

No device identity attributes (model, serial, IP, etc.) exposed via TTP.
"""
from threading import Event
from queue import Queue
from pytesira.block.base_level_mute import BaseLevelMute
from pytesira.util.ttp_response import TTPResponse
from pytesira.util.types import TTPResponseType
import logging


class AVBPOEAmp(BaseLevelMute):
    """
    AVB Power-over-Ethernet Amplifier output block (Parlé TC-AM / TC-EX).

    Extends BaseLevelMute for per-channel mute/level/subscription.
    Adds invert control per channel. Subscriptions (mutes, levels) are
    overridden to handle the early-fire pattern gracefully.
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

        self._logger = logging.getLogger(f"{__name__}.{block_id}")
        # label not supported — BaseLevelMute handles CMD_ERROR as empty string

        super().__init__(
            block_id,
            exit_flag,
            connected_flag,
            command_queue,
            subscriptions,
            init_helper,
        )

        # Query initial invert state for each channel
        self._query_invert()

    # ------------------------------------------------------------------
    # Subscription override — early-fire pattern (data arrives before +OK)
    # ------------------------------------------------------------------

    def _register_base_subscriptions(self) -> list[TTPResponse]:
        """
        Override BaseLevelMute to catch the subscription command timeout.
        AVBPOEAmp (like BFMic and AudioMeter) sends subscription data before
        returning +OK, so _sync_command times out. The token is pre-registered
        before the command is sent, so data routing works correctly regardless.
        """
        results = []
        for sub_type in ("mutes", "levels"):
            try:
                r = self._register_subscription(subscribe_type=sub_type, channel=None)
                results.append(r)
            except Exception as exc:
                self._logger.debug(
                    f"{sub_type} subscribe timed out (normal for AVBPOEAmp): {exc}"
                )
        return results

    # ------------------------------------------------------------------
    # Channel change callback — adds invert support
    # ------------------------------------------------------------------

    def _channel_change_callback(self, data_type, channel_index, new_value):
        if data_type == "inverted":
            new_val, cmd_res = self._set_and_update_val(
                "invert", value=str(new_value).lower(), channel=channel_index
            )
            if cmd_res.type != TTPResponseType.CMD_OK:
                raise ValueError(cmd_res.value)
            self.channels[channel_index]._inverted(new_val)
            return cmd_res
        return super()._channel_change_callback(data_type, channel_index, new_value)

    # ------------------------------------------------------------------
    # Invert state query (not part of BaseLevelMute)
    # ------------------------------------------------------------------

    def _query_invert(self) -> None:
        for i in self.channels:
            try:
                inv = self._sync_command(
                    f'"{self._block_id}" get invert {i}'
                ).value
                self.channels[i]._inverted(bool(inv))
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Public setter
    # ------------------------------------------------------------------

    def set_invert(self, channel: int, value: bool) -> None:
        """Set per-channel phase inversion."""
        self.channels[channel].inverted = value
