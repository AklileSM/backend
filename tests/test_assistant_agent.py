import json
import os
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Project, ProjectMember, RobotCapturePoint, RobotCommand, RobotMission, RobotMissionSchedule, User,
)
from app.services import robot_assistant as ra
from app.services import assistant_agent
from app.services.robot_assistant import handle_message


def tool(tool_name: str, /, **args) -> dict:
    return {"content": "", "tool_calls": [{"function": {"name": tool_name, "arguments": args}}]}


def text(reply: str) -> dict:
    return {"content": reply}


class ScriptedModel:
    """Stands in for the tool-calling model: answers each call from a script, and keeps what it was sent."""

    def __init__(self) -> None:
        self.script: list[dict | None] = []
        self.calls: list[list[dict]] = []

    def __call__(self, messages: list[dict], tools: list[dict]) -> dict | None:
        self.calls.append([dict(message) for message in messages])
        if not self.script:
            return text("(script ran out)")
        return self.script.pop(0)

    def tool_results(self) -> list[dict]:
        """What the code answered to the tool calls in the last model call."""
        return [json.loads(m["content"]) for m in self.calls[-1] if m["role"] == "tool"]


class ScheduleAgentTests(unittest.TestCase):
    """The model runs the conversation; the code checks every value and decides what gets created."""

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
        for name in ["room1", "lobby", "kitchen"]:
            self.db.add(RobotCapturePoint(project_id=self.project.id, name=name, map_x=0.0, map_y=0.0))
        self.db.commit()
        self.model = ScriptedModel()
        self.state = None
        self.result = None

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def say(self, message: str, *script: dict | None, voice: bool = False) -> str:
        self.model.script = list(script)
        self.result = handle_message(
            db=self.db, user=self.operator, project_slug="demo", robot_id="robot-1", message=message,
            state=self.state, timezone="Asia/Dubai", robot_connected=True, voice=voice,
            parser=lambda _message, _question: {"intent": "unknown"},
            chatter=lambda *_args: None, agent_model=self.model,
        )
        self.state = self.result.state
        return self.result.reply

    def schedules(self) -> list[RobotMissionSchedule]:
        return list(self.db.scalars(select(RobotMissionSchedule)).all())

    def propose_sketch(self) -> str:
        self.say("create a schedule called sketch", tool("set_fields", name="sketch"), text("Which waypoints?"))
        return self.say(
            "room 1 at 6pm on Monday and Friday, images",
            tool("set_fields", waypoints=["room 1"], time="18:00", weekdays=["Monday", "Friday"], capture=["image"]),
            tool("propose_schedule"),
        )

    # --- the happy path --------------------------------------------------------------------

    def test_conversation_reads_back_then_creates(self) -> None:
        self.assertEqual(self.say("create a schedule called sketch", tool("set_fields", name="sketch"),
                                  text("Which waypoints should it visit?")), "Which waypoints should it visit?")
        self.assertEqual(self.state["intent"], assistant_agent.INTENT)
        self.assertEqual(self.result.draft["name"], "Sketch")

        self.assertEqual(self.propose_sketch(), 'Here\'s the schedule: "Sketch": room1, at 18:00 on Monday and Friday, '
                                                "capturing 360° images. Shall I create it and switch it on?")
        self.assertEqual(self.schedules(), [])

        reply = self.say("yes", tool("create_schedule", enable=True))
        self.assertTrue(reply.startswith('Created "Sketch" and switched it on. It runs at 18:00 on Monday and Friday.'))
        schedule = self.schedules()[0]
        self.assertTrue(schedule.enabled)
        self.assertEqual((schedule.local_time, schedule.weekdays_json), ("18:00", [0, 4]))
        self.assertEqual(schedule.robot_meta_json["capture_outputs"], ["image"])
        self.assertEqual(self.result.action, {"type": "schedule_created", "schedule_id": schedule.id})
        self.assertIsNone(self.state)

    def test_kept_off_when_asked(self) -> None:
        self.propose_sketch()
        self.assertIn("switched off for now", self.say("create it but leave it off", tool("create_schedule", enable=False)))
        self.assertFalse(self.schedules()[0].enabled)

    def test_page_shows_the_draft_filling_in(self) -> None:
        self.say("create a schedule", tool("set_fields", name="Night", waypoints=["lobby", "kitchen"]), text("What time?"))
        draft = self.result.draft
        self.assertEqual((draft["kind"], draft["name"], draft["focus"]), ("schedule", "Night", "time"))
        self.assertEqual(len(draft["point_ids"]), 2)

    # --- what the code refuses -----------------------------------------------------------

    def test_create_without_reading_back_is_refused(self) -> None:
        self.say(
            "create a schedule called sketch at room1 6pm Monday images",
            tool("set_fields", name="sketch", waypoints=["room1"], time="18:00", weekdays=["Monday"], capture=["image"]),
            tool("create_schedule", enable=True),
            text("Let me check it with you first."),
        )
        self.assertIn("propose_schedule", self.model.tool_results()[-1]["error"])
        self.assertEqual(self.schedules(), [])

    def test_changing_the_schedule_after_the_read_back_needs_a_new_one(self) -> None:
        self.propose_sketch()
        self.say("make it 7pm", tool("set_fields", time="19:00"), tool("create_schedule", enable=True), text("OK."))
        self.assertIn("propose_schedule", self.model.tool_results()[-1]["error"])
        self.assertEqual(self.schedules(), [])

    def test_no_after_the_read_back_cannot_create(self) -> None:
        self.propose_sketch()
        self.say("no", tool("create_schedule", enable=True), text("What should I change?"))
        self.assertIn("said no", self.model.tool_results()[-1]["error"])
        self.assertEqual(self.schedules(), [])

    def test_bad_values_come_back_as_problems_and_stay_out_of_the_draft(self) -> None:
        self.say("create a schedule", tool("set_fields", name="sketch", waypoints=["main chat room"], time="7",
                                          weekdays=["Funday"]), text("Which waypoint?"))
        problems = " ".join(self.model.tool_results()[0]["problems"])
        self.assertIn('no waypoint called "main chat room"', problems)
        self.assertIn("07:00 or 19:00", problems)
        self.assertIn('"Funday" is not a day', problems)
        self.assertEqual(self.state["draft"]["name"], "Sketch")
        self.assertEqual((self.state["draft"]["waypoints"], self.state["draft"]["time"]), ([], None))

    def test_a_waypoint_on_an_older_map_is_refused_when_named(self) -> None:
        self.project.robot_map_json = {"map_version": 2}
        kitchen = self.db.scalars(select(RobotCapturePoint).where(RobotCapturePoint.name == "kitchen")).one()
        kitchen.metadata_json = {"map_version": 1}
        for point in self.db.scalars(select(RobotCapturePoint).where(RobotCapturePoint.name != "kitchen")):
            point.metadata_json = {"map_version": 2}
        self.db.commit()
        self.say("create a schedule", tool("set_fields", name="x", waypoints=["kitchen", "lobby"]), text("..."))
        self.assertIn("older robot map", self.model.tool_results()[0]["problems"][0])
        self.assertEqual(self.state["draft"]["waypoints"], [])

    def test_day_groups_are_checked_against_the_words(self) -> None:
        self.say("create a schedule called x that runs every weekday",
                 tool("set_fields", name="x", weekdays=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                                                       "Saturday", "Sunday"]),
                 text("..."))
        self.assertIn("which means weekdays, not every day", self.model.tool_results()[0]["problems"][0])
        self.assertEqual(self.state["draft"]["weekdays"], [])

    def test_saying_what_changed_in_the_draft_is_not_a_false_claim(self) -> None:
        self.say("create a schedule called x", tool("set_fields", name="x"), text("Which waypoints?"))
        reply = self.say("room1 and lobby", tool("set_fields", waypoints=["room1", "lobby"]),
                         text("I've added room1 and lobby. What time?"))
        self.assertEqual(reply, "I've added room1 and lobby. What time?")

    def test_a_bare_waypoint_is_not_taken_as_the_name(self) -> None:
        self.say("create a schedule", text("What should I call it?"))
        self.say("room 1", tool("set_fields", name="room 1"), text("..."))
        self.assertIn("is a waypoint", self.model.tool_results()[0]["problems"][0])
        self.say("create a schedule called kitchen", tool("set_fields", name="kitchen"), text("..."))
        self.assertEqual(self.state["draft"]["name"], "Kitchen")

    def test_existing_name_is_refused(self) -> None:
        self.propose_sketch()
        self.say("yes", tool("create_schedule", enable=True))
        self.say("create another schedule", tool("set_fields", name="Sketch"), text("That name is taken."))
        self.assertIn("already exists", self.model.tool_results()[0]["problems"][0])

    def test_unavailable_capture_is_refused(self) -> None:
        ctx = ra._Context(
            db=self.db, user=self.operator, project=self.project, robot_id="robot-1", timezone="UTC",
            robot_connected=True, voice=False, points=[], room_names={}, history=[], chatter=lambda *_: None,
            captures=[{"output": "image", "label": "360° image", "available": True, "reason": None},
                      {"output": "pointcloud", "label": "point cloud", "available": False,
                       "reason": "The LiDAR isn't sending data"}],
        )
        draft, result = assistant_agent.set_fields(ctx, assistant_agent._empty_draft(), {"capture": ["image", "pointcloud"]})
        self.assertEqual(draft["capture"], [])
        self.assertIn("The LiDAR isn't sending data.", result["problems"][0])

    def test_the_read_back_says_why_a_capture_type_was_dropped(self) -> None:
        no_camera = [{"output": "image", "label": "360° image", "available": False,
                      "reason": "The 360 camera isn't connected"},
                     {"output": "pointcloud", "label": "point cloud", "available": True, "reason": None}]
        with mock.patch.object(ra, "capture_capabilities", return_value=no_camera):
            reply = self.say(
                "create a schedule called sketch at room1 6pm Monday, both",
                tool("set_fields", name="sketch", waypoints=["room1"], time="18:00", weekdays=["Monday"],
                     capture=["image", "pointcloud"]),
                tool("set_fields", capture=["pointcloud"]),
                tool("propose_schedule"),
            )
        self.assertTrue(reply.startswith("The robot can't capture a 360° image right now: The 360 camera isn't "
                                         "connected. Here's the schedule:"), reply)
        self.assertIn("capturing point clouds", reply)

    def test_the_read_back_says_why_even_if_the_model_quietly_drops_it(self) -> None:
        no_camera = [{"output": "image", "label": "360° image", "available": False,
                      "reason": "The 360 camera isn't connected"},
                     {"output": "pointcloud", "label": "point cloud", "available": True, "reason": None}]
        with mock.patch.object(ra, "capture_capabilities", return_value=no_camera):
            reply = self.say(
                "create a schedule called sketch at room1 6pm Monday, both",
                tool("set_fields", name="sketch", waypoints=["room1"], time="18:00", weekdays=["Monday"],
                     capture=["pointcloud"]),
                tool("propose_schedule"),
            )
        self.assertTrue(reply.startswith("The robot can't capture a 360° image right now"), reply)

    def test_a_claimed_creation_is_replaced(self) -> None:
        reply = self.say("create a schedule called x", text("All done, your schedule is created!"))
        self.assertTrue(reply.startswith("I haven't created it yet."), reply)
        self.assertEqual(self.schedules(), [])

    def test_endless_tool_calls_end_the_turn(self) -> None:
        reply = self.say("create a schedule", *[tool("set_fields", name="x")] * 10)
        self.assertEqual(reply, "Which waypoints should it visit, in order?")
        self.assertEqual(len(self.model.calls), assistant_agent.MAX_MODEL_CALLS)

    # --- read-backs the code makes when the model doesn't -----------------------------------

    def test_a_complete_schedule_is_read_back_even_if_the_model_goes_quiet(self) -> None:
        reply = self.say(
            "create a schedule called sketch at room1 6pm Monday images",
            tool("set_fields", name="sketch", waypoints=["room1"], time="18:00", weekdays=["Monday"], capture=["image"]),
            text(""),
        )
        self.assertTrue(reply.startswith('Here\'s the schedule: "Sketch"'), reply)
        self.assertTrue(self.state["proposed"])

    def test_a_change_after_the_read_back_is_read_back_again(self) -> None:
        self.propose_sketch()
        reply = self.say("add Tuesday", tool("set_fields", weekdays=["Monday", "Tuesday", "Friday"]),
                         text("OK, I've added Tuesday."))
        self.assertIn("on Monday, Tuesday and Friday", reply)
        self.assertTrue(reply.startswith("Here's the schedule:"))
        self.say("yes", tool("create_schedule", enable=True))
        self.assertEqual(self.schedules()[0].weekdays_json, [0, 1, 4])

    def test_yes_creates_even_if_the_model_asks_to_read_back_again(self) -> None:
        self.propose_sketch()
        self.say("yes", tool("set_fields", weekdays=["Friday", "Monday"]), tool("propose_schedule"))
        self.assertEqual(len(self.schedules()), 1)
        self.assertEqual(self.result.action["type"], "schedule_created")

    def test_reading_back_again_without_a_yes_sends_the_model_back(self) -> None:
        self.propose_sketch()
        self.say("hmm", tool("propose_schedule"), text("Shall I go ahead?"))
        self.assertIn("already read back", self.model.tool_results()[-1]["error"])
        self.assertEqual(self.schedules(), [])

    def test_an_empty_answer_gets_one_more_try(self) -> None:
        self.say("create a schedule called x", text(""), tool("set_fields", name="x"), text("Which waypoints?"))
        self.assertEqual(self.result.reply, "Which waypoints?")
        self.assertEqual(self.model.calls[1][-1]["content"], assistant_agent.NUDGE)

    def test_a_side_question_after_the_read_back_keeps_it(self) -> None:
        self.propose_sketch()
        self.assertEqual(self.say("what's a point cloud?", text("A 3D scan of the room.")), "A 3D scan of the room.")
        self.say("ok yes", tool("create_schedule", enable=True))
        self.assertEqual(len(self.schedules()), 1)

    def test_a_tidied_up_time_is_checked_against_the_words(self) -> None:
        self.say("create a schedule called sketch", tool("set_fields", name="sketch"), text("Which waypoints?"))
        self.say("room1 at 7", tool("set_fields", waypoints=["room1"], time="07:00"), text("Morning or evening?"))
        self.assertIn("could be 07:00 or 19:00", self.model.tool_results()[0]["problems"][0])
        self.assertIsNone(self.state["draft"]["time"])
        self.say("in the morning", tool("set_fields", time="07:00"), text("Which days?"))
        self.assertEqual(self.state["draft"]["time"], "07:00")

    # --- around the model ------------------------------------------------------------------

    def test_stop_and_cancel_never_reach_the_model(self) -> None:
        for word in ["stop", "cancel"]:
            with self.subTest(word=word):
                self.say("create a schedule", tool("set_fields", name="x"), text("Which waypoints?"))
                calls = len(self.model.calls)
                self.assertEqual(self.say(word), "OK, cancelled.")
                self.assertEqual(len(self.model.calls), calls)
                self.assertIsNone(self.state)

    def test_cancel_tool_ends_the_conversation(self) -> None:
        self.say("create a schedule", tool("set_fields", name="x"), text("Which waypoints?"))
        self.assertEqual(self.say("forget about the schedule", tool("discard_draft")), "OK, I won't create it.")
        self.assertIsNone(self.state)

    def test_unreachable_model_carries_the_draft_into_the_rule_questions(self) -> None:
        self.say("create a schedule", tool("set_fields", name="sketch", waypoints=["room1"]), text("What time?"))
        self.assertEqual(self.say("6pm", None), "Which days should it run?")
        self.assertEqual(self.state["intent"], "create_schedule")
        self.assertEqual((self.state["name"], self.state["time"]), ("Sketch", "18:00"))

    def test_remove_a_day_is_about_the_draft_not_a_saved_schedule(self) -> None:
        self.propose_sketch()
        self.say("yes", tool("create_schedule", enable=True))
        self.say("create a schedule called patrol", tool("set_fields", name="patrol"), text("Which waypoints?"))
        self.say("remove it from Tuesday", text("It isn't on Tuesday yet. Which days should it run?"))
        self.assertEqual(len(self.schedules()), 1)
        self.assertEqual(self.state["intent"], assistant_agent.INTENT)

    def test_off_by_default(self) -> None:
        self.assertEqual(assistant_agent.mode(), "off")


