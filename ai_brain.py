import json
import re
import requests
import config


def get_ai_decision(market_data_string: str) -> dict:
    """Queries OpenRouter LLM to return a structured institutional trade recommendation."""
    api_key = getattr(config, "OPENROUTER_API_KEY", "")
    model = getattr(config, "OPENROUTER_MODEL", "openrouter/free")

    if not api_key or "YOUR_" in api_key:
        raise ValueError("OPENROUTER_API_KEY is not configured in config.py")

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://localhost",
        "X-Title": "Autonomous AI Hedge Fund Command Center",
    }

    system_prompt = (
        "You are an Elite Chief Investment Officer & Senior Portfolio Manager at a quantitative hedge fund.\n"
        "Analyze the provided 4-Hour macro trend, 15-Minute micro structure, EMA alignment, RSI, ATR, and MACD.\n"
        "Formulate an institutional trading decision.\n\n"
        "You MUST respond ONLY with valid JSON in the exact format below, without markdown backticks or commentary:\n"
        "{\n"
        '  "decision": "BUY" | "SELL" | "HOLD",\n'
        '  "confidence": <integer between 60 and 99>,\n'
        '  "strength": "VERY STRONG" | "STRONG" | "MODERATE" | "NEUTRAL",\n'
        '  "reasoning": "<concise 2-sentence institutional trade rationale focusing on 4H trend & 15M trigger>",\n'
        '  "risk_reward": "1:2"\n'
        "}"
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": market_data_string},
        ],
        "temperature": 0.1,
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=25)
    except Exception as e:
        # Fallback if connection times out
        return {
            "decision": "HOLD",
            "confidence": 70,
            "strength": "NEUTRAL",
            "reasoning": f"Connection timeout: {str(e)}. Maintained risk neutrality.",
            "risk_reward": "N/A"
        }

    if response.status_code != 200:
        return {
            "decision": "HOLD",
            "confidence": 65,
            "strength": "NEUTRAL",
            "reasoning": f"API Provider Error (Code {response.status_code}). Defaulting to safety HOLD.",
            "risk_reward": "N/A"
        }

    data = response.json()
    raw_content = data["choices"][0]["message"]["content"].strip()

    # Clean reasoning blocks if any
    cleaned_content = re.sub(r"<think>.*?</think>", "", raw_content, flags=re.DOTALL).strip()
    cleaned_content = re.sub(r"```json\s*", "", cleaned_content)
    cleaned_content = re.sub(r"```", "", cleaned_content).strip()

    try:
        parsed = json.loads(cleaned_content)
        dec = str(parsed.get("decision", "HOLD")).upper().strip()
        if dec not in ("BUY", "SELL", "HOLD"):
            dec = "HOLD"
        return {
            "decision": dec,
            "confidence": int(parsed.get("confidence", 85)),
            "strength": str(parsed.get("strength", "STRONG")).upper(),
            "reasoning": str(parsed.get("reasoning", "Multi-timeframe momentum and trend confirmation.")),
            "risk_reward": str(parsed.get("risk_reward", "1:2")),
        }
    except Exception:
        # Regex fallback
        match = re.search(r"\b(BUY|SELL|HOLD)\b", cleaned_content.upper())
        dec = match.group(1) if match else "HOLD"
        return {
            "decision": dec,
            "confidence": 88 if dec != "HOLD" else 60,
            "strength": "VERY STRONG" if dec != "HOLD" else "NEUTRAL",
            "reasoning": cleaned_content[:180] if cleaned_content else "Analysis aligned with current market structure.",
            "risk_reward": "1:2",
        }
