"""Connection validation only until the deterministic hardware adapter is accepted."""
import argparse
import json
import os
import time
from pathlib import Path
from .client import Client

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
        client.heartbeat({'agent_version':'0.7.0-alpha.1','busy':False,'error_code':'hardware_adapter_not_enabled'})
        print('Heartbeat accepted; hardware execution remains disabled.',flush=True)
        if args.once: break
        time.sleep(30)

if __name__=='__main__': main()
