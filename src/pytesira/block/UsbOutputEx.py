#!/usr/bin/env python3
"""
UsbOutputEx DSP block — USB audio output (Type-A USB on TesiraFORTE).

Confirmed attributes via live probe on TesiraForteX (FW 5.6.1.2):
  get numChannels       → int (2)
  get levels            → [float, float]   all-channel levels (-100..0 dB)
  get mutes             → [bool, bool]      all-channel mute states
  get level {ch}        → float (dB)
  set level {ch}        → float
  get mute {ch}         → bool
  set mute {ch}         → bool
  get minLevel {ch}     → float (-100.0)
  get maxLevel {ch}     → float (0.0)
  label / gain / invert / usbDeviceName  → NOT supported

Subscriptions:
  mutes / levels — push fires BEFORE +OK, but +OK arrives immediately
  (standard BaseLevelMute flow, no timeout workaround needed).

pytesira type string:
  "<id>" get BLOCKTYPE → -ERR ... UsbOutputExInterface::Attributes
  → module name: UsbOutputEx
"""
from threading import Event
from queue import Queue
from pytesira.block.base_level_mute import BaseLevelMute
import logging


class UsbOutputEx(BaseLevelMute):
    """
    USB audio output block (Type-A USB on TesiraFORTE hardware).

    Extends BaseLevelMute for per-channel level/mute/subscription.
    No additional attributes beyond the base class.
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
