#!/usr/bin/env python3
"""Subprocess helper for libacars WX bridge decoding.

This helper deliberately stays outside page/UI code. It reads a JSON payload
from stdin, accepts a decode mode argument (`message` or `vdl2`), and emits a
structured JSON object that `ui.libacars_bridge` can normalize into sounding
observations.

Backend resolution order:
1. `LIBACARS_HELPER_FIXTURE_FILE` JSON fixture backend for tests/manual checks
2. `LIBACARS_HELPER_BACKEND` dynamic Python backend (`module` or `module:attr`)

If no backend is configured or the backend cannot decode the payload, the
helper exits successfully with no stdout so callers degrade safely.
"""

from __future__ import annotations

import importlib
import inspect
import json
import os
import sys
from typing import Any


class _UnavailableBackend:
    available = False
    name = "unavailable"

    def __init__(self, reason: str):
        self.reason = str(reason or "unconfigured")

    def decode_message(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        del payload
        return None

    def decode_vdl2_frame(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        del payload
        return None


class _FixtureBackend:
    available = True
    name = "fixture"

    def __init__(self, fixture_path: str):
        with open(fixture_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("fixture payload must be a JSON object")
        self._payload = payload

    def _result(self, mode: str) -> dict[str, Any] | None:
        result = self._payload.get(mode)
        return result if isinstance(result, dict) else None

    def decode_message(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        del payload
        return self._result("message")

    def decode_vdl2_frame(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        del payload
        return self._result("vdl2")


class _CallableBackend:
    available = True
    name = "python_module"

    def __init__(self, target: Any):
        self._target = target

    def decode_message(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        return _invoke_target(self._target, "message", payload)

    def decode_vdl2_frame(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        return _invoke_target(self._target, "vdl2", payload)


def _invoke_target(target: Any, mode: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    if hasattr(target, "decode_message") and hasattr(target, "decode_vdl2_frame"):
        if mode == "message":
            result = target.decode_message(payload)
        else:
            result = target.decode_vdl2_frame(payload)
        return result if isinstance(result, dict) else None
    decode = getattr(target, "decode", None)
    if callable(decode):
        result = decode(mode, payload)
        return result if isinstance(result, dict) else None
    if callable(target):
        result = target(mode, payload)
        return result if isinstance(result, dict) else None
    return None


def _build_backend() -> Any:
    fixture_path = str(os.getenv("LIBACARS_HELPER_FIXTURE_FILE", "")).strip()
    if fixture_path:
        try:
            return _FixtureBackend(fixture_path)
        except Exception as exc:
            return _UnavailableBackend(f"fixture backend failed: {exc}")

    backend_spec = str(os.getenv("LIBACARS_HELPER_BACKEND", "")).strip()
    if backend_spec:
        try:
            if ":" in backend_spec:
                module_name, attr_name = backend_spec.split(":", 1)
            else:
                module_name, attr_name = backend_spec, ""
            module = importlib.import_module(module_name)
            target: Any = getattr(module, attr_name) if attr_name else module
            if inspect.isclass(target):
                target = target()
            return _CallableBackend(target)
        except Exception as exc:
            return _UnavailableBackend(f"python backend failed: {exc}")

    return _UnavailableBackend("no helper backend configured")


def _load_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    payload = json.loads(raw)
    return payload if isinstance(payload, dict) else {}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in {"message", "vdl2"}:
        sys.stderr.write("usage: libacars_bridge_helper.py {message|vdl2}\n")
        return 2
    mode = argv[1]
    payload = _load_payload()
    backend = _build_backend()
    if not getattr(backend, "available", False):
        return 0
    if mode == "message":
        result = backend.decode_message(payload)
    else:
        result = backend.decode_vdl2_frame(payload)
    if isinstance(result, dict):
        sys.stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
