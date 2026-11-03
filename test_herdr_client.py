"""Tests for herdr_client.py — thin NDJSON over Unix socket client for herdr server.

Unit tests use a mock socket to verify framing and method serialization.
Integration tests (gated behind HERDR_INTEGRATION env var) run against a live herdr server.
"""
import json
import os
import pytest
import socket
from unittest.mock import MagicMock, patch

from herdr_client import (
    HerdrClient,
    HerdrServerDown,
    HerdrTimeout,
    AgentNotReady,
    HerdrAPIError,
)

HOME = os.path.expanduser("~")


# ---------------------------------------------------------------------------
# Mock socket helper
# ---------------------------------------------------------------------------

class MockSocket:
    """A minimal mock socket that returns pre-programmed responses."""
    def __init__(self, responses=None, connect_fail=False, recv_timeout=False):
        self.responses = list(responses) if responses else []
        self.connect_fail = connect_fail
        self.recv_timeout = recv_timeout
        self.sent_data = []
        self._connected = False
        self._timeout = None

    def connect(self, path):
        if self.connect_fail:
            raise ConnectionRefusedError("Connection refused")
        self._connected = True

    def settimeout(self, val):
        self._timeout = val

    def sendall(self, data):
        self.sent_data.append(data)

    def send(self, data):
        self.sent_data.append(data)
        return len(data)

    def recv(self, bufsize):
        if self.recv_timeout:
            raise socket.timeout("timed out")
        if not self.responses:
            return b''
        resp = self.responses.pop(0)
        if isinstance(resp, bytes):
            return resp
        return resp.encode() + b'\n'

    def close(self):
        self._connected = False


def make_client_with_mock(responses=None, connect_fail=False, recv_timeout=False):
    """Create a HerdrClient that uses a mock socket for all calls.
    
    Returns (client, mock_socket) — the mock collects sent data and
    returns pre-programmed responses.
    """
    mock = MockSocket(responses=responses, connect_fail=connect_fail, recv_timeout=recv_timeout)
    client = HerdrClient(socket_path="/tmp/fake.sock")
    # Patch socket.socket to return our mock
    client._socket_factory = lambda: mock
    return client, mock


def _patched_call(self, method, params=None):
    """Override _call to use the mock socket factory."""
    import itertools as _it
    req_id = str(next(self._id_counter))
    request = {"id": req_id, "method": method, "params": params or {}}
    sock = self._socket_factory()
    try:
        sock.sendall(json.dumps(request).encode() + b"\n")
        try:
            raw = self._recv_line(sock)
        except socket.timeout:
            raise HerdrTimeout(f"Timed out waiting for response to {method}")
        except OSError as e:
            raise HerdrServerDown(f"Socket error reading response: {e}")
        resp = json.loads(raw)
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


# Patch _call for unit tests only — integration tests use real sockets
_original_call = HerdrClient._call

def _patched_call(self, method, params=None):
    """Override _call to use the mock socket factory (unit tests only)."""
    req_id = str(next(self._id_counter))
    request = {"id": req_id, "method": method, "params": params or {}}
    sock = self._socket_factory()
    try:
        sock.sendall(json.dumps(request).encode() + b"\n")
        try:
            raw = self._recv_line(sock)
        except socket.timeout:
            raise HerdrTimeout(f"Timed out waiting for response to {method}")
        except OSError as e:
            raise HerdrServerDown(f"Socket error reading response: {e}")
        resp = json.loads(raw)
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


# Base class for unit tests — patches _call to use mock sockets
class UnitTestBase:
    """Base class that patches _call to use mock sockets for each test."""
    
    @pytest.fixture(autouse=True)
    def patch_call(self):
        HerdrClient._call = _patched_call
        yield
        HerdrClient._call = _original_call


# ---------------------------------------------------------------------------
# Unit: NDJSON framing
# ---------------------------------------------------------------------------

