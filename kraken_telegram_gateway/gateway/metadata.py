from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from kraken_telegram_gateway.gateway.kraken import (
    InstrumentMetadata,
    KrakenOrderPayloadError,
    format_decimal,
    parse_instrument_metadata,
)


@dataclass(frozen=True)
class MetadataValidationResult:
    instruments: list[InstrumentMetadata]


def validate_metadata_cache(
    path: str | Path,
    required_symbols: Sequence[str] = (),
) -> MetadataValidationResult:
    data = load_metadata_json(path)
    payloads = collect_instrument_payloads(data)
    if not payloads:
        raise KrakenOrderPayloadError("instrument metadata cache contains no instruments")

    instruments: list[InstrumentMetadata] = []
    errors: list[str] = []
    for symbol, raw in sorted(payloads.items()):
        try:
            instrument = parse_instrument_metadata(raw, symbol)
            validate_instrument_values(instrument)
            instruments.append(instrument)
        except KrakenOrderPayloadError as exc:
            errors.append(f"{symbol}: {exc}")

    present_symbols = {instrument.symbol.upper() for instrument in instruments}
    for symbol in required_symbols:
        normalized = symbol.upper()
        if normalized not in present_symbols:
            errors.append(f"{normalized}: required instrument is missing")

    if errors:
        raise KrakenOrderPayloadError("; ".join(errors))
    return MetadataValidationResult(instruments=instruments)


def load_metadata_json(path: str | Path) -> object:
    metadata_path = Path(path)
    try:
        return json.loads(metadata_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise KrakenOrderPayloadError(f"instrument metadata file cannot be read: {metadata_path}") from exc
    except json.JSONDecodeError as exc:
        raise KrakenOrderPayloadError(f"instrument metadata file is not valid JSON: {metadata_path}") from exc


def collect_instrument_payloads(data: object) -> dict[str, Mapping[str, object]]:
    if not isinstance(data, Mapping):
        raise KrakenOrderPayloadError("instrument metadata cache must be a JSON object")

    instruments = data.get("instruments", data)
    payloads: dict[str, Mapping[str, object]] = {}
    if isinstance(instruments, Mapping):
        for key, value in instruments.items():
            if not isinstance(value, Mapping):
                raise KrakenOrderPayloadError(f"instrument metadata for {key} must be a JSON object")
            symbol = str(value.get("symbol") or key).upper()
            payloads[symbol] = value
        return payloads

    if isinstance(instruments, list):
        for index, value in enumerate(instruments):
            if not isinstance(value, Mapping):
                raise KrakenOrderPayloadError(f"instrument metadata at index {index} must be a JSON object")
            symbol = value.get("symbol")
            if not symbol:
                raise KrakenOrderPayloadError(f"instrument metadata at index {index} is missing symbol")
            payloads[str(symbol).upper()] = value
        return payloads

    raise KrakenOrderPayloadError("instrument metadata 'instruments' must be an object or list")


def validate_instrument_values(instrument: InstrumentMetadata) -> None:
    if instrument.contract_value_usdc <= 0:
        raise KrakenOrderPayloadError("contract_value_usdc must be positive")
    if instrument.size_step <= 0:
        raise KrakenOrderPayloadError("size_step must be positive")
    if instrument.min_size <= 0:
        raise KrakenOrderPayloadError("min_size must be positive")


def format_validation_result(result: MetadataValidationResult, path: str | Path) -> str:
    lines = [f"OK: {len(result.instruments)} instrument(s) valid in {path}"]
    for instrument in result.instruments:
        lines.append(
            "- "
            f"{instrument.symbol} "
            f"contract_value_usdc={format_decimal(instrument.contract_value_usdc)} "
            f"size_step={format_decimal(instrument.size_step)} "
            f"min_size={format_decimal(instrument.min_size)}"
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate a local Kraken Futures instrument metadata cache.")
    parser.add_argument("path", help="Path to the local metadata JSON file.")
    parser.add_argument(
        "--require",
        action="append",
        default=[],
        metavar="SYMBOL",
        help="Instrument symbol that must be present. Can be passed more than once.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = validate_metadata_cache(args.path, args.require)
    except KrakenOrderPayloadError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(format_validation_result(result, args.path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
