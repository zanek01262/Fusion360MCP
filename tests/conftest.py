"""Test setup. Nothing here needs Fusion 360.

MCPListener imports `adsk`, which exists only inside Fusion. A MagicMock
package is enough to import it and exercise its pure-Python parts (request
guard, job store, watchdog, helpers). The server's generated code is compiled
and, where it is plain Python, executed against small fakes.
"""
import os
import sys
import types
from unittest import mock

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

if "adsk" not in sys.modules:
    _adsk = types.ModuleType("adsk")
    _adsk.core = mock.MagicMock(name="adsk.core")
    _adsk.fusion = mock.MagicMock(name="adsk.fusion")
    sys.modules["adsk"] = _adsk
    sys.modules["adsk.core"] = _adsk.core
    sys.modules["adsk.fusion"] = _adsk.fusion


@pytest.fixture
def srv():
    import fusion_mcp_server
    return fusion_mcp_server


@pytest.fixture
def lst():
    import MCPListener
    return MCPListener


@pytest.fixture
def captured(srv, monkeypatch):
    """Capture the code each tool would send to Fusion instead of sending it."""
    sent = []

    def fake_send(code, compact=True):
        sent.append(code)
        return "STATUS: OK"

    monkeypatch.setattr(srv, "_send", fake_send)
    monkeypatch.setattr(srv, "_send_async", lambda code: (sent.append(code), "QUEUED")[1])
    return sent
