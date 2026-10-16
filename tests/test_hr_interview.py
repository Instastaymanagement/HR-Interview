import datetime as dt
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import hr_interview as h  # noqa: E402

COLS = {"Phone": "phone", "Hiring Stage": "stage", "Quo Last Message": "track", "Screening Notes": "notes",
        "Car / Licence": "car"}
LINES = {"Canada": "PN_CA", "USA": "PN_US"}
# Thu 2026-10-15 14:00 in Toronto
NOON = dt.datetime(2026, 10, 15, 14, 0, tzinfo=h.ZoneInfo("America/Toronto")).timestamp()
NIGHT = dt.datetime(2026, 10, 15, 23, 0, tzinfo=h.ZoneInfo("America/Toronto")).timestamp()
GROUP = "Part-Time Cleaner – London ON (posted Oct 14, 2026)"


def cand(**kw):
    base = {"id": 7, "name": "Tish Kroetsch", "group": GROUP, "phone": "+12269774436", "stage": "Shortlist – Call",
            "tracking": "", "last_in": 0.0, "sent": 0, "notes": "", "location": "London, ON", "experience": "10 yrs",
            "flags": "Confirm car", "car": "", "country": "Canada"}
    base.update(kw)
    return base


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class HelperTests(unittest.TestCase):
    def test_phone(self):
        self.assertEqual(h.digits_phone("+1 (226) 700-9127"), "+12267009127")
        self.assertEqual(h.digits_phone("2267009127"), "+12267009127")
        self.assertEqual(h.digits_phone(""), "")

    def test_vacancy_country_and_role(self):
        self.assertEqual(h.vacancy_country(GROUP), "Canada")
        self.assertEqual(h.vacancy_country("Cleaner – Boston MA"), "USA")
        self.assertEqual(h.role_name(GROUP), "Part-Time Cleaner")

    def test_tracking_round_trip(self):
        value = h.tracking_value(NOON, 3)
        self.assertEqual(h.parse_tracking(value), (float(int(NOON)), 3))
        self.assertEqual(h.parse_tracking(""), (0.0, 0))

    def test_send_hours(self):
        self.assertTrue(h.within_send_hours("Canada", NOON))
        self.assertFalse(h.within_send_hours("Canada", NIGHT))

    def test_role_info_matches_group(self):
        roles = {"Part-Time Cleaner – London ON": {"title": "Cleaner"}, "Cleaner": {"title": "x"}}
        self.assertEqual(h.role_info(roles, GROUP), {"title": "Cleaner"})
        self.assertEqual(h.role_info(roles, "Handyman – Boston MA"), {})

    def test_roles_file_is_valid(self):
        roles = h.load_roles(h.ROLES_FILE)
        self.assertIn("must_know", h.role_info(roles, GROUP))

    def test_should_screen(self):
        self.assertTrue(h.should_screen(cand()))
        self.assertTrue(h.should_screen(cand(stage="Screening", tracking="in=x;sent=1")))
        self.assertFalse(h.should_screen(cand(stage="Maybe")))
        self.assertFalse(h.should_screen(cand(stage="Not a Fit")))
        self.assertFalse(h.should_screen(cand(stage="Interview Booked", tracking="in=x;sent=2")))
        self.assertFalse(h.should_screen(cand(stage="Good Fit", tracking="in=x;sent=2")))


class CellTextTests(unittest.TestCase):
    labels = {"stage": {"1": "Shortlist – Call", "7": "Contacted"}}

    def test_battery_status(self):
        self.assertEqual(h.cell_text({"id": "stage", "text": "", "battery_value": [{"key": "1", "count": 1}]}, self.labels),
                         "Shortlist – Call")
        self.assertEqual(h.cell_text({"id": "stage", "text": "", "battery_value": []}, self.labels), "")

    def test_plain_and_phone(self):
        self.assertEqual(h.cell_text({"id": "x", "text": " London "}, {}), "London")
        self.assertEqual(h.cell_text({"id": "p", "text": "", "phone": "+12267009127"}, {}), "+12267009127")


