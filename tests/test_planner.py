from bot.services.outline_parser import validate_outline
from bot.services.planner import Draft, Op, apply_ops, build_plan, format_plan

# Transcribed from the plain-text extraction of tests/fixtures (NOT the lossy table view).
TOPICS12 = ["Substitution Method", "Integration by Parts", "Trigonometric Integrals", "Trigonometric Substitutions",
            "Integration by Partial Fractions", "Applications of Definite Integrals", "Improper Integrals",
            "Sequences and Sum of a Series", "Test for Divergence", "Comparison Test", "Absolute Convergence",
            "Power Series"]
MATH1920 = validate_outline({
    "course_name": "Single Variable Calculus II",
    "schedule": [{"week": i + 1, "topics": [t], "lab_or_tutorial_text": f"Worksheet {i + 1}"} for i, t in enumerate(TOPICS12)],
    "assessments": [
        {"name": "Quiz 1", "weight": "5%", "due": "14 October 2025"},
        {"name": "Assignment 1", "weight": "10%", "due": "28 October 2025"},
        {"name": "Exam 1", "weight": "15%", "due": "28 October 2025"},
        {"name": "Quiz 2", "weight": "5%"},
        {"name": "Assignment 2", "weight": "10%", "due": "25 November 2025"},
        {"name": "Exam 2", "weight": "15%"}, {"name": "Quiz 3", "weight": "5%"},
        {"name": "Assignment 3", "weight": "10%", "due": "9 December 2025"},
        {"name": "Final Exam", "weight": "30%", "due": "TBA"},
    ]})

MCS3320 = validate_outline({
    "schedule": [{"week": w, "topics": [f"topic {w}"]} for w in range(1, 13) if w != 5] + [{"week": 5, "topics": ["Midterm"]}],
    "assessments": [
        {"name": "Lecture Participation", "weight": "15%"}, {"name": "Quiz 1", "weight": "5%", "due": "Week3: Oct 15th"},
        {"name": "Assignment 1", "weight": "10%", "due": "Week4: Oct 22nd"}, {"name": "Midterm", "weight": "15%"},
        {"name": "Assignment 2", "weight": "10%", "due": "Week7: Nov 12th"}, {"name": "Quiz 2", "weight": "5%"},
        {"name": "Assignment 3", "weight": "10%", "due": "Week11: Dec 10th"}, {"name": "Final Exam", "weight": "30%"},
    ]})

CS4350 = validate_outline({
    "software": ["Visual Studio C++"], "language_hint": "C++/OpenGL",
    "schedule": [{"week": w, "topics": [f"t{w}"]} for w in (1, 2, 3, 4, 6, 7, 8, 9, 10)]
                + [{"week": 5, "topics": ["midterm"]}, {"week": 11, "topics": ["Final Project"]},
                   {"week": 12, "topics": ["Review and project delivery"]}],
    "assessments": [{"name": "Assignment 1", "due": "Week 3 Oct 15th"}, {"name": "Assignment 2", "due": "Week 6 Nov 5th"},
                    {"name": "Assignment 3", "due": "Week 9 Nov 26th"}, {"name": "Final Project", "weight": "10%"},
                    {"name": "Quiz 1"}, {"name": "Midterm"}, {"name": "Final Exam"}]})


def test_math1920_one_per_week_three_assignments():
    p = build_plan(MATH1920)
    t = [d for d in p if d.type == "tutorial"]
    assert [d.week for d in t] == list(range(1, 13)) and all(d.source == "outline" for d in t)
    a = [d for d in p if d.type == "assignment"]
    assert [d.title for d in a] == ["Assignment 1", "Assignment 2", "Assignment 3"]
    assert a[1].due_date == "25 November 2025" and a[1].source == "outline"
    assert not [d for d in p if d.type == "lab"]


def test_mcs3320_inferred_weekly_midterm_skipped():
    p = build_plan(MCS3320)
    weekly = [d for d in p if d.type != "assignment"]
    assert [d.week for d in weekly] == [1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12]  # 11 weeks, midterm week 5 skipped
    assert all(d.source == "inferred" and d.type == "tutorial" for d in weekly)  # no software => tutorial
    a = [d for d in p if d.type == "assignment"]
    assert [(d.title, d.week) for d in a] == [("Assignment 1", 4), ("Assignment 2", 7), ("Assignment 3", 11)]


def test_cs4350_labs_skip_exam_project_review_and_final_project_not_assignment():
    p = build_plan(CS4350)
    labs = [d for d in p if d.type == "lab"]
    assert [d.week for d in labs] == [1, 2, 3, 4, 6, 7, 8, 9, 10]
    assert [d.title for d in p if d.type == "assignment"] == ["Assignment 1", "Assignment 2", "Assignment 3"]


def test_merged_cells_inherit_topics():
    o = validate_outline({"schedule": [{"week": 1, "topics": ["Limits"]}, {"week": 2}, {"week": 3, "topics": ["Derivatives"]}]})
    w = [d for d in build_plan(o) if d.type != "assignment"]
    assert [d.week for d in w] == [1, 2, 3] and w[1].topics == ["Limits"]


