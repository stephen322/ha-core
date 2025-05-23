"""Support for scripts."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import voluptuous as vol
from voluptuous.humanize import humanize_error

from homeassistant.components.blueprint import CONF_USE_BLUEPRINT, BlueprintInputs
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_MODE,
    ATTR_NAME,
    CONF_ALIAS,
    CONF_DESCRIPTION,
    CONF_ICON,
    CONF_MODE,
    CONF_NAME,
    CONF_PATH,
    CONF_SEQUENCE,
    CONF_VARIABLES,
    SERVICE_RELOAD,
    SERVICE_TOGGLE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_ON,
)
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import extract_domain_configs
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.config_validation import make_entity_service_schema
from homeassistant.helpers.entity import ToggleEntity
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.integration_platform import (
    async_process_integration_platform_for_component,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.script import (
    ATTR_CUR,
    ATTR_MAX,
    CONF_MAX,
    CONF_MAX_EXCEEDED,
    Script,
    script_stack_cv,
)
from homeassistant.helpers.service import async_set_service_schema
from homeassistant.helpers.trace import trace_get, trace_path
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import bind_hass
from homeassistant.util.dt import parse_datetime

from .config import ScriptConfig, async_validate_config_item
from .const import (
    ATTR_LAST_ACTION,
    ATTR_LAST_TRIGGERED,
    ATTR_VARIABLES,
    CONF_FIELDS,
    CONF_TRACE,
    DOMAIN,
    ENTITY_ID_FORMAT,
    EVENT_SCRIPT_STARTED,
    LOGGER,
)
from .helpers import async_get_blueprints
from .trace import trace_script

SCRIPT_SERVICE_SCHEMA = vol.Schema(dict)
SCRIPT_TURN_ONOFF_SCHEMA = make_entity_service_schema(
    {vol.Optional(ATTR_VARIABLES): {str: cv.match_all}}
)
RELOAD_SERVICE_SCHEMA = vol.Schema({})


@bind_hass
def is_on(hass, entity_id):
    """Return if the script is on based on the statemachine."""
    return hass.states.is_state(entity_id, STATE_ON)


@callback
def scripts_with_entity(hass: HomeAssistant, entity_id: str) -> list[str]:
    """Return all scripts that reference the entity."""
    if DOMAIN not in hass.data:
        return []

    component = hass.data[DOMAIN]

    return [
        script_entity.entity_id
        for script_entity in component.entities
        if entity_id in script_entity.script.referenced_entities
    ]


@callback
def entities_in_script(hass: HomeAssistant, entity_id: str) -> list[str]:
    """Return all entities in script."""
    if DOMAIN not in hass.data:
        return []

    component = hass.data[DOMAIN]

    if (script_entity := component.get_entity(entity_id)) is None:
        return []

    return list(script_entity.script.referenced_entities)


@callback
def scripts_with_device(hass: HomeAssistant, device_id: str) -> list[str]:
    """Return all scripts that reference the device."""
    if DOMAIN not in hass.data:
        return []

    component = hass.data[DOMAIN]

    return [
        script_entity.entity_id
        for script_entity in component.entities
        if device_id in script_entity.script.referenced_devices
    ]


@callback
def devices_in_script(hass: HomeAssistant, entity_id: str) -> list[str]:
    """Return all devices in script."""
    if DOMAIN not in hass.data:
        return []

    component = hass.data[DOMAIN]

    if (script_entity := component.get_entity(entity_id)) is None:
        return []

    return list(script_entity.script.referenced_devices)


@callback
def scripts_with_area(hass: HomeAssistant, area_id: str) -> list[str]:
    """Return all scripts that reference the area."""
    if DOMAIN not in hass.data:
        return []

    component = hass.data[DOMAIN]

    return [
        script_entity.entity_id
        for script_entity in component.entities
        if area_id in script_entity.script.referenced_areas
    ]


@callback
def areas_in_script(hass: HomeAssistant, entity_id: str) -> list[str]:
    """Return all areas in a script."""
    if DOMAIN not in hass.data:
        return []

    component = hass.data[DOMAIN]

    if (script_entity := component.get_entity(entity_id)) is None:
        return []

    return list(script_entity.script.referenced_areas)


@callback
def scripts_with_blueprint(hass: HomeAssistant, blueprint_path: str) -> list[str]:
    """Return all scripts that reference the blueprint."""
    if DOMAIN not in hass.data:
        return []

    component = hass.data[DOMAIN]

    return [
        script_entity.entity_id
        for script_entity in component.entities
        if script_entity.referenced_blueprint == blueprint_path
    ]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Load the scripts from the configuration."""
    hass.data[DOMAIN] = component = EntityComponent[ScriptEntity](LOGGER, DOMAIN, hass)

    # Process integration platforms right away since
    # we will create entities before firing EVENT_COMPONENT_LOADED
    await async_process_integration_platform_for_component(hass, DOMAIN)

    # To register scripts as valid domain for Blueprint
    async_get_blueprints(hass)

    if not await _async_process_config(hass, config, component):
        await async_get_blueprints(hass).async_populate()

    async def reload_service(service: ServiceCall) -> None:
        """Call a service to reload scripts."""
        if (conf := await component.async_prepare_reload()) is None:
            return
        async_get_blueprints(hass).async_reset_cache()
        await _async_process_config(hass, conf, component)

    async def turn_on_service(service: ServiceCall) -> None:
        """Call a service to turn script on."""
        variables = service.data.get(ATTR_VARIABLES)
        script_entities = await component.async_extract_from_service(service)
        for script_entity in script_entities:
            await script_entity.async_turn_on(
                variables=variables, context=service.context, wait=False
            )

    async def turn_off_service(service: ServiceCall) -> None:
        """Cancel a script."""
        # Stopping a script is ok to be done in parallel
        script_entities = await component.async_extract_from_service(service)

        if not script_entities:
            return

        await asyncio.wait(
            [
                asyncio.create_task(script_entity.async_turn_off())
                for script_entity in script_entities
            ]
        )

    async def toggle_service(service: ServiceCall) -> None:
        """Toggle a script."""
        script_entities = await component.async_extract_from_service(service)
        for script_entity in script_entities:
            await script_entity.async_toggle(context=service.context, wait=False)

    hass.services.async_register(
        DOMAIN, SERVICE_RELOAD, reload_service, schema=RELOAD_SERVICE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_TURN_ON, turn_on_service, schema=SCRIPT_TURN_ONOFF_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_TURN_OFF, turn_off_service, schema=SCRIPT_TURN_ONOFF_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_TOGGLE, toggle_service, schema=SCRIPT_TURN_ONOFF_SCHEMA
    )

    return True


