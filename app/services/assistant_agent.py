"""The language model in charge of the assistant's conversation (prototype).

The rule-based assistant in robot_assistant.py only understands the phrasings it was written for.
Here a larger model talks with the person and acts through tools, so it can follow corrections,
misheard words and answers given out of order. The code still decides what is real:

- Every tool checks what it's given (waypoints exist and are on the current map, capture types are
  available on the robot right now, a schedule exists, a time is clear from the person's own
  words) and hands back problems instead of acting on bad values.
- Actions go through the existing coded paths and their checks: trips through the same questions
  and endpoint as before ("Should I connect it?", a busy robot), schedule changes through the
  coded "Delete ...? It can't be undone." confirmation, and everything through the same endpoints
  and permission checks as the robots page.
- A new schedule is read back from the draft itself before it is created, and only an unchanged,
  read-back draft with an answer that isn't "no" can be created.
- Stop, a plain "cancel" and the coded yes/no questions never wait for the model.

ASSISTANT_AGENT picks how much the model runs: "off" (the rule-based assistant), "schedules"
(only creating a schedule) or "all". When the model can't be reached, the rules carry on.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from typing import Any, Callable

import httpx

from app.api.robot_missions import create_robot_mission_schedule
from app.config import get_settings
from app.schemas import RobotMissionScheduleCreateRequest
from app.services import robot_assistant as ra
from app.services import robot_maps

logger = logging.getLogger(__name__)

INTENT = "agent"
MODES = ("off", "schedules", "all")
MAX_MODEL_CALLS = 5
MAX_TURNS = 12
FIELDS = ("name", "waypoints", "time", "weekdays", "capture")
QUESTIONS = {
    "name": "What should I call the schedule?",
    "waypoints": "Which waypoints should it visit, in order?",
    "time": "What time should it run?",
    "weekdays": "Which days should it run?",
    "capture": "What should it capture at each stop?",
}
QUESTION_AFTER_READ_BACK = "Shall I create it and switch it on?"
CAPTURE_PROBLEM = "The robot can't capture a"
NOTHING_DONE = "I haven't done anything yet."
NUDGE = ("You returned nothing. Respond to the user's last message: call a tool if they asked for "
         "something or gave an answer (a misheard phrase may still contain one), otherwise reply to them.")
# Besides robot_assistant's checks: "your schedule is created", "it's now switched on".
_DONE_CLAIM_RE = re.compile(
    r"\b(is|was|it'?s) (now|all) (created|set up|switched on|turned on|enabled)\b|\b(it'?s|is) (been )?(created|set up)\b",
    re.I,
)

_REAL_ACTION_CLAIM_RE = re.compile(
    r"\b(I'?ve|I have|I just|has been|have been)\s+(now\s+|just\s+|already\s+)?(created|scheduled|sent|started|"
    r"connected|disconnected|stopped|dispatched|enabled|switched on|deleted|paused|resumed)\b",
    re.I,
)

# One model call: (messages, tools) -> the model's message ({"content", "tool_calls"}), or None when unreachable.
AgentModel = Callable[[list[dict[str, Any]], list[dict[str, Any]]], dict[str, Any] | None]


def _tool(name: str, description: str, properties: dict[str, Any] | None = None,
          required: list[str] | None = None) -> dict[str, Any]:
    parameters: dict[str, Any] = {"type": "object", "properties": properties or {}}
    if required:
        parameters["required"] = required
    return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}


_CAPTURE_PARAM = {
    "type": "array", "items": {"type": "string", "enum": ["image", "pointcloud"]},
    "description": "image (360° image), pointcloud, or both.",
}
SCHEDULE_TOOLS = [
    _tool("set_fields",
          "Set or change fields of the schedule being drafted. Pass only the fields the user gave or changed. "
          "Lists replace the current value, so to add or remove a day pass the full new list.",
          {
              "name": {"type": "string", "description": "The schedule's name."},
              "waypoints": {"type": "array", "items": {"type": "string"},
                            "description": "Waypoint names in visiting order, as listed in the facts."},
              "time": {"type": "string", "description": "Time of day, 24-hour HH:MM."},
              "weekdays": {"type": "array", "items": {"type": "string"},
                           "description": "English day names, e.g. [\"Monday\", \"Friday\"]."},
              "capture": {**_CAPTURE_PARAM, "description": "What to capture at each stop: image, pointcloud, or both."},
          }),
    _tool("propose_schedule",
          "When every field of the draft is filled: read the finished schedule back to the user and ask to create it."),
    _tool("create_schedule",
          "Create the drafted schedule. Only after propose_schedule, when the user agreed to that exact schedule.",
          {"enable": {"type": "boolean",
                      "description": "true to switch it on (the default), false if the user wants it kept off."}},
          ["enable"]),
    _tool("discard_draft", "The user no longer wants the schedule being drafted."),
]
ROBOT_TOOLS = [
    _tool("start_trip",
          "Send the robot now to one or more waypoints, capturing at each. The app asks about anything missing, "
          "about connecting the robot, and refuses when the robot is busy.",
          {"waypoints": {"type": "array", "items": {"type": "string"},
                         "description": "Waypoint names in visiting order, as listed in the facts."},
           "capture": {**_CAPTURE_PARAM, "description": "Only if the user said what to capture."}},
          ["waypoints"]),
    _tool("manage_schedule",
          "Delete, pause, resume or run now a saved schedule. Deleting is confirmed with the user by the app.",
          {"action": {"type": "string", "enum": ["delete", "pause", "resume", "run_now"]},
           "schedule": {"type": "string", "description": "The schedule's name, as listed in the facts."}},
          ["action", "schedule"]),
    _tool("connect_robot", "Connect the robot (bring it online)."),
    _tool("disconnect_robot", "Disconnect the robot (take it offline), or stop it connecting."),
    _tool("stop_robot", "Stop the capture the robot is running, or stop it returning."),
    _tool("open_tab", "Show one of the robots page's tabs.",
          {"tab": {"type": "string", "enum": list(ra.TAB_LABELS)}}, ["tab"]),
]

SCHEDULE_RULES = """Creating a schedule (a name, waypoints visited in order, a time of day, the days of the week it runs, and what to capture at each stop):
- Whenever the user gives or changes anything, call set_fields with just those fields, all of them in one call: check the message for each of name, waypoints, time, days and capture. Work out the full new value yourself: "add Friday" to Monday and Tuesday is ["Monday", "Tuesday", "Friday"].
- set_fields tells you what is still missing and any problems. If there is a problem, say what it is in a few words ("There's no waypoint called roof terrace.") and ask about it. Otherwise ask for the next missing field in one short, natural question. Don't list every field at once.
- If the time doesn't say morning or evening and it could be either, ask.
- When nothing is missing, call propose_schedule. Don't describe the schedule yourself.
- After the proposal, if the user agrees, call create_schedule (enable false only if they want it kept off). If they change something, call set_fields instead.
- If they no longer want the schedule, call discard_draft."""

CAPTURE_WORDS = """Capture words: "photo", "picture", "360" and "image" mean image; "scan", "3D", "lidar" and "point cloud" mean pointcloud; "both" or "everything" means both."""

SPEECH = """The user may be speaking, and speech recognition mishears words: "Bose" or "board" usually means both, "month day" means Monday, "room one" is room1, "casual call" can be "schedule called". Use the names in the facts and what was just asked to make sense of it. If you really can't tell, ask."""

