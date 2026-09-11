"""Bounded integration data validation; never log rejected input or exceptions."""
import re
from .store import Denied, encode

SECRET_KEY = re.compile(r'(password|passwd|secret|token|api.?key|authorization|cookie|private.?key|encryption.?key|credentials?)', re.I)
SECRET_TEXT = re.compile(r'(-----BEGIN .*PRIVATE KEY|\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{12,}|https?://[^\s/@]+:[^\s/@]+@|(?:password|secret|token|api[_-]?key)\s*[=:]\s*["\']?[^\s"\']+)',re.I)


def validate_data(value, depth=0):
    if depth > 12:
        raise Denied('Integration nesting limit')
    if isinstance(value, dict):
        for k,v in value.items():
            if not isinstance(k,str) or SECRET_KEY.search(k):
                raise Denied('Credential-bearing integration data denied')
            validate_data(v,depth+1)
    elif isinstance(value,list):
        for v in value: validate_data(v,depth+1)
    elif isinstance(value,str):
        if SECRET_TEXT.search(value): raise Denied('Credential-bearing integration data denied')
    elif value is not None and type(value) not in (bool,int,float):
        raise Denied('JSON integration data required')
    if depth==0 and len(encode(value).encode())>65536:
        raise Denied('Integration payload too large')


def failure(outcome, code, **ids):
    return {'outcome':outcome,'ok':False,'code':code,**ids}
