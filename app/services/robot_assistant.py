"""Text and voice assistant that turns operator requests into robot missions and schedules.

The language model only reads each message into a fixed JSON shape (intent plus the details
it heard). Everything that has to hold every time lives in plain code here: which questions
are asked and in what order, waypoint matching, the am/pm and weekday guards, how long spoken
lists may be, and the reply text. The robot never moves without a resolved waypoint and an
explicit capture type, and "stop" never waits on the model.

Conversation state is a small JSON object the browser sends back with every message, so the
server keeps nothing between turns.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone as dt_timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.robot_missions import (
    cancel_robot_mission,
    create_robot_command,
    create_robot_mission,
    create_robot_mission_schedule,
    delete_robot_mission_schedule,
    run_robot_mission_schedule_now,
    stop_robot_mission_return,
    update_robot_mission_schedule,
)
from app.config import get_settings
from app.models import Project, Room, RobotCapturePoint, RobotMission, RobotMissionSchedule, User
from app.schemas import (
    RobotCommandCreateRequest,
    RobotMissionCreateRequest,
    RobotMissionScheduleCreateRequest,
    RobotMissionScheduleUpdateRequest,
)

logger = logging.getLogger(__name__)

settings = get_settings()

# Matches the frontend's ACTIVE_MISSION_STATUSES: anything here means the robot is busy.
ACTIVE_MISSION_STATUSES = (
    "queued",
    "dispatched",
    "running",
    "cancel_requested",
    "cancelling",
    "returning_to_start",
    "stop_requested",
)
SPOKEN_LIST_LIMIT = 5
TEXT_LIST_LIMIT = 10
MAX_SUGGESTIONS = 3

CAPABILITIES = "I can send the robot to a waypoint, create a schedule, or stop the robot."
CAPTURE_OPTIONS = "I can capture a 360° image, a point cloud, or both."
CAPABILITIES_FULL = (
    "I can send the robot to a waypoint to capture a 360° image, a point cloud, or both, for example "
    "\"go to the kitchen and take a photo\". I can also set up recurring capture schedules, list the waypoints, "
    "connect or disconnect the robot, stop it, and open the page's tabs. And I'm happy to chat or answer "
    "construction questions."
)
DEFAULT_FIELDS_NOTE = (
    "Time zone, busy handling, auto-connect, allowed lateness and failure handling "
    "are on their default values and can be changed."
)
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# ---------------------------------------------------------------------------
# Language model: one message in, one JSON object out.
# ---------------------------------------------------------------------------

PARSE_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": ["go_to", "create_schedule", "stop", "connect", "disconnect", "list", "open_tab",
                     "delete_schedule", "pause_schedule", "resume_schedule", "run_schedule", "answer", "unknown"],
        },
        "waypoints": {"type": "array", "items": {"type": "string"}},
        "time": {"type": ["string", "null"]},
        "weekdays": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 6}},
        "name": {"type": ["string", "null"]},
        "yes_no": {"type": ["string", "null"], "enum": ["yes", "no", None]},
        "tab": {"type": ["string", "null"], "enum": ["route", "schedules", "progress", "live", "history", None]},
        "schedule_name": {"type": ["string", "null"]},
    },
    "required": ["intent", "waypoints", "time", "weekdays", "name", "yes_no"],
}

PARSE_PROMPT = """You convert one message from a construction-site robot operator into JSON. You never reply in prose.

Intents:
- go_to: send the robot to one or more waypoints now, including "take a photo of the kitchen" or "scan room1".
- create_schedule: set up a recurring capture schedule.
- stop: stop or cancel the robot's current capture or movement.
- connect: connect the robot / bring it online.
- disconnect: disconnect the robot, shut it down or take it offline (this is not stop).
- list: the user asks which waypoints or rooms exist.
- open_tab: show a page tab (route, schedules, progress, live map, history), even if misspelled; put it in "tab".
- delete_schedule / pause_schedule / resume_schedule / run_schedule: manage an existing schedule; its name (if said) goes in "schedule_name".
- answer: the user is answering the assistant's last question (given below).
- unknown: anything else.

Fields (leave empty list / null when not said; never invent values):
- waypoints: place names exactly as the user said them, in the order said.
- time: 24-hour "HH:MM".
- weekdays: Monday=0 ... Sunday=6. "every day" = 0-6, "weekdays" = 0-4, "weekends" = 5-6.
- name: the schedule name if given.
- yes_no: "yes" or "no" when the user answers a yes/no question.

A question about something ("what is a point cloud?", "what are the choices?") is never a waypoint name.
Only put words in "waypoints" that name a place."""

Parser = Callable[[str, str | None], dict[str, Any] | None]


def parse_with_llm(message: str, last_question: str | None) -> dict[str, Any] | None:
    """Return the model's reading of one message, or None when the model is unreachable."""
    user = f"Assistant's last question: {last_question or 'none'}\nUser: {message}"
    try:
        response = httpx.post(
            settings.assistant_api_url,
            json={
                "model": settings.assistant_model,
                "messages": [
                    {"role": "system", "content": PARSE_PROMPT},
                    {"role": "user", "content": user},
                ],
                "format": PARSE_SCHEMA,
                "stream": False,
                # Messages are tiny; a small window loads faster on the shared GPU.
                "options": {"temperature": 0, "num_ctx": 4096},
            },
            timeout=60,
        )
        response.raise_for_status()
        content = response.json()["message"]["content"]
        parsed = json.loads(content)
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("Assistant parse failed: %s", exc)
        return None
    return parsed if isinstance(parsed, dict) else None


CHAT_PROMPT = """You are SiteScope's assistant, a friendly, helpful general assistant for people on a construction project. You can talk about anything: say hello, make small talk, answer general questions, and help with construction topics such as materials, methods, safety, scheduling or terminology. Talk the way a normal, warm assistant would.

The app around you can also control a site robot. Mention it only when it is relevant to what the person says, never in every reply. The robot can go to saved waypoints and capture a 360-degree image, a point cloud, or both; it can run on recurring schedules; it can list the waypoints; it can be connected or disconnected; and it can be stopped. The app can also open the page's tabs (Route, Schedules, Progress, Live map, History) and pause, resume, run or delete schedules. Separate code carries all of these out, not you.

Rules:
- Don't open with a greeting unless the person greeted you.
- You cannot see the page, which tab is open, or anything on screen. Never describe it.
- Never tell the person the exact words or commands to use. Talk naturally; if something is unclear, ask a short natural question.
- Write plain text with no markdown, lists or emoji. In a typed chat you may use up to about five sentences when the question needs it; keep greetings and small talk short.
- Never say you started, sent, scheduled, cancelled, opened, deleted or changed anything.
- About this project and its robot, only state the facts listed below; if you don't know, say so. General knowledge is fine.
- If a question is waiting for the person's answer, reply briefly and do not ask any question yourself: the waiting question is added after your reply.

Background: a 360-degree image is a panoramic photo of the whole room. A point cloud is a 3D LiDAR scan that records the room's geometry for measuring and comparing progress.

Facts:
{facts}"""

Chatter = Callable[[str, list[dict[str, str]], str], str | None]


def _plain_text(text: str) -> str:
    # Replies may be spoken, so markdown is dropped. Single underscores stay: names use them.
    text = re.sub(r"\*+|__|`+", "", text)
    text = re.sub(r"^\s*(#+|[-•])\s+", "", text, flags=re.M)
    return re.sub(r"\s+", " ", text).strip()[:900]


def chat_with_llm(message: str, history: list[dict[str, str]], facts: str) -> str | None:
    """A conversational reply for anything that is not a robot request. It never triggers actions."""
    messages = [{"role": "system", "content": CHAT_PROMPT.format(facts=facts)}]
    for turn in history[-8:]:
        text = str(turn.get("text") or "")[:500]
        if text:
            messages.append({"role": "assistant" if turn.get("role") == "assistant" else "user", "content": text})
    messages.append({"role": "user", "content": message})
    try:
        response = httpx.post(
            settings.assistant_api_url,
            json={
                "model": settings.assistant_model,
                "messages": messages,
                "stream": False,
                "think": False,
                "options": {"temperature": 0.7, "num_ctx": 4096, "num_predict": 320},
            },
            timeout=60,
        )
        response.raise_for_status()
        reply = _plain_text(response.json()["message"]["content"])
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("Assistant chat failed: %s", exc)
        return None
    return reply or None


class _LazyParse:
    """Calls the model at most once per message, and only if a deterministic path did not answer."""

    def __init__(self, parser: Parser, message: str, last_question: str | None) -> None:
        self._parser = parser
        self._message = message
        self._last_question = last_question
        self._done = False
        self._value: dict[str, Any] | None = None
        self.intent_override: str | None = None

    @property
    def value(self) -> dict[str, Any] | None:
        if not self._done:
            self._value = self._parser(self._message, self._last_question)
            self._done = True
        return self._value

    @property
    def unreachable(self) -> bool:
        return self.value is None

    def get(self, key: str, default: Any = None) -> Any:
        if key == "intent" and self.intent_override:
            return self.intent_override
        value = self.value or {}
        result = value.get(key)
        return default if result is None else result


# ---------------------------------------------------------------------------
# Deterministic readers. These are the guards: they only use what is in the text.
# ---------------------------------------------------------------------------

_STOP_RE = re.compile(r"\b(stop|halt|abort|freeze)\b", re.I)
_CANCEL_RE = re.compile(r"^\s*(cancel|never ?mind|forget (it|that)|start over)\b", re.I)
_YES_RE = re.compile(
    r"^\s*(yes|yeah|yea|ya|yah|yep|yup|sure|ok|okay|alright|all right|please do|yes please|do it|go ahead|"
    r"go for it|of course|correct|right|that'?s right|affirmative|enable it|turn it on|switch it on|"
    r"absolutely|definitely|sounds good|why not)\b",
    re.I,
)
_NO_RE = re.compile(
    r"^\s*(no|nope|nah|not (yet|now|really|right now)|don'?t|do not|later|maybe later|negative|"
    r"leave it( off)?|keep it off|no thanks)\b",
    re.I,
)
_LIST_RE = re.compile(
    r"\b(list|show)\b|\bby room\b|\bwhat (are|is) (the )?(waypoints|rooms|options)\b"
    r"|\bwhat (waypoints|rooms|options)\b|\bwhich (waypoints|rooms|options) (are|do)\b",
    re.I,
)

