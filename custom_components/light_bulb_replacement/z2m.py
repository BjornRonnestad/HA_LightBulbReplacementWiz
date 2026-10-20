"""Copy Zigbee2MQTT bindings through Home Assistant's existing MQTT client.

Discovery and verification use Zigbee2MQTT's bridge metadata, not a radio-level
binding-table read. Every write requires a correlated command response and a
matching bridge update. Original bindings and group memberships are retained.
"""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from typing import Any
from uuid import uuid4

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .replacement import ReplacementError

READ_TIMEOUT = 15
COMMAND_TIMEOUT = 60
STANDARD_CLUSTERS = {
    "genBasic",
    "genPowerCfg",
    "genIdentify",
    "genGroups",
    "genScenes",
    "genOnOff",
    "genLevelCtrl",
    "genOta",
    "lightingColorCtrl",
}
CACHED_DISCOVERY = (
    "Discovery uses Zigbee2MQTT's cached binding and group records. "
    "Unrecorded or stale device bindings cannot be detected or verified."
)


@dataclass(frozen=True)
class Endpoint:
    ieee: str
    endpoint: int


@dataclass(frozen=True)
class Binding:
    source: str
    endpoint: int
    cluster: str
    target: str | None = None
    target_endpoint: int | None = None
    group: int | None = None

    def copy_to(self, old: Endpoint, new: Endpoint) -> Binding:
        result = self
        if Endpoint(self.source, self.endpoint) == old:
            result = replace(result, source=new.ieee, endpoint=new.endpoint)
        if self.target and Endpoint(self.target, self.target_endpoint) == old:
            result = replace(result, target=new.ieee, target_endpoint=new.endpoint)
        return result

    def describe(self) -> str:
        target = (
            f"group 0x{self.group:04x}"
            if self.group is not None
            else f"{self.target}/{self.target_endpoint}"
        )
        return f"{self.source}/{self.endpoint} → {target} ({self.cluster})"


@dataclass(frozen=True)
class Z2mPlan:
    base_topic: str
    network: str
    old: Endpoint
    new: Endpoint
    bindings: tuple[Binding, ...]
    groups: tuple[int, ...]
    failed_reads: tuple[str, ...]
    blockers: tuple[str, ...]

    def journal(self) -> dict[str, Any]:
        return {
            "base_topic": self.base_topic,
            "network": self.network,
            "old": asdict(self.old),
            "new": asdict(self.new),
            "operations": [
                {"kind": "binding", "binding": asdict(binding), "owned": False}
                for binding in self.bindings
            ]
            + [
                {"kind": "group", **asdict(self.new), "group": group, "owned": False}
                for group in self.groups
            ],
        }


