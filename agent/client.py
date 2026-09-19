import json
import urllib.request
import urllib.parse

class Client:
    def __init__(self,url,token,allow_loopback=False):
        p=urllib.parse.urlsplit(url)
        if p.username or p.password or p.query or p.fragment: raise ValueError('Invalid server URL')
        if p.scheme!='https' and not (allow_loopback and p.scheme=='http' and p.hostname in {'127.0.0.1','localhost','::1'}):
            raise ValueError('HTTPS required; loopback HTTP is development-only')
        self.url=url.rstrip('/')+'/api/v1/agent'; self.token=token
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs): return None
        self.opener=urllib.request.build_opener(NoRedirect)

    def post(self,path,data):
        req=urllib.request.Request(self.url+path,data=json.dumps(data).encode(),headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'},method='POST')
        with self.opener.open(req,timeout=30) as r: return json.load(r)

    def heartbeat(self,state): return self.post('/heartbeat',state)
    def claim(self): return self.post('/claim',{})
    def boundary(self,j): return self.post(f'/jobs/{j["id"]}/submitting',{'lease_token':j['lease_token']})
