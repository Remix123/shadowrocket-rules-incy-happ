#!/usr/bin/env python3
"""Convert Johnshall Shadowrocket profiles into INCY routing JSON profiles."""

from __future__ import annotations

import argparse
import base64
import html
import ipaddress
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


OWNER = "Johnshall"
REPOSITORY = "Shadowrocket-ADBlock-Rules-Forever"
BRANCH = "release"
CONTENTS_API = f"https://api.github.com/repos/{OWNER}/{REPOSITORY}/contents?ref={BRANCH}"
RAW_ROOT = f"https://raw.githubusercontent.com/{OWNER}/{REPOSITORY}/{BRANCH}"
GEOIP_URL = "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat"
GEOSITE_URL = "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat"
SOURCE_LICENSE = "CC BY-SA 4.0"
SOURCE_LICENSE_URL = "https://creativecommons.org/licenses/by-sa/4.0/"
MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024
RULESET_HOSTS = {"raw.githubusercontent.com"}
SUPPORTED_CATEGORIES = ("DirectSites", "DirectIp", "ProxySites", "ProxyIp", "BlockSites", "BlockIp")
IPV4_MAPPED_IPV6_PREFIX = ipaddress.ip_network("::ffff:0:0/96")


class ConversionError(RuntimeError):
    pass


def fetch_url(url: str, token: str | None = None) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ConversionError(f"Refusing non-HTTPS URL: {url}")
    if parsed.hostname not in {"api.github.com", "raw.githubusercontent.com"}:
        raise ConversionError(f"Refusing URL outside GitHub source allowlist: {url}")
    headers = {"User-Agent": "incy-shadowrocket-converter/1.0"}
    if parsed.hostname == "api.github.com":
        headers["Accept"] = "application/vnd.github+json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read(MAX_DOWNLOAD_BYTES + 1)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise ConversionError(f"Download failed for {url}: {exc}") from exc
    if len(body) > MAX_DOWNLOAD_BYTES:
        raise ConversionError(f"Download exceeds {MAX_DOWNLOAD_BYTES} bytes: {url}")
    return body


def discover_profiles(fetcher: Callable[[str], bytes]) -> list[tuple[str, str]]:
    """Return all root-level .conf files from the upstream release branch."""
    contents = json.loads(fetcher(CONTENTS_API).decode("utf-8"))
    if not isinstance(contents, list):
        raise ConversionError("GitHub contents API did not return a root directory listing")
    paths = sorted(
        entry["name"]
        for entry in contents
        if entry.get("type") == "file" and entry.get("name", "").lower().endswith(".conf")
    )
    if not paths:
        raise ConversionError("No root-level .conf profiles found in upstream release tree")
    return [(path, f"{RAW_ROOT}/{quote(path)}") for path in paths]


def _category(action: str) -> str | None:
    normalized = action.strip().upper()
    if normalized == "DIRECT":
        return "direct"
    if normalized.startswith("REJECT") or normalized in {"BLOCK", "REJECT-DROP"}:
        return "block"
    if normalized in {"PROXY", "GLOBAL", "GLOBAL-PROXY"}:
        return "proxy"
    # Shadowrocket policy groups cannot be represented as separate INCY outbounds.
    # Treat custom groups as proxy and disclose this collapse in the report.
    if normalized and normalized not in {"PASS", "NO-RESOLVE"}:
        return "proxy"
    return None


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_network(value, strict=False)
        return True
    except ValueError:
        try:
            ipaddress.ip_address(value)
            return True
        except ValueError:
            return False


def _clean_domain(value: str) -> str | None:
    value = value.strip().lower().rstrip(".")
    value = value.removeprefix("*.")
    if not value or "/" in value or " " in value:
        return None
    if not re.fullmatch(r"[a-z0-9_-]+(?:\.[a-z0-9_-]+)*", value):
        return None
    return value


