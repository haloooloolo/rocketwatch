import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import discord

# Add rocketwatch source to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "rocketwatch"))

# Stub out shared_w3 which connects to RPC endpoints at import time.
_shared_w3_stub = ModuleType("utils.shared_w3")
_shared_w3_stub.w3 = MagicMock()
_shared_w3_stub.w3_mainnet = MagicMock()
_shared_w3_stub.w3_archive = MagicMock()
_shared_w3_stub.bacon = MagicMock()
sys.modules["utils.shared_w3"] = _shared_w3_stub

# Stub out utils.embeds which triggers CachedEns/web3 initialization at import time.
# Provide a minimal Embed class (discord.Embed subclass) for code that needs it.
_embeds_stub = ModuleType("utils.embeds")
_embeds_stub.Embed = discord.Embed
_embeds_stub.resolve_ens = MagicMock()
_embeds_stub.el_explorer_url = MagicMock()
_embeds_stub.prepare_args = MagicMock()
_embeds_stub.assemble = MagicMock()
sys.modules["utils.embeds"] = _embeds_stub

# With the lazy proxy in utils.config, cfg is importable without loading a file.
# No stubbing needed — tests that need a real Config can set cfg._instance directly.
