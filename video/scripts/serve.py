#!/usr/bin/env python3
"""
serve.py — serve finished videos so you can grab them on your iPhone.

Runs a tiny web server over your project folder, bound to all interfaces so a
phone on the same Wi-Fi (or on Tailscale) can browse to it and tap a video to
save it to the Files/Photos app. No cloud, no upload.

Usage:
    python3 serve.py            # serves the "Short News Video" folder on :8000
    python3 serve.py 9000       # custom port

Then on the iPhone open:  http://<mac-ip-or-tailscale-name>:<port>
Ctrl-C to stop.
"""
import http.server
import socket
import socketserver
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]   # .../Short News Video
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(ROOT), **k)

    def log_message(self, *a):
        pass  # quiet


def main():
    ip = lan_ip()
    print(f"Serving  {ROOT}")
    print("On your iPhone (same Wi-Fi or Tailscale), open one of these:")
    print(f"   http://{ip}:{PORT}")
    print(f"   http://Avas-MacBook-Pro.local:{PORT}")
    print("Browse into a project's 'deliverables' folder and tap final.mp4 "
          "to save it. Ctrl-C to stop.")
    with socketserver.TCPServer(("0.0.0.0", PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped.")


if __name__ == "__main__":
    main()
