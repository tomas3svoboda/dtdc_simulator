"""YAML loading for scenario configs and material property sets (BuildSpec §11).

BuildSpec §11: "File format: YAML. One file per material property set under
properties/; one scenario file binding a property set + model params +
geometry + operating/disturbance defaults." A scenario file may still embed
its own `physical:` block directly (useful for one-off overrides or tests);
`load_scenario` only resolves `properties/<material>.yaml` when the scenario
omits `physical:`.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from dtdc_simulator.config.schema import PhysicalParams, ScenarioConfig

DEFAULT_PROPERTIES_DIR = "properties"


def load_material_properties(
    material: str, properties_dir: str | Path = DEFAULT_PROPERTIES_DIR
) -> PhysicalParams:
    """Load a material property set from `<properties_dir>/<material>.yaml`."""
    path = Path(properties_dir) / f"{material}.yaml"
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return PhysicalParams.model_validate(raw)


def load_scenario(
    path: str | Path, properties_dir: str | Path = DEFAULT_PROPERTIES_DIR
) -> ScenarioConfig:
    """Load and validate a scenario YAML file into a `ScenarioConfig`.

    If the scenario has no inline `physical:` block, its `material:` key is
    resolved against `<properties_dir>/<material>.yaml`.

    Fails fast: any schema violation or cross-field inconsistency raises
    `pydantic.ValidationError` before assembly (BuildSpec §15).
    """
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if "physical" not in raw:
        material = raw.get("material")
        if not material:
            raise ValueError(
                f"{path}: scenario has no 'physical' block and no 'material' key to resolve one"
            )
        raw["physical"] = load_material_properties(material, properties_dir).model_dump()

    return ScenarioConfig.model_validate(raw)
