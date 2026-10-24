"""Temporary bridge between the search modules and the old SQLite database.

The search modules now talk to a Store (`db.is_sent`, `send(db, ..., source)`),
but main.py still calls them with `conn, cursor`. This adapter keeps that call
working. It goes away when main.py passes a Store.
"""
from collections.abc import Callable

from store import Source


class SqliteSentLinks:
    """Duck type of Store.is_sent over the old vagas_enviadas table."""

    def __init__(self, cursor) -> None:
        self._cursor = cursor

    def is_sent(self, link: str) -> bool:
        self._cursor.execute("SELECT 1 FROM vagas_enviadas WHERE link = ?", (link,))
        return self._cursor.fetchone() is not None


def bridge(conn, cursor, send: Callable) -> tuple[SqliteSentLinks, Callable[..., None]]:
    """Return (db, send) in the new contract, backed by conn/cursor and the old send."""

    def new_send(db, link: str, title: str, company: str, date: str, message: str,
                 source: Source) -> None:
        send(conn, cursor, link, title, company, date, message, source.label)

    return SqliteSentLinks(cursor), new_send
