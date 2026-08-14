from __future__ import annotations

from kraken_telegram_gateway.gateway.schemas import Target, TradeIntent


class CommandParseError(ValueError):
    pass


def parse_trade_command(text: str) -> TradeIntent:
    tokens = text.strip().split()
    if not tokens or tokens[0] != "/trade":
        raise CommandParseError("command must start with /trade")

    values: dict[str, str] = {}
    targets: list[Target] = []
    for token in tokens[1:]:
        if "=" not in token:
            raise CommandParseError(f"invalid token: {token}")
        key, raw_value = token.split("=", 1)
        key = key.lower()
        if key.startswith("t") and key[1:].isdigit():
            targets.append(_parse_target(raw_value))
        else:
            values[key] = raw_value

    required = {"pair", "side", "amount_usdc", "entry"}
    missing = sorted(required - values.keys())
    if missing:
        raise CommandParseError(f"missing required fields: {', '.join(missing)}")

    entry_type, entry_price = _parse_entry(values["entry"])

    return TradeIntent(
        pair=values["pair"],
        side=values["side"],
        amount_usdc=float(values["amount_usdc"]),
        entry_type=entry_type,
        entry_price=entry_price,
        targets=targets,
        stop_price=float(values["stop"]) if values.get("stop") else None,
        leverage=int(values["leverage"]) if values.get("leverage") else 1,
    )


def _parse_entry(raw_value: str) -> tuple[str, float]:
    parts = raw_value.split(":", 1)
    if len(parts) != 2:
        raise CommandParseError("entry must use format limit:<price>")
    return parts[0], float(parts[1])


def _parse_target(raw_value: str) -> Target:
    parts = raw_value.rstrip("%").split(":", 1)
    if len(parts) != 2:
        raise CommandParseError("targets must use format <price>:<percent>%")
    return Target(price=float(parts[0]), percent=float(parts[1]))
