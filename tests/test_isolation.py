import errno
import os
import socket

import pytest


@pytest.mark.skipif(
    os.environ.get("EXPECT_NO_NETWORK") != "1",
    reason="проверяется только в окружении make test",
)
def test_no_route_outside() -> None:
    # `make test` обещает работу без сети. Адрес документационный (RFC 5737): даже при
    # сломанной изоляции соединение уйдёт в никуда, а не на чужой сервер.
    with pytest.raises(OSError) as caught:
        socket.create_connection(("192.0.2.1", 443), timeout=2)
    # Нужен именно отказ маршрутизации: истечение времени означало бы, что маршрут наружу есть.
    assert caught.value.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH)
