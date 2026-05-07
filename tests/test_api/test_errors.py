from privaite.utils.errors import openai_error, provider_error_response


def test_openai_error_format():
    resp = openai_error("bad request", "invalid_request_error", 400, "invalid")
    assert resp.status_code == 400
    assert resp.body is not None


def test_provider_auth_error():
    exc = type("AuthenticationError", (Exception,), {})("auth failed")
    resp = provider_error_response(exc)
    assert resp.status_code == 401


def test_provider_rate_limit():
    exc = type("RateLimitError", (Exception,), {})("too many")
    resp = provider_error_response(exc)
    assert resp.status_code == 429


def test_provider_timeout():
    exc = type("TimeoutError", (Exception,), {})("timed out")
    resp = provider_error_response(exc)
    assert resp.status_code == 504


def test_provider_not_found():
    exc = type("NotFoundError", (Exception,), {})("not found")
    resp = provider_error_response(exc)
    assert resp.status_code == 404


def test_provider_unavailable():
    exc = type("ServiceUnavailableError", (Exception,), {})("down")
    resp = provider_error_response(exc)
    assert resp.status_code == 503


def test_provider_generic_error():
    exc = Exception("something broke")
    resp = provider_error_response(exc)
    assert resp.status_code == 502
