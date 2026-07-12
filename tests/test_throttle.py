import threading

from email_export_import.throttle import RateLimiter


class FakeClock:
    """Virtual clock: sleep() just advances time, so tests are instant."""

    def __init__(self):
        self.t = 0.0
        self.slept = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s
        self.slept += s


def test_unlimited_never_waits():
    c = FakeClock()
    r = RateLimiter(0, clock=c.now, sleep=c.sleep)
    r.acquire(10_000_000)
    assert c.slept == 0


def test_rate_is_enforced_across_calls():
    c = FakeClock()
    r = RateLimiter(1000, clock=c.now, sleep=c.sleep)  # 1000 B/s

    r.acquire(1000)  # first send goes immediately — no startup stall
    assert c.slept == 0

    r.acquire(1000)  # must wait ~1s for the previous 1000 bytes
    assert 0.9 <= c.t <= 1.15


def test_a_message_larger_than_one_second_of_budget_still_passes():
    # A capped token bucket would deadlock here; the pacer must not.
    c = FakeClock()
    r = RateLimiter(1000, clock=c.now, sleep=c.sleep)
    r.acquire(5000)          # first slot is immediate
    r.acquire(1)             # ...but the next one waits out the 5s of bytes
    assert 4.9 <= c.t <= 5.2


def test_idle_time_does_not_bank_unlimited_credit():
    c = FakeClock()
    r = RateLimiter(1000, clock=c.now, sleep=c.sleep)
    r.acquire(1000)
    c.t += 60  # long idle
    r.acquire(1000)  # allowed at once (slot is in the past), but no credit hoard
    before = c.t
    r.acquire(1000)  # the very next one still pays full price
    assert 0.9 <= c.t - before <= 1.15


def test_cancel_breaks_out_of_the_wait():
    c = FakeClock()
    cancel = threading.Event()
    cancel.set()
    r = RateLimiter(1, clock=c.now, sleep=c.sleep)  # 1 B/s → would wait ages
    r.acquire(10_000)  # reserves a slot far in the future
    r.acquire(10_000, cancel=cancel)
    # returned promptly instead of sleeping out a 10000-second debt
    assert c.slept < 1