class TestNDJSONFraming(UnitTestBase):
    """Verify request/response framing over the socket."""

    def test_request_has_string_id(self):
        """herdr requires string id, not int."""
        client, mock = make_client_with_mock([
            json.dumps({"id": "1", "result": {"type": "pong"}})
        ])
        client.ping()
        sent = json.loads(mock.sent_data[0].decode())
        assert isinstance(sent["id"], str), "id must be a string, not int"

    def test_request_has_method_and_params(self):
        client, mock = make_client_with_mock([
            json.dumps({"id": "1", "result": {"type": "pong"}})
        ])
        client.ping()
        sent = json.loads(mock.sent_data[0].decode())
        assert "method" in sent
        assert "params" in sent
        assert sent["method"] == "ping"

    def test_request_ends_with_newline(self):
        client, mock = make_client_with_mock([
            json.dumps({"id": "1", "result": {"type": "pong"}})
        ])
        client.ping()
        assert mock.sent_data[0].endswith(b"\n"), "request must end with newline"

    def test_response_id_matches_request(self):
        client, mock = make_client_with_mock([
            json.dumps({"id": "1", "result": {"type": "pong"}})
        ])
        result = client.ping()
        assert result is not None

    def test_incrementing_request_ids(self):
        """Each call should use a unique id."""
        client, mock = make_client_with_mock([
            json.dumps({"id": "1", "result": {"type": "pong"}}),
            json.dumps({"id": "2", "result": {"type": "ok"}}),
        ])
        client.ping()
        client.pane_close("w1:p1")
        ids = [json.loads(d.decode())["id"] for d in mock.sent_data]
        assert len(set(ids)) == 2, "each request should have a unique id"


# ---------------------------------------------------------------------------
# Unit: each method sends correct JSON
# ---------------------------------------------------------------------------

class TestMethods(UnitTestBase):
    """Verify each client method sends the correct method name and params."""

    def test_ping(self):
        client, mock = make_client_with_mock([
            json.dumps({"id": "1", "result": {"type": "pong", "version": "0.8.2"}})
        ])
        result = client.ping()
        sent = json.loads(mock.sent_data[0].decode())
        assert sent["method"] == "ping"
        assert result["type"] == "pong"

    def test_workspace_create(self):
        client, mock = make_client_with_mock([
            json.dumps({"id": "1", "result": {
                "type": "workspace_created",
                "workspace": {"workspace_id": "w1"},
                "tab": {"tab_id": "w1:t1"},
                "root_pane": {"pane_id": "w1:p1"}
            }})
        ])
        result = client.workspace_create(label="test", cwd="/tmp")
        sent = json.loads(mock.sent_data[0].decode())
        assert sent["method"] == "workspace.create"
        assert sent["params"]["label"] == "test"
        assert sent["params"]["cwd"] == "/tmp"
        assert result["pane_id"] == "w1:p1"

    def test_workspace_create_minimal(self):
        client, mock = make_client_with_mock([
            json.dumps({"id": "1", "result": {
                "type": "workspace_created",
                "root_pane": {"pane_id": "w1:p1"}
            }})
        ])
        result = client.workspace_create()
        sent = json.loads(mock.sent_data[0].decode())
        assert sent["method"] == "workspace.create"
        assert result["pane_id"] == "w1:p1"

    def test_agent_start(self):
        client, mock = make_client_with_mock([
            json.dumps({"id": "1", "result": {
                "type": "agent_started",
                "agent": {"name": "test", "agent_status": "unknown"}
            }})
        ])
        result = client.agent_start(name="test", pane_id="w1:p1", cwd="/tmp")
        sent = json.loads(mock.sent_data[0].decode())
        assert sent["method"] == "agent.start"
        assert sent["params"]["name"] == "test"
        assert sent["params"]["kind"] == "claude"
        assert sent["params"]["pane_id"] == "w1:p1"
        assert sent["params"]["cwd"] == "/tmp"

    def test_agent_start_with_args(self):
        client, mock = make_client_with_mock([
            json.dumps({"id": "1", "result": {
                "type": "agent_started",
                "agent": {"name": "test", "agent_status": "unknown"}
            }})
        ])
        client.agent_start(name="test", pane_id="w1:p1", args=["--resume", "session-123"])
        sent = json.loads(mock.sent_data[0].decode())
        assert sent["params"]["args"] == ["--resume", "session-123"]

    def test_agent_get(self):
        client, mock = make_client_with_mock([
            json.dumps({"id": "1", "result": {
                "type": "agent_info",
                "agent": {"name": "test", "agent_status": "idle", "pane_id": "w1:p1"}
            }})
        ])
        result = client.agent_get("test")
        sent = json.loads(mock.sent_data[0].decode())
        assert sent["method"] == "agent.get"
        assert sent["params"]["target"] == "test"
        assert result["agent_status"] == "idle"

    def test_agent_list(self):
        client, mock = make_client_with_mock([
            json.dumps({"id": "1", "result": {
                "type": "agent_list",
                "agents": [{"name": "a1"}, {"name": "a2"}]
            }})
        ])
        result = client.agent_list()
        sent = json.loads(mock.sent_data[0].decode())
        assert sent["method"] == "agent.list"
        assert len(result) == 2

    def test_agent_read(self):
        client, mock = make_client_with_mock([
            json.dumps({"id": "1", "result": {
                "type": "agent_read",
                "read": {"text": "hello world"}
            }})
        ])
        result = client.agent_read("test", source="recent_unwrapped")
        sent = json.loads(mock.sent_data[0].decode())
        assert sent["method"] == "agent.read"
        assert sent["params"]["target"] == "test"
        assert sent["params"]["source"] == "recent_unwrapped"
        assert sent["params"]["format"] == "text"
        assert result == "hello world"

    def test_agent_prompt(self):
        client, mock = make_client_with_mock([
            json.dumps({"id": "1", "result": {"type": "ok"}})
        ])
        client.agent_prompt("test", "do something")
        sent = json.loads(mock.sent_data[0].decode())
        assert sent["method"] == "agent.prompt"
        assert sent["params"]["target"] == "test"
        assert sent["params"]["text"] == "do something"

    def test_pane_send_keys(self):
        client, mock = make_client_with_mock([
            json.dumps({"id": "1", "result": {"type": "ok"}})
        ])
        client.pane_send_keys("w1:p1", "Down", "Enter")
        sent = json.loads(mock.sent_data[0].decode())
        assert sent["method"] == "pane.send_keys"
        assert sent["params"]["pane_id"] == "w1:p1"
        assert sent["params"]["keys"] == ["Down", "Enter"]

    def test_pane_read(self):
        client, mock = make_client_with_mock([
            json.dumps({"id": "1", "result": {
                "type": "pane_read",
                "read": {"text": "trust dialog text"}
            }})
        ])
        result = client.pane_read("w1:p1", source="visible")
        sent = json.loads(mock.sent_data[0].decode())
        assert sent["method"] == "pane.read"
        assert sent["params"]["pane_id"] == "w1:p1"
        assert sent["params"]["source"] == "visible"
        assert result == "trust dialog text"

    def test_pane_close(self):
        client, mock = make_client_with_mock([
            json.dumps({"id": "1", "result": {"type": "ok"}})
        ])
        client.pane_close("w1:p1")
        sent = json.loads(mock.sent_data[0].decode())
        assert sent["method"] == "pane.close"
        assert sent["params"]["pane_id"] == "w1:p1"