PROMPT_SCHEDULES = """You are SiteScope's assistant, helping someone on a construction site set up a recurring capture schedule for the site robot.

{schedule_rules}
- {capture_words}
- {speech}
- Never say a schedule was created, changed or switched on unless create_schedule said so.
- Reply in plain text with no lists, markdown or emoji. {length}

Facts:
{facts}"""

PROMPT_ALL = """You are SiteScope's assistant, a friendly, helpful assistant for people on a construction project. You can talk about anything: small talk, general questions, and construction topics such as materials, methods, safety and terminology. Don't open with a greeting unless the person greeted you.

You also control the site robot through tools. Mention the robot only when it's relevant.
- To send the robot somewhere now, call start_trip. Pass capture only if the user said what to capture.
- To delete, pause, resume or run a saved schedule now, call manage_schedule with its name from the facts.
- "Every Monday", "each day" or "daily" means a schedule, not a trip.
- The only thing you can create is a schedule, so "create X" or "make X" names a new schedule: for "make a morning check", call set_fields with name "Morning check" straight away and don't ask for a name. Use anything the user said just before it (a waypoint, a time) in the schedule too.
- connect_robot, disconnect_robot, stop_robot and open_tab do what they say. The page has these tabs: Route, Schedules, Progress, Live map and History.
- Answer questions about the robot, waypoints, capture types and schedules from the facts only. If the facts don't say, say you don't know.
- Never say you did something (sent, started, scheduled, created, deleted, paused, connected, stopped, opened) unless a tool just did it. You cannot see the page.

{schedule_rules}

{capture_words}

{speech}

Reply in plain text with no lists, markdown or emoji. {length}

Facts:
{facts}"""