# A question is never taken as an answer: "what is a point cloud?" must not start a capture.
# "do"/"is"/"can"... only start a question when a subject follows ("is it", "can you"), so
# answers like "do both" stay answers.
_QUESTION_RE = re.compile(
    r"^\s*(what|what's|whats|how|how's|why|when|where|who|which)\b"
    r"|^\s*(is|are|do|does|did|can|could|should|would|will|was|were|has|have)\s+"
    r"(it|this|that|there|you|i|we|they|he|she|the|a|an|my|your|our|any|anything|something)\b",
    re.I,
)
# Asking what the choices are, while the assistant waits for an answer.
_OPTIONS_RE = re.compile(
    r"\b(options?|choices?|available|possibilities|alternatives)\b"
    r"|\bwhat (can|could|should|do) (i|we) (pick|choose|say|use|select|have|go with)\b"
    r"|\bwhat (are|were) (they|there|those|these)\b|\bwhat'?s (there|available)\b"
    r"|^\s*(list|show)( me)?( them| all| everything| the list| all of them)?\s*[.!?]*$"
    r"|\b(list|show me|tell me) (them|all|the (waypoints|rooms|options|choices|names))\b"
    r"|\bwhich (ones|waypoints|rooms) (are|is|do|can)\b"
    r"|\bwhat \w+ (can|could|should|do) (i|we) (pick|choose|use|select|say|have|set)\b"
    r"|\b(possible|allowed|valid|supported)\b|\bwhat can (it|the robot) (capture|do|take|record)\b",
    re.I,
)
_HELP_RE = re.compile(
    r"^\s*(help|(i )?don'?t know|dunno|not sure|no idea|any ideas|any suggestions?)\b"
    r"|\bwhat (do you mean|should i (say|put|write|call it))\b|\bsuggest\b",
    re.I,
)
# "how about 8am", "let's do kitchen": a suggestion is an answer.
_SUGGEST_RE = re.compile(
    r"^\s*(?:(?:what|how) about|maybe|let'?s say|let'?s (?:do|go with|use|make it)|make it|go with|use|"
    r"(?:can|could|would) (?:you|we) (?:do|make it|use|go with|set it (?:to|for)))\s+",
    re.I,
)
_GREETING_RE = re.compile(
    r"^\s*(hi|hello|hey|hiya|yo|good (morning|afternoon|evening|day)|thanks|thank you|cheers|"
    r"how are you|how's it going|what's up|sup)( there| again| everyone)?\s*[.!?]*\s*$",
    re.I,
)
_ACK_RE = re.compile(r"^\s*(ok|okay|k|yes|yeah|no|nope|sure|cool|great|nice|fine|hmm+|um+|uh+|right)\s*[.!?]*\s*$", re.I)
_ACTION_WORDS_RE = re.compile(r"\b(take|grab|capture|shoot|get|snap|record|scan|do)\b", re.I)
_CAPABILITY_RE = re.compile(
    r"^\s*(what can you do|what do you do|what are you|who are you|how does this work|how do i use (this|you)|"
    r"what can i (ask|say|do)( here| with you)?|help( me)?|what are (the|my) (choices|options)|"
    r"what (choices|options) do i have|what are the things you can do)\s*[.!?]*\s*$",
    re.I,
)
# Requests recognised from the words alone, so the core flows never depend on the model's guess.
_SCHEDULE_REQUEST_RE = re.compile(
    r"\b(create|make|set ?up|add|start|build|plan|want|need|like|schedule)\b[^.?!]{0,40}"
    r"\b(schedules?|recurring|routine|scheduled|regular(ly)?|every (day|morning|evening|night|week)|daily|weekly)\b"
    r"|^\s*(a |an )?(new|another) schedule\b",
    re.I,
)
# Words that make a request repeat: "every weekday", "each morning", "on Mondays", "daily".
_RECURRING_RE = re.compile(
    r"\b(every|each)\s+(single\s+)?(day|morning|afternoon|evening|night|week|weekday|weekend|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b"
    r"|\bon\s+(weekdays|weekends|mondays|tuesdays|wednesdays|thursdays|fridays|saturdays|sundays)\b"
    r"|\b(daily|weekly|nightly|everyday|recurring|routinely|regularly)\b",
    re.I,
)
_NEW_ONE_RE = re.compile(r"\b(create|make|add|set ?up|start|build|do)\b.{0,20}\b(a |an )?(new|another) one\b", re.I)
_GO_VERB_RE = re.compile(
    r"\b(go|goes|head|drive|move|send|navigate|visit|bring (it|the robot)|take (it|the robot))\b", re.I
)
_STATUS_RE = re.compile(
    r"^\s*(is it|is that|are (you|we|they)|has it|did it|is the (robot|capture|mission))\s+"
    r"(done|finished|complete|completed|over|ready|working|busy|still going|running|back)\b"
    r"|\b(status|progress)\b(?! tab)|^\s*how('?s| is) (it|the (robot|capture|mission)) going\b"
    r"|^\s*where('?s| is) (it|the robot)\b|^\s*what('?s| is) (it|the robot) doing\b",
    re.I,
)
_REQUEST_RE = re.compile(r"^\s*(can|could|would|will) you\b|^\s*please\b", re.I)
_STOP_WORDS_RE = re.compile(r"\b(stop|halt|abort|cancel|freeze|pause|enough|come back|return|wait)\b", re.I)
_CORRECTION_RE = re.compile(r"\b(actually|instead|change|wait|make it|rather|switch|rename)\b", re.I)
# A chat reply must never claim an action; only the coded paths act.
_ACTION_CLAIM_RE = re.compile(
    r"\b(I'?ve|I have|I'?m|I am|I just)\s+(now\s+|just\s+|already\s+)?(sent|sending|started|starting|"
    r"scheduled|scheduling|created|creating|cancell?ed|cancell?ing|connected|connecting|disconnected|"
    r"disconnecting|stopped|stopping|moved|moving|enabled|enabling|dispatched|dispatching)\b",
    re.I,
)

_PLACES_RE = re.compile(r"\b(waypoints?|rooms?|places?|locations?|points?|stops?|spots?)\b", re.I)

_DISCONNECT_RE = re.compile(r"\b(dis ?connect|shut (it )?down|shutdown|power (it )?off|take (it )?offline|go offline)\b", re.I)
_CONNECT_RE = re.compile(r"(?<!dis)(?<!dis )\b(connect|bring (it|the robot) online|go online)\b", re.I)

# "open the schedules tab": the page switches tab; nothing reaches the robot.
_TAB_RE = re.compile(
    r"\b(open|show( me)?|switch to|go to|take me to|bring up|display|view)\s+(the\s+)?"
    r"(?P<tab>routes?|schedules?|progress|live ?maps?|maps?|history)(?P<word>\s+(tab|page|view|screen)s?)?\b",
    re.I,
)
# Every tab named in a message: each needs an opening verb in front or "tab"/"page" after it, so
# "a new schedule named demo1" is not a tab but "open history tab and schedules tab" is two.
_TAB_PHRASE_RE = re.compile(
    r"(?P<verb>\b(?:open|show(?: me)?|switch to|go to|take me to|bring up|display|view)\s+(?:the\s+)?)?"
    r"\b(?P<tab>live ?maps?|[a-z]+)(?P<word>\s+(?:tab|page|view|screen)s?)?\b",
    re.I,
)
TAB_IDS = {"route": "route", "routes": "route", "schedule": "schedules", "schedules": "schedules",
           "progress": "progress", "livemap": "live", "live map": "live", "livemaps": "live", "live maps": "live",
           "map": "live", "maps": "live", "history": "history"}
TAB_LABELS = {"route": "Route", "schedules": "Schedules", "progress": "Progress", "live": "Live map", "history": "History"}

_IMAGE_RE = re.compile(r"\b(images?|photos?|photographs?|photograph|pictures?|pics?|panoramas?|panos?|360)\b", re.I)
_POINTCLOUD_RE = re.compile(r"\b(point ?clouds?|lidar|3d|pcd|depth)\b", re.I)
_BOTH_RE = re.compile(r"\b(both|everything|all of (it|them)|all)\b", re.I)

_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11",
    "twelve": "12", "thirteen": "13", "fourteen": "14", "fifteen": "15", "sixteen": "16",
    "seventeen": "17", "eighteen": "18", "nineteen": "19", "twenty": "20",
}
_FILLER_RE = re.compile(
    r"\b(please|the|go|to|i|mean|meant|it'?s|is|um+|uh+|oh|ok|okay|so|well|just|yeah|waypoints?|let'?s|try|how about)\b",
    re.I,
)
_ORDINALS = {"first": 0, "second": 1, "third": 2}


def detect_capture(text: str, *, allow_both: bool = False) -> list[str]:
    """Capture types the user actually named. Nothing is ever added that was not said."""
    image = bool(_IMAGE_RE.search(text))
    pointcloud = bool(_POINTCLOUD_RE.search(text))
    if allow_both and _BOTH_RE.search(text):
        image = pointcloud = True
    return [kind for kind, wanted in (("image", image), ("pointcloud", pointcloud)) if wanted]


def yes_or_no(text: str, parsed: _LazyParse | None = None) -> str | None:
    if _YES_RE.search(text):
        return "yes"
    if _NO_RE.search(text):
        return "no"
    if parsed is not None and parsed.get("yes_no") in ("yes", "no"):
        return parsed.get("yes_no")
    return None


_DAY_TOKENS = [
    (0, r"mon(day)?s?"),
    (1, r"tue(s|sday)?s?"),
    (2, r"wed(nesday)?s?"),
    (3, r"thu(r|rs|rsday)?s?"),
    (4, r"fri(day)?s?"),
    (5, r"sat(urday)?s?"),
    (6, r"sun(day)?s?"),
]
_DAY_RE = re.compile(r"\b(" + "|".join(f"(?P<d{i}>{p})" for i, p in _DAY_TOKENS) + r")\b", re.I)
_RANGE_RE = re.compile(
    r"\b(?P<a>" + "|".join(p for _, p in _DAY_TOKENS) + r")\s*(?:to|through|thru|until|till|-)\s*"
    r"(?P<b>" + "|".join(p for _, p in _DAY_TOKENS) + r")\b",
    re.I,
)
_EXCEPT_RE = re.compile(r"\b(except|but not|apart from|excluding|other than|besides|without)\b", re.I)


def _day_index(token: str) -> int | None:
    for index, pattern in _DAY_TOKENS:
        if re.fullmatch(pattern, token, re.I):
            return index
    return None


def _explicit_days(text: str) -> set[int]:
    days: set[int] = set()
    for match in _RANGE_RE.finditer(text):
        start, end = _day_index(match.group("a")), _day_index(match.group("b"))
        if start is None or end is None:
            continue
        index = start
        while True:
            days.add(index)
            if index == end:
                break
            index = (index + 1) % 7
    for match in _DAY_RE.finditer(text):
        for index, _ in _DAY_TOKENS:
            if match.group(f"d{index}"):
                days.add(index)
    if re.search(r"\bweekends?\b", text, re.I):
        days |= {5, 6}
    return days


def parse_weekdays(text: str) -> list[int] | None:
    """Read weekdays straight from the words, or None when the text names none."""
    parts = _EXCEPT_RE.split(text, maxsplit=1)
    positive_text = parts[0]
    negative_text = parts[2] if len(parts) == 3 else ""

    days: set[int] = set()
    if re.search(
        r"\b(every ?day|everyday|daily|each day|all week|all days|all the days|every single day|seven days|7 days|"
        r"(every|each) (morning|afternoon|evening|night))\b",
        positive_text,
        re.I,
    ):
        days = set(range(7))
    if re.search(r"\b(weekdays|work ?days|working days|every weekday)\b", positive_text, re.I):
        days |= set(range(5))
    days |= _explicit_days(positive_text)
    if not days:
        return None
    days -= _explicit_days(negative_text)
    return sorted(days) or None


_AM_RE = re.compile(r"\b(a\.?m\.?|morning|in the morning)\b", re.I)
_PM_RE = re.compile(r"\b(p\.?m\.?|afternoon|evening|night|tonight)\b", re.I)
_CLOCK_RE = re.compile(r"\b(?P<h>\d{1,2})(?:[:.h](?P<m>[0-5]\d))?\s*(?P<ampm>a\.?m\.?|p\.?m\.?)?(?![\w:])", re.I)


@dataclass
class TimeReading:
    value: str | None = None
    candidates: list[str] = field(default_factory=list)  # set when am/pm is unclear


def _fmt(hour: int, minute: int) -> str:
    return f"{hour:02d}:{minute:02d}"


def _ampm_candidates(hour: int, minute: int) -> list[str]:
    base = hour % 12
    return [_fmt(base, minute), _fmt(base + 12, minute)]


def read_time(text: str, llm_time: str | None, *, expect_time: bool = False) -> TimeReading:
    """Return a definite time, two candidates when am/pm is unclear, or nothing.

    expect_time: the person was just asked for a time, so a bare "8" or "0830" is one.
    """
    if expect_time:
        military = re.fullmatch(r"\s*([01]\d|2[0-3])([0-5]\d)\s*(hours|hrs|h)?\s*[.!]?\s*", text, re.I)
        if military:
            return TimeReading(_fmt(int(military.group(1)), int(military.group(2))))
    if re.search(r"\b(noon|midday)\b", text, re.I):
        return TimeReading("12:00")
    if re.search(r"\bmidnight\b", text, re.I):
        return TimeReading("00:00")

    am, pm = bool(_AM_RE.search(text)), bool(_PM_RE.search(text))
    for match in _CLOCK_RE.finditer(text):
        hour = int(match.group("h"))
        minute = int(match.group("m") or 0)
        marker = (match.group("ampm") or "").lower()
        has_minutes = match.group("m") is not None
        if hour > 23:
            continue
        # A bare number is only a time when it is clearly used as one ("at 7", "7 pm").
        if not has_minutes and not marker:
            before = text[: match.start()].rstrip().lower()
            if not expect_time and not before.endswith("at") and not (am or pm):
                continue
        if marker.startswith("a") or (am and not marker):
            if hour > 12:
                continue
            return TimeReading(_fmt(hour % 12, minute))
        if marker.startswith("p") or (pm and not marker):
            if hour > 12:
                return TimeReading(_fmt(hour, minute))
            return TimeReading(_fmt(hour % 12 + 12, minute))
        # 24-hour clock written out ("18:00", "07:30", "0:15") is unambiguous.
        if has_minutes and (hour >= 13 or hour == 0 or match.group("h").startswith("0")):
            return TimeReading(_fmt(hour, minute))
        if hour >= 13:
            return TimeReading(_fmt(hour, minute))
        return TimeReading(candidates=_ampm_candidates(hour, minute))

    # Nothing readable in the digits ("half past seven"): fall back to the model, guarded.
    if llm_time and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", llm_time):
        hour, minute = int(llm_time[:2]), int(llm_time[3:])
        if am and hour < 12:
            return TimeReading(_fmt(hour, minute))
        if am and hour >= 12:
            return TimeReading(_fmt(hour - 12, minute))
        if pm and hour < 12:
            return TimeReading(_fmt(hour + 12, minute))
        if pm:
            return TimeReading(_fmt(hour, minute))
        return TimeReading(candidates=_ampm_candidates(hour, minute))
    return TimeReading()


