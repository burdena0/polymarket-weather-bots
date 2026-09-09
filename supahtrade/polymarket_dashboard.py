"""Standalone localhost, read-only Polymarket dashboard. Does not start trading."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from .bot_status import overview


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--port',type=int,default=8090)
    args=p.parse_args();root=Path(__file__).resolve().parents[1]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get('Host') not in (f'127.0.0.1:{args.port}',f'localhost:{args.port}'):
                self.send_error(403);return
            if self.path in ('/','/bots'):
                data=(root/'supahtrade/static/bots.html').read_bytes();mime='text/html; charset=utf-8'
            elif self.path=='/api/bots':
                data=json.dumps(overview(root,independent_weather_root=root/'data/us-weather-independent-user-live-v1')).encode();mime='application/json'
            else:self.send_error(404);return
            self.send_response(200);self.send_header('Content-Type',mime);self.send_header('Cache-Control','no-store')
            self.end_headers();self.wfile.write(data)
        def log_message(self,*args):pass
    print(f'Dashboard: http://127.0.0.1:{args.port}/bots (read-only)')
    ThreadingHTTPServer(('127.0.0.1',args.port),Handler).serve_forever()

if __name__=='__main__':main()
