"""Offline analysis-tool regression tests; no APK or camera required."""

import importlib.util
from pathlib import Path
import zipfile

import pytest

capstone = pytest.importorskip("capstone")
pytest.importorskip("elftools")

spec = importlib.util.spec_from_file_location(
    "native_inspector", Path(__file__).parents[1] / "tools" / "inspect_camera_native.py"
)
inspector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inspector)


@pytest.mark.parametrize(
    ("encoded", "expected"),
    [
        ("6cca8ce2", 0x6C000),  # add ip, ip, #108, #20
        ("01c68fe2", 0x100000),  # add ip, pc, #0x100000
        ("00c08ce2", 0),
    ],
)
def test_arm_plt_immediate(encoded, expected):
    decoder = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_ARM)
    decoder.detail = True
    instruction = next(decoder.disasm(bytes.fromhex(encoded), 0))
    assert instruction.mnemonic == "add"
    assert inspector.arm_immediate(instruction.operands) == expected


def test_read_native_library_accepts_direct_so(tmp_path):
    library = tmp_path / "libThingCameraSDK.so"
    library.write_bytes(b"\x7fELFdirect")

    assert inspector.read_native_library(library, "unused") == b"\x7fELFdirect"


def test_read_native_library_accepts_apk_archive(tmp_path):
    archive = tmp_path / "smartlife.apk"
    with zipfile.ZipFile(archive, "w") as apk:
        apk.writestr("lib/armeabi-v7a/libThingCameraSDK.so", b"\x7fELFarchive")

    assert (
        inspector.read_native_library(archive, "lib/armeabi-v7a/libThingCameraSDK.so")
        == b"\x7fELFarchive"
    )
