# Light Bulb Replacement Wizard 0.2.2

This release packages the current Home Assistant light bulb replacement wizard
for HACS. It adds HACS and Hassfest validation, an MIT license, and release
documentation. The replacement flows and Zigbee migration behavior are unchanged
from 0.2.1.

Install or update from HACS as a custom integration repository, then restart
Home Assistant. Home Assistant 2026.10 or newer is required. The existing setup
and replacement instructions are in the README.

Binding and group copying has been tested with simulated ZHA and Zigbee2MQTT
responses. It has not been tested with physical Zigbee hardware. Zigbee2MQTT
discovery uses cached bridge records, which may omit unknown or stale bindings.
Take a Home Assistant backup and review the preview before replacing a bulb.
