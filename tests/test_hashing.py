from rolling_budget_api.services.hashing import checksum_chain, sha256_hex


def test_canonical_hash_ignores_mapping_key_order() -> None:
    assert sha256_hex({"b": 2, "a": 1}) == sha256_hex({"a": 1, "b": 2})


def test_batch_checksum_chain_is_order_sensitive() -> None:
    first = "1" * 64
    second = "2" * 64

    assert checksum_chain([first, second]) != checksum_chain([second, first])
    assert checksum_chain([first, second]) == checksum_chain([first, second])
