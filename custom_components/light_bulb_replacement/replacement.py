"""Registry based replacement; never edit YAML or Home Assistant storage files."""

import asyncio
import logging
from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.automation import automations_with_device
from homeassistant.components.script import scripts_with_device
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store

from .const import DOMAIN

if TYPE_CHECKING:
    from .z2m import Z2mPlan
    from .zha import ZhaPlan

_LOGGER = logging.getLogger(__name__)
_FIELDS = ("name", "area_id", "icon", "labels", "aliases", "disabled_by")


class ReplacementError(Exception):
    """An error suitable for translation in a config flow."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ReplacementPlan:
    """Immutable selection, checked again immediately before execution."""

    old: er.RegistryEntry
    new: er.RegistryEntry
    archived_id: str
    name: str
    area_id: str | None
    labels: frozenset[str]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]


def _contains(value: Any, needle: str) -> bool:
    """Find exact identifiers in nested configuration, including mapping keys."""
    if isinstance(value, str):
        return value == needle
    if isinstance(value, Mapping):
        return any(
            _contains(k, needle) or _contains(v, needle) for k, v in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains(item, needle) for item in value)
    return False


def _snapshot(entry: er.RegistryEntry) -> dict[str, Any]:
    """Save only the user settings this integration changes."""
    return {
        "registry_id": entry.id,
        "entity_id": entry.entity_id,
        "name": entry.name,
        "area_id": entry.area_id,
        "icon": entry.icon,
        "labels": sorted(entry.labels),
        "aliases": [None if a is er.COMPUTED_NAME else a for a in entry.aliases],
        "disabled_by": entry.disabled_by.value if entry.disabled_by else None,
    }


def _settings(snapshot: dict[str, Any]) -> dict[str, Any]:
    values = {field: snapshot[field] for field in _FIELDS}
    values["labels"] = set(values["labels"])
    values["aliases"] = [
        er.COMPUTED_NAME if a is None else a for a in values["aliases"]
    ]
    if values["disabled_by"]:
        values["disabled_by"] = er.RegistryEntryDisabler(values["disabled_by"])
    return values


class ReplacementManager:
    """Serialize replacements and retain a crash recovery journal."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.lock = asyncio.Lock()
        self.store: Store[dict[str, Any]] = Store(hass, 1, DOMAIN)
        self.journal: dict[str, Any] | None = None

    async def async_load(self) -> None:
        self.journal = await self.store.async_load()

    @callback
    def preview(self, old_id: str, new_id: str) -> ReplacementPlan:
        """Validate both registry entries and the new bulb's live state."""
        if self.journal and self.journal["status"] in ("pending", "recovery_required"):
            raise ReplacementError("recovery_required")
        registry = er.async_get(self.hass)
        old, new = registry.async_get(old_id), registry.async_get(new_id)
        if old_id == new_id:
            raise ReplacementError("same_entity")
        if not old or not new or old.domain != "light" or new.domain != "light":
            raise ReplacementError("invalid_light")
        if new.disabled or new.platform == "group" or old.platform == "group":
            raise ReplacementError("invalid_light")
        state = self.hass.states.get(new_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            raise ReplacementError("new_unavailable")
        archived_id = f"{old_id}_old"
        if registry.async_get(archived_id) or not self.hass.states.async_available(
            archived_id
        ):
            raise ReplacementError("archive_exists")

        blockers: set[str] = set()
        # Device IDs remain bound to the original hardware. Do not blindly rewrite
        # device triggers: their type/subtype may not exist on the replacement.
        if old.device_id and old.device_id != new.device_id:
            blockers.update(automations_with_device(self.hass, old.device_id))
            blockers.update(scripts_with_device(self.hass, old.device_id))
        # Some helpers store the registry UUID instead of the entity ID.
        # Those would follow the archived hardware, so require manual conversion.
        for entry in self.hass.config_entries.async_entries():
            if entry.domain != DOMAIN and (
                _contains(entry.options, old.id) or _contains(entry.data, old.id)
            ):
                blockers.add(f"{entry.domain}: {entry.title}")

        warnings: list[str] = []
        old_state = self.hass.states.get(old_id)
        if old_state:
            old_modes = set(old_state.attributes.get("supported_color_modes", []))
            new_modes = set(state.attributes.get("supported_color_modes", []))
            if (
                old_modes != new_modes
                or old.supported_features != new.supported_features
            ):
                warnings.append("capabilities")

        # Copy effective area/device labels to the entity only. Never move or
        # rename a shared device (e.g. a multichannel light controller).
        devices = dr.async_get(self.hass)
        old_device = devices.async_get(old.device_id) if old.device_id else None
        new_device = devices.async_get(new.device_id) if new.device_id else None
        area_id = er.async_get_effective_area_id(self.hass, old)
        if (
            area_id is None
            and new_device
            and dr.async_get_effective_area_id(self.hass, new_device)
        ):
            raise ReplacementError("new_device_area")
        labels = old.labels | new.labels | (old_device.labels if old_device else set())
        name = er.async_get_full_entity_name(self.hass, old) or old_id
        return ReplacementPlan(
            old,
            new,
            archived_id,
            name,
            area_id,
            frozenset(labels),
            tuple(sorted(blockers)),
            tuple(warnings),
        )

    async def _wait_released(self, entity_id: str) -> None:
        """Let the owning entity process the registry rename and release its ID."""
        released = asyncio.Event()

        @callback
        def state_changed(_event) -> None:
            if self.hass.states.async_available(entity_id):
                released.set()

        unsubscribe = async_track_state_change_event(
            self.hass, [entity_id], state_changed
        )
        try:
            if not self.hass.states.async_available(entity_id):
                async with asyncio.timeout(10):
                    await released.wait()
        finally:
            unsubscribe()

    async def async_replace(
        self,
        plan: ReplacementPlan,
        zha_plan: ZhaPlan | None = None,
        *,
        accept_partial: bool = False,
        z2m_plan: Z2mPlan | None = None,
    ) -> None:
        """Save recovery information before either rename; roll back on failure."""
        async with self.lock, AsyncExitStack() as stack:
            current = self.preview(plan.old.entity_id, plan.new.entity_id)
            if current != plan:
                raise ReplacementError("selection_changed")
            if plan.blockers:
                raise ReplacementError("unsupported_references")
            migration = None
            radio_key = "zha"
            if zha_plan is not None and z2m_plan is not None:
                raise ReplacementError("binding_backend_conflict")
            if zha_plan is not None:
                from .zha import ZhaMigration

                migration = ZhaMigration(self.hass)
                migration.validate(zha_plan, plan.old, plan.new)
                if zha_plan.failed_reads and not accept_partial:
                    raise ReplacementError("zha_incomplete")
            if z2m_plan is not None:
                from .z2m import Z2mMigration

                radio_key = "z2m"
                migration = await stack.enter_async_context(
                    Z2mMigration(self.hass, z2m_plan.base_topic)
                )
                migration.validate(z2m_plan, plan.old, plan.new)
                if not accept_partial:
                    raise ReplacementError("z2m_incomplete")
            registry = er.async_get(self.hass)
            journal = {
                "status": "pending",
                "old": _snapshot(plan.old),
                "new": _snapshot(plan.new),
                "archived_id": plan.archived_id,
            }
            if zha_plan is not None:
                journal["zha"] = zha_plan.journal()
            if z2m_plan is not None:
                journal["z2m"] = z2m_plan.journal()
            # If this fails, the registry is still unchanged.
            await self.store.async_save(journal)
            # Saving performs I/O: someone may have edited either entity while
            # the journal was being written. Do not apply a stale selection.
            try:
                unchanged = self.preview(plan.old.entity_id, plan.new.entity_id) == plan
            except ReplacementError:
                unchanged = False
            if not unchanged:
                journal["status"] = "restored"
                self.journal = journal
                await self.store.async_save(journal)
                raise ReplacementError("selection_changed")
            self.journal = journal
            try:
                if migration is not None:
                    await migration.async_apply(journal[radio_key], self._save_journal)
                    migration.check_network(journal[radio_key])
                    # Radio commands may take minutes. Validate the entity selection
                    # once more before touching the registry.
                    if (
                        registry.async_get(plan.old.entity_id) != plan.old
                        or registry.async_get(plan.new.entity_id) != plan.new
                    ):
                        raise ReplacementError("selection_changed")
                registry.async_update_entity(
                    plan.old.entity_id,
                    new_entity_id=plan.archived_id,
                    name=f"{plan.name}_OLD",
                    aliases=[],
                    labels=set(),
                )
                await self._wait_released(plan.old.entity_id)
                if registry.async_get(plan.new.entity_id) != plan.new:
                    raise ReplacementError("selection_changed")
                registry.async_update_entity(
                    plan.new.entity_id,
                    new_entity_id=plan.old.entity_id,
                    name=plan.name,
                    area_id=plan.area_id,
                    icon=plan.old.icon,
                    labels=set(plan.labels),
                    aliases=list(plan.old.aliases),
                )
                await self._wait_released(plan.new.entity_id)
                registry.async_update_entity(
                    plan.archived_id, disabled_by=er.RegistryEntryDisabler.USER
                )
                self.journal["status"] = "completed"
                await self.store.async_save(self.journal)
            except Exception as err:
                _LOGGER.exception(
                    "Replacement failed; restoring original entity settings"
                )
                try:
                    await self._restore()
                except Exception:
                    self.journal["status"] = "recovery_required"
                    _LOGGER.exception("Recovery needs attention; journal retained")
                    await self.store.async_save(self.journal)
                    raise ReplacementError("recovery_required") from err
                code = (
                    err.code
                    if isinstance(err, ReplacementError)
                    else "replacement_failed"
                )
                raise ReplacementError(code) from err

    async def _save_journal(self) -> None:
        assert self.journal is not None
        await self.store.async_save(self.journal)

    async def _restore(self) -> None:
        """Restore IDs only if they still belong to the recorded registry entries."""
        assert self.journal is not None
        registry = er.async_get(self.hass)
        snapshots = [self.journal["new"], self.journal["old"]]
        entries: list[er.RegistryEntry] = []
        expected_locations = {
            self.journal["old"]["entity_id"],
            self.journal["new"]["entity_id"],
            self.journal["archived_id"],
        }
        for snapshot in snapshots:
            entry = registry.async_get(snapshot["registry_id"])
            if entry is None or entry.entity_id not in expected_locations:
                raise ReplacementError("recovery_conflict")
            entries.append(entry)
        for snapshot in snapshots:
            occupant = registry.async_get(snapshot["entity_id"])
            if occupant and occupant.id not in {entry.id for entry in entries}:
                raise ReplacementError("recovery_conflict")
        # New bulb first, to release the original ID for the old bulb.
        for snapshot, entry in zip(snapshots, entries, strict=True):
            if entry.entity_id != snapshot["entity_id"]:
                await self._wait_released(snapshot["entity_id"])
            registry.async_update_entity(
                entry.entity_id,
                new_entity_id=snapshot["entity_id"],
                **_settings(snapshot),
            )
            if entry.entity_id != snapshot["entity_id"]:
                await self._wait_released(entry.entity_id)
        if "zha" in self.journal:
            from .zha import ZhaMigration

            await ZhaMigration(self.hass).async_undo(
                self.journal["zha"], self._save_journal
            )
        if "z2m" in self.journal:
            from .z2m import Z2mMigration

            async with Z2mMigration(
                self.hass, self.journal["z2m"]["base_topic"]
            ) as z2m:
                await z2m.async_undo(self.journal["z2m"], self._save_journal)
        self.journal["status"] = "restored"
        await self.store.async_save(self.journal)

    async def async_restore(self) -> None:
        """Undo the last replacement or recover an interrupted replacement."""
        async with self.lock:
            if not self.journal or self.journal["status"] == "restored":
                raise ReplacementError("nothing_to_restore")
            # Recovery can itself be interrupted after restoring the first ID.
            # Keep further replacements blocked until both entries are restored.
            self.journal["status"] = "recovery_required"
            await self.store.async_save(self.journal)
            await self._restore()