# ---------------------------------------------------------------------------
# Unit: error cases
# ---------------------------------------------------------------------------

class TestErrors(UnitTestBase):
    """Verify error handling for various failure modes."""

    def test_server_down(self):
        """ConnectionRefusedError → HerdrServerDown."""
        client = HerdrClient(socket_path="/tmp/fake.sock")
        mock = MockSocket(connect_fail=True)
        client._socket_factory = lambda: mock
        with pytest.raises(HerdrServerDown):
            client.ping()

    def test_timeout(self):
        """socket.timeout → HerdrTimeout."""
        client, mock = make_client_with_mock(recv_timeout=True)
        with pytest.raises(HerdrTimeout):
            client.ping()

    def test_api_error_response(self):
        """Response with 'error' field → HerdrAPIError."""
        client, mock = make_client_with_mock([
            json.dumps({"id": "1", "error": {"code": "agent_not_found", "message": "not found"}})
        ])
        with pytest.raises(HerdrAPIError) as exc_info:
            client.agent_get("nonexistent")
        assert "agent_not_found" in str(exc_info.value)

    def test_agent_not_ready(self):
        """agent_not_ready error code → AgentNotReady."""
        client, mock = make_client_with_mock([
            json.dumps({"id": "1", "error": {"code": "agent_not_ready", "message": "blocked"}})
        ])
        with pytest.raises(AgentNotReady):
            client.agent_start(name="test", pane_id="w1:p1")

    def test_malformed_response(self):
        """Non-JSON response → should raise an error (not silently return None)."""
        client, mock = make_client_with_mock([b"not json at all\n"])
        with pytest.raises(Exception):
            client.ping()

    def test_empty_response(self):
        """Empty recv (server closed) → should raise an error."""
        client, mock = make_client_with_mock([b''])
        with pytest.raises(Exception):
            client.ping()

    def test_unknown_method_error(self):
        """Unknown method variant → HerdrAPIError."""
        client, mock = make_client_with_mock([
            json.dumps({"id": "1", "error": {"code": "invalid_request", "message": "unknown variant"}})
        ])
        with pytest.raises(HerdrAPIError):
            client._call("nonexistent.method", {})