async def _async_process_config(hass, config, component) -> bool:
    """Process script configuration.

    Return true, if Blueprints were used.
    """
    entities = []
    blueprints_used = False

    for config_key in extract_domain_configs(config, DOMAIN):
        conf: dict[str, dict[str, Any] | BlueprintInputs] = config[config_key]

        for object_id, config_block in conf.items():
            raw_blueprint_inputs = None
            raw_config = None

            if isinstance(config_block, BlueprintInputs):
                blueprints_used = True
                blueprint_inputs = config_block
                raw_blueprint_inputs = blueprint_inputs.config_with_inputs

                try:
                    raw_config = blueprint_inputs.async_substitute()
                    config_block = cast(
                        dict[str, Any],
                        await async_validate_config_item(hass, raw_config),
                    )
                except vol.Invalid as err:
                    LOGGER.error(
                        "Blueprint %s generated invalid script with input %s: %s",
                        blueprint_inputs.blueprint.name,
                        blueprint_inputs.inputs,
                        humanize_error(config_block, err),
                    )
                    continue
            else:
                raw_config = cast(ScriptConfig, config_block).raw_config

            entities.append(
                ScriptEntity(
                    hass, object_id, config_block, raw_config, raw_blueprint_inputs
                )
            )

    await component.async_add_entities(entities)

    async def service_handler(service: ServiceCall) -> None:
        """Execute a service call to script.<script name>."""
        entity_id = ENTITY_ID_FORMAT.format(service.service)
        script_entity = component.get_entity(entity_id)
        await script_entity.async_turn_on(
            variables=service.data, context=service.context
        )

    # Register services for all entities that were created successfully.
    for entity in entities:
        hass.services.async_register(
            DOMAIN, entity.object_id, service_handler, schema=SCRIPT_SERVICE_SCHEMA
        )

        # Register the service description
        service_desc = {
            CONF_NAME: entity.name,
            CONF_DESCRIPTION: entity.description,
            CONF_FIELDS: entity.fields,
        }
        async_set_service_schema(hass, DOMAIN, entity.object_id, service_desc)

    return blueprints_used


