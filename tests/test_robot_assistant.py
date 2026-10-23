import os
import unittest
from datetime import date, datetime, timezone

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Project,
    ProjectMember,
    RobotCapturePoint,
    RobotCommand,
    RobotMission,
    RobotMissionSchedule,
    RobotPresence,
    Room,
    User,
)
from app.services.robot_assistant import (
    CAPABILITIES,
    DEFAULT_FIELDS_NOTE,
    detect_capture,
    handle_message,
    normalize_name,
    parse_weekdays,
    read_time,
)


class FakeParser:
    """Stands in for the language model: a fixed reading per message, and a call log."""

    def __init__(self, readings: dict[str, dict] | None = None, *, reachable: bool = True) -> None:
        self.readings = readings or {}
        self.reachable = reachable
        self.calls: list[str] = []

    def __call__(self, message: str, last_question: str | None) -> dict | None:
        self.calls.append(message)
        if not self.reachable:
            return None
        reading = {"intent": "answer", "waypoints": [], "time": None, "weekdays": [], "name": None, "yes_no": None}
        reading.update(self.readings.get(message, {"intent": "unknown"}))
        return reading


class FakeChat:
    """Stands in for the conversational model. None means the model is unreachable."""

    def __init__(self, reply: str | None = None) -> None:
        self.reply = reply
        self.calls: list[tuple[str, list, str]] = []

    def __call__(self, message: str, history: list, facts: str) -> str | None:
        self.calls.append((message, history, facts))
        return self.reply


class ReaderTests(unittest.TestCase):
    def test_time_with_clear_am_pm_or_24h_is_used_as_is(self) -> None:
        self.assertEqual(read_time("at 8am", None).value, "08:00")
        self.assertEqual(read_time("every day at 6pm", None).value, "18:00")
        self.assertEqual(read_time("at 18:00", None).value, "18:00")
        self.assertEqual(read_time("07:30", None).value, "07:30")
        self.assertEqual(read_time("at noon", None).value, "12:00")
        self.assertEqual(read_time("half past seven in the evening", "07:30").value, "19:30")

    def test_unclear_am_pm_gives_two_candidates(self) -> None:
        self.assertEqual(read_time("at 7", None).candidates, ["07:00", "19:00"])
        self.assertEqual(read_time("7:30", None).candidates, ["07:30", "19:30"])
        # The model read this as 19:30 in the benchmark; the guard turns it into a question.
        self.assertEqual(read_time("half past seven", "19:30").candidates, ["07:30", "19:30"])

    def test_numbers_inside_names_are_not_times(self) -> None:
        reading = read_time("go to room1 and take a 360 photo", None)
        self.assertIsNone(reading.value)
        self.assertEqual(reading.candidates, [])

    def test_weekdays_from_words(self) -> None:
        self.assertEqual(parse_weekdays("Monday, Wednesday and Friday"), [0, 2, 4])
        self.assertEqual(parse_weekdays("every weekday except Friday"), [0, 1, 2, 3])
        self.assertEqual(parse_weekdays("every day"), list(range(7)))
        self.assertEqual(parse_weekdays("weekends"), [5, 6])
        self.assertEqual(parse_weekdays("monday to thursday"), [0, 1, 2, 3])
        self.assertIsNone(parse_weekdays("whenever"))

    def test_capture_only_keeps_what_was_said(self) -> None:
        self.assertEqual(detect_capture("take a 360 photo"), ["image"])
        self.assertEqual(detect_capture("grab a point cloud"), ["pointcloud"])
        self.assertEqual(detect_capture("go to bedroom"), [])
        self.assertEqual(detect_capture("both", allow_both=True), ["image", "pointcloud"])

    def test_spoken_numbers_match_written_names(self) -> None:
        self.assertEqual(normalize_name("bedroom one"), normalize_name("Bedroom-1"))


