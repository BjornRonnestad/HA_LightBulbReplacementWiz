# Releasing and submitting to HACS

This repository supports HACS installation as a custom **Integration** repository.
Default catalog inclusion requires a separate review by the HACS maintainers.

## Repository settings

Keep the repository public, unarchived and with Issues enabled. In GitHub's
repository **About** settings, use a descriptive summary, for example:

> Replace Home Assistant light bulbs while preserving entity IDs, with optional ZHA and Zigbee2MQTT binding and group migration.

Add topics such as `home-assistant`, `hacs`, `custom-integration`, `zigbee`,
`zha` and `zigbee2mqtt`. The MIT license is provided in `LICENSE`.

## Release checklist

1. Update `version` in `custom_components/light_bulb_replacement/manifest.json`
   and `pyproject.toml`, run `uv lock`, and update the README version and release
   notes. Use the same version for the Git tag, without reusing an existing tag.
2. Run `uv sync --frozen`, `uv run pytest -q`, `uv run ruff check .` and
   `uv run ruff format --check .` on Linux with Python 3.14.2 or newer.
3. Merge the release changes and require successful **Tests**, **HACS** and
   **Hassfest** jobs on that exact commit. Do not ignore HACS checks.
4. Publish a normal GitHub release from that commit. A tag alone is insufficient
   for default catalog submission. Describe changes, installation requirements
   and the physical-hardware testing limitations. HACS installs the integration
   from the source tree; no custom ZIP attachment is required.
5. Test installation and upgrade through HACS as a custom repository, restart
   Home Assistant, and verify setup and the Configure flow. Check replacement
   and recovery on physical hardware before claiming hardware validation.

## Default catalog submission

After the release and successful validation:

1. Fork [hacs/default](https://github.com/hacs/default) and create a new branch
   from its `master` branch.
2. Add `BjornRonnestad/HA_LightBulbReplacementWiz` to the `integration` JSON
   list in alphabetical order, preserving valid JSON.
3. Open a pull request as the repository owner or a major contributor, allow
   maintainer edits, and address all validation failures and review feedback.

The bundled `custom_components/light_bulb_replacement/brand/icon.png` supplies
the integration brand asset. The repository is not listed in the default catalog
until the HACS pull request is accepted and the scheduled catalog scan completes.

See the current [HACS submission requirements](https://www.hacs.xyz/docs/publish/include/)
and [integration requirements](https://www.hacs.xyz/docs/publish/integration/).
