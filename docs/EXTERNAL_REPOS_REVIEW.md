# External repo review — Task 8

Read-only. Both clones are outside this repo. Install scripts were not run and nothing was started. Nothing here is wired into an agent or the UI.

## experiential

- Clone: `E:\tools\experiential`
- Commit: `0408b0a311a06849edd79773d0a72fa883b3cbbf`
- Purpose, from its README: an open-source gateway and router for agent workflows. One OpenAI-compatible API in front of hosted, bring-your-own-key, and local models, with spend controls. It can also turn captured traffic into a custom router. It is not a market-data or news service.
- Language and runtime: Python `>=3.12` (`pyproject.toml`, package `experiential` 0.7.167). Console script `exp`. A compiled native gateway (`exp-gateway-native`) is a dependency. `exp capture` is a macOS mitmproxy capture path and needs Python 3.13.
- License: Apache License 2.0 (`LICENSE`). Permissive. Not AGPL, GPL, or a non-commercial licence. Attribution and the notice file still apply if the code is redistributed.
- Activity: version 0.7.167 is in the pinned tree. Maintainers publish a hosted platform at `platform.experientiallabs.ai`. This review did not rank individual committers.
- Dependencies and hooks: Python dependencies include `httpx`, `openai`, `posthog`, `boto3`, `google-auth`, and optional mitmproxy packages. There is no `postinstall` script. `pip install experiential` or `exp` would start setup. Neither was run.
- Outbound hosts named by the README and `.env.example`: `api.experientiallabs.ai`, `platform.experientiallabs.ai`, plus whatever provider the operator connects (OpenAI, Anthropic, Google, Azure, Bedrock, OpenRouter, Exa, E2B, Tinker). PostHog is a declared dependency, so a normal install can send product telemetry.
- Keys, names only: `ANTHROPIC_API_KEY`, `EXPLABS_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `AZURE_OPENAI_API_KEY`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `TINKER_API_KEY`, `E2B_API_KEY`, `EXA_API_KEY`, `EXP_ENDPOINT_API_KEY`. No values were copied.
- Data: OpenAI-shaped chat responses and local trace archives. Not OHLC bars and not a broker quote.
- Headless use: yes. `exp` is a CLI and `exp.load_router` returns an OpenAI-compatible client aimed at loopback. That is a library call, and it was not made.
- Telemetry: the README asks agents to upload LLM traces to the hosted platform. `posthog` is a dependency.

Recommendation: **skip**.

A forex and index paper-trading dashboard does not need another model router. Experiential could sit in front of an LLM call. It cannot supply bid, ask, a calendar event, or a company fundamental, and it must not be given the MetaTrader login. The capture tool installs a proxy certificate on macOS. Wiring it would add a secret path and a telemetry path for no order-desk feature. Do not import it.

## worldmonitor

- Clone: `E:\tools\worldmonitor`
- Commit: `761de732c597e2d588848ec95b035da415d57c31`
- Purpose, from its README: a real-time global intelligence dashboard. It aggregates news, geopolitical monitors, and infrastructure layers, and it ships finance, commodity, and energy variants. It is a situational-awareness site, not a broker.
- Language and runtime: TypeScript, Vite, and a Tauri desktop shell. Root package `world-monitor` 2.10.0. A separate `cli/` package exists.
- License: the root `LICENSE` and `package.json` are `AGPL-3.0-only`. Running a modified copy as a network service obliges you to offer the corresponding source to its users. Do not copy World Monitor source into this repo. The `cli/` package says it is MIT and that the platform itself stays AGPL-3.0. The MIT grant does not cover the dashboard.
- Activity: README marks the public site variants and the desktop binaries as stable and points at the GitHub issues board. This review did not rank individual committers.
- Dependencies and hooks: `postinstall` is `npm run inventory:facts`, which rewrites generated inventory files. `@sentry/cli` is allowed to run its install script. `npm install` was not run, so that hook did not run.
- Outbound hosts: the public sites `worldmonitor.app` and its tech, finance, commodity, energy, and happy hostnames, plus the news, macro, aviation, and conflict APIs named in `.env.example`. Sentry and an Umami compose file are in the tree, so a full install can emit error and usage telemetry. Ollama is the documented local-model option.
- Keys, names only, all optional in their example file: `OPENROUTER_API_KEY`, `FINNHUB_API_KEY`, `FRED_API_KEY`, `EIA_API_KEY`, `IMF_API_KEY`, `AVIATIONSTACK_API`, `ACLED_ACCESS_TOKEN`. Many more names are in that file. No values were copied.
- Data: news items, map layers, country-instability scores, and finance-radar panels. The shape is the dashboard's own panels, not this repo's agent schema.
- Headless use: the README documents `npx worldmonitor` and SDK packages. Those clients talk to the World Monitor service. They were not installed and not called. The dashboard itself is a Vite app, not a library this repo should import.
- Telemetry: Sentry is wired into the install allow-list, and Umami is a compose service.

Recommendation: **use sandboxed**, and not in this process.

What it could add: headlines and geopolitical context for agent 04 (News), and a coarse risk mood for agent 05 (Sentiment), if a later task fetches from the MIT CLI or a recorded export. The fetch would have to land in a strict record `{symbol_or_topic, published, headline, url, domain}` , cached per symbol or per day, under a call cap in `config_ext.yaml`, with the label `source: worldmonitor`. The model text stays untrusted and escaped, the same way Dexter summaries are escaped.

What it could not add: an order price, a fill, a stop, or a company fundamental for EURUSD. Forex and index paper trading still prices off MetaTrader 5. World Monitor is not on the order path. AGPL code stays outside this repo. This task did not wire it.

## Anchor Browser

Status only. No SDK install and no live call. A call can start a billed browser session. The vendor SDK reads `ANCHOR_API_KEY`. `ANCHORBROWSER_API_KEY` is a deprecated duplicate. When that name is non-empty, status text includes `deprecated duplicate, remove it from .env`. Later use would be a sandboxed browser for pages Dexter is not allowed to open. It can log into websites using stored credentials, so it must never receive the MetaTrader password or any HedgeFund `.env` value. It does not place orders.
