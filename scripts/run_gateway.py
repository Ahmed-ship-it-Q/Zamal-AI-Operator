#!/usr/bin/env python3
"""Run the existing gateway using its owner-approved private credential file."""
import os
from pathlib import Path
import stat
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from zamal_core.store import Denied
from zamal_core.webhook_gateway import serve


def load_secret(path):
    if path.parent.is_symlink():raise Denied('Gateway credential directory symlink denied')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd) as f:
        st=os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1 or st.st_mode & 0o077 or st.st_uid!=os.getuid():
            raise Denied('Gateway credential file must be private and owner-controlled')
        value=f.read(1025)
    prefix='ZAMAL_N8N_GATEWAY_SECRET='
    if len(value)>1024 or not value.startswith(prefix):raise Denied('Invalid gateway credential file')
    secret=value[len(prefix):].strip()
    if len(secret)<32 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in secret):
        raise Denied('Invalid gateway credential format')
    return secret


if __name__=='__main__':
    os.environ['ZAMAL_N8N_GATEWAY_SECRET']=load_secret(ROOT/'state/gateway/auth.env')
    serve(ROOT/'state/operator.sqlite3')
