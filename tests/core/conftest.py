"""Without huri-core[server] (Ray Serve, FastAPI), the tests of the server
parts are not collected: CI's client job runs everything else with only
huri-core[client] installed, which is what proves those parts never need the
server stack."""

import importlib.util

SERVER_TESTS = ["test_plugins.py", "test_server_session.py", "test_topic_wiring.py"]

if any(importlib.util.find_spec(m) is None for m in ("ray", "fastapi")):
    collect_ignore = SERVER_TESTS