class ScriptEntity(ToggleEntity, RestoreEntity):
    """Representation of a script entity."""

    icon = None

    def __init__(self, hass, object_id, cfg, raw_config, blueprint_inputs):
        """Initialize the script."""
        self.object_id = object_id
        self.icon = cfg.get(CONF_ICON)
        self.description = cfg[CONF_DESCRIPTION]
        self.fields = cfg[CONF_FIELDS]

        # The object ID of scripts need / are unique already
        # they cannot be changed from the UI after creating
        self._attr_unique_id = object_id

        self.entity_id = ENTITY_ID_FORMAT.format(object_id)
        self.script = Script(
            hass,
            cfg[CONF_SEQUENCE],
            cfg.get(CONF_ALIAS, object_id),
            DOMAIN,
            running_description="script sequence",
            change_listener=self.async_change_listener,
            script_mode=cfg[CONF_MODE],
            max_runs=cfg[CONF_MAX],
            max_exceeded=cfg[CONF_MAX_EXCEEDED],
            logger=logging.getLogger(f"{__name__}.{object_id}"),
            variables=cfg.get(CONF_VARIABLES),
        )
        self._changed = asyncio.Event()
        self._raw_config = raw_config
        self._trace_config = cfg[CONF_TRACE]
        self._blueprint_inputs = blueprint_inputs

    @property
    def should_poll(self):
        """No polling needed."""
        return False

    @property
    def name(self):
        """Return the name of the entity."""
        return self.script.name

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        attrs = {
            ATTR_LAST_TRIGGERED: self.script.last_triggered,
            ATTR_MODE: self.script.script_mode,
            ATTR_CUR: self.script.runs,
        }
        if self.script.supports_max:
            attrs[ATTR_MAX] = self.script.max_runs
        if self.script.last_action:
            attrs[ATTR_LAST_ACTION] = self.script.last_action
        return attrs

    @property
    def is_on(self):
        """Return true if script is on."""
        return self.script.is_running

    @property
    def referenced_blueprint(self):
        """Return referenced blueprint or None."""
        if self._blueprint_inputs is None:
            return None
        return self._blueprint_inputs[CONF_USE_BLUEPRINT][CONF_PATH]

    @callback
    def async_change_listener(self):
        """Update state."""
        self.async_write_ha_state()
        self._changed.set()

    async def async_turn_on(self, **kwargs):
        """Run the script.

        Depending on the script's run mode, this may do nothing, restart the script or
        fire an additional parallel run.
        """
        variables = kwargs.get("variables")
        context = kwargs.get("context")
        wait = kwargs.get("wait", True)
        self.async_set_context(context)
        self.hass.bus.async_fire(
            EVENT_SCRIPT_STARTED,
            {ATTR_NAME: self.script.name, ATTR_ENTITY_ID: self.entity_id},
            context=context,
        )
        coro = self._async_run(variables, context)
        if wait:
            await coro
            return

        # Caller does not want to wait for called script to finish so let script run in
        # separate Task. Make a new empty script stack; scripts are allowed to
        # recursively turn themselves on when not waiting.
        script_stack_cv.set([])

        self._changed.clear()
        self.hass.async_create_task(coro)
        # Wait for first state change so we can guarantee that
        # it is written to the State Machine before we return.
        await self._changed.wait()

    async def _async_run(self, variables, context):
        with trace_script(
            self.hass,
            self.object_id,
            self._raw_config,
            self._blueprint_inputs,
            context,
            self._trace_config,
        ) as script_trace:
            # Prepare tracing the execution of the script's sequence
            script_trace.set_trace(trace_get())
            with trace_path("sequence"):
                this = None
                if state := self.hass.states.get(self.entity_id):
                    this = state.as_dict()
                script_vars = {"this": this, **(variables or {})}
                return await self.script.async_run(script_vars, context)

    async def async_turn_off(self, **kwargs):
        """Stop running the script.

        If multiple runs are in progress, all will be stopped.
        """
        await self.script.async_stop()

    async def async_added_to_hass(self) -> None:
        """Restore last triggered on startup."""
        if state := await self.async_get_last_state():
            if last_triggered := state.attributes.get("last_triggered"):
                self.script.last_triggered = parse_datetime(last_triggered)

    async def async_will_remove_from_hass(self):
        """Stop script and remove service when it will be removed from Home Assistant."""
        await self.script.async_stop()

        # remove service
        self.hass.services.async_remove(DOMAIN, self.object_id)
