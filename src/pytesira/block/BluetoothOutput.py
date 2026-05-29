#!/usr/bin/env python3
"""
BluetoothOutput DSP block (EX-UBT Bluetooth audio output).

Confirmed attributes via live probe on TesiraForteX (FW 5.6.1.2):
  get numChannels     → int (1)
  get level {ch}      → float (dB, -100..0)
  set level {ch}      → float
  get mute {ch}       → bool
  set mute {ch}       → bool
  get minLevel {ch}   → float (-100.0)
  get maxLevel {ch}   → float (0.0)
  label / invert / gain       → NOT supported
  levels / mutes (no-channel) → NOT supported (CMD_ERROR)

Subscriptions:
  "subscribe level 1 ..."  — per-channel level, fires before +OK (early-fire)
  "subscribe mute 1 ..."   — per-channel mute, also early-fire

Block-level mutes/levels subscriptions (no channel) return CMD_ERROR;
per-channel level/mute subscriptions must be used instead.
"""
from threading import Event
from queue import Queue
from pytesira.block.base_level_mute_no_subscription import BaseLevelMuteNoSubscription
from pytesira.util.ttp_response import TTPResponse
import logging


class BluetoothOutput(BaseLevelMuteNoSubscription):
    """
    Bluetooth audio output block (EX-UBT transmitter).

    Extends BaseLevelMuteNoSubscription for channel/mute/level infrastructure.
    Adds per-channel level and mute subscriptions since block-level mutes/levels
    are not supported. Subscriptions use the early-fire pattern.
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
        # label not supported — BaseLevelMuteNoSubscription handles CMD_ERROR

        super().__init__(
            block_id,
            exit_flag,
            connected_flag,
            command_queue,
            subscriptions,
            init_helper,
        )

        # Populate initial mute/level state via poll (subscriptions set up later)
        self._query_status_attributes()

    # ------------------------------------------------------------------
    # Subscription lifecycle — per-channel (block-level not supported)
    # ------------------------------------------------------------------

    def subscribe(self) -> None:
        """Called by DSP after __init__. Register per-channel subscriptions."""
        self._subscribe_per_channel()

    def _register_base_subscriptions(self) -> list[TTPResponse]:
        """Called by DSP refresh loop for resubscription after reconnect."""
        return self._subscribe_per_channel()

    def _subscribe_per_channel(self) -> list[TTPResponse]:
        results = []
        for ch in self.channels:
            for sub_type in ("level", "mute"):
                try:
                    r = self._register_subscription(
                        subscribe_type=sub_type, channel=ch
                    )
                    results.append(r)
                except Exception as exc:
                    # BluetoothOutput fires subscription data before +OK
                    self._logger.debug(
                        f"{sub_type} ch{ch} subscribe timed out (normal): {exc}"
                    )
        return results

    # ------------------------------------------------------------------
    # Subscription callback — per-channel level / mute updates
    # ------------------------------------------------------------------

    def subscription_callback(self, response: TTPResponse) -> None:
        sub_type = response.subscription_type
        try:
            ch = int(response.subscription_channel_id)
        except (ValueError, TypeError):
            ch = None

        if ch is not None and ch in self.channels:
            if sub_type == "level":
                try:
                    self.channels[ch]._level(float(response.value))
                except (TypeError, ValueError):
                    pass
            elif sub_type == "mute":
                self.channels[ch]._muted(bool(response.value))

        # Fire registered HA callbacks
        self._on_subscription_callback()

    def refresh_status(self) -> None:
        """Re-query current mute and level state (polling fallback)."""
        self._query_status_attributes()
