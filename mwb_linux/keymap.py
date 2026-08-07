"""Linux evdev key codes to Windows virtual-key codes."""

from __future__ import annotations

# Values are stable Linux input-event ABI codes and Windows VK values.
EVDEV_TO_VK: dict[int, int] = {
    1: 0x1B,  # Esc
    2: 0x31,
    3: 0x32,
    4: 0x33,
    5: 0x34,
    6: 0x35,
    7: 0x36,
    8: 0x37,
    9: 0x38,
    10: 0x39,
    11: 0x30,
    12: 0xBD,
    13: 0xBB,
    14: 0x08,
    15: 0x09,
    16: 0x51,
    17: 0x57,
    18: 0x45,
    19: 0x52,
    20: 0x54,
    21: 0x59,
    22: 0x55,
    23: 0x49,
    24: 0x4F,
    25: 0x50,
    26: 0xDB,
    27: 0xDD,
    28: 0x0D,
    29: 0xA2,
    30: 0x41,
    31: 0x53,
    32: 0x44,
    33: 0x46,
    34: 0x47,
    35: 0x48,
    36: 0x4A,
    37: 0x4B,
    38: 0x4C,
    39: 0xBA,
    40: 0xDE,
    41: 0xC0,
    42: 0xA0,
    43: 0xDC,
    44: 0x5A,
    45: 0x58,
    46: 0x43,
    47: 0x56,
    48: 0x42,
    49: 0x4E,
    50: 0x4D,
    51: 0xBC,
    52: 0xBE,
    53: 0xBF,
    54: 0xA1,
    55: 0x6A,
    56: 0xA4,
    57: 0x20,
    58: 0x14,
    59: 0x70,
    60: 0x71,
    61: 0x72,
    62: 0x73,
    63: 0x74,
    64: 0x75,
    65: 0x76,
    66: 0x77,
    67: 0x78,
    68: 0x79,
    69: 0x90,
    70: 0x91,
    71: 0x67,
    72: 0x68,
    73: 0x69,
    74: 0x6D,
    75: 0x64,
    76: 0x65,
    77: 0x66,
    78: 0x6B,
    79: 0x61,
    80: 0x62,
    81: 0x63,
    82: 0x60,
    83: 0x6E,
    87: 0x7A,
    88: 0x7B,
    96: 0x0D,
    97: 0xA3,
    98: 0x6F,
    99: 0x2C,
    100: 0xA5,
    102: 0x24,
    103: 0x26,
    104: 0x21,
    105: 0x25,
    106: 0x27,
    107: 0x23,
    108: 0x28,
    109: 0x22,
    110: 0x2D,
    111: 0x2E,
    113: 0xAD,
    114: 0xAE,
    115: 0xAF,
    119: 0x13,
    125: 0x5B,
    126: 0x5C,
    127: 0x5D,
    163: 0xB0,
    164: 0xB3,
    165: 0xB1,
    166: 0xB2,
}

VK_TO_EVDEV: dict[int, int] = {}
for evdev_code, vk_code in EVDEV_TO_VK.items():
    # Prefer the normal Enter/modifier instances; extended is supplied separately.
    VK_TO_EVDEV.setdefault(vk_code, evdev_code)

# Low-level Windows hooks normally report side-specific modifiers, but generic
# values are valid input from older peers and accessibility software.
VK_TO_EVDEV.update({0x10: 42, 0x11: 29, 0x12: 56})

EXTENDED_EVDEV = {
    96,
    97,
    100,
    102,
    103,
    104,
    105,
    106,
    107,
    108,
    109,
    110,
    111,
    125,
    126,
}


def evdev_to_windows(code: int, pressed: bool) -> tuple[int, int] | None:
    vk = EVDEV_TO_VK.get(code)
    if vk is None:
        return None
    flags = 0x01 if code in EXTENDED_EVDEV else 0
    if not pressed:
        flags |= 0x80
    return vk, flags


def windows_to_evdev(vk: int, flags: int) -> tuple[int, bool] | None:
    code = VK_TO_EVDEV.get(vk)
    if code is None:
        return None
    # Resolve the handful of left/right values collapsed by basic maps.
    if vk == 0xA3:
        code = 97
    elif vk == 0xA5:
        code = 100
    elif vk == 0x5C:
        code = 126
    elif vk == 0x11 and flags & 0x01:
        code = 97
    elif vk == 0x12 and flags & 0x01:
        code = 100
    return code, not bool(flags & 0x80)
