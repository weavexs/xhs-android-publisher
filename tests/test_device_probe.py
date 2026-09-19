import subprocess
import unittest
from types import SimpleNamespace
from agent.device import observe

class DeviceProbeTest(unittest.TestCase):
    cfg={'adb_path':'adb','device_serial':'test-usb','expected_model':'TEST',
         'expected_screen':'Physical size: 1080x2280'}
    def probe(self, devices='test-usb device usb:1-1', power='mWakefulness=Asleep', model='TEST', display='mScreenState=OFF'):
        calls=[]
        def run(args, **kwargs):
            calls.append(args)
            out = ('List of devices attached\n'+devices if args[1:]==['devices','-l'] else
                   model if args[-2:]==['getprop','ro.product.model'] else
                   self.cfg['expected_screen'] if args[-2:]==['wm','size'] else
                   display if args[-2:]==['dumpsys','display'] else power)
            return SimpleNamespace(stdout=out)
        return observe(self.cfg,run),calls
    def test_real_usb_and_off(self):
        state,calls=self.probe()
        self.assertTrue(state['device_connected']); self.assertTrue(state['screen_off'])
        self.assertIsNone(state['account_matches'])
        self.assertFalse(any('input' in c or 'monkey' in c for c in calls))
    def test_disconnect_unauthorized_wrong_identity(self):
        for devices in ['', 'test-usb unauthorized usb:1-1','other device usb:1-1','test-usb device usb:1-1\nother device usb:1-2']:
            self.assertFalse(self.probe(devices=devices)[0]['device_connected'])
        self.assertFalse(self.probe(model='OTHER')[0]['device_connected'])
    def test_power_unknown_not_off(self):
        self.assertIsNone(self.probe(power='mWakefulness=Dozing',display='')[0]['screen_off'])
        self.assertTrue(self.probe(power='mWakefulness=Dozing')[0]['screen_off'])
        self.assertFalse(self.probe(power='mWakefulness=Dozing',display='mScreenState=DOZE')[0]['screen_off'])
        self.assertFalse(self.probe(power='mWakefulness=Awake')[0]['screen_off'])
    def test_timeout_fails_closed(self):
        def run(*a,**k): raise subprocess.TimeoutExpired('adb',8)
        self.assertFalse(observe(self.cfg,run)['device_connected'])
