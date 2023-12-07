# -*- coding: utf-8 -*-
import struct
import traceback
from time import time as now
from collections import namedtuple
from ._keyboard_event import KeyboardEvent, KEY_DOWN, KEY_UP
from ._canonical_names import all_modifiers, normalize_name, canonical_names
from ._nixcommon import EV_KEY, aggregate_devices
from ._xk_keysyms import XK_KEYSYM_SYMBOLS

# TODO: start by reading current keyboard state, as to not missing any already pressed keys.
# See: http://stackoverflow.com/questions/3649874/how-to-get-keyboard-state-in-linux

def cleanup_key(name):
    """ Formats a dumpkeys format to our standard. """
    name = name.lstrip('+')
    is_keypad = name.startswith('KP_')
    for mod in ('Meta_', 'Control_', 'dead_', 'KP_'):
        if name.startswith(mod):
            name = name[len(mod):]

    # Dumpkeys is weird like that.
    if name == 'Remove':
        name = 'Delete'
    elif name == 'Delete':
        name = 'Backspace'

    if name.endswith('_r'):
        name = 'right ' + name[:-2]
    if name.endswith('_l'):
        name = 'left ' + name[:-2]


    return normalize_name(name), is_keypad

def cleanup_modifier(modifier):
    modifier = normalize_name(modifier)
    if modifier in all_modifiers:
        return modifier
    if modifier[:-1] in all_modifiers:
        return modifier[:-1]
    raise ValueError('Unknown modifier {}'.format(modifier))

"""
Use `dumpkeys --keys-only` to list all scan codes and their names. We
then parse the output and built a table. For each scan code and modifiers we
have a list of names and vice-versa.
"""
from subprocess import check_output, CalledProcessError, PIPE
from collections import defaultdict
import re

to_name = defaultdict(list)
from_name = defaultdict(list)
keypad_scan_codes = set()

def register_key(key_and_modifiers, name):
    if name not in to_name[key_and_modifiers]:
        to_name[key_and_modifiers].append(name)
    if key_and_modifiers not in from_name[name]:
        from_name[name].append(key_and_modifiers)

def _register_key_from_xmodmap_keysym(keyname: str, modifiers: tuple[str], keycode: int):
    if keyname == "NoSymbol":
        return
    if len(keyname) == 1:
        register_key((keycode, modifiers), keyname)
    elif keyname in canonical_names:
        register_key((keycode, modifiers), canonical_names[keyname])
    elif (normalized_name := keyname.lower().replace('_', ' ')) in canonical_names:
        print(keyname, normalized_name)
        register_key((keycode, modifiers), canonical_names[normalized_name])
    elif keyname in XK_KEYSYM_SYMBOLS:
        register_key((keycode, modifiers), XK_KEYSYM_SYMBOLS[keyname])
    # elif keyname in KEYS_RENAME_MAP:
    #     register_key((keycode, modifiers), KEYS_RENAME_MAP[keyname])
    # elif modifiers == () and re.match("^F\d+$", keyname):
    #     register_key((keycode, ()), keyname.lower())
    else:
        if not keyname.startswith('XF86'):
            print(f"{keyname} at keycode {keycode} and modifiers {modifiers} was not registered")

def build_tables():
    if to_name and from_name: return

    xmodmap_output = check_output('xmodmap -pke', shell=True)
    # xmodmap command was tested on Ubuntu 20, Red Hat Enterprise 9, Raspberry OS Bullseye, Almalinux 9
    if isinstance(xmodmap_output, bytes):
        xmodmap_output = xmodmap_output.decode()

    pattern = re.compile('keycode\s+(\d+) =(.*)')
    parsed_lines = re.findall(pattern, xmodmap_output)
    min_keycode = int(parsed_lines[0][0])  # this is 8 and gets subtracted from all keycodes
    if min_keycode != 8:
        print(f'Minimum keycode is usually 8. Found value {min_keycode}.')
    if parsed_lines[0][1] != '':
        print(f'Minimum keycode should not have symbols assigned. Found symbols {parsed_lines[0][1].strip()}')

    for line in parsed_lines[1:]:
        keycode = int(line[0]) - min_keycode
        keynames = [name.strip() for name in line[1].split()]
        # https://wiki.ubuntuusers.de/Xmodmap/
        # column[0] -> simple keypress
        # column[1] -> shift + keypress
        # column[4] -> alt gr + keypress
        # column[5] -> alt gr + shift + keypress
        if len(keynames) > 0:
            _register_key_from_xmodmap_keysym(keynames[0], (), keycode)
        if len(keynames) > 1:
            _register_key_from_xmodmap_keysym(keynames[1], ("shift",), keycode)
        if len(keynames) > 4:
            _register_key_from_xmodmap_keysym(keynames[4], ("alt gr",), keycode)
        if len(keynames) > 5:
            _register_key_from_xmodmap_keysym(keynames[5], ("shift", "alt gr"), keycode)

    print(to_name)
    print(from_name)
    a = 0

device = None
def build_device():
    global device
    if device: return
    device = aggregate_devices('kbd')

def init():
    build_device()
    build_tables()

pressed_modifiers = set()

def listen(callback):
    build_device()
    build_tables()

    while True:
        time, type, code, value, device_id = device.read_event()
        if type != EV_KEY:
            continue

        scan_code = code
        event_type = KEY_DOWN if value else KEY_UP # 0 = UP, 1 = DOWN, 2 = HOLD

        pressed_modifiers_tuple = tuple(sorted(pressed_modifiers))
        names = to_name[(scan_code, pressed_modifiers_tuple)] or to_name[(scan_code, ())] or ['unknown']
        name = names[0]
            
        if name in all_modifiers:
            if event_type == KEY_DOWN:
                pressed_modifiers.add(name)
            else:
                pressed_modifiers.discard(name)

        is_keypad = scan_code in keypad_scan_codes
        callback(KeyboardEvent(event_type=event_type, scan_code=scan_code, name=name, time=time, device=device_id, is_keypad=is_keypad, modifiers=pressed_modifiers_tuple))

def write_event(scan_code, is_down):
    build_device()
    device.write_event(EV_KEY, scan_code, int(is_down))

def map_name(name):
    build_tables()
    for entry in from_name[name]:
        yield entry

    parts = name.split(' ', 1)
    if len(parts) > 1 and parts[0] in ('left', 'right'):
        for entry in from_name[parts[1]]:
            yield entry

def press(scan_code):
    write_event(scan_code, True)

def release(scan_code):
    write_event(scan_code, False)

def type_unicode(character):
    codepoint = ord(character)
    hexadecimal = hex(codepoint)[len('0x'):]

    for key in ['ctrl', 'shift', 'u']:
        scan_code, _ = next(map_name(key))
        press(scan_code)

    for key in hexadecimal:
        scan_code, _ = next(map_name(key))
        press(scan_code)
        release(scan_code)

    for key in ['ctrl', 'shift', 'u']:
        scan_code, _ = next(map_name(key))
        release(scan_code)

if __name__ == '__main__':
    def p(e):
        print(e)
    listen(p)
