from __future__ import annotations

import re

from kraken_telegram_gateway.gateway.schemas import ScalpIntent, Target, TradeIntent


class CommandParseError(ValueError):
    pass


def parse_trade_command(text: str) -> TradeIntent:
    tokens = text.strip().split()
    if not tokens:
        raise CommandParseError("command must start with /trade")
    if tokens[0] != "/trade":
        tokens = ["/trade", *tokens]

    values: dict[str, str] = {}
    targets: list[Target] = []
    bare_tokens: list[str] = []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if "=" not in token:
            lower_token = token.lower()
            if lower_token in {"entry", "sl", "stop"} and index + 1 < len(tokens):
                values["entry" if lower_token == "entry" else "stop"] = tokens[index + 1]
                index += 2
                continue
            bare_tokens.append(token)
            index += 1
            continue
        key, raw_value = token.split("=", 1)
        key = key.lower()
        if key == "sl":
            key = "stop"
        if key.startswith("t") and key[1:].isdigit():
            targets.append(_parse_target(raw_value))
        else:
            values[key] = raw_value
        index += 1

    _apply_bare_tokens(values, bare_tokens)

    required = {"pair", "side", "amount_usdc", "entry"}
    missing = sorted(required - values.keys())
    if missing:
        raise CommandParseError(f"missing required fields: {', '.join(missing)}")

    entry_type, entry_price = _parse_entry(values["entry"])

    return TradeIntent(
        pair=_normalize_pair(values["pair"]),
        side=values["side"],
        amount_usdc=float(values["amount_usdc"]),
        entry_type=entry_type,
        entry_price=entry_price,
        targets=targets,
        stop_price=float(values["stop"]) if values.get("stop") else None,
        leverage=int(values["leverage"]) if values.get("leverage") else 1,
    )


def parse_scalp_start_command(text: str) -> ScalpIntent:
    tokens = text.strip().split()
    if not tokens or tokens[0].split("@", 1)[0].lower() != "/scalp_start":
        raise CommandParseError("command must start with /scalp_start")

    values: dict[str, str] = {}
    for token in tokens[1:]:
        if "=" not in token:
            raise CommandParseError(f"invalid token: {token}")
        key, raw_value = token.split("=", 1)
        key = key.lower()
        if key in {"amount", "amount_usdc"}:
            values["amount_usdc"] = _strip_currency(raw_value)
        elif key in {"side", "direction"}:
            values["side_mode"] = raw_value
        elif key in {"min_pnl", "min_net_pnl"}:
            values["min_net_pnl"] = _strip_currency(raw_value)
        elif key in {"duration", "max_hold"}:
            values[f"{key}_seconds"] = str(_parse_duration_seconds(raw_value))
        elif key == "pair":
            values["pair"] = _normalize_pair(raw_value)
        else:
            values[key] = raw_value

    missing = sorted({"pair", "amount_usdc"} - values.keys())
    if missing:
        raise CommandParseError(f"missing required fields: {', '.join(missing)}")

    return ScalpIntent(
        pair=values["pair"],
        side_mode=values.get("side_mode", "both"),
        amount_usdc=float(values["amount_usdc"]),
        leverage=int(values.get("leverage", "1")),
        duration_seconds=int(values.get("duration_seconds", "3600")),
        max_hold_seconds=int(values.get("max_hold_seconds", "300")),
        max_losses=int(values.get("max_losses", "3")),
        min_net_pnl=float(values.get("min_net_pnl", "10.0")),
        mode=values.get("mode", "paper"),
    )


def _parse_duration_seconds(raw_value: str) -> int:
    match = re.fullmatch(r"(\d+)(s|m|h)?", raw_value.lower())
    if not match:
        raise CommandParseError(f"invalid duration: {raw_value}")
    amount = int(match.group(1))
    unit = match.group(2) or "s"
    multiplier = {"s": 1, "m": 60, "h": 3600}[unit]
    return amount * multiplier


def _strip_currency(raw_value: str) -> str:
    return re.sub(r"(?:usdc|usd)$", "", raw_value.lower())


def _parse_entry(raw_value: str) -> tuple[str, float]:
    parts = raw_value.split(":", 1)
    if len(parts) == 1:
        return "limit", float(parts[0])
    if len(parts) != 2:
        raise CommandParseError("entry must use format limit:<price> or <price>")
    return parts[0], float(parts[1])


def _parse_target(raw_value: str) -> Target:
    parts = raw_value.rstrip("%").split(":", 1)
    if len(parts) != 2:
        raise CommandParseError("targets must use format <price>:<percent>%")
    return Target(price=float(parts[0]), percent=float(parts[1]))


def _apply_bare_tokens(values: dict[str, str], tokens: list[str]) -> None:
    for token in tokens:
        lower_token = token.lower()
        if lower_token in {"long", "buy"}:
            values.setdefault("side", "buy")
        elif lower_token in {"short", "sell"}:
            values.setdefault("side", "sell")
        elif re.fullmatch(r"\d+(?:\.\d+)?x", lower_token):
            values.setdefault("leverage", lower_token.removesuffix("x"))
        elif re.fullmatch(r"\d+(?:\.\d+)?(?:usdc|usd)", lower_token):
            values.setdefault("amount_usdc", re.sub(r"(?:usdc|usd)$", "", lower_token))
        elif "pair" not in values and re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*", token):
            values["pair"] = token
        else:
            raise CommandParseError(f"invalid token: {token}")


def _normalize_pair(value: str) -> str:
    pair = value.upper()
    if pair.startswith("PF_"):
        return pair
    if pair.endswith("USDC"):
        return f"PF_{pair[:-4]}USD"
    if pair.endswith("USD"):
        return f"PF_{pair}"
    return f"PF_{pair}USD"
