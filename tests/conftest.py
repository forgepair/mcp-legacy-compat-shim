import pytest


@pytest.fixture
def make_probe():
    def _make(request_id):
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "server/discover",
            "params": {},
        }

    return _make
