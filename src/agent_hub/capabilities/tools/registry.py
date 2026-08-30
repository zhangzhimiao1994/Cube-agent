from __future__ import annotations


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, object] = {}

    def register(self, name: str, tool: object) -> None:
        if not name or name in self._tools:
            raise ValueError("tool name is invalid")
        self._tools[name] = tool

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def schemas(self) -> tuple[dict[str, str], ...]:
        return tuple({"name": name} for name in self.names())


def create_builtin_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register("calculator.evaluate", object())
    registry.register("document.generate_docx", object())
    registry.register("http.read", object())
    registry.register("presentation.generate_pptx", object())
    registry.register("project.generate_zip", object())
    registry.register("workspace.read", object())
    return registry
