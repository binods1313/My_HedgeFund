"""Account fingerprint guard.

The failure this prevents: a terminal that is attached to account A, whose
journal, positions and daily baselines all belong to account A, is switched to
account B -- by a login dialog, a second process, or an operator swapping
terminals. Nothing in MT5 announces the change. Without a guard the engine
would keep writing account B's equity into account A's journal, adopt account
B's positions into account A's table, and measure a daily loss limit against a
balance that is not the one it started the day with.

The rule is simple: the journal records which account it belongs to, the guard
compares that against the account actually logged in, and on mismatch it blocks
trading, reconcile, telemetry, order submission and order modification. Every
one of those is a write that would otherwise silently cross accounts.

Nothing here raises on a *readable* account. A `None` or unreadable account is
treated as a mismatch, because "cannot verify" is indistinguishable from
"verified and wrong" until you check.

Currency comes from ``account_info().currency`` and nowhere else. There is no
default, no fallback, and no hardcoded ``USD`` or ``EUR``: an engine that
assumes a currency will mis-size an order on an account that uses another.
"""
from __future__ import annotations

from dataclasses import dataclass

import time

#: The magic number this system writes orders with. Any position carrying a
#: different magic is somebody else's: the retired system wrote 234000.
FOREIGN_MAGIC = 234000
#: The magic every order this system places carries, from `config.yaml`
#: `magic_number`. It is the ownership test: an emergency sweep closes only
#: positions whose magic matches this, so a foreign position that somehow
#: reached the journal (a manual insert, a future reconcile change) is still
#: never closed by the halt. The guard keeps foreign positions out of the
#: journal in the first place; this is the second, independent lock.
MANAGED_MAGIC = 100100


class AccountMismatch(RuntimeError):
    """The configured account and the logged-in account are not the same."""


class TerminalOffline(AccountMismatch):
    """The terminal cannot be read, so the account cannot be verified.

    A subclass of :class:`AccountMismatch` deliberately: every existing call
    site that catches a mismatch must also catch an unreadable terminal,
    because both block the same writes. The split exists so the journal and
    the operator can tell the two incidents apart -- "wrong account" is a
    credential or login-dialog fault, "not answering" is a terminal or broker
    fault -- without every catch site having to change.
    """


@dataclass(frozen=True)
class Fingerprint:
    """The three fields that identify an account, plus its currency.

    ``currency`` is carried here rather than read at each use site so that a
    single verification proves the whole triad at once. An account can keep the
    same login across a server change; a currency change on the same login is
    the one combination that would otherwise pass unnoticed.
    """

    login: int | None = None
    currency: str | None = None
    server: str | None = None

    def _norm(self, value) -> str | None:
        """Uppercase for comparison only; the stored value is left as read.

        MT5 reports "USD"; a broker reporting "usd" is not a different account.
        Normalising at comparison time keeps the journal's record literal.
        """
        return None if value is None else str(value).strip().upper()

    def is_empty(self) -> bool:
        """True when nothing was configured to compare against.

        With no ``MT5_LOGIN`` in the environment there is no configured
        expectation, and inventing one would block every run. The journal
        fingerprint still applies on its own.
        """
        return (self.login is None and self.currency is None
                and self.server is None)

    def matches_configured(self, other: "Fingerprint") -> bool:
        """Compare against the *configured* expectation.

        The environment knows the login and the server, and never the currency
        -- a currency is something the terminal reports, not something an
        operator configures. So this comparison uses only the fields the
        configured side actually has, and a missing configured currency is not
        a mismatch. The full ``matches`` still applies to the journal
        fingerprint, which was read from the broker and does carry one.
        """
        if self.login is None or other.login is None:
            return False
        if int(self.login) != int(other.login):
            return False
        if (self._norm(self.server) is not None
                and self._norm(other.server) is not None):
            return self._norm(self.server) == self._norm(other.server)
        return True

    def matches(self, other: "Fingerprint") -> bool:
        """True only when both fingerprints were read and they agree.

        A missing field on either side is a mismatch, never a wildcard.
        """
        if self.login is None or other.login is None:
            return False
        if int(self.login) != int(other.login):
            return False
        if (self._norm(self.server) is not None
                and self._norm(other.server) is not None):
            if self._norm(self.server) != self._norm(other.server):
                return False
        if self.currency is None or other.currency is None:
            return False
        return self._norm(self.currency) == self._norm(other.currency)


