"""Tests for voice_reply module — text preparation and provider resolution.

Markdown stripping, SSML, rate and pitch validation and sentence splitting, then
which TTS provider and engine a host resolves (system, Polly), the system
engine's rate mapping and SAPI command, and how the system voices are listed. Each provider's synthesis is covered in
``test_voice_reply_synthesis.py``; the pipeline around it (dispatch, upload,
streaming, stitching) in ``test_voice_reply_pipeline.py``.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import tempfile
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from voice_reply_helpers import _no_argv_prefixers  # noqa: F401
from voice_reply_helpers import _FAKE_AWS_CLI, _mock_subprocess

from kiro_crew.voice_reply import (
    _SAY_VOICE_RE,
    DEFAULT_LENGTH_SCALE,
    DEFAULT_PITCH,
    DEFAULT_PROVIDER,
    DEFAULT_RATE,
    PROVIDER_PIPER,
    PROVIDER_POLLY,
    PROVIDER_SYSTEM,
    SYSTEM_ENGINE_ESPEAK,
    SYSTEM_ENGINE_SAPI,
    SYSTEM_ENGINE_SAY,
    VALID_ENGINES,
    VALID_PROVIDERS,
    SystemVoiceProbeError,
    _parse_system_voices,
    _resolve_piper_binary,
    _sapi_encoded_command,
    _sapi_rate,
    _synthesize_piper,
    _system_wpm,
    _validate_pitch,
    _validate_rate,
    is_available,
    list_system_voices,
    resolve_configured_provider,
    resolve_system_tts,
    resolve_system_tts_async,
    split_sentences,
    stream_pocket_speech,
    strip_markdown,
    synthesis_settings,
    synthesize_speech,
    text_to_ssml,
    validate_length_scale,
)


class TestStripMarkdown:
    def test_removes_code_blocks(self) -> None:
        assert strip_markdown("before ```code``` after") == "before (code block) after"

    def test_removes_inline_code(self) -> None:
        assert strip_markdown("use `foo` here") == "use foo here"

    def test_removes_slack_links(self) -> None:
        assert strip_markdown("<https://example.com|Example>") == "Example"
        assert strip_markdown("<https://example.com>") == "(link)"

    def test_removes_markdown_links(self) -> None:
        assert strip_markdown("[click](https://example.com)") == "click"

    def test_a_balanced_pair_in_a_link_url_leaves_only_the_label(self) -> None:
        url = "https://en.wikipedia.org/wiki/Python_(programming_language)"
        assert strip_markdown(f"see [Python]({url}) now") == "see Python now"
        assert strip_markdown("see [docs](https://example.com/a_(b)_c) now") == "see docs now"

    def test_a_web_address_in_an_unbalanced_link_is_never_spoken(self) -> None:
        """An unbalanced ``(`` makes this text, not a link, so it stays as written
        and the bare-URL pass replaces the web address."""
        assert strip_markdown("see [a](https://example.com/(b) now") == "see [a]( (link) now"

    def test_a_relative_destination_in_an_unbalanced_link_is_spoken_as_written(self) -> None:
        """The bare-URL pass only knows web addresses, so nothing shortens this one."""
        text = "see [guide](docs/setup(old) now"
        assert strip_markdown(text) == text

    def test_removes_bold_italic(self) -> None:
        assert strip_markdown("**bold** and *italic*") == "bold and italic"

    def test_removes_emoji_shortcodes(self) -> None:
        assert strip_markdown("hello :wave: world") == "hello world"

    def test_preserves_plain_text(self) -> None:
        assert strip_markdown("hello world") == "hello world"

    def test_removes_control_tag_comments(self) -> None:
        # Trailing control-tag LINES are stripped — the tail-anchored grammar
        # shared with the frontend recognizer. Stacked tags all go.
        assert strip_markdown("report body\n<!-- keep-visible -->") == "report body"
        assert strip_markdown("done\n<!-- deliver:dashboard -->") == "done"
        assert strip_markdown("report\n<!-- keep-visible -->\n<!-- deliver:slack -->") == "report"

    def test_mid_body_and_same_line_tags_are_rendered_content(self) -> None:
        # Only tail LINES are control tags: producers emit "as its final
        # line" (prompt contract; heartbeat/task-planner both append). A tag
        # mid-body or trailing on a prose line is content, never stripped —
        # this is what makes a tag quoted in ANY code dialect untouchable.
        assert "deliver" in strip_markdown("done <!-- deliver:dashboard --> ok")
        assert "keep-visible" in strip_markdown("prose tail <!-- keep-visible -->")

    def test_ordinary_html_comments_are_preserved(self) -> None:
        # Scoped to known control tags, never all comments: an ordinary
        # comment quoted in inline code is visible content the user asked
        # about — a generic strip deleted it from speech.
        assert strip_markdown("use `<!-- ordinary -->` here") == "use <!-- ordinary --> here"

    def test_recognized_tag_quoted_in_inline_code_is_preserved(self) -> None:
        # A RECOGNIZED tag in inline code renders literally — quoted visible
        # content, so speech keeps it (round-5 grammar unification).
        assert strip_markdown("the `<!-- keep-visible -->` tag") == (
            "the <!-- keep-visible --> tag"
        )

    def test_plan_task_id_tag_is_not_spoken(self) -> None:
        # Third control-tag family; task_planner appends "\n<!-- plan_task_id:… -->",
        # so the tag arrives as its own trailing line.
        assert strip_markdown("plan ready\n<!-- plan_task_id:abc123 -->") == "plan ready"

    def test_html_comment_inside_fence_is_already_placeholdered(self) -> None:
        # Fences are replaced before the comment strip runs, so a comment
        # inside code cannot swallow surrounding prose.
        assert strip_markdown("a ```<!-- not a tag -->``` b") == "a (code block) b"

    def test_unterminated_control_tag_no_longer_swallows_text(self) -> None:
        # A truncated control tag leaks literally instead of silently eating
        # the rest of the message (visible garbage beats silent data loss).
        assert strip_markdown("safe part <!-- broken") == "safe part <!-- broken"

    def test_credential_rejoined_by_comment_strip_is_redacted(self) -> None:
        # A control tag interposed inside a key id splits it, so a redaction
        # scan on the RAW text misses it; the strip rejoins the halves. The
        # post-strip redaction pass must catch the reconstructed secret
        # before it reaches TTS.
        out = strip_markdown("key AKIAIOSF<!-- keep-visible -->ODNN7EXAMPLE end")
        assert "AKIAIOSFODNN7EXAMPLE" not in out

    def test_credential_rejoined_by_emphasis_strip_is_redacted(self) -> None:
        # Same class, different strip: `**` emphasis markers inside a key id
        # are removed by the [*_~]+ pass. The invariant covers every strip,
        # not just the control-tag one.
        out = strip_markdown("key AKIAIOSF**ODNN7EXAMPLE** end")
        assert "AKIAIOSFODNN7EXAMPLE" not in out

    def test_contiguous_credential_still_redacted_after_strip(self) -> None:
        # Idempotence: a credential the pre-strip scan would catch is also
        # caught by the post-strip pass when strip_markdown is used alone
        # (split_sentences path has no pre-strip redaction).
        out = strip_markdown("key AKIAIOSFODNN7EXAMPLE end")
        assert "AKIAIOSFODNN7EXAMPLE" not in out

    def test_control_tag_regex_linear_on_adversarial_input(self) -> None:
        # CodeQL py/polynomial-redos, two vectors: (a) "<!--deliver:" + many
        # tabs (adjacent-quantifier ambiguity); (b) the
        # repeated prefix "<!--deliver:" * n, where an UNBOUNDED body lets
        # each of n start positions rescan an O(n) tail = quadratic.
        # Bounding every quantifier keeps a failed attempt constant work.
        # This times the shared helper; strip_markdown's other passes are
        # not under test.
        # Polynomial time at this size hangs for minutes; linear completes
        # in milliseconds. Generous bound for slow CI.
        import time

        from kiro_crew.constants import strip_control_comments

        start = time.monotonic()
        out_tabs = strip_control_comments("<!--deliver:" + "\t" * 50_000)
        out_reps = strip_control_comments("<!--deliver:" * 20_000)
        assert time.monotonic() - start < 5.0
        # Unterminated tags are preserved (no swallow), not stripped.
        assert out_tabs.startswith("<!--deliver:")
        assert out_reps.startswith("<!--deliver:")

    def test_unterminated_tag_preserved_through_full_strip(self) -> None:
        # Same no-swallow contract through the full TTS pipeline.
        assert strip_markdown("safe <!--deliver:oops").startswith("safe <!--deliver:oops")

    def test_oversized_tag_body_is_not_treated_as_control_tag(self) -> None:
        # The body bound (256) is what makes matching linear; a "tag" larger
        # than any real emission is left visible rather than stripped.
        big = "<!-- deliver:" + "x" * 300 + " -->"
        assert strip_markdown(f"before {big} after").strip() != "before after"

    def test_collapses_whitespace(self) -> None:
        assert strip_markdown("a\n\n\n\nb") == "a\n\nb"

    def test_preserves_bullet_lists(self) -> None:
        result = strip_markdown("- item one\n- item two")
        assert "item one" in result
        assert "item two" in result


class TestTextToSsml:
    def test_empty_input(self) -> None:
        assert text_to_ssml("") == ""

    def test_basic_ssml(self) -> None:
        result = text_to_ssml("Hello world")
        assert result.startswith("<speak>")
        assert result.endswith("</speak>")
        assert "Hello world" in result

    def test_includes_rate(self) -> None:
        result = text_to_ssml("test", rate="110%")
        assert 'rate="110%"' in result

    def test_returns_ssml_without_prosody_for_neural(self) -> None:
        result = text_to_ssml("test", pitch="+10%", engine="neural")
        assert result.startswith("<speak>")
        assert "</speak>" in result
        assert "<prosody" not in result
        assert "pitch" not in result

    def test_neural_escapes_xml_entities(self) -> None:
        result = text_to_ssml("a & b < c > d", engine="neural")
        assert "&amp;" in result
        assert "&lt;" in result
        assert "&gt;" in result
        assert "<prosody" not in result

    def test_neural_adds_break_tags(self) -> None:
        result = text_to_ssml("para one\n\npara two", engine="neural")
        assert 'break time="600ms"' in result
        assert "<prosody" not in result

    def test_neural_truncates_long_text(self) -> None:
        long_text = "word. " * 1000
        result = text_to_ssml(long_text, engine="neural")
        assert result.startswith("<speak>")
        assert "</speak>" in result
        assert "<prosody" not in result
        # Should be truncated
        assert len(result) < len(long_text) + 100

    def test_excludes_pitch_for_generative(self) -> None:
        result = text_to_ssml("test", pitch="+10%", engine="generative")
        assert "pitch" not in result

    def test_excludes_pitch_for_long_form(self) -> None:
        result = text_to_ssml("test", pitch="+10%", engine="long-form")
        assert "pitch" not in result

    def test_escapes_xml_entities(self) -> None:
        result = text_to_ssml("a & b < c > d")
        assert "&amp;" in result
        assert "&lt;" in result
        assert "&gt;" in result

    def test_truncates_long_text(self) -> None:
        long_text = "word. " * 1000
        result = text_to_ssml(long_text)
        # Should end with a period (sentence boundary truncation)
        assert result.endswith("</prosody></speak>")

    def test_adds_paragraph_breaks(self) -> None:
        result = text_to_ssml("para one\n\npara two")
        assert 'break time="600ms"' in result

    def test_adds_line_breaks(self) -> None:
        result = text_to_ssml("line one\nline two")
        assert 'break time="300ms"' in result


class TestValidation:
    def test_valid_rate(self) -> None:
        assert _validate_rate("95%") == "95%"
        assert _validate_rate("110%") == "110%"
        assert _validate_rate("50%") == "50%"

    def test_invalid_rate_returns_default(self) -> None:
        assert _validate_rate("banana") == DEFAULT_RATE
        assert _validate_rate("") == DEFAULT_RATE
        assert _validate_rate("1000%") == DEFAULT_RATE

    def test_valid_pitch(self) -> None:
        assert _validate_pitch("+10%") == "+10%"
        assert _validate_pitch("-5%") == "-5%"
        assert _validate_pitch("+0%") == "+0%"

    def test_invalid_pitch_returns_default(self) -> None:
        assert _validate_pitch("banana") == DEFAULT_PITCH
        assert _validate_pitch("10%") == DEFAULT_PITCH  # missing +/-
        assert _validate_pitch("") == DEFAULT_PITCH

    def test_valid_engines(self) -> None:
        assert "neural" in VALID_ENGINES
        assert "generative" in VALID_ENGINES
        assert "long-form" in VALID_ENGINES
        assert "standard" in VALID_ENGINES
        assert "invalid" not in VALID_ENGINES


class TestSplitSentences:
    def test_multi_sentence(self) -> None:
        assert split_sentences("Hello world. How are you?") == [
            "Hello world.",
            "How are you?",
        ]

    def test_single_sentence(self) -> None:
        assert split_sentences("Hello world.") == ["Hello world."]

    def test_empty_input(self) -> None:
        assert split_sentences("") == []

    def test_strips_markdown_before_splitting(self) -> None:
        assert split_sentences("**Bold sentence.** Another one.") == [
            "Bold sentence.",
            "Another one.",
        ]


# ── Provider constants ──────────────────────────────────────────────────


class TestProviderConstants:
    def test_constants_defined(self) -> None:
        assert PROVIDER_POLLY == "polly"
        assert PROVIDER_SYSTEM == "system"
        # The host's built-in engine is the default because it is the only
        # provider that needs nothing installed, so auto-speak works on a fresh
        # machine. The paired assertion is the load-bearing one: whatever the
        # default becomes, it must never be the paid cloud provider.
        assert DEFAULT_PROVIDER == PROVIDER_SYSTEM
        assert DEFAULT_PROVIDER != PROVIDER_POLLY
        assert PROVIDER_POLLY in VALID_PROVIDERS
        assert "piper" in VALID_PROVIDERS
        assert "pocket" in VALID_PROVIDERS
        assert PROVIDER_SYSTEM in VALID_PROVIDERS


# ── is_available() ──────────────────────────────────────────────────────


class TestIsAvailable:
    def test_polly_available_when_aws_on_path(self) -> None:
        with patch("kiro_crew.voice_reply.shutil.which", return_value="/usr/bin/aws"):
            assert is_available(PROVIDER_POLLY) is True

    def test_polly_unavailable_when_aws_missing(self) -> None:
        with patch("kiro_crew.voice_reply.shutil.which", return_value=None):
            assert is_available(PROVIDER_POLLY) is False

    def test_unknown_provider_returns_false(self, caplog) -> None:
        assert is_available("bogus") is False

    def test_system_available_when_engine_resolves(self) -> None:
        with patch(
            "kiro_crew.voice_reply.resolve_system_tts",
            return_value=(SYSTEM_ENGINE_SAY, "/usr/bin/say"),
        ):
            assert is_available(PROVIDER_SYSTEM) is True

    def test_system_unavailable_without_engine(self) -> None:
        with patch("kiro_crew.voice_reply.resolve_system_tts", return_value=None):
            assert is_available(PROVIDER_SYSTEM) is False


# ── resolve_system_tts() ─────────────────────────────────────────────────


class TestResolveSystemTts:
    def test_macos_uses_say(self) -> None:
        with (
            patch("kiro_crew.voice_reply.IS_MACOS", True),
            patch("kiro_crew.voice_reply.IS_WINDOWS", False),
            patch("kiro_crew.voice_reply.trusted_system_bin", return_value="/usr/bin/say") as probe,
        ):
            assert resolve_system_tts() == (SYSTEM_ENGINE_SAY, "/usr/bin/say")
        probe.assert_called_once_with("say")

    def test_windows_uses_powershell_five(self) -> None:
        # Windows PowerShell specifically: System.Speech is .NET-Framework-only
        # and throws in pwsh 7, so resolving anything else would report a
        # provider as available that fails on every call.
        ps = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        with (
            patch("kiro_crew.voice_reply.IS_MACOS", False),
            patch("kiro_crew.voice_reply.IS_WINDOWS", True),
            patch("kiro_crew.voice_reply.trusted_system_bin", return_value=ps) as probe,
        ):
            assert resolve_system_tts() == (SYSTEM_ENGINE_SAPI, ps)
        probe.assert_called_once_with("powershell")

    def test_linux_prefers_espeak_ng_then_espeak(self) -> None:
        calls: list[str] = []

        def probe(name: str) -> str | None:
            calls.append(name)
            return "/usr/bin/espeak" if name == "espeak" else None

        with (
            patch("kiro_crew.voice_reply.IS_MACOS", False),
            patch("kiro_crew.voice_reply.IS_WINDOWS", False),
            patch("kiro_crew.voice_reply.trusted_system_bin", side_effect=probe),
        ):
            assert resolve_system_tts() == (SYSTEM_ENGINE_ESPEAK, "/usr/bin/espeak")
        assert calls == ["espeak-ng", "espeak"]

    def test_linux_without_engine_returns_none(self) -> None:
        # The normal answer on a stock server image, not a broken host.
        with (
            patch("kiro_crew.voice_reply.IS_MACOS", False),
            patch("kiro_crew.voice_reply.IS_WINDOWS", False),
            patch("kiro_crew.voice_reply.trusted_system_bin", return_value=None),
        ):
            assert resolve_system_tts() is None

    def test_macos_without_say_returns_none(self) -> None:
        with (
            patch("kiro_crew.voice_reply.IS_MACOS", True),
            patch("kiro_crew.voice_reply.IS_WINDOWS", False),
            patch("kiro_crew.voice_reply.trusted_system_bin", return_value=None),
        ):
            assert resolve_system_tts() is None


# ── system speed mapping ─────────────────────────────────────────────────


class TestResolveSystemTtsAsync:
    @pytest.mark.asyncio
    async def test_resolution_runs_off_the_event_loop(self) -> None:
        """Directory stats are not bounded, and one loop serves every session.

        A fixed directory on a stalled network or fuse mount would freeze the
        gateway and its heartbeats, so resolution is handed to a worker thread.
        """
        loop_thread = threading.get_ident()
        seen: list[int] = []

        def slow_resolve() -> tuple[str, str] | None:
            seen.append(threading.get_ident())
            return (SYSTEM_ENGINE_SAY, "/usr/bin/say")

        with patch("kiro_crew.voice_reply.resolve_system_tts", side_effect=slow_resolve):
            assert await resolve_system_tts_async() == (SYSTEM_ENGINE_SAY, "/usr/bin/say")
        assert seen and seen[0] != loop_thread


# ── system speed mapping ─────────────────────────────────────────────────


class TestSystemRateMapping:
    def test_wpm_scales_from_the_engine_default(self) -> None:
        assert _system_wpm("100%") == 175
        assert _system_wpm("200%") == 350
        assert _system_wpm("50%") == 88

    def test_wpm_is_clamped_to_a_speakable_band(self) -> None:
        # A rate the UI cannot produce but config.json can.
        assert _system_wpm("999%") == 500
        assert _system_wpm("1%") == 80

    def test_wpm_falls_back_on_an_invalid_rate(self) -> None:
        assert _system_wpm("fast") == 175

    def test_a_non_string_rate_does_not_crash(self) -> None:
        # config.json is JSON, so a hand-edited `"rate": 100` arrives as an int
        # and can reach `_RATE_RE.match` as a non-string. The built-in engine
        # is the default, so that typo would drop the audio of every reply.
        assert _system_wpm(100) == 175  # type: ignore[arg-type]
        assert _sapi_rate(100) == 0  # type: ignore[arg-type]
        assert _system_wpm(None) == 175  # type: ignore[arg-type]
        assert _sapi_rate(None) == 0  # type: ignore[arg-type]

    def test_the_validators_themselves_absorb_a_non_string(self) -> None:
        """Pinned on the validators, not their callers.

        The coercion lives inside ``_validate_rate`` / ``_validate_pitch`` so
        every reader is covered at once. Guarding only the built-in engine's two
        call sites left ``text_to_ssml`` — reached from both Polly paths — with
        the same TypeError, and left pitch unguarded entirely.
        """
        assert _validate_rate(100) == "100%"  # type: ignore[arg-type]
        assert _validate_rate(None) == "100%"  # type: ignore[arg-type]
        assert _validate_rate([]) == "100%"  # type: ignore[arg-type]
        assert _validate_pitch(0) == "+0%"  # type: ignore[arg-type]
        assert _validate_pitch(None) == "+0%"  # type: ignore[arg-type]
        assert _validate_pitch({}) == "+0%"  # type: ignore[arg-type]
        # The SSML builder is the third caller; a non-string must not reach it
        # as one. A pitch that lands on the default is omitted from the prosody
        # by design, so only the rate is asserted here.
        ssml = text_to_ssml("hi", rate=100, pitch=0)  # type: ignore[arg-type]
        assert 'rate="100%"' in ssml and "pitch=" not in ssml
        assert 'rate="100%"' in text_to_ssml("hi", rate=100, pitch=-25)  # type: ignore[arg-type]

    def test_sapi_rate_centres_on_zero(self) -> None:
        assert _sapi_rate("100%") == 0
        assert _sapi_rate("150%") == 5
        assert _sapi_rate("80%") == -2

    def test_sapi_rate_is_clamped_to_the_api_range(self) -> None:
        assert _sapi_rate("999%") == 10
        assert _sapi_rate("1%") == -10


# ── _sapi_encoded_command() ──────────────────────────────────────────────


class TestSapiEncodedCommand:
    def _decode(self, payload: str) -> str:
        return base64.b64decode(payload).decode("utf-16-le")

    def test_payload_is_utf16le_base64(self) -> None:
        script = self._decode(_sapi_encoded_command("out.wav", "in.txt", "", 0))
        assert "System.Speech" in script
        assert "SetOutputToWaveFile" in script

    def test_spoken_text_is_never_in_the_script(self) -> None:
        # The whole reason the text goes to a file: nothing a model produced may
        # reach a command line, where quoting decides how it is parsed.
        script = self._decode(_sapi_encoded_command("out.wav", "in.txt", "", 0))
        assert "ReadAllText('in.txt'" in script
        assert "Speak($t)" in script

    def test_quotes_in_paths_are_escaped(self) -> None:
        script = self._decode(_sapi_encoded_command("o'ut.wav", "in.txt", "", 0))
        assert "'o''ut.wav'" in script

    def test_the_voice_name_is_read_from_a_file_never_the_script(self) -> None:
        """The configured voice must not reach argv, only its temp path.

        This is what makes the Windows argv derived entirely inside the package,
        which is the claim ``first_party_fixed_argv`` rests on at the sandbox
        chokepoint. A name interpolated into the script would be user config
        travelling on the command line.
        """
        without = self._decode(_sapi_encoded_command("o.wav", "i.txt", "", 0))
        assert "SelectVoice" not in without

        script = self._decode(_sapi_encoded_command("o.wav", "i.txt", "/tmp/v.txt", 0))
        assert "ReadAllText('/tmp/v.txt'" in script
        assert "SelectVoice($v)" in script
        # The name itself appears nowhere — only the path it is read from.
        assert "Zira" not in script

    def test_rate_is_interpolated_as_an_integer(self) -> None:
        assert "$s.Rate=-3;" in self._decode(_sapi_encoded_command("o.wav", "i.txt", "", -3))


# ── _parse_system_voices() ───────────────────────────────────────────────


class TestParseSystemVoices:
    def test_say_listing(self) -> None:
        out = (
            "Alex                en_US    # Most people recognize me by my voice.\n"
            "Grandma (Deutsch)   de_DE    # Ich bin die Oma.\n"
            "Tingting            zh_CN    # 你好。\n"
        )
        assert _parse_system_voices(SYSTEM_ENGINE_SAY, out) == [
            {"id": "Grandma (Deutsch)", "name": "Grandma (Deutsch)", "language": "de-DE"},
            {"id": "Alex", "name": "Alex", "language": "en-US"},
            {"id": "Tingting", "name": "Tingting", "language": "zh-CN"},
        ]

    def test_say_name_containing_spaces_is_not_split(self) -> None:
        # Two-space padding is the only field separator, so a name with an
        # internal space would otherwise be truncated to its first word.
        rows = _parse_system_voices(SYSTEM_ENGINE_SAY, "Eddy (English (UK))  en_GB    # Hello.\n")
        assert rows[0]["name"] == "Eddy (English (UK))"

    def test_espeak_listing_skips_header(self) -> None:
        out = (
            "Pty Language       Age/Gender VoiceName          File\n"
            " 5  af              --/M      Afrikaans          gmw/af\n"
            " 5  en-gb           --/M      English_(GB)       gmw/en\n"
        )
        assert _parse_system_voices(SYSTEM_ENGINE_ESPEAK, out) == [
            {"id": "af", "name": "Afrikaans", "language": "af"},
            {"id": "en-gb", "name": "English_(GB)", "language": "en-gb"},
        ]

    def test_sapi_listing(self) -> None:
        out = "Microsoft Zira Desktop|en-US\nMicrosoft Hanhan Desktop|zh-CN\n"
        assert _parse_system_voices(SYSTEM_ENGINE_SAPI, out) == [
            {
                "id": "Microsoft Zira Desktop",
                "name": "Microsoft Zira Desktop",
                "language": "en-US",
            },
            {
                "id": "Microsoft Hanhan Desktop",
                "name": "Microsoft Hanhan Desktop",
                "language": "zh-CN",
            },
        ]

    def test_duplicates_are_collapsed(self) -> None:
        out = "Alex                en_US    # a\nAlex                en_US    # a\n"
        assert len(_parse_system_voices(SYSTEM_ENGINE_SAY, out)) == 1

    def test_blank_and_unparseable_lines_are_skipped(self) -> None:
        assert _parse_system_voices(SYSTEM_ENGINE_SAY, "\n   \ngarbage\n") == []

    def test_the_locale_group_cannot_backtrack_exponentially(self) -> None:
        """The locale tail must exclude the separators that delimit it.

        With ``\\w`` the tail could also match ``-``/``_``, so a run like
        ``_0_0_0`` has many valid partitions and a non-matching line makes the
        engine try them all — measured at 34ms for 20 repetitions and growing
        ~3.7x per further two, i.e. hours by 40. A voice listing is engine
        output, but a third-party installed voice supplies its own name.

        Asserted as SHAPE first, because that is deterministic: no separator may
        appear in the repeated group's character class. The timing check is a
        generously-bounded backstop for a future rewrite that reintroduces the
        ambiguity some other way.
        """
        # The repeated group is `(?:<sep class><tail class>+)`. Ambiguity exists
        # exactly when the tail class can also match a separator, so compile it
        # and ask, instead of pattern-matching the source text.
        pattern = _SAY_VOICE_RE.pattern
        group = pattern[pattern.index("(?:") : pattern.index(")*")]
        classes = re.findall(r"\[[^\]]*\]", group)
        assert len(classes) == 2, f"unexpected locale group shape: {group}"
        tail = re.compile(classes[1])
        for sep in ("-", "_"):
            assert not tail.match(sep), f"locale tail also matches {sep!r}: {classes[1]}"

        adversarial = "Voice  en" + "_0" * 5_000 + "!\n"
        start = time.perf_counter()
        assert _parse_system_voices(SYSTEM_ENGINE_SAY, adversarial) == []
        # Linear parsing lands near a millisecond; the ambiguous form would not
        # finish. The ceiling is loose on purpose — this is not a benchmark.
        assert time.perf_counter() - start < 2.0


# ── list_system_voices() ─────────────────────────────────────────────────


class TestListSystemVoices:
    """A failed probe must be distinguishable from a host with no engine.

    If both answer with an empty list, the endpoint reports
    ``available: true`` and the panel renders a picker holding only the OS
    default — which reads as "this host has one voice", not as a failure the
    user can retry.
    """

    @pytest.mark.asyncio
    async def test_no_engine_is_an_empty_list_not_an_error(self) -> None:
        with patch("kiro_crew.voice_reply.resolve_system_tts", return_value=None):
            assert await list_system_voices() == []

    @pytest.mark.asyncio
    async def test_a_spawn_failure_raises(self) -> None:
        with (
            patch(
                "kiro_crew.voice_reply.resolve_system_tts",
                return_value=(SYSTEM_ENGINE_SAY, "/usr/bin/say"),
            ),
            patch("asyncio.create_subprocess_exec", side_effect=OSError("no exec")),
        ):
            with pytest.raises(SystemVoiceProbeError):
                await list_system_voices()

    @pytest.mark.asyncio
    async def test_a_nonzero_exit_raises(self) -> None:
        async def fake_exec(*_cmd, **_kw):
            return _mock_subprocess(returncode=3, stderr=b"bad flag")

        with (
            patch(
                "kiro_crew.voice_reply.resolve_system_tts",
                return_value=(SYSTEM_ENGINE_SAY, "/usr/bin/say"),
            ),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            with pytest.raises(SystemVoiceProbeError):
                await list_system_voices()

    @pytest.mark.asyncio
    async def test_a_timeout_raises_and_reaps(self) -> None:
        proc = _mock_subprocess(returncode=0)
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)

        async def fake_exec(*_cmd, **_kw):
            return proc

        with (
            patch(
                "kiro_crew.voice_reply.resolve_system_tts",
                return_value=(SYSTEM_ENGINE_SAY, "/usr/bin/say"),
            ),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            with pytest.raises(SystemVoiceProbeError):
                await list_system_voices()
        # A probe left running would hold the pipe open for the process's life.
        proc.kill.assert_called()

    @pytest.mark.asyncio
    async def test_cancellation_still_propagates_as_cancellation(self) -> None:
        proc = _mock_subprocess(returncode=0)
        proc.communicate = AsyncMock(side_effect=asyncio.CancelledError)

        async def fake_exec(*_cmd, **_kw):
            return proc

        with (
            patch(
                "kiro_crew.voice_reply.resolve_system_tts",
                return_value=(SYSTEM_ENGINE_SAY, "/usr/bin/say"),
            ),
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        ):
            with pytest.raises(asyncio.CancelledError):
                await list_system_voices()


# ── resolve_polly_cli() ─────────────────────────────────────────


class TestResolvePollyCli:
    @pytest.mark.skipif(
        os.name == "nt",
        reason="fallback install dirs are POSIX literals; dead on Windows by design",
    )
    def test_resolved_absolutely_under_minimal_path(self, monkeypatch, tmp_path) -> None:
        """A GUI-launched gateway's minimal PATH must still resolve the CLI
        absolutely via the deploy engine's well-known-dirs resolver instead of
        silently skipping TTS."""
        from kiro_crew import github_runner, voice_reply
        from kiro_crew.deploy import engine

        fake_aws = tmp_path / "aws"
        fake_aws.write_text("#!/bin/sh\n")
        fake_aws.chmod(0o755)
        empty_bin = tmp_path / "emptybin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", str(empty_bin))
        monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(tmp_path),))
        monkeypatch.setattr(github_runner, "validate_provider_executable", lambda c: c)

        assert voice_reply.resolve_polly_cli() == str(fake_aws)
        # The converted is_available() probe site sees the same resolution.
        assert is_available(PROVIDER_POLLY) is True

    def test_none_when_cli_absent_everywhere(self, monkeypatch, tmp_path) -> None:
        """Bare-name fallback that is not invocable maps to None — the value
        every probe site already treats as 'unavailable'."""
        from kiro_crew import voice_reply
        from kiro_crew.deploy import engine

        empty_bin = tmp_path / "emptybin"
        empty_bin.mkdir()
        monkeypatch.setenv("PATH", str(empty_bin))
        monkeypatch.setattr(engine, "_AWS_BIN_DIRS", ())

        assert voice_reply.resolve_polly_cli() is None
        assert is_available(PROVIDER_POLLY) is False


# ── resolve_configured_provider() ────────────────────────────────────────


@pytest.fixture
def fresh_removed_warning(monkeypatch):
    """The removed-provider notice fires once per process; give each test its own."""
    monkeypatch.setattr("kiro_crew.voice_reply._warned_removed_providers", set())


class TestResolveConfiguredProvider:
    def test_named_provider_is_kept(self) -> None:
        for name in (PROVIDER_SYSTEM, PROVIDER_POLLY):
            assert resolve_configured_provider({"provider": name}) == name

    def test_absent_section_resolves_to_the_default(self) -> None:
        assert resolve_configured_provider(None) == DEFAULT_PROVIDER
        assert resolve_configured_provider({}) == DEFAULT_PROVIDER

    def test_invalid_provider_warns_and_never_reaches_polly(self, caplog) -> None:
        import logging

        with caplog.at_level(logging.WARNING, logger="kiro_crew.voice_reply"):
            assert resolve_configured_provider({"provider": "ploly"}) == DEFAULT_PROVIDER
        assert DEFAULT_PROVIDER != PROVIDER_POLLY
        assert any("ploly" in rec.message for rec in caplog.records)

    def test_unhashable_provider_does_not_raise(self) -> None:
        # config.json can hold a list or dict where a string belongs; `in
        # VALID_PROVIDERS` would raise TypeError on those.
        assert resolve_configured_provider({"provider": ["polly"]}) == DEFAULT_PROVIDER
        assert resolve_configured_provider({"provider": {"a": 1}}) == DEFAULT_PROVIDER


class TestRetainedLocalProviders:
    """Configured local engines remain selected while new setups use the system default."""

    def test_a_named_piper_provider_resolves_to_the_local_default(
        self, caplog, fresh_removed_warning
    ) -> None:
        import logging

        with caplog.at_level(logging.WARNING, logger="kiro_crew.voice_reply"):
            got = resolve_configured_provider({"provider": "piper", "piper_model": "~/v.onnx"})
        assert got == PROVIDER_PIPER
        notices = [r.message for r in caplog.records if "was removed" in r.message]
        assert notices == []

    def test_an_unnamed_provider_with_a_piper_model_resolves_to_the_default(
        self, caplog, fresh_removed_warning
    ) -> None:
        # The pre-system-default upgrade shape: no provider, only a model path.
        import logging

        with caplog.at_level(logging.WARNING, logger="kiro_crew.voice_reply"):
            assert resolve_configured_provider({"piper_model": "~/v.onnx"}) == PROVIDER_PIPER
            assert resolve_configured_provider({"provider": "", "piper_model": "~/v.onnx"}) == (
                PROVIDER_PIPER
            )
        assert not any("was removed" in r.message for r in caplog.records)

    @pytest.mark.parametrize("malformed", [{}, [], 0, 3, None, "", "   "])
    def test_a_malformed_piper_model_resolves_quietly(
        self, malformed, caplog, fresh_removed_warning
    ) -> None:
        import logging

        with caplog.at_level(logging.WARNING, logger="kiro_crew.voice_reply"):
            assert resolve_configured_provider({"piper_model": malformed}) == DEFAULT_PROVIDER
        assert not any("was removed" in r.message for r in caplog.records)

    def test_retained_local_engines_do_not_emit_a_removal_notice(
        self, caplog, fresh_removed_warning
    ) -> None:
        import logging

        with caplog.at_level(logging.WARNING, logger="kiro_crew.voice_reply"):
            for _ in range(3):
                resolve_configured_provider({"provider": "piper"})
        assert not any("was removed" in r.message for r in caplog.records)

    def test_an_explicit_provider_outranks_a_leftover_model(
        self, caplog, fresh_removed_warning
    ) -> None:
        import logging

        with caplog.at_level(logging.WARNING, logger="kiro_crew.voice_reply"):
            got = resolve_configured_provider(
                {"provider": PROVIDER_POLLY, "piper_model": "~/voices/en.onnx"}
            )
        assert got == PROVIDER_POLLY
        assert not any("was removed" in r.message for r in caplog.records)

    def test_local_provider_keys_reach_synthesis_settings(self, fresh_removed_warning) -> None:
        settings = synthesis_settings(
            {
                "provider": "piper",
                "piper_binary": "/opt/piper",
                "piper_model": "~/v.onnx",
                "piper_length_scale": 0.8,
            }
        )
        assert settings["provider"] == PROVIDER_PIPER
        assert settings["piper_binary"] == "/opt/piper"
        assert settings["piper_model"] == "~/v.onnx"
        assert settings["length_scale"] == 0.8


class TestValidationForkContracts:
    def test_valid_length_scale(self) -> None:
        assert validate_length_scale(1.5) == 1.5
        assert validate_length_scale("0.85") == 0.85
        assert validate_length_scale(2) == 2.0

    def test_invalid_length_scale_returns_default(self) -> None:
        # Non-numeric, non-finite, zero/negative, and OverflowError (huge int)
        # all fall back to the default rather than reaching synthesis or being
        # persisted as unserializable JSON.
        for bad in ["fast", None, float("inf"), float("nan"), 0, -1.0, 10**400, [1]]:
            assert validate_length_scale(bad) == DEFAULT_LENGTH_SCALE


class TestIsAvailableForkContracts:
    def test_piper_unavailable_when_binary_missing(self, tmp_path) -> None:
        model = tmp_path / "voice.onnx"
        model.write_bytes(b"fake")
        with patch(
            "kiro_crew.voice_reply._resolve_piper_binary",
            return_value=None,
        ):
            assert (
                is_available(
                    PROVIDER_PIPER,
                    piper_binary="",
                    piper_model=str(model),
                )
                is False
            )

    def test_piper_unavailable_when_model_empty(self, tmp_path) -> None:
        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        with patch(
            "kiro_crew.voice_reply._resolve_piper_binary",
            return_value=str(bin_path),
        ):
            assert is_available(PROVIDER_PIPER, piper_model="") is False

    def test_piper_unavailable_when_model_file_missing(self, tmp_path) -> None:
        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        with patch(
            "kiro_crew.voice_reply._resolve_piper_binary",
            return_value=str(bin_path),
        ):
            # Model path provided but file doesn't exist.
            assert (
                is_available(
                    PROVIDER_PIPER,
                    piper_model=str(tmp_path / "nope.onnx"),
                )
                is False
            )

    def test_piper_available_when_binary_and_model_exist(self, tmp_path) -> None:
        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        model = tmp_path / "voice.onnx"
        model.write_bytes(b"fake model")
        with patch(
            "kiro_crew.voice_reply._resolve_piper_binary",
            return_value=str(bin_path),
        ):
            assert (
                is_available(
                    PROVIDER_PIPER,
                    piper_model=str(model),
                )
                is True
            )


class TestResolvePiperBinaryForkContracts:
    def test_configured_path_preferred(self, tmp_path) -> None:
        bin_path = tmp_path / "my-piper"
        _make_executable(str(bin_path))
        assert _resolve_piper_binary(str(bin_path)) == str(bin_path)

    def test_configured_path_missing_returns_none(self, tmp_path) -> None:
        assert _resolve_piper_binary(str(tmp_path / "nope")) is None

    def test_windows_rejects_a_non_launchable_suffix(self, tmp_path, monkeypatch) -> None:
        """A `.txt` must not pass as a Windows binary.

        `os.access(X_OK)` calls any readable file executable there, so without a
        suffix check a configured notes file reports the provider available, the
        caller skips its "voice unavailable" notice, and the spawn then fails
        with nothing shown to the user.
        """
        monkeypatch.setattr("kiro_crew.voice_reply.IS_WINDOWS", True)
        monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
        notes = tmp_path / "piper_notes.txt"
        notes.write_text("not a binary")
        assert _resolve_piper_binary(str(notes)) is None
        exe = tmp_path / "piper.exe"
        exe.write_text("stub")
        assert _resolve_piper_binary(str(exe)) == str(exe)
        # Case is not significant on Windows, and PATHEXT is operator-extensible.
        ps1 = tmp_path / "piper.PS1"
        ps1.write_text("stub")
        assert _resolve_piper_binary(str(ps1)) is None
        monkeypatch.setenv("PATHEXT", ".COM;.EXE;.PS1")
        assert _resolve_piper_binary(str(ps1)) == str(ps1)
        # An extensionless explicit path stands: PATHEXT governs how a bare NAME
        # resolves against PATH, not what CreateProcess launches from a full
        # path, and a PE image carries no required suffix.
        bare = tmp_path / "my-piper"
        bare.write_text("stub")
        assert _resolve_piper_binary(str(bare)) == str(bare)

    def test_configured_path_not_executable_returns_none(self, tmp_path) -> None:
        p = tmp_path / "not-exec"
        p.write_bytes(b"")  # exists but not chmod +x
        assert _resolve_piper_binary(str(p)) is None

    def test_configured_expanduser(self, tmp_path, monkeypatch) -> None:
        bin_path = tmp_path / "piper-home"
        _make_executable(str(bin_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        assert _resolve_piper_binary("~/piper-home") == str(bin_path)

    def test_falls_back_to_path(self, tmp_path) -> None:
        with (
            patch(
                "kiro_crew.voice_reply.shutil.which",
                return_value="/usr/local/bin/piper",
            ),
            patch("os.path.isfile", return_value=False),
        ):
            assert _resolve_piper_binary("") == "/usr/local/bin/piper"

    def test_falls_back_to_venv(self, tmp_path, monkeypatch) -> None:
        venv_bin = tmp_path / "piper-venv" / "bin"
        venv_bin.mkdir(parents=True)
        bin_path = venv_bin / "piper"
        _make_executable(str(bin_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        with patch("kiro_crew.voice_reply.shutil.which", return_value=None):
            assert _resolve_piper_binary("") == str(bin_path)

    def test_nothing_found_returns_none(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))  # no venv exists
        with patch("kiro_crew.voice_reply.shutil.which", return_value=None):
            assert _resolve_piper_binary("") is None


class TestResolveConfiguredProviderForkContracts:
    @pytest.mark.parametrize("malformed", [{}, [], 0, 3, None, "", "   "])
    def test_a_malformed_piper_model_does_not_claim_a_piper_install(self, malformed):
        """The migration gate must read a real path, not any truthy stringification.

        It answers one question -- did an operator configure Piper? -- and keeping
        Piper for an upgrader is only correct when the answer is yes. ``str({})``
        is ``"{}"``, which is truthy, so a malformed value would answer yes and
        hand that operator a provider with no model file: silence, which is the
        precise failure the new default exists to remove. A config with no usable
        model resolves to the default instead.
        """
        assert resolve_configured_provider({"piper_model": malformed}) == DEFAULT_PROVIDER

    def test_a_real_piper_model_still_keeps_piper(self):
        """The migration guarantee itself -- tightening the gate must not drop it."""
        assert resolve_configured_provider({"piper_model": "/models/en_US.onnx"}) == PROVIDER_PIPER

    def test_a_configured_piper_model_keeps_piper_when_unnamed(self) -> None:
        # The upgrade case: a working Piper install from before the built-in
        # engine became the default. Resolving it to the default would silently
        # downgrade the voice with nothing to alert the operator.
        assert resolve_configured_provider({"piper_model": "~/voices/en.onnx"}) == PROVIDER_PIPER
        assert resolve_configured_provider({"provider": "", "piper_model": "~/v.onnx"}) == (
            PROVIDER_PIPER
        )

    def test_a_blank_piper_model_does_not_win(self) -> None:
        assert resolve_configured_provider({"piper_model": "   "}) == DEFAULT_PROVIDER

    def test_an_explicit_provider_outranks_a_configured_model(self) -> None:
        assert (
            resolve_configured_provider(
                {"provider": PROVIDER_SYSTEM, "piper_model": "~/voices/en.onnx"}
            )
            == PROVIDER_SYSTEM
        )


class TestSynthesizePiperForkContracts:
    @pytest.mark.asyncio
    async def test_binary_not_found_warns_and_returns_none(self, caplog) -> None:
        with (
            patch("kiro_crew.voice_reply._resolve_piper_binary", return_value=None),
            caplog.at_level("DEBUG", logger="kiro_crew.voice_reply"),
        ):
            assert await _synthesize_piper("hi") is None

        records = [
            record for record in caplog.records if "piper binary not found" in record.message
        ]
        assert len(records) == 1
        assert records[0].levelname == "WARNING"

    @pytest.mark.asyncio
    async def test_model_missing_returns_none(self, tmp_path) -> None:
        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        with patch(
            "kiro_crew.voice_reply._resolve_piper_binary",
            return_value=str(bin_path),
        ):
            # Empty model
            assert await _synthesize_piper("hi", piper_model="") is None
            # Nonexistent file
            assert (
                await _synthesize_piper(
                    "hi",
                    piper_model=str(tmp_path / "missing.onnx"),
                )
                is None
            )

    @pytest.mark.asyncio
    async def test_success_returns_wav_path(self, tmp_path) -> None:
        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        model = tmp_path / "voice.onnx"
        model.write_bytes(b"m")

        proc = _mock_subprocess(returncode=0)

        async def fake_wrap(cmd, *_a, **_kw):
            return cmd, {}, None  # no sandbox, no cleanup

        # The synthesized file needs size >= 100 to be considered valid.
        async def fake_exec(*cmd, **kwargs):
            # The output file is the arg after "-f"
            out_idx = cmd.index("-f") + 1
            out_path = cmd[out_idx]
            with open(out_path, "wb") as f:
                f.write(b"RIFF" + b"x" * 200)
            return proc

        with (
            patch(
                "kiro_crew.voice_reply._resolve_piper_binary",
                return_value=str(bin_path),
            ),
            patch("kiro_crew.voice_reply.sandboxed_spawn_argv_async", side_effect=fake_wrap),
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=fake_exec,
            ),
        ):
            result = await _synthesize_piper(
                "hello",
                piper_model=str(model),
            )
        assert result is not None
        assert result.endswith(".wav")
        assert os.path.isfile(result)
        os.unlink(result)

    @pytest.mark.asyncio
    async def test_success_with_config_and_length_scale(self, tmp_path) -> None:
        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        model = tmp_path / "voice.onnx"
        model.write_bytes(b"m")
        cfg = tmp_path / "voice.onnx.json"
        cfg.write_text("{}")

        proc = _mock_subprocess(returncode=0)
        captured_cmd: list[str] = []

        async def fake_wrap(cmd, *_a, **_kw):
            captured_cmd.extend(cmd)
            return cmd, {}, None

        async def fake_exec(*cmd, **kwargs):
            out_path = cmd[cmd.index("-f") + 1]
            with open(out_path, "wb") as f:
                f.write(b"x" * 200)
            return proc

        with (
            patch(
                "kiro_crew.voice_reply._resolve_piper_binary",
                return_value=str(bin_path),
            ),
            patch("kiro_crew.voice_reply.sandboxed_spawn_argv_async", side_effect=fake_wrap),
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=fake_exec,
            ),
        ):
            result = await _synthesize_piper(
                "hello",
                piper_model=str(model),
                piper_model_config=str(cfg),
                length_scale=0.9,
            )
        assert result is not None
        os.unlink(result)
        # Config + length-scale should be present in cmd.
        assert "-c" in captured_cmd
        assert str(cfg) in captured_cmd
        assert "--length-scale" in captured_cmd
        assert "0.9" in captured_cmd

    @pytest.mark.asyncio
    async def test_nonzero_returncode_unlinks_and_returns_none(self, tmp_path) -> None:
        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        model = tmp_path / "voice.onnx"
        model.write_bytes(b"m")

        proc = _mock_subprocess(returncode=1, stderr=b"bad voice")

        with (
            patch(
                "kiro_crew.voice_reply._resolve_piper_binary",
                return_value=str(bin_path),
            ),
            patch(
                "kiro_crew.voice_reply.sandboxed_spawn_argv_async",
                side_effect=_prepared(),
            ),
            patch(
                "asyncio.create_subprocess_exec",
                return_value=proc,
            ),
        ):
            assert await _synthesize_piper("hello", piper_model=str(model)) is None

    @pytest.mark.asyncio
    async def test_output_too_small_unlinks_and_returns_none(self, tmp_path) -> None:
        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        model = tmp_path / "voice.onnx"
        model.write_bytes(b"m")

        proc = _mock_subprocess(returncode=0)

        async def fake_exec(*cmd, **kwargs):
            out_path = cmd[cmd.index("-f") + 1]
            with open(out_path, "wb") as f:
                f.write(b"tiny")  # < 100 bytes
            return proc

        with (
            patch(
                "kiro_crew.voice_reply._resolve_piper_binary",
                return_value=str(bin_path),
            ),
            patch(
                "kiro_crew.voice_reply.sandboxed_spawn_argv_async",
                side_effect=_prepared(),
            ),
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=fake_exec,
            ),
        ):
            assert await _synthesize_piper("hello", piper_model=str(model)) is None


class TestStreamPocketSpeechForkContracts:
    @pytest.mark.asyncio
    async def test_uses_stream_shim_and_yields_ogg_bytes(self, tmp_path) -> None:
        model = tmp_path / "voice.onnx"
        model.write_bytes(b"m")
        stdin = MagicMock()
        stdin.drain = AsyncMock()
        stdout = MagicMock()
        stdout.read = AsyncMock(side_effect=[b"OggSfirst", b"second", b""])
        stderr = MagicMock()
        stderr.read = AsyncMock(return_value=b"")
        proc = MagicMock(stdin=stdin, stdout=stdout, stderr=stderr, returncode=0)
        proc.wait = AsyncMock(return_value=0)
        captured: dict[str, object] = {}
        loop_thread = threading.get_ident()

        def resolve_binary(configured):
            assert threading.get_ident() != loop_thread
            assert configured == "/bin/piper"
            captured["binary_resolved"] = True
            return configured

        def prepare_sandbox(cmd, mode):
            assert threading.get_ident() != loop_thread
            assert mode == "standard"
            captured["sandbox_prepared"] = True
            return cmd, None

        async def fake_create(*cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs["env"]
            return proc

        with (
            patch("kiro_crew.voice_reply._resolve_piper_binary", side_effect=resolve_binary),
            patch("kiro_crew.voice_reply.wrap_argv", side_effect=prepare_sandbox),
            patch("kiro_crew.voice_reply.create_subprocess_limited", side_effect=fake_create),
        ):
            chunks = [
                chunk
                async for chunk in stream_pocket_speech(
                    "hello",
                    voice_id="michael",
                    piper_binary="/bin/piper",
                    piper_model=str(model),
                )
            ]

        assert chunks == [b"OggSfirst", b"second"]
        assert captured["binary_resolved"] is True
        assert captured["sandbox_prepared"] is True
        assert "--stream" in captured["cmd"]
        assert captured["env"]["KIROCREW_TTS_ENGINE"] == "pocket"
        assert captured["env"]["KIROCREW_TTS_VOICE"] == "michael"


class TestSynthesizeSpeechDispatcherForkContracts:
    @pytest.mark.asyncio
    async def test_piper_dispatch(self) -> None:
        with (
            patch(
                "kiro_crew.voice_reply._synthesize_polly",
                new=AsyncMock(return_value="/tmp/out.mp3"),
            ) as mock_polly,
            patch(
                "kiro_crew.voice_reply._synthesize_piper",
                new=AsyncMock(return_value="/tmp/out.wav"),
            ) as mock_piper,
        ):
            out = await synthesize_speech("hello world", provider=PROVIDER_PIPER)
        assert out == "/tmp/out.wav"
        mock_piper.assert_awaited_once()
        mock_polly.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_piper_empty_plain_returns_none(self) -> None:
        with patch(
            "kiro_crew.voice_reply._synthesize_piper",
            new=AsyncMock(return_value=None),
        ) as mock_piper:
            out = await synthesize_speech("   ", provider=PROVIDER_PIPER)
        assert out is None
        mock_piper.assert_not_awaited()


class TestSynthesizePiperCancelOwnershipForkContracts:
    @staticmethod
    def _piper_env(tmp_path, monkeypatch):
        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        model = tmp_path / "voice.onnx"
        model.write_bytes(b"m")
        owned = tmp_path / "owned.wav"
        _pin_mkstemp(monkeypatch, owned)
        monkeypatch.setattr(
            "kiro_crew.voice_reply._resolve_piper_binary", lambda cfg: str(bin_path)
        )
        monkeypatch.setattr("kiro_crew.voice_reply.sandboxed_spawn_argv_async", _prepared())
        return model, owned

    @pytest.mark.asyncio
    async def test_cancellation_reaps_piper_before_removing_the_wav(self, tmp_path, monkeypatch):
        model, owned = self._piper_env(tmp_path, monkeypatch)
        events: list[str] = []
        _track_unlink(monkeypatch, owned, events)

        with patch("asyncio.create_subprocess_exec", return_value=_CancelOnceProc(events)):
            with pytest.raises(asyncio.CancelledError):
                await _synthesize_piper("hello", piper_model=str(model))
        assert events == ["killed", "reaped", "unlinked"]
        assert not owned.exists()

    @pytest.mark.asyncio
    async def test_locked_wav_does_not_replace_the_cancellation(self, tmp_path, monkeypatch):
        """Worst case on Windows: the child still holds the ``.wav`` so the
        unlink raises ``PermissionError``. That must not REPLACE the in-flight
        cancellation — the ``OSError`` guard swallows it and the original
        propagates."""
        model, owned = self._piper_env(tmp_path, monkeypatch)
        events: list[str] = []
        _lock_unlink(monkeypatch, owned, events)

        with patch("asyncio.create_subprocess_exec", return_value=_CancelOnceProc(events)):
            with pytest.raises(asyncio.CancelledError):
                await _synthesize_piper("hello", piper_model=str(model))
        assert events == ["killed", "reaped", "unlink_attempted"]
        # The locked unlink never removed the file — the guarantee under test
        # is exception identity, not removal.
        assert owned.exists()

    @pytest.mark.asyncio
    async def test_cancellation_during_spawn_still_removes_the_wav(self, tmp_path, monkeypatch):
        """A cancellation landing on the spawn itself means no child exists —
        the ``.wav`` must still be removed and the cancellation propagate."""
        model, owned = self._piper_env(tmp_path, monkeypatch)

        with patch("asyncio.create_subprocess_exec", side_effect=asyncio.CancelledError()):
            with pytest.raises(asyncio.CancelledError):
                await _synthesize_piper("hello", piper_model=str(model))
        assert not owned.exists()

    @pytest.mark.asyncio
    async def test_cancellation_still_unlinks_the_sandbox_cleanup_path(self, tmp_path, monkeypatch):
        """The outer ``finally`` owns the sandbox cleanup path; a cancelled
        synthesis must not leak the launcher script either."""
        model, owned = self._piper_env(tmp_path, monkeypatch)
        launcher = tmp_path / "launcher.sh"
        launcher.write_text("#!/bin/sh\n")
        monkeypatch.setattr(
            "kiro_crew.voice_reply.sandboxed_spawn_argv_async",
            _prepared(cleanup=str(launcher)),
        )
        events: list[str] = []

        with patch("asyncio.create_subprocess_exec", return_value=_CancelOnceProc(events)):
            with pytest.raises(asyncio.CancelledError):
                await _synthesize_piper("hello", piper_model=str(model))
        assert not owned.exists()
        assert not launcher.exists()


class TestSynthesizeSpeechDoesNotSwallowARefusalForkContracts:
    @pytest.mark.asyncio
    async def test_piper_refusal_travels_through_synthesize_speech(self, tmp_path, monkeypatch):
        from kiro_crew.sandbox import SandboxUnavailableError
        from kiro_crew.voice_reply import synthesize_speech

        prose = "SEAM-SENTINEL: the sandbox refused"
        bin_path = tmp_path / "piper"
        _make_executable(str(bin_path))
        model = tmp_path / "voice.onnx"
        model.write_bytes(b"m")

        # Patch the seam the spawn actually goes through. Reaching the real
        # sandbox layer here would make the test pass for the wrong reason on a
        # host that genuinely refuses, and vacuously on one that does not.
        async def refuse(cmd, **kw):
            raise SandboxUnavailableError(prose, "no_backend", "not Linux")

        monkeypatch.setattr("kiro_crew.voice_reply.sandboxed_spawn_argv_async", refuse)
        monkeypatch.setattr(
            "kiro_crew.voice_reply._resolve_piper_binary", lambda *a, **k: str(bin_path)
        )

        with pytest.raises(SandboxUnavailableError) as caught:
            await synthesize_speech("hello", provider="piper", piper_model=str(model))
        assert prose in str(caught.value)


def _capture_mkstemp(monkeypatch) -> list[str]:
    """Record every path the module allocates via ``tempfile.mkstemp``."""
    allocated: list[str] = []
    real_mkstemp = tempfile.mkstemp

    def recording_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        allocated.append(path)
        return fd, path

    monkeypatch.setattr("kiro_crew.voice_reply.tempfile.mkstemp", recording_mkstemp)
    return allocated


def _make_executable(path: str) -> None:
    """Touch *path* and flag it executable so shutil.which / os.access pass."""
    with open(path, "wb") as f:
        f.write(b"#!/bin/sh\n")
    os.chmod(path, 0o755)


def _patch_aws_on_path(monkeypatch) -> None:
    """Make ``shutil.which`` report the ``aws`` CLI present, others absent."""
    monkeypatch.setattr(
        "kiro_crew.voice_reply.shutil.which",
        lambda name, *a, **k: _FAKE_AWS_CLI if name == "aws" else None,
    )
    # The which stub above is name-sensitive ("aws" only), but the shared
    # deploy-engine resolver would feed it a PATH-hit absolute path.
    # Pin the resolver to the bare name so this fixture keeps meaning exactly
    # "the aws CLI is present" regardless of the host.
    monkeypatch.setattr("kiro_crew.voice_reply.resolve_aws_bin", lambda: "aws")


def _writing_exec(flag: str, *, size: int = 200):
    """A ``create_subprocess_exec`` stub that writes a WAV at the *flag* argument."""
    captured: dict[str, object] = {}

    async def fake_exec(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["kwargs"] = dict(kwargs)
        out_path = cmd[cmd.index(flag) + 1]
        with open(out_path, "wb") as fh:
            fh.write(b"RIFF" + b"x" * size)
        return _mock_subprocess(returncode=0)

    return fake_exec, captured


def _passthrough_wrap():
    """Patch the sandbox wrap to return its argv unchanged.

    The built-in engine is confined on macOS and Linux, so on those hosts the
    real wrap prepends the launcher and ``cmd[0]`` is not the engine.
    These tests assert how the ENGINE's own argv is built, which is what sits
    inside the wrap either way, so they pin that rather than the host's backend.
    """

    async def identity(cmd, *_a, **_kw):
        return list(cmd), {}, None

    return patch("kiro_crew.voice_reply.sandboxed_spawn_argv_async", side_effect=identity)


def _prepared(cleanup=None, env=None):
    """An async ``sandboxed_spawn_argv_async`` double returning its argv intact.

    The real call returns (argv, scrubbed_env, cleanup_path); these tests assert
    the engine's own argv and the env/cleanup handling around it, so the double
    passes argv through and lets each caller pin the other two.
    """

    async def prepare(cmd, *_a, **_kw):
        return list(cmd), {} if env is None else env, cleanup

    return prepare


def _matching_identity(consent_mod, account: str):
    """An async ``probe_identity`` stand-in that resolves to ``account``."""

    async def _probe(_profile: str, _region: str, *, use_cache: bool = True):
        return consent_mod.Identity(ok=True, account=account)

    return _probe


@pytest.fixture()
def _polly_consented(tmp_path_factory, monkeypatch):
    """Record operator consent for Polly under the default profile+region.

    ``_synthesize_polly`` now refuses without one, so every test that means to
    exercise the SYNTHESIS path has to consent first. The grant is written into
    a throwaway data home, never the real one. Tests that assert the refusal
    itself deliberately do not use this fixture (see ``test_aws_consent.py``).
    """
    home = tmp_path_factory.mktemp("consent-home")
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    from kiro_crew import aws_consent
    from kiro_crew.config.loader import config_dir

    config_dir().mkdir(parents=True, exist_ok=True)
    aws_consent.record_grant(
        aws_consent.SERVICE_POLLY,
        profile="",
        region="",
        account="111122223333",
        arn="arn:aws:iam::111122223333:user/test",
        granted_at="2026-08-21T00:00:00+00:00",
    )
    # The gate also verifies the LIVE account, which would spawn the AWS CLI.
    # These cases are about synthesis, so return a matching identity instead.
    monkeypatch.setattr(
        aws_consent, "probe_identity", _matching_identity(aws_consent, "111122223333")
    )


class _CancelOnceProc:
    """Process double: first ``communicate`` raises ``CancelledError``, the
    second (the reap) records itself and returns."""

    def __init__(self, events: list[str]):
        self._events = events
        self._calls = 0

    async def communicate(self, _input: bytes | None = None):
        self._calls += 1
        if self._calls == 1:
            raise asyncio.CancelledError()
        self._events.append("reaped")
        return b"", b""

    def kill(self):
        self._events.append("killed")


class _CancelAlwaysProc:
    """Process double: EVERY ``communicate`` raises ``CancelledError`` — the
    second raise is the repeat cancellation landing on the reap await."""

    def __init__(self, events: list[str]):
        self._events = events

    async def communicate(self, _input: bytes | None = None):
        raise asyncio.CancelledError()

    def kill(self):
        self._events.append("killed")


def _pin_mkstemp(monkeypatch, owned) -> None:
    """Pin ``tempfile.mkstemp`` to a known file so the tests can watch it."""

    def fake_mkstemp(suffix: str = ""):
        return os.open(str(owned), os.O_WRONLY | os.O_CREAT), str(owned)

    monkeypatch.setattr("kiro_crew.voice_reply.tempfile.mkstemp", fake_mkstemp)


def _track_unlink(monkeypatch, owned, events: list[str]) -> None:
    real_unlink = os.unlink

    def tracked(path, *args, **kwargs):
        if str(path) == str(owned):
            events.append("unlinked")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr("kiro_crew.voice_reply.os.unlink", tracked)


def _lock_unlink(monkeypatch, owned, events: list[str]) -> None:
    """Make the unlink of *owned* fail as if the child still held it open.

    Every OTHER call reaches the real ``os.unlink`` with its full signature.
    ``kiro_crew.voice_reply.os`` IS the stdlib module, so this patch is
    process-global, and it outlives the ``tmp_path`` teardown: the module
    autouse fixture requests ``monkeypatch`` before ``tmp_path`` exists, so
    ``monkeypatch`` is undone last. Under ``tmp_path_retention_policy =
    failed`` that teardown is a ``shutil.rmtree`` whose fd-walk calls
    ``os.unlink(leaf, dir_fd=fd)``. A wrapper that forwarded ``os.remove(path)``
    dropped ``dir_fd``, re-resolved ``piper`` / ``voice.onnx`` / ``owned.wav``
    against the CWD -- the checkout -- and left the tmp dir behind.
    """
    real_unlink = os.unlink

    def locked(path, *args, **kwargs):
        if str(path) == str(owned):
            events.append("unlink_attempted")
            raise PermissionError("file is locked by the child")
        # A bare relative name here would be resolved against the CWD, i.e.
        # the checkout; only fd-relative (rmtree) or absolute paths may pass.
        assert (
            os.path.isabs(os.fspath(path)) or "dir_fd" in kwargs
        ), f"relative unlink {path!r} without dir_fd would touch the CWD"
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr("kiro_crew.voice_reply.os.unlink", locked)
