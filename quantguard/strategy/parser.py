"""
Strategy parsers.

Every parser implements the same interface: take a trader's plain-
English description (plus any answers to previous clarifying
questions), and return a ParseResult - either the fully structured
strategy, or a list of specific questions still needed before it's
safe to move forward.

Four implementations:
- MockStrategyParser: simple rule-based extraction (regex/keywords).
  No API key needed, works fully offline - this is what lets the
  whole flow be built and tested without a live LLM connection yet.
  It can build STRUCTURED entry conditions (see conditions.py) for a
  handful of simple, common patterns (a single RSI threshold, or a
  percent-drop-from-high) - anything more complex than that gets
  recorded as a plain description only, honestly leaving the strategy
  "complete but not yet executable" rather than guessing at structure.
- ClaudeStrategyParser / GeminiStrategyParser / GrokStrategyParser:
  real LLM-backed versions that can understand genuinely complex,
  multi-indicator strategies and build the full structured condition
  tree - not just the simple cases the mock parser can handle.

This mirrors the MockConnector / BinanceConnector pattern used for
brokers: same interface, swap the implementation, nothing else in
the app needs to change.

IMPORTANT DESIGN RULE: the LLM (or the mock parser) is used ONLY to
translate a description into structured rules, ONE TIME, with the
trader reviewing and approving the result. It is never called again
to make live trading decisions - the strategy engine (a separate,
plain deterministic piece, built next) is what actually watches
prices and fires orders, mechanically following the approved rules.
"""

import json
import re
from abc import ABC, abstractmethod

from .models import StrategyConfig, ParseResult, StrategyStatus
from .conditions import Indicator, Operator, IndicatorRef, Condition, ConditionGroup


class StrategyParser(ABC):
    @abstractmethod
    def parse(self, description: str, existing: StrategyConfig = None) -> ParseResult:
        """
        `description` is the trader's latest message (either the
        original strategy description, or their answer to a
        clarifying question). `existing` is what's been figured out
        so far, if this isn't the first message in the conversation.
        """
        ...


QUESTION_TEXT = {
    "symbol": "Which symbol should this strategy trade (e.g. BTCUSDT)?",
    "side": "Is this a BUY (long) strategy or a SELL (short) strategy?",
    "entry_description": "What exact condition should trigger an entry? (e.g. \"price drops 3% from the 20-day high\", or \"RSI below 30\")",
    "position_size": "How large should each position be (e.g. 0.1 BTC, or a dollar amount)?",
    "stop_loss_pct": "What stop-loss should protect this position (as a % below entry)? This is required - no strategy runs without one.",
}


def generate_warnings(strategy: StrategyConfig) -> list[str]:
    """
    Advisory pushback on a COMPLETE strategy - not blocking, not
    guessing at intent, just flagging things a careful trader would
    want to double-check before approving. Used as a shared final
    pass by every parser (including the LLM-backed ones, as a
    consistent baseline on top of whatever they generate themselves).
    """
    warnings = []

    if strategy.stop_loss_pct is not None:
        if strategy.stop_loss_pct < 0.5:
            warnings.append(
                f"Your stop-loss is very tight ({strategy.stop_loss_pct:g}%) - on a volatile "
                f"symbol this could get triggered by normal price noise rather than a real reversal."
            )
        elif strategy.stop_loss_pct > 15:
            warnings.append(
                f"Your stop-loss is quite wide ({strategy.stop_loss_pct:g}%) - that's a large "
                f"potential loss on any single trade. Worth double-checking this is intentional."
            )

    if strategy.take_profit_pct is None:
        warnings.append(
            "No take-profit is set - this strategy will only exit via the stop-loss (or a "
            "manual pause). Consider whether you want a profit target too."
        )
    elif strategy.stop_loss_pct is not None and strategy.take_profit_pct < strategy.stop_loss_pct:
        warnings.append(
            f"Your take-profit ({strategy.take_profit_pct:g}%) is smaller than your stop-loss "
            f"({strategy.stop_loss_pct:g}%) - even a strategy that wins half the time could lose "
            f"money overall, since losses are bigger than wins."
        )

    if strategy.max_daily_loss is None:
        warnings.append(
            "No max daily loss set for this strategy specifically - it'll rely on the account-wide "
            "kill-switch default instead."
        )

    return warnings


