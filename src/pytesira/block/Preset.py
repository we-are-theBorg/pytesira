#!/usr/bin/env python3
from threading import Event
from pytesira.block.block import Block
from queue import Queue
from pytesira.util.ttp_response import TTPResponse
from pytesira.util.types import TTPResponseType
from datetime import datetime
import logging


class Preset(Block):
    """
    Tesira Preset block.

    Represents a named Preset object in the Tesira design file.  Each preset
    appears as an alias in SESSION get aliases with block type "Preset".

    TTP commands used:
      "<instance_tag>" recall   -- recall this preset
      "<instance_tag>" save     -- save current DSP state to this preset

    No subscriptions: preset operations are fire-and-forget.
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

        self.__last_recalled: datetime | None = None
        self.__last_recall_ok: bool | None = None

        # No attributes to query; init helper is always empty.
        self._init_helper = {}

    # ------------------------------------------------------------------

    @property
    def last_recalled(self) -> datetime | None:
        """Timestamp of the most recent successful recall, or None."""
        return self.__last_recalled

    @property
    def last_recall_ok(self) -> bool | None:
        """Whether the most recent recall command succeeded, or None if never called."""
        return self.__last_recall_ok

    # ------------------------------------------------------------------

    def recall(self) -> bool:
        """
        Recall this preset on the DSP.

        Sends: "<block_id>" recall
        Returns True on success, False on DSP error.
        Fires registered callbacks so HA entities can reflect the update.
        """
        cmd_res = self._sync_command(f'"{self._block_id}" recall')
        ok = cmd_res.type == TTPResponseType.CMD_OK
        self.__last_recall_ok = ok
        if ok:
            self.__last_recalled = datetime.now()
            self._logger.info(f"preset recalled: {self._block_id}")
        else:
            self._logger.warning(f"preset recall failed: {self._block_id}: {cmd_res.value}")
        self._on_subscription_callback()
        return ok

    def save(self) -> bool:
        """
        Save current DSP state to this preset.

        Sends: "<block_id>" save
        Returns True on success, False on DSP error.
        """
        cmd_res = self._sync_command(f'"{self._block_id}" save')
        ok = cmd_res.type == TTPResponseType.CMD_OK
        if ok:
            self._logger.info(f"preset saved: {self._block_id}")
        else:
            self._logger.warning(f"preset save failed: {self._block_id}: {cmd_res.value}")
        return ok

    # ------------------------------------------------------------------

    def export_init_helper(self) -> dict:
        return {"version": self.VERSION, "helper": {}}
