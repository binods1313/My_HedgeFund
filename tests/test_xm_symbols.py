r"""Tests for broker symbol naming and asset classification.

The approved MetaQuotes-Demo whitelist uses the three live plain-name forex
crosses and four FULL metals. The old suffixed crypto and index names are not
whitelisted; classification remains based on each symbol's broker path.

Classification is by path. Two bugs are pinned here:

  - 'Cryptocurrencies\Ultra Low\BTCUSD#' CONTAINS the substring 'CURRENCIES',
    so a forex-first test filed bitcoin under Forex Currencies.
  - 'COIN' on another broker was Coinbase Global Inc, an equity, purely by
    ticker. A name-based classifier would file it as crypto.

Run: python -m pytest tests/test_xm_symbols.py -v
"""
from pathlib import Path

import pytest

from data_engine import _classify_symbol
from config_loader import load_config

REPO_ROOT = Path(__file__).resolve().parent.parent


class FakeSymbolInfo:
    """Minimal stand-in carrying only what the classifier reads."""

    def __init__(self, name, path, description=""):
        self.name = name
        self.path = path
        self.description = description


class TestAssetClassification:

    @pytest.mark.parametrize("name,path,expected", [
        # The regression: this path contains 'CURRENCIES'.
        ("BTCUSD#", "Cryptocurrencies\\Ultra Low\\BTCUSD#", "Crypto Assets"),
        ("ETHUSD#", "Cryptocurrencies\\Ultra Low\\ETHUSD#", "Crypto Assets"),
        # The trap: an equity that reads as crypto by ticker.
        ("COIN", "Stocks\\US\\Coinbase", "Indices & Equities"),
        ("BTCU", "Nasdaq\\ETF\\BTCU", "Indices & Equities"),
        ("ETHV", "Nasdaq\\ETF\\ETHV", "Indices & Equities"),
        # Forex majors and crosses.
        ("EURUSD", "Forex\\Standard\\Majors\\EURUSD", "Forex Currencies"),
        ("EURGBP", "Forex\\Standard\\Majors\\EURGBP", "Forex Currencies"),
        # Broker index contracts.
        ("US500Cash#", "Derivatives\\Cash Ultra Low\\Indices\\US500Cash#",
         "Indices & Equities"),
        ("UK100Cash#", "Derivatives\\Cash Ultra Low\\Indices\\UK100Cash#",
         "Indices & Equities"),
        # Metals, for completeness.
        ("XAUUSD", "Metals\\Precious\\XAUUSD", "Metals & Commodities"),
        # Nothing recognisable.
        ("MYSTERY", "Weird\\Unknown", "Unclassified"),
    ])
    def test_classification(self, name, path, expected):
        assert _classify_symbol(FakeSymbolInfo(name, path)) == expected

    def test_bitcoin_is_not_classified_as_forex(self):
        """The regression this file exists for.

        'Cryptocurrencies' contains 'CURRENCIES', so testing forex first put
        bitcoin in the wrong dropdown.
        """
        info = FakeSymbolInfo("BTCUSD#", "Cryptocurrencies\\Ultra Low\\BTCUSD#")
        assert _classify_symbol(info) == "Crypto Assets"

    def test_bitcoin_is_not_a_metal(self):
        """Requested placement was metals; the broker path says otherwise.

        Bitcoin is not a commodity. Filing it under metals would tell the
        operator a crypto instrument is a commodity and would apply metals
        assumptions to it.
        """
        info = FakeSymbolInfo("BTCUSD#", "Cryptocurrencies\\Ultra Low\\BTCUSD#")
        assert _classify_symbol(info) != "Metals & Commodities"

    def test_ticker_alone_never_decides_the_class(self):
        """'COIN' is an equity despite reading as a cryptocurrency."""
        equity = FakeSymbolInfo("COIN", "Stocks\\US\\Coinbase")
        assert _classify_symbol(equity) != "Crypto Assets"

    def test_empty_path_is_unclassified(self):
        assert _classify_symbol(FakeSymbolInfo("X", "")) == "Unclassified"