class HandleCandidateTests(unittest.TestCase):
    def run_handle(self, c, msgs, result, now=NOON):
        calls = {"sent": [], "updates": [], "posts": []}
        report = []
        with mock.patch.object(h, "thread", return_value=msgs), \
             mock.patch.object(h, "screen", return_value=result), \
             mock.patch.object(h, "send_text", side_effect=lambda line, phone, text: calls["sent"].append((line, phone, text))), \
             mock.patch.object(h, "update_candidate", side_effect=lambda *a, **k: calls["updates"].append(k)), \
             mock.patch.object(h, "post_update", side_effect=lambda i, b: calls["posts"].append(b)):
            h.handle_candidate(c, {}, LINES, COLS, None, now, report)
        return calls, report

    def test_first_contact(self):
        calls, _ = self.run_handle(cand(), [], {"reply": "Hi Tish, ...", "decision": "continue", "notes": "", "car": "unknown"})
        self.assertEqual(calls["sent"], [("PN_CA", "+12269774436", "Hi Tish, ...")])
        self.assertEqual(calls["updates"][0]["stage"], "Contacted")
        self.assertEqual(h.parse_tracking(calls["updates"][0]["tracking"])[1], 1)

    def test_no_new_reply_does_nothing(self):
        c = cand(stage="Contacted", tracking=h.tracking_value(NOON - 600, 1), last_in=NOON - 600, sent=1)
        msgs = [{"id": "a", "at": NOON - 600, "from_us": True, "text": "Hi"}]
        calls, _ = self.run_handle(c, msgs, None)
        self.assertEqual(calls, {"sent": [], "updates": [], "posts": []})

    def test_reply_continues_screening(self):
        c = cand(stage="Contacted", tracking=h.tracking_value(NOON - 600, 1), last_in=NOON - 600, sent=1)
        msgs = [{"id": "a", "at": NOON - 600, "from_us": True, "text": "Hi"},
                {"id": "b", "at": NOON - 60, "from_us": False, "text": "Yes I have a car"}]
        calls, _ = self.run_handle(c, msgs, {"reply": "Great, which days?", "decision": "continue", "notes": "n", "car": "yes"})
        self.assertEqual(calls["sent"][0][2], "Great, which days?")
        up = calls["updates"][0]
        self.assertEqual(up["stage"], "Screening")
        self.assertEqual(up["car"], "Confirmed by text: has car")
        self.assertEqual(h.parse_tracking(up["tracking"]), (float(int(NOON - 60)), 2))
        self.assertEqual(calls["posts"], [])

    def test_decision_posts_update(self):
        c = cand(stage="Screening", tracking=h.tracking_value(NOON - 600, 3), last_in=NOON - 600, sent=3)
        msgs = [{"id": "b", "at": NOON - 60, "from_us": False, "text": "Weekends only"}]
        calls, report = self.run_handle(c, msgs, {"reply": "Thanks! We'll be in touch.", "decision": "good_fit",
                                                  "notes": "Has car, weekdays ok.", "car": "yes"})
        self.assertEqual(calls["updates"][0]["stage"], "Good Fit")
        self.assertIn("good fit", calls["posts"][0])
        self.assertIn("notes: Has car", report[0])

    def test_quiet_hours_wait(self):
        calls, _ = self.run_handle(cand(), [], {"reply": "x", "decision": "continue", "notes": "", "car": "unknown"}, now=NIGHT)
        self.assertEqual(calls["sent"], [])
        self.assertEqual(calls["updates"], [])

    def test_person_already_answered(self):
        c = cand(stage="Screening", tracking=h.tracking_value(NOON - 900, 2), last_in=NOON - 900, sent=2)
        msgs = [{"id": "b", "at": NOON - 600, "from_us": False, "text": "Question?"},
                {"id": "c", "at": NOON - 300, "from_us": True, "text": "Ivy answered"}]
        calls, _ = self.run_handle(c, msgs, None)
        self.assertEqual(calls["sent"], [])
        self.assertEqual(h.parse_tracking(calls["updates"][0]["tracking"]), (float(int(NOON - 600)), 2))


class CloseVacancyTests(unittest.TestCase):
    def test_thanks_only_texted_candidates(self):
        members = [cand(id=1, name="Hired Person", stage="Hired", tracking="in=;sent=3"),
                   cand(id=2, name="Lucia Orsaia", stage="Good Fit", tracking="in=;sent=3"),
                   cand(id=3, name="Never Texted", stage="Shortlist – Call"),
                   cand(id=4, name="Already Closed", stage="Not Selected", tracking="in=;sent=4")]
        sent, updates, report = [], [], []
        with mock.patch.object(h, "send_text", side_effect=lambda l, p, t: sent.append(t)), \
             mock.patch.object(h, "update_candidate", side_effect=lambda *a, **k: updates.append((a[1], k))), \
             mock.patch.object(h, "post_update"):
            h.close_vacancy(GROUP, members, COLS, LINES, NOON, report)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].startswith("Hi Lucia, thank you for your interest in the Part-Time Cleaner role"))
        self.assertEqual(updates[0][0], 2)
        self.assertEqual(updates[0][1]["stage"], "Not Selected")


class SchemaTests(unittest.TestCase):
    def test_schema_strict(self):
        self.assertFalse(h.OUTPUT_SCHEMA["additionalProperties"])
        self.assertEqual(set(h.OUTPUT_SCHEMA["required"]), set(h.OUTPUT_SCHEMA["properties"]))
        json.dumps(h.OUTPUT_SCHEMA)


if __name__ == "__main__":
    unittest.main()
