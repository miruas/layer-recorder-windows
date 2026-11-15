# SPDX-License-Identifier: GPL-3.0-only
"""English command matching and persisted Voice Control preferences."""

import json
import re
from dataclasses import asdict, dataclass

from .common import CONFIG, atomic_json


ACTIONS = ("start_recording", "stop_recording", "toggle_recording", "save_replay")
CONFIG_PATH = CONFIG / "voice-control.json"
SMALL = dict(zip(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split(),
    range(20),
))
TENS = dict(zip("twenty thirty forty fifty sixty seventy eighty ninety".split(), range(20, 100, 10)))
UNITS = {"second": 1, "seconds": 1, "minute": 60, "minutes": 60, "hour": 3600, "hours": 3600}
PLACEHOLDER = "{duration}"


@dataclass(frozen=True)
class VoiceCommand:
    template: str
    action: str


DEFAULT_COMMANDS = (
    VoiceCommand("start recording", "start_recording"),
    VoiceCommand("stop recording", "stop_recording"),
    VoiceCommand("toggle recording", "toggle_recording"),
    VoiceCommand("save the last {duration}", "save_replay"),
    VoiceCommand("last {duration}", "save_replay"),
)


@dataclass(frozen=True)
class VoiceConfig:
    enabled: bool = False
    microphone: str = ""
    wake_phrase: str = "Layer"
    commands: tuple[VoiceCommand, ...] = DEFAULT_COMMANDS


@dataclass(frozen=True)
class VoiceMatch:
    action: str
    seconds: int | None = None


def words(text):
    # Keep unsupported number punctuation so a decimal or negative cannot become a positive integer.
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text.lower())
    return tuple(re.findall(r"[a-z]+|[+-]?\d+(?:[.,:/]\d+)*", text))


def number(tokens):
    if len(tokens) == 1:
        token = tokens[0]
        if token.isascii() and token.isdecimal() and len(token) <= 9:
            return int(token)
        return 1 if token in ("a", "an") else SMALL.get(token, TENS.get(token))
    if len(tokens) == 2 and tokens[0] in TENS and 0 < SMALL.get(tokens[1], 0) < 10:
        return TENS[tokens[0]] + SMALL[tokens[1]]
    if len(tokens) >= 2 and tokens[1] == "hundred" and 0 < SMALL.get(tokens[0], 0) < 10:
        tail = tokens[2:]
        if not tail:
            return SMALL[tokens[0]] * 100
        remainder = number(tail[1:] if tail[0] == "and" else tail)
        if remainder is not None and 0 < remainder < 100:
            return SMALL[tokens[0]] * 100 + remainder
    if tokens.count("thousand") == 1:
        split = tokens.index("thousand")
        thousands, tail = number(tokens[:split]), tokens[split + 1:]
        remainder = number(tail[1:] if tail[:1] == ("and",) else tail) if tail else 0
        if thousands is not None and 0 < thousands < 1000 and remainder is not None and 0 <= remainder < 1000:
            return thousands * 1000 + remainder
    return None


def duration_seconds(tokens):
    total, start, previous_unit = 0, 0, float("inf")
    for index, token in enumerate(tokens):
        if token not in UNITS:
            continue
        unit = UNITS[token]
        part = tokens[start:index]
        if start and part[:1] == ("and",):
            part = part[1:]
        count = number(part)
        if count is None or unit >= previous_unit:
            return None
        total += unit * count
        previous_unit, start = unit, index + 1
    return total if start == len(tokens) and total > 0 else None


def template_parts(template):
    head, marker, tail = template.lower().partition(PLACEHOLDER)
    return words(head), bool(marker), words(tail)


def validate_config(config):
    if type(config.enabled) is not bool or not isinstance(config.microphone, str):
        raise ValueError("Invalid Voice Control or microphone preference.")
    if not re.fullmatch(r"[a-zA-Z]+(?:[ -][a-zA-Z]+)*", config.wake_phrase.strip()):
        raise ValueError("Enter an English wake phrase using letters, spaces or hyphens.")
    validate_commands(config.commands)


def validate_commands(commands):
    seen = set()
    for command in commands:
        template = command.template.lower()
        parts = template_parts(template)
        head, dynamic, tail = parts
        remaining = template.replace(PLACEHOLDER, "", 1)
        if command.action not in ACTIONS or not re.fullmatch(r"[a-z0-9 ,.!?'\-]*", remaining):
            raise ValueError("Use English words and an optional {duration} in a command template.")
        if dynamic != (command.action == "save_replay"):
            raise ValueError("Replay commands require one {duration}. Recording commands use fixed phrases.")
        if not (head or tail or dynamic):
            raise ValueError("Enter a command phrase.")
        if parts in seen:
            raise ValueError("Every command template must be unique.")
        seen.add(parts)


class CommandMatcher:
    def __init__(self, config):
        self.wake = words(config.wake_phrase)
        self.commands = [(template_parts(command.template), command.action) for command in config.commands]

    def match(self, transcript):
        phrase = words(transcript)
        if phrase[:len(self.wake)] != self.wake:
            return None
        phrase = phrase[len(self.wake):]
        matches = []
        for (head, dynamic, tail), action in self.commands:
            if not dynamic:
                if phrase == head:
                    matches.append(VoiceMatch(action))
                continue
            end = len(phrase) - len(tail)
            if phrase[:len(head)] == head and phrase[end:] == tail and end > len(head):
                seconds = duration_seconds(phrase[len(head):end])
                if seconds is not None:
                    matches.append(VoiceMatch(action, seconds))
        if matches and all(match == matches[0] for match in matches):
            return matches[0]
        return None


def load_config(path=CONFIG_PATH):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return VoiceConfig()
    try:
        config = VoiceConfig(**{**data, "commands": tuple(VoiceCommand(**row) for row in data["commands"])})
        validate_config(config)
        return config
    except (TypeError, AttributeError, KeyError) as error:
        raise ValueError("Invalid Voice Control settings.") from error


def save_config(config, path=CONFIG_PATH):
    validate_config(config)
    atomic_json(path, asdict(config))
