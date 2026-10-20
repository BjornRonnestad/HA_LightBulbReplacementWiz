"""Config and options flows for the replacement wizard."""

import logging
from typing import Any

import probatio as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import CONF_CONFIRM, CONF_NEW, CONF_OLD, DOMAIN, TITLE
from .replacement import ReplacementError, ReplacementManager, ReplacementPlan

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

    _plan: ReplacementPlan | None = None

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
                }
            ),
            errors=errors,
        )

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        assert self._plan is not None
        plan = self._plan
        errors = {}
        if user_input is not None:
            if not user_input.get(CONF_CONFIRM):
                errors["base"] = "confirmation_required"
            else:
                try:
                    await self.manager.async_replace(plan)
                except ReplacementError as err:
                    errors["base"] = err.code
                except Exception:
                    _LOGGER.exception("Could not persist replacement journal")
                    errors["base"] = "storage_failed"
                else:
                    return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CONFIRM, default=False
                    ): selector.BooleanSelector(),
                }
            ),
            description_placeholders={
                "old": plan.old.entity_id,
                "new": plan.new.entity_id,
                "archived": plan.archived_id,
                "name": plan.name,
                "area": plan.area_id or "—",
                "blockers": ", ".join(plan.blockers) or "—",
                "capabilities": ", ".join(plan.warnings) or "—",
            },
            errors=errors,
        )

    async def async_step_restore(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors = {}
        if user_input is not None:
            if not user_input.get(CONF_CONFIRM):
                errors["base"] = "confirmation_required"
            else:
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
