#!/usr/bin/env python3
"""Read a small authenticated media byte range without saving tokens or media."""
import argparse
import json
from pathlib import Path
import urllib.parse
import urllib.request
import uuid

parser = argparse.ArgumentParser()
parser.add_argument("--credentials", required=True, type=Path,
                    help="External JSON file with username/password; never commit it")
parser.add_argument("--url", default="http://10.77.0.14:8096")
args = parser.parse_args()
credentials = json.loads(args.credentials.read_text())
base = args.url.rstrip("/")
headers = {"Content-Type": "application/json", "Authorization":
           'MediaBrowser Client="VPN Verification", Device="Smoke Test", '
           f'DeviceId="vpn-test-{uuid.uuid4().hex}", Version="1"'}
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(path, data=None):
    request = urllib.request.Request(base + path, headers=headers,
                                    data=None if data is None else json.dumps(data).encode())
    with opener.open(request, timeout=30) as response:
        payload = response.read()
        return json.loads(payload) if payload else None


session = api("/Users/AuthenticateByName", {"Username": credentials["username"],
                                         "Pw": credentials["password"]})
headers["Authorization"] += ', Token="' + session["AccessToken"] + '"'
try:
    items = api("/Items?Recursive=true&IncludeItemTypes=Movie,Episode&Fields=Path&Limit=500")["Items"]
    item = next((item for item in items if item.get("Path", "").startswith("/media/")
                 and not item["Path"].endswith(".strm")), None)
    if item is None:
        raise SystemExit("Authenticated API OK; no local media file found for byte-range test")
    path = "/Videos/" + item["Id"] + "/stream?" + urllib.parse.urlencode({"static": "true"})
    request = urllib.request.Request(base + path, headers={**headers, "Range": "bytes=0-65535"})
    with opener.open(request, timeout=30) as response:
        payload = response.read(65536)
        if response.status != 206 or len(payload) != 65536:
            raise SystemExit("Unexpected streaming response")
        print(f"Authenticated Jellyfin media range: HTTP {response.status}, {len(payload)} bytes")
finally:
    api("/Sessions/Logout", {})
