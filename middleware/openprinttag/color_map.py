"""
Color Name → Hex Converter for OpenPrintTag
=============================================
OpenPrintTag stores filament color as a descriptive name (e.g. "Galaxy Black",
"Prusa Orange", "Mystic Green"), not as a hex value. This module converts those
names to approximate hex colors for LED display and Spoolman integration.

Strategy:
  1. Check if the value is already a hex color (pass through).
  2. Try an exact match in the known color map.
  3. Extract the base color word from the name ("Galaxy Black" → "black")
     and map that to a hex value.
  4. Fall back to white if nothing matches.

The known_colors dict can be expanded as more Prusament/filament colors are
encountered. Contributions welcome — just add the name and hex value.
"""

import re
import logging

logger = logging.getLogger(__name__)

# Known full color names from Prusament / OpenPrintTag ecosystem.
# Add more as you encounter them.
KNOWN_COLORS = {
    # Prusament PLA
    "prusa orange": "FF6600",
    "galaxy black": "1A1A2E",
    "galaxy silver": "C0C0C0",
    "galaxy purple": "6B3FA0",
    "jet black": "0A0A0A",
    "mystic green": "2D6B4F",
    "lipstick red": "CC1133",
    "azure blue": "0070DD",
    "gentleman grey": "5A5A5A",
    "vanilla white": "F5F0E1",
    "urban grey": "6E6E6E",
    "army green": "4B5320",
    "oh my gold": "D4AF37",
    "royal blue": "4169E1",
    "carmine red": "960018",
    "vertigo grey": "808080",
    "pristine green": "228B22",
    # Prusament PETG
    "neon green": "39FF14",
    "signal white": "F0F0F0",
    "iron grey": "434B4D",
    # Generic base colors — catch-all for names we don't have exact matches for
    "white": "FFFFFF",
    "black": "000000",
    "red": "FF0000",
    "green": "00FF00",
    "blue": "0000FF",
    "yellow": "FFFF00",
    "orange": "FF8800",
    "purple": "800080",
    "pink": "FF69B4",
    "grey": "808080",
    "gray": "808080",
    "silver": "C0C0C0",
    "gold": "FFD700",
    "brown": "8B4513",
    "cyan": "00FFFF",
    "magenta": "FF00FF",
    "navy": "000080",
    "teal": "008080",
    "maroon": "800000",
    "olive": "808000",
    "coral": "FF7F50",
    "ivory": "FFFFF0",
    "beige": "F5F5DC",
    "transparent": "FFFFFF",
    "natural": "F5F0E1",
    "clear": "FFFFFF",
}

# Hex color pattern: 6 hex chars with optional # prefix
_HEX_PATTERN = re.compile(r'^#?([0-9a-fA-F]{6})$')


def color_name_to_hex(color_value: str) -> str:
    """
    Convert a color name or hex string to a 6-character hex color (no #).

    Args:
        color_value: Color name ("Galaxy Black") or hex string ("#1A1A2E" / "1A1A2E")

    Returns:
        6-character uppercase hex string, e.g. "1A1A2E". Falls back to "FFFFFF".
    """
    if not color_value:
        return "FFFFFF"

    color_value = color_value.strip()

    # 1. Already a hex value? Pass through.
    match = _HEX_PATTERN.match(color_value)
    if match:
        return match.group(1).upper()

    # 2. Exact match in known colors (case-insensitive)
    lower = color_value.lower()
    if lower in KNOWN_COLORS:
        return KNOWN_COLORS[lower].upper()

    # 3. Extract the last word as the base color
    #    "Galaxy Black" → "black", "Neon Green" → "green", "Oh My Gold" → "gold"
    words = lower.split()
    for word in reversed(words):
        if word in KNOWN_COLORS:
            logger.info(f"Color '{color_value}' → base color '{word}' → #{KNOWN_COLORS[word]}")
            return KNOWN_COLORS[word].upper()

    # 4. Try matching any word (not just last)
    for word in words:
        if word in KNOWN_COLORS:
            logger.info(f"Color '{color_value}' → matched word '{word}' → #{KNOWN_COLORS[word]}")
            return KNOWN_COLORS[word].upper()

    logger.warning(f"Unknown color name '{color_value}' — falling back to white (FFFFFF)")
    return "FFFFFF"