class MockStrategyParser(StrategyParser):
    """
    Rule-based extraction using regex/keyword matching. Good enough to
    pull out clearly-stated numbers and symbols, and honest about what
    it can't confidently extract - anything ambiguous becomes a
    clarifying question rather than a guess.
    """

    SYMBOL_PATTERN = re.compile(r"\b([A-Z]{2,10}(?:USDT|USDC|USD|BTC|EUR)?)\b")
    PERCENT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent\b|pct\b)", re.IGNORECASE)
    QUANTITY_PATTERN = re.compile(r"\b(\d+(?:\.\d+)?)\s*(BTC|ETH|units?|shares?|coins?)\b", re.IGNORECASE)

    # Simple, recognizable patterns this rule-based parser CAN turn
    # into real structured conditions. Anything outside these patterns
    # still gets recorded as entry_description text, just not
    # entry_conditions - an honest "understood the words, can't
    # mechanically execute it yet" signal, not a silent guess.
    RSI_PATTERN = re.compile(r"rsi\s*\(?(\d+)?\)?\s*(?:is\s+)?(below|under|less than|<|above|over|greater than|>)\s*(\d+)", re.IGNORECASE)
    DROP_FROM_HIGH_PATTERN = re.compile(r"drops?\s+(\d+(?:\.\d+)?)\s*%.*?(\d+)[\s-]*day\s+high", re.IGNORECASE)
    TIMEFRAME_PATTERN = re.compile(
        r"\b(\d+)[\s-]*(minute|min|hour|hr|day)s?\b|"
        r"\b(1m|3m|5m|15m|30m|1h|4h|1d)\b|"
        r"\bdaily\b|\bhourly\b",
        re.IGNORECASE,
    )

    @staticmethod
    def _normalize_timeframe(raw: str) -> str:
        """Turns a matched phrase like '1-minute' or '5 min' or 'daily'
        into the short form (1m, 5m, 1h, 1d) the engine expects."""
        raw = raw.lower().strip()
        if raw in ("daily",):
            return "1d"
        if raw in ("hourly",):
            return "1h"
        # Already in short form, e.g. "5m", "1h", "4h"
        if re.fullmatch(r"\d+[mhd]", raw):
            return raw
        match = re.match(r"(\d+)[\s-]*(minute|min|hour|hr|day)s?", raw)
        if match:
            number, unit = match.group(1), match.group(2)
            unit_letter = "m" if unit.startswith("min") else ("h" if unit.startswith("h") else "d")
            return f"{number}{unit_letter}"
        return "1h"  # shouldn't normally reach here given the pattern that matched, but a safe fallback

    def parse(self, description: str, existing: StrategyConfig = None) -> ParseResult:
        strategy = existing or StrategyConfig()
        text = description.strip()
        lower = text.lower()

        # --- bare-number replies ---
        # If the trader's entire reply is just a number (optionally with
        # $ or %), they're almost certainly answering the ONE specific
        # question just asked, not writing a new full description. The
        # general extraction below requires context words ("0.1 BTC",
        # "stop loss 2%") that a bare "0.05" reply won't have - without
        # this, a plain numeric answer would be silently ignored and the
        # same question would just get asked again.
        bare_number_match = re.fullmatch(r"\$?\s*(\d+(?:\.\d+)?)\s*%?", text)
        if bare_number_match and existing is not None:
            value = float(bare_number_match.group(1))
            is_percent = text.strip().endswith("%")
            if is_percent and strategy.stop_loss_pct is None:
                strategy.stop_loss_pct = value
            elif is_percent and strategy.take_profit_pct is None:
                strategy.take_profit_pct = value
            elif not is_percent and strategy.position_size is None:
                strategy.position_size = value
            elif not is_percent and strategy.max_daily_loss is None:
                strategy.max_daily_loss = value

            missing = strategy.missing_fields()
            if missing:
                return ParseResult(status=StrategyStatus.NEEDS_CLARIFICATION, strategy=strategy,
                                    questions=[QUESTION_TEXT[f] for f in missing])
            return ParseResult(status=StrategyStatus.READY_FOR_REVIEW, strategy=strategy, questions=[])

        # --- side ---
        if strategy.side is None:
            if any(w in lower for w in ["buy", "long", "go long"]):
                strategy.side = "BUY"
            elif any(w in lower for w in ["sell", "short", "go short"]):
                strategy.side = "SELL"

        # --- symbol ---
        # Prefer symbols ending in a specific quote currency (BTCUSDT,
        # ETHUSDT) since those are unambiguous. Skip anything that's
        # immediately preceded by a number (e.g. "0.1 BTC" - that's a
        # quantity unit, not the traded symbol).
        if strategy.symbol is None:
            best_candidate = None
            for match in self.SYMBOL_PATTERN.finditer(text):
                candidate = match.group(1)
                if candidate in ("BUY", "SELL", "USD", "PCT", "STOP", "LOSS", "PROFIT"):
                    continue
                preceding_text = text[:match.start()]
                preceded_by_number = bool(re.search(r"\d\s*$", preceding_text))
                is_full_pair = any(candidate.endswith(suf) for suf in ("USDT", "USDC", "EUR"))

                if is_full_pair and not preceded_by_number:
                    best_candidate = candidate
                    break  # a full pair like BTCUSDT is the strongest signal - stop here
                elif not preceded_by_number and best_candidate is None and len(candidate) <= 5:
                    best_candidate = candidate  # weaker fallback, keep looking for a better match
            strategy.symbol = best_candidate

        # --- stop loss / take profit percentages ---
        # Look at a window of text just before each percentage for the
        # words "stop"/"loss" vs "profit"/"target"/"take". Deliberately
        # NOT splitting the text on periods/commas first - that would
        # break decimal numbers like "1.5%" into "1" and "5%".
        for pct_match in self.PERCENT_PATTERN.finditer(text):
            pct = float(pct_match.group(1))
            window_start = max(0, pct_match.start() - 30)
            window = text[window_start:pct_match.start()].lower()
            if strategy.stop_loss_pct is None and ("stop" in window or "loss" in window):
                strategy.stop_loss_pct = pct
            elif strategy.take_profit_pct is None and ("profit" in window or "target" in window or "take" in window):
                strategy.take_profit_pct = pct

        # --- position size ---
        if strategy.position_size is None:
            qty_match = self.QUANTITY_PATTERN.search(text)
            if qty_match:
                strategy.position_size = float(qty_match.group(1))

        # --- max daily loss (dollar amount) ---
        if strategy.max_daily_loss is None:
            dollar_match = re.search(r"\$\s*(\d+(?:,\d{3})*(?:\.\d+)?)\s*(?:per day|daily|a day)", lower)
            if dollar_match:
                strategy.max_daily_loss = float(dollar_match.group(1).replace(",", ""))

        # --- entry description (plain text, always attempted) ---
        if strategy.entry_description is None:
            cond_match = re.search(r"(?:when|if|once)\s+(.+?)(?:[.,]|$)", text, re.IGNORECASE)
            if cond_match:
                strategy.entry_description = cond_match.group(1).strip()

        # --- timeframe (e.g. "on a 1-minute timeframe", "5m chart", "daily") ---
        timeframe_match = self.TIMEFRAME_PATTERN.search(text)
        if timeframe_match:
            strategy.default_timeframe = self._normalize_timeframe(timeframe_match.group(0))

        # --- entry conditions (STRUCTURED, only for simple recognizable
        # patterns) --- this is the "can we actually execute this"
        # upgrade: a single RSI threshold or a percent-drop-from-high
        # are common and easy enough to structure reliably with regex.
        # Anything more complex (multiple indicators, AND/OR logic,
        # crossovers) is NOT attempted here - that's exactly the gap a
        # real LLM parser (Claude/Gemini/Grok) closes, by understanding
        # arbitrary phrasing instead of matching fixed patterns.
        if strategy.entry_conditions is None:
            rsi_match = self.RSI_PATTERN.search(text)
            drop_match = self.DROP_FROM_HIGH_PATTERN.search(text)

            if rsi_match:
                period = int(rsi_match.group(1)) if rsi_match.group(1) else 14  # 14 is RSI's standard default period
                direction = rsi_match.group(2).lower()
                threshold = float(rsi_match.group(3))
                operator = Operator.LT if direction in ("below", "under", "less than", "<") else Operator.GT
                strategy.entry_conditions = ConditionGroup(logic="AND", items=[
                    Condition(left=IndicatorRef(Indicator.RSI, period=period), operator=operator, right=threshold)
                ])
            elif drop_match:
                drop_pct = float(drop_match.group(1))
                lookback_days = int(drop_match.group(2))
                strategy.entry_conditions = ConditionGroup(logic="AND", items=[
                    Condition(
                        left=IndicatorRef(Indicator.PERCENT_CHANGE_FROM_HIGH, period=lookback_days),
                        operator=Operator.LTE,
                        right=-drop_pct,
                    )
                ])

        missing = strategy.missing_fields()
        if missing:
            questions = [QUESTION_TEXT[f] for f in missing]
            return ParseResult(status=StrategyStatus.NEEDS_CLARIFICATION, strategy=strategy, questions=questions)

        return ParseResult(status=StrategyStatus.READY_FOR_REVIEW, strategy=strategy, questions=[],
                            warnings=generate_warnings(strategy))


