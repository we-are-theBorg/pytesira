#!/usr/bin/env python3
"""
BluetoothControlStatus DSP block (EX-UBT Bluetooth control/status).

Confirmed attributes via live probe on TesiraForteX (FW 5.6.1.2):
  get connected              → bool  (True = device connected)
  get discoverable           → bool  (True = device is discoverable)
  get deviceName             → str   ("ARCnet" — Tesira's BT device name)
  get profile                → str   ("A2DP: APTX SNK 48000")
  get connectedDeviceName    → str   ("ARC Fairphone 5 5G")

  subscribe connected "{token}"  → fires immediately with current state,
                                   then +OK arrives normally — standard
                                   _register_subscription() flow works correctly.

No per-channel attributes, no audio level/mute control.
This is a pure status block for the EX-UBT Bluetooth adapter.
"""
from threading import Event
from queue import Queue
from pytesira.block.block import Block
from pytesira.util.ttp_response import TTPResponse
from pytesira.util.types import TTPResponseType
import logging


class BluetoothControlStatus(Block):
    """
    Bluetooth control and status block (EX-UBT adapter).

    Provides real-time connection status via a 'connected' subscription
    and read-only status attributes: discoverable, device name, BT profile,
    and the name of the currently connected remote device.
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

        super().__init__(
            block_id,
            exit_flag,
            connected_flag,
            command_queue,
            subscriptions,
            init_helper,
        )

        # BT status attributes
        self.bt_connected: bool = False       # True when a remote device is connected
        self.discoverable: bool = False       # True when the adapter is discoverable
        self.device_name: str = ""            # This Tesira's BT name (e.g. "ARCnet")
        self.profile: str = ""               # Active BT profile (e.g. "A2DP: APTX SNK 48000")
        self.connected_device_name: str = "" # Remote device name (e.g. "ARC Fairphone 5 5G")

        # Load from init helper (cached device_name / profile) or query all live
        try:
            if init_helper is not None:
                self._load_init_helper(init_helper)
            else:
                raise ValueError("no helper")
        except Exception as exc:
            self._logger.debug(f"init helper unusable: {exc}, querying DSP")
            self._query_attributes()

        # Cache slow-changing attrs; connected_device_name is live
        self._init_helper = {
            "device_name": self.device_name,
            "profile": self.profile,
        }

    # ------------------------------------------------------------------
    # Subscription lifecycle — connected fires data first, +OK follows normally
    # ------------------------------------------------------------------

    def subscribe(self) -> None:
        """Called by DSP after __init__ to subscribe to connection state changes."""
        self._subscribe_connected()

    def _register_base_subscriptions(self) -> list[TTPResponse]:
        """Called by DSP refresh loop for resubscription."""
        return self._subscribe_connected()

    def _subscribe_connected(self) -> list[TTPResponse]:
        results = []
        try:
            r = self._register_subscription(subscribe_type="connected", channel=None)
            results.append(r)
        except Exception as exc:
            self._logger.warning(f"connected subscribe failed: {exc}")
        return results

    # ------------------------------------------------------------------
    # Subscription callback
    # ------------------------------------------------------------------

    def subscription_callback(self, response: TTPResponse) -> None:
        if response.subscription_type == "connected":
            self.bt_connected = bool(response.value)
            self._logger.debug(f"BT connected={self.bt_connected}")
            # Refresh connected device name when state changes
            if self.bt_connected:
                try:
                    r = self._sync_command(
                        f'"{self._block_id}" get connectedDeviceName'
                    )
                    self.connected_device_name = str(r.value or "")
                except Exception:
                    pass
            else:
                self.connected_device_name = ""
        super().subscription_callback(response)

    # ------------------------------------------------------------------
    # Attribute queries
    # ------------------------------------------------------------------

    def _query_attributes(self) -> None:
        def _get(attr):
            try:
                return self._sync_command(f'"{self._block_id}" get {attr}').value
            except Exception:
                return None

        self.bt_connected = bool(_get("connected"))
        self.discoverable = bool(_get("discoverable"))
        self.device_name = str(_get("deviceName") or "")
        self.profile = str(_get("profile") or "")
        self.connected_device_name = str(_get("connectedDeviceName") or "")

    def _load_init_helper(self, helper: dict) -> None:
        """Load slow-changing attrs from cache; query live state."""
        self.device_name = str(helper.get("device_name", ""))
        self.profile = str(helper.get("profile", ""))
        # Always query live state (connected / connected_device_name change frequently)
        self.bt_connected = bool(
            self._sync_command(f'"{self._block_id}" get connected').value
        )
        self.discoverable = bool(
            self._sync_command(f'"{self._block_id}" get discoverable').value
        )
        val = self._sync_command(f'"{self._block_id}" get connectedDeviceName').value
        self.connected_device_name = str(val or "")

    def refresh_status(self) -> None:
        """Re-query all live attributes."""
        self._query_attributes()

    def export_init_helper(self) -> dict:
        return {"version": self.VERSION, "helper": self._init_helper}

    # ------------------------------------------------------------------
    # Setters (discoverable mode toggle — documented as settable)
    # ------------------------------------------------------------------

    def set_discoverable(self, value: bool) -> None:
        """Enable or disable Bluetooth discoverability."""
        cmd_res = self._sync_command(
            f'"{self._block_id}" set discoverable {str(value).lower()}'
        )
        if cmd_res.type != TTPResponseType.CMD_OK:
            raise ValueError(cmd_res.value)
        self.discoverable = value
