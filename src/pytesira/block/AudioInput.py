#!/usr/bin/env python3
"""
AudioInput DSP block (analog audio input).

Confirmed attributes via live probe on TesiraForteX (FW 5.6.1.2):
  get numChannels     → int (2)
  get gain {ch}       → float (dB, 0-66 in 6 dB steps)
  set gain {ch}       → float
  get invert {ch}     → bool
  set invert {ch}     → bool
  get level {ch}      → float (dB, minLevel..maxLevel)
  set level {ch}      → float
  get mute {ch}       → bool
  set mute {ch}       → bool
  get minLevel {ch}   → float (-100.0)
  get maxLevel {ch}   → float (12.0)
  label               → NOT supported (auto-generated)

TTP docs also list (not probed — correct attr names):
  get/set/toggle phantomPower {ch}  → bool
  get/subscribe peak {ch}           → bool (peak indicator)
  get/subscribe peaks               → all-channel peaks

No working subscriptions confirmed on this DSP — entities should poll
via refresh_status().
"""
from threading import Event
from queue import Queue
from pytesira.block.base_level_mute_no_subscription import BaseLevelMuteNoSubscription
from pytesira.util.types import TTPResponseType
import logging


class AudioInput(BaseLevelMuteNoSubscription):
    """
    Analog audio input block.

    Extends BaseLevelMuteNoSubscription for per-channel mute/level with
    polling-based refresh. Adds input-specific attributes: gain (preamp),
    invert (polarity), and phantomPower (48 V for condenser microphones).
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
        # label not supported — BaseLevelMuteNoSubscription auto-generates "{block_id}_{ch}"
        self._chan_label_key = "@"

        super().__init__(
            block_id,
            exit_flag,
            connected_flag,
            command_queue,
            subscriptions,
            init_helper,
        )

        # Input-specific per-channel state (dicts keyed by channel index)
        self.gain: dict[int, float] = {}
        self.phantom_power: dict[int, bool] = {}

        # Load input-specific attributes (init_helper or live query via refresh)
        ai_init = None
        if isinstance(init_helper, dict):
            ai_init = init_helper.get("audio_input")

        if ai_init:
            self._load_input_helper(ai_init)
        else:
            self._query_input_attrs()

        # Extend block-map helper with input-specific data
        self._init_helper["audio_input"] = {
            ch: {
                "gain": self.gain.get(ch, 0.0),
                "phantom_power": self.phantom_power.get(ch, False),
            }
            for ch in self.channels
        }

    # ------------------------------------------------------------------
    # Channel change callback (called by Channel.muted / .level setters
    # and also by invert setter when using Channel._inverted mechanism)
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
        else:
            return super()._channel_change_callback(data_type, channel_index, new_value)

    # ------------------------------------------------------------------
    # Status queries (called on init and by refresh_status)
    # ------------------------------------------------------------------

    def _query_status_attributes(self) -> None:
        """Query runtime state: level, mute (from super), gain, invert, phantomPower."""
        super()._query_status_attributes()
        self._query_input_attrs()

    def _query_input_attrs(self) -> None:
        for i in self.channels:
            # gain
            try:
                self.gain[i] = float(
                    self._sync_command(f'"{self._block_id}" get gain {i}').value
                )
            except Exception:
                self.gain[i] = 0.0

            # invert (store via Channel hidden setter so Channel.inverted works)
            try:
                inv = self._sync_command(f'"{self._block_id}" get invert {i}').value
                self.channels[i]._inverted(bool(inv))
            except Exception:
                pass

            # phantomPower
            try:
                pp = self._sync_command(f'"{self._block_id}" get phantomPower {i}').value
                self.phantom_power[i] = bool(pp)
            except Exception:
                self.phantom_power[i] = False

    def _load_input_helper(self, ai_init: dict) -> None:
        for ch_key, data in ai_init.items():
            ch = int(ch_key)
            if ch not in self.channels:
                continue
            self.gain[ch] = float(data.get("gain", 0.0))
            self.phantom_power[ch] = bool(data.get("phantom_power", False))

    def refresh_status(self) -> None:
        """Re-query all status attributes (mute, level, gain, invert, phantomPower)."""
        self._query_status_attributes()

    def export_init_helper(self) -> dict:
        return {"version": self.VERSION, "helper": self._init_helper}

    # ------------------------------------------------------------------
    # Setters
    # ------------------------------------------------------------------

    def set_gain(self, channel: int, value: float) -> None:
        """Set preamp gain (0-66 dB, 6 dB increments)."""
        cmd_res = self._sync_command(
            f'"{self._block_id}" set gain {channel} {value}'
        )
        if cmd_res.type != TTPResponseType.CMD_OK:
            raise ValueError(cmd_res.value)
        self.gain[channel] = value

    def set_invert(self, channel: int, value: bool) -> None:
        """Set per-channel phase inversion."""
        self.channels[channel].inverted = value

    def set_phantom_power(self, channel: int, value: bool) -> None:
        """Enable/disable 48 V phantom power for condenser microphones."""
        cmd_res = self._sync_command(
            f'"{self._block_id}" set phantomPower {channel} {str(value).lower()}'
        )
        if cmd_res.type != TTPResponseType.CMD_OK:
            raise ValueError(cmd_res.value)
        self.phantom_power[channel] = value
