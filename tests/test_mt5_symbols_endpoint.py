"""Symbol-listing endpoint wiring tests (no MT5, no server).

Calls the FastAPI view function directly with stubbed settings and
enumeration to protect the response shape and cache semantics.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

from web import app as app_module
from web.app import api_mt5_symbols


class Mt5SymbolsEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self._cache_patch = mock.patch.object(
            app_module, "_MT5_SYMBOLS_CACHE", {"ts": 0.0, "symbols": []}
        )
        self._cache_patch.start()
        self.addCleanup(self._cache_patch.stop)

    def _patched(self, symbols, validate_ok: bool = True):
        mt5_config = {"custom_suffix": ".DUK"}
        store = SimpleNamespace(get_mt5=lambda: dict(mt5_config))
        validate = mock.patch.object(
            app_module.Mt5Settings, "validate_config",
            side_effect=None if validate_ok else ValueError("bad config"),
        )
        enum = mock.patch.object(
            app_module, "list_terminal_symbols", return_value=list(symbols)
        )
        patches = [
            mock.patch.object(app_module, "settings_store", return_value=store),
            validate,
            enum,
        ]
        for patcher in patches[:-1]:
            patcher.start()
            self.addCleanup(patcher.stop)
        enum_mock = patches[-1].start()
        self.addCleanup(patches[-1].stop)
        return enum_mock

    def test_returns_symbols_and_caches(self) -> None:
        enum = self._patched(["EURUSD", "US30"])
        first = api_mt5_symbols(refresh=False)
        second = api_mt5_symbols(refresh=False)
        self.assertEqual(first["symbols"], ["EURUSD", "US30"])
        self.assertEqual(second["symbols"], ["EURUSD", "US30"])
        self.assertEqual(enum.call_count, 1)

    def test_refresh_bypasses_cache(self) -> None:
        enum = self._patched(["EURUSD"])
        api_mt5_symbols(refresh=False)
        api_mt5_symbols(refresh=True)
        self.assertEqual(enum.call_count, 2)

    def test_invalid_config_returns_400(self) -> None:
        self._patched(["EURUSD"], validate_ok=False)
        with self.assertRaises(HTTPException) as ctx:
            api_mt5_symbols(refresh=False)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_enumeration_failure_returns_502(self) -> None:
        self._patched([], validate_ok=True)
        with mock.patch.object(
            app_module, "list_terminal_symbols",
            side_effect=RuntimeError("terminal offline"),
        ):
            with self.assertRaises(HTTPException) as ctx:
                api_mt5_symbols(refresh=False)
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("terminal offline", str(ctx.exception.detail))


if __name__ == "__main__":
    unittest.main()