def chat_with_tools(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any] | None:
    settings = get_settings()
    try:
        response = httpx.post(
            settings.assistant_api_url,
            json={
                "model": settings.assistant_agent_model,
                "messages": messages,
                "tools": tools,
                "stream": False,
                "think": False,
                "options": {"temperature": 0.2, "num_ctx": 8192, "num_predict": 400},
            },
            timeout=90,
        )
        response.raise_for_status()
        return response.json()["message"]
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("Assistant agent call failed: %s", exc)
        return None


def mode() -> str:
    value = (get_settings().assistant_agent or "off").strip().lower()
    return value if value in MODES else "off"


# --- the schedule draft ---------------------------------------------------------------------


def _empty_draft() -> dict[str, Any]:
    return {"name": None, "waypoints": [], "time": None, "weekdays": [], "capture": []}


def _missing(draft: dict[str, Any]) -> list[str]:
    return [key for key in FIELDS if not draft.get(key)]


def _draft_view(ctx: ra._Context, draft: dict[str, Any]) -> dict[str, Any]:
    """The draft as the model reads it: names, not ids."""
    return {
        "name": draft.get("name"),
        "waypoints": ctx.names(draft.get("waypoints") or []),
        "time": draft.get("time"),
        "weekdays": [ra.WEEKDAY_NAMES[day] for day in draft.get("weekdays") or []],
        "capture": draft.get("capture") or [],
    }


def _current_points(ctx: ra._Context) -> list:
    return [point for point in ctx.points if not robot_maps.is_retired(point, ctx.project)]


def _check_waypoints(ctx: ra._Context, values: list[Any]) -> tuple[list[str] | None, list[str]]:
    ids, problems = [], []
    for value in values:
        point, suggestions = ra.match_points(str(value), ctx.points)
        if point is None:
            close = f" Closest: {ra._join([p.name for p in suggestions], 'or')}." if suggestions else ""
            problems.append(f'There is no waypoint called "{value}".{close}')
        elif point.id in ids:
            problems.append(f"A route can visit {point.name} only once.")
        elif robot_maps.is_retired(point, ctx.project):
            # The endpoints refuse these; say so now, not after every other question.
            problems.append(f"{point.name} was placed on an older robot map, so the robot can't use it. "
                            "Ask for a different waypoint.")
        else:
            ids.append(point.id)
    return (ids if ids and not problems else None), problems


