"""Validate the actual audio bytes the TTS endpoint returned, not its claims.

Gemini 3.1 TTS returns headerless signed 16-bit little-endian PCM at 24 kHz
mono. Only that format and an already-correct PCM WAV of the same shape are
accepted; ``normalize_audio`` turns either into one validated WAV in memory,
which the encoder stage then turns into OGG/Opus. Anything else fails closed:
this module is a boundary check, not a general-purpose audio converter.

Byte-level validation proves container and frame-alignment consistency. Raw
PCM has no container signature, so it can never prove that the samples
contain speech, and nothing here claims to verify the spoken words.

Audio bytes are excluded from ``repr``. Every rejection this module reports
raises :class:`InvalidAudioError`, whose message is fixed and whose cause is
suppressed, so a caller that logs that failure cannot leak the payload. A
``ValidationError`` raised by constructing :class:`NarrationAudio` directly is
not that boundary and does carry the rejected value: route untrusted
candidates through :func:`ensure_valid_audio` or :func:`normalize_audio`.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_DURATION_SECONDS = 120

# The documented shape of this endpoint's audio: 24 kHz, mono, signed 16-bit LE.
PCM_SAMPLE_RATE = 24000
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH = 2
PCM_BLOCK_ALIGN = PCM_CHANNELS * PCM_SAMPLE_WIDTH
PCM_BYTES_PER_SECOND = PCM_SAMPLE_RATE * PCM_BLOCK_ALIGN

PCM_MIME_TYPE = "audio/l16"
WAV_MIME_TYPES = frozenset({"audio/wav", "audio/x-wav"})
# WAVE_FORMAT_PCM: uncompressed integer samples. Float/compressed tags are rejected.
_WAVE_FORMAT_PCM = 1
_WAV_HEADER_BYTES = 44


class InvalidAudioError(ValueError):
    """Raised when audio fails the format contract.

    The message is fixed and the cause is suppressed, so the rejected bytes
    can never reach a log through this exception.
    """


def _unquote(value: str) -> str:
    """Return a MIME parameter value, rejecting unbalanced quoting.

    RFC 2045 allows a quoted string, but a stray quote makes the value
    ambiguous (``rate=24000"`` is not the number 24000), so it is rejected
    rather than silently stripped.
    """
    if value.startswith('"'):
        if len(value) < 2 or not value.endswith('"'):
            raise InvalidAudioError("Audio MIME type has an unterminated quoted parameter")
        value = value[1:-1]
    if '"' in value:
        raise InvalidAudioError("Audio MIME type has a malformed quoted parameter")
    return value


def _parse_mime(mime_type: str) -> tuple[str, dict[str, str]]:
    """Split a MIME type into a lowercase type and lowercase parameter names.

    A parameter repeated with two different values is contradictory, not a
    duplicate to be collapsed, so it is rejected here.
    """
    parts = [segment.strip() for segment in mime_type.split(";")]
    media_type = parts[0].lower()
    params: dict[str, str] = {}
    for segment in parts[1:]:
        if not segment:
            raise InvalidAudioError("Audio MIME type has an empty parameter")
        name, separator, value = segment.partition("=")
        if not separator:
            raise InvalidAudioError("Audio MIME type has a malformed parameter")
        key = name.strip().lower()
        stripped = _unquote(value.strip())
        if key in params and params[key] != stripped.lower():
            raise InvalidAudioError("Audio MIME type has contradictory parameters")
        params[key] = stripped.lower()
    return media_type, params


class NarrationAudio(BaseModel):
    """One rendered narration: raw provider bytes plus their declared MIME.

    ``strict`` rejects wrong types (a ``str`` or ``bytearray`` payload is a
    type error, not something to coerce), ``extra="forbid"`` rejects a
    malformed schema, and ``revalidate_instances="always"`` means an instance
    built by ``model_construct`` is revalidated whenever it is passed back
    through ``model_validate``. ``data`` is excluded from ``repr``.
    """

    model_config = ConfigDict(strict=True, extra="forbid", revalidate_instances="always")

    data: bytes = Field(repr=False)
    mime_type: str

    @field_validator("data")
    @classmethod
    def _check_data(cls, value: bytes) -> bytes:
        if not value:
            raise InvalidAudioError("Narration audio is empty")
        if len(value) > MAX_AUDIO_BYTES:
            raise InvalidAudioError("Narration audio is larger than the byte limit")
        return value

    @field_validator("mime_type")
    @classmethod
    def _check_mime_type(cls, value: str) -> str:
        mime_type = value.strip()
        if not mime_type:
            raise InvalidAudioError("Narration audio has no declared MIME type")
        media_type, _ = _parse_mime(mime_type)
        if not media_type.startswith("audio/") or media_type == "audio/":
            raise InvalidAudioError("Narration audio does not declare an audio MIME type")
        return mime_type


def ensure_valid_audio(candidate: object) -> NarrationAudio:
    """Revalidate any narration-audio candidate, whatever produced it.

    Accepts a :class:`NarrationAudio` (including ``model_construct`` results,
    which skipped validation) or a mapping. Anything else is a wrong type.
    """
    payload: object
    if isinstance(candidate, NarrationAudio):
        payload = candidate
    elif isinstance(candidate, Mapping):
        payload = dict(candidate)
    else:
        raise InvalidAudioError("Narration audio candidate is not an audio schema")
    try:
        return NarrationAudio.model_validate(payload)
    except ValidationError:
        # from None: the ValidationError repr carries the rejected payload.
        raise InvalidAudioError("Narration audio failed validation") from None


def _check_duration(frame_bytes: int) -> None:
    """Reject a zero-length or implausibly long rendering."""
    if frame_bytes <= 0:
        raise InvalidAudioError("Narration audio has no audio frames")
    if frame_bytes % PCM_BLOCK_ALIGN:
        raise InvalidAudioError("Narration audio frames are not sample-aligned")
    if frame_bytes / PCM_BYTES_PER_SECOND > MAX_DURATION_SECONDS:
        raise InvalidAudioError("Narration audio is longer than the duration limit")


def _check_pcm_parameters(params: dict[str, str]) -> None:
    """Validate the declared PCM parameters against this endpoint's contract.

    ``channels`` may be absent: the documented endpoint is mono, so the
    contract supplies the value rather than guessing. ``rate`` may not be
    absent, because an unknown sample rate would make the duration unknowable.
    """
    unknown = set(params) - {"codec", "rate", "channels"}
    if unknown:
        raise InvalidAudioError("Narration audio declares unknown MIME parameters")
    codec = params.get("codec")
    if codec is not None and codec != "pcm":
        raise InvalidAudioError("Narration audio declares an unsupported codec")
    rate = params.get("rate")
    if rate is None:
        raise InvalidAudioError("Narration audio declares no sample rate")
    if not rate.isdigit() or int(rate) != PCM_SAMPLE_RATE:
        raise InvalidAudioError("Narration audio declares an unsupported sample rate")
    channels = params.get("channels")
    if channels is not None and (not channels.isdigit() or int(channels) != PCM_CHANNELS):
        raise InvalidAudioError("Narration audio declares an unsupported channel count")


def _canonical_wav(frames: bytes) -> bytes:
    """Wrap validated PCM frames in one canonical 44-byte-header PCM WAV."""
    header = (
        b"RIFF"
        + struct.pack("<I", 36 + len(frames))
        + b"WAVE"
        + b"fmt "
        + struct.pack(
            "<IHHIIHH",
            16,
            _WAVE_FORMAT_PCM,
            PCM_CHANNELS,
            PCM_SAMPLE_RATE,
            PCM_BYTES_PER_SECOND,
            PCM_BLOCK_ALIGN,
            PCM_SAMPLE_WIDTH * 8,
        )
        + b"data"
        + struct.pack("<I", len(frames))
    )
    if len(header) != _WAV_HEADER_BYTES:
        raise InvalidAudioError("Narration audio header could not be built")
    wav = header + frames
    if len(wav) > MAX_AUDIO_BYTES:
        raise InvalidAudioError("Narration audio is larger than the byte limit")
    return wav


def _validate_wav(data: bytes) -> None:
    """Walk the RIFF chunks and reject anything inconsistent or unsupported.

    Checks declared sizes against the real buffer, so a truncated or
    over-declared container is rejected rather than trusted on magic bytes.
    Exactly one ``fmt `` chunk followed by exactly one ``data`` chunk is
    accepted, with no trailing bytes: an accepted WAV is therefore already
    canonical and is returned unchanged rather than wrapped a second time.
    """
    if len(data) < _WAV_HEADER_BYTES:
        raise InvalidAudioError("Narration audio is too short to be a WAV container")
    if data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise InvalidAudioError("Narration audio is not a RIFF/WAVE container")
    (riff_size,) = struct.unpack("<I", data[4:8])
    if riff_size != len(data) - 8:
        raise InvalidAudioError("Narration audio RIFF size does not match the payload")

    offset = 12
    fmt_seen = False
    data_size: int | None = None
    while offset < len(data):
        if offset + 8 > len(data):
            raise InvalidAudioError("Narration audio has a truncated chunk header")
        chunk_id = data[offset : offset + 4]
        (chunk_size,) = struct.unpack("<I", data[offset + 4 : offset + 8])
        body = offset + 8
        if chunk_size > len(data) - body:
            raise InvalidAudioError("Narration audio has a truncated chunk")
        if chunk_id == b"fmt ":
            if fmt_seen or data_size is not None or chunk_size != 16:
                raise InvalidAudioError("Narration audio has an unsupported format chunk")
            tag, channels, rate, byte_rate, block_align, bits = struct.unpack(
                "<HHIIHH", data[body : body + 16]
            )
            if tag != _WAVE_FORMAT_PCM or bits != PCM_SAMPLE_WIDTH * 8:
                raise InvalidAudioError("Narration audio is not uncompressed 16-bit PCM")
            if channels != PCM_CHANNELS or rate != PCM_SAMPLE_RATE:
                raise InvalidAudioError("Narration audio is not 24 kHz mono")
            if block_align != PCM_BLOCK_ALIGN or byte_rate != PCM_BYTES_PER_SECOND:
                raise InvalidAudioError("Narration audio has inconsistent format fields")
            fmt_seen = True
        elif chunk_id == b"data":
            if not fmt_seen or data_size is not None:
                raise InvalidAudioError("Narration audio has an unsupported chunk order")
            data_size = chunk_size
        else:
            raise InvalidAudioError("Narration audio contains an unsupported chunk")
        offset = body + chunk_size
    if not fmt_seen or data_size is None:
        raise InvalidAudioError("Narration audio is missing a format or data chunk")
    if offset != len(data):
        raise InvalidAudioError("Narration audio has trailing bytes after the data chunk")
    _check_duration(data_size)


def normalize_audio(audio: NarrationAudio) -> bytes:
    """Return one validated PCM WAV for the supplied narration audio.

    Raw ``audio/L16`` PCM is wrapped once in a canonical WAV header; an
    already-correct PCM WAV is returned unchanged, never double-wrapped.
    Every other MIME type, parameter combination, malformed container, size
    or duration raises :class:`InvalidAudioError`.
    """
    validated = ensure_valid_audio(audio)
    data = validated.data
    media_type, params = _parse_mime(validated.mime_type)

    if media_type == PCM_MIME_TYPE:
        _check_pcm_parameters(params)
        _check_duration(len(data))
        return _canonical_wav(data)

    if media_type in WAV_MIME_TYPES:
        if set(params):
            raise InvalidAudioError("Narration audio declares unknown MIME parameters")
        _validate_wav(data)
        return data

    raise InvalidAudioError("Narration audio declares an unsupported MIME type")