class Z2mMigration:
    """Use one explicit bridge; never open a second MQTT or radio connection."""

    def __init__(self, hass: HomeAssistant, base_topic: str = "zigbee2mqtt") -> None:
        self.hass = hass
        if (
            not isinstance(base_topic, str)
            or not base_topic.strip("/")
            or any(c in base_topic for c in ("+", "#", "\x00"))
            or base_topic != base_topic.strip().strip("/")
        ):
            raise ReplacementError("z2m_invalid_topic")
        if "mqtt" not in hass.config.components:
            raise ReplacementError("z2m_unavailable")
        from homeassistant.components import mqtt

        self.mqtt = mqtt
        self.base_topic = base_topic
        self._data: dict[str, Any] = {}
        self._invalid: set[str] = set()
        self._revisions: dict[str, int] = {}
        self._changed = asyncio.Event()
        self._pending: dict[str, tuple[str, asyncio.Future]] = {}
        self._unsubscribe: Callable[[], None] | None = None
        self._network: str | None = None

    def _check_mqtt(self) -> None:
        if "mqtt" not in self.hass.config.components or not self.mqtt.is_connected(
            self.hass
        ):
            raise ReplacementError("z2m_unavailable")

    @callback
    def _receive(self, message) -> None:
        topic = message.topic.removeprefix(f"{self.base_topic}/bridge/")
        try:
            data = json.loads(message.payload)
        except (TypeError, ValueError):
            if topic in {"state", "info", "devices", "groups"}:
                self._invalid.add(topic)
                self._changed.set()
            return
        if topic in {"state", "info", "devices", "groups"}:
            self._data[topic] = data
            self._revisions[topic] = self._revisions.get(topic, 0) + 1
            self._invalid.discard(topic)
            self._changed.set()
        elif topic.startswith("response/") and not message.retain:
            if not isinstance(data, dict):
                return
            transaction = data.get("transaction")
            if not isinstance(transaction, str):
                return
            if pending := self._pending.get(transaction):
                command, future = pending
                if topic == f"response/{command}" and not future.done():
                    future.set_result(data)

    async def __aenter__(self):
        self._check_mqtt()
        try:
            self._unsubscribe = await self.mqtt.async_subscribe(
                self.hass, f"{self.base_topic}/bridge/#", self._receive, qos=0
            )
            async with asyncio.timeout(READ_TIMEOUT):
                while not {"state", "info", "devices", "groups"} <= self._data.keys():
                    if self._invalid:
                        raise ReplacementError("z2m_invalid_data")
                    self._changed.clear()
                    await self._changed.wait()
            self._network = self.network
            # A retained 'online' message alone does not prove the bridge is alive.
            result = await self._request("health_check", {})
            if result.get("healthy") is not True:
                raise ReplacementError("z2m_unavailable")
            return self
        except TimeoutError as err:
            self.close()
            raise ReplacementError("z2m_timeout") from err
        except BaseException:
            self.close()
            raise

    async def __aexit__(self, *args):
        self.close()

    def close(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        for _, future in self._pending.values():
            future.cancel()
        self._pending.clear()

    @property
    def network(self) -> str:
        self._check_mqtt()
        if self._invalid:
            raise ReplacementError("z2m_invalid_data")
        state = self._data.get("state")
        if not isinstance(state, dict) or state.get("state") != "online":
            raise ReplacementError("z2m_unavailable")
        try:
            info = self._data["info"]
            coordinator = info["coordinator"]["ieee_address"]
            pan = info["network"]["extended_pan_id"]
            if (
                not isinstance(coordinator, str)
                or not re.fullmatch(r"0x[0-9a-fA-F]{16}", coordinator)
                or not isinstance(pan, str)
                or not re.fullmatch(r"0x[0-9a-fA-F]{16}", pan)
            ):
                raise ValueError
            return f"{self.base_topic}/{coordinator}/{pan}"
        except (KeyError, TypeError, ValueError) as err:
            raise ReplacementError("z2m_invalid_data") from err

    def check_network(self, journal: dict[str, Any]) -> None:
        if self.network != journal["network"]:
            raise ReplacementError("z2m_network_changed")

    async def _request(self, command: str, payload: dict) -> dict:
        if self.network != self._network:
            raise ReplacementError("z2m_network_changed")
        transaction = uuid4().hex
        future = self.hass.loop.create_future()
        self._pending[transaction] = (command, future)
        try:
            async with asyncio.timeout(COMMAND_TIMEOUT):
                await self.mqtt.async_publish(
                    self.hass,
                    f"{self.base_topic}/bridge/request/{command}",
                    json.dumps({**payload, "transaction": transaction}),
                    qos=0,
                    retain=False,
                )
                response = await future
            if self.network != self._network:
                raise ReplacementError("z2m_network_changed")
            if response.get("status") != "ok" or not isinstance(
                response.get("data"), dict
            ):
                raise ReplacementError("z2m_write_failed")
            return response["data"]
        except TimeoutError as err:
            raise ReplacementError("z2m_timeout") from err
        finally:
            self._pending.pop(transaction, None)
            if not future.done():
                future.cancel()

    def devices(self) -> dict[str, dict]:
        try:
            devices = self._data["devices"]
            if not isinstance(devices, list):
                raise ValueError
            result = {}
            for device in devices:
                ieee = device["ieee_address"]
                if not re.fullmatch(r"0x[0-9a-fA-F]{16}", ieee) or ieee in result:
                    raise ValueError
                if not isinstance(device["endpoints"], dict):
                    raise ValueError
                for key, endpoint in device["endpoints"].items():
                    if not 1 <= int(key) <= 255 or not isinstance(endpoint, dict):
                        raise ValueError
                    if not isinstance(endpoint["bindings"], list):
                        raise ValueError
                    for side in ("input", "output"):
                        clusters = endpoint["clusters"][side]
                        if not isinstance(clusters, list) or not all(
                            isinstance(cluster, str) for cluster in clusters
                        ):
                            raise ValueError
                result[ieee] = device
            return result
        except (KeyError, TypeError, ValueError) as err:
            raise ReplacementError("z2m_invalid_data") from err

    def endpoint(self, ref: Endpoint) -> dict:
        try:
            return self.devices()[ref.ieee]["endpoints"][str(ref.endpoint)]
        except KeyError as err:
            raise ReplacementError("z2m_device_missing") from err

    def resolve(self, entry: er.RegistryEntry, endpoint: int | None = None) -> Endpoint:
        if entry.platform != "mqtt" or entry.domain != "light" or not entry.device_id:
            raise ReplacementError("z2m_lights_required")
        device = dr.async_get(self.hass).async_get(entry.device_id)
        if not device:
            raise ReplacementError("z2m_device_missing")
        addresses = [
            value.removeprefix("zigbee2mqtt_")
            for domain, value in device.identifiers
            if domain == "mqtt"
            and re.fullmatch(r"zigbee2mqtt_0x[0-9a-fA-F]{16}", value)
        ]
        if len(addresses) != 1 or addresses[0] not in self.devices():
            raise ReplacementError("z2m_lights_required")
        ieee = addresses[0]
        if not (
            entry.unique_id.startswith(f"{ieee}_light_")
            and entry.unique_id.endswith(f"_{self.base_topic}")
        ):
            raise ReplacementError("z2m_lights_required")
        endpoints = self.devices()[ieee]["endpoints"]
        try:
            candidates = [
                int(key)
                for key, data in endpoints.items()
                if "genOnOff" in data["clusters"]["input"] and 1 <= int(key) <= 240
            ]
            named = [
                key
                for key in candidates
                if (name := endpoints[str(key)].get("name"))
                and entry.unique_id == f"{ieee}_light_{name}_{self.base_topic}"
            ]
        except (KeyError, TypeError, ValueError) as err:
            raise ReplacementError("z2m_invalid_data") from err
        if endpoint is not None:
            if endpoint not in candidates or (named and endpoint not in named):
                raise ReplacementError("z2m_endpoint_required")
        elif len(named) == 1:
            endpoint = named[0]
        elif len(candidates) == 1:
            endpoint = candidates[0]
        else:
            raise ReplacementError("z2m_endpoint_required")
        return Endpoint(ieee, endpoint)

    def bindings(self) -> set[Binding]:
        result = set()
        try:
            for ieee, device in self.devices().items():
                for endpoint, data in device["endpoints"].items():
                    for raw in data["bindings"]:
                        target = raw["target"]
                        if not isinstance(raw["cluster"], str):
                            raise ValueError
                        if target["type"] == "endpoint":
                            binding = Binding(
                                ieee,
                                int(endpoint),
                                raw["cluster"],
                                target["ieee_address"],
                                int(target["endpoint"]),
                            )
                        elif target["type"] == "group":
                            binding = Binding(
                                ieee,
                                int(endpoint),
                                raw["cluster"],
                                group=int(target["id"]),
                            )
                        else:
                            raise ValueError
                        result.add(binding)
            return result
        except (KeyError, TypeError, ValueError) as err:
            raise ReplacementError("z2m_invalid_data") from err

    def groups(self, ref: Endpoint) -> set[int]:
        try:
            groups = self._data["groups"]
            if not isinstance(groups, list):
                raise ValueError
            return {
                int(group["id"])
                for group in groups
                if any(
                    member["ieee_address"] == ref.ieee
                    and int(member["endpoint"]) == ref.endpoint
                    for member in group["members"]
                )
            }
        except (KeyError, TypeError, ValueError) as err:
            raise ReplacementError("z2m_invalid_data") from err

    def make_plan(
        self,
        old_entry: er.RegistryEntry,
        new_entry: er.RegistryEntry,
        old_endpoint: int | None = None,
        new_endpoint: int | None = None,
    ) -> Z2mPlan:
        old = self.resolve(old_entry, old_endpoint)
        new = self.resolve(new_entry, new_endpoint)
        if old.ieee == new.ieee:
            raise ReplacementError("z2m_lights_required")
        old_ep, new_ep = self.endpoint(old), self.endpoint(new)
        bindings: set[Binding] = set()
        blockers: set[str] = set()
        try:
            for original in self.bindings():
                copied = original.copy_to(old, new)
                if copied == original:
                    continue
                try:
                    self.endpoint(Endpoint(copied.source, copied.endpoint))
                    if copied.target:
                        self.endpoint(Endpoint(copied.target, copied.target_endpoint))
                except ReplacementError:
                    blockers.add(original.describe())
                    continue
                sides = [
                    side
                    for side in ("input", "output")
                    if original.cluster in old_ep["clusters"][side]
                ]
                if (
                    original.cluster not in STANDARD_CLUSTERS
                    or not sides
                    or any(original.cluster not in new_ep["clusters"][s] for s in sides)
                ):
                    blockers.add(original.describe())
                else:
                    bindings.add(copied)
            groups = self.groups(old)
            if groups and "genGroups" not in new_ep["clusters"]["input"]:
                blockers.add("Groups cluster genGroups")
        except (KeyError, TypeError) as err:
            raise ReplacementError("z2m_invalid_data") from err
        return Z2mPlan(
            self.base_topic,
            self.network,
            old,
            new,
            tuple(sorted(bindings, key=lambda b: b.describe())),
            tuple(sorted(groups)),
            (CACHED_DISCOVERY,),
            tuple(sorted(blockers)),
        )

    async def async_scan(
        self, old_entry, new_entry, old_endpoint=None, new_endpoint=None
    ):
        async with self:
            return self.make_plan(old_entry, new_entry, old_endpoint, new_endpoint)

    def validate(self, plan: Z2mPlan, old, new) -> None:
        self.check_network(plan.journal())
        current = self.make_plan(old, new, plan.old.endpoint, plan.new.endpoint)
        if current.blockers:
            raise ReplacementError("z2m_incompatible")
        if current != plan:
            raise ReplacementError("selection_changed")

    def present(self, operation: dict) -> bool:
        if operation["kind"] == "binding":
            return Binding(**operation["binding"]) in self.bindings()
        return operation["group"] in self.groups(
            Endpoint(operation["ieee"], operation["endpoint"])
        )

    async def write(self, operation: dict, *, remove: bool) -> None:
        if operation["kind"] == "binding":
            binding = Binding(**operation["binding"])
            command = "device/unbind" if remove else "device/bind"
            payload = {
                "from": binding.source,
                "from_endpoint": binding.endpoint,
                "to": binding.target if binding.group is None else binding.group,
                "clusters": [binding.cluster],
                "skip_disable_reporting": True,
            }
            if binding.target_endpoint is not None:
                payload["to_endpoint"] = binding.target_endpoint
        else:
            command = f"group/members/{'remove' if remove else 'add'}"
            payload = {
                "device": operation["ieee"],
                "endpoint": operation["endpoint"],
                "group": str(operation["group"]),
                "skip_disable_reporting": True,
            }
        metadata = "devices" if operation["kind"] == "binding" else "groups"
        revision = self._revisions.get(metadata, 0)
        data = await self._request(command, payload)
        echoed = {
            key: value
            for key, value in payload.items()
            if key not in {"clusters", "skip_disable_reporting"}
        }
        if any(data.get(key) != value for key, value in echoed.items()):
            raise ReplacementError("z2m_write_failed")
        if operation["kind"] == "binding" and (
            data.get("failed") != []
            or not isinstance(data.get("clusters"), list)
            or binding.cluster not in data["clusters"]
        ):
            raise ReplacementError("z2m_write_failed")
        # Wait for bridge/devices or bridge/groups, not merely the response status.
        try:
            async with asyncio.timeout(READ_TIMEOUT):
                while True:
                    if self.network != self._network:
                        raise ReplacementError("z2m_network_changed")
                    if (
                        self._revisions.get(metadata, 0) > revision
                        and self.present(operation) != remove
                    ):
                        return
                    self._changed.clear()
                    await self._changed.wait()
        except TimeoutError as err:
            raise ReplacementError("z2m_verification_failed") from err

    async def async_apply(self, journal: dict, save: Callable[[], Awaitable[None]]):
        self.check_network(journal)
        for operation in journal["operations"]:
            operation["preexisting"] = self.present(operation)
        await save()
        for operation in journal["operations"]:
            self.check_network(journal)
            if operation["preexisting"] or self.present(operation):
                continue
            operation["owned"] = True
            await save()
            self.check_network(journal)
            await self.write(operation, remove=False)
            operation["verified"] = True
            await save()
        for operation in journal["operations"]:
            if not self.present(operation):
                raise ReplacementError("z2m_verification_failed")

    async def async_undo(self, journal: dict, save: Callable[[], Awaitable[None]]):
        self.check_network(journal)
        for operation in reversed(journal["operations"]):
            self.check_network(journal)
            if not operation["owned"]:
                continue
            # Cached absence cannot rule out a command whose acknowledgement was
            # lost. Explicitly remove every recorded write intent, even if absent.
            await self.write(operation, remove=True)
            operation["owned"] = False
            await save()
