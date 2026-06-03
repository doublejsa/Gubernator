"""PTY session management — SSH connections via paramiko."""
from __future__ import annotations
import asyncio, socket, time
from typing import Optional
import paramiko


class PTYSession:
    def __init__(self):
        self.client:    Optional[paramiko.SSHClient] = None
        self.channel:   Optional[paramiko.Channel]   = None
        self.connected: bool = False

    def _connect(self, host: str, port: int, username: str, password: str,
                 command: Optional[str], cols: int, rows: int):
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(host, port=port, username=username, password=password, timeout=15)
        self.client.get_transport().set_keepalive(30)
        self.channel = self.client.invoke_shell(term="xterm-256color", width=cols, height=rows)
        self.channel.settimeout(0.5)
        if command:
            time.sleep(0.3)
            self.channel.send(command + "\n")
        self.connected = True

    async def connect(self, host: str, port: int, username: str, password: str,
                      command: Optional[str] = None, cols: int = 220, rows: int = 50):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: self._connect(host, port, username, password, command, cols, rows))

    def _read(self) -> Optional[bytes]:
        try:
            data = self.channel.recv(4096)
            return data or None
        except socket.timeout:
            return b""
        except Exception:
            return None

    async def read(self) -> Optional[bytes]:
        return await asyncio.get_event_loop().run_in_executor(None, self._read)

    def write(self, data: bytes):
        if self.channel and self.connected:
            try:
                self.channel.send(data)
            except Exception:
                pass

    def resize(self, cols: int, rows: int):
        if self.channel and self.connected:
            try:
                self.channel.resize_pty(width=cols, height=rows)
            except Exception:
                pass

    def exec(self, cmd: str, timeout: int = 30) -> str:
        """Non-interactive exec_command — returns combined stdout+stderr."""
        if not (self.client and self.connected):
            return "(not connected)"
        try:
            transport = self.client.get_transport()
            if not (transport and transport.is_active()):
                self.connected = False
                return "(connection dropped)"
            _, stdout, stderr = self.client.exec_command(cmd, timeout=timeout)
            stdout.channel.recv_exit_status()
            return (stdout.read().decode() + stderr.read().decode()).strip() or "(no output)"
        except Exception as e:
            return f"Error: {e}"

    def close(self):
        self.connected = False
        try:
            if self.channel: self.channel.close()
            if self.client:  self.client.close()
        except Exception:
            pass
