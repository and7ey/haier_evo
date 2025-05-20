
from ratelimit.decorators import RateLimitDecorator


class ResettableLimits(RateLimitDecorator):

    def __init__(self, **kwargs) -> None:
        self._multiplier = kwargs.pop("multiplier", 1)
        self._max = kwargs.pop("max", kwargs.get("period"))
        self._exp_base = kwargs.pop("exp_base", 2)
        super().__init__(**kwargs)
        self._init_period = self.period
        self._min = self.period
        self._counter = 0
        self._RateLimitDecorator__period_remaining = self.__period_remaining

    def set_period(self, period: int | float = None):
        self.period = period or self._init_period

    def add_period(self, period: int | float = None) -> float | int:
        period = self.period + period
        self.period = min(period, self._max)
        return self.period

    def expand_period(self) -> float | int:
        try:
            exp = self._exp_base ** (self._counter - 1)
            result = self._multiplier * exp
        except OverflowError:
            result = self._max
        else:
            result = max(max(0, self._min), min(result, self._max))
        self.period = result
        return self.period

    def reset(self) -> None:
        self.period = self._init_period
        self.num_calls = 0
        self.last_reset = self.clock()
        self._counter = 0

    def __period_remaining(self) -> float:
        self._counter += 1
        elapsed = self.clock() - self.last_reset
        return self.period - elapsed
