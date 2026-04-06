import sys
from types import ModuleType
from unittest.mock import MagicMock

import discord

# Stub out shared_w3 which connects to RPC endpoints at import time.
_shared_w3_stub = ModuleType("rocketwatch.utils.shared_w3")
_shared_w3_stub.w3 = MagicMock()
_shared_w3_stub.w3_mainnet = MagicMock()
_shared_w3_stub.w3_archive = MagicMock()
_shared_w3_stub.bacon = MagicMock()
sys.modules["rocketwatch.utils.shared_w3"] = _shared_w3_stub

# Stub out utils.embeds which triggers CachedEns/web3 initialization at import time.
_embeds_stub = ModuleType("rocketwatch.utils.embeds")
_embeds_stub.Embed = discord.Embed
_embeds_stub.resolve_ens = MagicMock()
_embeds_stub.el_explorer_url = MagicMock()
_embeds_stub.format_value = MagicMock()
_embeds_stub.build_event_embed = MagicMock()
_embeds_stub.build_small_event_embed = MagicMock()
_embeds_stub.build_rich_event_embed = MagicMock()
sys.modules["rocketwatch.utils.embeds"] = _embeds_stub

# With the lazy proxy in utils.config, cfg is importable without loading a file.
# No stubbing needed — tests that need a real Config can set cfg._instance directly.
