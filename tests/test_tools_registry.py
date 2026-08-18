import pytest
from pydantic import BaseModel
from tools.registry import Tool, register, call_tool, _REGISTRY


class _EchoIn(BaseModel):
    value: int


class _EchoOut(BaseModel):
    doubled: int


def _echo(inp: _EchoIn) -> _EchoOut:
    return _EchoOut(doubled=inp.value * 2)


def test_call_registered_tool_validates_and_runs():
    _REGISTRY.pop("echo", None)
    register(Tool(name="echo", input_model=_EchoIn, output_model=_EchoOut, func=_echo))
    result = call_tool("echo", {"value": 5})
    assert result.doubled == 10


def test_call_unknown_tool_raises():
    with pytest.raises(KeyError):
        call_tool("does_not_exist", {})


def test_invalid_input_raises_before_calling_func():
    _REGISTRY.pop("echo", None)
    register(Tool(name="echo", input_model=_EchoIn, output_model=_EchoOut, func=_echo))
    with pytest.raises(Exception):  # pydantic.ValidationError
        call_tool("echo", {"value": "not an int"})


def test_register_all_tools_populates_registry():
    from tools.registry import register_all_tools

    register_all_tools()
    assert {"scraper", "optimizer", "semantic_matcher"} <= set(_REGISTRY.keys())
