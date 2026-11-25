"""Pisa's words, one place per concern: month and weekday names, number and date parsing, travel modes, the
language names and the not-advice line. Every module that needs one of these imports it from here.

Where callers accept different sets (one reads "sept", another does not), each keeps its own set, built
from these tables and named where it is used.

No I/O here."""
