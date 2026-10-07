"""Service integrations for the UI layer."""

from .news import (
	FetchResult,
	NewsEvent,
	countdown_text,
	currencies_for_symbol,
	fetch_news,
	high_impact_within,
	next_upcoming_high_impact,
	parse_calendar_xml,
)

__all__ = [
	"FetchResult",
	"NewsEvent",
	"countdown_text",
	"currencies_for_symbol",
	"fetch_news",
	"high_impact_within",
	"next_upcoming_high_impact",
	"parse_calendar_xml",
]