class TestWhitelistUsesBrokerNames:

    @staticmethod
    def _whitelist():
        """The per-asset-class mapping from config.yaml.

        cfg.symbols is a validated SymbolSettings model, so the per-class lists
        are reached through attribute access rather than subscripting.
        """
        return load_config().symbols.whitelist

    def test_config_loads(self):
        cfg = load_config()
        assert cfg.trading.tradable_symbols

    def test_old_crypto_and_index_symbols_are_not_whitelisted(self):
        """The approved proposal intentionally leaves these classes empty."""
        cfg = load_config()
        old_names = {
            "BTCUSD#", "ETHUSD#", "EURGBP#", "EURJPY#", "GBPJPY#",
            "US500Cash#", "US30Cash#",
            "US100Cash#", "UK100Cash#",
        }
        assert old_names.isdisjoint(cfg.trading.tradable_symbols)
        assert cfg.symbols.whitelist["crypto"] == []
        assert cfg.symbols.whitelist["indices"] == []

    def test_approved_whitelist_and_explicit_cross_mappings(self):
        cfg = load_config()
        expected = {
            "EURUSD", "GBPUSD", "USDJPY", "USDCAD", "USDCHF", "AUDUSD",
            "NZDUSD", "EURGBP", "EURJPY", "GBPJPY", "AUDJPY", "CHFJPY",
            "XAUUSD", "XAGUSD", "XAUEUR", "XAUAUD",
        }
        assert set(cfg.trading.tradable_symbols) == expected
        assert cfg.symbols.mapping == {
            "EURGBP#": "EURGBP",
            "EURJPY#": "EURJPY",
            "GBPJPY#": "GBPJPY",
        }

    def test_both_whitelists_agree(self):
        """config_loader enforces this, so a successful load proves it."""
        cfg = load_config()
        flat = {s.upper() for s in cfg.trading.tradable_symbols}
        per_class = set()
        for symbols in self._whitelist().values():
            per_class.update(s.upper() for s in symbols)
        assert flat == per_class, (
            f"tradable_symbols and symbols.whitelist disagree: "
            f"{flat ^ per_class}"
        )

    def test_indices_use_the_broker_contract_names(self):
        indices = self._whitelist().get("indices", [])
        assert not indices, "no index symbols are approved in this whitelist"

    def test_bitcoin_is_under_crypto_not_metals(self):
        whitelist = self._whitelist()
        assert whitelist.get("crypto", []) == []
        assert not any("BTC" in s for s in
                       whitelist.get("metals_commodities", [])), (
            "bitcoin is not a commodity; it must not be filed under metals"
        )

    def test_every_whitelisted_symbol_resolves_on_live_terminal(self):
        try:
            import MetaTrader5 as mt5
        except ImportError:
            pytest.skip("MetaTrader5 package is not installed")

        if not mt5.initialize(timeout=10_000):
            pytest.skip(f"MetaTrader5 terminal unavailable: {mt5.last_error()}")

        try:
            missing = [
                symbol for symbol in load_config().trading.tradable_symbols
                if mt5.symbol_info(symbol) is None
            ]
        finally:
            mt5.shutdown()

        assert not missing, f"whitelist symbols absent from live terminal: {missing}"


class TestDiscoveryActivatesHiddenSymbols:
    """XM symbols are invisible until symbol_select() adds them.

    Without activation the dropdown omits a symbol that is present and
    tradable, which reads as 'not offered' rather than 'not selected'.
    """

    def test_dropdown_selects_before_filtering(self):
        source = (REPO_ROOT / "data_engine.py").read_text(encoding="utf-8")
        body = source.split("def get_available_symbols", 1)[1]
        select_at = body.find("symbol_select")
        assert select_at > 0, "the dropdown must call symbol_select"
        # The catalogue is rebuilt after selection, otherwise a newly
        # activated symbol is still filtered out.
        assert "_catalogue()" in body
        recatalogue_at = body.rfind("_catalogue()")
        assert recatalogue_at > select_at, (
            "the catalogue must be rebuilt after symbol_select, or a newly "
            "activated symbol never appears"
        )

    def test_selection_places_no_order(self):
        """Activating a quote feed is not an order."""
        source = (REPO_ROOT / "data_engine.py").read_text(encoding="utf-8")
        body = source.split("def get_available_symbols", 1)[1]
        body = body.split("\ndef ", 1)[0]
        assert "order_send" not in body
        assert "order_check" not in body