class ConversationBase(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        self.operator = User(username="operator", password_hash="x", is_admin=True)
        self.robot = User(username="robot-1", password_hash="x", is_robot=True)
        self.project = Project(name="Demo", slug="demo")
        self.db.add_all([self.operator, self.robot, self.project])
        self.db.flush()
        self.db.add(ProjectMember(project_id=self.project.id, user_id=self.robot.id, role="editor"))
        self.points = {}
        for name in ["room1", "room2", "room3", "corridor", "lobby"]:
            self.add_point(name)
        self.db.commit()
        self.state = None
        self.parser = FakeParser()
        self.chat = FakeChat()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def add_point(self, name: str, room_slug: str | None = None) -> RobotCapturePoint:
        point = RobotCapturePoint(project_id=self.project.id, name=name, room_slug=room_slug, map_x=0.0, map_y=0.0)
        self.db.add(point)
        self.db.flush()
        self.points[name] = point
        return point

    def say(self, message: str, *, connected: bool = True, voice: bool = False, event: str | None = None) -> str:
        result = handle_message(
            db=self.db,
            user=self.operator,
            project_slug="demo",
            robot_id="robot-1",
            message=message,
            state=self.state,
            timezone="Asia/Dubai",
            robot_connected=connected,
            voice=voice,
            event=event,
            history=[{"role": "user", "text": "earlier message"}],
            parser=self.parser,
            chatter=self.chat,
        )
        self.state = result.state
        self.last_action = result.action
        return result.reply

    def missions(self) -> list[RobotMission]:
        return list(self.db.scalars(select(RobotMission)).all())


class AssistantConversationTests(ConversationBase):
    # --- going to a waypoint ------------------------------------------------------------

    def test_go_to_without_capture_type_asks_and_does_not_move(self) -> None:
        self.parser.readings = {"go to room1": {"intent": "go_to", "waypoints": ["room1"]}}
        reply = self.say("go to room1")
        self.assertEqual(reply, "What should I capture at room1? Want me to list the options?")
        self.assertEqual(self.missions(), [])

        self.assertEqual(self.say("yes"), "I can capture a 360° image, a point cloud, or both. Which one would you like?")
        self.assertEqual(self.missions(), [])

        reply = self.say("both")
        self.assertEqual(reply, "Going to room1 and capturing a 360° image and a point cloud.")
        [mission] = self.missions()
        self.assertEqual(mission.robot_meta_json["capture_outputs"], ["image", "pointcloud"])
        self.assertEqual(mission.robot_meta_json["capture_point_ids"], [self.points["room1"].id])
        self.assertEqual(mission.robot_meta_json["trigger"], "assistant")
        self.assertIsNone(self.state)

    def test_go_to_with_capture_type_starts_right_away(self) -> None:
        message = "go to room2 and grab a point cloud"
        self.parser.readings = {message: {"intent": "go_to", "waypoints": ["room2"]}}
        self.assertEqual(self.say(message), "Going to room2 and capturing a point cloud.")
        [mission] = self.missions()
        self.assertEqual(mission.capture_mode, "pointcloud")

    def test_unknown_waypoint_in_small_project_lists_all(self) -> None:
        self.parser.readings = {"go to bedroom": {"intent": "go_to", "waypoints": ["bedroom"]}}
        reply = self.say("go to bedroom")
        self.assertEqual(
            reply,
            'I couldn\'t find a waypoint called "bedroom" in this project. The available waypoints are '
            "corridor, lobby, room1, room2 and room3. Which one do you mean?",
        )
        self.assertEqual(self.say("the lobby"), "What should I capture at lobby? Want me to list the options?")

    def test_close_matches_are_offered_not_used(self) -> None:
        for name in ["bedroom-1", "bedroom-2", "master-bedroom", "kitchen", "garage", "stairs"]:
            self.add_point(name)
        self.db.commit()
        self.parser.readings = {"go to bedroom": {"intent": "go_to", "waypoints": ["bedroom"]}}
        reply = self.say("go to bedroom")
        self.assertEqual(reply, 'I couldn\'t find "bedroom". Did you mean bedroom-1, bedroom-2 or master-bedroom?')
        self.assertEqual(self.missions(), [])
        self.assertEqual(self.say("the second one"), "What should I capture at bedroom-2? Want me to list the options?")

    def test_typo_is_offered_as_a_suggestion(self) -> None:
        self.add_point("kitchen")
        self.db.commit()
        self.parser.readings = {"go to kichen": {"intent": "go_to", "waypoints": ["kichen"]}}
        self.assertEqual(self.say("go to kichen"), 'I couldn\'t find "kichen". Did you mean kitchen?')
        self.assertEqual(self.missions(), [])
        self.assertEqual(self.say("yes"), "What should I capture at kitchen? Want me to list the options?")

    def test_leading_article_still_matches_exactly(self) -> None:
        message = "go to the lobby and take a photo"
        self.parser.readings = {message: {"intent": "go_to", "waypoints": ["the lobby"]}}
        self.assertEqual(self.say(message), "Going to lobby and capturing a 360° image.")

    def test_spoken_number_matches_waypoint(self) -> None:
        self.parser.readings = {
            "go to room one and take a photo": {"intent": "go_to", "waypoints": ["room one"]},
        }
        self.assertEqual(self.say("go to room one and take a photo"), "Going to room1 and capturing a 360° image.")

    def test_capture_word_inside_a_waypoint_name_is_not_a_capture_type(self) -> None:
        self.add_point("3D lab")
        self.db.commit()
        self.parser.readings = {"go to 3D lab": {"intent": "go_to", "waypoints": ["3D lab"]}}
        self.assertEqual(self.say("go to 3D lab"), "What should I capture at 3D lab? Want me to list the options?")
        self.assertEqual(self.missions(), [])

    def test_sixty_waypoints_are_narrowed_down_by_room(self) -> None:
        rooms = ["kitchen", "living", "corridor-a", "bedroom-wing", "lobby-a", "stairs", "garage", "roof"]
        for slug in rooms:
            self.db.add(Room(project_id=self.project.id, name=slug.replace("-", " ").title(), slug=slug))
        for index in range(55):
            self.add_point(f"{rooms[index % len(rooms)]}-p{index}", room_slug=rooms[index % len(rooms)])
        self.db.commit()
        self.assertEqual(len(self.db.scalars(select(RobotCapturePoint)).all()), 60)

        self.parser.readings = {"go to the garden": {"intent": "go_to", "waypoints": ["garden"]}}
        reply = self.say("go to the garden", voice=True)
        self.assertEqual(
            reply,
            'I couldn\'t find "garden". This project has 60 waypoints. '
            "Tell me part of the name, or I can list them by room.",
        )
        reply = self.say("list them by room", voice=True)
        self.assertTrue(reply.startswith("There are waypoints in 9 rooms, including "), reply)
        self.assertEqual(reply.count(","), 4)  # five names when spoken

        reply = self.say("bedroom wing", voice=True)
        self.assertTrue(reply.startswith("Bedroom Wing has 7 waypoints, including bedroom-wing-p11"), reply)
        reply = self.say("bedroom-wing-p3", voice=True)
        self.assertEqual(reply, "What should I capture at bedroom-wing-p3? Want me to list the options?")

    def test_room_name_after_a_full_list_narrows_to_that_room(self) -> None:
        self.db.add(Room(project_id=self.project.id, name="Bedroom wing", slug="bedroom-wing"))
        for name in ["bedroom-1", "bedroom-2"]:
            self.add_point(name, room_slug="bedroom-wing")
        self.db.commit()
        self.parser.readings = {"what waypoints are there?": {"intent": "list"}}
        self.assertTrue(self.say("what waypoints are there?").startswith("The available waypoints are"))
        self.assertEqual(
            self.say("bedroom wing"),
            "Bedroom wing has 2 waypoints: bedroom-1 and bedroom-2. Want me to send the robot to one of them?",
        )
        self.assertEqual(self.say("bedroom 2"), "What should I capture at bedroom-2? Want me to list the options?")

    def test_offline_robot_is_connected_first_then_mission_starts(self) -> None:
        message = "go to corridor and take a picture"
        self.parser.readings = {message: {"intent": "go_to", "waypoints": ["corridor"]}}
        self.assertEqual(self.say(message, connected=False), "The robot isn't connected. Should I connect it?")
        reply = self.say("yes", connected=False)
        self.assertEqual(reply, "Connecting the robot. I'll go to corridor as soon as it's connected.")
        [command] = self.db.scalars(select(RobotCommand)).all()
        self.assertEqual(command.kind, "connect")
        self.assertEqual(self.missions(), [])

        reply = self.say("", event="robot_connected")
        self.assertEqual(reply, "The robot is connected. Going to corridor and capturing a 360° image.")
        self.assertEqual(len(self.missions()), 1)

    def test_offline_robot_declined_does_nothing(self) -> None:
        message = "go to corridor and take a picture"
        self.parser.readings = {message: {"intent": "go_to", "waypoints": ["corridor"]}}
        self.say(message, connected=False)
        self.assertEqual(self.say("no", connected=False), "OK, I won't start. Connect the robot and ask me again.")
        self.assertEqual(self.db.scalars(select(RobotCommand)).all(), [])
        self.assertEqual(self.missions(), [])

    def test_busy_robot_does_not_get_a_second_mission(self) -> None:
        self.add_running_mission("running")
        message = "go to lobby and take a photo"
        self.parser.readings = {message: {"intent": "go_to", "waypoints": ["lobby"]}}
        self.assertEqual(self.say(message), "The robot is busy with another capture. Say stop to cancel it first.")
        self.assertEqual(len(self.missions()), 1)

    # --- stopping -----------------------------------------------------------------------

    def add_running_mission(self, status: str) -> RobotMission:
        mission = RobotMission(
            robot_user_id=self.robot.id,
            robot_username=self.robot.username,
            project_id=self.project.id,
            requested_by_user_id=self.operator.id,
            status=status,
            capture_mode="panorama",
            capture_date=date(2026, 10, 21),
            waypoints_json=[{"name": "room1"}],
        )
        self.db.add(mission)
        self.db.commit()
        return mission

    def test_stop_skips_the_model_and_cancels(self) -> None:
        mission = self.add_running_mission("running")
        reply = self.say("hold on, stop the robot right now")
        self.assertEqual(reply, "Stopping. The robot will cancel the capture and return to its start.")
        self.assertEqual(self.parser.calls, [])
        self.db.refresh(mission)
        self.assertEqual(mission.status, "cancel_requested")

    def test_stop_while_returning_keeps_robot_in_place(self) -> None:
        mission = self.add_running_mission("returning_to_start")
        self.assertEqual(self.say("stop"), "Stopping the return. The robot will stay where it is.")
        self.db.refresh(mission)
        self.assertEqual(mission.status, "stop_requested")

    def test_stop_with_nothing_running(self) -> None:
        self.assertEqual(self.say("stop"), "The robot isn't running a capture right now.")

    def test_stop_works_when_model_is_unreachable(self) -> None:
        self.parser.reachable = False
        mission = self.add_running_mission("queued")
        self.assertEqual(self.say("stop"), "Cancelled the queued capture.")
        self.db.refresh(mission)
        self.assertEqual(mission.status, "cancelled")
        self.assertIn("isn't reachable", self.say("send it somewhere nice"))
        # A waypoint named on its own needs no model at all.
        self.assertEqual(self.say("the lobby"), "What should I capture at lobby? Want me to list the options?")

    # --- connecting and disconnecting -----------------------------------------------------

    def commands(self) -> list[RobotCommand]:
        return list(self.db.scalars(select(RobotCommand)).all())

    def test_disconnect_requests_disconnect_the_robot(self) -> None:
        for message in ["disconnect it", "i said disconnect the robot", "so disconnect it", "can you disconnect the robot?"]:
            self.db.query(RobotCommand).delete()
            self.db.commit()
            self.assertEqual(self.say(message), "Disconnecting the robot.", message)
            [command] = self.commands()
            self.assertEqual(command.kind, "disconnect")
            self.assertEqual(self.last_action, {"type": "robot_disconnecting"})
        self.assertEqual(self.parser.calls, [])  # plain requests need no model

    def test_question_about_connection_is_not_a_request(self) -> None:
        self.chat.reply = "Yes, robot-1 is connected and idle."
        self.parser.readings = {"is it connected?": {"intent": "unknown"}}
        self.assertEqual(self.say("is it connected?"), self.chat.reply)
        self.assertEqual(self.commands(), [])

    def test_disconnect_waits_for_a_running_capture(self) -> None:
        self.add_running_mission("running")
        self.assertEqual(
            self.say("disconnect the robot"),
            "The robot is busy with a capture. Say stop first, then I can disconnect it.",
        )
        self.assertEqual(self.commands(), [])

    def test_connect_and_already_states(self) -> None:
        self.assertEqual(self.say("connect the robot", connected=False), "Connecting the robot. It takes a moment to come online.")
        self.assertEqual([c.kind for c in self.commands()], ["connect"])
        self.assertEqual(self.say("connect it", connected=True), "The robot is already connected.")
        self.assertEqual(self.say("disconnect it", connected=False), "The robot is already disconnected.")
        self.assertEqual(len(self.commands()), 1)

    def test_model_reading_disconnect_is_not_treated_as_stop(self) -> None:
        self.parser.readings = {"take the robot off the network": {"intent": "disconnect"}}
        self.assertEqual(self.say("take the robot off the network"), "Disconnecting the robot.")

    # --- page tabs ----------------------------------------------------------------------

    def test_open_a_tab(self) -> None:
        self.assertEqual(self.say("open the schedules tab"), "Opened the Schedules tab.")
        self.assertEqual(self.last_action, {"type": "open_tab", "tab": "schedules"})
        self.assertEqual(self.say("show me the live map"), "Opened the Live map tab.")
        self.assertEqual(self.last_action, {"type": "open_tab", "tab": "live"})
        self.assertEqual(self.parser.calls, [])

    def test_opening_a_tab_keeps_the_pending_question(self) -> None:
        self.parser.readings = {"go to room1": {"intent": "go_to", "waypoints": ["room1"]}}
        self.say("go to room1")
        reply = self.say("open the progress tab")
        self.assertEqual(reply, "Opened the Progress tab. What should I capture at room1: a 360° image, a point cloud, or both?")
        self.assertEqual(self.state["awaiting"], "capture")

    def test_waypoint_named_like_a_tab_wins_without_the_word_tab(self) -> None:
        self.add_point("history")
        self.db.commit()
        self.parser.readings = {"go to history": {"intent": "go_to", "waypoints": ["history"]}}
        self.assertEqual(self.say("go to history"), "What should I capture at history? Want me to list the options?")
        self.assertEqual(self.say("never mind"), "OK, cancelled.")
        self.assertEqual(self.say("go to the history tab"), "Opened the History tab.")

    # --- schedules ----------------------------------------------------------------------

    def test_schedule_asks_only_for_fields_without_defaults(self) -> None:
        self.parser.readings = {
            "create a schedule": {"intent": "create_schedule"},
            "room1, room3 then room2": {"intent": "answer", "waypoints": ["room1", "room3", "room2"]},
            "half past seven": {"intent": "answer", "time": "19:30"},
        }
        self.assertEqual(self.say("create a schedule"), "What should I call this schedule?")
        self.assertEqual(self.say("morning sweep"), "Which waypoints should it visit, in order?")
        self.assertEqual(self.say("room1, room3 then room2"), "What time should it run?")
        self.assertEqual(self.say("half past seven"), "07:30 or 19:30?")
        self.assertEqual(self.say("in the morning"), "Which days should it run?")
        self.assertEqual(
            self.say("Monday, Wednesday and Friday"),
            "What should it capture at each stop? Want me to list the options?",
        )
        reply = self.say("just images")
        self.assertEqual(
            reply,
            'Created "Morning sweep": room1 → room3 → room2, at 07:30 on Monday, Wednesday and Friday, '
            f"capturing 360° images. {DEFAULT_FIELDS_NOTE} Do you want me to enable this schedule?",
        )
        [schedule] = self.db.scalars(select(RobotMissionSchedule)).all()
        self.assertFalse(schedule.enabled)
        self.assertEqual(schedule.timezone, "Asia/Dubai")
        self.assertEqual(schedule.local_time, "07:30")
        self.assertEqual(schedule.weekdays_json, [0, 2, 4])
        self.assertEqual(
            schedule.capture_point_ids_json,
            [self.points["room1"].id, self.points["room3"].id, self.points["room2"].id],
        )
        self.assertEqual(self.say("yeah go ahead"), 'Done, "Morning sweep" is enabled.')
        self.db.refresh(schedule)
        self.assertTrue(schedule.enabled)
        self.assertIsNotNone(schedule.next_run_at)

    def test_listing_while_building_a_route_accepts_several_waypoints(self) -> None:
        self.parser.readings = {
            "can you create a scheduled captyure": {"intent": "create_schedule"},
            "what waypoints are available?": {"intent": "list"},
        }
        self.assertEqual(self.say("can you create a scheduled captyure"), "What should I call this schedule?")
        self.assertEqual(self.say("Demo 1"), "Which waypoints should it visit, in order?")
        self.assertEqual(
            self.say("what waypoints are available?"),
            "The available waypoints are corridor, lobby, room1, room2 and room3. Which waypoints should it visit, in order?",
        )
        self.assertEqual(self.say("okay room3 and lobby"), "What time should it run?")
        self.assertEqual(
            [entry["id"] for entry in self.state["waypoints"]],
            [self.points["room3"].id, self.points["lobby"].id],
        )

    def test_unknown_name_among_several_is_still_asked_about(self) -> None:
        self.parser.readings = {"create a schedule called demo": {"intent": "create_schedule", "name": "demo"}}
        self.say("create a schedule called demo")
        reply = self.say("lobby and the gym")
        self.assertTrue(reply.startswith('I couldn\'t find a waypoint called "gym"'), reply)

    def test_several_waypoints_answer_a_single_choice(self) -> None:
        self.parser.readings = {"go to bedroom": {"intent": "go_to", "waypoints": ["bedroom"]}}
        self.say("go to bedroom")  # no such waypoint: lists all five
        self.assertEqual(self.say("corridor and lobby"), "What should I capture at corridor and lobby? Want me to list the options?")

    def test_schedule_in_one_sentence_with_day_mismatch_is_read_back(self) -> None:
        message = "set up a schedule called morning sweep for room1, room3 then room2 at 8am on Monday, Wednesday and Friday"
        self.parser.readings = {
            message: {
                "intent": "create_schedule",
                "name": "morning sweep",
                "waypoints": ["room1", "room3", "room2"],
                "time": "08:00",
                "weekdays": [1, 3, 5],  # the gemma3:12b mistake from the benchmark
            }
        }
        self.assertEqual(self.say(message), "Monday, Wednesday and Friday, right?")
        self.assertEqual(self.say("yes"), "What should it capture at each stop? Want me to list the options?")
        reply = self.say("both")
        self.assertIn("at 08:00 on Monday, Wednesday and Friday", reply)
        self.assertIn("capturing 360° images and point clouds", reply)
        self.assertEqual(self.say("not yet"), "OK, it's saved but switched off. You can enable it later from the Schedules tab.")
        [schedule] = self.db.scalars(select(RobotMissionSchedule)).all()
        self.assertFalse(schedule.enabled)

    def test_schedule_does_not_list_default_values(self) -> None:
        message = "create a schedule called nightly for the kitchen every day at 6pm capturing point clouds"
        self.add_point("kitchen")
        self.db.commit()
        self.parser.readings = {
            message: {"intent": "create_schedule", "name": "nightly", "waypoints": ["kitchen"], "time": "18:00", "weekdays": list(range(7))},
        }
        reply = self.say(message)
        self.assertIn("kitchen, every day at 18:00, capturing point clouds", reply)
        for value in ("Asia/Dubai", "skip", "30 minutes", "Enabled"):
            self.assertNotIn(value, reply)

    # --- everything else ----------------------------------------------------------------

    def test_unknown_request_lists_capabilities(self) -> None:
        self.assertEqual(self.say("make me a coffee"), CAPABILITIES)

    def test_greeting_gets_a_conversational_reply_with_real_facts(self) -> None:
        self.chat.reply = "Hi! I can send robot-1 to one of five waypoints or set up a schedule."
        self.parser.readings = {"hi": {"intent": "unknown"}}
        self.assertEqual(self.say("hi", connected=False), self.chat.reply)
        [(message, history, facts)] = self.chat.calls
        self.assertEqual(message, "hi")
        self.assertEqual(history, [{"role": "user", "text": "earlier message"}])
        self.assertIn("robot-1, not connected", facts)
        self.assertIn("Waypoints (5): corridor, lobby, room1, room2, room3", facts)
        self.assertIn("Robot activity: idle", facts)
        self.assertIsNone(self.state)
        self.assertEqual(self.missions(), [])

    def test_small_talk_mid_question_answers_then_asks_again(self) -> None:
        self.parser.readings = {"go to room1": {"intent": "go_to", "waypoints": ["room1"]}}
        self.say("go to room1")
        self.chat.reply = "A point cloud is a 3D scan of the room. Would you like one?"
        reply = self.say("what is a point cloud?")
        self.assertEqual(
            reply,
            "A point cloud is a 3D scan of the room. What should I capture at room1: a 360° image, a point cloud, or both?",
        )
        self.assertIn("Question waiting for the operator's answer: What should I capture at room1:", self.chat.calls[0][2])
        self.assertEqual(self.say("ok, a point cloud then"), "Going to room1 and capturing a point cloud.")

    def test_cancel_drops_the_conversation(self) -> None:
        self.parser.readings = {"go to room1": {"intent": "go_to", "waypoints": ["room1"]}}
        self.say("go to room1")
        self.assertEqual(self.say("never mind"), "OK, cancelled.")
        self.assertIsNone(self.state)
        self.assertEqual(self.missions(), [])

    def test_unclear_answer_repeats_the_question(self) -> None:
        self.parser.readings = {"go to room1": {"intent": "go_to", "waypoints": ["room1"]}}
        self.say("go to room1")
        reply = self.say("hmm what")
        self.assertEqual(reply, "Sorry, I didn't catch that. What should I capture at room1: a 360° image, a point cloud, or both?")


class PendingQuestionTests(ConversationBase):
    """Every kind of reply to every kind of pending question. Only real answers may fill a field."""

    SCHEDULE_START = "create a schedule"

    def start_schedule_at(self, slot: str) -> None:
        self.parser.readings = {self.SCHEDULE_START: {"intent": "create_schedule"}}
        steps = {"name": [], "waypoints": ["Demo 1"], "time": ["Demo 1", "room1 and lobby"],
                 "weekdays": ["Demo 1", "room1 and lobby", "8am"],
                 "capture": ["Demo 1", "room1 and lobby", "8am", "weekdays"]}[slot]
        self.say(self.SCHEDULE_START)
        for step in steps:
            self.say(step)
        self.assertEqual(self.state["awaiting"], slot)

    def test_asking_for_the_choices_never_fills_a_field(self) -> None:
        expected = {
            "name": 'Any name you like, for example "Morning sweep" or "Kitchen check". What should I call this schedule?',
            "waypoints": "The available waypoints are corridor, lobby, room1, room2 and room3. Which waypoints should it visit, in order?",
            "time": "Any time of day works, for example 08:00, 7:30 am or 6 pm. What time should it run?",
            "weekdays": "Any days of the week, for example every day, weekdays, weekends, or Monday and Thursday. Which days should it run?",
            "capture": "I can capture a 360° image, a point cloud, or both. Which one would you like?",
        }
        phrasings = ["what are the choices?", "what are the choices", "what is available", "what are my options",
                     "what can I choose", "help", "I don't know", "list them", "show me"]
        for slot, reply in expected.items():
            for phrasing in phrasings:
                if slot == "name" and phrasing in ("what is available", "list them", "show me", "what are the choices"):
                    continue  # on its own, without a question mark, that could be a name
                with self.subTest(slot=slot, phrasing=phrasing):
                    self.state = None
                    self.start_schedule_at(slot)
                    self.assertEqual(self.say(phrasing), reply)
                    self.assertEqual(self.state["awaiting"], slot)
                    self.assertFalse(self.state.get("waypoints") and slot == "waypoints")
        self.assertEqual(self.db.scalars(select(RobotMissionSchedule)).all(), [])

    def test_the_reported_conversation(self) -> None:
        self.parser.readings = {"lets create a new one": {"intent": "create_schedule"}}
        self.assertEqual(self.say("lets create a new one"), "What should I call this schedule?")
        self.assertEqual(self.say("new"), "Which waypoints should it visit, in order?")
        reply = self.say("what are the choices?")
        self.assertEqual(reply, "The available waypoints are corridor, lobby, room1, room2 and room3. Which waypoints should it visit, in order?")
        reply = self.say("what is available")
        self.assertEqual(reply.count("The available waypoints are"), 1, reply)
        self.assertNotIn("couldn't find", reply)

    def test_questions_get_an_answer_and_a_short_reask(self) -> None:
        self.chat.reply = "Sure. It is a 3D scan. Do you want one?"
        for slot in ("name", "waypoints", "time", "weekdays", "capture"):
            with self.subTest(slot=slot):
                self.state = None
                self.start_schedule_at(slot)
                before = dict(self.state)
                reply = self.say("what is a point cloud?")
                self.assertTrue(reply.startswith("Sure. It is a 3D scan. "), reply)
                self.assertNotIn("Do you want one?", reply)  # the model's own question is dropped
                self.assertTrue(reply.endswith(before["reask"]), reply)
                self.assertEqual(self.state["awaiting"], slot)

    def test_suggestions_are_answers(self) -> None:
        self.start_schedule_at("time")
        self.assertEqual(self.say("how about 8am"), "Which days should it run?")
        self.assertEqual(self.state["time"], "08:00")
        self.assertEqual(self.say("let's do weekdays"), "What should it capture at each stop? Want me to list the options?")
        self.assertTrue(self.say("maybe both").startswith('Created "Demo 1"'))

    def test_all_of_them_means_every_waypoint(self) -> None:
        self.start_schedule_at("waypoints")
        self.assertEqual(self.say("all of them"), "What time should it run?")
        self.assertEqual(len(self.state["waypoints"]), 5)

    def test_bare_and_military_times_when_a_time_was_asked(self) -> None:
        for said, expected in [("8", "08:00 or 20:00?"), ("20", "Which days should it run?"),
                               ("0830", "Which days should it run?"), ("18:45", "Which days should it run?")]:
            with self.subTest(said=said):
                self.state = None
                self.start_schedule_at("time")
                self.assertEqual(self.say(said), expected)

    def test_details_said_together_are_all_kept(self) -> None:
        self.start_schedule_at("waypoints")
        reply = self.say("room1 and lobby at 7am on weekends, point clouds")
        self.assertTrue(reply.startswith('Created "Demo 1": room1 → lobby, on weekends at 07:00, capturing point clouds.'), reply)

    def test_corrections_change_an_earlier_answer(self) -> None:
        self.start_schedule_at("weekdays")
        self.assertEqual(self.say("actually make it 9am"), "Got it, at 09:00. Which days should it run?")
        self.assertEqual(self.state["time"], "09:00")
        self.say("mondays")
        reply = self.say("wait, call it Night run")
        self.assertEqual(reply, 'Got it, named "Night run". What should it capture at each stop? Want me to list the options?')
        self.assertEqual(self.state["name"], "Night run")

    def test_unmistakable_detail_at_another_question_is_a_correction(self) -> None:
        self.start_schedule_at("capture")
        self.assertEqual(self.say("mondays"), "Got it, on Monday. What should it capture at each stop? Want me to list the options?")
        self.assertEqual(self.state["weekdays"], [0])
        self.assertEqual(self.say("7pm"), "Got it, at 19:00. What should it capture at each stop? Want me to list the options?")
        self.assertEqual(self.state["time"], "19:00")

    def test_the_reported_add_days_conversation(self) -> None:
        self.parser.readings = {"create a schedule named demo for monday": {"intent": "create_schedule"}}
        self.assertEqual(self.say("create a schedule named demo for monday"), "Which waypoints should it visit, in order?")
        self.assertEqual(self.state["weekdays"], [0])
        self.assertEqual(self.say("use room1 and add tuesday too"),
                         "Got it, on Monday and Tuesday. What time should it run?")
        self.assertEqual(self.state["weekdays"], [0, 1])
        self.assertEqual(self.say("19"), "What should it capture at each stop? Want me to list the options?")
        self.assertEqual(self.say("add tuesday to the days"),
                         "That's already included. What should it capture at each stop: a 360° image, a point cloud, or both?")
        self.assertEqual(self.say("I add monday"),
                         "That's already included. What should it capture at each stop: a 360° image, a point cloud, or both?")
        self.assertEqual(self.say("add wednesday"),
                         "Got it, on Monday, Tuesday and Wednesday. What should it capture at each stop? Want me to list the options?")
        self.assertEqual(self.say("remove monday"),
                         "Got it, on Tuesday and Wednesday. What should it capture at each stop? Want me to list the options?")
        reply = self.say("photos and add point clouds too")
        self.assertTrue(reply.startswith('Created "Demo": room1, at 19:00 on Tuesday and Wednesday, capturing 360° images and point clouds.'), reply)

    def test_add_and_remove_stops_and_capture(self) -> None:
        self.parser.readings = {"create a schedule called loop": {"intent": "create_schedule", "name": "loop"}}
        self.say("create a schedule called loop")
        self.say("room1 and room2")
        self.assertEqual(self.say("add the lobby as well"), "Got it, route room1 → room2 → lobby. What time should it run?")
        self.assertEqual(self.say("remove room2"), "Got it, route room1 → lobby. What time should it run?")
        self.say("8am")
        self.say("weekdays")
        self.assertEqual(self.say("both"), self.say_peek_created())

    def say_peek_created(self) -> str:
        schedule = self.db.scalars(select(RobotMissionSchedule)).one()
        return (f'Created "Loop": room1 → lobby, on weekdays at 08:00, capturing 360° images and point clouds. '
                f"{DEFAULT_FIELDS_NOTE} Do you want me to enable this schedule?") if schedule else ""

    def test_every_day_except_is_not_a_removal(self) -> None:
        self.start_schedule_at("weekdays")
        self.say("every day except friday")
        self.assertEqual(self.state["weekdays"], [0, 1, 2, 3, 5, 6])

    def test_go_to_correction_switches_the_waypoint(self) -> None:
        self.parser.readings = {"go to room1": {"intent": "go_to", "waypoints": ["room1"]}}
        self.say("go to room1")
        self.assertEqual(self.say("actually the lobby instead"),
                         "Got it, going to lobby. What should I capture at lobby? Want me to list the options?")

    def test_small_talk_mid_question_is_answered_then_reasked(self) -> None:
        self.chat.reply = "Hi there!"
        self.start_schedule_at("time")
        self.assertEqual(self.say("hello"), "Hi there! What time should it run?")

    def test_yes_no_questions_ignore_non_answers(self) -> None:
        self.chat.reply = "Enabling means it runs automatically."
        message = "create a schedule called nightly for lobby every day at 6pm capturing point clouds"
        self.parser.readings = {message: {"intent": "create_schedule", "name": "nightly", "waypoints": ["lobby"]}}
        self.say(message)
        self.assertEqual(self.say("what does enable mean?"), "Enabling means it runs automatically. Do you want me to enable this schedule?")
        self.assertEqual(self.say("what are the options?"),
                         "Yes switches the schedule on so it runs at its set time. No keeps it saved but switched off. "
                         "Do you want me to enable this schedule?")
        self.assertEqual(self.say("sounds good"), 'Done, "Nightly" is enabled.')

    def test_listing_on_request_offers_but_does_not_insist(self) -> None:
        self.parser.readings = {"what waypoints are there?": {"intent": "list"}}
        self.chat.reply = "You're welcome!"
        self.assertEqual(self.say("what waypoints are there?"),
                         "The available waypoints are corridor, lobby, room1, room2 and room3. Want me to send the robot to one of them?")
        self.assertEqual(self.say("thanks"), "You're welcome!")
        self.assertIsNone(self.state)
        self.say("what waypoints are there?")
        self.assertEqual(self.say("no"), "OK, no problem.")
        self.say("what waypoints are there?")
        self.assertEqual(self.say("yes"), "Which one?")
        self.assertEqual(self.say("lobby"), "What should I capture at lobby? Want me to list the options?")

    def test_stop_with_nothing_running_ends_the_conversation(self) -> None:
        self.start_schedule_at("time")
        self.assertEqual(self.say("stop"), "OK, cancelled.")
        self.assertIsNone(self.state)

    def test_chat_reply_claiming_an_action_is_replaced(self) -> None:
        self.chat.reply = "Sure! I've sent the robot to the kitchen."
        self.assertIn("I haven't done anything yet", self.say("could you maybe tidy up the site"))
        self.assertEqual(self.missions(), [])

    def test_capability_questions_get_the_full_answer(self) -> None:
        for message in ["what can you do?", "help", "what are the choices?", "what are my options", "how does this work?"]:
            with self.subTest(message=message):
                reply = self.say(message)
                self.assertTrue(reply.startswith(
                    'I can send the robot to a waypoint to capture a 360° image, a point cloud, or both, for example '
                    '"go to the kitchen and take a photo".'
                ), reply)
                self.assertIn("recurring capture schedules", reply)
        self.assertEqual(self.chat.calls, [])

    def test_offer_ended_by_thanks_does_not_repeat_the_offer(self) -> None:
        self.parser.readings = {"what waypoints are there?": {"intent": "list"}}
        self.chat.reply = "You're welcome. Want me to send the robot to one of them?"
        self.say("what waypoints are there?")
        self.assertEqual(self.say("thanks"), "You're welcome.")
        self.assertIsNone(self.state)

    def test_more_ways_to_ask_for_the_options(self) -> None:
        for slot, message in [("weekdays", "what days are possible?"), ("time", "what times can I pick?"),
                              ("capture", "what can it capture?"), ("name", "ok")]:
            with self.subTest(slot=slot):
                self.state = None
                self.start_schedule_at(slot)
                reask = self.state["reask"]
                reply = self.say(message)
                self.assertNotIn("Sorry", reply)
                self.assertTrue(reply.endswith(reask) or reply.endswith("Which one would you like?"), reply)
                self.assertEqual(self.chat.calls, [])

    def test_room_name_lists_that_rooms_waypoints(self) -> None:
        self.db.add(Room(project_id=self.project.id, name="Bedroom wing", slug="bedroom-wing"))
        for name in ["bedroom-1", "bedroom-2"]:
            self.add_point(name, room_slug="bedroom-wing")
        self.db.commit()
        self.parser.readings = {"go to the bedroom wing": {"intent": "go_to", "waypoints": ["the bedroom wing"]}}
        self.assertEqual(self.say("go to the bedroom wing"),
                         "Bedroom wing has 2 waypoints: bedroom-1 and bedroom-2. Which one do you mean?")
        self.assertEqual(self.say("bedroom-1"), "What should I capture at bedroom-1? Want me to list the options?")

    def test_core_requests_do_not_depend_on_the_model(self) -> None:
        # The parser returns "unknown" for everything here: the words alone must be enough.
        self.assertEqual(self.say("create a schedule"), "What should I call this schedule?")
        self.say("never mind")
        self.assertEqual(self.say("I want to set up a recurring capture"), "What should I call this schedule?")
        self.say("never mind")
        self.assertEqual(self.say("send the robot to the lobby"), "What should I capture at lobby? Want me to list the options?")
        self.say("never mind")
        self.assertEqual(self.say("take a photo of room2"), "Going to room2 and capturing a 360° image.")

    def test_repeating_requests_are_schedules_not_trips(self) -> None:
        for message in ["can you make the robot visit the corridor every weekday at 7am",
                        "photograph the lobby every morning", "send it to room1 on mondays",
                        "scan room3 daily", "go to room2 each evening"]:
            with self.subTest(message=message):
                self.state = None
                self.assertEqual(self.say(message), "What should I call this schedule?")
        self.assertEqual(self.missions(), [])
        self.state = None
        self.assertEqual(self.say("go to room2 now and take a photo"), "Going to room2 and capturing a 360° image.")

    def test_new_one_after_talking_about_schedules(self) -> None:
        from app.services import robot_assistant

        def say_with_history(message: str, history: list) -> str:
            result = robot_assistant.handle_message(
                db=self.db, user=self.operator, project_slug="demo", robot_id="robot-1", message=message,
                state=self.state, timezone="Asia/Dubai", robot_connected=True, voice=False,
                history=history, parser=self.parser, chatter=self.chat,
            )
            self.state = result.state
            return result.reply

        history = [{"role": "user", "text": "open the schedules tab"}, {"role": "assistant", "text": "Opened the Schedules tab."}]
        self.assertEqual(say_with_history("lets create a new one", history), "What should I call this schedule?")

    def test_a_model_guess_cannot_switch_tasks_mid_conversation(self) -> None:
        self.parser.readings = {
            "go to room1": {"intent": "go_to", "waypoints": ["room1"]},
            "8am": {"intent": "create_schedule", "time": "08:00"},
        }
        self.say("go to room1")
        reply = self.say("8am")
        self.assertEqual(self.state["intent"], "go_to")
        self.assertTrue(reply.endswith("What should I capture at room1: a 360° image, a point cloud, or both?"), reply)

    def finished_mission(self, status: str, step_statuses: list[str], errors: list[str | None] | None = None) -> RobotMission:
        from app.models import RobotMissionStep

        mission = RobotMission(
            robot_user_id=self.robot.id, robot_username=self.robot.username, project_id=self.project.id,
            requested_by_user_id=self.operator.id, status=status, capture_mode="panorama",
            capture_date=date(2026, 10, 22), waypoints_json=[], robot_meta_json={"capture_outputs": ["image"]},
        )
        self.db.add(mission)
        self.db.flush()
        for index, (name, step_status) in enumerate(zip(["room1", "room2", "room3"], step_statuses), start=1):
            self.db.add(RobotMissionStep(mission_id=mission.id, sequence_index=index, waypoint_name=name,
                                         status=step_status, error_message=(errors or [None] * 3)[index - 1]))
        self.db.commit()
        self.db.refresh(mission)
        return mission

    def announce(self, mission: RobotMission) -> str:
        from app.services import robot_assistant

        return robot_assistant.handle_message(
            db=self.db, user=self.operator, project_slug="demo", robot_id="robot-1", message="", state=None,
            timezone="Asia/Dubai", robot_connected=True, voice=False, event="mission_finished",
            mission_id=mission.id, parser=self.parser, chatter=self.chat,
        ).reply

    def test_finish_messages(self) -> None:
        self.assertEqual(self.announce(self.finished_mission("succeeded", ["succeeded"])),
                         "Done. The robot captured a 360° image at room1.")
        self.assertEqual(self.announce(self.finished_mission("succeeded", ["succeeded", "succeeded"])),
                         "Done. The robot visited room1 and room2 and captured 360° images at each stop.")
        self.assertEqual(self.announce(self.finished_mission("succeeded", ["succeeded", "failed"], [None, "blocked path"])),
                         "The capture finished, but 1 of 2 stops had problems: room2 (blocked path).")
        self.assertEqual(self.announce(self.finished_mission("cancelled", ["succeeded", "pending", "pending"])),
                         "The capture was cancelled after 1 of 3 stops.")
        self.assertEqual(self.announce(self.finished_mission("failed", ["failed"], ["navigation timed out"])),
                         "The capture failed at room1: navigation timed out")

    def test_is_it_done(self) -> None:
        mission = self.finished_mission("running", ["succeeded", "running", "pending"])
        self.assertEqual(self.say("is it done?"), "Not yet. The robot has finished 1 of 3 stops, currently at room2.")
        mission.status = "succeeded"
        for step in mission.steps:
            step.status = "succeeded"
        mission.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self.db.commit()
        reply = self.say("is it done?")
        self.assertTrue(reply.startswith("Done. The robot visited room1, room2 and room3"), reply)
        self.assertIn("That was just now. The robot is now idle and connected.", reply)
        self.assertEqual(self.chat.calls, [])

    def test_connection_events_are_announced(self) -> None:
        from app.services import robot_assistant

        def event(name: str, message: str = "") -> str:
            return robot_assistant.handle_message(
                db=self.db, user=self.operator, project_slug="demo", robot_id="robot-1", message=message,
                state=None, timezone="Asia/Dubai", robot_connected=True, voice=False, event=name,
                parser=self.parser, chatter=self.chat,
            ).reply

        self.assertEqual(event("robot_connected"), "The robot is connected now.")
        self.assertEqual(event("robot_disconnected"), "The robot is disconnected now.")
        self.assertEqual(event("robot_connect_failed", "ROS stack timed out"), "The robot couldn't connect: ROS stack timed out")

    def test_drafts_follow_the_conversation(self) -> None:
        from app.services.robot_assistant import draft_of

        self.parser.readings = {"create a schedule": {"intent": "create_schedule"}}
        self.say("create a schedule")
        self.assertEqual(draft_of(self.state)["kind"], "schedule")
        self.say("Night run")
        self.say("room1 and lobby")
        self.say("8am")
        draft = draft_of(self.state)
        self.assertEqual(draft["name"], "Night run")
        self.assertEqual(draft["point_ids"], [self.points["room1"].id, self.points["lobby"].id])
        self.assertEqual(draft["time"], "08:00")
        self.assertEqual(draft["focus"], "weekdays")
        self.say("never mind")
        self.assertIsNone(draft_of(self.state))

    def test_tab_and_request_in_one_message(self) -> None:
        reply = self.say("open schedules and create a new schedule named demo1")
        self.assertEqual(reply, "Opened the Schedules tab. Which waypoints should it visit, in order?")
        self.assertEqual(self.state["name"], "Demo1")
        self.say("never mind")
        reply = self.say("open the route tab and go to the lobby")
        self.assertEqual(reply, "Opened the Route tab. What should I capture at lobby? Want me to list the options?")
        self.say("never mind")
        self.assertEqual(self.say("open the history tab please"), "Opened the History tab.")
        self.assertEqual(self.last_action, {"type": "open_tab", "tab": "history"})

    def test_several_tabs_and_plural_names(self) -> None:
        cases = [
            ("open the routes tab then open schedules tab", "Opened the Route tab. Want me to open the Schedules tab next?", "route"),
            ("yes", "Opened the Schedules tab.", "schedules"),
            ("open history tab and shecdules tab", "Opened the History tab. Want me to open the Schedules tab next?", "history"),
            ("show me the live maps", "Opened the Live map tab.", "live"),
            ("open routes", "Opened the Route tab.", "route"),
            ("live map", "Opened the Live map tab.", "live"),
            ("histroy tab please", "Opened the History tab.", "history"),
        ]
        for message, reply, tab in cases:
            with self.subTest(message=message):
                self.assertEqual(self.say(message), reply)
                self.assertEqual(self.last_action, {"type": "open_tab", "tab": tab})
        self.assertEqual(self.parser.calls, [])
        self.chat.reply = "It lists past captures."
        self.assertEqual(self.say("what's in the history tab?"), "It lists past captures.")
        self.assertIsNone(self.last_action)

    def make_schedule(self, name: str, enabled: bool = True) -> RobotMissionSchedule:
        schedule = RobotMissionSchedule(
            name=name, robot_user_id=self.robot.id, robot_username=self.robot.username, project_id=self.project.id,
            requested_by_user_id=self.operator.id, enabled=enabled, timezone="Asia/Dubai", local_time="19:00",
            weekdays_json=list(range(7)), capture_point_ids_json=[self.points["room1"].id], capture_mode="panorama",
            retry_policy_json={}, robot_meta_json={"capture_outputs": ["image"]}, busy_policy="skip",
            auto_connect=True, max_lateness_minutes=30,
        )
        self.db.add(schedule)
        self.db.commit()
        return schedule

    def say_after(self, message: str, history_text: str) -> str:
        from app.services import robot_assistant

        result = robot_assistant.handle_message(
            db=self.db, user=self.operator, project_slug="demo", robot_id="robot-1", message=message,
            state=self.state, timezone="Asia/Dubai", robot_connected=True, voice=False,
            history=[{"role": "assistant", "text": history_text}], parser=self.parser, chatter=self.chat,
        )
        self.state, self.last_action = result.state, result.action
        return result.reply

    def test_the_reported_delete_conversation(self) -> None:
        self.make_schedule("Old one")
        demo = self.make_schedule("Demo")
        reply = self.say_after("del;ete it", 'Done, "Demo" is enabled. That schedule will run every day.')
        self.assertEqual(reply, 'Delete the schedule "Demo"? It can\'t be undone.')
        self.assertEqual(self.say("what?"), "Sorry, I didn't catch that. Should I delete \"Demo\"?")
        self.assertEqual(self.say("yes"), 'Deleted "Demo".')
        self.assertEqual(self.last_action, {"type": "schedule_deleted", "schedule_id": demo.id})
        self.assertEqual([s.name for s in self.db.scalars(select(RobotMissionSchedule)).all()], ["Old one"])
        self.assertEqual(self.missions(), [])

    def test_manage_schedules_by_name(self) -> None:
        self.make_schedule("Morning sweep")
        self.make_schedule("Night run", enabled=False)
        self.assertEqual(self.say("pause the morning sweep schedule"), 'Paused "Morning sweep". It won\'t run until you resume it.')
        self.assertEqual(self.say("stop the morning sweep schedule"), '"Morning sweep" is already paused.')
        self.assertEqual(self.say("turn the night run schedule back on"), 'Turned on "Night run". It runs every day at 19:00.')
        reply = self.say("run morning sweep now")
        self.assertEqual(reply, 'Running "Morning sweep" now: room1.')
        self.assertEqual(self.last_action["type"], "mission_started")
        self.assertEqual(self.say("delete the schedule"), "Which schedule? You have Morning sweep and Night run.")
        self.assertEqual(self.say("night run"), 'Delete the schedule "Night run"? It can\'t be undone.')
        self.assertEqual(self.say("no"), 'OK, I kept "Night run".')
        self.assertEqual(len(self.db.scalars(select(RobotMissionSchedule)).all()), 2)

    def test_model_stop_guess_needs_a_stop_word(self) -> None:
        mission = self.finished_mission("running", ["running"])
        self.parser.readings = {"delete it": {"intent": "stop"}}
        self.chat.reply = "What would you like me to delete?"
        self.assertEqual(self.say("delete it"), "What would you like me to delete?")
        self.db.refresh(mission)
        self.assertEqual(mission.status, "running")

    def test_connect_and_go_in_one_message_does_not_ask_again(self) -> None:
        reply = self.say("connect the robot and go to the lobby", connected=False)
        self.assertEqual(reply, "What should I capture at lobby? Want me to list the options?")
        reply = self.say("photo", connected=False)
        self.assertEqual(reply, "Connecting the robot. I'll go to lobby as soon as it's connected.")
        self.assertEqual([c.kind for c in self.db.scalars(select(RobotCommand)).all()], ["connect"])
        self.assertEqual(self.say("", connected=True, event="robot_connected"),
                         "The robot is connected. Going to lobby and capturing a 360° image.")

    def test_connect_and_schedule_in_one_message(self) -> None:
        reply = self.say("connect the robot and create a schedule called Night run", connected=False)
        self.assertEqual(reply, "Connecting the robot. It takes a moment to come online. Which waypoints should it visit, in order?")
        self.assertEqual(self.state["name"], "Night run")

    def test_schedule_names_said_in_the_request(self) -> None:
        from app.services.robot_assistant import _said_name

        for text, name in [("create a schedule named demo1", "Demo1"),
                           ("make a schedule called Morning sweep for room1 at 8am", "Morning sweep"),
                           ('set up a schedule called "Night run" on weekends', "Night run"),
                           ("create a schedule", None)]:
            with self.subTest(text=text):
                self.assertEqual(_said_name(text), name)

    def test_chat_knows_schedules_and_time(self) -> None:
        self.start_schedule_at("capture")
        self.say("photos")
        self.say("no")
        self.say("anything scheduled?")
        facts = self.chat.calls[-1][2]
        self.assertIn('Schedules: "Demo 1" on weekdays at 08:00, switched off.', facts)
        self.assertIn("Current local time:", facts)


class PayloadCapabilityTests(ConversationBase):
    """The assistant offers only the capture types whose payload the robot reports as working."""

    LIDAR_DOWN = "No point cloud arrived on /livox/lidar in 5 s."

    def report(self, *, camera: bool = True, lidar: bool = True) -> None:
        payloads = [
            {"id": "camera", "output": "image", "detected": camera, "name": "Insta360 X4",
             "detail": "USB 2e1a:0002" if camera else "The 360 camera isn't connected over USB (or isn't switched on)."},
            {"id": "lidar", "output": "pointcloud", "detected": lidar, "name": "LiDAR",
             "detail": "/livox/lidar: 20000 points per scan" if lidar else self.LIDAR_DOWN},
        ]
        self.db.add(RobotPresence(robot_user_id=self.robot.id, robot_username="robot-1",
                                  payload_json={"heartbeat": {"payloads": payloads}}))
        self.db.commit()

    def test_options_list_only_what_works_and_say_why(self) -> None:
        self.report(lidar=False)
        self.parser.readings = {"go to room1": {"intent": "go_to", "waypoints": ["room1"]}}
        self.assertEqual(self.say("go to room1"), "What should I capture at room1? Want me to list the options?")
        self.assertEqual(
            self.say("yes"),
            f"Right now I can only capture a 360° image. A point cloud isn't possible right now: {self.LIDAR_DOWN} "
            "Which one would you like?",
        )
        self.assertEqual(self.say("a photo"), "Going to room1 and capturing a 360° image.")

    def test_asking_for_a_missing_payload_offers_the_working_one(self) -> None:
        self.report(lidar=False)
        message = "go to room2 and grab a point cloud"
        self.parser.readings = {message: {"intent": "go_to", "waypoints": ["room2"]}}
        self.assertEqual(
            self.say(message),
            f"I can't take a point cloud right now: {self.LIDAR_DOWN} Should I capture a 360° image at room2 instead?",
        )
        self.assertEqual(self.missions(), [])
        self.assertEqual(self.say("yes"), "Going to room2 and capturing a 360° image.")
        [mission] = self.missions()
        self.assertEqual(mission.robot_meta_json["capture_outputs"], ["image"])

    def test_declining_the_offer_does_nothing(self) -> None:
        self.report(camera=False)
        message = "go to room2 and take a photo and a point cloud"
        self.parser.readings = {message: {"intent": "go_to", "waypoints": ["room2"]}}
        self.assertIn("I can't take a 360° image right now", self.say(message))
        self.assertEqual(self.say("no"), "OK, I'll leave it there.")
        self.assertIsNone(self.state)
        self.assertEqual(self.missions(), [])

    def test_nothing_working_means_no_capture(self) -> None:
        self.report(camera=False, lidar=False)
        self.parser.readings = {"go to room1": {"intent": "go_to", "waypoints": ["room1"]}}
        reply = self.say("go to room1")
        self.assertTrue(reply.startswith("The robot can't capture anything right now."), reply)
        self.assertIn(self.LIDAR_DOWN, reply)
        self.assertIsNone(self.state)
        self.assertEqual(self.missions(), [])

    def test_schedules_are_held_to_the_same_rule(self) -> None:
        self.report(lidar=False)
        message = "every monday at 8am scan room1 with lidar"
        self.parser.readings = {message: {"intent": "create_schedule", "waypoints": ["room1"], "time": "08:00",
                                          "weekdays": [0]}}
        reply = self.say(message)
        self.assertIn("I can't take a point cloud right now", reply)
        self.assertTrue(reply.endswith("Should I capture a 360° image at each stop instead?"), reply)
        self.assertEqual(list(self.db.scalars(select(RobotMissionSchedule)).all()), [])

    def test_capability_answer_and_chat_facts_follow_the_robot(self) -> None:
        self.report(lidar=False)
        reply = self.say("what can you do?")
        self.assertTrue(reply.startswith("I can send the robot to a waypoint to capture a 360° image, for example"), reply)
        self.chat.reply = "Hello!"
        self.parser.readings = {"hi": {"intent": "unknown"}}
        self.say("hi")
        facts = self.chat.calls[-1][2]
        self.assertIn("What the robot can capture right now: Right now I can only capture a 360° image.", facts)


class AssistantEndpointTests(unittest.TestCase):
    """One round trip through the real HTTP route, with auth and the database overridden."""

    def setUp(self) -> None:
        from fastapi.testclient import TestClient
        from sqlalchemy.pool import StaticPool

        from app.api.deps import get_current_user
        from app.database import get_db
        from app.main import app

        self.engine = create_engine(
            "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(self.engine)
        with Session(self.engine, expire_on_commit=False) as db:
            self.operator = User(username="operator", password_hash="x", is_admin=True)
            robot = User(username="robot-1", password_hash="x", is_robot=True)
            project = Project(name="Demo", slug="demo")
            db.add_all([self.operator, robot, project])
            db.flush()
            db.add(ProjectMember(project_id=project.id, user_id=robot.id, role="editor"))
            db.add(RobotCapturePoint(project_id=project.id, name="lobby", map_x=0.0, map_y=0.0))
            db.commit()

        def override_db():
            with Session(self.engine) as db:
                yield db

        self.app = app
        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = lambda: self.operator
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.app.dependency_overrides.clear()
        self.engine.dispose()

    def test_message_round_trip_creates_mission(self) -> None:
        from unittest import mock

        parser = FakeParser({"go to the lobby": {"intent": "go_to", "waypoints": ["lobby"]}})
        body = {"project_slug": "demo", "robot_id": "robot-1", "timezone": "Asia/Dubai", "robot_connected": True}
        with mock.patch("app.services.robot_assistant.parse_with_llm", parser):
            first = self.client.post("/api/robot/assistant/message", json={**body, "message": "go to the lobby"})
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json()["reply"], "What should I capture at lobby? Want me to list the options?")
            second = self.client.post(
                "/api/robot/assistant/message",
                json={**body, "message": "a point cloud", "state": first.json()["state"]},
            )
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["reply"], "Going to lobby and capturing a point cloud.")
        self.assertEqual(second.json()["action"]["type"], "mission_started")
        self.assertIsNone(second.json()["state"])
        with Session(self.engine) as db:
            self.assertEqual(len(db.scalars(select(RobotMission)).all()), 1)


if __name__ == "__main__":
    unittest.main()