def _check_time(value: Any, said: str = "") -> tuple[str | None, list[str]]:
    text = str(value).strip()
    match = re.fullmatch(r"([01]?\d|2[0-3])[:.]([0-5]\d)", text)
    if match:
        hhmm = f"{int(match.group(1)):02d}:{match.group(2)}"
        # The model writes "at 7" as a clean "07:00"; the person's own words decide whether that's clear.
        heard = ra.read_time(said, None)
        if hhmm in heard.candidates:
            return None, [f'The user said "{said}", which could be {ra._join(heard.candidates, "or")}: ask which.']
        return hhmm, []
    reading = ra.read_time(text, None, expect_time=True)
    if reading.value:
        return reading.value, []
    if reading.candidates:
        return None, [f"{text} could be {ra._join(reading.candidates, 'or')}: ask which."]
    return None, [f'"{text}" is not a time of day.']


_DAY_GROUP_RE = re.compile(r"\b(week ?days?|work ?days|working days|week ?ends?|every ?day|everyday|daily)\b", re.I)


def _check_weekdays(values: list[Any], said: str = "") -> tuple[list[int] | None, list[str]]:
    days: set[int] = set()
    for value in values:
        if isinstance(value, int) and 0 <= value <= 6:
            days.add(value)
            continue
        found = ra.parse_weekdays(str(value))
        if not found:
            return None, [f'"{value}" is not a day of the week.']
        days.update(found)
    # Models miscount "every weekday" and the like; the person's own words decide those.
    heard = ra.parse_weekdays(said) if _DAY_GROUP_RE.search(said) else None
    if heard and sorted(days) != heard and not (ra._ADD_RE.search(said) or ra._REMOVE_RE.search(said)):
        return None, [f'The user said "{said}", which means {ra.describe_days(heard)}, '
                      f"not {ra.describe_days(sorted(days))}. Use {ra.describe_days(heard)}."]
    return (sorted(days) or None), []


def _check_capture(ctx: ra._Context, values: list[Any]) -> tuple[list[str] | None, list[str]]:
    said = ra.detect_capture(" ".join(str(value) for value in values).replace("pointcloud", "point cloud"),
                             allow_both=True)
    if not said:
        return None, [f"Capture must be image, pointcloud or both, not {values}."]
    available = ctx.available_captures()
    blocked = [output for output in said if output not in available]
    if blocked:
        capability = ctx.capture_capability(blocked[0]) or {}
        reason = ra._sentence(capability.get("reason")) or "It isn't available."
        return None, [f"{CAPTURE_PROBLEM} {ra._capture_label(blocked[0])} right now: {reason}"]
    return said, []


def _check_name(ctx: ra._Context, value: Any, said: str = "") -> tuple[str | None, list[str]]:
    name = ra._clean_schedule_name(str(value))
    if not name:
        return None, ["The name is empty."]
    point, _ = ra.match_points(said, ctx.points)
    if point is not None and ra.normalize_name(name) == ra.normalize_name(point.name):
        # A message that is only a waypoint's name answers "where?", not "what's it called?".
        return None, [f'"{said}" is a waypoint. Use it as a waypoint, and ask for the name again.']
    if any(ra.normalize_name(s.name) == ra.normalize_name(name) for s in ra._schedules(ctx)):
        return None, [f'A schedule called "{name}" already exists: ask for another name.']
    return name, []


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def set_fields(ctx: ra._Context, draft: dict[str, Any], args: dict[str, Any],
               said: str = "") -> tuple[dict[str, Any], dict[str, Any]]:
    """(new draft, result for the model). Only values that pass their check change the draft.
    `said` is the person's message, for checks the model's tidied-up values would hide."""
    draft = {**draft}
    problems: list[str] = []
    checks = {
        "name": lambda v: _check_name(ctx, v, said),
        "waypoints": lambda v: _check_waypoints(ctx, _as_list(v)),
        "time": lambda v: _check_time(v, said),
        "weekdays": lambda v: _check_weekdays(_as_list(v), said),
        "capture": lambda v: _check_capture(ctx, _as_list(v)),
    }
    for key, check in checks.items():
        if args.get(key) in (None, "", []):
            continue
        value, found = check(args[key])
        problems += found
        if value is not None and not found:
            draft[key] = value
    return draft, {"draft": _draft_view(ctx, draft), "missing": _missing(draft), "problems": problems}


