"""TextFold 的局域网/ZeroTier 反向代理（3018 → 127.0.0.1:3017）。

TextFold 只监听 127.0.0.1，并且要求 Host 是 localhost/127.0.0.1（否则 421，
防 DNS rebinding）。所以别的设备直接用不了它的页面。

这个代理做三件事：
1. 把请求转发到 127.0.0.1:3017；
2. 把 Host 改写成 localhost:3017，Origin 一并改写（否则 POST 会被拒）；
3. 保持路径不变（/p/…、/static/…、/api/… 全部原样），页面里的相对/绝对
   路径因此都能工作。

安全：TextFold 自身没有鉴权，谁能访问这个端口，谁就能读整个文献库。
默认只绑 ZeroTier 地址（WB_PROJECT_TF_PROXY_BIND，默认 0.0.0.0），
不需要时把 WB_PROJECT_TF_PROXY 设成 0 即可关闭。
"""

from __future__ import annotations

import http.client
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TARGET_HOST = "127.0.0.1"
TARGET_PORT = int(os.environ.get("WB_PROJECT_TEXTFOLD_PORT", "3017"))
LISTEN_PORT = int(os.environ.get("WB_PROJECT_TF_PROXY_PORT", "3018"))
def _zerotier_ip() -> str:
    """取 ZeroTier 虚拟网卡的 IPv4；取不到就退回 only-localhost（宁可不暴露）。"""
    try:
        out = subprocess.run(["ipconfig"], capture_output=True, text=True,
                             errors="replace", timeout=10).stdout or ""
    except Exception:
        return "127.0.0.1"
    block: list[str] = []
    for line in out.splitlines():
        if "ZeroTier" in line:
            block = [line]
        elif block:
            block.append(line)
            if "IPv4" in line:
                break
    for line in block:
        if "IPv4" in line:
            match = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", line)
            if match:
                return match.group(1)
    return "127.0.0.1"


LISTEN_HOST = os.environ.get("WB_PROJECT_TF_PROXY_BIND") or _zerotier_ip()

_SKIP_REQUEST = {"host", "origin", "referer", "connection", "accept-encoding", "content-length"}
_SKIP_RESPONSE = {"transfer-encoding", "content-length", "connection", "keep-alive"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 安静：不往 stderr 刷每一条
        return

    def _relay(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in _SKIP_REQUEST}
        headers["Host"] = f"localhost:{TARGET_PORT}"
        if self.headers.get("Origin"):
            headers["Origin"] = f"http://localhost:{TARGET_PORT}"
        conn = http.client.HTTPConnection(TARGET_HOST, TARGET_PORT, timeout=300)
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
        except OSError:
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            payload = "TextFold 不可达（127.0.0.1:3017 是否在运行？）".encode()
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        finally:
            conn.close()

        self.send_response(resp.status)
        for key, value in resp.getheaders():
            if key.lower() in _SKIP_RESPONSE:
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    do_GET = _relay
    do_POST = _relay
    do_PUT = _relay
    do_DELETE = _relay
    do_HEAD = _relay


def start() -> ThreadingHTTPServer | None:
    """后台线程起代理；起不来返回 None（不影响插件其它功能）。"""
    try:
        server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    except OSError:
        return None
    thread = threading.Thread(target=server.serve_forever, name="textfold-proxy", daemon=True)
    thread.start()
    return server