def _build_system_prompt(existing: StrategyConfig) -> str:
    """Shared instructions used by every LLM-backed parser (Claude, Gemini, Grok) -
    keeps their behavior consistent regardless of which model is answering."""
    existing_json = json.dumps(existing.to_dict()) if existing else "{}"
    return f"""You translate a trader's plain-English trading strategy into a structured JSON format. You do NOT invent or suggest strategy ideas - you only extract what the trader has actually said.

Required fields: symbol, side (BUY or SELL), entry_description (a plain-English summary of the entry condition), position_size (a number), stop_loss_pct (a number, required - never leave this blank or invent a default).
Optional fields: take_profit_pct, max_daily_loss, default_timeframe (a short code like "1m", "5m", "1h", "4h", "1d" - defaults to "1h" if the trader doesn't mention a specific chart timeframe).

ALSO try to build "entry_conditions": a STRUCTURED, mechanically-executable version of the entry logic, using this schema:
- An IndicatorRef is: {{"indicator": one of ["PRICE","RSI","EMA","SMA","VOLUME","HIGH","LOW","PERCENT_CHANGE_FROM_HIGH"], "period": an integer or null (null only for PRICE/VOLUME which need no period), "timeframe": a short code like "1m"/"5m"/"1h"/"4h"/"1d", or null to use the strategy's default_timeframe}}
- A Condition is: {{"kind": "condition", "left": <IndicatorRef>, "operator": one of ["<",">","<=",">=","=="], "right": {{"type": "number", "value": <number>}} OR {{"type": "indicator", "value": <IndicatorRef>}}}}
- A ConditionGroup is: {{"logic": "AND" or "OR", "items": [<Condition or nested ConditionGroup, each tagged with "kind": "condition" or "kind": "group">]}}

If the trader mentions a specific timeframe for a particular indicator (e.g. "RSI on the 1-minute chart"), set that IndicatorRef's own "timeframe" field. If they only mention one overall timeframe for the whole strategy, put it in the top-level "default_timeframe" instead and leave each IndicatorRef's "timeframe" as null.

Example: "RSI(14) below 30 on the 1-minute chart AND price above the 200-day EMA" becomes:
{{"default_timeframe": "1h", "entry_conditions": {{"logic": "AND", "items": [
  {{"kind": "condition", "left": {{"indicator": "RSI", "period": 14, "timeframe": "1m"}}, "operator": "<", "right": {{"type": "number", "value": 30}}}},
  {{"kind": "condition", "left": {{"indicator": "PRICE", "period": null, "timeframe": null}}, "operator": ">", "right": {{"type": "indicator", "value": {{"indicator": "EMA", "period": 200, "timeframe": null}}}}}}
]}}}}

If the trader's entry logic is too vague, idiosyncratic, or doesn't map to these indicators (e.g. "buy when it feels right", "buy on good news"), set entry_conditions to null - do NOT force a structure onto something that isn't really indicator-based. A strategy can still be approved with entry_conditions null; it just won't be executable by an automated engine until it's refined into something structured.

Known so far: {existing_json}

When the strategy is READY_FOR_REVIEW (complete), ALSO include a "warnings" field: a list of short, specific, advisory notes about anything that looks risky or possibly a mistake - NOT blocking, just flagging things a careful trader would want to double-check. Good examples: an unusually tight stop-loss that normal price noise could trigger, an entry condition that's almost always true (weak signal), a take-profit smaller than the stop-loss (meaning even 50% win rate loses money overall), leverage or position size that seems large relative to what they've described, or a strategy with no real exit plan beyond the stop-loss. Only include genuinely specific concerns about THIS strategy - don't pad the list with generic disclaimers, and don't invent a concern just to have something to say. An empty list is fine if nothing stands out.

Given the trader's latest message, respond with ONLY valid JSON, no other text, no markdown code fences, in this exact shape:
{{"status": "READY_FOR_REVIEW", "strategy": {{"symbol": ..., "side": ..., "entry_description": ..., "entry_conditions": <ConditionGroup or null>, "default_timeframe": ..., "position_size": ..., "stop_loss_pct": ..., "take_profit_pct": ..., "max_daily_loss": ...}}, "warnings": ["specific concern 1", ...]}}
or, if required fields are still missing or ambiguous:
{{"status": "NEEDS_CLARIFICATION", "strategy": {{...fields extracted so far, same shape...}}, "questions": ["specific question 1", "specific question 2"]}}

Never guess a stop_loss_pct if the trader didn't state one - always ask instead."""