def summary(ctx: ra._Context, draft: dict[str, Any]) -> str:
    route = ra._join(ctx.names(draft["waypoints"]))
    return (f'"{draft["name"]}": {route}, {ra.describe_when(draft["time"], draft["weekdays"])}, '
            f"capturing {ra.describe_schedule_capture(draft['capture'])}")


def create(ctx: ra._Context, draft: dict[str, Any], enable: bool) -> ra.AssistantResult:
    capture = draft["capture"]
    schedule = create_robot_mission_schedule(
        RobotMissionScheduleCreateRequest(
            name=draft["name"],
            robot_id=ctx.robot_id,
            project_slug=ctx.project.slug,
            capture_point_ids=draft["waypoints"],
            local_time=draft["time"],
            timezone=ctx.timezone,
            weekdays=draft["weekdays"],
            enabled=enable,
            capture_mode="panorama" if "image" in capture else "pointcloud",
            retry_policy={"continue_on_failure": True},
            robot_meta={"capture_outputs": capture, "trigger": "assistant"},
        ),
        current_user=ctx.user,
        db=ctx.db,
    )
    when = ra.describe_when(draft["time"], draft["weekdays"])
    reply = (f'Created "{schedule.name}" and switched it on. It runs {when}.' if enable
             else f'Created "{schedule.name}". It\'s switched off for now; you can turn it on from the Schedules tab.')
    if not ctx.voice:
        reply += f" {ra.DEFAULT_FIELDS_NOTE}"
    return ra.AssistantResult(reply=reply, state=None, action={"type": "schedule_created", "schedule_id": schedule.id})


# --- robot tools ----------------------------------------------------------------------------


def _start_trip(ctx: ra._Context, args: dict[str, Any]) -> ra.AssistantResult | dict[str, Any]:
    """Hands a checked trip to the coded flow, which asks what's missing, connects and starts it."""
    names = [str(name) for name in _as_list(args.get("waypoints") or []) if str(name).strip()]
    if not names:
        return {"error": "No waypoints given: ask where the robot should go."}
    ids, problems = _check_waypoints(ctx, names)
    capture: list[str] = []
    if args.get("capture"):
        capture, capture_problems = _check_capture(ctx, _as_list(args["capture"]))
        problems += capture_problems
    if problems or not ids:
        return {"problems": problems}
    trip = {"intent": "go_to", "waypoints": [{"query": n, "id": i} for n, i in zip(ctx.names(ids), ids)],
            "capture": capture or []}
    return ra._advance(ctx, trip)


def _schedule_named(ctx: ra._Context, name: str):
    schedules = ra._schedules(ctx)
    key = ra.normalize_name(name)
    if not key:
        return None
    exact = [s for s in schedules if ra.normalize_name(s.name) == key or ra._name_in(name, s.name)]
    if len(exact) == 1:
        return exact[0]
    close = difflib.get_close_matches(key, [ra.normalize_name(s.name) for s in schedules], n=1, cutoff=0.75)
    return next((s for s in schedules if close and ra.normalize_name(s.name) == close[0]), None)


def _manage(ctx: ra._Context, args: dict[str, Any]) -> ra.AssistantResult | dict[str, Any]:
    action = {"delete": "delete", "pause": "pause", "resume": "resume", "run_now": "run"}.get(str(args.get("action")))
    if action is None:
        return {"error": "action must be delete, pause, resume or run_now."}
    name = str(args.get("schedule") or "")
    schedule = _schedule_named(ctx, name)
    if schedule is None:
        names = ", ".join(s.name for s in ra._schedules(ctx)) or "none"
        return {"error": f'There is no schedule called "{name}". The schedules are: {names}.'}
    return ra._manage_schedule(ctx, None, action, schedule)


