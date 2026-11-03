"""herdr_client.py — thin NDJSON over Unix socket client for herdr server.

One method per herdr API call. No business logic. If this file exceeds ~150 LOC,
logic is leaking from the bridge and should move there.

API shapes confirmed by smoke spike (Task 1) + Task 2 findings.
"""
import json
import socket
import itertools


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class HerdrServerDown(Exception):
    """herdr server is not running or socket unreachable."""


class HerdrTimeout(Exception):
    """Request timed out waiting for a response."""


class AgentNotReady(Exception):
    """Agent is blocked (e.g. trust dialog) and not ready for commands."""


class HerdrAPIError(Exception):
    """herdr returned an error response."""

    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class HerdrClient:
    """Thin NDJSON-over-Unix-socket client for herdr server."""

    def __init__(self, socket_path=None, timeout=30):
        self.socket_path = socket_path or _default_socket_path()
        self._timeout = timeout
        self._id_counter = itertools.count(1)

    # -- core request/response --

    def _call(self, method, params=None):
        """Send a request and return the result field. Raises on error.
        
        Opens a fresh connection per call (herdr closes the socket after each response).
        """
        req_id = str(next(self._id_counter))
        request = {"id": req_id, "method": method, "params": params or {}}
        
        # Fresh connection per call
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self._timeout)
        try:
            sock.connect(self.socket_path)
        except (ConnectionRefusedError, FileNotFoundError, OSError) as e:
            sock.close()
            raise HerdrServerDown(f"Cannot connect to herdr at {self.socket_path}: {e}")
        
        try:
            sock.sendall(json.dumps(request).encode() + b"\n")
            try:
                raw = self._recv_line(sock)
            except socket.timeout:
                raise HerdrTimeout(f"Timed out waiting for response to {method}")
            except OSError as e:
                raise HerdrServerDown(f"Socket error reading response: {e}")
            try:
                resp = json.loads(raw)
            except json.JSONDecodeError as e:
                raise HerdrAPIError("malformed_response", f"Invalid JSON from herdr: {e}")
            if "error" in resp:
                err = resp["error"]
                code = err.get("code", "unknown")
                msg = err.get("message", "")
                if code == "agent_not_ready":
                    raise AgentNotReady(msg)
                raise HerdrAPIError(code, msg)
            return resp.get("result", {})
        finally:
            sock.close()

    def _recv_line(self, sock):
        """Read until newline (NDJSON framing) from the given socket."""
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                if not buf:
                    raise HerdrServerDown("herdr closed connection with no response")
                break
            buf += chunk
        return buf.decode().strip()

    # -- public API methods --

    def ping(self):
        """Health check. Returns result dict with type='pong'."""
        return self._call("ping")

    def workspace_create(self, label=None, cwd=None):
        """Create a workspace. Returns dict with workspace_id, tab_id, pane_id."""
        params = {}
        if label:
            params["label"] = label
        if cwd:
            params["cwd"] = cwd
        result = self._call("workspace.create", params)
        root_pane = result.get("root_pane", {})
        return {
            "workspace_id": result.get("workspace", {}).get("workspace_id", ""),
            "tab_id": result.get("tab", {}).get("tab_id", ""),
            "pane_id": root_pane.get("pane_id", ""),
        }

    def agent_start(self, name, kind="claude", pane_id=None, cwd=None,
                    timeout_ms=60000, args=None):
        """Start an agent. Returns when herdr detects the process (NOT readiness)."""
        params = {"name": name, "kind": kind, "pane_id": pane_id}
        if cwd:
            params["cwd"] = cwd
        if timeout_ms:
            params["timeout_ms"] = timeout_ms
        if args:
            params["args"] = args
        return self._call("agent.start", params)

    def agent_get(self, target):
        """Get agent info. Returns the agent dict."""
        result = self._call("agent.get", {"target": target})
        return result.get("agent", result)

    def agent_list(self):
        """List all agents. Returns a list of agent dicts."""
        result = self._call("agent.list")
        return result.get("agents", [])

    def agent_read(self, target, source="recent_unwrapped", format="text"):
        """Read agent transcript. Returns text string."""
        result = self._call("agent.read", {
            "target": target,
            "source": source,
            "format": format,
        })
        # Response shape: {type: "agent_read", read: {text: "..."}}
        read = result.get("read", result)
        return read.get("text", "") if isinstance(read, dict) else str(read)

    def agent_prompt(self, target, text, wait=False, until=None, timeout_ms=None):
        """Send a prompt to an agent (multi-turn)."""
        params = {"target": target, "text": text}
        if wait:
            params["wait"] = True
            if until:
                params["until"] = until
            if timeout_ms:
                params["timeout_ms"] = timeout_ms
        return self._call("agent.prompt", params)

    def pane_send_keys(self, pane_id, *keys):
        """Send key presses to a pane."""
        return self._call("pane.send_keys", {
            "pane_id": pane_id,
            "keys": list(keys),
        })

    def pane_read(self, pane_id, source="visible", format="text"):
        """Read pane text. Used for trust-dialog content guard."""
        result = self._call("pane.read", {
            "pane_id": pane_id,
            "source": source,
            "format": format,
        })
        read = result.get("read", result)
        return read.get("text", "") if isinstance(read, dict) else str(read)

    def pane_close(self, pane_id):
        """Close a pane (kills the process)."""
        return self._call("pane.close", {"pane_id": pane_id})

    def close(self):
        """No-op — connections are per-call."""
        pass


def _default_socket_path():
    import os
    return os.path.expanduser("~/.config/herdr/herdr.sock")