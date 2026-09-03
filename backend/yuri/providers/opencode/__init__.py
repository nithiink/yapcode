"""The OpenCode provider — a headless HTTP coding agent behind Yuri's
AgentProvider contract.

Three files, three jobs: `client.py` speaks HTTP (the `{"data": …}` envelope,
the auth header, the error translation), `server.py` gets hold of a server
(attach if one answers, else spawn one Yuri then owns), and `provider.py`
implements the contract on top of both.
"""
from .client import OpenCodeClient, OpenCodeError, OpenCodeRequestError
from .provider import OpenCodeProvider
from .server import OpenCodeServer, OpenCodeUnavailable

__all__ = ["OpenCodeClient", "OpenCodeError", "OpenCodeRequestError",
           "OpenCodeProvider", "OpenCodeServer", "OpenCodeUnavailable"]
