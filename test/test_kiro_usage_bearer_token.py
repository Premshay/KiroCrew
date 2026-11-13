"""Usage API bearer-token selection."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from test_kiro_usage_api import _resp

import kiro_crew.dashboard.handlers.kiro_usage_api as api


class TestLoadBearerToken:
    def test_reads_access_token_from_json(self):
        # JSON reads go through hooks.safe_read_file_internal (sensitive path).
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

        def fake_read(read_id):
            return (
                json.dumps({"accessToken": "tok-abc", "expiresAt": future}).encode()
                if read_id == "kiro_usage_api.sso_token_cli"
                else None
            )

        with (
            patch("kiro_crew.hooks.safe_read_file_internal", side_effect=fake_read),
            patch.object(api, "_CLI_SQLITE_DBS", ()),
            patch.object(api, "_OTHER_SQLITE_DBS", ()),
        ):
            assert api._load_bearer_token() == "tok-abc"

    def test_skips_expired_json_token(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        blob = json.dumps({"accessToken": "old", "expiresAt": past}).encode()
        with (
            patch("kiro_crew.hooks.safe_read_file_internal", return_value=blob),
            patch.object(api, "_CLI_SQLITE_DBS", ()),
            patch.object(api, "_OTHER_SQLITE_DBS", ()),
        ):
            assert api._load_bearer_token() is None

    def test_accepts_future_json_token(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

        def fake_read(read_id):
            return (
                json.dumps({"accessToken": "fresh", "expiresAt": future}).encode()
                if read_id.endswith("cli")
                else None
            )

        with (
            patch("kiro_crew.hooks.safe_read_file_internal", side_effect=fake_read),
            patch.object(api, "_CLI_SQLITE_DBS", ()),
            patch.object(api, "_OTHER_SQLITE_DBS", ()),
        ):
            assert api._load_bearer_token() == "fresh"

    def test_missing_sources_return_none(self, tmp_path):
        with (
            patch("kiro_crew.hooks.safe_read_file_internal", return_value=None),
            patch.object(api, "_CLI_SQLITE_DBS", (tmp_path / "nope.sqlite3",)),
            patch.object(api, "_OTHER_SQLITE_DBS", ()),
        ):
            assert api._load_bearer_token() is None

    def test_falls_through_expired_json_to_sqlite(self, tmp_path):
        # Stale JSON (expired) must be skipped and the live SQLite token used —
        # exactly the Linux clouddesk situation.
        past = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        stale = json.dumps({"accessToken": "stale", "expiresAt": past}).encode()
        db = tmp_path / "data.sqlite3"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        con.execute(
            "INSERT INTO auth_kv VALUES (?, ?)",
            (
                "kirocli:odic:token",
                json.dumps({"access_token": "live-sqlite", "expires_at": future}),
            ),
        )
        con.commit()
        con.close()
        with (
            patch("kiro_crew.hooks.safe_read_file_internal", return_value=stale),
            patch("kiro_crew.hooks.emit_internal_read_audit", return_value=True),
            patch.object(api, "_CLI_SQLITE_DBS", (db,)),
            patch.object(api, "_OTHER_SQLITE_DBS", ()),
        ):
            assert api._load_bearer_token() == "live-sqlite"

    def test_sqlite_token_denied_when_audit_cannot_be_recorded(self, tmp_path):
        # Fail closed: a live token whose SEL audit cannot be persisted must NOT
        # be returned — the caller degrades to the text scrape instead.
        db = tmp_path / "data.sqlite3"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        con.execute(
            "INSERT INTO auth_kv VALUES (?, ?)",
            (
                "kirocli:odic:token",
                json.dumps({"access_token": "live-sqlite", "expires_at": future}),
            ),
        )
        con.commit()
        con.close()
        with (
            patch("kiro_crew.hooks.safe_read_file_internal", return_value=None),
            patch("kiro_crew.hooks.emit_internal_read_audit", return_value=False),
            patch.object(api, "_CLI_SQLITE_DBS", (db,)),
            patch.object(api, "_OTHER_SQLITE_DBS", ()),
        ):
            assert api._load_bearer_token() is None

    def test_skips_expired_sqlite_token(self, tmp_path):
        db = tmp_path / "data.sqlite3"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        con.execute(
            "INSERT INTO auth_kv VALUES (?, ?)",
            ("kirocli:odic:token", json.dumps({"access_token": "old", "expires_at": past})),
        )
        con.commit()
        con.close()
        with (
            patch("kiro_crew.hooks.safe_read_file_internal", return_value=None),
            patch("kiro_crew.hooks.emit_internal_read_audit", return_value=True) as audit,
            patch.object(api, "_CLI_SQLITE_DBS", (db,)),
            patch.object(api, "_OTHER_SQLITE_DBS", ()),
        ):
            assert api._load_bearer_token() is None
        # The credential blob WAS read even though expired — that access must
        # be recorded so the audit trail covers every read of the store.
        audit.assert_any_call(api._SQLITE_AUDIT_READ_ID, "expired")

    def test_unregistered_audit_read_id_is_rejected(self):
        # The audit entry point enforces its own registry -- an unregistered
        # read_id must be refused (False), so it cannot serve as an unscoped
        # bypass of the SEL-audit surface, and callers fail closed on it.
        from kiro_crew import hooks

        assert hooks.emit_internal_read_audit("rogue.read_id", "success") is False
        assert "kiro_usage_api.sqlite_token" in hooks._AUDIT_ONLY_READ_IDS

    def test_sqlite_oserror_fails_closed(self, tmp_path):
        # An OSError from db.exists() (e.g. permission-denied parent dir) is not
        # a sqlite3.Error — it must still fail closed to None, never raise.
        db = tmp_path / "data.sqlite3"

        class _RaisingPath(type(db)):  # pathlib.Path subclass
            def exists(self):
                raise OSError("permission denied")

        raising = _RaisingPath(db)
        with patch("kiro_crew.hooks.emit_internal_read_audit", return_value=True):
            now = datetime.now(timezone.utc)
            assert api._token_from_sqlite(raising, now) is None

    def test_json_token_non_dict_ignored(self):
        # A JSON list/scalar cache entry must not raise on .get — it fails closed
        # (None) without aborting the remaining candidate sources.
        with (
            patch("kiro_crew.hooks.safe_read_file_internal", return_value=b"[]"),
            patch.object(api, "_CLI_SQLITE_DBS", ()),
            patch.object(api, "_OTHER_SQLITE_DBS", ()),
        ):
            assert api._load_bearer_token() is None

    def test_symlinked_sqlite_db_is_rejected(self, tmp_path):
        # A symlinked DB path must be refused so the read cannot be redirected.
        real = tmp_path / "data.sqlite3"
        con = sqlite3.connect(str(real))
        con.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        con.commit()
        con.close()
        link = tmp_path / "link.sqlite3"
        link.symlink_to(real)
        with patch("kiro_crew.hooks.emit_internal_read_audit", return_value=True):
            assert api._token_from_sqlite(link, datetime.now(timezone.utc)) is None

    def test_sqlite_opened_but_no_token_still_audits(self, tmp_path):
        # Audit-on-every-outcome: an opened DB with no matching token row must
        # still record one audit outcome, not fall through silently.
        db = tmp_path / "data.sqlite3"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        con.commit()
        con.close()
        with patch("kiro_crew.hooks.emit_internal_read_audit", return_value=True) as audit:
            assert api._token_from_sqlite(db, datetime.now(timezone.utc)) is None
        audit.assert_any_call(api._SQLITE_AUDIT_READ_ID, "no_token")

    def test_social_login_token_key_is_recognized(self, tmp_path):
        # GitHub social login stores its bearer token under a different key
        # (kirocli:social:token) than the OIDC flow (kirocli:odic:token).
        # The API module must read it from kiro-cli's own store.
        db = tmp_path / "data.sqlite3"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        con.execute(
            "INSERT INTO auth_kv VALUES (?, ?)",
            (
                "kirocli:social:token",
                json.dumps({"access_token": "social-tok", "expires_at": future}),
            ),
        )
        con.commit()
        con.close()
        with (
            patch("kiro_crew.hooks.safe_read_file_internal", return_value=None),
            patch("kiro_crew.hooks.emit_internal_read_audit", return_value=True),
            patch.object(api, "_CLI_SQLITE_DBS", (db,)),
            patch.object(api, "_OTHER_SQLITE_DBS", ()),
        ):
            assert api._load_bearer_token() == "social-tok"

    def test_external_idp_token_key_is_recognized(self, tmp_path):
        # Identity Center (org/enterprise SSO) stores its bearer token under
        # kirocli:external-idp:token. On Linux the JSON SSO cache is not
        # refreshed, so this SQLite key is the only live source.
        db = tmp_path / "data.sqlite3"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        con.execute(
            "INSERT INTO auth_kv VALUES (?, ?)",
            (
                "kirocli:external-idp:token",
                json.dumps({"access_token": "idp-tok", "expires_at": future}),
            ),
        )
        con.commit()
        con.close()
        with (
            patch("kiro_crew.hooks.safe_read_file_internal", return_value=None),
            patch("kiro_crew.hooks.emit_internal_read_audit", return_value=True),
            patch.object(api, "_CLI_SQLITE_DBS", (db,)),
            patch.object(api, "_OTHER_SQLITE_DBS", ()),
        ):
            assert api._load_bearer_token() == "idp-tok"


class TestWindowsCliStore:
    """kiro-cli on Windows keeps the same auth store under
    ``%LOCALAPPDATA%\\kiro-cli`` (``~/AppData/Local/kiro-cli`` by default);
    older layouts used ``%APPDATA%\\kiro-cli`` (``~/AppData/Roaming/kiro-cli``).

    With only the Roaming location in ``_CLI_SQLITE_DBS``, a current Windows
    host's store was never discovered: no candidate carried
    ``from_cli_store=True``, the source-anchored provenance path could never be
    satisfied, and whenever the ARN anchor also failed the usage API returned
    unavailable and the credit pill silently disappeared.
    """

    @pytest.fixture(autouse=True)
    def _clear_arn_cache(self):
        api._PROFILE_ARN_CACHE.clear()
        api._PROFILE_NAME_CACHE.clear()
        yield
        api._PROFILE_ARN_CACHE.clear()
        api._PROFILE_NAME_CACHE.clear()

    def test_windows_stores_are_default_candidates(self):
        # The FIXED default locations, not %LOCALAPPDATA%/%APPDATA%-resolved:
        # the sensitive-path fence that makes membership a trust claim is
        # home-anchored at exactly these paths, so an env-resolved location
        # either equals an entry or falls outside the fence and must not be
        # trusted. Local is where current kiro-cli writes; Roaming stays for
        # older layouts.
        local = Path.home() / "AppData" / "Local" / "kiro-cli" / "data.sqlite3"
        roaming = Path.home() / "AppData" / "Roaming" / "kiro-cli" / "data.sqlite3"
        assert local in api._CLI_SQLITE_DBS
        assert roaming in api._CLI_SQLITE_DBS

    def test_windows_store_token_is_trusted_without_arn(self, tmp_path):
        # End-to-end: a token read out of a Windows-layout store carries
        # from_cli_store=True, so the provenance path accepts it when whoami
        # reports no profile ARN -- exactly the case that failed with "all 1
        # candidate credential(s) failed" before the store was a candidate.
        db = tmp_path / "AppData" / "Roaming" / "kiro-cli" / "data.sqlite3"
        db.parent.mkdir(parents=True)
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        con.execute(
            "INSERT INTO auth_kv VALUES (?, ?)",
            ("kirocli:odic:token", json.dumps({"access_token": "win-tok", "expires_at": future})),
        )
        con.commit()
        con.close()

        usage_body = {
            "usageBreakdownList": [
                {"resourceType": "CREDIT", "currentUsage": 7.0, "usageLimit": 100.0}
            ]
        }

        def fake_post(token, target, payload, **_kwargs):
            if target == api._TARGET_LIST_PROFILES:
                return _resp(200, {"profiles": []})
            return _resp(200, usage_body)

        with (
            patch("kiro_crew.hooks.safe_read_file_internal", return_value=None),
            patch("kiro_crew.hooks.emit_internal_read_audit", return_value=True),
            patch.object(api, "_CLI_SQLITE_DBS", (db,)),
            patch.object(api, "_OTHER_SQLITE_DBS", ()),
            patch.object(api, "_post", side_effect=fake_post),
        ):
            cands = api._candidate_tokens()
            assert [c.token for c in cands] == ["win-tok"]
            assert cands[0].from_cli_store is True
            out = api.fetch_usage_limits(expected_arn=None).usage
        assert out is not None
        assert out["credits_used"] == 7.0

    def test_local_appdata_store_token_is_trusted_without_arn(self, tmp_path):
        # Same end-to-end path for the CURRENT Windows layout
        # (%LOCALAPPDATA%\kiro-cli): a token read out of a Local-layout store
        # carries from_cli_store=True, so the provenance path accepts it when
        # whoami reports no profile ARN -- the case where the store exists only
        # under AppData/Local, which would otherwise blank the pill.
        db = tmp_path / "AppData" / "Local" / "kiro-cli" / "data.sqlite3"
        db.parent.mkdir(parents=True)
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        con.execute(
            "INSERT INTO auth_kv VALUES (?, ?)",
            ("kirocli:odic:token", json.dumps({"access_token": "local-tok", "expires_at": future})),
        )
        con.commit()
        con.close()

        usage_body = {
            "usageBreakdownList": [
                {"resourceType": "CREDIT", "currentUsage": 3.0, "usageLimit": 100.0}
            ]
        }

        def fake_post(token, target, payload, **_kwargs):
            if target == api._TARGET_LIST_PROFILES:
                return _resp(200, {"profiles": []})
            return _resp(200, usage_body)

        with (
            patch("kiro_crew.hooks.safe_read_file_internal", return_value=None),
            patch("kiro_crew.hooks.emit_internal_read_audit", return_value=True),
            patch.object(api, "_CLI_SQLITE_DBS", (db,)),
            patch.object(api, "_OTHER_SQLITE_DBS", ()),
            patch.object(api, "_post", side_effect=fake_post),
        ):
            cands = api._candidate_tokens()
            assert [c.token for c in cands] == ["local-tok"]
            assert cands[0].from_cli_store is True
            out = api.fetch_usage_limits(expected_arn=None).usage
        assert out is not None
        assert out["credits_used"] == 3.0


class TestExternalIdpTokenType:
    """An External IdP token carries ``TokenType: EXTERNAL_IDP`` on both control-plane calls."""

    ARN = "arn:aws:codewhisperer:us-east-1:1:profile/p"

    @pytest.fixture(autouse=True)
    def _clear_caches(self):
        api._PROFILE_ARN_CACHE.clear()
        api._PROFILE_NAME_CACHE.clear()
        yield
        api._PROFILE_ARN_CACHE.clear()
        api._PROFILE_NAME_CACHE.clear()

    @staticmethod
    def _capture_open(captured: list):
        class _FakeResp:
            status = 200

            def read(self, n=None):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_open(self, req, timeout=None):
            captured.append(req)
            return _FakeResp()

        return fake_open

    def test_post_sends_the_token_type_header(self):
        captured: list = []
        with patch("urllib.request.OpenerDirector.open", self._capture_open(captured)):
            api._post("tok", api._TARGET_GET_USAGE, {}, token_type=api.TOKEN_TYPE_EXTERNAL_IDP)
        # urllib stores header keys capitalized (first letter only).
        assert captured[0].get_header("Tokentype") == "EXTERNAL_IDP"

    def test_post_without_a_token_type_sends_no_header(self):
        captured: list = []
        with patch("urllib.request.OpenerDirector.open", self._capture_open(captured)):
            api._post("tok", api._TARGET_GET_USAGE, {})
        assert captured[0].get_header("Tokentype") is None

    @staticmethod
    def _store(tmp_path, key: str):
        db = tmp_path / "data.sqlite3"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        con.execute(
            "INSERT INTO auth_kv VALUES (?, ?)",
            (key, json.dumps({"access_token": "live", "expires_at": future})),
        )
        con.commit()
        con.close()
        return db

    @pytest.mark.parametrize(
        "key, token_type",
        [("kirocli:external-idp:token", "EXTERNAL_IDP"), ("kirocli:odic:token", "")],
    )
    def test_store_key_decides_the_token_type(self, tmp_path, key, token_type):
        db = self._store(tmp_path, key)
        with (
            patch("kiro_crew.hooks.emit_internal_read_audit", return_value=True),
            patch.object(api, "_token_from_json", return_value=None),
            patch.object(api, "_CLI_SQLITE_DBS", (db,)),
            patch.object(api, "_OTHER_SQLITE_DBS", ()),
        ):
            found = api._token_from_sqlite(db, datetime.now(timezone.utc))
            cands = api._candidate_tokens()
        assert found is not None and found.token_type == token_type
        assert [(c.token, c.token_type) for c in cands] == [("live", token_type)]

    def _fetch(self, token_type: str) -> list:
        sent: list = []

        def fake_post(token, target, payload, *, endpoint=None, token_type=""):
            sent.append((target, token_type))
            if target == api._TARGET_LIST_PROFILES:
                return _resp(200, {"profiles": [{"arn": self.ARN}]})
            return _resp(
                200,
                {
                    "usageBreakdownList": [
                        {"resourceType": "CREDIT", "currentUsage": 1.0, "usageLimit": 10.0}
                    ]
                },
            )

        soon = datetime.now(timezone.utc) + timedelta(hours=1)
        hit = api._StoreToken("idp-tok", soon, None, token_type)
        with (
            patch.object(api, "_token_from_json", return_value=None),
            patch.object(
                api,
                "_token_from_sqlite",
                side_effect=lambda db, _n: hit if db == api._CLI_SQLITE_DBS[0] else None,
            ),
            patch.object(api, "_post", side_effect=fake_post),
        ):
            assert api.fetch_usage_limits(expected_arn=self.ARN).usage is not None
        return sent

    def test_external_idp_candidate_sends_it_on_both_calls(self):
        sent = self._fetch(api.TOKEN_TYPE_EXTERNAL_IDP)
        assert {t for t, _ in sent} == {api._TARGET_LIST_PROFILES, api._TARGET_GET_USAGE}
        assert all(tt == "EXTERNAL_IDP" for _, tt in sent)

    def test_other_candidate_sends_none(self):
        sent = self._fetch("")
        assert {t for t, _ in sent} == {api._TARGET_LIST_PROFILES, api._TARGET_GET_USAGE}
        assert all(tt == "" for _, tt in sent)