class ProfileConverter:
    def __init__(self, source_name: str, source_url: str, fetcher: Callable[[str], bytes]):
        self.source_name = source_name
        self.source_url = source_url
        self.fetcher = fetcher
        self.values: dict[str, set[str]] = {field: set() for field in SUPPORTED_CATEGORIES}
        self.counts: Counter[str] = Counter()
        self.unsupported: Counter[str] = Counter()
        self.unsupported_examples: list[str] = []
        self.lossy: Counter[str] = Counter()
        self.lossy_examples: list[str] = []
        self.custom_groups: Counter[str] = Counter()
        self.rule_set_urls: set[str] = set()
        self.visited_rule_sets: set[tuple[str, str]] = set()
        self.in_progress_rule_sets: set[tuple[str, str]] = set()
        self.final_policy: str | None = None
        self.ignored_sections: Counter[str] = Counter()
        self.ignored_general: Counter[str] = Counter()

    def _record(self, counter: Counter[str], examples: list[str], key: str, example: str) -> None:
        counter[key] += 1
        if len(examples) < 30 and example not in examples:
            examples.append(example)

    def _add(self, kind: str, action: str, value: str, origin: str, *, lossy: str | None = None) -> None:
        category = _category(action)
        if category is None:
            self._record(self.unsupported, self.unsupported_examples, f"policy:{action or '(empty)'}", origin)
            return
        if category == "proxy" and action.strip().upper() not in {"PROXY", "GLOBAL", "GLOBAL-PROXY"}:
            self.custom_groups[action] += 1
        if category == "block" and action.strip().upper() != "REJECT":
            self._record(self.lossy, self.lossy_examples, "REJECT variant mapped to generic block", origin)
        if kind == "domain":
            item = _clean_domain(value)
            if item is None:
                self._record(self.unsupported, self.unsupported_examples, "invalid-domain", origin)
                return
            field = {"direct": "DirectSites", "proxy": "ProxySites", "block": "BlockSites"}[category]
        elif kind == "ip":
            item = value.strip()
            if not _is_ip(item):
                self._record(self.unsupported, self.unsupported_examples, "invalid-ip-or-cidr", origin)
                return
            network = ipaddress.ip_network(item, strict=False)
            if network.version == 6 and network.subnet_of(IPV4_MAPPED_IPV6_PREFIX):
                self._record(
                    self.unsupported,
                    self.unsupported_examples,
                    "IPv4-mapped IPv6 range omitted (core rejects IPv4-mapped CIDR prefix)",
                    origin,
                )
                return
            field = {"direct": "DirectIp", "proxy": "ProxyIp", "block": "BlockIp"}[category]
        elif kind == "geoip":
            item = f"geoip:{value.strip().lower()}"
            field = {"direct": "DirectIp", "proxy": "ProxyIp", "block": "BlockIp"}[category]
        else:
            raise AssertionError(kind)
        self.values[field].add(item)
        self.counts[f"{field}:{'RULE-SET' if origin.startswith('RULE-SET ') else 'rule'}"] += 1
        if lossy:
            self._record(self.lossy, self.lossy_examples, lossy, origin)

    def _rule_set(self, url: str, action: str, depth: int, origin: str) -> None:
        if depth >= 4:
            self._record(self.unsupported, self.unsupported_examples, "RULE-SET-depth-limit", origin)
            return
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in RULESET_HOSTS:
            self._record(self.unsupported, self.unsupported_examples, "RULE-SET-host-not-allowed", origin)
            return
        cache_key = (url, action)
        if cache_key in self.in_progress_rule_sets:
            self._record(self.unsupported, self.unsupported_examples, "RULE-SET-cycle", origin)
            return
        self.rule_set_urls.add(url)
        if cache_key in self.visited_rule_sets:
            return
        self.visited_rule_sets.add(cache_key)
        self.in_progress_rule_sets.add(cache_key)
        try:
            content = self.fetcher(url).decode("utf-8-sig", errors="replace")
            self._parse_text(content, action, depth + 1, f"RULE-SET {url}")
        except ConversionError as exc:
            self._record(self.unsupported, self.unsupported_examples, "RULE-SET-download-failed", f"{origin}: {exc}")
        finally:
            self.in_progress_rule_sets.discard(cache_key)

    def _parse_rule(self, line: str, inherited_action: str | None, depth: int, source: str, line_number: int) -> None:
        clean = line.split("#", 1)[0].strip()
        if not clean:
            return
        parts = [part.strip() for part in clean.split(",")]
        operator = parts[0].upper()
        origin = f"{source}:{line_number}: {clean[:240]}"
        if operator in {"FINAL", "MATCH"}:
            action = (parts[1] if len(parts) > 1 else "") or (inherited_action or "")
            category = _category(action)
            if category in {"direct", "proxy"}:
                self.final_policy = category
                self.counts["default-policy"] += 1
                if category == "proxy" and action.strip().upper() not in {"PROXY", "GLOBAL", "GLOBAL-PROXY"}:
                    self.custom_groups[action] += 1
            elif category == "block":
                self.final_policy = "direct"
                self._record(self.lossy, self.lossy_examples, "block-default-collapsed-to-direct", origin)
            else:
                self._record(self.unsupported, self.unsupported_examples, "unsupported-default-policy", origin)
            return
        if operator == "RULE-SET":
            if len(parts) < 2:
                self._record(self.unsupported, self.unsupported_examples, "malformed-RULE-SET", origin)
                return
            action = (parts[2] if len(parts) > 2 else "") or (inherited_action or "")
            if not action:
                self._record(self.unsupported, self.unsupported_examples, "RULE-SET-missing-policy", origin)
                return
            self._rule_set(parts[1], action, depth, origin)
            return

        action = (parts[2] if len(parts) > 2 else "") or (inherited_action or "")
        if not action:
            self._record(self.unsupported, self.unsupported_examples, f"operator:{operator}", origin)
            return
        if operator in {"DOMAIN-SUFFIX", "DOMAIN"}:
            if len(parts) < 2:
                self._record(self.unsupported, self.unsupported_examples, f"malformed:{operator}", origin)
                return
            lossy = "DOMAIN exact-vs-suffix distinction" if operator == "DOMAIN" else None
            self._add("domain", action, parts[1], origin, lossy=lossy)
        elif operator in {"IP-CIDR", "IP-CIDR6"}:
            if len(parts) < 2:
                self._record(self.unsupported, self.unsupported_examples, f"malformed:{operator}", origin)
                return
            self._add("ip", action, parts[1], origin)
            if any(part.lower() == "no-resolve" for part in parts[3:]):
                self._record(self.lossy, self.lossy_examples, "IP no-resolve option", origin)
        elif operator == "GEOIP":
            if len(parts) < 2:
                self._record(self.unsupported, self.unsupported_examples, "malformed:GEOIP", origin)
                return
            self._add("geoip", action, parts[1], origin)
            if any(part.lower() == "no-resolve" for part in parts[3:]):
                self._record(self.lossy, self.lossy_examples, "GEOIP no-resolve option", origin)
        elif operator in {"DOMAIN-KEYWORD", "IP-ASN", "URL-REGEX", "USER-AGENT", "PROCESS-NAME", "DEST-PORT", "SRC-IP", "SRC-PORT", "PROTOCOL", "AND", "OR", "NOT"}:
            self._record(self.unsupported, self.unsupported_examples, f"operator:{operator}", origin)
        else:
            # A custom Shadowrocket group (e.g. AI or Netflix) is mapped to the
            # single INCY proxy route, with the group collapse reported.
            category = _category(action)
            if category == "proxy" and action.strip().upper() not in {"PROXY", "GLOBAL", "GLOBAL-PROXY"}:
                self.custom_groups[action] += 1
            self._record(self.unsupported, self.unsupported_examples, f"operator:{operator}", origin)

    def _parse_text(self, text: str, inherited_action: str | None, depth: int, source: str) -> None:
        section = ""
        for line_number, raw in enumerate(text.splitlines(), start=1):
            stripped = raw.strip()
            if not stripped or stripped.startswith(("#", ";")):
                continue
            section_match = re.fullmatch(r"\[([^]]+)\]", stripped)
            if section_match:
                section = section_match.group(1).strip().upper()
                continue
            if section == "RULE" or (not section and depth > 0):
                self._parse_rule(stripped, inherited_action, depth, source, line_number)
                continue
            if section == "GENERAL":
                key, sep, value = stripped.partition("=")
                if not sep:
                    self.ignored_general[key.strip().lower()] += 1
                    continue
                key = key.strip().lower()
                if key in {"skip-proxy", "bypass-tun"}:
                    for raw_value in value.split(","):
                        item = raw_value.strip()
                        if not item:
                            continue
                        origin = f"{source}:{line_number} [{key}]: {item}"
                        if _is_ip(item):
                            self._add("ip", "DIRECT", item, origin)
                        elif key == "skip-proxy":
                            self._add("domain", "DIRECT", item, origin, lossy="wildcard skip-proxy pattern" if "*" in item else None)
                        else:
                            self._record(self.unsupported, self.unsupported_examples, "non-IP bypass-tun entry", origin)
                else:
                    self.ignored_general[key] += 1
                continue
            self.ignored_sections[section or "(no section)"] += 1

    def convert(self, text: str) -> tuple[dict, dict]:
        self._parse_text(text, None, 0, self.source_name)
        default_policy = self.final_policy
        inferred_default = False
        if default_policy is None:
            stem = Path(self.source_name).stem.lower()
            if stem.startswith(("sr_top500_whitelist", "sr_proxy_banad")):
                default_policy = "proxy"
            elif stem.startswith(("sr_top500_banlist", "sr_direct_banad", "sr_ad_only", "sr_adb", "sr_backcn")):
                default_policy = "direct"
            else:
                default_policy = "direct"
            inferred_default = True
            self._record(self.lossy, self.lossy_examples, f"default-policy-inferred-as-{default_policy}", self.source_name)
        conflicts: dict[str, list[str]] = {}
        route_to_fields: dict[str, list[str]] = {}
        for field, items in self.values.items():
            for item in items:
                route_to_fields.setdefault(item, []).append(field)
        for item, fields in sorted(route_to_fields.items()):
            if len(fields) > 1:
                conflicts[item] = fields

        profile = {
            "Name": f"{Path(self.source_name).stem} (INCY)",
            "GlobalProxy": "true" if default_policy == "proxy" else "false",
            "Geoipurl": GEOIP_URL,
            "Geositeurl": GEOSITE_URL,
            "DomainStrategy": "IPIfNonMatch",
        }
        for field in SUPPORTED_CATEGORIES:
            profile[field] = sorted(self.values[field])
        report = {
            "source": self.source_url,
            "sourceLicense": SOURCE_LICENSE,
            "sourceLicenseUrl": SOURCE_LICENSE_URL,
            "sourceFile": self.source_name,
            "defaultPolicy": default_policy,
            "defaultPolicyInferred": inferred_default,
            "counts": dict(sorted(self.counts.items())),
            "unsupported": dict(sorted(self.unsupported.items())),
            "unsupportedExamples": self.unsupported_examples,
            "lossyMappings": dict(sorted(self.lossy.items())),
            "lossyExamples": self.lossy_examples,
            "customPolicyGroupsCollapsedToProxy": dict(sorted(self.custom_groups.items())),
            "expandedRuleSets": sorted(self.rule_set_urls),
            "conflictingEntries": conflicts,
            "ignoredGeneralSettings": dict(sorted(self.ignored_general.items())),
            "ignoredNonRoutingSections": dict(sorted(self.ignored_sections.items())),
            "notes": [
                "INCY and Happ routing profiles group routes into direct/proxy/block arrays; Shadowrocket first-match order is not retained.",
                "DOMAIN exact matching and DOMAIN-SUFFIX matching share the target profile's domain-list representation.",
                "IPv4-mapped IPv6 ranges are omitted because the target core interprets them as IPv4 and rejects their IPv6 prefix length.",
                "Shadowrocket URL Rewrite, MITM, Host, DNS, and policy-group behavior is outside these routing profiles.",
            ],
        }
        return profile, report


