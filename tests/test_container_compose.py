from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class ContainerComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compose = yaml.safe_load((ROOT / "runtime" / "compose.yaml").read_text())

    def test_three_base_services_and_fixed_runtime_names(self):
        self.assertEqual(set(self.compose["services"]), {"control", "hardware", "egress"})
        for component, service in self.compose["services"].items():
            self.assertEqual(service["container_name"], f"mdd-sim-gateway-{component}")
            self.assertEqual(service["labels"]["io.mdd-sim-gateway.managed"], "true")
            self.assertEqual(service["labels"]["io.mdd-sim-gateway.component"], component)
        self.assertEqual(self.compose["networks"]["engine"]["name"],
                         "mdd-sim-gateway-engine")
        self.assertTrue(self.compose["networks"]["engine"]["internal"])
        self.assertEqual(self.compose["volumes"]["pcscd"]["name"],
                         "mdd-sim-gateway-pcscd")

    def test_release_images_use_one_required_versioned_ghcr_tag(self):
        marker = "${MDD_IMAGE_TAG:?set a release tag such as v1.12.0}"
        for component, service in self.compose["services"].items():
            self.assertEqual(
                service["image"],
                f"ghcr.io/mddidd/mdd-sim-gateway-{component}:{marker}")
        self.assertEqual(
            self.compose["services"]["control"]["environment"]["MDD_ENGINE_IMAGE"],
            f"ghcr.io/mddidd/mdd-sim-gateway-engine:{marker}")
        self.assertNotIn("issue-108-dev", (ROOT / "runtime" / "compose.yaml").read_text())

    def test_host_https_defaults_to_nonstandard_10443_but_internal_port_stays_8443(self):
        control = self.compose["services"]["control"]
        self.assertEqual(control["ports"], ["${MDD_HTTP_PORT:-10443}:8443"])
        self.assertEqual(control["environment"]["MDD_HTTP_PORT"], 8443)

    def test_hardware_networkmanager_is_bounded_to_cellular_interfaces(self):
        hardware = self.compose["services"]["hardware"]
        self.assertEqual(hardware["network_mode"], "host")
        self.assertEqual(set(hardware.get("cap_add", [])),
                         {"SETUID", "SETGID", "DAC_OVERRIDE", "KILL",
                          "NET_ADMIN", "NET_RAW", "SYS_ADMIN"})
        self.assertEqual(hardware["security_opt"], ["apparmor:unconfined"])
        self.assertFalse(hardware.get("privileged", False))
        self.assertNotIn("ports", hardware)
        dockerfile = (ROOT / "runtime" / "Dockerfile.hardware").read_text()
        # ww* rather than wwan*: systemd's predictable names (wws27u1i4) start with ww.
        rule = ("unmanaged-devices=*,except:interface-name:ww*;"
                "except:interface-name:cdc-wdm*")
        self.assertIn(rule, dockerfile)
        self.assertIn(rule, (ROOT / "runtime" / "Dockerfile.hardware-overlay").read_text())
        self.assertIn("no-auto-default=*", dockerfile)
        self.assertIn('"reconcile_error" not in d', dockerfile)
        self.assertIn("hardware-dbus:/run/dbus:ro",
                      self.compose["services"]["control"]["volumes"])
        self.assertIn("/sys/devices:/sys/devices:rw", hardware["volumes"])
        self.assertNotIn("/sys:/sys:rw", hardware["volumes"])

    def test_hardware_owns_physical_usb_readers_inside_the_container(self):
        hardware = self.compose["services"]["hardware"]
        self.assertIn("c 189:* rwm", hardware["device_cgroup_rules"])
        dockerfile = (ROOT / "runtime" / "Dockerfile.hardware").read_text()
        self.assertIn("ARG CCID_VERSION=1.6.2", dockerfile)
        self.assertIn("-Dlibudev=false -Dlibusb=true", dockerfile)
        self.assertIn("01_hsic_slot_status.patch", dockerfile)
        self.assertIn("02_hsic_malformed_atr.patch", dockerfile)
        self.assertIn("03_scr_prime_reader.patch", dockerfile)

    def test_control_packages_the_patched_lpac_client(self):
        dockerfile = (ROOT / "runtime" / "Dockerfile.control-overlay").read_text()
        self.assertIn("ARG LPAC_COMMIT=c2fcf5e4b21c712d54e35a11da2ad9ad134fb821", dockerfile)
        self.assertIn("01_pcsc_reader_selection.patch", dockerfile)
        self.assertIn("COPY --from=lpac-build", dockerfile)
        self.assertIn("COPY webui/dist/ /app/webui/dist/", dockerfile)
        self.assertIn("host/mdd_container_update.py", dockerfile)
        self.assertIn("docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose",
                      dockerfile)
        self.assertIn("HEALTHCHECK --interval=30s", dockerfile)
        # Docker before 25 fails the whole build on this flag; install.sh builds with the
        # host's Docker (Debian 12 ships 20.10, DSM 24).
        self.assertNotRegex(dockerfile, r"HEALTHCHECK[^\n]*--start-interval")

    def test_an_operator_owned_data_directory_can_be_used(self):
        """A folder created in File Station belongs to the operator's account with mode
        0700. Control and Egress drop every capability, so root inside them could neither
        enter it nor chmod it, and a fresh install failed. Only these are added back."""
        services = self.compose["services"]
        self.assertEqual(services["control"]["cap_drop"], ["ALL"])
        self.assertEqual(set(services["control"]["cap_add"]), {"DAC_OVERRIDE", "FOWNER"})
        self.assertEqual(services["egress"]["cap_add"], ["DAC_OVERRIDE"])

    def test_engine_network_is_the_only_egress_control_path(self):
        egress = self.compose["services"]["egress"]
        self.assertEqual(egress["cap_drop"], ["ALL"])
        self.assertEqual(egress["cap_add"], ["DAC_OVERRIDE"])
        self.assertNotIn("ports", egress)
        control_env = self.compose["services"]["control"]["environment"]
        self.assertEqual(control_env["MDD_EGRESS_TRANSPORT"], "socks5")
        self.assertEqual(control_env["MDD_ENGINE_NETWORK"], "mdd-sim-gateway-engine")
        self.assertEqual(control_env["MDD_ENGINE_DIRECT_NETWORK"], "mdd-sim-gateway-uplink")
        self.assertEqual(control_env["MDD_PCSCD_VOLUME"], "mdd-sim-gateway-pcscd")
        self.assertEqual(egress["extra_hosts"],
                         ["${MDD_NAS_HOSTNAME:-host.docker.internal}:host-gateway"])


if __name__ == "__main__":
    unittest.main()
