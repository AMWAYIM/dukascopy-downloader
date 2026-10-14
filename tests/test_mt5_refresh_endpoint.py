"""Refresh-endpoint wiring tests (no MT5, no server).

Calls the FastAPI view function directly with stubbed MT5/settings/DB
dependencies to protect the entries mapping and response shape.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from storage.metadata_db import MetadataDB
from web import app as app_module
from web.app import Mt5RefreshRequest, api_refresh_mt5_custom_symbols


def _live(*symbols: str) -> list[dict]:
    return [
        {"symbol": s, "ticks": 1, "first_ms": 0, "last_ms": 0, "symbol_path": ""}
        for s in symbols
    ]


class Mt5RefreshEndpointTests(unittest.TestCase):
    def _patched(self, tmp: str, live: list[dict], terminals: list[dict] | None = None):
        metadata = MetadataDB(Path(tmp) / "meta.db")
        mt5_config = {"custom_suffix": ".DUK"}
        cfg = SimpleNamespace(data_dir=Path(tmp))
        store = SimpleNamespace(get_mt5=lambda: dict(mt5_config))
        patches = [
            mock.patch.object(app_module, "db", return_value=metadata),
            mock.patch.object(app_module, "settings", return_value=cfg),
            mock.patch.object(app_module, "settings_store", return_value=store),
            mock.patch.object(app_module, "list_custom_symbols", return_value=live),
            mock.patch.object(
                app_module,
                "diagnose_mt5",
                return_value={"terminal_exe": "t.exe", "mode": "roaming"},
            ),
            mock.patch.object(
                app_module, "list_terminal_processes", return_value=terminals or [],
            ),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return metadata

    def test_refresh_syncs_and_reports_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            metadata = self._patched(temp_dir, _live("USDJPY.DUK", "EURUSD.DUK"))
            try:
                metadata.upsert_mt5_custom_symbol(symbol="USDJPY.DUK", source_symbol="USDJPY")
                metadata.upsert_mt5_custom_symbol(symbol="XAUUSD.DUK", source_symbol="XAUUSD")

                res = api_refresh_mt5_custom_symbols()

                self.assertEqual(res["removed"], ["XAUUSD.DUK"])
                self.assertEqual(res["live_count"], 2)
                self.assertFalse(res["pruned_empty"])
                self.assertEqual(
                    [s["symbol"] for s in res["symbols"]],
                    ["EURUSD.DUK", "USDJPY.DUK"],
                )
                self.assertEqual(
                    res["symbols"][0]["source_symbol"], "EURUSD",
                )
                self.assertIsNone(res["warning"])
            finally:
                metadata.close()

    def test_refresh_empty_live_keeps_cache_then_clears_on_confirm(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            metadata = self._patched(temp_dir, [])
            try:
                metadata.upsert_mt5_custom_symbol(symbol="XAUUSD.DUK", source_symbol="XAUUSD")

                res = api_refresh_mt5_custom_symbols()

                self.assertEqual(res["removed"], [])
                self.assertFalse(res["pruned_empty"])
                self.assertEqual([s["symbol"] for s in res["symbols"]], ["XAUUSD.DUK"])
                self.assertIn("left untouched", res["warning"])

                res = api_refresh_mt5_custom_symbols(Mt5RefreshRequest(prune_empty=True))

                self.assertEqual(res["removed"], ["XAUUSD.DUK"])
                self.assertTrue(res["pruned_empty"])
                self.assertEqual(res["symbols"], [])
            finally:
                metadata.close()

    def test_refresh_warns_on_multiple_terminals(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            metadata = self._patched(
                temp_dir,
                _live("USDJPY.DUK"),
                terminals=[{"pid": 1, "path": "a"}, {"pid": 2, "path": "b"}],
            )
            try:
                res = api_refresh_mt5_custom_symbols()
                self.assertIn("2 MT5 terminals", res["warning"])
            finally:
                metadata.close()

    def test_skip_relist_clears_cache_without_mt5_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            metadata = self._patched(temp_dir, _live("USDJPY.DUK"))
            try:
                metadata.upsert_mt5_custom_symbol(symbol="XAUUSD.DUK", source_symbol="XAUUSD")
                with mock.patch.object(
                    app_module, "list_custom_symbols",
                    side_effect=AssertionError("must not relist MT5"),
                ):
                    res = api_refresh_mt5_custom_symbols(
                        Mt5RefreshRequest(prune_empty=True, skip_relist=True),
                    )
                self.assertEqual(res["removed"], ["XAUUSD.DUK"])
                self.assertTrue(res["pruned_empty"])
                self.assertEqual(res["live_count"], 0)
                self.assertEqual(res["symbols"], [])
            finally:
                metadata.close()

    def test_skip_relist_requires_prune_empty(self) -> None:
        from fastapi import HTTPException

        with tempfile.TemporaryDirectory() as temp_dir:
            metadata = self._patched(temp_dir, [])
            try:
                with self.assertRaises(HTTPException) as ctx:
                    api_refresh_mt5_custom_symbols(Mt5RefreshRequest(skip_relist=True))
                self.assertEqual(ctx.exception.status_code, 400)
            finally:
                metadata.close()

    def test_refresh_body_is_optional_at_http_layer(self) -> None:
        """Bodiless POST must be accepted (frontend sends no body by default)."""
        from pydantic_core import PydanticUndefined

        from web.app import app

        route = next(
            r for r in app.routes
            if getattr(r, "path", "") == "/api/mt5/custom-symbols/refresh"
            and "POST" in getattr(r, "methods", set())
        )
        self.assertIsNotNone(route.body_field)
        assert route.body_field is not None
        self.assertIsNot(route.body_field.default, PydanticUndefined)
        self.assertIsNone(route.body_field.default)


if __name__ == "__main__":
    unittest.main()
