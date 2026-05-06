"""Reef-tank parameter definitions.

Each parameter is declared once here; sensors and number-entry entities are
generated from this list at startup. Adding a new parameter = adding a new
dict entry, no other code changes.

Fields:
  id            — slug used in entity IDs (lowercase, no spaces)
  name          — display name
  unit          — unit of measurement (used as `unit_of_measurement`)
  icon          — mdi icon
  group         — high-level grouping for UI (macro, nutrient, trace, etc.)
  min/max/step  — range and precision for the number entry entity
  precision     — how many decimal places to display in the latest sensor
  auto_source   — optional sensor entity_id whose state is used as the
                  "auto" source for drift comparison and freshness
  default_target_min/max — used as a hint until the user sets their own
                            target via input_number / icpimport overrides
  common_methods — list of test method labels suggested in the UI
  test_cadence_days — informational; days between tests is "expected" cadence

input_only=True parameters are user-entered (Hanna/Salifert). icp_only=True
parameters are populated by the ICP importer; we still create sensors for
them so they show up in the dashboard, but no number-entry entity.
"""
from __future__ import annotations

from typing import TypedDict


class ParameterDef(TypedDict, total=False):
    id: str
    name: str
    unit: str
    icon: str
    group: str
    min: float
    max: float
    step: float
    precision: int
    auto_source: str | None
    default_target_min: float | None
    default_target_max: float | None
    common_methods: list[str]
    test_cadence_days: int
    input_only: bool
    icp_only: bool


# Parameters the user typically tests with home kits (Hanna / Salifert / etc).
# Manual entry creates a number entity per parameter.
INPUT_PARAMETERS: list[ParameterDef] = [
    {
        "id": "kh", "name": "KH", "unit": "dKH", "icon": "mdi:test-tube",
        "group": "macro",
        "min": 0, "max": 20, "step": 0.01, "precision": 2,
        "auto_source": "sensor.kh_keeper_kh",
        "default_target_min": 8.5, "default_target_max": 8.9,
        "common_methods": ["Hanna ULR", "Salifert", "API"],
        "test_cadence_days": 3,
        "input_only": True,
    },
    {
        "id": "ph", "name": "pH", "unit": "pH", "icon": "mdi:water-percent",
        "group": "macro",
        "min": 6.0, "max": 9.0, "step": 0.01, "precision": 2,
        "auto_source": "sensor.kh_keeper_ph",
        "default_target_min": 7.9, "default_target_max": 8.4,
        "common_methods": ["Hanna", "Probe"],
        "test_cadence_days": 7,
        "input_only": True,
    },
    {
        "id": "calcium", "name": "Calcium", "unit": "ppm", "icon": "mdi:atom",
        "group": "macro",
        "min": 200, "max": 600, "step": 1, "precision": 0,
        "default_target_min": 415, "default_target_max": 450,
        "common_methods": ["Hanna ULR", "Salifert"],
        "test_cadence_days": 7,
        "input_only": True,
    },
    {
        "id": "magnesium", "name": "Magnesium", "unit": "ppm", "icon": "mdi:atom",
        "group": "macro",
        "min": 800, "max": 1800, "step": 5, "precision": 0,
        "default_target_min": 1320, "default_target_max": 1400,
        "common_methods": ["Salifert", "Hanna"],
        "test_cadence_days": 14,
        "input_only": True,
    },
    {
        "id": "nitrate", "name": "Nitrate", "unit": "ppm", "icon": "mdi:flask",
        "group": "nutrient",
        "min": 0, "max": 100, "step": 0.1, "precision": 1,
        "default_target_min": 1, "default_target_max": 10,
        "common_methods": ["Hanna ULR", "Salifert", "Red Sea Pro"],
        "test_cadence_days": 7,
        "input_only": True,
    },
    {
        "id": "nitrite", "name": "Nitrite", "unit": "ppm", "icon": "mdi:flask",
        "group": "nutrient",
        "min": 0, "max": 5, "step": 0.01, "precision": 2,
        "default_target_min": 0, "default_target_max": 0,
        "common_methods": ["API", "Salifert"],
        "test_cadence_days": 30,
        "input_only": True,
    },
    {
        "id": "ammonia", "name": "Ammonia", "unit": "ppm", "icon": "mdi:flask",
        "group": "nutrient",
        "min": 0, "max": 5, "step": 0.05, "precision": 2,
        "default_target_min": 0, "default_target_max": 0,
        "common_methods": ["Salifert", "API"],
        "test_cadence_days": 30,
        "input_only": True,
    },
    {
        "id": "phosphate", "name": "Phosphate", "unit": "ppm", "icon": "mdi:flask",
        "group": "nutrient",
        "min": 0, "max": 1, "step": 0.001, "precision": 3,
        "default_target_min": 0.02, "default_target_max": 0.10,
        "common_methods": ["Hanna ULR", "Hanna Marine"],
        "test_cadence_days": 7,
        "input_only": True,
    },
    {
        "id": "salinity", "name": "Salinity", "unit": "ppt", "icon": "mdi:water-opacity",
        "group": "macro",
        "min": 25, "max": 40, "step": 0.1, "precision": 1,
        "default_target_min": 35, "default_target_max": 35.5,
        "common_methods": ["Refractometer", "Apex", "Hanna"],
        "test_cadence_days": 7,
        "input_only": True,
    },
    {
        "id": "temperature", "name": "Temperature", "unit": "°C", "icon": "mdi:thermometer",
        "group": "macro",
        "min": 20, "max": 32, "step": 0.1, "precision": 1,
        "auto_source": "sensor.rsato_1757534228_temperature",
        "default_target_min": 25, "default_target_max": 26.5,
        "common_methods": ["Probe", "Manual"],
        "test_cadence_days": 1,
        "input_only": True,
    },
    {
        "id": "iodine", "name": "Iodine", "unit": "ppm", "icon": "mdi:atom",
        "group": "trace",
        "min": 0, "max": 0.2, "step": 0.001, "precision": 3,
        "default_target_min": 0.03, "default_target_max": 0.09,
        "common_methods": ["Salifert"],
        "test_cadence_days": 30,
        "input_only": True,
    },
    {
        "id": "strontium", "name": "Strontium", "unit": "ppm", "icon": "mdi:atom",
        "group": "trace",
        "min": 0, "max": 20, "step": 0.1, "precision": 1,
        "default_target_min": 8, "default_target_max": 12,
        "common_methods": ["Salifert"],
        "test_cadence_days": 30,
        "input_only": True,
    },
]

