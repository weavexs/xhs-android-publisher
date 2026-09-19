"""Pair a locally verified USB phone without printing credentials."""
import argparse
import http.cookiejar
import json
import os
import shutil
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from .client import Client

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--server',required=True)
    p.add_argument('--password-file',required=True)
    p.add_argument('--config',required=True)
    p.add_argument('--verified-account',required=True,help='Nickname just verified on the phone')
    p.add_argument('--allow-loopback',action='store_true')
    args=p.parse_args()
    Client(args.server,'validation-only',args.allow_loopback)
    target=Path(args.config)
    if target.exists(): raise SystemExit('Config already exists; refusing duplicate pairing.')
    adb=shutil.which('adb')
    def call(*cmd): return subprocess.check_output([adb,*cmd],text=True,timeout=10).strip()
    usb=[line.split() for line in call('devices','-l').splitlines()[1:] if 'usb:' in line]
    if len(usb)!=1 or usb[0][1]!='device': raise SystemExit('Exactly one authorized USB phone is required.')
    serial=usb[0][0]
    model=call('-s',serial,'shell','getprop','ro.product.model')
    screen=call('-s',serial,'shell','wm','size')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs): return None
    opener=urllib.request.build_opener(NoRedirect,urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    csrf=''
    def api(path,data=None):
        req=urllib.request.Request(args.server.rstrip('/')+'/api/v1'+path,
            data=None if data is None else json.dumps(data).encode(),
            headers={'Content-Type':'application/json','X-CSRF':csrf})
        with opener.open(req,timeout=20) as r: return json.load(r)
    password=Path(args.password_file)
    if password.stat().st_mode&0o077: raise SystemExit('Password file must be private.')
    csrf=api('/login',{'password':password.read_text().strip()})['csrf']
    try:
        matches=[a for a in api('/accounts') if a['name']==args.verified_account]
        if len(matches)>1: raise SystemExit('Ambiguous account; resolve before pairing.')
        account=matches[0] if matches else api('/accounts',{'name':args.verified_account,'enabled':False,'daily_budget':0})
        if account['settings']['expected_account']!=args.verified_account:
            raise SystemExit('Configured account identity mismatch.')
        paired=api('/accounts/'+account['id']+'/devices',{'name':model+' · USB 状态代理'})
        config={'server':args.server,'token':paired['token'],'allow_loopback':args.allow_loopback,
                'account_id':account['id'],'device_id':paired['id'],'device_serial':serial,
                'adb_path':adb,'expected_model':model,'expected_screen':screen,
                'account_verified_at':datetime.now(timezone.utc).isoformat()}
        target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f: json.dump(config,f,ensure_ascii=False,indent=2)
        print('Paired. Credential saved privately; no execution enabled.')
    finally:
        api('/logout',{})

if __name__=='__main__': main()
