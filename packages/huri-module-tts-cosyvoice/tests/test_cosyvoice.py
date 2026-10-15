"""What can be checked of CosyVoice without its sources and model."""

from huri_tts_cosyvoice.handle import CosyVoiceTTSHandle, _normalize_transcript
from huri_tts_cosyvoice.module import CosyVoiceTTS


def test_a_bare_transcript_is_placed_after_the_prompt_marker():
    # Before the marker the LM reads it as an instruction and may speak it.
    bare = "Instinct creates its own oppressors."
    assert _normalize_transcript(bare) == (
        f"You are a helpful assistant.<|endofprompt|>{bare}"
    )


def test_an_explicit_instruction_is_kept():
    raw = "Speak softly.<|endofprompt|>Hello."
    assert _normalize_transcript(raw) == raw


def test_the_module_binds_its_own_deployment():
    assert CosyVoiceTTS._handle_cls is CosyVoiceTTSHandle
    # Not "TTSHandle": both engines can then run in one Serve app.
    assert CosyVoiceTTSHandle.name == "CosyVoiceTTSHandle"
    assert (CosyVoiceTTS.input_type, CosyVoiceTTS.output_type) == (
        "huri.token",
        "huri.audio.out",
    )
