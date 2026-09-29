import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "validate_nas_catalog", ROOT / "tools" / "validate_nas_catalog.py")
catalog = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(catalog)


class NasCatalogTests(unittest.TestCase):
    def test_checked_in_catalog_is_valid(self):
        for path in sorted((ROOT / "drivers" / "catalog").glob("*.json")):
            if path.name != "schema.json":
                catalog.validate(path)

    def test_sensitive_identity_keys_are_rejected(self):
        source = ROOT / "drivers" / "catalog" / \
            "synology-ds1621plus-dsm-7.4.1-90080-k4.4.302plus-x86_64.json"
        record = json.loads(source.read_text())
        record["imei"] = "redacted"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / f"{record['id']}.json"
            path.write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, "forbidden sensitive keys"):
                catalog.validate(path)


if __name__ == "__main__":
    unittest.main()