# ---------------------------------------------------------------------------
# Integration tests (gated behind HERDR_INTEGRATION env var)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not os.environ.get("HERDR_INTEGRATION"),
    reason="Set HERDR_INTEGRATION=1 to run integration tests (requires live herdr server)"
)
class TestIntegration:
    """Integration tests against a live herdr server."""

    SOCKET_PATH = os.path.expanduser("~/.config/herdr/herdr.sock")

    def test_ping(self):
        client = HerdrClient(socket_path=self.SOCKET_PATH)
        result = client.ping()
        assert "pong" in result.get("type", "") or "version" in result

    def test_workspace_create_and_close(self):
        client = HerdrClient(socket_path=self.SOCKET_PATH)
        ws = client.workspace_create(label="integration-test")
        assert "pane_id" in ws
        assert ws["pane_id"], "pane_id should not be empty"
        # Clean up
        try:
            client._call("workspace.close", {"workspace_id": ws.get("workspace_id", "")})
        except Exception:
            pass  # best-effort cleanup

    def test_full_round_trip(self):
        """ping → workspace_create → agent_start → agent_prompt → agent_get → agent_read."""
        import uuid
        client = HerdrClient(socket_path=self.SOCKET_PATH)
        agent_name = f"rt-{uuid.uuid4().hex[:6]}"
        
        # Ping
        assert client.ping() is not None

        # Create workspace
        ws = client.workspace_create(label=agent_name, cwd=f"{HOME}/DEV")
        pane_id = ws["pane_id"]
        workspace_id = ws.get("workspace_id", "")

        try:
            # Start agent
            try:
                client.agent_start(name=agent_name, pane_id=pane_id, cwd=f"{HOME}/DEV")
            except AgentNotReady:
                pass  # May be blocked on trust dialog

            # Wait for agent to be ready (poll), handling trust dialog
            import time
            idle_confirmations = 0
            for _ in range(60):
                try:
                    info = client.agent_get(agent_name)
                    status = info.get("agent_status", "")
                    if status == "idle":
                        idle_confirmations += 1
                        if idle_confirmations >= 3:
                            break  # require 3 consecutive idle readings
                    elif status == "blocked":
                        idle_confirmations = 0
                        text = client.pane_read(pane_id, source="visible")
                        if "trust" in text.lower():
                            client.pane_send_keys(pane_id, "Down")
                            time.sleep(1)
                            client.pane_send_keys(pane_id, "Enter")
                            time.sleep(5)
                            continue
                    else:
                        idle_confirmations = 0
                except HerdrAPIError:
                    idle_confirmations = 0
                time.sleep(1)

            # Verify agent is truly idle
            info = client.agent_get(agent_name)
            assert info.get("agent_status") == "idle", f"Agent not idle after startup, status={info.get('agent_status')}"

            # Send a trivial prompt
            client.agent_prompt(agent_name, "Reply with exactly: HERDR_OK and nothing else.")
            
            # Wait for agent to start working
            time.sleep(2)
            
            # Wait for completion (agent goes working → idle)
            for _ in range(120):
                info = client.agent_get(agent_name)
                status = info.get("agent_status", "")
                if status == "idle":
                    # Verify we're not just catching a momentary idle — check the pane for the response
                    break
                time.sleep(1)

            # Read the result
            text = client.agent_read(agent_name, source="recent_unwrapped")
            assert "HERDR_OK" in text, f"Expected HERDR_OK in response, got: {text[:500]}"

        finally:
            # Clean up
            try:
                client.pane_close(pane_id)
            except Exception:
                pass
            try:
                client._call("workspace.close", {"workspace_id": workspace_id})
            except Exception:
                pass