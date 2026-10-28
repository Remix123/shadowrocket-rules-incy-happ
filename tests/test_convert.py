import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.convert import CONTENTS_API, ProfileConverter, _base_url, _write_site, build, discover_profiles, happ_import_link


class ConverterTests(unittest.TestCase):
    def test_supported_rules_and_general_bypass(self):
        source = """[General]
skip-proxy = localhost, *.local, 192.168.0.0/16
bypass-tun = 10.0.0.0/8,::ffff:0:0/96

[Rule]
DOMAIN-SUFFIX,example.com,PROXY
DOMAIN,exact.example.net,DIRECT
IP-CIDR6,2001:db8::/32,REJECT,no-resolve
IP-CIDR6,::ffff:192.0.2.0/120,REJECT
GEOIP,CN,DIRECT
FINAL,PROXY
"""
        profile, report = ProfileConverter("sr_cnip.conf", "https://example.test/sr_cnip.conf", lambda _: b"").convert(source)

        self.assertEqual(profile["GlobalProxy"], "true")
        self.assertIn("example.com", profile["ProxySites"])
        self.assertIn("exact.example.net", profile["DirectSites"])
        self.assertIn("geoip:cn", profile["DirectIp"])
        self.assertIn("10.0.0.0/8", profile["DirectIp"])
        self.assertIn("192.168.0.0/16", profile["DirectIp"])
        self.assertIn("2001:db8::/32", profile["BlockIp"])
        self.assertNotIn("::ffff:0:0/96", profile["DirectIp"])
        self.assertNotIn("::ffff:192.0.2.0/120", profile["BlockIp"])
        self.assertIn("localhost", profile["DirectSites"])
        self.assertEqual(report["lossyMappings"]["DOMAIN exact-vs-suffix distinction"], 1)
        self.assertEqual(report["lossyMappings"]["IP no-resolve option"], 1)
        self.assertEqual(
            report["unsupported"]["IPv4-mapped IPv6 range omitted (core rejects IPv4-mapped CIDR prefix)"],
            2,
        )
        json.loads(json.dumps(profile))

    def test_rule_set_expansion_and_unsupported_operator_reporting(self):
        rule_set_url = "https://raw.githubusercontent.com/example/rules/main/rules.list"
        payloads = {
            rule_set_url: b"DOMAIN-SUFFIX,from-list.example\nDOMAIN-KEYWORD,keyword,DIRECT\n",
        }
        source = f"[Rule]\nRULE-SET,{rule_set_url},PROXY\nFINAL,DIRECT\n"
        profile, report = ProfileConverter("test.conf", "https://example.test/test.conf", payloads.__getitem__).convert(source)

        self.assertIn("from-list.example", profile["ProxySites"])
        self.assertEqual(report["unsupported"]["operator:DOMAIN-KEYWORD"], 1)
        self.assertIn(rule_set_url, report["expandedRuleSets"])
        self.assertEqual(profile["GlobalProxy"], "false")

    def test_root_conf_discovery_ignores_nested_files(self):
        contents = [
            {"name": "sr_cnip.conf", "type": "file"},
            {"name": "internal.conf", "type": "dir"},
            {"name": "README.md", "type": "file"},
        ]
        profiles = discover_profiles(lambda _: json.dumps(contents).encode())
        self.assertEqual([name for name, _ in profiles], ["sr_cnip.conf"])

    def test_ad_only_profile_uses_direct_default_and_reports_inference(self):
        profile, report = ProfileConverter("sr_adb.conf", "https://example.test/sr_adb.conf", lambda _: b"").convert(
            "[Rule]\nDOMAIN-SUFFIX,ads.example,REJECT\n"
        )
        self.assertEqual(profile["GlobalProxy"], "false")
        self.assertTrue(report["defaultPolicyInferred"])
        self.assertIn("default-policy-inferred-as-direct", report["lossyMappings"])
        self.assertIn("ads.example", profile["BlockSites"])

    def test_github_pages_json_base_urls(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "amino/routing-rules"}, clear=True):
            self.assertEqual(_base_url(), "https://amino.github.io/routing-rules/")
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "amino/amino.github.io"}, clear=True):
            self.assertEqual(_base_url(), "https://amino.github.io/")

    def test_happ_deeplink_contains_base64_encoded_routing_profile(self):
        profile = {"Name": "sr_cnip (Happ)", "GlobalProxy": "true", "DirectSites": ["example.cn"]}
        link = happ_import_link(profile)
        prefix = "happ://routing/onadd/"
        self.assertTrue(link.startswith(prefix))
        decoded = base64.b64decode(link.removeprefix(prefix)).decode("utf-8")
        self.assertEqual(json.loads(decoded), profile)

    def test_pages_index_lists_happ_json_and_import_link(self):
        entry = {
            "name": "test (INCY)",
            "url": "https://owner.github.io/repo/test.json",
            "reportUrl": "https://owner.github.io/repo/test.report.json",
            "happUrl": "https://owner.github.io/repo/happ/test.json",
            "happImportLinkUrl": "https://owner.github.io/repo/happ/test.link.txt",
            "happReportUrl": "https://owner.github.io/repo/happ/test.report.json",
            "ruleCount": 3,
            "unsupportedCount": 0,
            "lossyCount": 0,
            "conflictCount": 0,
        }
        with tempfile.TemporaryDirectory() as directory:
            _write_site(Path(directory), [entry], {})
            page = (Path(directory) / "index.html").read_text(encoding="utf-8")
            manifest = json.loads((Path(directory) / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn(entry["happUrl"], page)
        self.assertIn(entry["happImportLinkUrl"], page)
        self.assertEqual(manifest["profiles"][0]["happUrl"], entry["happUrl"])

    def test_build_writes_inc_and_happ_artifacts_from_upstream(self):
        source = b"[Rule]\nDOMAIN-SUFFIX,example.cn,DIRECT\nFINAL,PROXY\n"

        def fake_fetch(url, token=None):
            if url == CONTENTS_API:
                return json.dumps([{"name": "sr_cnip.conf", "type": "file"}]).encode()
            if url.endswith("/sr_cnip.conf"):
                return source
            raise AssertionError(f"Unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as directory:
            with patch("scripts.convert.fetch_url", side_effect=fake_fetch):
                with patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/rules"}, clear=True):
                    entries = build(Path(directory) / "site")
            inc_profile = json.loads((Path(directory) / "site/sr_cnip.json").read_text(encoding="utf-8"))
            happ_profile = json.loads((Path(directory) / "site/happ/sr_cnip.json").read_text(encoding="utf-8"))
            happ_link = (Path(directory) / "site/happ/sr_cnip.link.txt").read_text(encoding="utf-8").strip()
        self.assertEqual(inc_profile["Name"], "sr_cnip (INCY)")
        self.assertEqual(happ_profile["Name"], "sr_cnip (Happ)")
        self.assertEqual(happ_profile["DirectSites"], inc_profile["DirectSites"])
        payload = happ_link.removeprefix("happ://routing/onadd/")
        self.assertEqual(json.loads(base64.b64decode(payload)), happ_profile)
        self.assertEqual(entries[0]["happUrl"], "https://owner.github.io/rules/happ/sr_cnip.json")


if __name__ == "__main__":
    unittest.main()