def _base_url() -> str:
    override = os.environ.get("PAGES_BASE_URL")
    if override:
        return override.rstrip("/") + "/"
    repository = os.environ.get("GITHUB_REPOSITORY", "owner/repository")
    owner, repo = repository.split("/", 1)
    suffix = "" if repo.lower() == f"{owner}.github.io".lower() else f"{repo}/"
    return f"https://{owner}.github.io/{suffix}"


def happ_import_link(profile: dict) -> str:
    """Create the Happ routing import deeplink for a routing-profile JSON object."""
    payload = json.dumps(profile, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encoded = base64.b64encode(payload).decode("ascii")
    return f"happ://routing/onadd/{encoded}"


def _write_site(output_dir: Path, entries: list[dict], reports: dict[str, dict]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest = {
        "generatedAt": generated,
        "sourceRepository": f"https://github.com/{OWNER}/{REPOSITORY}/tree/{BRANCH}",
        "sourceLicense": SOURCE_LICENSE,
        "sourceLicenseUrl": SOURCE_LICENSE_URL,
        "profiles": entries,
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "conversion-report.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    attribution = (
        f"Source rules: https://github.com/{OWNER}/{REPOSITORY}/tree/{BRANCH}\n"
        f"Source license: {SOURCE_LICENSE} ({SOURCE_LICENSE_URL})\n"
        "Converted rule data is published with the same attribution and share-alike license. "
        "Other upstream dependencies may have separate license terms.\n"
    )
    (output_dir / "ATTRIBUTION.txt").write_text(attribution, encoding="utf-8")
    rows = []
    for entry in entries:
        name = html.escape(entry["name"])
        json_link = html.escape(entry["url"], quote=True)
        happ_json_link = html.escape(entry["happUrl"], quote=True)
        happ_import_link_url = html.escape(entry["happImportLinkUrl"], quote=True)
        report_link = html.escape(entry["reportUrl"], quote=True)
        happ_report_link = html.escape(entry["happReportUrl"], quote=True)
        status = "查看通用限制与报告"
        rows.append(
            f'<tr><td>{name}</td><td>INCY: <a href="{json_link}">JSON</a> · '
            f'<a href="{report_link}">报告</a><br>'
            f'Happ: <a href="{happ_json_link}">JSON</a> · '
            f'<a href="{happ_import_link_url}">导入链接</a> · '
            f'<a href="{happ_report_link}">报告</a></td><td>{entry["ruleCount"]}</td>'
            f'<td>{entry["unsupportedCount"]}</td><td>{entry["lossyCount"]}</td>'
            f'<td>{entry["conflictCount"]}</td><td>{status}</td></tr>'
        )
    page = """<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shadowrocket → INCY / Happ 路由规则</title>
<style>body{font:16px system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#202124}table{border-collapse:collapse;width:100%}th,td{padding:.65rem;border-bottom:1px solid #ddd;text-align:left}code{overflow-wrap:anywhere}a{color:#0759b5}</style>
<h1>Shadowrocket → INCY / Happ 路由规则</h1>
<p>上游规则每日自动转换。INCY 与 Happ 的路由 JSON 分开提供；Happ 导入链接按官方 happ://routing/onadd/Base64 格式生成。请先看转换报告中的未映射语法与语义折损。</p>
<table><thead><tr><th>配置</th><th>文件</th><th>已转换条目</th><th>未映射条目</th><th>有折损映射</th><th>冲突项</th><th>状态</th></tr></thead><tbody>
__ROWS__
</tbody></table>
<p>来源：<a href="https://github.com/Johnshall/Shadowrocket-ADBlock-Rules-Forever">Johnshall/Shadowrocket-ADBlock-Rules-Forever</a>；转换规则数据按 <a href="https://creativecommons.org/licenses/by-sa/4.0/">CC BY-SA 4.0</a> 标注；<a href="ATTRIBUTION.txt">署名说明</a>；生成时间：__GENERATED__ UTC。</p>
</html>
""".replace("__ROWS__", "\n".join(rows)).replace("__GENERATED__", html.escape(generated))
    (output_dir / "index.html").write_text(page, encoding="utf-8")


def build(output_dir: Path, token: str | None = None) -> list[dict]:
    fetcher = lambda url: fetch_url(url, token)
    profiles = discover_profiles(fetcher)
    output_dir.mkdir(parents=True, exist_ok=True)
    base_url = _base_url()
    entries: list[dict] = []
    reports: dict[str, dict] = {}
    for filename, source_url in profiles:
        content = fetcher(source_url).decode("utf-8-sig", errors="replace")
        converter = ProfileConverter(filename, source_url, fetcher)
        profile, report = converter.convert(content)
        stem = Path(filename).stem
        profile_filename = f"{stem}.json"
        report_filename = f"{stem}.report.json"
        (output_dir / profile_filename).write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (output_dir / report_filename).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        happ_profile = dict(profile)
        happ_profile["Name"] = f"{stem} (Happ)"
        happ_dir = output_dir / "happ"
        happ_dir.mkdir(parents=True, exist_ok=True)
        happ_filename = f"{stem}.json"
        happ_link_filename = f"{stem}.link.txt"
        happ_report_filename = f"{stem}.report.json"
        happ_json = json.dumps(happ_profile, ensure_ascii=False, indent=2) + "\n"
        happ_link = happ_import_link(happ_profile)
        (happ_dir / happ_filename).write_text(happ_json, encoding="utf-8")
        (happ_dir / happ_link_filename).write_text(happ_link + "\n", encoding="utf-8")
        happ_report = dict(report)
        happ_report["target"] = "Happ routing profile"
        happ_report["importMethod"] = "Copy the companion happ://routing/onadd/{base64} link into Happ."
        happ_report["importLinkBytes"] = len(happ_link.encode("utf-8"))
        (happ_dir / happ_report_filename).write_text(json.dumps(happ_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        unsupported_count = sum(report["unsupported"].values())
        lossy_count = sum(report["lossyMappings"].values())
        conflict_count = len(report["conflictingEntries"])
        rule_count = sum(sum(1 for _ in profile[field]) for field in SUPPORTED_CATEGORIES)
        entry = {
            "name": profile["Name"],
            "source": source_url,
            "json": profile_filename,
            "report": report_filename,
            "url": base_url + profile_filename,
            "reportUrl": base_url + report_filename,
            "happJson": f"happ/{happ_filename}",
            "happUrl": base_url + f"happ/{happ_filename}",
            "happImportLink": f"happ/{happ_link_filename}",
            "happImportLinkUrl": base_url + f"happ/{happ_link_filename}",
            "happReportUrl": base_url + f"happ/{happ_report_filename}",
            "happImportLinkBytes": len(happ_link.encode("utf-8")),
            "ruleCount": rule_count,
            "unsupportedCount": unsupported_count,
            "lossyCount": lossy_count,
            "conflictCount": conflict_count,
            "defaultPolicy": report["defaultPolicy"],
        }
        entries.append(entry)
        reports[filename] = report
        print(f"{filename}: {rule_count} mapped, {unsupported_count} unsupported")
    _write_site(output_dir, entries, reports)
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("site"), help="output directory for GitHub Pages")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"), help="optional GitHub API token")
    args = parser.parse_args()
    try:
        entries = build(args.output, args.token)
    except (ConversionError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Generated {len(entries)} INCY JSON profiles in {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
