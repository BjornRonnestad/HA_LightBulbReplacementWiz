"""Optional ZHA adapter using Zigpy's ZDO and ZCL commands.

No radio connection is opened here: all commands use ZHA's existing gateway.
The adapter only adds bindings/memberships and records each intended write
before sending it, so recovery can handle a lost acknowledgement.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from zigpy.types import EUI64
from zigpy.zcl.foundation import Status as ZclStatus
from zigpy.zdo import types as zdo

from .replacement import ReplacementError

READ_TIMEOUT = 8
WRITE_TIMEOUT = 10
SCAN_CONCURRENCY = 3


def compatible(old_endpoint, new_endpoint, cluster: int) -> bool:
    sides = [
        side
        for side in ("in_clusters", "out_clusters")
        if cluster in getattr(old_endpoint, side)
    ]
    return (
        cluster < 0xFC00
        and bool(sides)
        and all(cluster in getattr(new_endpoint, side) for side in sides)
    )


def get_gateway(hass: HomeAssistant):
    """Import ZHA only when requested, leaving other integrations optional."""
    if "zha" not in hass.config.components:
        raise ReplacementError("zha_unavailable")
    from homeassistant.components.zha.helpers import get_zha_gateway

    try:
        return get_zha_gateway(hass)
    except (ValueError, KeyError) as err:
        raise ReplacementError("zha_unavailable") from err


@dataclass(frozen=True, order=True)
class Endpoint:
    ieee: str
    endpoint: int


@dataclass(frozen=True)
class Binding:
    source: str
    endpoint: int
    cluster: int
    target: str | None = None
    target_endpoint: int | None = None
    group: int | None = None

    @classmethod
    def from_zigpy(cls, entry: zdo.Binding) -> Binding:
        dest = entry.DstAddress
        if dest.addrmode == 1:
            return cls(
                str(entry.SrcAddress),
                int(entry.SrcEndpoint),
                int(entry.ClusterId),
                group=int(dest.nwk),
            )
        if dest.addrmode != 3:
            raise ReplacementError("zha_invalid_table")
        return cls(
            str(entry.SrcAddress),
            int(entry.SrcEndpoint),
            int(entry.ClusterId),
            str(dest.ieee),
            int(dest.endpoint),
        )

    def copy_to(self, old: Endpoint, new: Endpoint) -> Binding:
        result = self
        if Endpoint(self.source, self.endpoint) == old:
            result = replace(result, source=new.ieee, endpoint=new.endpoint)
        if self.target and Endpoint(self.target, self.target_endpoint) == old:
            result = replace(result, target=new.ieee, target_endpoint=new.endpoint)
        return result

    def destination(self) -> zdo.MultiAddress:
        if self.group is not None:
            return zdo.MultiAddress(addrmode=1, nwk=self.group)
        return zdo.MultiAddress(
            addrmode=3, ieee=EUI64.convert(self.target), endpoint=self.target_endpoint
        )

    def describe(self) -> str:
        target = (
            f"group 0x{self.group:04x}"
            if self.group is not None
            else f"{self.target}/{self.target_endpoint}"
        )
        return f"{self.source}/{self.endpoint} → {target} (0x{self.cluster:04x})"


@dataclass(frozen=True)
class ZhaPlan:
    network: str
    old: Endpoint
    new: Endpoint
    bindings: tuple[Binding, ...]
    groups: tuple[int, ...]
    failed_reads: tuple[str, ...]
    blockers: tuple[str, ...]

    def journal(self) -> dict[str, Any]:
        operations = [
            {"kind": "binding", "binding": asdict(binding), "owned": False}
            for binding in self.bindings
        ]
        operations.extend(
            {"kind": "group", **asdict(self.new), "group": group, "owned": False}
            for group in self.groups
        )
        return {
            "network": self.network,
            "old": asdict(self.old),
            "new": asdict(self.new),
            "failed_reads": list(self.failed_reads),
            "operations": operations,
        }


class ZhaMigration:
    """Discover incoming/outgoing bindings and copy one light endpoint."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.gateway = get_gateway(hass)

    @property
    def network(self) -> str:
        state = self.gateway.state
        return f"{state.node_info.ieee}/{state.network_info.extended_pan_id}"

    def device(self, ieee: str):
        device = self.gateway.devices.get(EUI64.convert(ieee))
        if device is None:
            raise ReplacementError("zha_device_missing")
        return device.device

    def endpoint(self, ref: Endpoint):
        endpoint = self.device(ref.ieee).endpoints.get(ref.endpoint)
        if endpoint is None or ref.endpoint == 0:
            raise ReplacementError("zha_device_missing")
        return endpoint

    def resolve(self, entry: er.RegistryEntry) -> Endpoint:
        """Use ZHA's light platform entity, never guess an endpoint from an ID."""
        if entry.platform != "zha" or entry.domain != "light" or not entry.device_id:
            raise ReplacementError("zha_lights_required")
        device_entry = dr.async_get(self.hass).async_get(entry.device_id)
        if not device_entry:
            raise ReplacementError("zha_device_missing")
        addresses = [
            value for domain, value in device_entry.identifiers if domain == "zha"
        ]
        if len(addresses) != 1:
            raise ReplacementError("zha_lights_required")
        device = self.gateway.devices.get(EUI64.convert(addresses[0]))
        if device is None:
            raise ReplacementError("zha_device_missing")
        entity = device.platform_entities.get(("light", entry.unique_id))
        if entity is None or not getattr(entity, "endpoint", None):
            raise ReplacementError("zha_lights_required")
        ref = Endpoint(str(device.ieee), int(entity.endpoint.id))
        self.endpoint(ref)
        return ref

    async def read_bindings(self, ieee: str) -> set[Binding]:
        """Read every page; reject truncated, changing or non-progressing tables."""
        result: set[Binding] = set()
        start = 0
        total = None
        async with asyncio.timeout(READ_TIMEOUT):
            while True:
                response = await self.device(ieee).zdo.request(
                    zdo.ZDOCmd.Mgmt_Bind_req, start
                )
                if (
                    not response
                    or response[0] != zdo.Status.SUCCESS
                    or len(response) != 4
                ):
                    raise ReplacementError("zha_read_failed")
                count, offset, page = int(response[1]), int(response[2]), response[3]
                if total is None:
                    total = count
                if (
                    count != total
                    or offset != start
                    or start + len(page) > count
                    or (not page and start < count)
                ):
                    raise ReplacementError("zha_invalid_table")
                for raw in page:
                    binding = Binding.from_zigpy(raw)
                    if binding.source != ieee or binding in result:
                        raise ReplacementError("zha_invalid_table")
                    result.add(binding)
                start += len(page)
                if start == count:
                    return result

    async def read_groups(self, ref: Endpoint) -> set[int]:
        endpoint = self.endpoint(ref)
        if 4 not in endpoint.in_clusters:
            return set()
        async with asyncio.timeout(READ_TIMEOUT):
            response = await endpoint.in_clusters[4].get_membership(groups=[])
        # A Default Response is not a membership table.
        if len(response) != 2 or not isinstance(response[1], (list, tuple)):
            raise ReplacementError("zha_read_failed")
        return {int(group) for group in response[1]}

    def cached_groups(self, ref: Endpoint) -> set[int]:
        return {
            int(group.group_id)
            for group in self.gateway.groups.values()
            if any(
                str(member.device.ieee) == ref.ieee
                and member.endpoint_id == ref.endpoint
                for member in group.members
            )
        }

    async def async_scan(
        self, old_entry: er.RegistryEntry, new_entry: er.RegistryEntry
    ) -> ZhaPlan:
        old, new = self.resolve(old_entry), self.resolve(new_entry)
        if old.ieee == new.ieee:
            raise ReplacementError("zha_lights_required")
        old_ep, new_ep = self.endpoint(old), self.endpoint(new)
        if old_ep.profile_id != new_ep.profile_id:
            raise ReplacementError("zha_incompatible")
        semaphore = asyncio.Semaphore(SCAN_CONCURRENCY)
        failed: list[str] = []

        async def scan_source(device) -> set[Binding]:
            async with semaphore:
                ieee = str(device.ieee)
                try:
                    return await self.read_bindings(ieee)
                except Exception as err:
                    reason = getattr(err, "code", type(err).__name__)
                    failed.append(f"{device.name} ({ieee}): {reason}")
                    return set()

        # A remote's incoming binding lives on the remote, not on the old bulb.
        tables = await asyncio.gather(
            *(scan_source(device) for device in self.gateway.devices.values())
        )
        required: set[Binding] = set()
        blockers: set[str] = set()
        for table in tables:
            for original in table:
                copied = original.copy_to(old, new)
                if copied == original:
                    continue
                if not compatible(old_ep, new_ep, original.cluster):
                    blockers.add(original.describe())
                else:
                    required.add(copied)
        try:
            groups = await self.read_groups(old)
        except Exception as err:
            groups = self.cached_groups(old)
            failed.append(
                f"groups {old.ieee}/{old.endpoint}: {type(err).__name__} (cached)"
            )
        if groups and 4 not in new_ep.in_clusters:
            blockers.add("Groups cluster 0x0004")
        # Baseline membership must be readable even when partial scanning is accepted.
        await self.read_groups(new)
        return ZhaPlan(
            self.network,
            old,
            new,
            tuple(sorted(required, key=lambda b: b.describe())),
            tuple(sorted(groups)),
            tuple(sorted(failed)),
            tuple(sorted(blockers)),
        )

    def validate(
        self, plan: ZhaPlan, old: er.RegistryEntry, new: er.RegistryEntry
    ) -> None:
        if (
            self.network != plan.network
            or self.resolve(old) != plan.old
            or self.resolve(new) != plan.new
        ):
            raise ReplacementError("zha_network_changed")
        old_ep, new_ep = self.endpoint(plan.old), self.endpoint(plan.new)
        if (
            plan.blockers
            or old_ep.profile_id != new_ep.profile_id
            or any(not compatible(old_ep, new_ep, b.cluster) for b in plan.bindings)
            or (plan.groups and 4 not in new_ep.in_clusters)
        ):
            raise ReplacementError("zha_incompatible")

    def check_network(self, journal: dict[str, Any]) -> None:
        if self.network != journal["network"]:
            raise ReplacementError("zha_network_changed")

    async def present(self, operation: dict[str, Any]) -> bool:
        if operation["kind"] == "binding":
            binding = Binding(**operation["binding"])
            return binding in await self.read_bindings(binding.source)
        ref = Endpoint(operation["ieee"], operation["endpoint"])
        if 4 not in self.endpoint(ref).in_clusters:
            raise ReplacementError("zha_incompatible")
        return operation["group"] in await self.read_groups(ref)

    async def write(self, operation: dict[str, Any], *, remove: bool) -> None:
        """Require a successful protocol response and verify it independently."""
        async with asyncio.timeout(WRITE_TIMEOUT):
            if operation["kind"] == "binding":
                binding = Binding(**operation["binding"])
                response = await self.device(binding.source).zdo.request(
                    zdo.ZDOCmd.Unbind_req if remove else zdo.ZDOCmd.Bind_req,
                    EUI64.convert(binding.source),
                    binding.endpoint,
                    binding.cluster,
                    binding.destination(),
                )
                if not response or response[0] != zdo.Status.SUCCESS:
                    raise ReplacementError("zha_write_failed")
            else:
                endpoint = self.endpoint(
                    Endpoint(operation["ieee"], operation["endpoint"])
                )
                if remove:
                    status = await endpoint.remove_from_group(operation["group"])
                    valid = (ZclStatus.SUCCESS, ZclStatus.NOT_FOUND)
                else:
                    status = await endpoint.add_to_group(
                        operation["group"], name=f"0x{operation['group']:04X}"
                    )
                    valid = (ZclStatus.SUCCESS, ZclStatus.DUPLICATE_EXISTS)
                if status not in valid:
                    raise ReplacementError("zha_write_failed")
        if await self.present(operation) == remove:
            raise ReplacementError("zha_verification_failed")

    async def async_apply(
        self, journal: dict[str, Any], save: Callable[[], Awaitable[None]]
    ) -> None:
        self.check_network(journal)
        # Establish every baseline before making the first network change.
        for operation in journal["operations"]:
            operation["preexisting"] = await self.present(operation)
        await save()
        for operation in journal["operations"]:
            self.check_network(journal)
            if operation["preexisting"] or await self.present(operation):
                continue
            operation["owned"] = True
            await save()  # Persist intent even if the following reply is lost.
            await self.write(operation, remove=False)
            operation["verified"] = True
            await save()
        for operation in journal["operations"]:
            if not await self.present(operation):
                raise ReplacementError("zha_verification_failed")

    async def async_undo(
        self, journal: dict[str, Any], save: Callable[[], Awaitable[None]]
    ) -> None:
        self.check_network(journal)
        for operation in reversed(journal["operations"]):
            self.check_network(journal)
            if not operation["owned"]:
                continue
            if await self.present(operation):
                await self.write(operation, remove=True)
            operation["owned"] = False
            await save()