# --- the conversation -----------------------------------------------------------------------


def _schedule_line(schedule) -> str:
    when = ra.describe_when(schedule.local_time, list(schedule.weekdays_json or []))
    return f"{schedule.name} ({when}, {'on' if schedule.enabled else 'paused'})"


def _facts(ctx: ra._Context, state: dict[str, Any], everything: bool) -> str:
    draft = state["draft"]
    names = [point.name for point in _current_points(ctx)]
    lines = [
        f"Project: {ctx.project.name}.",
        f"Waypoints ({len(names)}): {', '.join(names) if names else 'none yet'}.",
        f"What the robot can capture right now: {ra.capture_options(ctx)}",
    ]
    if everything:
        connection = ("connected" if ctx.robot_connected
                      else "connecting" if ra._connecting(ctx) is not None else "not connected")
        schedules = [_schedule_line(s) for s in ra._schedules(ctx)]
        lines += [
            f"Robot: {ctx.robot_id}, {connection}.",
            f"Robot status: {ra._status_reply(ctx)}",
            f"Schedules ({len(schedules)}): {'; '.join(schedules) if schedules else 'none'}.",
        ]
    else:
        lines.append(f"Existing schedules: {', '.join(s.name for s in ra._schedules(ctx)) or 'none'}.")
    if not everything or any(draft.get(key) for key in FIELDS):
        lines += [
            f"Schedule being drafted: {json.dumps(_draft_view(ctx, draft))}.",
            f"Still missing from it: {', '.join(_missing(draft)) or 'nothing'}.",
            f"The finished schedule was just read back to the user: {'yes' if state.get('proposed') else 'no'}.",
        ]
    return "\n".join(lines)


def _asking(state: dict[str, Any], reply: str) -> ra.AssistantResult:
    """Keep the conversation open. The reply doubles as the question re-asked after a side trip."""
    return ra.AssistantResult(reply=reply, state={**state, "question": reply, "reask": reply})


def _fallback(ctx: ra._Context, state: dict[str, Any], text: str) -> ra.AssistantResult:
    """The model can't be reached: carry the draft on in the rule-based flow, using this message
    as the answer to its next question when it is one."""
    draft = state["draft"]
    rule_state = {
        "intent": "create_schedule",
        "name": draft.get("name"),
        "waypoints": [{"query": name, "id": point_id}
                      for name, point_id in zip(ctx.names(draft["waypoints"]), draft["waypoints"])],
        "time": draft.get("time"),
        "weekdays": draft.get("weekdays") or [],
        "capture": draft.get("capture") or [],
    }
    asked = ra._advance(ctx, rule_state)
    if not asked.state or not asked.state.get("awaiting"):
        return asked
    unreachable = ra._LazyParse(lambda *_: None, text, None)
    answered = ra._fill_slot(ctx, asked.state, text, unreachable)
    return answered if answered is not None else asked


def _new_state() -> dict[str, Any]:
    return {"intent": INTENT, "awaiting": "agent", "draft": _empty_draft(), "proposed": False}


def start(ctx: ra._Context, text: str, model: AgentModel | None = None) -> ra.AssistantResult:
    """'schedules' mode: a schedule request starts the model's conversation."""
    return reply_to(ctx, _new_state(), text, model)  # type: ignore[return-value]


