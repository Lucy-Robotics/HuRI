# huri-tts

The engine-agnostic half of HuRI's text-to-speech modules: `StreamingTTS`
opens one synthesis session per utterance, pushes each `huri.token` into it in
order and streams `huri.audio.out` back with presentation timestamps. An engine
package subclasses it and sets `_handle_cls` to a Serve deployment exposing
`get_sample_rate()`, `start_session(sid)`, `push_text(sid, text, end)` and
`stream_audio(sid)`.

A library only: it registers no module. Used by `huri-module-tts-piper` and
`huri-module-tts-cosyvoice`; laid out to move as-is to its own repository.
