# Light Bulb Replacement Wizard 0.2.3

The maintainer has successfully tested bulb replacement on a physical ZHA setup
in Home Assistant. The README now records this result instead of saying that
no physical Zigbee hardware has been tested.

This result covers the replacement flow tested on that setup. It does not claim
physical validation of every binding, group, undo or interrupted-recovery
scenario. Zigbee2MQTT still has automated tests with simulated bridge responses
and has not yet been tested with physical hardware. Its cached discovery
limitations remain unchanged.

This is a documentation and version update; replacement behavior is unchanged.
Update through HACS and restart Home Assistant. Home Assistant 2026.10 or newer
is required.
