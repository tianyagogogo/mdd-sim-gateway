import ast
import io
from pathlib import Path
import socket
import struct
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "engine" / "swu_ike.py"

ROUTE_HEADER = ("Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\t"
                "Window\tIRTT\n")
# An Engine on the internal Docker network behind a country exit: a subnet route, no default.
INTERNAL_ONLY = ROUTE_HEADER + "eth0\t000014AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
WITH_DEFAULT = ROUTE_HEADER + "eth0\t00000000\t010011AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"


def module_functions():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    wanted = {"get_default_gateway_linux", "get_default_source_address"}
    nodes = [node for node in tree.body
             if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace = {"socket": socket, "struct": struct, "subprocess": subprocess}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace


class SourceAddressTests(unittest.TestCase):
    """swu_ike.py computes the -s default while building its option parser, even when the
    entrypoint passes -s. With no default route that raised TypeError and the line never
    started (seen on the Pi, rc10, a line behind the GB country exit)."""

    def test_no_default_route_gives_no_default_instead_of_crashing(self):
        ns = module_functions()
        with patch("builtins.open", return_value=io.StringIO(INTERNAL_ONLY)):
            self.assertIsNone(ns["get_default_source_address"]())

    def test_a_default_route_still_yields_its_interface_address(self):
        ns = module_functions()

        class Proc:
            stdout = io.BytesIO(b"        inet 172.17.0.3  netmask 255.255.0.0\n")

        with patch("builtins.open", return_value=io.StringIO(WITH_DEFAULT)), \
                patch.object(subprocess, "Popen", return_value=Proc()) as popen:
            self.assertEqual(ns["get_default_source_address"](), "172.17.0.3")
        self.assertIn("grep -A 1 eth0", popen.call_args.args[0])

    def test_main_refuses_to_run_without_any_source_address(self):
        text = SOURCE.read_text(encoding="utf-8")
        self.assertIn("if not options.source_addr:\n        parser.error(", text)


    def test_set_routes_pins_the_epdg_only_when_there_is_a_gateway(self):
        """After this fix the tunnel came up behind the GB exit and then crashed in set_routes,
        pinning the ePDG host route through a default gateway the internal network lacks."""
        text = SOURCE.read_text(encoding="utf-8")
        start = text.index("    def set_routes(self):")
        body = text[start:text.index("\n    def ", start + 1)]
        self.assertNotIn("get_default_gateway_linux()[0]", body)
        self.assertIn("(self.get_default_gateway_linux() or [None])[0]", body)
        self.assertIn("ePDG host route not needed", body)

if __name__ == "__main__":
    unittest.main()
