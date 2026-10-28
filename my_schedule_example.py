"""Example semester for schedule.py. Copy this file to my_schedule.py (which is gitignored) and fill in your own classes."""

from datetime import date, datetime

SEMESTER = "Fall 2026"
SEMESTER_END = date(2026, 12, 11)                 # last day classes are shown
NEXT_SEMESTER_REMINDER = datetime(2027, 1, 10, 9, 0)   # ping to set up the next semester
HOLIDAYS = [                                      # (first day, last day, name): no classes
    (date(2026, 11, 25), date(2026, 11, 28), "Thanksgiving break"),
]
CLASSES = [                                       # (name, days, start, end)
    ("CS 101", "MWF", "10:30am", "11:20am"),      # days: MWF, TR, TTh, or mon,wed
    ("MA 261", "TR", "1:30pm", "2:45pm"),
    ("CS 101 Lab", "R", "3:30pm", "5:20pm"),
]
