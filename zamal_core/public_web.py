"""Read-only public HTTPS: pinned DNS, no proxy, redirects, cookies or local URLs."""
import hashlib,html,http.client,ipaddress,json,re,socket,ssl,time
from html.parser import HTMLParser
from urllib.parse import urlsplit,urlencode,quote
from .store import Denied
from .integration_security import validate_data

class PublicWeb:
    def fetch(self,url,check=lambda:None):
        validate_data({'url':url});u=urlsplit(url)
        if u.scheme!='https' or not u.hostname or u.username or u.password or u.fragment or u.port not in (None,443) or len(url)>2048:
            raise Denied('Public HTTPS URL required')
        host=u.hostname.encode('idna').decode();check()
        addresses=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise Denied('Private/local network access denied')
        # Connect only to the already-validated address; TLS still verifies original host.
        conn=http.client.HTTPSConnection(host,timeout=8,context=ssl.create_default_context())
        def connect(address,timeout=None,source_address=None,**kwargs):
            family,typ,proto,_,sockaddr=addresses[0];s=socket.socket(family,typ,proto);s.settimeout(8)
            try:s.connect(sockaddr);return s
            except BaseException:s.close();raise
        conn._create_connection=connect
        try:
            check();conn.request('GET',u.path+('?' + u.query if u.query else ''),headers={'User-Agent':'ZamalOperator/1.0 (personal research; bounded reads)','Accept':'application/json,text/html,text/plain','Accept-Encoding':'identity'})
            res=conn.getresponse()
            if res.status!=200:raise Denied('Public source HTTP '+str(res.status))
            body=bytearray();deadline=time.monotonic()+25
            while True:
                check()
                if time.monotonic()>deadline:raise Denied('Public source deadline')
                chunk=res.read(32768)
                if not chunk:break
                body.extend(chunk)
                if len(body)>4*1024*1024:raise Denied('Public source size limit')
            check()
            return {'url':url,'retrieved_at':time.time(),'sha256':hashlib.sha256(body).hexdigest(),'content_type':res.getheader('Content-Type',''),'body':body.decode('utf-8',errors='replace')}
        except (OSError,http.client.HTTPException):raise Denied('Public source unavailable') from None
        finally:conn.close()
    def search(self,query,check=lambda:None):
        src=self.fetch('https://en.wikipedia.org/w/rest.php/v1/search/page?'+urlencode({'q':query,'limit':3}),check)
        data=json.loads(src['body'])
        return ['https://en.wikipedia.org/wiki/'+quote(p['key'],safe='') for p in data.get('pages',[])[:3]]

class TextExtractor(HTMLParser):
    def __init__(self):super().__init__();self.depth=0;self.parts=[]
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style','noscript'):self.depth+=1
    def handle_endtag(self,tag):
        if tag in ('script','style','noscript'):self.depth=max(0,self.depth-1)
    def handle_data(self,data):
        if not self.depth:self.parts.append(data)
def plain(text):
    p=TextExtractor();p.feed(html.unescape(text));return re.sub(r'\s+',' ',' '.join(p.parts)).strip()
