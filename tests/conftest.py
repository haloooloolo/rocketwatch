import sys
from types import ModuleType
from unittest.mock import MagicMock

# Stub out shared_w3 which connects to RPC endpoints at import time.
_shared_w3_stub = ModuleType("rocketwatch.utils.shared_w3")
_shared_w3_stub.w3 = MagicMock()
_shared_w3_stub.w3_mainnet = MagicMock()
_shared_w3_stub.bacon = MagicMock()
sys.modules["rocketwatch.utils.shared_w3"] = _shared_w3_stub

# With the lazy proxy in utils.config, cfg is importable without loading a file.
# No stubbing needed — tests that need a real Config can set cfg._instance directly.

# utils.visibility pulls in support_utils which reads cfg at module-import time.
# Stub it out so plugins that depend on visibility can be imported in isolation.
_visibility_stub = ModuleType("rocketwatch.utils.visibility")
_visibility_stub.is_hidden = MagicMock(return_value=False)
sys.modules["rocketwatch.utils.visibility"] = _visibility_stub