def _parse_llm_json_response(raw: str) -> ParseResult:
    """Shared response parsing - strips markdown fences some models add
    despite instructions, then parses into the same ParseResult shape
    every parser (mock or LLM-backed) returns. Warnings from the LLM
    itself are combined with the same deterministic baseline checks
    (generate_warnings) the mock parser uses - the LLM can catch
    nuanced things regex can't, but the baseline guarantees a
    consistent floor regardless of what the model did or didn't think
    to mention."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned.strip())
    data = json.loads(cleaned)
    strategy = StrategyConfig.from_dict(data.get("strategy", {}))
    status = StrategyStatus(data["status"])
    questions = data.get("questions", [])
    llm_warnings = data.get("warnings", [])

    # If the LLM attempted structured entry_conditions but
    # StrategyConfig.from_dict had to fail-safe them back to None (an
    # unsupported indicator, malformed shape, etc.), surface that to
    # the trader explicitly - "not structured" should never be silent
    # when the model actually tried and failed, versus correctly
    # deciding not to attempt it at all.
    attempted_conditions = data.get("strategy", {}).get("entry_conditions")
    if attempted_conditions and strategy.entry_conditions is None:
        llm_warnings = list(llm_warnings) + [
            "This strategy's entry logic uses something outside the currently supported "
            "indicators (RSI, EMA, SMA, price, volume, high/low, percent-change-from-high) - "
            "it's saved as a description only and won't be automatable until it's rephrased "
            "in terms of a supported indicator."
        ]

    warnings = []
    if status == StrategyStatus.READY_FOR_REVIEW:
        warnings = list(llm_warnings) + generate_warnings(strategy)

    return ParseResult(status=status, strategy=strategy, questions=questions, warnings=warnings)


class ClaudeStrategyParser(StrategyParser):
    """
    Real LLM-backed parser using Claude. Requires ANTHROPIC_API_KEY.

    Sends the trader's description (and conversation so far) to Claude
    with instructions to extract the structured fields OR ask specific
    clarifying questions - same contract as MockStrategyParser, just
    with real language understanding instead of regex, and able to
    build structured entry_conditions for genuinely complex,
    multi-indicator strategies.
    """

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6"):
        import anthropic  # imported here so the app works without the package installed
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def parse(self, description: str, existing: StrategyConfig = None) -> ParseResult:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=1500,
            system=_build_system_prompt(existing),
            messages=[{"role": "user", "content": description}],
        )
        raw = response.content[0].text
        return _parse_llm_json_response(raw)


class GeminiStrategyParser(StrategyParser):
    """
    Real LLM-backed parser using Google's Gemini. Requires GEMINI_API_KEY.

    Worth knowing: Gemini has a genuinely free API tier (no credit card
    required), unlike Anthropic's and xAI's limited trial credits - a
    good option for testing this feature at zero cost, ongoing.

    Uses "gemini-flash-latest" by default rather than a pinned version
    number - Google regularly retires specific model versions (this is
    exactly what happened with the previous default, gemini-2.5-flash,
    which stopped working for new API users), and the "-latest" alias
    automatically points at whatever the current stable Flash model is,
    so this doesn't need a code fix every time Google ships a new one.
    Override with a specific version via the GEMINI_MODEL environment
    variable if you want pinned, unchanging behavior instead.
    """

    def __init__(self, api_key: str, model: str = None):
        import google.generativeai as genai  # imported here so the app works without the package installed
        import os
        genai.configure(api_key=api_key)
        self.model_name = model or os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
        self.genai = genai

    def parse(self, description: str, existing: StrategyConfig = None) -> ParseResult:
        model = self.genai.GenerativeModel(
            self.model_name,
            system_instruction=_build_system_prompt(existing),
        )
        response = model.generate_content(description)
        return _parse_llm_json_response(response.text)


class GrokStrategyParser(StrategyParser):
    """
    Real LLM-backed parser using xAI's Grok. Requires XAI_API_KEY.

    Grok's API is OpenAI-compatible, so this uses the `openai` package
    pointed at xAI's endpoint rather than a separate xAI-specific SDK.
    """

    def __init__(self, api_key: str, model: str = "grok-4"):
        import openai  # imported here so the app works without the package installed
        self.client = openai.OpenAI(api_key=api_key, base_url="https://api.x.ai/v1")
        self.model = model

    def parse(self, description: str, existing: StrategyConfig = None) -> ParseResult:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": _build_system_prompt(existing)},
                {"role": "user", "content": description},
            ],
        )
        raw = response.choices[0].message.content
        return _parse_llm_json_response(raw)
