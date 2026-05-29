#!/usr/bin/env python3
"""
UsbInputEx DSP block — USB audio input (Type-A USB on TesiraFORTE).

Confirmed attributes via live probe on TesiraForteX (FW 5.6.1.2):
  get numChannels          → int (2)
  get levels               → [float, float]   all-channel levels (-50..0 dB)
  get mutes                → [bool, bool]      all-channel mute states
  get level {ch}           → float (dB)
  set level {ch}           → float
  get mute {ch}            → bool
  set mute {ch}            → bool
  get minLevel {ch}        → float (-50.0)
  get maxLevel {ch}        → float (0.0)
  get usbDeviceName        → str   (name of connected USB host, e.g. "Biamp TesiraFORTE X")
  label / gain / invert    → NOT supported

Subscriptions:
  mutes / levels — push fires BEFORE +OK, but +OK arrives immediately
  (standard BaseLevelMute flow, no timeout workaround needed).

pytesira type string:
  "<id>" get BLOCKTYPE → -ERR ... UsbInputExInterface::Attributes
  → module name: UsbInputEx
"""
from threading import Event
from queue import Queue
from pytesira.block.base_level_mute import BaseLevelMute
from pytesira.util.ttp_response import TTPResponse
import logging


class UsbInputEx(BaseLevelMute):
    """
    USB audio input block (Type-A USB on TesiraFORTE hardware).

    Extends BaseLevelMute for per-channel level/mute/subscription.
    Adds usbDeviceName to identify the connected USB host device.
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

        # USB device name (read-only, query after BaseLevelMute init)
        self.usb_device_name: str = ""
        try:
            val = self._sync_command(
                f'"{self._block_id}" get usbDeviceName'
            ).value
            self.usb_device_name = str(val) if val is not None else ""
        except Exception:
            pass
