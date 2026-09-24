"""Mock ToolLoader for synthetic fixture tests.

Provides tool definitions without requiring the real ToolMaze benchmark.
"""

from __future__ import annotations
from typing import Any, Optional


class ToolLoader:
    """Load tool definitions from YAML-like synthetic data."""

    def __init__(self, definitions_dir: str = "") -> None:
        self._definitions_dir = definitions_dir
        # Built-in synthetic tool definitions
        self._tools: dict[str, dict[str, Any]] = {
            "search_web": {
                "description": "Search the web for information",
                "category": "search",
                "domain": "web",
                "paradigms": {
                    "function_call": {
                        "spec": {
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "query": {"type": "string", "description": "Search query"},
                                },
                                "required": ["query"],
                            }
                        }
                    }
                },
            },
            "read_file": {
                "description": "Read file contents",
                "category": "file",
                "domain": "system",
                "paradigms": {
                    "function_call": {
                        "spec": {
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string", "description": "File path"},
                                },
                                "required": ["path"],
                            }
                        }
                    }
                },
            },
            "write_report": {
                "description": "Write a report",
                "category": "output",
                "domain": "reporting",
                "paradigms": {
                    "function_call": {
                        "spec": {
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "body": {"type": "string"},
                                },
                                "required": ["title"],
                            }
                        }
                    }
                },
            },
            "process_data": {
                "description": "Process and transform data",
                "category": "data",
                "domain": "analytics",
                "paradigms": {
                    "function_call": {
                        "spec": {
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "input": {"type": "string"},
                                },
                                "required": ["input"],
                            }
                        }
                    }
                },
            },
        }

    def get_tool_by_name(self, name: str) -> Optional[dict[str, Any]]:
        """Get a tool definition by name."""
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        """List all available tool names."""
        return list(self._tools.keys())
