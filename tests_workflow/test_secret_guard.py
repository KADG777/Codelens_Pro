from scripts.check_secrets import contains_secret


def test_secret_guard_matches_actual_key_and_provider_format_without_echoing():
    sample = b"sk-" + b"a1b2c3d4" * 4
    assert contains_secret(b'{"api_key": "' + sample + b'"}')
    assert contains_secret(b"prefix private-token suffix", b"private-token")
    assert not contains_secret(b'DEEPSEEK_API_KEY=\napi_key=os.getenv("DEEPSEEK_API_KEY")')
