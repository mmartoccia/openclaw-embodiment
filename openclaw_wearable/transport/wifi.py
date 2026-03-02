"""TCP transport helper using Wearable Packet v1 framing."""

import socket
import struct
from typing import Optional


class WiFiClient:
    """Simple TCP framed client for fallback transport."""

    def __init__(self, host: str, port: int, timeout_s: float = 3.0) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self.sock = None  # type: Optional[socket.socket]

    def connect(self) -> None:
        """Connect socket to configured endpoint."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.timeout_s)
        s.connect((self.host, self.port))
        self.sock = s

    def send(self, packet: bytes) -> int:
        """Send length-prefixed packet and return bytes sent."""
        if self.sock is None:
            raise RuntimeError("wifi client not connected")
        self.sock.sendall(struct.pack("<I", len(packet)) + packet)
        return len(packet)

    def receive(self, timeout_ms: int = 1000) -> Optional[bytes]:
        """Receive length-prefixed packet or None on timeout/EOF."""
        if self.sock is None:
            return None
        self.sock.settimeout(timeout_ms / 1000.0)
        try:
            hdr = self.sock.recv(4)
            if len(hdr) < 4:
                return None
            length = struct.unpack("<I", hdr)[0]
            buf = b""
            while len(buf) < length:
                part = self.sock.recv(length - len(buf))
                if not part:
                    return None
                buf += part
            return buf
        except socket.timeout:
            return None

    def close(self) -> None:
        """Close socket safely."""
        if self.sock is not None:
            self.sock.close()
            self.sock = None