def pick_time_candidate(text: str, candidates: list[str]) -> str | None:
    if _AM_RE.search(text):
        return candidates[0]
    if _PM_RE.search(text):
        return candidates[1]
    for candidate in candidates:
        hour, minute = int(candidate[:2]), int(candidate[3:])
        spelled = {candidate, f"{hour}:{minute:02d}"}
        if any(re.search(rf"(?<![\d:]){re.escape(s)}(?![\d])", text) for s in spelled):
            return candidate
    for word, index in _ORDINALS.items():
        if re.search(rf"\b{word}\b", text, re.I) and index < len(candidates):
            return candidates[index]
    if re.search(r"\b(last|latter|later one)\b", text, re.I):
        return candidates[-1]
    return None


# ---------------------------------------------------------------------------
# Wording helpers.
# ---------------------------------------------------------------------------


def _join(items: list[str], last: str = "and") -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} {last} {items[-1]}"


def describe_capture(capture: list[str]) -> str:
    labels = {"image": "a 360° image", "pointcloud": "a point cloud"}
    return _join([labels[kind] for kind in ("image", "pointcloud") if kind in capture])


def describe_schedule_capture(capture: list[str]) -> str:
    labels = {"image": "360° images", "pointcloud": "point clouds"}
    return _join([labels[kind] for kind in ("image", "pointcloud") if kind in capture])


def describe_days(days: list[int]) -> str:
    if sorted(days) == list(range(7)):
        return "every day"
    if sorted(days) == list(range(5)):
        return "weekdays"
    if sorted(days) == [5, 6]:
        return "weekends"
    return _join([WEEKDAY_NAMES[day] for day in sorted(days)])


def describe_when(local_time: str, days: list[int]) -> str:
    when = describe_days(days)
    if when in ("every day", "weekdays", "weekends"):
        return f"{'every day' if when == 'every day' else 'on ' + when} at {local_time}"
    return f"at {local_time} on {when}"


def normalize_name(text: str) -> str:
    lowered = text.lower()
    for word, digit in _NUMBER_WORDS.items():
        lowered = re.sub(rf"\b{word}\b", digit, lowered)
    return re.sub(r"[^a-z0-9]", "", lowered)


# ---------------------------------------------------------------------------
# The conversation.
# ---------------------------------------------------------------------------


@dataclass
class AssistantResult:
    reply: str
    state: dict[str, Any] | None = None
    action: dict[str, Any] | None = None
    # What is being built right now (a route or a schedule), so the page can show it filling in.
    draft: dict[str, Any] | None = None


@dataclass
class _Context:
    db: Session
    user: User
    project: Project
    robot_id: str
    timezone: str
    robot_connected: bool
    voice: bool
    points: list[RobotCapturePoint]
    room_names: dict[str, str]
    history: list[dict[str, str]]
    chatter: Chatter
    # Set when the same message also asked to connect the robot ("connect it and go to the lobby").
    connect_ok: bool = False

    @property
    def list_limit(self) -> int:
        return SPOKEN_LIST_LIMIT if self.voice else TEXT_LIST_LIMIT

    def point(self, point_id: str) -> RobotCapturePoint | None:
        return next((p for p in self.points if p.id == point_id), None)

    def names(self, point_ids: list[str]) -> list[str]:
        return [p.name for pid in point_ids if (p := self.point(pid)) is not None]

    def rooms(self) -> dict[str, list[RobotCapturePoint]]:
        grouped: dict[str, list[RobotCapturePoint]] = {}
        for point in self.points:
            grouped.setdefault(point.room_slug or "", []).append(point)
        return grouped

    def room_label(self, slug: str) -> str:
        if not slug:
            return "other"
        return self.room_names.get(slug, slug)


def _ask(
    state: dict[str, Any],
    awaiting: str,
    question: str,
    *,
    prefix: str = "",
    reask: str | None = None,
    **extra: Any,
) -> AssistantResult:
    """Ask a question. `reask` is the short form used when the question has to be put again,
    so a long list is never repeated after a conversational reply."""
    state = {**state, "awaiting": awaiting, "question": question, "reask": reask or question, **extra}
    return AssistantResult(reply=f"{prefix}{question}", state=state)


def _done(reply: str, action: dict[str, Any] | None = None) -> AssistantResult:
    return AssistantResult(reply=reply, state=None, action=action)


def match_points(query: str, points: list[RobotCapturePoint]) -> tuple[RobotCapturePoint | None, list[RobotCapturePoint]]:
    """Return (exact match, suggestions). Suggestions are only offered, never used silently."""
    key = normalize_name(re.sub(r"^\s*(the|a|an)\s+", "", query, flags=re.I))
    if not key:
        return None, []
    by_key = {normalize_name(point.name): point for point in points}
    if key in by_key:
        return by_key[key], []
    contains = [point for point in points if key in normalize_name(point.name)]
    close = [by_key[k] for k in difflib.get_close_matches(key, list(by_key), n=MAX_SUGGESTIONS, cutoff=0.75)]
    suggestions: list[RobotCapturePoint] = []
    for point in contains + close:
        if point not in suggestions:
            suggestions.append(point)
    return None, suggestions


def _names_in_text(text: str, points: list[RobotCapturePoint]) -> list[RobotCapturePoint]:
    """Waypoints named word for word in the text, in the order they were said."""
    found: list[tuple[int, int, RobotCapturePoint]] = []
    for point in sorted(points, key=lambda p: len(p.name), reverse=True):
        parts = [re.escape(part) for part in re.split(r"[\s_-]+", point.name) if part]
        pattern = r"(?<![\w-])" + r"[\s_-]*".join(parts) + r"(?![\w-])"
        for match in re.finditer(pattern, text, re.I):
            if not any(start <= match.start() < end for start, end, _ in found):
                found.append((match.start(), match.end(), point))
    return [point for _, _, point in sorted(found, key=lambda item: item[0])]


def _clean_query(text: str) -> str:
    return re.sub(r"\s+", " ", _FILLER_RE.sub(" ", text)).strip(" .,!?")


def _split_names(text: str) -> list[str]:
    """'kitchen, lobby then room1' -> ['kitchen', 'lobby', 'room1'] (raw, unresolved)."""
    parts = re.split(r",|;|\bthen\b|\band\b|\bafter that\b|->|→", _clean_query(text), flags=re.I)
    return [part.strip() for part in parts if part.strip()]


def _pick_option(text: str, options: list[RobotCapturePoint]) -> RobotCapturePoint | None:
    exact, suggestions = match_points(_clean_query(text), options)
    if exact is not None:
        return exact
    if len(suggestions) == 1:
        return suggestions[0]
    for word, index in _ORDINALS.items():
        if re.search(rf"\b{word}\b", text, re.I) and index < len(options):
            return options[index]
    if re.search(r"\blast\b", text, re.I) and options:
        return options[-1]
    return None


ROUTE_QUESTION = "Which waypoints should it visit, in order?"
WHICH_WAYPOINT = "Which waypoint do you mean?"
GO_WHERE = "Which waypoint should the robot go to?"
OFFER_GO = "Want me to send the robot to one of them?"


def _choosing_route(state: dict[str, Any]) -> bool:
    return state.get("intent") == "create_schedule" and state.get("pending_index") is None


def _choice_tail(state: dict[str, Any]) -> tuple[str, str]:
    """(question, short re-ask) to end a list of waypoints with."""
    if state.get("soft"):
        return OFFER_GO, OFFER_GO
    if state.get("pending_index") is not None:
        return "Which one do you mean?", WHICH_WAYPOINT
    return "Which one?", GO_WHERE


def _list_rooms(ctx: _Context, state: dict[str, Any]) -> AssistantResult:
    rooms = ctx.rooms()
    names = [point.name for point in ctx.points]
    if len(ctx.points) <= ctx.list_limit:
        listing = f"The available waypoints are {_join(names)}."
        if _choosing_route(state):
            return _ask(state, "waypoints", f"{listing} {ROUTE_QUESTION}", reask=ROUTE_QUESTION)
        tail, reask = _choice_tail(state)
        return _ask(state, "waypoint_choice", f"{listing} {tail}", reask=reask, options=[p.id for p in ctx.points])
    if len([slug for slug in rooms if slug]) < 2:
        # Rooms don't split the list up usefully: show a sample and narrow by name.
        question = (
            f"This project has {len(names)} waypoints, including {_join(names[: ctx.list_limit])}. "
            "Tell me part of a name and I'll narrow it down."
        )
        return _ask(state, "waypoint_query", question, reask="Which waypoint? Tell me part of its name.")
    labels = [ctx.room_label(slug) for slug in rooms]
    if len(labels) <= ctx.list_limit:
        question = f"There are waypoints in {len(labels)} rooms: {_join(labels)}. Which room?"
    else:
        question = f"There are waypoints in {len(labels)} rooms, including {_join(labels[: ctx.list_limit])}. Which room?"
    return _ask(state, "room_choice", question, reask="Which room?", options=list(rooms))


def _list_room_points(ctx: _Context, state: dict[str, Any], slug: str) -> AssistantResult:
    points = ctx.rooms().get(slug, [])
    label = ctx.room_label(slug)
    label = label[:1].upper() + label[1:]
    names = [point.name for point in points]
    if len(points) <= ctx.list_limit:
        listing = f"{label} has {len(points)} waypoint{'s' if len(points) != 1 else ''}: {_join(names)}."
    else:
        listing = f"{label} has {len(points)} waypoints, including {_join(names[: ctx.list_limit])}."
    if _choosing_route(state):
        return _ask(state, "waypoints", f"{listing} {ROUTE_QUESTION}", reask=ROUTE_QUESTION)
    tail, reask = _choice_tail(state)
    return _ask(state, "waypoint_choice", f"{listing} {tail}", reask=reask, options=[p.id for p in points])


def _pick_room(text: str, ctx: _Context, slugs: list[str]) -> str | None:
    key = normalize_name(_clean_query(text))
    if not key:
        return None
    labels = {normalize_name(ctx.room_label(slug)): slug for slug in slugs}
    labels.update({normalize_name(slug): slug for slug in slugs if slug})
    if key in labels:
        return labels[key]
    contains = {slug for label, slug in labels.items() if key in label}
    if len(contains) == 1:
        return contains.pop()
    close = difflib.get_close_matches(key, list(labels), n=1, cutoff=0.7)
    return labels[close[0]] if close else None


def _room_named(ctx: _Context, query: str, *, loose: bool) -> str | None:
    """The room a query names: exactly, or (when nothing else matched) as part of its name."""
    key = normalize_name(re.sub(r"^\s*(the|a|an)\s+", "", query, flags=re.I))
    if not key:
        return None
    slugs = [slug for slug in ctx.rooms() if slug]
    for slug in slugs:
        if key in (normalize_name(slug), normalize_name(ctx.room_label(slug))):
            return slug
    if loose:
        partial = [slug for slug in slugs if key in normalize_name(ctx.room_label(slug)) or normalize_name(ctx.room_label(slug)) in key]
        if len(partial) == 1:
            return partial[0]
    return None


def _local_now(ctx: _Context) -> datetime:
    try:
        return datetime.now(ZoneInfo(ctx.timezone))
    except Exception:  # noqa: BLE001 - an unknown browser zone falls back to UTC
        return datetime.now(dt_timezone.utc)


TERMINAL_STEP_OK = ("succeeded", "done", "completed", "skipped_ok")


def _mission_points(mission: RobotMission) -> list[str]:
    steps = sorted(mission.steps, key=lambda step: step.sequence_index)
    return [step.waypoint_name for step in steps]


def _mission_capture(mission: RobotMission) -> list[str]:
    outputs = (mission.robot_meta_json or {}).get("capture_outputs")
    if isinstance(outputs, list) and outputs:
        return [kind for kind in ("image", "pointcloud") if kind in outputs]
    return ["pointcloud"] if mission.capture_mode == "pointcloud" else ["image"]


