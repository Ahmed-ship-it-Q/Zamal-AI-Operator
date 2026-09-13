"""Loopback-only authenticated n8n intake. Never accepts approval decisions.

Provision ZAMAL_N8N_GATEWAY_SECRET through the trusted credential control plane;
this module does not create, persist, export or print credentials.
"""
import hmac
import json
import os
import signal
from http.server import BaseHTTPRequestHandler, HTTPServer
from .store import Store, Denied
from .gateway import InboundEventGateway
from .integration_security import failure


class WebhookBoundary:
    def __init__(self, store, secret):
        if not isinstance(secret,str) or len(secret)<32:
            raise Denied('Gateway credential not configured')
        self.store,self._secret=store,secret

    def receive(self, raw, authorization):
        if not isinstance(authorization,str) or not hmac.compare_digest(authorization.encode(),('Bearer '+self._secret).encode()):
            return failure('DENIED','unauthorized')
        if not isinstance(raw,bytes) or len(raw)>65536: return failure('DENIED','invalid_envelope')
        try:
            def pairs(items):
                d={}
                for k,v in items:
                    if k in d: raise ValueError('Duplicate JSON key')
                    d[k]=v
                return d
            event=json.loads(raw,object_pairs_hook=pairs)
            if not isinstance(event,dict) or set(event)!={'event_type','event_key','payload'}:
                return failure('DENIED','invalid_envelope')
            return InboundEventGateway(self.store).receive(event['event_type'],'n8n',event['payload'],event['event_key'])
        except (ValueError,TypeError,RecursionError):
            return failure('DENIED','invalid_envelope')


def handler_for(path, secret):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass  # Never record authorization or payloads.
        def do_POST(self):
            self.connection.settimeout(5)
            try:
                if self.path!='/events' or self.headers.get('Origin') or self.headers.get('Transfer-Encoding'):
                    result=failure('DENIED','invalid_request')
                else:
                    length=int(self.headers.get('Content-Length','-1'))
                    if not 0<=length<=65536: raise ValueError()
                    raw=self.rfile.read(length)
                    if len(raw)!=length: raise ValueError()
                    store=Store(path)
                    try:result=WebhookBoundary(store,secret).receive(raw,self.headers.get('Authorization',''))
                    finally:store.close()
                self.send_response(200 if result.get('ok') or result['outcome']=='DUPLICATE' else 403)
                data=json.dumps(result).encode()
                self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
            except Exception:
                self.send_error(400,'Invalid gateway request')
    return Handler


def serve(path, port=8788):
    secret=os.environ.get('ZAMAL_N8N_GATEWAY_SECRET','')
    if len(secret)<32: raise Denied('Gateway credential not configured')
    if not 1024<=port<=65535: raise Denied('Invalid gateway port')
    with HTTPServer(('127.0.0.1',port),handler_for(path,secret)) as server:
        # Graceful shutdown on SIGTERM and SIGINT.
        # server.shutdown() is thread-safe and stops serve_forever()'s select loop.
        # Emergency Stop and in-flight request handling are not weakened: signals
        # are only handled here in the main serve() call, not in the request handler.
        def _shutdown(signum, frame):
            # Run in a daemon thread so the signal handler returns immediately.
            import threading
            t = threading.Thread(target=server.shutdown, daemon=True)
            t.start()
        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)
        server.serve_forever()
