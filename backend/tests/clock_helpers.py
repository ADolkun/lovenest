from datetime import datetime, tzinfo


def instant_in[D: datetime](cls: type[D], instant: datetime, tz: tzinfo | None) -> D:
    # `datetime.now` is typed to return Self, so a frozen-clock subclass must build an instance of itself.
    moment = instant.astimezone(tz)
    return cls.combine(moment.date(), moment.timetz())
