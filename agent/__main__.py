"""Connection validation only until the deterministic hardware adapter is accepted."""
import argparse
import json
import os
import time
from pathlib import Path
from .client import Client
from .device import observe

def main():
    parser=argparse.ArgumentParser(description='XHS outbound agent (connection-only alpha)')
    parser.add_argument('--config',required=True)
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    p=Path(args.config)
    if os.name!='nt' and p.stat().st_mode&0o077:
        raise SystemExit('配置必须仅当前用户可读：chmod 600 <config>')
    cfg=json.loads(p.read_text())
    client=Client(cfg['server'],cfg['token'],cfg.get('allow_loopback',False))
    while True:
        # Deliberately do not claim work: no side effects before adapter acceptance.
        state=observe(cfg)
        try:
            client.heartbeat(state)
            print(json.dumps({'heartbeat':'accepted','device_connected':state['device_connected'],
                              'screen_off':state['screen_off'],'mode':'connection_only'}),flush=True)
        except Exception:
            # Never log tokens, URLs or raw HTTP exception bodies.
            print('Heartbeat unavailable; hardware execution remains disabled.',flush=True)
            if args.once: raise SystemExit(1)
        if args.once: break
        time.sleep(30)

if __name__=='__main__': main()
