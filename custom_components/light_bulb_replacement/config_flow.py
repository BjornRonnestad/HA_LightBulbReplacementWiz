"""Config and options flows for the replacement wizard."""

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import probatio as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import CONF_CONFIRM, CONF_NEW, CONF_OLD, DOMAIN, TITLE
from .replacement import ReplacementError, ReplacementManager, ReplacementPlan

if TYPE_CHECKING:
    from .zha import ZhaMigration, ZhaPlan

_LOGGER = logging.getLogger(__name__)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Install a single wizard, reused for each replacement."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title=TITLE, data={})
        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return ReplacementOptionsFlow()


class ReplacementOptionsFlow(config_entries.OptionsFlow):
    """Select two lights, preview changes, then explicitly confirm."""

    def __init__(self) -> None:
        super().__init__()
        self._plan: ReplacementPlan | None = None
        self._zha_plan: ZhaPlan | None = None
        self._migration: ZhaMigration | None = None
        self._scan_task: asyncio.Task | None = None
        self._operation_task: asyncio.Task | None = None
        self._progress_error: str | None = None

    @property
    def manager(self) -> ReplacementManager:
        return self.hass.data[DOMAIN]

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["select", "restore"])

    async def async_step_select(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors = {}
        if user_input is not None:
            try:
                self._plan = self.manager.preview(
                    user_input[CONF_OLD], user_input[CONF_NEW]
                )
                self._zha_plan = None
                self._scan_task = None
                if user_input.get("copy_zha"):
                    if (
                        self._plan.old.platform != "zha"
                        or self._plan.new.platform != "zha"
                    ):
                        raise ReplacementError("zha_lights_required")
                    from .zha import ZhaMigration

                    self._migration = ZhaMigration(self.hass)
                    return await self.async_step_scan()
            except ReplacementError as err:
                errors["base"] = err.code
            else:
                return await self.async_step_confirm()
        return self.async_show_form(
            step_id="select",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_OLD): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="light")
                    ),
                    vol.Required(CONF_NEW): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="light")
                    ),
                    vol.Optional("copy_zha", default=False): selector.BooleanSelector(),
                }
            ),
            errors=errors,
        )

    async def async_step_scan(self, user_input=None) -> config_entries.ConfigFlowResult:
        assert self._plan is not None and self._migration is not None
        if self._scan_task is None:
            self._scan_task = self.hass.async_create_task(
                self._migration.async_scan(self._plan.old, self._plan.new),
                "Scan ZHA bindings",
            )
        if not self._scan_task.done():
            return self.async_show_progress(
                step_id="scan",
                progress_action="scan_zha",
                progress_task=self._scan_task,
            )
        try:
            self._zha_plan = self._scan_task.result()
        except ReplacementError as err:
            self._progress_error = err.code
        except Exception:
            _LOGGER.exception("ZHA discovery failed")
            self._progress_error = "zha_read_failed"
        else:
            return self.async_show_progress_done(next_step_id="confirm_zha")
        return self.async_show_progress_done(next_step_id="operation_failed")

    async def async_step_confirm_zha(
        self, user_input=None
    ) -> config_entries.ConfigFlowResult:
        return await self.async_step_confirm(user_input)

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        assert self._plan is not None
        plan = self._plan
        errors = {}
        if user_input is not None:
            if not user_input.get(CONF_CONFIRM):
                errors["base"] = "confirmation_required"
            elif (
                self._zha_plan
                and self._zha_plan.failed_reads
                and not user_input.get("accept_partial")
            ):
                errors["base"] = "zha_incomplete"
            else:
                if self._zha_plan is not None:
                    self._operation_task = self.hass.async_create_task(
                        self.manager.async_replace(
                            plan,
                            self._zha_plan,
                            accept_partial=user_input.get("accept_partial", False),
                        ),
                        "Copy ZHA configuration and replace light",
                    )
                    return await self.async_step_execute()
                try:
                    await self.manager.async_replace(plan)
                except ReplacementError as err:
                    errors["base"] = err.code
                except Exception:
                    _LOGGER.exception("Could not persist replacement journal")
                    errors["base"] = "storage_failed"
                else:
                    return self.async_create_entry(title="", data={})
        fields = {
            vol.Required(CONF_CONFIRM, default=False): selector.BooleanSelector(),
        }
        if self._zha_plan and self._zha_plan.failed_reads:
            fields[vol.Required("accept_partial", default=False)] = (
                selector.BooleanSelector()
            )
        placeholders = {
            "old": plan.old.entity_id,
            "new": plan.new.entity_id,
            "archived": plan.archived_id,
            "name": plan.name,
            "area": plan.area_id or "—",
            "blockers": ", ".join(plan.blockers) or "—",
            "capabilities": ", ".join(plan.warnings) or "—",
        }
        if self._zha_plan:
            placeholders.update(
                {
                    "zha_bindings": "\n".join(
                        b.describe() for b in self._zha_plan.bindings
                    )
                    or "—",
                    "zha_groups": ", ".join(
                        f"0x{group:04x}" for group in self._zha_plan.groups
                    )
                    or "—",
                    "zha_missing": "\n".join(self._zha_plan.failed_reads) or "—",
                    "zha_blockers": "\n".join(self._zha_plan.blockers) or "—",
                }
            )
        return self.async_show_form(
            step_id="confirm_zha" if self._zha_plan else "confirm",
            data_schema=vol.Schema(fields),
            description_placeholders=placeholders,
            errors=errors,
        )

    async def async_step_execute(
        self, user_input=None
    ) -> config_entries.ConfigFlowResult:
        assert self._operation_task is not None
        if not self._operation_task.done():
            return self.async_show_progress(
                step_id="execute",
                progress_action="execute_zha",
                progress_task=self._operation_task,
            )
        try:
            self._operation_task.result()
        except ReplacementError as err:
            self._progress_error = err.code
        except Exception:
            _LOGGER.exception("ZHA replacement or recovery failed")
            self._progress_error = "recovery_required"
        else:
            return self.async_show_progress_done(next_step_id="operation_done")
        return self.async_show_progress_done(next_step_id="operation_failed")

    async def async_step_operation_done(
        self, user_input=None
    ) -> config_entries.ConfigFlowResult:
        return self.async_create_entry(title="", data={})

    async def async_step_operation_failed(
        self, user_input=None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_init()
        return self.async_show_form(
            step_id="operation_failed",
            data_schema=vol.Schema({}),
            errors={"base": self._progress_error or "recovery_required"},
        )

    async def async_step_restore(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors = {}
        if user_input is not None:
            if not user_input.get(CONF_CONFIRM):
                errors["base"] = "confirmation_required"
            else:
                if self.manager.journal and "zha" in self.manager.journal:
                    self._operation_task = self.hass.async_create_task(
                        self.manager.async_restore(), "Restore ZHA replacement"
                    )
                    return await self.async_step_execute()
                try:
                    await self.manager.async_restore()
                except ReplacementError as err:
                    errors["base"] = err.code
                except Exception:
                    _LOGGER.exception("Could not restore the replacement")
                    errors["base"] = "recovery_required"
                else:
                    return self.async_create_entry(title="", data={})
        journal = self.manager.journal or {}
        return self.async_show_form(
            step_id="restore",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CONFIRM, default=False
                    ): selector.BooleanSelector(),
                }
            ),
            description_placeholders={
                "old": journal.get("old", {}).get("entity_id", "—"),
                "new": journal.get("new", {}).get("entity_id", "—"),
                "status": journal.get("status", "—"),
            },
            errors=errors,
        )