def _ago(ctx: _Context, moment: datetime | None) -> str:
    if moment is None:
        return ""
    minutes = int((datetime.now(dt_timezone.utc).replace(tzinfo=None) - moment).total_seconds() // 60)
    if minutes < 1:
        return " just now"
    if minutes < 60:
        return f" {minutes} minute{'s' if minutes != 1 else ''} ago"
    local = moment.replace(tzinfo=dt_timezone.utc).astimezone(_local_now(ctx).tzinfo)
    return f" at {local:%H:%M}"


def describe_finished(ctx: _Context, mission: RobotMission) -> str:
    """One plain sentence on how a capture ended."""
    steps = sorted(mission.steps, key=lambda step: step.sequence_index)
    names = [step.waypoint_name for step in steps]
    capture = _mission_capture(mission)
    ok = [step for step in steps if step.status in TERMINAL_STEP_OK]
    bad = [step for step in steps if step.status in ("failed", "error", "skipped")]
    if mission.status == "succeeded":
        if bad:
            problems = ", ".join(
                f"{step.waypoint_name}{f' ({step.error_message})' if step.error_message else ''}" for step in bad
            )
            return f"The capture finished, but {len(bad)} of {len(steps)} stops had problems: {problems}."
        if len(names) == 1:
            return f"Done. The robot captured {describe_capture(capture)} at {names[0]}."
        return (f"Done. The robot visited {_join(names)} and captured {describe_schedule_capture(capture)} "
                "at each stop.")
    if mission.status in ("cancelled", "stopped"):
        return f"The capture was cancelled after {len(ok)} of {len(steps)} stop{'s' if len(steps) != 1 else ''}."
    if mission.status == "failed":
        failed = bad[0] if bad else None
        if failed is not None:
            detail = f": {failed.error_message}" if failed.error_message else "."
            return f"The capture failed at {failed.waypoint_name}{detail}"
        return "The capture failed. The Progress tab has the details."
    return f"The capture ended with status \"{mission.status.replace('_', ' ')}\". The Progress tab has the details."


def _status_reply(ctx: _Context) -> str:
    mission = _active_mission(ctx)
    connection = "connected" if ctx.robot_connected else "not connected"
    if mission is not None:
        steps = sorted(mission.steps, key=lambda step: step.sequence_index)
        done = sum(1 for step in steps if step.status in TERMINAL_STEP_OK)
        current = next((step.waypoint_name for step in steps if step.status == "running"), None)
        if mission.status in ("queued", "dispatched"):
            return "Not yet. The capture is queued and the robot is about to start."
        if mission.status in ("cancel_requested", "cancelling", "returning_to_start", "stop_requested"):
            return "The capture is being cancelled and the robot is heading back."
        where = f", currently at {current}" if current else ""
        return f"Not yet. The robot has finished {done} of {len(steps)} stops{where}."
    last = ctx.db.scalar(
        select(RobotMission)
        .join(User, RobotMission.robot_user_id == User.id)
        .where((User.username == ctx.robot_id) | (User.id == ctx.robot_id))
        .order_by(RobotMission.created_at.desc())
    )
    if last is None:
        return f"The robot is idle and {connection}. It hasn't run any captures yet."
    ended = last.completed_at or last.cancelled_at or last.created_at
    return f"{describe_finished(ctx, last)} That was{_ago(ctx, ended)}. The robot is now idle and {connection}."


def _facts(ctx: _Context, state: dict[str, Any] | None) -> str:
    """Everything the chat model may state about this project and robot. Nothing else is known."""
    mission = _active_mission(ctx)
    names = [point.name for point in ctx.points]
    now = _local_now(ctx)
    lines = [
        f"Current local time: {now:%A %d %B %Y, %H:%M} ({ctx.timezone}).",
        f"Project: {ctx.project.name}.",
        f"Robot: {ctx.robot_id}, {'connected' if ctx.robot_connected else 'not connected'}.",
        f"Robot activity: {'a capture is ' + mission.status.replace('_', ' ') if mission else 'idle'}.",
        (
            f"Waypoints ({len(names)}): {', '.join(names[:40])}{', and more' if len(names) > 40 else ''}."
            if names
            else "This project has no waypoints yet; they are added on the Route tab."
        ),
    ]
    schedules = ctx.db.scalars(
        select(RobotMissionSchedule)
        .where(RobotMissionSchedule.project_id == ctx.project.id)
        .order_by(RobotMissionSchedule.created_at.desc())
        .limit(10)
    ).all()
    if schedules:
        described = "; ".join(
            f'"{s.name}" {describe_when(s.local_time, list(s.weekdays_json or []))}, '
            f"{'enabled' if s.enabled else 'switched off'}"
            for s in schedules
        )
        lines.append(f"Schedules: {described}.")
    else:
        lines.append("Schedules: none yet.")
    last = ctx.db.scalar(
        select(RobotMission)
        .join(User, RobotMission.robot_user_id == User.id)
        .where((User.username == ctx.robot_id) | (User.id == ctx.robot_id))
        .order_by(RobotMission.created_at.desc())
    )
    if last is not None:
        ended = last.completed_at or last.cancelled_at or last.created_at
        lines.append(f"Most recent capture (finished{_ago(ctx, ended)}): {describe_finished(ctx, last)}")
    if state and state.get("question"):
        lines.append(f"Question waiting for the operator's answer: {state.get('reask') or state['question']}")
    if ctx.voice:
        lines.append("This reply will be spoken aloud, so keep it to one or two short sentences.")
    return "\n".join(lines)


def _chat(ctx: _Context, text: str, state: dict[str, Any] | None = None) -> str | None:
    reply = ctx.chatter(text, ctx.history, _facts(ctx, state))
    if reply and _ACTION_CLAIM_RE.search(reply):
        logger.info("Assistant chat reply claimed an action and was replaced: %r", reply)
        return (
            "I haven't done anything yet. Tell me what you'd like, such as where to send the robot "
            "or which schedule to change, and I'll take care of it."
        )
    return reply


def _without_questions(reply: str) -> str:
    sentences = re.findall(r"[^.!?]+[.!?]*", reply)
    return " ".join(part.strip() for part in sentences if not part.strip().endswith("?")).strip()


def _reask(ctx: _Context, state: dict[str, Any], text: str) -> AssistantResult:
    """Small talk or a question mid-flow: answer naturally, then put the pending question again
    in its short form. An offer ("want me to send the robot?") just ends instead."""
    if state.get("soft"):
        reply = _chat(ctx, text, None)
        return _done(_without_questions(reply) if reply and _without_questions(reply) else (reply or CAPABILITIES))
    reply = _chat(ctx, text, state)
    reply = _without_questions(reply) if reply else ""
    reask = state.get("reask") or state.get("question") or ""
    prefix = f"{reply} " if reply else "Sorry, I didn't catch that. "
    return AssistantResult(reply=f"{prefix}{reask}", state=state)


SLOT_HELP = {
    "time": "Any time of day works, for example 08:00, 7:30 am or 6 pm.",
    "weekdays": "Any days of the week, for example every day, weekdays, weekends, or Monday and Thursday.",
    "weekdays_confirm": "Say yes to keep those days, or tell me different ones.",
    "name": 'Any name you like, for example "Morning sweep" or "Kitchen check".',
    "enable": "Yes switches the schedule on so it runs at its set time. No keeps it saved but switched off.",
    "connect": "Yes connects the robot and then starts the capture. No cancels it.",
    "waypoint_confirm": "Say yes to use that waypoint, or tell me a different one.",
}
WAYPOINT_SLOTS = ("waypoints", "waypoint_choice", "waypoint_query", "room_choice", "waypoint_confirm")


def _slot_help(ctx: _Context, state: dict[str, Any]) -> AssistantResult:
    """'What are the choices?' for whatever is being asked right now."""
    awaiting = state.get("awaiting")
    reask = state.get("reask") or state.get("question") or ""
    if awaiting in ("waypoints", "waypoint_choice", "waypoint_query", "room_choice"):
        if not ctx.points:
            return _done("This project has no waypoints yet. Add them on the Route tab first.")
        return _list_rooms(ctx, state)
    if awaiting == "capture":
        return AssistantResult(reply=f"{CAPTURE_OPTIONS} Which one would you like?", state=state)
    if awaiting == "time_ampm":
        first, second = state.get("time_candidates") or ["", ""]
        return AssistantResult(reply=f"{first} is in the morning and {second} is in the evening. {reask}", state=state)
    return AssistantResult(reply=f"{SLOT_HELP.get(awaiting, CAPABILITIES)} {reask}", state=state)


def _not_understood(ctx: _Context, state: dict[str, Any]) -> AssistantResult:
    awaiting = state.get("awaiting")
    reask = state.get("reask") or state.get("question") or ""
    if awaiting in WAYPOINT_SLOTS and awaiting != "waypoint_confirm":
        hint = "Tell me a waypoint name, or ask me to list them."
    elif awaiting == "capture":
        hint = CAPTURE_OPTIONS
    else:
        hint = SLOT_HELP.get(awaiting, "")
    return AssistantResult(reply=f"Sorry, I didn't get that. {hint} {reask}".replace("  ", " "), state=state)


def _unresolved_index(state: dict[str, Any]) -> int | None:
    for index, entry in enumerate(state.get("waypoints") or []):
        if not entry.get("id"):
            return index
    return None


def _resolve_waypoints(ctx: _Context, state: dict[str, Any]) -> AssistantResult | None:
    """Resolve each named waypoint in turn. Returns a question, or None when all are known."""
    while (index := _unresolved_index(state)) is not None:
        entry = state["waypoints"][index]
        exact, suggestions = match_points(entry["query"], ctx.points)
        if exact is not None:
            entry["id"] = exact.id
            continue
        query = entry["query"]
        state = {**state, "pending_index": index}
        room = _room_named(ctx, query, loose=not suggestions)
        if room is not None:
            return _list_room_points(ctx, state, room)
        if len(suggestions) == 1:
            question = f'I couldn\'t find "{query}". Did you mean {suggestions[0].name}?'
            return _ask(state, "waypoint_confirm", question, reask=f"Did you mean {suggestions[0].name}?",
                        options=[suggestions[0].id])
        if 1 < len(suggestions) <= MAX_SUGGESTIONS:
            names = [point.name for point in suggestions]
            return _ask(
                state,
                "waypoint_choice",
                f'I couldn\'t find "{query}". Did you mean {_join(names, "or")}?',
                reask=f"Did you mean {_join(names, 'or')}?",
                options=[point.id for point in suggestions],
            )
        if len(suggestions) > MAX_SUGGESTIONS:
            return _ask(
                state,
                "waypoint_query",
                f'{len(suggestions)} waypoints match "{query}". Tell me more of the name, or I can list them by room.',
                reask="Which one do you mean? Tell me more of the name.",
            )
        if len(ctx.points) <= ctx.list_limit:
            names = [point.name for point in ctx.points]
            return _ask(
                state,
                "waypoint_choice",
                f'I couldn\'t find a waypoint called "{query}" in this project. '
                f"The available waypoints are {_join(names)}. Which one do you mean?",
                reask=WHICH_WAYPOINT,
                options=[point.id for point in ctx.points],
            )
        return _ask(
            state,
            "waypoint_query",
            f'I couldn\'t find "{query}". This project has {len(ctx.points)} waypoints. '
            "Tell me part of the name, or I can list them by room.",
            reask="Which waypoint do you mean? Tell me part of its name.",
        )
    state.pop("pending_index", None)
    return None


def _replace_pending(state: dict[str, Any], new: list[dict[str, Any]]) -> dict[str, Any]:
    """Put waypoint entries where the pending one was, or make them the route when none is pending."""
    entries = [dict(entry) for entry in state.get("waypoints") or []]
    index = state.get("pending_index")
    if index is None or index >= len(entries):
        entries = new if state.get("awaiting") in ("waypoints", "waypoint_choice", "room_choice") else entries + new
    else:
        entries[index:index + 1] = new
    state = {**state, "waypoints": entries, "soft": False}
    state.pop("pending_index", None)
    return state


def _with_points(state: dict[str, Any], points: list[RobotCapturePoint]) -> dict[str, Any]:
    return _replace_pending(state, [{"query": point.name, "id": point.id} for point in points])


def _with_queries(state: dict[str, Any], queries: list[str]) -> dict[str, Any]:
    return _replace_pending(state, [{"query": query, "id": None} for query in queries])


def _active_mission(ctx: _Context) -> RobotMission | None:
    return ctx.db.scalar(
        select(RobotMission)
        .join(User, RobotMission.robot_user_id == User.id)
        .where(
            (User.username == ctx.robot_id) | (User.id == ctx.robot_id),
            RobotMission.status.in_(ACTIVE_MISSION_STATUSES),
        )
        .order_by(RobotMission.created_at.desc())
    )


def _today(ctx: _Context):
    return _local_now(ctx).date()


def _start_mission(ctx: _Context, state: dict[str, Any]) -> AssistantResult:
    point_ids = [entry["id"] for entry in state["waypoints"]]
    capture = state["capture"]
    mission = create_robot_mission(
        RobotMissionCreateRequest(
            robot_id=ctx.robot_id,
            project_slug=ctx.project.slug,
            capture_point_ids=point_ids,
            capture_mode="panorama" if "image" in capture else "pointcloud",
            capture_date=_today(ctx),
            retry_policy={"continue_on_failure": True},
            robot_meta={"capture_outputs": capture, "trigger": "assistant"},
        ),
        current_user=ctx.user,
        db=ctx.db,
    )
    names = ctx.names(point_ids)
    if len(names) == 1:
        reply = f"Going to {names[0]} and capturing {describe_capture(capture)}."
    else:
        reply = f"Going to {', then '.join(names)}, capturing {describe_capture(capture)} at each stop."
    return _done(reply, {"type": "mission_started", "mission_id": mission.id})


def _advance_go_to(ctx: _Context, state: dict[str, Any], prefix: str = "") -> AssistantResult:
    if ctx.connect_ok and not state.get("connect_ok"):
        state = {**state, "connect_ok": True}  # remembered across the questions still to come
    if not state.get("waypoints"):
        return _ask(state, "waypoints", "Where should I go?", prefix=prefix, reask=GO_WHERE)
    question = _resolve_waypoints(ctx, state)
    if question is not None:
        question.reply = prefix + question.reply
        return question
    names = ctx.names([entry["id"] for entry in state["waypoints"]])
    if not state.get("capture"):
        return _ask(
            state,
            "capture",
            f"What should I capture at {_join(names)}? Want me to list the options?",
            prefix=prefix,
            reask=f"What should I capture at {_join(names)}: a 360° image, a point cloud, or both?",
        )
    if _active_mission(ctx) is not None:
        return _done(f"{prefix}The robot is busy with another capture. Say stop to cancel it first.")
    if not ctx.robot_connected and (ctx.connect_ok or state.get("connect_ok")):
        create_robot_command(
            RobotCommandCreateRequest(robot_id=ctx.robot_id, kind="connect"),
            current_user=ctx.user,
            db=ctx.db,
        )
        return AssistantResult(
            reply=f"{prefix}Connecting the robot. I'll go to {_join(names)} as soon as it's connected.",
            state={**state, "awaiting": "connect_wait", "question": None, "reask": None},
            action={"type": "robot_connecting"},
        )
    if not ctx.robot_connected:
        return _ask(state, "connect", "The robot isn't connected. Should I connect it?", prefix=prefix,
                    reask="Should I connect the robot?")
    result = _start_mission(ctx, state)
    result.reply = prefix + result.reply
    return result


def _create_schedule(ctx: _Context, state: dict[str, Any]) -> AssistantResult:
    point_ids = [entry["id"] for entry in state["waypoints"]]
    capture = state["capture"]
    schedule = create_robot_mission_schedule(
        RobotMissionScheduleCreateRequest(
            name=state["name"],
            robot_id=ctx.robot_id,
            project_slug=ctx.project.slug,
            capture_point_ids=point_ids,
            local_time=state["time"],
            timezone=ctx.timezone,
            weekdays=state["weekdays"],
            enabled=False,
            capture_mode="panorama" if "image" in capture else "pointcloud",
            retry_policy={"continue_on_failure": True},
            robot_meta={"capture_outputs": capture, "trigger": "assistant"},
        ),
        current_user=ctx.user,
        db=ctx.db,
    )
    route = " → ".join(ctx.names(point_ids))
    summary = (
        f'Created "{schedule.name}": {route}, {describe_when(state["time"], state["weekdays"])}, '
        f"capturing {describe_schedule_capture(capture)}. {DEFAULT_FIELDS_NOTE} "
    )
    question = "Do you want me to enable this schedule?"
    return AssistantResult(
        reply=summary + question,
        state={
            "intent": "enable_schedule",
            "schedule_id": schedule.id,
            "schedule_name": schedule.name,
            "awaiting": "enable",
            "question": question,
            "reask": question,
        },
        action={"type": "schedule_created", "schedule_id": schedule.id},
    )


def _advance_schedule(ctx: _Context, state: dict[str, Any], prefix: str = "") -> AssistantResult:
    if not state.get("name"):
        return _ask(state, "name", "What should I call this schedule?", prefix=prefix)
    if not state.get("waypoints"):
        return _ask(state, "waypoints", ROUTE_QUESTION, prefix=prefix)
    question = _resolve_waypoints(ctx, state)
    if question is not None:
        question.reply = prefix + question.reply
        return question
    point_ids = [entry["id"] for entry in state["waypoints"]]
    if len(set(point_ids)) != len(point_ids):
        state = {**state, "waypoints": []}
        return _ask(
            state,
            "waypoints",
            f"A schedule can visit each waypoint only once. {ROUTE_QUESTION}",
            prefix=prefix,
            reask=ROUTE_QUESTION,
        )
    if state.get("time_candidates"):
        first, second = state["time_candidates"]
        return _ask(state, "time_ampm", f"{first} or {second}?", prefix=prefix)
    if not state.get("time"):
        return _ask(state, "time", "What time should it run?", prefix=prefix)
    if state.get("weekdays_pending"):
        days = describe_days(state["weekdays_pending"])
        return _ask(state, "weekdays_confirm", f"{days[:1].upper()}{days[1:]}, right?", prefix=prefix)
    if not state.get("weekdays"):
        return _ask(state, "weekdays", "Which days should it run?", prefix=prefix)
    if not state.get("capture"):
        return _ask(
            state,
            "capture",
            "What should it capture at each stop? Want me to list the options?",
            prefix=prefix,
            reask="What should it capture at each stop: a 360° image, a point cloud, or both?",
        )
    result = _create_schedule(ctx, state)
    result.reply = prefix + result.reply
    return result


def _advance(ctx: _Context, state: dict[str, Any], prefix: str = "") -> AssistantResult:
    if state.get("intent") == "go_to":
        return _advance_go_to(ctx, state, prefix)
    if state.get("intent") == "create_schedule":
        return _advance_schedule(ctx, state, prefix)
    return _done(prefix + CAPABILITIES)


def _apply_time(state: dict[str, Any], reading: TimeReading) -> dict[str, Any]:
    state = {**state}
    state.pop("time_candidates", None)
    if reading.value:
        state["time"] = reading.value
    elif reading.candidates:
        state["time_candidates"] = reading.candidates
    return state


def _apply_weekdays(state: dict[str, Any], text: str, llm_days: list[int] | None) -> dict[str, Any]:
    """Days read from the words win when they agree with the model; any disagreement is asked."""
    words = parse_weekdays(text)
    model = sorted({int(day) for day in llm_days or [] if isinstance(day, int) and 0 <= day <= 6}) or None
    state = {**state}
    state.pop("weekdays_pending", None)
    if words and (model is None or model == words):
        state["weekdays"] = words
    elif words:
        state["weekdays_pending"] = words
    elif model:
        state["weekdays_pending"] = model
    return state


def _clean_schedule_name(text: str) -> str:
    name = re.sub(r"^\s*(call it|name it|it'?s called|let'?s call it|the name is|name:?)\s+", "", text, flags=re.I)
    name = name.strip(" \"'“”.!?")
    return name[:1].upper() + name[1:120] if name else ""


def _without_names(text: str, names: list[str]) -> str:
    """Drop waypoint names first, so a waypoint called "3D lab" is not read as a capture type."""
    for name in sorted(names, key=len, reverse=True):
        text = re.sub(re.escape(name), " ", text, flags=re.I)
    return text


_ADD_RE = re.compile(r"\b(add|adding|also|too|plus|as well|include|including|extra|another)\b", re.I)
_REMOVE_RE = re.compile(
    r"\b(remove|drop|take (?:off|out)|delete|without|exclude|no longer|not on|minus)\b", re.I
)


def _merge_set(current: list, said: list, mode: str) -> list:
    if mode == "add":
        return current + [item for item in said if item not in current]
    if mode == "remove":
        return [item for item in current if item not in said]
    return said


def _merge_details(ctx: _Context, state: dict[str, Any], text: str, *, fill_only: bool,
                   include_waypoints: bool, skip: str | None = None) -> dict[str, Any]:
    """Days, time, capture types and stops said anywhere in a message, merged into the plan.

    "add tuesday" adds, "remove tuesday" / "without tuesday" takes away, and a plain "tuesday"
    replaces. fill_only: the message was answering something else, so a plain mention only fills
    a field that is still empty, while "add"/"remove" always apply.
    """
    names = [p.name for p in ctx.points]
    plain = _without_names(text, names)
    mode = "remove" if _REMOVE_RE.search(text) else "add" if _ADD_RE.search(text) else "replace"
    state = {**state}
    is_schedule = state.get("intent") == "create_schedule"

    def apply_set(key: str, said: list) -> None:
        current = list(state.get(key) or [])
        if mode == "replace" and fill_only and current:
            return
        merged = _merge_set(current, said, mode)
        if merged or mode == "remove":
            state[key] = merged

    capture = detect_capture(plain) if skip != "capture" else []
    if capture:
        apply_set("capture", capture)
    if is_schedule:
        reading = read_time(plain, None) if skip != "time" else TimeReading()
        if (reading.value or reading.candidates) and not (fill_only and (state.get("time") or state.get("time_candidates"))):
            state = _apply_time(state, reading)
        # "remove friday" names the day to remove; "every day except friday" is already a full set.
        days = parse_weekdays(re.sub(r"\b(remove|drop|take (?:off|out)|delete|without|exclude|no longer|not on|minus)\b",
                                     " ", text, flags=re.I)) if mode == "remove" else parse_weekdays(text)
        if days and skip != "weekdays":
            current = list(state.get("weekdays") or state.get("weekdays_pending") or [])
            if not (mode == "replace" and fill_only and current):
                state["weekdays"] = sorted(_merge_set(current, days, mode)) if mode != "replace" else days
                state.pop("weekdays_pending", None)
    if include_waypoints:
        named = _names_in_text(text, ctx.points)
        if named:
            current = [entry for entry in state.get("waypoints") or [] if entry.get("id")]
            said = [{"query": point.name, "id": point.id} for point in named]
            current_ids = [entry["id"] for entry in current]
            said_ids = [entry["id"] for entry in said]
            if mode == "add":
                state["waypoints"] = current + [e for e in said if e["id"] not in current_ids]
            elif mode == "remove":
                state["waypoints"] = [e for e in current if e["id"] not in said_ids]
            else:
                state["waypoints"] = said
            state.pop("pending_index", None)
    return state


def _absorb(ctx: _Context, state: dict[str, Any], text: str, skip: str | None = None) -> dict[str, Any]:
    """Pick up other details said in the same breath ("kitchen at 8am on weekdays, photos",
    "use kitchen and add tuesday too"). `skip` is the field the message just answered."""
    return _merge_details(ctx, state, text, fill_only=True, include_waypoints=False, skip=skip)


def _describe_changes(ctx: _Context, before: dict[str, Any], after: dict[str, Any], skip: str | None) -> str:
    """'on Monday and Tuesday, at 19:00': what a message changed besides the question it answered."""
    pieces = []
    if skip != "name" and after.get("name") and after.get("name") != before.get("name"):
        pieces.append(f'named "{after["name"]}"')
    before_ids = [e.get("id") for e in before.get("waypoints") or []]
    after_ids = [e.get("id") for e in after.get("waypoints") or []]
    if skip != "waypoints" and after_ids != before_ids and all(after_ids):
        names = ctx.names(after_ids)
        if after.get("intent") == "go_to":
            pieces.append(f"going to {_join(names)}")
        else:
            pieces.append(f"route {' → '.join(names)}" if names else "no stops yet")
    if skip != "time" and after.get("time") and after.get("time") != before.get("time"):
        pieces.append(f"at {after['time']}")
    if skip != "weekdays" and after.get("weekdays") and after.get("weekdays") != before.get("weekdays"):
        days = describe_days(after["weekdays"])
        pieces.append(days if days == "every day" else f"on {days}")
    if skip != "capture" and after.get("capture") and after.get("capture") != before.get("capture"):
        describe = describe_schedule_capture if after.get("intent") == "create_schedule" else describe_capture
        pieces.append(f"capturing {describe(after['capture'])}")
    return ", ".join(pieces)


SLOT_FIELD = {"name": "name", "waypoints": "waypoints", "waypoint_choice": "waypoints", "waypoint_query": "waypoints",
              "waypoint_confirm": "waypoints", "room_choice": "waypoints", "time": "time", "time_ampm": "time",
              "weekdays": "weekdays", "weekdays_confirm": "weekdays", "capture": "capture"}


def _with_change_note(ctx: _Context, before: dict[str, Any], result: AssistantResult, skip: str | None) -> AssistantResult:
    """Confirm anything a message changed besides the question it answered."""
    after = result.state
    if not after or after.get("intent") != before.get("intent"):
        return result
    changes = _describe_changes(ctx, before, after, skip)
    if changes:
        result.reply = f"Got it, {changes}. {result.reply}"
    return result


def _model_place_names(parsed: _LazyParse) -> list[str]:
    """Waypoint names from the model, minus anything that is really a detail or an action."""
    names = []
    for name in parsed.get("waypoints", []):
        name = str(name).strip()
        if name and not _is_detail(name) and not _ACTION_WORDS_RE.search(name):
            names.append(name)
    return names


def _detect_request(ctx: _Context, text: str) -> str | None:
    """'create_schedule' or 'go_to' when the words plainly ask for it, else None."""
    if _QUESTION_RE.search(text) and not _REQUEST_RE.search(text):
        return None
    if _SCHEDULE_REQUEST_RE.search(text):
        return "create_schedule"
    named = _names_in_text(text, ctx.points)
    rest = _without_names(text, [point.name for point in named])
    # A repeating request about a place or a capture is a schedule, not a one-off trip.
    if _RECURRING_RE.search(rest) and (named or detect_capture(rest) or _GO_VERB_RE.search(rest)):
        return "create_schedule"
    if _NEW_ONE_RE.search(text):
        recent = " ".join(turn.get("text", "") for turn in ctx.history[-4:])
        if re.search(r"\bschedules?\b", recent, re.I):
            return "create_schedule"
    if named and (_GO_VERB_RE.search(rest) or detect_capture(rest)):
        return "go_to"
    return None


_NAMED_RE = re.compile(
    r"\b(?:named|called|name it|call it|titled)\s+[\"'“]?(?P<name>.+?)[\"'”]?"
    r"(?=\s+(?:for|at|on|every|each|daily|with|to|that|which|visiting|going|capturing|and then|then)\b|[,.;!?]|$)",
    re.I,
)


def _said_name(text: str) -> str | None:
    match = _NAMED_RE.search(text)
    return _clean_schedule_name(match.group("name")) if match else None


def _start_intent(ctx: _Context, text: str, parsed: _LazyParse) -> AssistantResult:
    intent = parsed.get("intent", "unknown")
    waypoints = [{"query": name, "id": None} for name in _model_place_names(parsed)]
    named = _names_in_text(text, ctx.points)
    if named and len(named) >= len(waypoints):
        waypoints = [{"query": point.name, "id": point.id} for point in named]
    capture_text = _without_names(text, [entry["query"] for entry in waypoints] + [p.name for p in ctx.points])
    if intent == "go_to":
        state = {"intent": "go_to", "waypoints": waypoints, "capture": detect_capture(capture_text)}
        return _advance(ctx, state)
    if intent == "create_schedule":
        name = _said_name(text) or parsed.get("name")
        state: dict[str, Any] = {
            "intent": "create_schedule",
            "name": _clean_schedule_name(str(name)) if name else None,
            "waypoints": waypoints,
            "capture": detect_capture(capture_text),
        }
        reading = read_time(capture_text, None)
        if reading.value is None and not reading.candidates:
            reading = read_time(capture_text, parsed.get("time"))
        state = _apply_time(state, reading)
        state = _apply_weekdays(state, text, parsed.get("weekdays"))
        return _advance(ctx, state)
    if _LIST_RE.search(text) or (intent == "list" and _PLACES_RE.search(text)):
        if not ctx.points:
            return _done("This project has no waypoints yet. Add them on the Route tab first.")
        # Listing on request is information; it offers to go but doesn't insist.
        return _list_rooms(ctx, {"intent": "go_to", "waypoints": [], "soft": True})
    return _done(_chat(ctx, text) or CAPABILITIES)


def _tab_request(ctx: _Context, text: str) -> str | None:
    tabs, _ = _tab_requests(ctx, text)
    return tabs[-1] if tabs else None


def _tab_word(word: str) -> str | None:
    """The tab a word names, allowing small misspellings ("shecdules", "histroy")."""
    key = re.sub(r"\s+", " ", word.lower()).strip()
    tab = TAB_IDS.get(key) or TAB_IDS.get(key.replace(" ", ""))
    if tab:
        return tab
    close = difflib.get_close_matches(key, list(TAB_IDS), n=1, cutoff=0.78)
    return TAB_IDS[close[0]] if close else None


_BARE_TAB_RE = re.compile(
    r"^\s*(?:please\s+)?(?:the\s+)?(?P<tab>[a-z]+(?:\s+maps?)?)(?:\s+(?:tab|page|view|screen)s?)?(?:\s+please)?\s*[.!]*\s*$",
    re.I,
)


def _tab_requests(ctx: _Context, text: str) -> tuple[list[str], str]:
    """Tabs asked for, in order, and what is left of the message once they are taken out."""
    bare = _BARE_TAB_RE.match(text)
    if bare and not _QUESTION_RE.search(text):
        word = bare.group("tab")
        tab = _tab_word(word)
        # A bare waypoint name ("kitchen") means go there, never a tab.
        if tab and match_points(word, ctx.points)[0] is None and not _names_in_text(text, ctx.points):
            return [tab], ""
    matches = [m for m in _TAB_PHRASE_RE.finditer(text) if (m.group("verb") or m.group("word")) and _tab_word(m.group("tab"))]
    if not any(m.group("verb") for m in matches):
        return [], text
    tabs: list[str] = []
    rest = text
    for match in reversed(matches):
        word = re.sub(r"\s+", " ", match.group("tab").lower())
        # "go to history" is a waypoint when one has that name, unless they said "tab" or "page".
        if not match.group("word") and match_points(word, ctx.points)[0] is not None:
            continue
        tab = _tab_word(word)
        if tab is None:
            continue
        tabs.insert(0, tab)
        rest = f"{rest[: match.start()]} , {rest[match.end():]}"
    if not tabs:
        return [], text
    pieces = [piece.strip() for piece in re.split(r"\s*(?:,|;|\band then\b|\bthen\b|\band\b|\balso\b)\s*", rest, flags=re.I)]
    pieces = [piece for piece in pieces if piece and not _EMPTY_REST_RE.match(piece)]
    remainder = " and ".join(pieces)
    if len(remainder.split()) < 2:
        remainder = ""
    unique: list[str] = []
    for tab in tabs:
        if tab not in unique:
            unique.append(tab)
    return unique, remainder


def _open_and_offer(tabs: list[str]) -> AssistantResult:
    first, rest = tabs[0], tabs[1:]
    reply = f"Opened the {TAB_LABELS[first]} tab."
    if not rest:
        return AssistantResult(reply=reply, state=None, action={"type": "open_tab", "tab": first})
    question = f"Want me to open the {TAB_LABELS[rest[0]]} tab next?"
    return AssistantResult(
        reply=f"{reply} {question}",
        state={"intent": "tabs", "soft": True, "awaiting": "tab_next", "tabs": rest, "question": question,
               "reask": question},
        action={"type": "open_tab", "tab": first},
    )


# --- F. managing schedules ------------------------------------------------------------------------
_MANAGE_VERBS = [
    ("delete", re.compile(r"\b(delete|remove|erase|get rid of|trash|cancel)\b", re.I)),
    ("pause", re.compile(r"\b(pause|disable|deactivate|suspend|stop)\b|\b(turn|switch)\b.{0,40}\boff\b", re.I)),
    ("resume", re.compile(r"\b(resume|enable|re-?enable|activate|unpause|restart)\b|\b(turn|switch)\b.{0,40}\bon\b", re.I)),
    ("run", re.compile(r"\b(run|start|trigger|kick off|launch|execute)\b", re.I)),
]
_SCHEDULE_WORD_RE = re.compile(r"\bschedules?\b", re.I)
_PRONOUN_RE = re.compile(r"\b(it|that|this|that one|this one|the last one|the one (i|we) just made)\b", re.I)


def _words(text: str) -> list[str]:
    lowered = text.lower()
    for word, digit in _NUMBER_WORDS.items():
        lowered = re.sub(rf"\b{word}\b", digit, lowered)
    return re.findall(r"[a-z0-9]+", lowered)


def _name_in(text: str, name: str) -> bool:
    """Whether a schedule name appears as whole words ("delete the demo 1 schedule" has "Demo1")."""
    target = normalize_name(name)
    if not target:
        return False
    words = _words(text)
    for start in range(len(words)):
        joined = ""
        for word in words[start:start + 6]:
            joined += word
            if joined == target:
                return True
            if len(joined) >= len(target):
                break
    return False


def _schedules(ctx: _Context) -> list[RobotMissionSchedule]:
    return list(ctx.db.scalars(
        select(RobotMissionSchedule)
        .where(RobotMissionSchedule.project_id == ctx.project.id)
        .order_by(RobotMissionSchedule.updated_at.desc())
    ).all())


def _find_schedule(ctx: _Context, state: dict[str, Any] | None, text: str, said: str | None = None):
    """The schedule a message is about: named, or "it" meaning the one just worked on.
    Returns a schedule, a list of candidates when it isn't clear which, or None when there are none."""
    schedules = _schedules(ctx)
    if not schedules:
        return None
    for candidate in [said or ""] + [text]:
        key = normalize_name(candidate)
        if not key:
            continue
        exact = [s for s in schedules if _name_in(candidate, s.name)]
        if len(exact) == 1:
            return exact[0]
        if said and candidate == said:
            close = difflib.get_close_matches(key, [normalize_name(s.name) for s in schedules], n=1, cutoff=0.75)
            if close:
                return next(s for s in schedules if normalize_name(s.name) == close[0])
    if state and state.get("schedule_id"):
        return ctx.db.get(RobotMissionSchedule, state["schedule_id"])
    if _PRONOUN_RE.search(text) or len(schedules) == 1:
        return schedules[0]  # most recently created or changed
    return schedules


def _schedule_request(ctx: _Context, state: dict[str, Any] | None, text: str):
    """(action, target) when a message plainly manages a schedule, else None."""
    if _QUESTION_RE.search(text) and not _REQUEST_RE.search(text):
        return None
    mentions_name = any(_name_in(text, s.name) for s in _schedules(ctx))
    recent = " ".join(turn.get("text", "") for turn in ctx.history[-4:])
    about_schedule = (
        _SCHEDULE_WORD_RE.search(text)
        or mentions_name
        or (_PRONOUN_RE.search(text) and (_SCHEDULE_WORD_RE.search(recent) or (state and state.get("schedule_id"))))
    )
    if not about_schedule or _SCHEDULE_REQUEST_RE.search(text) and not re.search(r"\b(delete|remove|pause|resume|run|disable|enable)\b", text, re.I):
        return None
    for action, pattern in _MANAGE_VERBS:
        if pattern.search(text):
            if action == "run" and not re.search(r"\b(now|right away|immediately|once)\b|\brun (it|that|the)\b", text, re.I):
                continue
            return action, _find_schedule(ctx, state, text)
    return None


def _manage_schedule(ctx: _Context, state: dict[str, Any] | None, action: str, target) -> AssistantResult:
    if target is None:
        return _done("There are no schedules in this project yet.")
    if isinstance(target, list):
        names = [s.name for s in target]
        question = f"Which schedule? You have {_join(names[:ctx.list_limit], 'and')}."
        return AssistantResult(
            reply=question,
            state={"intent": "manage_schedule", "manage": action, "awaiting": "schedule_choice",
                   "options": [s.id for s in target], "question": question, "reask": "Which schedule?"},
        )
    name = target.name
    if action == "delete":
        question = f'Delete the schedule "{name}"? It can\'t be undone.'
        return AssistantResult(
            reply=question,
            state={"intent": "manage_schedule", "manage": "delete", "awaiting": "confirm_delete",
                   "schedule_id": target.id, "schedule_name": name, "question": question,
                   "reask": f'Should I delete "{name}"?'},
        )
    if action in ("pause", "resume"):
        enable = action == "resume"
        if target.enabled == enable:
            return _done(f'"{name}" is already {"on" if enable else "paused"}.')
        update_robot_mission_schedule(
            target.id, RobotMissionScheduleUpdateRequest(enabled=enable), current_user=ctx.user, db=ctx.db
        )
        reply = (f'Turned on "{name}". It runs {describe_when(target.local_time, list(target.weekdays_json or []))}.'
                 if enable else f'Paused "{name}". It won\'t run until you resume it.')
        return _done(reply, {"type": "schedule_updated", "schedule_id": target.id})
    if action == "run":
        if _active_mission(ctx) is not None:
            return _done("The robot is busy with another capture. Say stop to cancel it first.")
        mission = run_robot_mission_schedule_now(target.id, current_user=ctx.user, db=ctx.db)
        route = " → ".join(step.waypoint_name for step in sorted(mission.steps, key=lambda step: step.sequence_index))
        return _done(f'Running "{name}" now: {route}.', {"type": "mission_started", "mission_id": mission.id})
    return _done(CAPABILITIES)


def _connect(ctx: _Context) -> AssistantResult:
    if ctx.robot_connected:
        return _done("The robot is already connected.")
    create_robot_command(
        RobotCommandCreateRequest(robot_id=ctx.robot_id, kind="connect"),
        current_user=ctx.user,
        db=ctx.db,
    )
    return _done("Connecting the robot. It takes a moment to come online.", {"type": "robot_connecting"})


def _disconnect(ctx: _Context) -> AssistantResult:
    if not ctx.robot_connected:
        return _done("The robot is already disconnected.")
    if _active_mission(ctx) is not None:
        return _done("The robot is busy with a capture. Say stop first, then I can disconnect it.")
    create_robot_command(
        RobotCommandCreateRequest(robot_id=ctx.robot_id, kind="disconnect"),
        current_user=ctx.user,
        db=ctx.db,
    )
    return _done("Disconnecting the robot.", {"type": "robot_disconnecting"})


def _connection_request(text: str, parsed: "_LazyParse | None" = None) -> str | None:
    """'connect' or 'disconnect' when the person asks for it. Questions ("is it connected?") are not requests."""
    if not _QUESTION_RE.search(text) or _REQUEST_RE.search(text):
        if _DISCONNECT_RE.search(text):
            return "disconnect"
        if _CONNECT_RE.search(text):
            return "connect"
    if parsed is not None and parsed.get("intent") in ("connect", "disconnect"):
        return parsed.get("intent")
    return None


def _stop(ctx: _Context, state: dict[str, Any] | None = None) -> AssistantResult:
    mission = _active_mission(ctx)
    if mission is None:
        # Nothing to stop on the robot: "stop" ends whatever we were in the middle of.
        return _done("OK, cancelled." if state else "The robot isn't running a capture right now.")
    if mission.status in ("queued", "dispatched", "running"):
        queued = mission.status == "queued"
        cancel_robot_mission(mission.id, current_user=ctx.user, db=ctx.db)
        reply = (
            "Cancelled the queued capture."
            if queued
            else "Stopping. The robot will cancel the capture and return to its start."
        )
        return _done(reply, {"type": "mission_cancelled", "mission_id": mission.id})
    if mission.status == "returning_to_start":
        stop_robot_mission_return(mission.id, current_user=ctx.user, db=ctx.db)
        return _done(
            "Stopping the return. The robot will stay where it is.",
            {"type": "mission_stopped", "mission_id": mission.id},
        )
    return _done("The robot is already stopping.")


def _message_kind(text: str, awaiting: str | None) -> tuple[str, str]:
    """Classify a message sent while a question is pending: ('options' | 'question' | 'answer', text).

    Only an 'answer' may fill the pending field. A suggestion ("how about 8am") is an answer with
    the lead-in removed; a polite request ("can you do kitchen and lobby") is an answer too.
    """
    stripped = text.rstrip()
    is_question = stripped.endswith("?") or bool(_QUESTION_RE.search(text))
    if _GREETING_RE.search(text):
        return "question", text
    # A bare "ok"/"yes" is an answer only to a yes/no question (or the capture offer).
    if _ACK_RE.search(text) and awaiting in ("name", "waypoints", "time", "weekdays", "room_choice", "waypoint_query"):
        return "options", text
    if awaiting == "name":
        # Almost anything can be a name; only clear questions and requests for help are not.
        if _HELP_RE.search(text) or (is_question and _OPTIONS_RE.search(text)):
            return "options", text
        return ("question", text) if is_question and not _REQUEST_RE.search(text) else ("answer", text)
    suggestion = _SUGGEST_RE.sub("", text, count=1)
    if suggestion != text and suggestion.strip():
        return "answer", suggestion
    if _HELP_RE.search(text) or _OPTIONS_RE.search(text):
        return "options", text
    if _REQUEST_RE.search(text):
        return "answer", text
    if is_question:
        return "question", text
    return "answer", text


def _is_detail(part: str) -> bool:
    """A piece of an answer that is a time, days or a capture type rather than a place."""
    reading = read_time(part, None)
    return bool(
        detect_capture(part, allow_both=True)
        or parse_weekdays(part)
        or reading.value
        or reading.candidates
        or re.fullmatch(r"\s*(please|thanks|thank you|ok|okay)?\s*", part, re.I)
    )


def _fill_waypoints(ctx: _Context, state: dict[str, Any], text: str, parsed: _LazyParse) -> AssistantResult | None:
    awaiting = state.get("awaiting")
    options = [p for pid in state.get("options") or [] if (p := ctx.point(pid)) is not None]

    if awaiting == "room_choice":
        slug = _pick_room(text, ctx, list(state.get("options") or []))
        if slug is not None:
            return _list_room_points(ctx, state, slug)

    if awaiting == "waypoint_confirm":
        answer = yes_or_no(text)
        if answer == "yes" and options:
            return _advance(ctx, _absorb(ctx, _with_points(state, options[:1]), text))
        if answer == "no":
            return _ask(state, "waypoint_query", "OK. Which waypoint do you mean?", reask=WHICH_WAYPOINT)

    if state.get("soft") and awaiting == "waypoint_choice" and not _names_in_text(text, ctx.points):
        answer = yes_or_no(text)
        if answer == "yes":
            return _ask({**state, "soft": False}, "waypoint_choice", "Which one?", reask=GO_WHERE,
                        options=state.get("options"))
        if answer == "no":
            return _done("OK, no problem.")

    # "all of them" for a route means every waypoint, in the listed order.
    if awaiting == "waypoints" and state.get("intent") == "create_schedule" and re.match(
        r"^\s*(all|all of them|every one|every waypoint|everything|all waypoints|all the waypoints)\b", text, re.I
    ):
        return _advance(ctx, _absorb(ctx, _with_points(state, ctx.points), text))

    # Waypoints named word for word. Each piece of the answer is checked: exact names are used,
    # details (time, days, capture type) are picked up separately, and an unknown name next to
    # them ("kitchen and the gym") is kept in its place and asked about, never dropped.
    named = _names_in_text(text, ctx.points)
    if named:
        entries: list[dict[str, Any]] = []
        for part in _split_names(text):
            inside = _names_in_text(part, ctx.points)
            if inside:
                entries += [{"query": point.name, "id": point.id} for point in inside]
            elif not _is_detail(part) and len(part.split()) <= 4:
                entries.append({"query": part, "id": None})
        if entries:
            rest = _without_names(text, [point.name for point in named])
            return _advance(ctx, _absorb(ctx, _replace_pending(state, entries), rest))

    if awaiting == "waypoint_choice" and options:
        picked = _pick_option(text, options)
        if picked is not None:
            return _advance(ctx, _absorb(ctx, _with_points(state, [picked]), text))

    # A room name narrows the list to that room.
    if awaiting in ("waypoint_choice", "waypoint_query", "waypoints") and not named:
        slug = _pick_room(text, ctx, [s for s in ctx.rooms() if s])
        if slug is not None:
            return _list_room_points(ctx, state, slug)

    # Names that aren't exact: take them from the model, or from the words when it is unreachable.
    if parsed.unreachable:
        names = [part for part in said if len(part.split()) <= 4]
    else:
        names = _model_place_names(parsed)
    if not names:
        return None
    if awaiting == "waypoints" or len(names) >= 2:
        return _advance(ctx, _with_queries(state, names))
    return _advance(ctx, _with_queries(state, names[:1]))


def _fill_slot(ctx: _Context, state: dict[str, Any], text: str, parsed: _LazyParse) -> AssistantResult | None:
    """Use the message as the answer to the pending question, or return None if it isn't one."""
    awaiting = state.get("awaiting")

    if awaiting == "enable":
        answer = yes_or_no(text, parsed)
        if answer == "yes":
            update_robot_mission_schedule(
                state["schedule_id"],
                RobotMissionScheduleUpdateRequest(enabled=True),
                current_user=ctx.user,
                db=ctx.db,
            )
            return _done(
                f'Done, "{state.get("schedule_name")}" is enabled.',
                {"type": "schedule_enabled", "schedule_id": state["schedule_id"]},
            )
        if answer == "no":
            return _done("OK, it's saved but switched off. You can enable it later from the Schedules tab.")
        return None

    if awaiting == "tab_next":
        answer = yes_or_no(text)
        if answer == "yes":
            return _open_and_offer(list(state.get("tabs") or []))
        if answer == "no":
            return _done("OK.")
        return None

    if awaiting == "confirm_delete":
        answer = yes_or_no(text, parsed)
        if answer == "yes":
            delete_robot_mission_schedule(state["schedule_id"], current_user=ctx.user, db=ctx.db)
            return _done(f'Deleted "{state.get("schedule_name")}".',
                         {"type": "schedule_deleted", "schedule_id": state["schedule_id"]})
        if answer == "no":
            return _done(f'OK, I kept "{state.get("schedule_name")}".')
        return None

    if awaiting == "schedule_choice":
        options = [s for s in _schedules(ctx) if s.id in (state.get("options") or [])]
        key = normalize_name(_clean_query(text))
        picked = [s for s in options if key and (normalize_name(s.name) == key or _name_in(text, s.name))]
        if not picked and key:
            close = difflib.get_close_matches(key, [normalize_name(s.name) for s in options], n=1, cutoff=0.7)
            picked = [s for s in options if close and normalize_name(s.name) == close[0]]
        if picked:
            return _manage_schedule(ctx, None, state.get("manage"), picked[0])
        return None

    if awaiting == "connect":
        answer = yes_or_no(text, parsed)
        if answer == "yes":
            create_robot_command(
                RobotCommandCreateRequest(robot_id=ctx.robot_id, kind="connect"),
                current_user=ctx.user,
                db=ctx.db,
            )
            names = ctx.names([entry["id"] for entry in state["waypoints"]])
            return AssistantResult(
                reply=f"Connecting the robot. I'll go to {_join(names)} as soon as it's connected.",
                state={**state, "awaiting": "connect_wait", "question": None, "reask": None},
                action={"type": "robot_connecting"},
            )
        if answer == "no":
            return _done("OK, I won't start. Connect the robot and ask me again.")
        return None

    if awaiting == "capture":
        capture = detect_capture(_without_names(text, [p.name for p in ctx.points]), allow_both=True)
        if capture:
            return _advance(ctx, _absorb(ctx, {**state, "capture": capture}, text, skip="capture"))
        answer = yes_or_no(text)
        if answer == "yes":
            return _slot_help(ctx, state)
        if answer == "no":
            return AssistantResult(reply=f"OK. {state.get('reask')}", state=state)
        return None

    if awaiting == "time_ampm":
        picked = pick_time_candidate(text, state.get("time_candidates") or [])
        if picked is None:
            reading = read_time(text, None, expect_time=True)
            picked = reading.value
        if picked:
            next_state = {**state, "time": picked}
            next_state.pop("time_candidates", None)
            return _advance(ctx, _absorb(ctx, next_state, text, skip="time"))
        return None

    if awaiting == "time":
        reading = read_time(text, None, expect_time=True)
        if reading.value is None and not reading.candidates and not parsed.unreachable:
            reading = read_time(text, parsed.get("time"), expect_time=True)
        if reading.value or reading.candidates:
            return _advance(ctx, _absorb(ctx, _apply_time(state, reading), text, skip="time"))
        return None

    if awaiting == "weekdays":
        if re.fullmatch(r"\s*(all|all of them|all days|every one|the whole week)\s*[.!]*\s*", text, re.I):
            return _advance(ctx, {**state, "weekdays": list(range(7))})
        if parse_weekdays(text):
            return _advance(ctx, _absorb(ctx, _apply_weekdays(state, text, parse_weekdays(text)), text, skip="weekdays"))
        if parsed.unreachable:
            return None
        next_state = _apply_weekdays(state, text, parsed.get("weekdays"))
        if next_state.get("weekdays") or next_state.get("weekdays_pending"):
            return _advance(ctx, next_state)
        return None

    if awaiting == "weekdays_confirm":
        answer = yes_or_no(text)
        if parse_weekdays(text):
            return _advance(ctx, _apply_weekdays(state, text, parse_weekdays(text)))
        if answer == "yes":
            next_state = {**state, "weekdays": state["weekdays_pending"]}
            next_state.pop("weekdays_pending", None)
            return _advance(ctx, next_state)
        if answer == "no":
            next_state = {**state}
            next_state.pop("weekdays_pending", None)
            return _ask(next_state, "weekdays", "Which days should it run?", prefix="OK. ")
        return None

    if awaiting == "name":
        name = _clean_schedule_name(text)
        return _advance(ctx, {**state, "name": name}) if name else None

    if awaiting in WAYPOINT_SLOTS:
        return _fill_waypoints(ctx, state, text, parsed)
    return None


def _apply_correction(ctx: _Context, state: dict[str, Any], text: str) -> AssistantResult | None:
    """'actually make it 9am', 'the lobby instead', 'call it Night run': change an earlier answer."""
    intent = state.get("intent")
    if intent not in ("go_to", "create_schedule"):
        return None
    # A time, days or capture type is unmistakable, so it is applied even without "actually";
    # switching places or renaming needs the person to say it is a change.
    marked = bool(_CORRECTION_RE.search(text) or _ADD_RE.search(text) or _REMOVE_RE.search(text))
    new = _merge_details(ctx, state, text, fill_only=False, include_waypoints=marked)
    if intent == "create_schedule" and marked:
        renamed = re.search(r"\b(?:call it|name it|rename it(?: to)?)\s+(.+)$", text, re.I)
        if renamed:
            new["name"] = _clean_schedule_name(renamed.group(1))
    if new == state:
        # Something recognisable that is already part of the plan: say so instead of ignoring it.
        plain = _without_names(text, [p.name for p in ctx.points])
        if (intent == "create_schedule" and parse_weekdays(text)) or detect_capture(plain) or (
            marked and _names_in_text(text, ctx.points)
        ):
            reask = state.get("reask") or state.get("question") or ""
            return AssistantResult(reply=f"That's already included. {reask}".strip(), state=state)
        return None
    if intent == "create_schedule" and new.get("waypoints") == [] and state.get("waypoints"):
        new.pop("waypoints")  # removing every stop just asks for them again
    result = _advance(ctx, new)
    if result.state and result.state.get("intent") == intent:
        changes = _describe_changes(ctx, state, result.state, None)
        result.reply = f"Got it, {changes}. {result.reply}" if changes else f"Got it. {result.reply}"
    else:
        result.reply = f"Got it. {result.reply}"
    return result


def _answer(ctx: _Context, state: dict[str, Any], text: str, parsed: _LazyParse) -> AssistantResult:
    awaiting = state.get("awaiting")

    if awaiting == "connect_wait":
        return AssistantResult(reply="Still connecting the robot. I'll start as soon as it's connected.", state=state)

    kind, body = _message_kind(text, awaiting)
    if kind == "options":
        return _slot_help(ctx, state)
    if kind == "question" and _STATUS_RE.search(text):
        if state.get("soft"):
            return _done(_status_reply(ctx))
        return AssistantResult(reply=f"{_status_reply(ctx)} {state.get('reask') or state.get('question')}", state=state)
    if kind == "question":
        return _reask(ctx, state, text)

    filled = _fill_slot(ctx, state, body, parsed)
    if filled is not None:
        return _with_change_note(ctx, state, filled, SLOT_FIELD.get(awaiting))
    corrected = _apply_correction(ctx, state, text)
    if corrected is not None:
        return corrected

    request = _detect_request(ctx, text)
    if request is not None and (request != state.get("intent") or awaiting == "enable" or state.get("soft")
                                or request == "go_to"):
        parsed.intent_override = request
        return _start_intent(ctx, text, parsed)
    wanted = _connection_request(text)
    if wanted is not None and awaiting not in ("connect", "connect_wait"):
        return _disconnect(ctx) if wanted == "disconnect" else _connect(ctx)
    if parsed.unreachable:
        return _not_understood(ctx, state)
    intent = parsed.get("intent", "unknown")
    if intent == "list":
        return _slot_help(ctx, state)
    if state.get("soft"):
        return _done(_chat(ctx, text) or CAPABILITIES)
    if intent == "answer":
        return _not_understood(ctx, state)
    return _reask(ctx, state, text)


def handle_message(
    *,
    db: Session,
    user: User,
    project_slug: str,
    robot_id: str,
    message: str,
    state: dict[str, Any] | None,
    timezone: str,
    robot_connected: bool,
    voice: bool,
    event: str | None = None,
    history: list[dict[str, str]] | None = None,
    parser: Parser | None = None,
    chatter: Chatter | None = None,
    mission_id: str | None = None,
) -> AssistantResult:
    result = _handle_message(
        db=db, user=user, project_slug=project_slug, robot_id=robot_id, message=message, state=state,
        timezone=timezone, robot_connected=robot_connected, voice=voice, event=event, history=history,
        parser=parser, chatter=chatter, mission_id=mission_id,
    )
    result.draft = draft_of(result.state)
    return result


def draft_of(state: dict[str, Any] | None) -> dict[str, Any] | None:
    """The route or schedule being built, for the page to show filling in. None when nothing is."""
    if not state or state.get("soft") or state.get("intent") not in ("go_to", "create_schedule"):
        return None
    point_ids = [entry["id"] for entry in state.get("waypoints") or [] if entry.get("id")]
    if state["intent"] == "go_to":
        return {"kind": "route", "point_ids": point_ids, "capture": state.get("capture") or [],
                "focus": state.get("awaiting")}
    return {
        "kind": "schedule",
        "name": state.get("name") or "",
        "point_ids": point_ids,
        "time": state.get("time"),
        "weekdays": state.get("weekdays") or [],
        "capture": state.get("capture") or [],
        "focus": state.get("awaiting"),
    }


_CONNECTOR_RE = re.compile(r"^\s*(?:(?:and|then|also|after that|and then|&|plus)\b|[,;])\s*", re.I)
_EMPTY_REST_RE = re.compile(r"^\s*(?:please|for me|thanks|thank you|now|tab|page|the|it|up|a)?\s*[.!?]*\s*$", re.I)


def _remainder(text: str, match: re.Match | None) -> str:
    """What is left of a message once one request is taken out, or "" when nothing meaningful is."""
    if match is None:
        return ""
    rest = f"{text[: match.start()]} {text[match.end():]}".strip(" ,.;")
    previous = None
    while rest != previous:
        previous = rest
        rest = _CONNECTOR_RE.sub("", rest).strip(" ,.;")
    rest = re.sub(r"^\s*(?:please|can you|could you)\s+(?=\w)", "", rest, flags=re.I).strip()
    if _EMPTY_REST_RE.match(rest) or len(rest.split()) < 2:
        return ""
    return rest


def _handle_message(
    *,
    db: Session,
    user: User,
    project_slug: str,
    robot_id: str,
    message: str,
    state: dict[str, Any] | None,
    timezone: str,
    robot_connected: bool,
    voice: bool,
    event: str | None,
    history: list[dict[str, str]] | None,
    parser: Parser | None,
    chatter: Chatter | None,
    mission_id: str | None,
    connect_ok: bool = False,
) -> AssistantResult:
    call = dict(db=db, user=user, project_slug=project_slug, robot_id=robot_id, message=message, state=state,
                timezone=timezone, robot_connected=robot_connected, voice=voice, event=event, history=history,
                parser=parser, chatter=chatter, mission_id=mission_id, connect_ok=connect_ok)
    project = db.scalar(select(Project).where(Project.slug == project_slug))
    if project is None:
        return _done("Pick a project first.")
    points = list(
        db.scalars(
            select(RobotCapturePoint)
            .where(RobotCapturePoint.project_id == project.id)
            .order_by(RobotCapturePoint.name.asc())
        ).all()
    )
    rooms = db.scalars(select(Room).where(Room.project_id == project.id)).all()
    ctx = _Context(
        db=db,
        user=user,
        project=project,
        robot_id=robot_id,
        timezone=timezone or "UTC",
        robot_connected=robot_connected,
        voice=voice,
        points=points,
        room_names={room.slug: room.name for room in rooms if room.slug},
        history=list(history or []),
        chatter=chatter or chat_with_llm,
        connect_ok=connect_ok,
    )
    state = dict(state) if state else None

    try:
        if event == "robot_connected":
            if state and state.get("awaiting") == "connect_wait":
                ctx.robot_connected = True
                result = _advance(ctx, {**state, "awaiting": None})
                result.reply = "The robot is connected. " + result.reply
                return result
            return AssistantResult(reply="The robot is connected now.", state=state)
        if event == "robot_disconnected":
            return AssistantResult(reply="The robot is disconnected now.", state=state)
        if event == "robot_connect_failed":
            detail = f": {message.strip()}" if message.strip() else "."
            reply = f"The robot couldn't connect{detail}"
            if state and state.get("awaiting") == "connect_wait":
                return _done(f"{reply} I didn't start the capture.")
            return AssistantResult(reply=reply, state=state)
        if event == "mission_finished":
            mission = db.get(RobotMission, mission_id) if mission_id else None
            if mission is None:
                return AssistantResult(reply="", state=state)
            return AssistantResult(reply=describe_finished(ctx, mission), state=state)

        # Stray punctuation inside words ("del;ete") is a typo, not meaning.
        text = re.sub(r"(?<=\w)[;:`]+(?=\w)", "", message.strip())
        if not text:
            return AssistantResult(reply="I didn't hear anything.", state=state)

        awaiting = state.get("awaiting") if state else None
        # Managing a schedule comes first: "stop the demo schedule" pauses it, it doesn't stop the robot.
        if awaiting not in ("name", "confirm_delete", "schedule_choice"):
            managed = _schedule_request(ctx, state, text)
            if managed is not None:
                return _manage_schedule(ctx, state, *managed)
        # Stop never waits on the model. A schedule name is the one place the word is just a word.
        if awaiting != "name" and _STOP_RE.search(text) and not _QUESTION_RE.search(text):
            return _stop(ctx, state)
        if _CANCEL_RE.search(text):
            return _done("OK, cancelled.") if state else _stop(ctx)

        tabs, rest = _tab_requests(ctx, text)
        if tabs and not rest and len(tabs) > 1 and (not state or state.get("soft")):
            # Only one tab shows at a time: open the first and offer the next.
            return _open_and_offer(tabs)
        if tabs and state and state.get("soft"):
            state = None  # a new tab request ends an open offer ("want me to open X next?")
        if tabs:
            tab = tabs[-1]  # only one tab shows at a time: the last one asked for stays open
            opened = "Opened the " + ", then the ".join(f"{TAB_LABELS[t]} tab" for t in tabs) + "."
            # "open schedules and create a schedule named demo1": the rest is its own request.
            if rest:
                inner = _handle_message(**{**call, "message": rest})
                return AssistantResult(reply=f"{opened} {inner.reply}".strip(), state=inner.state,
                                       action=inner.action or {"type": "open_tab", "tab": tab})
            reply = opened
            if state and (state.get("reask") or state.get("question")):
                reply = f"{reply} {state.get('reask') or state['question']}"
            return AssistantResult(reply=reply, state=state, action={"type": "open_tab", "tab": tab})

        parsed = _LazyParse(parser or parse_with_llm, text, (state.get("reask") or state.get("question")) if state else None)
        if awaiting:
            return _answer(ctx, state, text, parsed)

        if _CAPABILITY_RE.search(text):
            return _done(CAPABILITIES_FULL)
        if _STATUS_RE.search(text):
            return _done(_status_reply(ctx))

        # Plain "connect"/"disconnect" requests need no model; questions about it go to the model.
        wanted = _connection_request(text)
        if wanted is not None:
            pattern = _DISCONNECT_RE if wanted == "disconnect" else _CONNECT_RE
            rest = _remainder(text, pattern.search(text))
            if rest and wanted == "connect" and _detect_request(ctx, rest) == "go_to":
                # "connect the robot and go to the lobby": the trip connects first without asking.
                return _handle_message(**{**call, "message": rest, "connect_ok": True})
            result = _disconnect(ctx) if wanted == "disconnect" else _connect(ctx)
            if rest and _detect_request(ctx, rest):
                inner = _handle_message(**{**call, "message": rest})
                return AssistantResult(reply=f"{result.reply} {inner.reply}".strip(), state=inner.state,
                                       action=result.action or inner.action)
            return result

        request = _detect_request(ctx, text)

        # A waypoint name on its own ("the kitchen please") means go there.
        named = _names_in_text(text, ctx.points)
        if (request in (None, "go_to") and len(named) == 1 and len(_split_names(text)) == 1
                and not _QUESTION_RE.search(text) and len(text.split()) <= 5):
            return _advance(ctx, {"intent": "go_to", "waypoints": [{"query": named[0].name, "id": named[0].id}],
                                  "capture": detect_capture(_without_names(text, [named[0].name]))})

        if request is not None:
            parsed.intent_override = request
            if parsed.unreachable and request == "create_schedule":
                return _start_intent(ctx, text, parsed)
        if parsed.unreachable and request is None:
            return _done(
                "Sorry, I can't understand requests right now because the language model "
                "isn't reachable. You can still say stop."
            )
        if parsed.get("intent") == "stop" and not _QUESTION_RE.search(text) and _STOP_WORDS_RE.search(text):
            return _stop(ctx)
        if parsed.get("intent") == "open_tab" and parsed.get("tab") in TAB_LABELS:
            return AssistantResult(reply=f"Opened the {TAB_LABELS[parsed.get('tab')]} tab.", state=None,
                                   action={"type": "open_tab", "tab": parsed.get("tab")})
        model_manage = {"delete_schedule": "delete", "pause_schedule": "pause", "resume_schedule": "resume",
                        "run_schedule": "run"}.get(parsed.get("intent"))
        if model_manage:
            return _manage_schedule(ctx, state, model_manage, _find_schedule(ctx, state, text, parsed.get("schedule_name")))
        wanted = _connection_request(text, parsed)
        if wanted is not None:
            return _disconnect(ctx) if wanted == "disconnect" else _connect(ctx)
        if not ctx.points and parsed.get("intent") in ("go_to", "create_schedule", "list"):
            return _done("This project has no waypoints yet. Add them on the Route tab first.")
        return _start_intent(ctx, text, parsed)
    except HTTPException as exc:
        db.rollback()
        return _done(f"I couldn't do that: {exc.detail}")
