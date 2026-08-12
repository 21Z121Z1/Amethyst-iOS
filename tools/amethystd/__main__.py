from __future__ import annotations

import asyncio

from .server import AgentServer


if __name__ == "__main__":
    asyncio.run(AgentServer().serve())
