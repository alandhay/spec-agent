"""What the agent remembers about one person from one conversation to the next.

A short list of notes in memory.json in the person's folder, shared by all their
projects. The agent adds and removes notes with its remember and forget tools;
every new conversation starts with the notes in its instructions. The person can
see the notes on the page and remove any of them.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

MAX_NOTES = 40
MAX_CHARS = 300


class Memory:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def notes(self) -> list[dict]:
        """Each note as {"text", "at"}, oldest first."""
        try:
            notes = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [n for n in notes if isinstance(n, dict) and isinstance(n.get("text"), str)] if isinstance(notes, list) else []

    def _save(self, notes: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        part = self.path.with_suffix(".part")
        part.write_text(json.dumps(notes), encoding="utf-8")
        part.replace(self.path)

    def add(self, text: str) -> str:
        """Keep a note; the answer is worded for the agent."""
        text = " ".join(str(text).split())
        if not text:
            return "Nothing to remember: the note was empty."
        if len(text) > MAX_CHARS:
            return f"That note is too long ({len(text)} characters); keep it under {MAX_CHARS}."
        with self._lock:
            notes = self.notes()
            if any(n["text"] == text for n in notes):
                return "That is already remembered."
            if len(notes) >= MAX_NOTES:
                return (f"Memory is full ({MAX_NOTES} notes). Forget one that no longer matters first; "
                        "the notes are listed in your instructions.")
            notes.append({"text": text, "at": time.time()})
            self._save(notes)
        return f"Remembered as note {len(notes)}."

    def forget(self, number: int) -> str:
        """Remove note ``number`` (counting from 1)."""
        with self._lock:
            notes = self.notes()
            if not 1 <= number <= len(notes):
                return f"There is no note {number}; there are {len(notes)}."
            gone = notes.pop(number - 1)
            self._save(notes)
        return f"Forgot note {number}: {gone['text']}"

    def prompt(self) -> str:
        """The section added to the agent's instructions at the start of a conversation."""
        notes = self.notes()
        if not notes:
            return ("\n\n## What you remember\n\nNothing yet. Use the remember tool for anything worth knowing "
                    "next time (see Memory above).")
        lines = "\n".join(f"{i}. {n['text']}" for i, n in enumerate(notes, 1))
        return (f"\n\n## What you remember\n\nNotes you kept in earlier conversations with this person "
                f"(forget a note by its number when it is wrong or out of date):\n\n{lines}")
