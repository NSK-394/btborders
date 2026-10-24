"""Safe, hardware-free checks of the pynput glue (no hooks are installed, nothing is injected)."""

import sys

import pytest

pynput = pytest.importorskip("pynput")
from pynput import keyboard  # noqa: E402

from btborders import pynput_backend as pb  # noqa: E402


def test_special_keys_use_enum_names():
    assert pb.key_to_name(keyboard.Key.ctrl_l)[0] == "key:ctrl_l"
    assert pb.key_to_name(keyboard.Key.enter)[0] == "key:enter"


def test_printable_char():
    assert pb.key_to_name(keyboard.KeyCode.from_char("a")) == ("char:a", None)
    assert pb.key_to_name(keyboard.KeyCode.from_char("é"))[0] == "char:é"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows control-char quirk")
def test_ctrl_q_control_char_recovered_from_vk():
    # With Ctrl held Windows reports '\x11' for Q; the hotkey must still resolve to 'q'.
    k = keyboard.KeyCode(vk=0x51, char="\x11")
    assert pb.key_to_name(k) == ("char:q", 0x51)


def test_name_to_key_prefers_enum_then_vk_then_char():
    assert pb.name_to_key("key:ctrl_l", 162, True) == keyboard.Key.ctrl_l
    assert pb.name_to_key("char:a", 65, True) == keyboard.KeyCode.from_vk(65)
    assert pb.name_to_key("char:a", 65, False) == keyboard.KeyCode.from_char("a")
    assert pb.name_to_key("key:nonexistent", None, False) is None


def test_screen_bounds_positive():
    if sys.platform.startswith("linux") and not __import__("os").environ.get("DISPLAY"):
        pytest.skip("no display")
    b = pb.screen_bounds()
    assert b.width > 0 and b.height > 0
