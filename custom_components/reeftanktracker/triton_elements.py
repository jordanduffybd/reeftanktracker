"""Mapping from Triton ICP-OES element symbols to integration parameter ids.

Used by:
  - `icp_importer.py` (the URL-ingestion service introduced in 0.4.0)
  - `scripts/import_history.py` (CSV import — imports this map indirectly)

Single source of truth. Adding a new element to the integration's
`parameters.py` and forgetting to map it here would cause ICP imports
to silently skip that element — keep the two in sync.
"""
from __future__ import annotations

# Triton element symbol → integration parameter id.
# Values must exist as `id` entries in `parameters.py`'s ALL_PARAMETERS.
TRITON_ELEMENT_TO_PARAM_ID: dict[str, str] = {
    # Heavy metals (typically warning-level only)
    "Al": "aluminium",  "Sb": "antimony",  "As": "arsenic",  "Pb": "lead",
    "Cd": "cadmium",    "Cu": "copper",    "La": "lanthanum","Hg": "mercury",
    "Sc": "scandium",   "Se": "selenium",  "Ti": "titanium", "W": "tungsten",
    "Sn": "tin",
    # Major ions / macronutrients
    "Cl": "chloride",   "Na": "sodium",    "Ca": "calcium",  "Mg": "magnesium",
    "K":  "potassium",  "Br": "bromide",   "B":  "boron",    "F":  "fluoride",
    "Sr": "strontium",  "S":  "sulphur",
    # Trace elements
    "Li": "lithium",    "Ni": "nickel",    "Mo": "molybdenum",
    "V":  "vanadium",   "Zn": "zinc",      "Mn": "manganese","I":  "iodine",
    "Cr": "chromium",   "Co": "cobalt",    "Fe": "iron",
    "Ba": "barium",     "Be": "beryllium",
    "Si": "silicon",
    # Phosphorus reported two ways by Triton
    "P":   "phosphorus", "PO4": "phosphate",
    # Salinity reported as "Sal"
    "Sal": "salinity",
}


def param_id_for_element(symbol: str) -> str | None:
    """Look up the integration parameter id for a Triton element symbol.

    Returns None for unknown symbols — callers should log + skip rather
    than fail (Triton occasionally adds new elements).
    """
    return TRITON_ELEMENT_TO_PARAM_ID.get(symbol)
