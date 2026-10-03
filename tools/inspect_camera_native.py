"""Small offline helpers for inspecting the Smart Life camera native library.

This module intentionally never contacts a camera or Tuya service. It accepts
either a standalone ``.so`` file or an APK containing the native library.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import zipfile


def read_native_library(path: Path, archive_member: str) -> bytes:
    """Read a native library from a direct file or from an APK archive."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            return archive.read(archive_member)
    return path.read_bytes()


def arm_immediate(operands: Sequence[object]) -> int:
    """Return the effective immediate from a decoded ARM instruction.

    Capstone exposes ARM's immediate value and its rotation as distinct
    operands. Reconstruct the 32-bit value used by the instruction.
    """
    immediates: list[int] = []
    for operand in operands:
        # Capstone ARM_OP_IMM is 2; register union fields also expose `.imm`.
        if getattr(operand, "type", None) != 2:
            continue
        value = getattr(operand, "imm", None)
        if isinstance(value, int):
            immediates.append(value)
    if not immediates:
        raise ValueError("instruction has no immediate operand")
    if len(immediates) == 1:
        return immediates[0]
    value, rotation = immediates[-2:]
    rotation %= 32
    if rotation == 0:
        return value
    return ((value >> rotation) | (value << (32 - rotation))) & 0xFFFFFFFF
