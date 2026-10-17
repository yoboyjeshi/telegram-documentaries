"""Strict raw PCM / WAV validation at the Narrator's audio boundary.

Only the two formats this Gemini TTS endpoint actually produces are accepted.
Everything else fails closed: this is not a general-purpose audio converter.
"""
import struct

import pytest
from pydantic import ValidationError

from app.audio_format import (
    MAX_AUDIO_BYTES,
    MAX_DURATION_SECONDS,
    PCM_CHANNELS,
    PCM_SAMPLE_RATE,
    PCM_SAMPLE_WIDTH,
    InvalidAudioError,
    NarrationAudio,
    ensure_valid_audio,
    normalize_audio,
)

PCM_MIME = 'audio/L16;codec=pcm;rate=24000'
BYTES_PER_SECOND = PCM_SAMPLE_RATE * PCM_CHANNELS * PCM_SAMPLE_WIDTH


def pcm(seconds=0.25):
    return b'\x01\x02' * int(PCM_SAMPLE_RATE * seconds)


def wav(frames, *, channels=1, rate=24000, width=2, fmt=1,
        riff_size=None, data_size=None, trailing=b'', fmt_chunk=True):
    block = channels * width
    body = b'WAVE'
    if fmt_chunk:
        body += b'fmt ' + struct.pack(
            '<IHHIIHH', 16, fmt, channels, rate, rate * block, block, width * 8)
    declared = len(frames) if data_size is None else data_size
    body += b'data' + struct.pack('<I', declared) + frames + trailing
    size = len(body) if riff_size is None else riff_size
    return b'RIFF' + struct.pack('<I', size) + body


def audio(data, mime=PCM_MIME):
    return NarrationAudio.model_construct(data=data, mime_type=mime)


# --- supported raw PCM ---

def test_pcm_is_wrapped_in_one_canonical_wav():
    frames = pcm()
    out = normalize_audio(NarrationAudio(data=frames, mime_type=PCM_MIME))
    assert out == wav(frames)
    assert out.count(b'RIFF') == 1 and out[8:12] == b'WAVE'
    assert out.endswith(frames)


@pytest.mark.parametrize('mime', [
    'audio/L16;codec=pcm;rate=24000',
    'AUDIO/L16;CODEC=PCM;RATE=24000',
    'audio/l16; codec=pcm; rate=24000',
    'audio/L16;rate=24000',
    'audio/L16;rate=24000;channels=1',
    'audio/L16;codec=pcm;rate=24000;channels=1',
])
def test_pcm_mime_parameters_are_parsed_case_insensitively(mime):
    frames = pcm()
    assert normalize_audio(NarrationAudio(data=frames, mime_type=mime)) == wav(frames)


@pytest.mark.parametrize('mime', [
    'audio/L16',
    'audio/L16;codec=opus;rate=24000',
    'audio/L16;rate=16000',
    'audio/L16;rate=48000',
    'audio/L16;rate=24000;channels=2',
    'audio/L16;rate=24000;rate=16000',
    'audio/L16;rate=notanumber',
    'audio/L16;rate=24000;extra=1',
])
def test_contradictory_or_missing_pcm_parameters_are_rejected(mime):
    with pytest.raises(InvalidAudioError):
        normalize_audio(audio(pcm(), mime))


@pytest.mark.parametrize('mime', [
    'audio/L16;codec=pcm;rate=24000"',
    'audio/L16;codec=pcm;rate="24000',
    'audio/L16;codec="pcm;rate=24000',
    'audio/L16;codec=pcm";rate=24000',
    'audio/L16;rate=2"4000"',
])
def test_unmatched_quotes_in_mime_parameters_are_rejected(mime):
    with pytest.raises(InvalidAudioError):
        normalize_audio(audio(pcm(), mime))


def test_properly_quoted_mime_parameters_are_accepted():
    frames = pcm()
    mime = 'audio/L16;codec="pcm";rate="24000"'
    assert normalize_audio(NarrationAudio(data=frames, mime_type=mime)) == wav(frames)


def test_odd_length_pcm_is_rejected():
    with pytest.raises(InvalidAudioError):
        normalize_audio(audio(pcm() + b'\x00'))


# --- supported WAV ---

@pytest.mark.parametrize('mime', ['audio/wav', 'audio/x-wav', 'AUDIO/WAV', 'audio/wav; charset=x'])
def test_supported_wav_is_returned_without_double_wrapping(mime):
    frames = pcm()
    source = wav(frames)
    out = normalize_audio(audio(source, 'audio/wav' if ';' in mime else mime))
    assert out == source
    assert out.count(b'RIFF') == 1


def test_wav_mime_variants_are_accepted():
    frames = pcm()
    assert normalize_audio(audio(wav(frames), 'audio/x-wav')) == wav(frames)
    assert normalize_audio(audio(wav(frames), 'AUDIO/WAV')) == wav(frames)


