"""Constants for the Reef Tank Tracker integration."""
from __future__ import annotations

DOMAIN = "reeftanktracker"

# Single virtual device that groups every entity in the integration.
# All sensors / numbers attach to this device so they appear together
# under Settings → Devices & Services → Reef Tank Tracker → "Reef Tank".
DEVICE_ID = "reef_tank"
DEVICE_NAME = "Reef Tank"
DEVICE_MANUFACTURER = "Reef Tank Tracker"
DEVICE_MODEL = "Parameter Tracking"

# Storage
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}_data"

# Source labels
SOURCE_MANUAL = "manual"
SOURCE_AUTO = "auto"
SOURCE_ICP = "icp"

# Habitat options (mirrors Triton's classifications so the icpimport
# companion can swap setpoints/recommendations cleanly)
HABITATS = [
    "SPS Outer Reef",
    "SPS Inner Reef",
    "Mixed Reef",
    "LPS Dominant",
    "Soft Coral Dominant",
    "Reef Aquarium with Clam",
    "Fish Only",
    "NPS (Filter Feeders)",
    "Seagrass",
]

# Problem options (mirrors Triton's issue categories)
PROBLEMS = [
    "None",
    "Cyanobacteria",
    "RTN",
    "STN",
    "Poor growth",
    "Poor colour",
    "Mortality",
    "Nuisance Algae",
]

# Common test methods/brands across the reef hobby. Used to populate the
# session-level "active test method" select. The first item is the
# default on first install. "Unspecified" is the explicit "I don't want
# to record a method" option, which leaves method=None on entries.
TEST_METHODS = [
    "Unspecified",
    "Hanna ULR",
    "Hanna",
    "Salifert",
    "Red Sea Pro",
    "API",
    "Tropic Marin",
    "Triton ICP",
    "Refractometer",
    "Probe",
    "Other",
]

# Inventory categories
INVENTORY_CATEGORIES = [
    "coral",
    "fish",
    "invertebrate",
    "clam",
    "anemone",
    "macroalgae",
    "other",
]

# Service names
SERVICE_RECORD_READING = "record_reading"
SERVICE_ADD_INVENTORY = "add_inventory"
SERVICE_REMOVE_INVENTORY = "remove_inventory"
SERVICE_SET_HABITAT = "set_habitat"
SERVICE_IMPORT_ICP = "import_icp"
SERVICE_ADD_SUPPLEMENT_PROFILE = "add_supplement_profile"
SERVICE_REMOVE_SUPPLEMENT_PROFILE = "remove_supplement_profile"
SERVICE_LIST_SUPPLEMENT_PROFILES = "list_supplement_profiles"
SERVICE_LOG_WATER_CHANGE = "log_water_change"

# Signal names for state-change dispatching
SIGNAL_READING_RECORDED = f"{DOMAIN}_reading_recorded"
SIGNAL_INVENTORY_CHANGED = f"{DOMAIN}_inventory_changed"
SIGNAL_HABITAT_CHANGED = f"{DOMAIN}_habitat_changed"
SIGNAL_ADVISOR_UPDATED = f"{DOMAIN}_advisor_updated"
