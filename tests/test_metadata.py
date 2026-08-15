from kraken_telegram_gateway.gateway.kraken import KrakenOrderPayloadError
from kraken_telegram_gateway.gateway.metadata import main, validate_metadata_cache


def test_metadata_cache_validator_accepts_required_symbol(tmp_path):
    metadata_file = tmp_path / "instruments.json"
    metadata_file.write_text(
        """
        {
          "instruments": {
            "PF_XBTUSD": {
              "contract_value_usdc": "5",
              "size_step": "0.5",
              "min_size": "1"
            }
          }
        }
        """,
        encoding="utf-8",
    )

    result = validate_metadata_cache(metadata_file, required_symbols=["pf_xbtusd"])

    assert len(result.instruments) == 1
    assert result.instruments[0].symbol == "PF_XBTUSD"


def test_metadata_cache_validator_rejects_missing_required_symbol(tmp_path):
    metadata_file = tmp_path / "instruments.json"
    metadata_file.write_text(
        """
        {
          "instruments": []
        }
        """,
        encoding="utf-8",
    )

    try:
        validate_metadata_cache(metadata_file, required_symbols=["PF_XBTUSD"])
    except KrakenOrderPayloadError as exc:
        assert "contains no instruments" in str(exc)
    else:
        raise AssertionError("expected metadata cache validation to fail")


def test_metadata_cache_validator_rejects_non_positive_values(tmp_path):
    metadata_file = tmp_path / "instruments.json"
    metadata_file.write_text(
        """
        {
          "instruments": [
            {
              "symbol": "PF_XBTUSD",
              "contract_value_usdc": "0",
              "size_step": "0.5",
              "min_size": "1"
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    try:
        validate_metadata_cache(metadata_file)
    except KrakenOrderPayloadError as exc:
        assert "contract_value_usdc must be positive" in str(exc)
    else:
        raise AssertionError("expected metadata cache validation to fail")


def test_metadata_cache_validator_cli_reports_valid_instruments(tmp_path, capsys):
    metadata_file = tmp_path / "instruments.json"
    metadata_file.write_text(
        """
        {
          "instruments": {
            "PF_XBTUSD": {
              "contract_value_usdc": "5",
              "size_step": "0.5",
              "min_size": "1"
            }
          }
        }
        """,
        encoding="utf-8",
    )

    exit_code = main([str(metadata_file), "--require", "PF_XBTUSD"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "OK: 1 instrument(s) valid" in captured.out
    assert "PF_XBTUSD contract_value_usdc=5 size_step=0.5 min_size=1" in captured.out
