from dataclasses import dataclass, fields
from typing import Any, NewType, get_type_hints, override
from zoneinfo import available_timezones

from flask import has_request_context, request

Undefined = NewType("Undefined", object)

THEMES: dict[str, str] = {
    "default": "Default",
    "light": "Light",
    "black": "Black",
    "catppuccin_mocha": "Catppuccin mocha",
    "catppuccin_latte": "Catppuccin latte",
}
COMMENT_SORTS: tuple[str, ...] = ("filtered", "newest", "all")
TIMEZONES: frozenset[str] = frozenset(available_timezones())
ZONE_NAMES: dict[str, str] = {i.lower(): i for i in TIMEZONES}  # typed names match whatever their case
LEGACY_OFFSETS: range = range(-12, 15)  # the setting used to be a UTC offset in hours
BOOLEANS: dict[str, bool] = {"on": True, "true": True, "off": False, "false": False, "": False}


def parse_setting(name: str, raw: str | None) -> object:
    """Turn a raw cookie/form value into a valid setting, or Undefined if it isn't one."""
    if raw is None:
        return Undefined
    match name:
        case "theme":
            return raw if raw in THEMES else Undefined
        case "comments_sort":
            return raw if raw in COMMENT_SORTS else Undefined
        case "proxy" | "expand":
            return BOOLEANS.get(raw, Undefined)
        case "timezone":
            if len(raw) <= 3 and raw.removeprefix("-").isdigit():
                if int(raw) not in LEGACY_OFFSETS:
                    return Undefined
                # Etc/GMT zones have the sign reversed: UTC+1 is Etc/GMT-1.
                return f"Etc/GMT{-int(raw):+d}" if int(raw) else "UTC"
            return ZONE_NAMES.get(raw.strip().lower(), Undefined)
        case _:
            return Undefined


def serialize_setting(value: object) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


class ConfigBase:
    @override
    def __setattr__(self, name: str, value: Any, /) -> None:
        type_hints: dict[str, Any] = get_type_hints(self.__class__)
        if not (name in type_hints and isinstance(value, type_hints[name])):
            raise TypeError
        super().__setattr__(name, value)


@dataclass
class UserSettings(ConfigBase):
    theme: str = "default"
    comments_sort: str = "filtered"
    proxy: bool = True
    expand: bool = False
    timezone: str | int = "UTC"  # an IANA time zone name; an old UTC offset in hours is converted

    def __post_init__(self) -> None:
        for i in fields(self):
            value: object = object.__getattribute__(self, i.name)
            parsed: object = parse_setting(i.name, serialize_setting(value))
            if parsed is Undefined:
                raise ValueError(f"Invalid default_user_settings.{i.name}: {value!r}")
            setattr(self, i.name, parsed)

    @override
    def __getattribute__(self, name: str, /) -> Any:
        if has_request_context():
            v: Any | Undefined = parse_setting(name, request.cookies.get(name))
            if v is not Undefined:
                return v
        return super().__getattribute__(name)


@dataclass
class PhiceConfig(ConfigBase):
    enable_rss: bool = True
    proxy: str | None = None
    trusted_proxies: int = 0
    show_tracebacks: bool = False

    def __post_init__(self) -> None:
        if self.trusted_proxies < 0:
            raise ValueError(f"Invalid phice.trusted_proxies: {self.trusted_proxies!r}")