def reply_to(ctx: ra._Context, state: dict[str, Any] | None, text: str,
             model: AgentModel | None = None) -> ra.AssistantResult | None:
    """One user message. In 'all' mode, None means the model can't be reached and nothing is
    being drafted: the rule-based assistant should answer instead."""
    model = model or chat_with_tools
    everything = ctx.agent_mode == "all"
    state = state if state and state.get("intent") == INTENT else _new_state()
    proposed_before = bool(state.get("proposed"))
    state = {**state, "draft": {**_empty_draft(), **(state.get("draft") or {})}, "proposed": False}
    length = ("Your replies are spoken aloud: one short sentence." if ctx.voice
              else "Keep replies to one or two short sentences, longer only when a question needs it.")
    prompt = PROMPT_ALL if everything else PROMPT_SCHEDULES
    messages: list[dict[str, Any]] = [{
        "role": "system",
        "content": prompt.format(schedule_rules=SCHEDULE_RULES, capture_words=CAPTURE_WORDS, speech=SPEECH, length=length,
                                 facts=_facts(ctx, {**state, "proposed": proposed_before}, everything)),
    }]
    for turn in ctx.history[-MAX_TURNS:]:
        content = str(turn.get("text") or "")[:600]
        if content:
            messages.append({"role": "assistant" if turn.get("role") == "assistant" else "user", "content": content})
    messages.append({"role": "user", "content": text})
    tools = SCHEDULE_TOOLS + ROBOT_TOOLS if everything else SCHEDULE_TOOLS

    start_draft = state["draft"]
    reply = ""
    nudged = False
    acted = False  # a tool ran this turn, so an empty answer after it is fine
    tab_action: dict[str, Any] | None = None
    # Capture types asked for but unavailable: a read-back says why, so a dropped one isn't a
    # surprise. From the person's own words too, since the model may quietly leave one out.
    notes = _unavailable_asked(ctx, text)
    for _ in range(MAX_MODEL_CALLS):
        message = model(messages, tools)
        if message is None:
            if everything and not any(state["draft"].get(key) for key in FIELDS):
                return None
            return _fallback(ctx, state, text)
        calls = message.get("tool_calls") or []
        if not calls:
            reply = ra._plain_text(message.get("content") or "")
            if not reply and not nudged and not acted:
                # Models sometimes return nothing; once, ask again before giving up on them.
                nudged = True
                messages.append({"role": "system", "content": NUDGE})
                continue
            # After editing the draft, "I've added Friday" is true; claims of real actions never are.
            claim_re = _REAL_ACTION_CLAIM_RE if acted else ra._ACTION_CLAIM_RE
            if claim_re.search(reply) or _DONE_CLAIM_RE.search(reply):
                logger.info("Assistant agent claimed an action and was replaced: %r", reply)
                reply = NOTHING_DONE
            break

        messages.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": calls})
        acted = True
        for call in calls:
            name = (call.get("function") or {}).get("name")
            args = (call.get("function") or {}).get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            if not any(tool["function"]["name"] == name for tool in tools):
                result: dict[str, Any] = {"error": f"There is no tool called {name}."}
            elif name == "set_fields":
                draft, result = set_fields(ctx, state["draft"], args, said=text)
                notes += [problem for problem in result["problems"] if problem.startswith(CAPTURE_PROBLEM)]
                if draft != state["draft"]:
                    proposed_before = False  # a changed schedule must be read back again
                state = {**state, "draft": draft}
            elif name == "propose_schedule":
                missing = _missing(state["draft"])
                if missing:
                    result = {"error": f"Still missing: {', '.join(missing)}. Ask for it first."}
                elif proposed_before and ra.yes_or_no(text) == "yes" and not _create_refusal(state, text, True):
                    # Asked to read back what was just read back and agreed to: the yes stands.
                    return create(ctx, state["draft"], True)
                elif proposed_before:
                    result = {"error": "This exact schedule was already read back and the user has answered. "
                                       "Call create_schedule if they agreed, or set_fields if they changed something."}
                else:
                    return _propose(ctx, state, notes)
            elif name == "create_schedule":
                refusal = _create_refusal(state, text, proposed_before)
                if refusal:
                    result = {"error": refusal}
                else:
                    return create(ctx, state["draft"], args.get("enable") is not False)
            elif name == "discard_draft":
                return ra._done("OK, I won't create it.")
            elif name == "open_tab":
                tab = str(args.get("tab"))
                if tab not in ra.TAB_LABELS:
                    result = {"error": f"tab must be one of {', '.join(ra.TAB_LABELS)}."}
                else:
                    tab_action = {"type": "open_tab", "tab": tab}
                    result = {"opened": f"the {ra.TAB_LABELS[tab]} tab"}
            else:
                # The robot tools hand over to the coded paths, which reply themselves.
                done = {
                    "start_trip": lambda: _start_trip(ctx, args),
                    "manage_schedule": lambda: _manage(ctx, args),
                    "connect_robot": lambda: ra._connect(ctx),
                    "disconnect_robot": lambda: ra._disconnect(ctx),
                    "stop_robot": lambda: ra._stop(ctx, None, text),
                }[name]()
                if isinstance(done, ra.AssistantResult):
                    if tab_action and done.action is None:
                        done.action = tab_action
                    return done
                result = done
            messages.append({"role": "tool", "tool_name": name or "", "content": json.dumps(result)})

    drafting = any(state["draft"].get(key) for key in FIELDS) or proposed_before
    if everything and not drafting:
        # Plain conversation: nothing to keep between messages.
        if tab_action and reply in ("", NOTHING_DONE):
            reply = f"Opened the {ra.TAB_LABELS[tab_action['tab']]} tab."
        elif reply == NOTHING_DONE:
            reply = ("I haven't done anything yet. Tell me what you'd like, such as where to send the robot "
                     "or which schedule to change, and I'll take care of it.")
        elif not reply:
            reply = "Sorry, I didn't catch that. What would you like to do?"
        return ra.AssistantResult(reply=reply, state=None, action=tab_action)

    # A schedule that just became complete, or changed after it was read back, is read back now:
    # the model doesn't get to skip that.
    missing = _missing(state["draft"])
    still_proposed = proposed_before and state["draft"] == start_draft
    if not missing and not still_proposed:
        asked = _propose(ctx, state, notes)
    else:
        if reply in ("", NOTHING_DONE):
            question = QUESTION_AFTER_READ_BACK if not missing else QUESTIONS[missing[0]]
            reply = f"I haven't created it yet. {question}" if reply else question
        asked = _asking({**state, "proposed": still_proposed}, reply)
    asked.action = tab_action
    return asked