class AllModeTests(ScheduleAgentTests):
    """ASSISTANT_AGENT=all: the model answers everything, through tools that hand over to the coded paths.
    The schedule conversations inherited from ScheduleAgentTests run again in this mode."""

    def setUp(self) -> None:
        super().setUp()
        self.connected = True

    def say(self, message: str, *script: dict | None, voice: bool = False) -> str:
        self.model.script = list(script)
        self.result = handle_message(
            db=self.db, user=self.operator, project_slug="demo", robot_id="robot-1", message=message,
            state=self.state, timezone="Asia/Dubai", robot_connected=self.connected, voice=voice,
            parser=lambda _message, _question: None, chatter=lambda *_args: None,
            agent_model=self.model, agent_mode="all",
        )
        self.state = self.result.state
        return self.result.reply

    def add_schedule(self, name: str = "Night patrol") -> RobotMissionSchedule:
        self.say(f"create a schedule called {name}",
                 tool("set_fields", name=name, waypoints=["room1"], time="18:00", weekdays=["Monday"], capture=["image"]),
                 tool("propose_schedule"))
        self.say("yes", tool("create_schedule", enable=True))
        return self.db.scalars(select(RobotMissionSchedule).where(RobotMissionSchedule.name == name)).one()

    def missions(self) -> list[RobotMission]:
        return list(self.db.scalars(select(RobotMission)).all())

    # --- trips -----------------------------------------------------------------------------

    def test_trip_starts_through_the_coded_path(self) -> None:
        reply = self.say("go to the kitchen and take a photo",
                         tool("start_trip", waypoints=["kitchen"], capture=["image"]))
        self.assertEqual(reply, "Going to kitchen and capturing a 360° image.")
        self.assertEqual(self.result.action["type"], "mission_started")
        self.assertEqual(len(self.missions()), 1)

    def test_trip_to_an_unknown_waypoint_is_a_problem(self) -> None:
        self.say("go to the roof", tool("start_trip", waypoints=["roof"]), text("There's no roof waypoint."))
        self.assertIn('no waypoint called "roof"', self.model.tool_results()[0]["problems"][0])
        self.assertEqual(self.missions(), [])

    def test_coded_questions_are_answered_without_the_model(self) -> None:
        self.say("go to the kitchen", tool("start_trip", waypoints=["kitchen"]))
        self.assertEqual(self.state["awaiting"], "capture")
        calls = len(self.model.calls)
        self.assertEqual(self.say("Bose"), "Going to kitchen and capturing a 360° image and a point cloud.")
        self.assertEqual(len(self.model.calls), calls)

    def test_offline_trip_asks_to_connect_then_connects(self) -> None:
        self.connected = False
        reply = self.say("go to the lobby and scan it", tool("start_trip", waypoints=["lobby"], capture=["pointcloud"]))
        self.assertEqual(reply, "The robot isn't connected. Should I connect it?")
        self.assertIn("Connecting the robot", self.say("yes"))
        self.assertEqual([c.kind for c in self.db.scalars(select(RobotCommand))], ["connect"])
        self.assertEqual(self.state["awaiting"], "connect_wait")

    def test_something_else_instead_of_answering_goes_to_the_model(self) -> None:
        self.say("go to the kitchen", tool("start_trip", waypoints=["kitchen"]))
        self.assertEqual(self.say("tell me a joke", text("Why did the beam blush? It saw the load.")),
                         "Why did the beam blush? It saw the load.")
        self.assertIsNone(self.state)
        self.assertEqual(self.missions(), [])

    # --- saved schedules -------------------------------------------------------------------

    def test_delete_is_confirmed_by_the_code(self) -> None:
        self.add_schedule()
        reply = self.say("delete the night patrol", tool("manage_schedule", action="delete", schedule="night patrol"))
        self.assertEqual(reply, "Delete the schedule \"Night patrol\"? It can't be undone.")
        self.assertEqual(len(self.schedules()), 1)
        self.assertEqual(self.say("yes"), "Deleted \"Night patrol\".")
        self.assertEqual(self.schedules(), [])

    def test_a_misheard_schedule_name_is_matched(self) -> None:
        schedule = self.add_schedule()
        self.say("pause the knight patrol", tool("manage_schedule", action="pause", schedule="knight patrol"))
        self.db.refresh(schedule)
        self.assertFalse(schedule.enabled)

    def test_an_unknown_schedule_changes_nothing(self) -> None:
        schedule = self.add_schedule()
        self.say("pause the roof check", tool("manage_schedule", action="pause", schedule="roof check"),
                 text("There's no roof check schedule."))
        self.assertIn("no schedule called \"roof check\"", self.model.tool_results()[0]["error"])
        self.db.refresh(schedule)
        self.assertTrue(schedule.enabled)

    def test_the_model_sees_the_robot_and_its_schedules(self) -> None:
        self.add_schedule()
        self.say("anything scheduled?", text("Night patrol runs on Mondays."))
        facts = self.model.calls[-1][0]["content"]
        self.assertIn("Schedules (1): Night patrol (at 18:00 on Monday, on)", facts)
        self.assertIn("Robot: robot-1, connected.", facts)

    # --- instant paths ---------------------------------------------------------------------

    def test_stop_and_plain_cancel_never_wait_for_the_model(self) -> None:
        for message in ["stop", "cancel"]:
            with self.subTest(message=message):
                self.say(message)
                self.assertEqual(self.model.calls, [])

    def test_a_longer_cancel_goes_to_the_model(self) -> None:
        self.add_schedule()
        calls = len(self.model.calls)
        self.say("cancel the night patrol schedule", tool("manage_schedule", action="delete", schedule="Night patrol"))
        self.assertGreater(len(self.model.calls), calls)
        self.assertEqual(self.state["awaiting"], "confirm_delete")

    # --- conversation ----------------------------------------------------------------------

    def test_chat_leaves_no_state_and_no_draft(self) -> None:
        self.assertEqual(self.say("hi", text("Hi! What can I do for you?")), "Hi! What can I do for you?")
        self.assertIsNone(self.state)
        self.assertIsNone(self.result.draft)

    def test_a_claimed_action_in_chat_is_replaced(self) -> None:
        reply = self.say("tidy the site", text("Sure, I've sent the robot to the kitchen."))
        self.assertTrue(reply.startswith("I haven't done anything yet."))
        self.assertEqual(self.missions(), [])

    def test_open_tab(self) -> None:
        self.assertEqual(self.say("show me the history", tool("open_tab", tab="history"), text("")),
                         "Opened the History tab.")
        self.assertEqual(self.result.action, {"type": "open_tab", "tab": "history"})

    def test_unreachable_model_falls_back_to_the_rules(self) -> None:
        reply = self.say("go to the kitchen and take a photo", None)
        self.assertEqual(reply, "Going to kitchen and capturing a 360° image.")
        self.assertEqual(len(self.missions()), 1)

    def test_a_question_instead_of_an_answer_is_answered_and_the_question_put_again(self) -> None:
        self.say("go to the kitchen", tool("start_trip", waypoints=["kitchen"]))
        reply = self.say("what's a point cloud?", text("A 3D scan of the room. Want one?"))
        self.assertEqual(reply, "A 3D scan of the room. What should I capture at kitchen: a 360° image, "
                                "a point cloud, or both?")
        self.assertEqual(self.missions(), [])
        self.assertEqual(self.say("the point cloud then"), "Going to kitchen and capturing a point cloud.")

    def test_an_answer_naming_no_capture_goes_to_the_model(self) -> None:
        self.say("go to the kitchen", tool("start_trip", waypoints=["kitchen"]))
        reply = self.say("ok that one", tool("start_trip", waypoints=["kitchen"], capture=["pointcloud"]))
        self.assertEqual(reply, "Going to kitchen and capturing a point cloud.")

    # Inherited checks that work differently in this mode.

    def test_a_claimed_creation_is_replaced(self) -> None:
        reply = self.say("create a schedule called x", text("All done, your schedule is created!"))
        self.assertTrue(reply.startswith("I haven't done anything yet."), reply)
        self.assertEqual(self.schedules(), [])

    def test_stop_and_cancel_never_reach_the_model(self) -> None:
        self.say("create a schedule", tool("set_fields", name="x"), text("Which waypoints?"))
        calls = len(self.model.calls)
        self.assertEqual(self.say("cancel"), "OK, cancelled.")
        self.assertEqual(len(self.model.calls), calls)

    def test_unreachable_model_carries_the_draft_into_the_rule_questions(self) -> None:
        self.say("create a schedule", tool("set_fields", name="sketch", waypoints=["room1"]), text("What time?"))
        self.assertEqual(self.say("6pm", None), "Which days should it run?")
        self.assertEqual((self.state["name"], self.state["time"]), ("Sketch", "18:00"))


if __name__ == "__main__":
    unittest.main()