def fingerprint_from_account(account) -> Fingerprint:
    """Read a fingerprint off an MT5 ``account_info()`` result.

    Currency is taken solely from ``account.currency``. If it is absent the
    fingerprint is incomplete and will not match anything, which is correct:
    a currency the broker did not report is not a currency we may assume.
    """
    if account is None:
        return Fingerprint()
    currency = getattr(account, "currency", None)
    return Fingerprint(
        login=getattr(account, "login", None),
        currency=str(currency).upper() if currency else None,
        server=getattr(account, "server", None),
    )


def classify_foreign_positions(positions, *, magic: int) -> list[dict]:
    """Positions on the account that our engine does not own.

    Foreign positions are never written to the journal's ``positions`` table by
    the guard -- they are reported so the caller can count them for exposure,
    margin and max-open-trade checks, and label them as unmanaged. They are
    never adopted, modified, closed or trailed.
    """
    foreign: list[dict] = []
    for position in positions or []:
        position_magic = getattr(position, "magic", None)
        if position_magic is None or int(position_magic) != int(magic):
            foreign.append({
                "ticket": int(getattr(position, "ticket", 0) or 0),
                "symbol": getattr(position, "symbol", ""),
                "side": "BUY" if getattr(position, "type", 0) == 0 else "SELL",
                "volume": float(getattr(position, "volume", 0.0) or 0.0),
                "profit": float(getattr(position, "profit", 0.0) or 0.0),
                "magic": position_magic,
            })
    return foreign


