"""bili-autopilot: let a local qwen model drive dsh-bili-plus.

DeepSeek/OpenCode only calls the MCP facade. The local model runs in a
detached process, keeps its own memory, and auto-continues after context
exhaustion. The facade returns a compact digest at the end.
"""

__version__ = "1.0.0"
