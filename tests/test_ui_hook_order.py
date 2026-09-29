"""React hooks must run on every render, so none may follow a component's early return.

A hook placed after ``if (...) return`` runs only once the guard passes; the render that first
gets past it calls more hooks than the one before, and React replaces the page with its error
screen. The Settings page shipped that way once: the signed-in apps list loaded from a hook
written below the "still loading settings" return.
"""
import re
import unittest
from pathlib import Path

VIEWS = Path(__file__).resolve().parent.parent / "webui" / "src" / "views"
HOOK = re.compile(r"\buse[A-Z]\w*\(")
# A top-level statement of the component body (two-space indent) that returns early.
EARLY_RETURN = re.compile(r"^  if \(.*\) return\b")


def components(source: str):
    """(name, body lines) of each top-level function in a view file, exported or not."""
    starts = [(m.start(), m.group(1)) for m in
              re.finditer(r"^(?:export )?(?:default )?function (\w+)\(", source, re.M)]
    for index, (start, name) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(source)
        yield name, source[start:end].splitlines()


class HookOrderTests(unittest.TestCase):
    def test_no_hook_follows_an_early_return(self):
        for path in sorted(VIEWS.glob("*.jsx")):
            for name, lines in components(path.read_text(encoding="utf-8")):
                returned = False
                for line in lines:
                    if EARLY_RETURN.match(line):
                        returned = True
                    elif returned and line.startswith("  ") and not line.startswith("   ") \
                            and HOOK.search(line):
                        self.fail(f"{path.name}:{name} calls a hook after an early return: "
                                  f"{line.strip()[:80]}")


if __name__ == "__main__":
    unittest.main()