# Built lazily and identified by name: a bytes parameter would become a
# multi-megabyte pytest node id.
BAD_WAV = {
    'wrong sample rate': lambda: wav(pcm(), rate=44100),
    'stereo': lambda: wav(pcm(), channels=2),
    '8-bit samples': lambda: wav(pcm(), width=1),
    'ieee float': lambda: wav(pcm(), fmt=3),
    'no fmt chunk': lambda: wav(pcm(), fmt_chunk=False),
    'truncated frames': lambda: wav(pcm())[:-200],
    'over-declared data size': lambda: wav(pcm(), data_size=10 ** 7),
    'riff size mismatch': lambda: wav(pcm(), riff_size=99),
    'trailing junk': lambda: wav(pcm(), trailing=b'JUNKJUNK'),
    'no frames': lambda: wav(b''),
    'headerless pcm': pcm,
    'riff magic only': lambda: b'RIFF',
    'not a wave form': lambda: b'RIFF' + struct.pack('<I', 4) + b'AVIX',
}


@pytest.mark.parametrize('name', sorted(BAD_WAV))
def test_malformed_or_unsupported_wav_is_rejected(name):
    with pytest.raises(InvalidAudioError):
        normalize_audio(audio(BAD_WAV[name](), 'audio/wav'))


def test_wav_data_chunk_must_be_frame_aligned():
    frames = pcm() + b'\x00'
    with pytest.raises(InvalidAudioError):
        normalize_audio(audio(wav(frames), 'audio/wav'))


# --- rejected MIME and payloads ---

@pytest.mark.parametrize('mime', [
    'audio/mpeg', 'audio/ogg', 'audio/ogg;codecs=opus', 'audio/L24;rate=24000',
    'audio/pcm;rate=24000', 'application/octet-stream', 'audio/', 'audio',
])
def test_unsupported_mime_is_rejected(mime):
    with pytest.raises((InvalidAudioError, ValidationError)):
        normalize_audio(audio(pcm(), mime))


@pytest.mark.parametrize('data', [b'', None, 'pcm as text', bytearray(b'\x01\x02'), 123],
                         ids=['empty', 'none', 'str', 'bytearray', 'int'])
def test_empty_or_non_byte_payloads_are_rejected(data):
    with pytest.raises(InvalidAudioError):
        normalize_audio(audio(data))


@pytest.mark.parametrize('mime', ['', '   ', None, 7])
def test_missing_mime_is_rejected(mime):
    with pytest.raises(InvalidAudioError):
        normalize_audio(audio(pcm(), mime))


# --- size and duration limits ---

def test_oversized_input_is_rejected_before_wrapping():
    with pytest.raises(InvalidAudioError):
        normalize_audio(audio(b'\x00' * (MAX_AUDIO_BYTES + 2)))


def test_input_within_limit_whose_wav_would_exceed_it_is_rejected():
    with pytest.raises(InvalidAudioError):
        normalize_audio(audio(b'\x00' * (MAX_AUDIO_BYTES - 2)))


def test_duration_at_the_limit_is_accepted():
    frames = b'\x00' * (BYTES_PER_SECOND * MAX_DURATION_SECONDS)
    assert len(normalize_audio(audio(frames))) == len(frames) + 44


def test_excessive_duration_is_rejected():
    frames = b'\x00' * (BYTES_PER_SECOND * (MAX_DURATION_SECONDS + 1))
    with pytest.raises(InvalidAudioError):
        normalize_audio(audio(frames))


def test_zero_duration_wav_is_rejected():
    with pytest.raises(InvalidAudioError):
        normalize_audio(audio(wav(b''), 'audio/wav'))


def test_limits_match_the_approved_spec():
    assert MAX_AUDIO_BYTES == 10 * 1024 * 1024
    assert MAX_DURATION_SECONDS == 120


# --- the Pydantic boundary ---

def test_narration_audio_is_strict_and_closed():
    with pytest.raises(ValidationError):
        NarrationAudio(data='text', mime_type=PCM_MIME)
    with pytest.raises(ValidationError):
        NarrationAudio(data=pcm(), mime_type=b'audio/wav')
    with pytest.raises(ValidationError):
        NarrationAudio(data=pcm(), mime_type=PCM_MIME, rate=24000)
    with pytest.raises(ValidationError):
        NarrationAudio(data=pcm(), mime_type='video/mp4')


def test_audio_bytes_never_appear_in_repr_or_str():
    secret = b'SENTINEL_AUDIO_BYTES' * 16
    model = NarrationAudio(data=secret, mime_type=PCM_MIME)
    for text in (repr(model), str(model), f'{model}'):
        assert 'SENTINEL_AUDIO_BYTES' not in text
    assert PCM_MIME in repr(model)
    assert model.data == secret


def test_injected_and_model_construct_results_are_revalidated():
    frames = pcm()
    assert ensure_valid_audio({'data': frames, 'mime_type': PCM_MIME}).data == frames
    assert ensure_valid_audio(NarrationAudio(data=frames, mime_type=PCM_MIME)).data == frames
    with pytest.raises(InvalidAudioError):
        ensure_valid_audio(NarrationAudio.model_construct(data='nope', mime_type=PCM_MIME))
    with pytest.raises(InvalidAudioError):
        ensure_valid_audio(b'raw bytes are not a schema')
    with pytest.raises(InvalidAudioError):
        ensure_valid_audio(None)


def test_rejection_messages_never_echo_the_payload():
    secret = b'SENTINEL_AUDIO_BYTES' * 16
    with pytest.raises(InvalidAudioError) as excinfo:
        normalize_audio(audio(secret, 'audio/ogg'))
    assert 'SENTINEL_AUDIO_BYTES' not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