def test_no_assignments_listed_defaults_to_three_inferred():
    o = validate_outline({"schedule": [{"week": w, "topics": ["x"]} for w in range(1, 13)]})
    a = [d for d in build_plan(o) if d.type == "assignment"]
    assert len(a) == 3 and all(d.source == "inferred" for d in a) and [d.week for d in a] == [3, 6, 9]


def test_apply_ops_uses_displayed_numbering():
    p = build_plan(MATH1920)
    new, log = apply_ops(p, [Op("remove", "tutorial", 2), Op("add", "tutorial", week=6, title="AVL trees"),
                             Op("move", "assignment", 1, week=9), Op("remove", "lab", 9)])
    t = [d for d in new if d.type == "tutorial"]
    assert len(t) == 12 and 2 not in [d.week for d in t if d.source == "outline"]
    assert [d.seq for d in t] == list(range(1, 13)) and any(d.source == "user" for d in t)
    assert [d for d in new if d.type == "assignment"][0].week == 9
    assert any("Could not find" in l for l in log)


def test_format_plan_chunks():
    big = [Draft("lab", "x" * 100, i, ["t" * 150], seq=i) for i in range(1, 60)]
    chunks = format_plan(big)
    assert len(chunks) > 1 and all(len(c) < 4096 for c in chunks)


def test_cs2920_two_sessions_per_week_grouped_assignments_quiz_in_lecture():
    """Real CS-2920 shape: 2 session rows per week, quiz/midterm inside lecture rows, 'Ass N' names, Java => lab."""
    sess = [(1, "Abstract Data Types"), (1, "Fundamental Data Structures"), (2, "Recursion"),
            (2, "Quiz 1 - in the beginning"), (2, "Stacks & Queues"), (3, "Trees"),
            (3, "Midterm Exam during lecture."), (3, "Trees (Cont'd)"), (4, "Sorting Techniques"), (4, "Priority Queues"),
            (5, "Quiz 2 - in the beginning"), (5, "Search Trees"), (6, "Graphs")]
    o = validate_outline({"software": ["Java"], "language_hint": "Java",
        "schedule": [{"week": w, "topics": [t], "lab_or_tutorial_text": "Further Exercises on related topic of the week."} for w, t in sess],
        "assessments": [{"name": "Ass 1", "week": 2}, {"name": "Ass 2", "week": 5}, {"name": "Ass 3", "week": 6},
                        {"name": "Quiz 1"}, {"name": "Midterm"}, {"name": "Final"}]})
    p = build_plan(o)
    labs = [d for d in p if d.type == "lab"]
    assert [d.week for d in labs] == [1, 2, 3, 4, 5, 6]            # exactly one per week despite 2 sessions/week
    assert labs[1].topics == ["Recursion", "Stacks & Queues"]      # quiz mention stripped, not a skipped week
    assert labs[2].topics == ["Trees", "Trees (Cont'd)"] and labs[0].title.startswith("Lab: Abstract")
    assert [d.title for d in p if d.type == "assignment"] == ["Ass 1", "Ass 2", "Ass 3"]


def test_cs4650_optional_labs_and_bare_project():
    rows = [(1, "Game Loop"), (2, "Game Objects"), (3, "Events"), (4, "Data driven design"), (5, "Midterm exam"),
            (6, "scripting"), (7, "State Machines"), (8, "Collision and Physics"), (9, "Game AI"), (10, "Navigation"),
            (11, "Content Pipeline"), (12, "Project delivery/presentation")]
    o = validate_outline({"software": ["Unreal Engine"], "schedule": [{"week": w, "topics": [t], "lab_or_tutorial_text":
        None if w in (5, 12) else f"Exercises on {t}"} for w, t in rows],
        "assessments": [{"name": "Assignment 1"}, {"name": "Quiz"}, {"name": "Midterm"}, {"name": "Assignment 2"},
                        {"name": "Assignment 3"}, {"name": "Project", "weight": "10%"}, {"name": "Final Exam"}]})
    p = build_plan(o)
    assert [d.week for d in p if d.type == "lab"] == [1, 2, 3, 4, 6, 7, 8, 9, 10, 11]
    assert len([d for d in p if d.type == "assignment"]) == 3


def test_cs3130_participation_column_is_labs_with_topic_titles():
    o = validate_outline({"software": ["Android Studio"], "language_hint": "Kotlin",
        "schedule": [{"week": w, "topics": [f"Topic {w}"], "lab_or_tutorial_text": "Participation"} for w in (1, 2, 3)],
        "assessments": [{"name": "Assignment 1"}, {"name": "Assignment 2"}, {"name": "Assignment 3"}, {"name": "Project", "weight": "44%"}]})
    p = build_plan(o)
    assert [(d.type, d.title) for d in p if d.type == "lab"][0] == ("lab", "Participation: Topic 1")
