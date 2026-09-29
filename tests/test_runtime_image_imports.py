"""Every repository module a runtime image imports is copied into that image.

v1.13.0-rc1 shipped an Egress image whose orchestrator imported host/modem_probe.py, which the
Dockerfile never copied: Egress died at import, never became healthy, and every container
update to that release rolled back. CI builds Control and Engine but not Egress or Hardware, so
nothing ran the image. This follows the imports from each image's entry point and checks them
against its COPY lines instead.
"""
import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# (Dockerfile, entry module) for the images CI does not build.
IMAGES = (("runtime/Dockerfile.egress", "runtime/egress.py"),
          ("runtime/Dockerfile.hardware", "runtime/hardware.py"))


def module_file(name: str, importer: Path) -> Path | None:
    """The repository file a module name refers to, or None for stdlib and third party."""
    candidates = [ROOT / (name.replace(".", "/") + ".py")]
    # A host script imported as a sibling ("import modem_probe") when run from host/.
    candidates.append(importer.parent / (name + ".py"))
    return next((path for path in candidates if path.is_file()), None)


def repository_imports(entry: Path) -> set[str]:
    """Repository files reachable by import from entry, as paths relative to the root."""
    seen, queue = set(), [entry]
    while queue:
        path = queue.pop()
        if path in seen:
            continue
        seen.add(path)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
            for name in names:
                found = module_file(name, path)
                if found is not None:
                    queue.append(found)
    return {str(path.relative_to(ROOT)) for path in seen}


def copied_sources(dockerfile: str) -> set[str]:
    text = (ROOT / dockerfile).read_text(encoding="utf-8")
    return {match.group(1) for match in re.finditer(r"^COPY (?:--\S+ )*(\S+) \S+$", text, re.M)}


class RuntimeImageImportTests(unittest.TestCase):
    def test_each_image_carries_what_its_entry_point_imports(self):
        for dockerfile, entry in IMAGES:
            with self.subTest(image=dockerfile):
                missing = repository_imports(ROOT / entry) - copied_sources(dockerfile)
                self.assertEqual(missing, set(), f"{dockerfile} does not copy {sorted(missing)}")

    def test_the_egress_import_chain_is_followed(self):
        # The case that broke: runtime/egress.py -> host/mdd_orchestrator.py -> modem_probe ->
        # vpcd_modem_bridge. If the walk stopped short, the check above would pass vacuously.
        self.assertTrue({"host/mdd_orchestrator.py", "host/modem_probe.py",
                         "host/vpcd_modem_bridge.py", "control/app/egress_contract.py"}
                        <= repository_imports(ROOT / "runtime/egress.py"))


if __name__ == "__main__":
    unittest.main()
