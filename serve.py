#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
#  serve.py — simple web server to serve the scanner HTML
#  Runs on port 8080
#  Access from browser: http://YOUR_VM_IP:8080
# ═══════════════════════════════════════════════════════════════

import http.server
import socketserver
import os

PORT     = 8080
BASE_DIR = os.path.expanduser('~/nse-scanner')
WWW_DIR  = os.path.join(BASE_DIR, 'www')

os.makedirs(WWW_DIR, exist_ok=True)
os.chdir(WWW_DIR)

Handler = http.server.SimpleHTTPRequestHandler

print(f"NSE Scanner web server running on port {PORT}")
print(f"Serving files from: {WWW_DIR}")
print(f"Open in browser: http://YOUR_VM_EXTERNAL_IP:{PORT}")
print("Press Ctrl+C to stop")

with socketserver.TCPServer(('', PORT), Handler) as httpd:
    httpd.serve_forever()