def _unavailable_asked(ctx: ra._Context, text: str) -> list[str]:
    said = ra.detect_capture(ra._BOTH_MISHEARD_RE.sub("both", text), allow_both=True)
    return [problem for output in said if output not in ctx.available_captures()
            for problem in _check_capture(ctx, [output])[1]]


def _propose(ctx: ra._Context, state: dict[str, Any], notes: list[str] | None = None) -> ra.AssistantResult:
    said = "".join(f"{note} " for note in dict.fromkeys(notes or []))
    reply = f"{said}Here's the schedule: {summary(ctx, state['draft'])}. {QUESTION_AFTER_READ_BACK}"
    return _asking({**state, "proposed": True}, reply)


def _create_refusal(state: dict[str, Any], text: str, proposed_before: bool) -> str | None:
    """Why the schedule can't be created on this message, or None when it can."""
    if _missing(state["draft"]):
        return f"Still missing: {', '.join(_missing(state['draft']))}."
    if not proposed_before:
        return "Call propose_schedule first so the user hears the finished schedule."
    if ra._NO_RE.search(text):
        return "The user said no. Ask what to change."
    return None


def draft_of(state: dict[str, Any]) -> dict[str, Any]:
    draft = state.get("draft") or {}
    missing = _missing({**_empty_draft(), **draft})
    return {
        "kind": "schedule",
        "name": draft.get("name") or "",
        "point_ids": list(draft.get("waypoints") or []),
        "time": draft.get("time"),
        "weekdays": list(draft.get("weekdays") or []),
        "capture": list(draft.get("capture") or []),
        "focus": missing[0] if missing else None,
    }
