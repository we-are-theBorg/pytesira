#!/usr/bin/env python3
"""
BluetoothInput DSP block (EX-UBT Bluetooth audio input).

Confirmed attributes via live probe on TesiraForteX (FW 5.6.1.2):
  get numChannels     → int (2)
  get levels          → [float, float]   all-channel levels
  get mutes           → [bool, bool]     all-channel mute states
  get level {ch}      → float (dB, -100..0 — unity max, same as AVBPOEAmp)
  set level {ch}      → float
  get mute {ch}       → bool
  set mute {ch}       → bool
  get minLevel {ch}   → float (-100.0)
  get maxLevel {ch}   → float (0.0)
  label / invert / gain / stereo → NOT supported

Subscriptions (mutes, levels) use the early-fire pattern — data arrives
before +OK. Token is pre-registered via _register_subscription(); timeout
on the sync command is caught and ignored.

Structurally identical to AVBPOEAmp but without invert support.
"""
from threading import Event
from queue import Queue
from pytesira.block.base_level_mute import BaseLevelMute
from pytesira.util.ttp_response import TTPResponse
import logging


class BluetoothInput(BaseLevelMute):
    """
    Bluetooth audio input block (EX-UBT receiver).

    Extends BaseLevelMute for per-channel mute/level/subscription.
    Overrides _register_base_subscriptions to handle the early-fire pattern
    where subscription data arrives before the +OK acknowledgment.
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

    # ------------------------------------------------------------------
    # Subscription override — early-fire pattern (data arrives before +OK)
    # ------------------------------------------------------------------

    def _register_base_subscriptions(self) -> list[TTPResponse]:
        """
        BluetoothInput sends subscription data before returning +OK,
        causing _sync_command to time out. Token is pre-registered before
        the command is sent, so routing and callbacks work correctly.
        """
        results = []
        for sub_type in ("mutes", "levels"):
            try:
                r = self._register_subscription(subscribe_type=sub_type, channel=None)
                results.append(r)
            except Exception as exc:
                self._logger.debug(
                    f"{sub_type} subscribe timed out (normal for BluetoothInput): {exc}"
                )
        return results
