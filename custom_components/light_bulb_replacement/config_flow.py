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
    from .z2m import Z2mMigration, Z2mPlan
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
        self._z2m_plan: Z2mPlan | None = None
        self._backend = "zha"
        self._z2m_settings: dict[str, Any] = {}
        self._migration: ZhaMigration | Z2mMigration | None = None
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
                self._z2m_plan = None
                self._progress_error = None
                self._scan_task = None
                if user_input.get("copy_zha") and user_input.get("copy_z2m"):
                    raise ReplacementError("binding_backend_conflict")
                if user_input.get("copy_z2m"):
                    self._backend = "z2m"
                    return await self.async_step_z2m()
                if user_input.get("copy_zha"):
                    self._backend = "zha"
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
                    vol.Optional("copy_z2m", default=False): selector.BooleanSelector(),
                }
            ),
            errors=errors,
        )

    async def async_step_z2m(self, user_input=None) -> config_entries.ConfigFlowResult:
        errors = {}
        if user_input is not None:
            try:
                from .z2m import Z2mMigration

                self._migration = Z2mMigration(
                    self.hass, user_input.get("base_topic", "zigbee2mqtt")
                )
                for key in ("old_endpoint", "new_endpoint"):
                    if key in user_input and (
                        not isinstance(user_input[key], (int, float))
                        or not 1 <= user_input[key] <= 240
                        or int(user_input[key]) != user_input[key]
                    ):
                        raise ReplacementError("z2m_endpoint_required")
                self._z2m_settings = {
                    key: int(user_input[key])
                    for key in ("old_endpoint", "new_endpoint")
                    if key in user_input
                }
                return await self.async_step_scan_z2m()
            except ReplacementError as err:
                errors["base"] = err.code
        return self.async_show_form(
            step_id="z2m",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "base_topic", default="zigbee2mqtt"
                    ): selector.TextSelector(),
                    **{
                        vol.Optional(key): selector.NumberSelector(
                            selector.NumberSelectorConfig(
                                min=1,
                                max=240,
                                step=1,
                                mode=selector.NumberSelectorMode.BOX,
                            )
                        )
                        for key in ("old_endpoint", "new_endpoint")
                    },
                }
            ),
            errors=errors,
        )

    async def async_step_scan_z2m(self, user_input=None):
        return await self.async_step_scan(user_input)

    async def async_step_scan(self, user_input=None) -> config_entries.ConfigFlowResult:
        assert self._plan is not None and self._migration is not None
        if self._scan_task is None:
            settings = self._z2m_settings if self._backend == "z2m" else {}
            self._scan_task = self.hass.async_create_task(
                self._migration.async_scan(self._plan.old, self._plan.new, **settings),
                "Discover Zigbee bindings",
            )
        if not self._scan_task.done():
            return self.async_show_progress(
                step_id="scan_z2m" if self._backend == "z2m" else "scan",
                progress_action=f"scan_{self._backend}",
                progress_task=self._scan_task,
            )
        try:
            radio_plan = self._scan_task.result()
        except ReplacementError as err:
            self._progress_error = err.code
        except Exception:
            _LOGGER.exception("Zigbee binding discovery failed")
            self._progress_error = f"{self._backend}_read_failed"
        else:
            if self._backend == "z2m":
                self._z2m_plan = radio_plan
            else:
                self._zha_plan = radio_plan
            return self.async_show_progress_done(
                next_step_id=f"confirm_{self._backend}"
            )
        return self.async_show_progress_done(next_step_id="operation_failed")

    async def async_step_confirm_zha(self, user_input=None):
        return await self.async_step_confirm(user_input)

    async def async_step_confirm_z2m(self, user_input=None):
        return await self.async_step_confirm(user_input)

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        assert self._plan is not None
        plan = self._plan
        radio_plan = self._zha_plan or self._z2m_plan
        errors = {}
        if user_input is not None:
            if not user_input.get(CONF_CONFIRM):
                errors["base"] = "confirmation_required"
            elif (
                radio_plan
                and radio_plan.failed_reads
                and not user_input.get("accept_partial")
            ):
                errors["base"] = f"{self._backend}_incomplete"
            else:
                if radio_plan is not None:
                    self._operation_task = self.hass.async_create_task(
                        self.manager.async_replace(
                            plan,
                            self._zha_plan,
                            accept_partial=user_input.get("accept_partial", False),
                            z2m_plan=self._z2m_plan,
                        ),
                        "Copy Zigbee configuration and replace light",
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
        if radio_plan and radio_plan.failed_reads:
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
        if radio_plan:
            placeholders.update(
                {
                    f"{self._backend}_bindings": "\n".join(
                        b.describe() for b in radio_plan.bindings
                    )
                    or "—",
                    f"{self._backend}_groups": ", ".join(
                        f"0x{group:04x}" for group in radio_plan.groups
                    )
                    or "—",
                    f"{self._backend}_missing": "\n".join(radio_plan.failed_reads)
                    or "—",
                    f"{self._backend}_blockers": "\n".join(radio_plan.blockers) or "—",
                }
            )
        return self.async_show_form(
            step_id=f"confirm_{self._backend}" if radio_plan else "confirm",
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
                progress_action=f"execute_{self._backend}",
                progress_task=self._operation_task,
            )
        try:
            self._operation_task.result()
        except ReplacementError as err:
            self._progress_error = err.code
        except Exception:
            _LOGGER.exception("Zigbee replacement or recovery failed")
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
                if self.manager.journal and any(
                    key in self.manager.journal for key in ("zha", "z2m")
                ):
                    self._backend = "z2m" if "z2m" in self.manager.journal else "zha"
                    self._operation_task = self.hass.async_create_task(
                        self.manager.async_restore(), "Restore Zigbee replacement"
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
