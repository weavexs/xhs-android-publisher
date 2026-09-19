"""Read-only USB probe. Never wakes, unlocks, launches apps or claims work."""
import subprocess
import re


def observe(cfg, run=subprocess.run):
    state = {'device_connected': False, 'screen_off': None,
             'account_matches': None, 'mode': 'connection_only',
             'agent_version': '0.7.0-alpha.2', 'busy': False,
             'account_verified_at': cfg.get('account_verified_at'),
             'error_code': 'usb_not_connected'}
    def adb(*args):
        return run([cfg['adb_path'], *args], capture_output=True, text=True,
                   check=True, timeout=8).stdout.strip()
    try:
        usb = [line.split() for line in adb('devices', '-l').splitlines()[1:]
               if 'usb:' in line]
        if len(usb) != 1 or usb[0][0] != cfg['device_serial'] or usb[0][1] != 'device':
            return state
        def shell(*args):
            return adb('-s', cfg['device_serial'], 'shell', *args)
        model = shell('getprop', 'ro.product.model')
        size = shell('wm', 'size')
        if model != cfg['expected_model'] or size != cfg['expected_screen']:
            state['error_code'] = 'device_identity_mismatch'
            return state
        state.update(device_connected=True, device_model=model, screen_size=size)
        power = shell('dumpsys', 'power')
        display = shell('dumpsys', 'display')
        screens = re.findall(r'^\s*mScreenState=(\w+)\s*$', display, re.M)
        # Dozing alone is inconclusive; verify the actual current display state.
        off = bool(screens) and all(s == 'OFF' for s in screens) and ('mWakefulness=Asleep' in power or 'mWakefulness=Dozing' in power)
        on = 'mWakefulness=Awake' in power or any(s != 'OFF' for s in screens)
        state['screen_off'] = True if off else False if on else None
        state['error_code'] = 'hardware_execution_disabled'
    except (OSError, subprocess.SubprocessError, KeyError):
        state.update(device_connected=False, screen_off=None, error_code='usb_probe_failed')
    return state