# Parameters populated by the ICP importer only — no user-facing entry,
# but we still expose `latest`/`latest_at` sensors so they're queryable.
# These match the Triton ICP-OES element list.
ICP_ONLY_PARAMETERS: list[ParameterDef] = [
    # Heavy metals (µg/l)
    {"id": "aluminium", "name": "Aluminium", "unit": "µg/l", "icon": "mdi:atom-variant", "group": "heavy_metal", "precision": 2, "icp_only": True},
    {"id": "antimony", "name": "Antimony", "unit": "µg/l", "icon": "mdi:atom-variant", "group": "heavy_metal", "precision": 2, "icp_only": True},
    {"id": "arsenic", "name": "Arsenic", "unit": "µg/l", "icon": "mdi:atom-variant", "group": "heavy_metal", "precision": 2, "icp_only": True},
    {"id": "lead", "name": "Lead", "unit": "µg/l", "icon": "mdi:atom-variant", "group": "heavy_metal", "precision": 2, "icp_only": True},
    {"id": "cadmium", "name": "Cadmium", "unit": "µg/l", "icon": "mdi:atom-variant", "group": "heavy_metal", "precision": 2, "icp_only": True},
    {"id": "copper", "name": "Copper", "unit": "µg/l", "icon": "mdi:atom-variant", "group": "heavy_metal", "precision": 2, "icp_only": True},
    {"id": "lanthanum", "name": "Lanthanum", "unit": "µg/l", "icon": "mdi:atom-variant", "group": "heavy_metal", "precision": 2, "icp_only": True},
    {"id": "mercury", "name": "Mercury", "unit": "µg/l", "icon": "mdi:atom-variant", "group": "heavy_metal", "precision": 2, "icp_only": True},
    {"id": "scandium", "name": "Scandium", "unit": "µg/l", "icon": "mdi:atom-variant", "group": "heavy_metal", "precision": 2, "icp_only": True},
    {"id": "selenium", "name": "Selenium", "unit": "µg/l", "icon": "mdi:atom-variant", "group": "heavy_metal", "precision": 2, "icp_only": True},
    {"id": "titanium", "name": "Titanium", "unit": "µg/l", "icon": "mdi:atom-variant", "group": "heavy_metal", "precision": 2, "icp_only": True},
    {"id": "tungsten", "name": "Tungsten", "unit": "µg/l", "icon": "mdi:atom-variant", "group": "heavy_metal", "precision": 2, "icp_only": True},
    {"id": "tin", "name": "Tin", "unit": "µg/l", "icon": "mdi:atom-variant", "group": "heavy_metal", "precision": 2, "icp_only": True},
    # Macro elements (mg/l, but Triton splits Salinity in PSU)
    {"id": "chloride", "name": "Chloride", "unit": "mg/l", "icon": "mdi:atom", "group": "macro", "precision": 0, "icp_only": True},
    {"id": "sodium", "name": "Sodium", "unit": "mg/l", "icon": "mdi:atom", "group": "macro", "precision": 0, "icp_only": True},
    {"id": "potassium", "name": "Potassium", "unit": "mg/l", "icon": "mdi:atom", "group": "macro", "precision": 0, "icp_only": True},
    {"id": "bromide", "name": "Bromide", "unit": "mg/l", "icon": "mdi:atom", "group": "macro", "precision": 1, "icp_only": True},
    {"id": "boron", "name": "Boron", "unit": "mg/l", "icon": "mdi:atom", "group": "macro", "precision": 1, "icp_only": True},
    {"id": "fluoride", "name": "Fluoride", "unit": "mg/l", "icon": "mdi:atom", "group": "macro", "precision": 2, "icp_only": True},
    {"id": "sulphur", "name": "Sulphur", "unit": "mg/l", "icon": "mdi:atom", "group": "macro", "precision": 0, "icp_only": True},
    # Trace
    {"id": "lithium", "name": "Lithium", "unit": "µg/l", "icon": "mdi:atom-variant", "group": "trace", "precision": 0, "icp_only": True},
    {"id": "nickel", "name": "Nickel", "unit": "µg/l", "icon": "mdi:atom-variant", "group": "trace", "precision": 1, "icp_only": True},
    {"id": "molybdenum", "name": "Molybdenum", "unit": "µg/l", "icon": "mdi:atom-variant", "group": "trace", "precision": 1, "icp_only": True},
    {"id": "vanadium", "name": "Vanadium", "unit": "µg/l", "icon": "mdi:atom-variant", "group": "trace", "precision": 1, "icp_only": True},
    {"id": "zinc", "name": "Zinc", "unit": "µg/l", "icon": "mdi:atom-variant", "group": "trace", "precision": 1, "icp_only": True},
    {"id": "manganese", "name": "Manganese", "unit": "µg/l", "icon": "mdi:atom-variant", "group": "trace", "precision": 1, "icp_only": True},
    {"id": "chromium", "name": "Chromium", "unit": "µg/l", "icon": "mdi:atom-variant", "group": "trace", "precision": 1, "icp_only": True},
    {"id": "cobalt", "name": "Cobalt", "unit": "µg/l", "icon": "mdi:atom-variant", "group": "trace", "precision": 1, "icp_only": True},
    {"id": "iron", "name": "Iron", "unit": "µg/l", "icon": "mdi:atom-variant", "group": "trace", "precision": 1, "icp_only": True},
    {"id": "barium", "name": "Barium", "unit": "µg/l", "icon": "mdi:atom-variant", "group": "trace", "precision": 1, "icp_only": True},
    {"id": "beryllium", "name": "Beryllium", "unit": "µg/l", "icon": "mdi:atom-variant", "group": "trace", "precision": 2, "icp_only": True},
    {"id": "silicon", "name": "Silicon", "unit": "µg/l", "icon": "mdi:atom-variant", "group": "trace", "precision": 0, "icp_only": True},
    # Phosphorus is reported separately from Phosphate by Triton
    {"id": "phosphorus", "name": "Phosphorus", "unit": "µg/l", "icon": "mdi:atom-variant", "group": "nutrient", "precision": 0, "icp_only": True},
]