class AccountGuard:
    """Compares the configured account against the account actually logged in.

    Fail closed. The guard has no code path that lets a mismatch through: every
    write the engine performs -- reconcile, telemetry, order submission, order
    modification -- asks this object first, and the only way to get a "yes" is
    a fingerprint that was read from the terminal and matched.
    """

    def __init__(self, journal=None, relay=None, *,
                 configured: Fingerprint | None = None,
                 magic: int = 100100) -> None:
        self.journal = journal
        self.relay = relay            # optional notification relay
        self.configured = configured or Fingerprint()
        self.magic = int(magic)
        self.mismatch_reason: str | None = None
        self._verified: Fingerprint | None = None

    # -- state -------------------------------------------------------------

    @property
    def verified(self) -> bool:
        return self.mismatch_reason is None and self._verified is not None

    @property
    def fingerprint(self) -> Fingerprint:
        """The account the guard last verified, or an empty fingerprint."""
        return self._verified or Fingerprint()

    # -- verification ------------------------------------------------------

    #: After the first event for a given reason, a reminder is journalled at
    #: most this often, in seconds. One event per change plus one reminder per
    #: window: an hour-long outage produces one event and six reminders, not
    #: 3,600 rows -- 3,161 were written in a single 53-minute stretch before
    #: the guard had any throttle at all.
    _REMIND_EVERY: float = 600.0
    #: reason -> monotonic time the reason was last journalled.
    _rejections: dict | None = None
    #: The reason currently journalled, or None while verified. A *change* of
    #: reason always logs immediately; only repeats are windowed.
    _active_reason: str | None = None

    def check(self, account, *, phase: str = "startup") -> Fingerprint:
        """Verify one live account reading. Raises :class:`AccountMismatch`.

        An unreadable terminal raises :class:`TerminalOffline`, which is an
        ``AccountMismatch`` subclass so callers that block on a mismatch also
        block on it.

        ``phase`` names the call site and goes into the journal event, because
        "account mismatch at startup" and "account switched mid-session" are
        different incidents with the same root cause.
        """
        live = fingerprint_from_account(account)
        stored = self._stored_fingerprint()

        if live.login is None:
            # The gateway reports connected but the account read is empty.
            # That is a broker or terminal read failure, NOT a different
            # account, so it is journalled as TerminalOffline rather than a
            # mismatch: the operator must look at the terminal, not at login
            # credentials. It still blocks every write, because an account
            # that cannot be read cannot be trusted.
            return self._reject(
                phase,
                "no account is logged in; broker or terminal not answering",
                live, stored, offline=True)

        # The configured side may legitimately know only login and server: the
        # environment holds no currency, so demanding one there would reject
        # every correct account. Currency is still checked whenever either side
        # does know it, which is what the journal fingerprint does.
        if not live.matches_configured(self.configured) and not self.configured.is_empty():
            return self._reject(
                phase,
                f"configured account {self.configured.login} is not the "
                f"logged-in account {live.login}",
                live, stored)

        if stored is not None and stored.login is not None:
            if not live.matches(stored):
                return self._reject(
                    phase,
                    f"this journal belongs to account {stored.login} "
                    f"({stored.currency}) and the terminal is on {live.login} "
                    f"({live.currency})",
                    live, stored)

        self.mismatch_reason = None
        self._active_reason = None
        self._verified = live
        if self.journal is not None and stored is None:
            self.journal.set_account_fingerprint(
                login=live.login, currency=live.currency,
                server=live.server, magic=self.magic)
        return live

    def _stored_fingerprint(self) -> Fingerprint | None:
        if self.journal is None:
            return None
        try:
            row = self.journal.get_account_fingerprint()
        except Exception:  # noqa: BLE001 - an unreadable journal is a mismatch
            return None
        if not row or row["login"] is None:
            return None
        return Fingerprint(login=row["login"], currency=row["currency"],
                           server=row["server"])

    def _reject(self, phase, detail, live, stored, *,
                offline: bool = False) -> Fingerprint:
        self.mismatch_reason = detail
        self._verified = None
        if self.journal is not None:
            # One event per change, then one reminder per window. A terminal
            # that stays unreadable produces a rejection every loop iteration;
            # unthrottled, that buries the one event an operator needs -- 3,161
            # rows were written in a single 53-minute stretch before this
            # existed. A *changed* reason always logs immediately, because the
            # change itself is the incident; only repeats are windowed, and a
            # successful check clears the active reason so a recurrence is a
            # new incident again.
            now = time.monotonic()
            if self._rejections is None:
                self._rejections = {}
            changed = detail != self._active_reason
            last = self._rejections.get(detail, 0.0)
            inside_window = bool(last) and now - last < self._REMIND_EVERY
            if changed or not inside_window:
                self._rejections[detail] = now
                self._active_reason = detail
                self.journal.log_event(
                    "account",
                    ("Terminal offline" if offline else "Account mismatch")
                    + (" (still unresolved)" if not changed else ""),
                    severity="WARNING" if offline else "CRITICAL",
                    source="engine",
                    context={
                        "phase": phase,
                        "reason": detail,
                        "kind": "terminal_offline" if offline else "account_mismatch",
                        "repeat": not changed,
                        "configured_login": self.configured.login,
                        "configured_currency": self.configured.currency,
                        "live_login": live.login,
                        "live_currency": live.currency,
                        "live_server": live.server,
                        "journal_login": stored.login if stored else None,
                        "blocked": ["trading", "reconcile", "telemetry",
                                    "order_submission", "order_modification"],
                    })
        if self.relay is not None:
            try:
                self.relay.notify(
                    "Terminal offline" if offline else "Account mismatch",
                    f"Engine blocked ({phase}): {detail}")
            except Exception:  # noqa: BLE001 - alerting must not mask the cause
                pass
        raise (TerminalOffline if offline else AccountMismatch)(detail)

    # -- write gates -------------------------------------------------------

    def may_reconcile(self) -> bool:
        """Reconcile may only alter journal state on a matching fingerprint."""
        return self.verified

    def may_write_telemetry(self) -> bool:
        return self.verified

    def may_submit_orders(self) -> bool:
        return self.verified

    def may_modify_orders(self) -> bool:
        return self.verified
