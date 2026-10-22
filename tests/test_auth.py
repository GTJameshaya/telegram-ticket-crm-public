import json
import time
from urllib.parse import urlencode

import pytest

from app.auth import InitDataError, sign_init_data, verify_init_data

TOKEN = "123456:TEST-TOKEN"


def make_init_data(user_id=1001, token=TOKEN, auth_date=None, **extra) -> str:
    fields = {
        "auth_date": str(int(auth_date if auth_date is not None else time.time())),
        "query_id": "AAH",
        "user": json.dumps({"id": user_id, "first_name": "Анна", "username": "anna"}, ensure_ascii=False),
        **extra,
    }
    fields["hash"] = sign_init_data(fields, token)
    return urlencode(fields)


def test_valid_init_data():
    user = verify_init_data(make_init_data(), TOKEN)
    assert user["id"] == 1001 and user["first_name"] == "Анна"


def test_tampered_init_data_rejected():
    data = make_init_data().replace("1001", "1002")
    with pytest.raises(InitDataError):
        verify_init_data(data, TOKEN)


def test_wrong_token_rejected():
    with pytest.raises(InitDataError):
        verify_init_data(make_init_data(token="999:OTHER"), TOKEN)


def test_expired_init_data_rejected():
    old = make_init_data(auth_date=time.time() - 2 * 86400)
    with pytest.raises(InitDataError):
        verify_init_data(old, TOKEN, max_age=86400)


def test_missing_hash_or_empty():
    with pytest.raises(InitDataError):
        verify_init_data("", TOKEN)
    with pytest.raises(InitDataError):
        verify_init_data("auth_date=1&user=%7B%7D", TOKEN)