ALL_PARAMETERS: list[ParameterDef] = INPUT_PARAMETERS + ICP_ONLY_PARAMETERS

# Symbol → parameter id mapping for ICP imports
ICP_SYMBOL_TO_ID: dict[str, str] = {
    "Al": "aluminium", "Sb": "antimony", "As": "arsenic", "Pb": "lead",
    "Cd": "cadmium", "Cu": "copper", "La": "lanthanum", "Hg": "mercury",
    "Sc": "scandium", "Se": "selenium", "Ti": "titanium", "W": "tungsten",
    "Sn": "tin",
    "Cl": "chloride", "Na": "sodium", "Ca": "calcium", "Mg": "magnesium",
    "K": "potassium", "Br": "bromide", "B": "boron", "F": "fluoride",
    "Sr": "strontium", "S": "sulphur",
    "Li": "lithium", "Ni": "nickel", "Mo": "molybdenum",
    "V": "vanadium", "Zn": "zinc", "Mn": "manganese", "I": "iodine",
    "Cr": "chromium", "Co": "cobalt", "Fe": "iron",
    "Ba": "barium", "Be": "beryllium",
    "Si": "silicon",
    "P": "phosphorus", "PO4": "phosphate",
    "Sal": "salinity",
}


def get_parameter(param_id: str) -> ParameterDef | None:
    """Look up a parameter definition by its id."""
    for p in ALL_PARAMETERS:
        if p["id"] == param_id:
            return p
    return None


def get_parameter_by_symbol(symbol: str) -> ParameterDef | None:
    """Look up a parameter definition by its ICP element symbol."""
    pid = ICP_SYMBOL_TO_ID.get(symbol)
    return get_parameter(pid) if pid else None
