"""Codec-boundary tests for app.audio_encoding.

Two kinds of test live here on purpose:

* Process fakes cover every failure the real encoder cannot be made to produce
  on demand -- nonzero exits, output caps, timeouts, cancellation, a process
  that ignores terminate, and malformed container bytes.
* One real round-trip test runs the FFmpeg found on ``PATH``. It never skips:
  if no usable encoder is on ``PATH`` it fails and says why, so a green gate
  always means a real OGG/Opus artifact was encoded and decoded locally.

Fixture OGG pages are built here with an independent CRC implementation, so a
bug in the module's own checksum table cannot hide behind a matching fixture.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import math
import re
import struct
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from shutil import which

import pytest

from app import audio_encoding
from app.audio_encoding import (
    ENCODING_TIMEOUT_SECONDS,
    FFMPEG_BINARY,
    VOICE_BITRATE,
    VOICE_CHANNELS,
    VOICE_SAMPLE_RATE,
    AudioEncodingError,
    CodecProcess,
    drain_codec_cleanup,
    encode_voice,
    pending_codec_cleanup,
    validate_voice,
)
from app.audio_format import MAX_AUDIO_BYTES, MAX_DURATION_SECONDS

SENTINEL_NARRATION = "SENTINEL-NARRATION-DO-NOT-LOG"
SENTINEL_KEY = "SENTINEL-API-KEY-DO-NOT-LOG"


def asyncio_test(test: object) -> object:
    """Run an async test body on a fresh loop, matching this project's style.

    The suite has no pytest-asyncio dependency, and a fresh loop per test also
    makes the leaked-task assertions meaningful.
    """

    @functools.wraps(test)  # type: ignore[arg-type]
    def runner(*args: object, **kwargs: object) -> None:
        asyncio.run(test(*args, **kwargs))  # type: ignore[operator]

    return runner



@pytest.fixture(autouse=True)
def fresh_cleanup_registry() -> None:
    """Isolate the module's ownership registry: every test gets a fresh loop."""
    audio_encoding._CLEANUP_TASKS.clear()
    audio_encoding._UNCOLLECTED.clear()
    audio_encoding._ADOPTIONS.clear()


# --------------------------------------------------------------------------- #
# Fixtures built in-process: no committed binaries, no real speech.
# --------------------------------------------------------------------------- #


def make_wav(seconds: float = 0.5, *, rate: int = VOICE_SAMPLE_RATE, channels: int = 1,
             bits: int = 16, frames: bytes | None = None) -> bytes:
    """Build a canonical PCM WAV, with knobs for the invalid variants."""
    if frames is None:
        count = int(seconds * rate)
        frames = b"".join(
            struct.pack("<h", int(6000 * math.sin(2 * math.pi * 220 * i / rate)))
            for i in range(count)
        )
    width = bits // 8
    block_align = channels * width
    header = (
        b"RIFF"
        + struct.pack("<I", 36 + len(frames))
        + b"WAVE"
        + b"fmt "
        + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * block_align, block_align, bits)
        + b"data"
        + struct.pack("<I", len(frames))
    )
    return header + frames


def _reference_crc(data: bytes) -> int:
    """OGG's CRC-32 computed bit by bit, independently of the module's table."""
    crc = 0
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) & 0xFFFF_FFFF) ^ (0x04C1_1DB7 if crc & 0x8000_0000 else 0)
    return crc


def ogg_page(serial: int, sequence: int, granule: int, packets: Sequence[bytes], *,
             bos: bool = False, eos: bool = False, continued: bool = False,
             version: int = 0, crc: int | None = None, extra_flags: int = 0) -> bytes:
    segments: list[int] = []
    for packet in packets:
        remaining = len(packet)
        while remaining >= 255:
            segments.append(255)
            remaining -= 255
        segments.append(remaining)
    flags = (
        (0x01 if continued else 0)
        | (0x02 if bos else 0)
        | (0x04 if eos else 0)
        | extra_flags
    )
    head = (
        b"OggS"
        + bytes([version, flags])
        + struct.pack("<q", granule)
        + struct.pack("<Ii", serial, sequence)
    )
    tail = bytes([len(segments)]) + bytes(segments)
    body = b"".join(packets)
    page = head + bytes(4) + tail + body
    return head + struct.pack("<I", _reference_crc(page) if crc is None else crc) + tail + body


def opus_head(*, channels: int = 1, pre_skip: int = 312, rate: int = VOICE_SAMPLE_RATE,
              version: int = 1, mapping: int = 0) -> bytes:
    return (
        b"OpusHead"
        + bytes([version, channels])
        + struct.pack("<HIh", pre_skip, rate, 0)
        + bytes([mapping])
    )


def opus_tags() -> bytes:
    return b"OpusTags" + struct.pack("<I", 6) + b"pytest" + struct.pack("<I", 0)


def make_ogg(*, seconds: float = 0.5, serial: int = 0x5EED_1234, **head: object) -> bytes:
    """A structurally valid OGG/Opus stream with placeholder audio packets.

    The audio packets are not real Opus frames: the container is genuine, so
    the strict parser is genuinely exercised, while a decode of this fixture is
    always supplied by a process fake rather than pretended to be real audio.
    """
    pre_skip = int(head.get("pre_skip", 312))
    pages = [ogg_page(serial, 0, 0, [opus_head(**head)], bos=True)]  # type: ignore[arg-type]
    pages.append(ogg_page(serial, 1, 0, [opus_tags()]))
    frames = max(int(seconds * 1000 / 20), 1)
    packet = bytes([0x78]) + b"\x00" * 40
    sequence = 2
    done = 0
    while done < frames:
        batch = min(50, frames - done)
        done += batch
        pages.append(
            ogg_page(
                serial,
                sequence,
                done * 960 + pre_skip,
                [packet] * batch,
                eos=done >= frames,
            )
        )
        sequence += 1
    return b"".join(pages)


def decoded_pcm(seconds: float = 0.5) -> bytes:
    """What a bounded raw-PCM decode of a voice note looks like."""
    return b"\x00" * int(seconds * VOICE_SAMPLE_RATE * 2)


# --------------------------------------------------------------------------- #
# Process fakes
# --------------------------------------------------------------------------- #


class FakeReader:
    def __init__(self, payload: bytes = b"", *, chunks: Sequence[bytes] | None = None) -> None:
        self._queue = list(chunks) if chunks is not None else ([payload] if payload else [])

    async def read(self, n: int = -1) -> bytes:
        if not self._queue:
            return b""
        head = self._queue[0]
        if n is not None and n >= 0 and len(head) > n:
            self._queue[0] = head[n:]
            return head[:n]
        return self._queue.pop(0)


class EndlessReader:
    """Never reaches EOF, so only a timeout or cancellation can end the read."""

    def __init__(self, chunk: bytes = b"\x00" * 1024) -> None:
        self._chunk = chunk
        self.reading = asyncio.Event()

    async def read(self, n: int = -1) -> bytes:
        self.reading.set()
        await asyncio.sleep(0)
        return self._chunk


class FakeWriter:
    def __init__(self, *, broken: bool = False) -> None:
        self.written = bytearray()
        self.closed = 0
        self._broken = broken

    def write(self, data: bytes) -> None:
        if self._broken:
            raise BrokenPipeError
        self.written.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed += 1


class FakeProcess:
    """A codec process fake with per-test exit, output and stop behaviour."""

    def __init__(self, *, stdout: object = b"", stderr: bytes = b"", returncode: int = 0,
                 ignore_terminate: bool = False, ignore_kill: bool = False,
                 hang: bool = False, cancel_wait: int = 0, broken_stdin: bool = False,
                 no_stdin: bool = False, unwaitable: bool = False) -> None:
        self.stdin = None if no_stdin else FakeWriter(broken=broken_stdin)
        self.stdout = stdout if not isinstance(stdout, bytes) else FakeReader(stdout)
        self.stderr = FakeReader(stderr)
        self._returncode: int | None = None
        self._exit = returncode
        self._hang = hang
        self._ignore_terminate = ignore_terminate
        self._ignore_kill = ignore_kill
        self._cancel_wait = cancel_wait
        self._unwaitable = unwaitable
        self.terminated = 0
        self.killed = 0
        self.waits = 0
        self.signalled = asyncio.Event()
        self.kill_signalled = asyncio.Event()
        self._stopped = asyncio.Event()
        if not hang:
            self._stopped.set()

    def terminate(self) -> None:
        self.terminated += 1
        self.signalled.set()
        if not self._ignore_terminate:
            self._stopped.set()

    def kill(self) -> None:
        self.killed += 1
        self.signalled.set()
        self.kill_signalled.set()
        if not self._ignore_kill:
            self._stopped.set()

    def release(self) -> None:
        """Let a process that ignored every signal finally exit."""
        self._stopped.set()

    def make_waitable(self) -> None:
        """Let a child whose wait() kept failing become collectable again."""
        self._unwaitable = False

    async def wait(self) -> int:
        self.waits += 1
        if self._unwaitable:
            raise OSError("wait() is unavailable for this child")
        if self._cancel_wait > 0:
            self._cancel_wait -= 1
            raise asyncio.CancelledError
        await self._stopped.wait()
        self._returncode = self._exit
        return self._exit

    @property
    def returncode(self) -> int | None:
        return self._returncode


class Spawner:
    """Hands out queued fakes and records every argument vector seen."""

    def __init__(self, *processes: FakeProcess) -> None:
        self._queue = list(processes)
        self.calls: list[list[str]] = []
        self.spawned: list[FakeProcess] = []

    async def __call__(self, args: Sequence[str]) -> CodecProcess:
        self.calls.append(list(args))
        process = self._queue.pop(0) if len(self._queue) > 1 else self._queue[0]
        self.spawned.append(process)
        return process

    @property
    def last(self) -> FakeProcess:
        return self.spawned[-1]


def good_pipeline(ogg: bytes | None = None, *, seconds: float = 0.5) -> Spawner:
    """An encoder that returns a valid artifact and a decoder that reads it."""
    return Spawner(
        FakeProcess(stdout=ogg if ogg is not None else make_ogg(seconds=seconds)),
        FakeProcess(stdout=decoded_pcm(seconds)),
    )


# --------------------------------------------------------------------------- #
# Contract and arguments
# --------------------------------------------------------------------------- #


def test_encoding_subdeadline_is_fifteen_seconds() -> None:
    assert ENCODING_TIMEOUT_SECONDS == 15.0


@asyncio_test
async def test_encode_voice_returns_the_encoded_artifact() -> None:
    expected = make_ogg()
    spawner = good_pipeline(expected)
    assert await encode_voice(make_wav(), spawn=spawner) == expected


@asyncio_test
async def test_encode_requests_mono_24k_48kbps_ogg_opus_over_pipes() -> None:
    spawner = good_pipeline()
    encoded = await encode_voice(make_wav(), spawn=spawner)
    await validate_voice(encoded, spawn=spawner)
    encode_args, decode_args = spawner.calls
    assert encode_args[encode_args.index("-c:a") + 1] == "libopus"
    assert encode_args[encode_args.index("-b:a") + 1] == VOICE_BITRATE == "48k"
    assert encode_args[encode_args.index("-ac") + 1] == str(VOICE_CHANNELS) == "1"
    assert encode_args[encode_args.index("-ar") + 1] == str(VOICE_SAMPLE_RATE) == "24000"
    assert encode_args[-2:] == ["-f", "ogg"] or encode_args[-3:-1] == ["-f", "ogg"]
    assert encode_args[-1] == "pipe:1"
    assert "-nostdin" in encode_args and "pipe:0" in encode_args
    assert decode_args[decode_args.index("-f") + 1] == "ogg"
    assert decode_args[-1] == "pipe:1"
    assert decode_args[-2] == "s16le"


@asyncio_test
async def test_codec_arguments_carry_no_shell_paths_protocols_or_secrets() -> None:
    spawner = good_pipeline()
    await validate_voice(await encode_voice(make_wav(), spawn=spawner), spawn=spawner)
    assert len(spawner.calls) == 2
    for args in spawner.calls:
        joined = " ".join(args)
        assert SENTINEL_NARRATION not in joined and SENTINEL_KEY not in joined
        assert not re.search(r"https?://|ftp://|tcp://|rtmp://|file:", joined)
        for token in args:
            assert not any(ch in token for ch in ";|&><`$\n")
            assert token.startswith("pipe:") or not re.match(r"^[A-Za-z]:[\\/]|^/", token)


@asyncio_test
async def test_encode_feeds_the_whole_wav_and_closes_stdin() -> None:
    wav = make_wav(1.0)
    spawner = good_pipeline()
    await encode_voice(wav, spawn=spawner)
    encoder = spawner.spawned[0]
    assert encoder.stdin is not None
    assert bytes(encoder.stdin.written) == wav
    assert encoder.stdin.closed >= 1


# --------------------------------------------------------------------------- #
# WAV input validation
# --------------------------------------------------------------------------- #


BAD_WAVS: list[tuple[str, object]] = [
        ("empty", b""),
        ("not-bytes", "not bytes"),
        ("too-short", b"RIFF"),
        ("wrong-magic", b"FORM" + make_wav()[4:]),
        ("wrong-wave-tag", make_wav()[:8] + b"AVIX" + make_wav()[12:]),
        ("riff-size-lie", make_wav()[:4] + struct.pack("<I", 99999) + make_wav()[8:]),
        ("truncated-data", make_wav()[:-100]),
        ("stereo", make_wav(channels=2)),
        ("eight-bit", make_wav(bits=8)),
        ("no-frames", make_wav(frames=b"")),
        ("odd-length-frames", make_wav(frames=b"\x01\x02\x03")),
]


@pytest.mark.parametrize(("name", "payload"), BAD_WAVS, ids=[n for n, _ in BAD_WAVS])
@asyncio_test
async def test_encode_rejects_unusable_wav_without_spawning(name: str, payload: object) -> None:
    spawner = good_pipeline()
    with pytest.raises(AudioEncodingError):
        await encode_voice(payload, spawn=spawner)  # type: ignore[arg-type]
    assert spawner.calls == [], f"{name} reached the encoder"


@asyncio_test
async def test_encode_rejects_wav_above_the_ten_mebibyte_cap() -> None:
    oversized = make_wav(frames=b"\x00" * (MAX_AUDIO_BYTES + 2))
    spawner = good_pipeline()
    with pytest.raises(AudioEncodingError):
        await encode_voice(oversized, spawn=spawner)
    assert spawner.calls == []


@asyncio_test
async def test_encode_rejects_wav_longer_than_the_duration_limit() -> None:
    frames = b"\x00" * ((MAX_DURATION_SECONDS + 1) * VOICE_SAMPLE_RATE * 2)
    spawner = good_pipeline()
    with pytest.raises(AudioEncodingError):
        await encode_voice(make_wav(frames=frames), spawn=spawner)
    assert spawner.calls == []


@asyncio_test
async def test_wav_at_the_duration_limit_is_accepted() -> None:
    frames = b"\x00" * (MAX_DURATION_SECONDS * VOICE_SAMPLE_RATE * 2)
    spawner = good_pipeline()
    assert await encode_voice(make_wav(frames=frames), spawn=spawner)


# --------------------------------------------------------------------------- #
# Strict OGG/Opus container validation
# --------------------------------------------------------------------------- #

SERIAL = 0x5EED_1234


def _no_audio_pages() -> bytes:
    return ogg_page(SERIAL, 0, 0, [opus_head()], bos=True) + ogg_page(
        SERIAL, 1, 0, [opus_tags()], eos=True
    )


BROKEN_ARTIFACTS: list[tuple[str, bytes]] = [
    ("empty", b""),
    ("magic-only", b"OggS" + b"OpusHead" + b"\x00" * 64),
    ("wrong-magic", b"OggX" + make_ogg()[4:]),
    ("unsupported-version", make_ogg(serial=SERIAL)[:4] + b"\x01" + make_ogg()[5:]),
    ("bad-checksum", b"".join([
        ogg_page(SERIAL, 0, 0, [opus_head()], bos=True, crc=0xDEAD_BEEF),
        ogg_page(SERIAL, 1, 0, [opus_tags()]),
        ogg_page(SERIAL, 2, 1920 + 312, [b"\x78" + b"\x00" * 10], eos=True),
    ])),
    ("truncated-final-page", make_ogg()[:-20]),
    ("truncated-header", make_ogg()[:15]),
    ("trailing-garbage", make_ogg() + b"junkjunkjunk"),
    ("missing-opus-head", b"".join([
        ogg_page(SERIAL, 0, 0, [b"NotOpus!" + b"\x00" * 16], bos=True),
        ogg_page(SERIAL, 1, 0, [opus_tags()]),
        ogg_page(SERIAL, 2, 1920 + 312, [b"\x78" + b"\x00" * 10], eos=True),
    ])),
    ("missing-opus-tags", b"".join([
        ogg_page(SERIAL, 0, 0, [opus_head()], bos=True),
        ogg_page(SERIAL, 1, 0, [b"NotTags!" + b"\x00" * 8]),
        ogg_page(SERIAL, 2, 1920 + 312, [b"\x78" + b"\x00" * 10], eos=True),
    ])),
    ("no-bos-flag", b"".join([
        ogg_page(SERIAL, 0, 0, [opus_head()]),
        ogg_page(SERIAL, 1, 0, [opus_tags()]),
        ogg_page(SERIAL, 2, 1920 + 312, [b"\x78" + b"\x00" * 10], eos=True),
    ])),
    ("no-eos-flag", b"".join([
        ogg_page(SERIAL, 0, 0, [opus_head()], bos=True),
        ogg_page(SERIAL, 1, 0, [opus_tags()]),
        ogg_page(SERIAL, 2, 1920 + 312, [b"\x78" + b"\x00" * 10]),
    ])),
    ("out-of-order-pages", b"".join([
        ogg_page(SERIAL, 0, 0, [opus_head()], bos=True),
        ogg_page(SERIAL, 5, 0, [opus_tags()]),
        ogg_page(SERIAL, 6, 1920 + 312, [b"\x78" + b"\x00" * 10], eos=True),
    ])),
    ("multiplexed-serials", b"".join([
        ogg_page(SERIAL, 0, 0, [opus_head()], bos=True),
        ogg_page(SERIAL + 1, 1, 0, [opus_tags()]),
        ogg_page(SERIAL, 2, 1920 + 312, [b"\x78" + b"\x00" * 10], eos=True),
    ])),
    ("stereo-opus-head", make_ogg(channels=2)),
    ("wrong-input-rate", make_ogg(rate=48000)),
    ("unsupported-head-version", make_ogg(version=2)),
    ("unsupported-channel-mapping", make_ogg(mapping=1)),
    ("short-opus-head", b"".join([
        ogg_page(SERIAL, 0, 0, [b"OpusHead\x01\x01"], bos=True),
        ogg_page(SERIAL, 1, 0, [opus_tags()]),
        ogg_page(SERIAL, 2, 1920 + 312, [b"\x78" + b"\x00" * 10], eos=True),
    ])),
    ("headers-only-no-audio", _no_audio_pages()),
    ("zero-duration", b"".join([
        ogg_page(SERIAL, 0, 0, [opus_head()], bos=True),
        ogg_page(SERIAL, 1, 0, [opus_tags()]),
        ogg_page(SERIAL, 2, 312, [b"\x78" + b"\x00" * 10], eos=True),
    ])),
    ("duration-over-limit", make_ogg(seconds=MAX_DURATION_SECONDS + 5)),
    ("reserved-page-flags", b"".join([
        ogg_page(SERIAL, 0, 0, [opus_head()], bos=True, extra_flags=0x08),
        ogg_page(SERIAL, 1, 0, [opus_tags()]),
        ogg_page(SERIAL, 2, 1920 + 312, [b"\x78" + b"\x00" * 10], eos=True),
    ])),
    ("first-page-continuation", b"".join([
        ogg_page(SERIAL, 0, 0, [opus_head()], bos=True, continued=True),
        ogg_page(SERIAL, 1, 0, [opus_tags()]),
        ogg_page(SERIAL, 2, 1920 + 312, [b"\x78" + b"\x00" * 10], eos=True),
    ])),
    ("padded-opus-head-mapping-zero", b"".join([
        ogg_page(SERIAL, 0, 0, [opus_head() + b"\x00" * 8], bos=True),
        ogg_page(SERIAL, 1, 0, [opus_tags()]),
        ogg_page(SERIAL, 2, 1920 + 312, [b"\x78" + b"\x00" * 10], eos=True),
    ])),
    ("truncated-opus-tags", b"".join([
        ogg_page(SERIAL, 0, 0, [opus_head()], bos=True),
        ogg_page(SERIAL, 1, 0, [b"OpusTags" + struct.pack("<I", 64) + b"short"]),
        ogg_page(SERIAL, 2, 1920 + 312, [b"\x78" + b"\x00" * 10], eos=True),
    ])),
    ("opus-tags-comment-count-lies", b"".join([
        ogg_page(SERIAL, 0, 0, [opus_head()], bos=True),
        ogg_page(SERIAL, 1, 0, [
            b"OpusTags" + struct.pack("<I", 6) + b"pytest" + struct.pack("<I", 9)
        ]),
        ogg_page(SERIAL, 2, 1920 + 312, [b"\x78" + b"\x00" * 10], eos=True),
    ])),
]


@pytest.mark.parametrize(("name", "artifact"), BROKEN_ARTIFACTS, ids=[n for n, _ in BROKEN_ARTIFACTS])
@asyncio_test
async def test_validate_voice_rejects_malformed_ogg_opus(name: str, artifact: bytes) -> None:
    decoder = Spawner(FakeProcess(stdout=decoded_pcm()))
    with pytest.raises(AudioEncodingError):
        await validate_voice(artifact, spawn=decoder)
    assert decoder.calls == [], f"{name} reached a decode it should never have had"


@pytest.mark.parametrize(("name", "artifact"), BROKEN_ARTIFACTS, ids=[n for n, _ in BROKEN_ARTIFACTS])
@asyncio_test
async def test_the_single_final_validation_rejects_malformed_encoder_output(
    name: str, artifact: bytes
) -> None:
    """Encoding no longer self-validates, so the one final check must catch this."""
    spawner = Spawner(FakeProcess(stdout=artifact), FakeProcess(stdout=decoded_pcm()))
    encoded = await encode_voice(make_wav(), spawn=spawner)
    assert encoded == artifact
    with pytest.raises(AudioEncodingError):
        await validate_voice(encoded, spawn=spawner)


@asyncio_test
async def test_validate_voice_accepts_a_complete_stream_and_returns_none() -> None:
    decoder = Spawner(FakeProcess(stdout=decoded_pcm()))
    assert await validate_voice(make_ogg(), spawn=decoder) is None
    assert len(decoder.calls) == 1


@asyncio_test
async def test_validate_voice_rejects_an_artifact_above_the_byte_cap() -> None:
    decoder = Spawner(FakeProcess(stdout=decoded_pcm()))
    with pytest.raises(AudioEncodingError):
        await validate_voice(make_ogg() + b"\x00" * MAX_AUDIO_BYTES, spawn=decoder)
    assert decoder.calls == []


# --------------------------------------------------------------------------- #
# Bounded decode validation
# --------------------------------------------------------------------------- #


@asyncio_test
async def test_validate_rejects_an_artifact_the_decoder_cannot_read() -> None:
    decoder = Spawner(FakeProcess(stdout=b"", returncode=1))
    with pytest.raises(AudioEncodingError):
        await validate_voice(make_ogg(), spawn=decoder)


@asyncio_test
async def test_validate_rejects_a_decode_with_no_frames() -> None:
    decoder = Spawner(FakeProcess(stdout=b""))
    with pytest.raises(AudioEncodingError):
        await validate_voice(make_ogg(), spawn=decoder)


@asyncio_test
async def test_validate_rejects_a_decode_with_a_partial_sample() -> None:
    decoder = Spawner(FakeProcess(stdout=decoded_pcm() + b"\x01"))
    with pytest.raises(AudioEncodingError):
        await validate_voice(make_ogg(), spawn=decoder)


@asyncio_test
async def test_decode_refuses_to_conceal_corrupt_packets() -> None:
    """Concealment would hide damage the container check cannot see."""
    decoder = Spawner(FakeProcess(stdout=decoded_pcm()))
    await validate_voice(make_ogg(), spawn=decoder)
    args = decoder.calls[0]
    assert "-xerror" in args
    assert args[args.index("-err_detect") + 1] == "explode"


@asyncio_test
async def test_validate_rejects_a_decode_longer_than_the_duration_limit() -> None:
    frames = b"\x00" * ((MAX_DURATION_SECONDS + 10) * VOICE_SAMPLE_RATE * 2)
    decoder = Spawner(FakeProcess(stdout=FakeReader(chunks=[frames[i:i + 65536]
                                                            for i in range(0, len(frames), 65536)])))
    with pytest.raises(AudioEncodingError):
        await validate_voice(make_ogg(), spawn=decoder)


@asyncio_test
async def test_validate_caps_decoded_output_instead_of_accumulating() -> None:
    decoder = Spawner(FakeProcess(stdout=EndlessReader(b"\x00" * 65536)))
    with pytest.raises(AudioEncodingError):
        await validate_voice(make_ogg(), spawn=decoder)
    assert decoder.last.terminated + decoder.last.killed >= 1


@asyncio_test
async def test_encode_does_not_decode_its_own_output() -> None:
    """Encoding encodes; the caller runs the single decode validation once."""
    spawner = good_pipeline()
    await encode_voice(make_wav(), spawn=spawner)
    assert len(spawner.calls) == 1
    assert spawner.calls[0][spawner.calls[0].index("-c:a") + 1] == "libopus"


@asyncio_test
async def test_encode_then_validate_spawns_exactly_two_processes() -> None:
    spawner = good_pipeline()
    await validate_voice(await encode_voice(make_wav(), spawn=spawner), spawn=spawner)
    assert len(spawner.calls) == 2


# --------------------------------------------------------------------------- #
# Process failures, caps, timeouts and cancellation
# --------------------------------------------------------------------------- #


@asyncio_test
async def test_nonzero_encoder_exit_fails_closed_and_is_reaped() -> None:
    spawner = Spawner(FakeProcess(stdout=make_ogg(), returncode=3))
    with pytest.raises(AudioEncodingError):
        await encode_voice(make_wav(), spawn=spawner)
    assert spawner.last.stdin is not None and spawner.last.stdin.closed >= 1


@asyncio_test
async def test_encoder_output_above_the_cap_is_rejected_and_stopped() -> None:
    spawner = Spawner(FakeProcess(stdout=EndlessReader(b"\x00" * 65536)))
    with pytest.raises(AudioEncodingError):
        await encode_voice(make_wav(), spawn=spawner)
    assert spawner.last.terminated + spawner.last.killed >= 1


@asyncio_test
async def test_a_hanging_encoder_times_out_and_is_terminated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio_encoding, "ENCODING_TIMEOUT_SECONDS", 0.05)
    process = FakeProcess(stdout=EndlessReader(), hang=True)
    spawner = Spawner(process)
    with pytest.raises(AudioEncodingError):
        await encode_voice(make_wav(), spawn=spawner)
    assert process.terminated >= 1


@asyncio_test
async def test_a_missing_stdin_pipe_fails_closed() -> None:
    spawner = Spawner(FakeProcess(stdout=make_ogg(), no_stdin=True))
    with pytest.raises(AudioEncodingError):
        await encode_voice(make_wav(), spawn=spawner)


@asyncio_test
async def test_a_broken_stdin_pipe_defers_to_the_exit_code() -> None:
    spawner = Spawner(FakeProcess(stdout=make_ogg(), returncode=1, broken_stdin=True))
    with pytest.raises(AudioEncodingError):
        await encode_voice(make_wav(), spawn=spawner)


@asyncio_test
async def test_cancellation_propagates_and_stops_the_encoder() -> None:
    reader = EndlessReader()
    process = FakeProcess(stdout=reader, hang=True)
    task = asyncio.ensure_future(encode_voice(make_wav(), spawn=Spawner(process)))
    await reader.reading.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.terminated >= 1
    assert process.returncode is not None
    assert pending_codec_cleanup() == 0


@asyncio_test
async def test_repeated_cancellation_still_reaps_and_never_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second cancellation lands inside the reap; the exit is still confirmed."""
    monkeypatch.setattr(audio_encoding, "TERMINATE_GRACE_SECONDS", 0.05)
    reader = EndlessReader()
    process = FakeProcess(stdout=reader, hang=True, ignore_terminate=True, cancel_wait=2)
    task = asyncio.ensure_future(encode_voice(make_wav(), spawn=Spawner(process)))
    await reader.reading.wait()
    task.cancel()
    await process.signalled.wait()  # the reap has begun and sent terminate
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert process.terminated >= 1
    assert process.killed >= 1
    await drain_codec_cleanup(1)
    assert process.returncode is not None
    assert pending_codec_cleanup() == 0


async def _child_task_cancellation_shape(fut: object, _timeout: float) -> object:
    """Simulate only the *cancellation shape* of CPython 3.11's ``asyncio.wait_for``.

    It runs the body in a child task and, on cancellation, awaits that child
    through a helper that a second cancellation interrupts -- which is the bit
    that let a cancelled call return while the child was still escalating.

    This is deliberately not a full ``wait_for``: ``_timeout`` is ignored and no
    deadline is enforced, so it is only valid for tests that cancel explicitly.
    """
    loop = asyncio.get_running_loop()
    child = asyncio.ensure_future(fut)
    waiter = loop.create_future()

    def release(_task: object) -> None:
        if not waiter.done():
            waiter.set_result(None)

    child.add_done_callback(release)
    try:
        await waiter
    except asyncio.CancelledError:
        if child.done():
            return child.result()
        child.remove_done_callback(release)
        child.cancel()
        settled = loop.create_future()
        child.add_done_callback(lambda _t: settled.done() or settled.set_result(None))
        await settled
        raise
    return child.result()


CANCELLABLE_CALLS: list[tuple[str, Callable[[object], Awaitable[object]]]] = [
    ("encode_voice", lambda spawn: encode_voice(make_wav(), spawn=spawn)),  # type: ignore[arg-type]
    ("validate_voice", lambda spawn: validate_voice(make_ogg(), spawn=spawn)),  # type: ignore[arg-type]
]


@pytest.mark.parametrize(("name", "call"), CANCELLABLE_CALLS,
                         ids=[n for n, _ in CANCELLABLE_CALLS])
@asyncio_test
async def test_cleanup_stays_in_the_callers_task_under_the_3_11_cancellation_shape(
    name: str,
    call: Callable[[object], Awaitable[object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a cancelled call must not return before the child is stopped."""
    watchdog = asyncio.wait_for  # the real one, captured before the patch
    monkeypatch.setattr(asyncio, "wait_for", _child_task_cancellation_shape)
    monkeypatch.setattr(audio_encoding, "TERMINATE_GRACE_SECONDS", 0.05)
    reader = EndlessReader()
    process = FakeProcess(stdout=reader, hang=True, ignore_terminate=True, cancel_wait=2)
    task = asyncio.ensure_future(call(Spawner(process)))
    await reader.reading.wait()
    task.cancel()
    await process.signalled.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await watchdog(task, 5)  # a regression must fail, never hang
    assert process.terminated >= 1, name
    assert process.killed >= 1, name
    assert process.returncode is not None, name
    assert pending_codec_cleanup() == 0, name
    assert asyncio.all_tasks() == {asyncio.current_task()}  # no abandoned child task


@asyncio_test
async def test_a_process_that_ignores_terminate_is_killed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio_encoding, "ENCODING_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(audio_encoding, "TERMINATE_GRACE_SECONDS", 0.05)
    process = FakeProcess(stdout=EndlessReader(), hang=True, ignore_terminate=True)
    with pytest.raises(AudioEncodingError):
        await encode_voice(make_wav(), spawn=Spawner(process))
    assert process.terminated >= 1 and process.killed >= 1


@asyncio_test
async def test_a_process_that_ignores_kill_fails_explicitly_and_stays_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unkillable child is never reported as reaped: it fails and stays tracked."""
    monkeypatch.setattr(audio_encoding, "ENCODING_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(audio_encoding, "TERMINATE_GRACE_SECONDS", 0.05)
    process = FakeProcess(stdout=EndlessReader(), hang=True, ignore_terminate=True,
                          ignore_kill=True)
    with pytest.raises(AudioEncodingError):
        await asyncio.wait_for(encode_voice(make_wav(), spawn=Spawner(process)), 5)
    assert process.terminated >= 1 and process.killed >= 1
    assert process.returncode is None
    assert pending_codec_cleanup() == 1

    with pytest.raises(AudioEncodingError):
        await drain_codec_cleanup(0.05)  # shutdown refuses to claim a clean exit

    process.release()
    await drain_codec_cleanup(1)
    assert process.returncode is not None
    assert pending_codec_cleanup() == 0


@asyncio_test
async def test_a_spawn_failure_fails_closed() -> None:
    async def refuse(_args: Sequence[str]) -> CodecProcess:
        raise AudioEncodingError("Audio encoder unavailable")

    with pytest.raises(AudioEncodingError):
        await encode_voice(make_wav(), spawn=refuse)


@asyncio_test
async def test_a_missing_encoder_on_path_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio_encoding, "which", lambda _name: None)
    with pytest.raises(AudioEncodingError):
        await encode_voice(make_wav())


@asyncio_test
async def test_cancellation_during_spawn_still_reaps_the_late_child() -> None:
    """A spawn that completes after the cancellation must not leak an unowned child."""
    process = FakeProcess(stdout=EndlessReader(), hang=True)
    entered = asyncio.Event()
    released = asyncio.Event()

    async def slow_spawn(_args: Sequence[str]) -> CodecProcess:
        entered.set()
        await released.wait()
        return process

    task = asyncio.ensure_future(encode_voice(make_wav(), spawn=slow_spawn))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.stdin is not None and bytes(process.stdin.written) == b""
    assert process.terminated + process.killed == 0  # nothing existed to stop yet
    assert pending_codec_cleanup() == 1  # ownership of the late child is tracked

    released.set()
    await drain_codec_cleanup(2)
    assert process.terminated + process.killed >= 1
    assert process.returncode is not None
    assert pending_codec_cleanup() == 0


def test_the_corruption_fixture_keeps_the_container_valid() -> None:
    """Prove the gate's fixture locally: real audio damaged, container intact."""
    stream = make_ogg(seconds=1.0)
    corrupted = _invalidate_an_audio_packet(stream)
    assert corrupted != stream
    assert len(_split_pages(corrupted)) == len(_split_pages(stream))
    assert audio_encoding._inspect_ogg_opus(corrupted) > 0
    assert INVALID_OPUS_PACKET in corrupted


@asyncio_test
async def test_a_child_whose_wait_fails_keeps_ownership_and_fails_the_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wait() that errors is not an exit, so ownership must not be discarded."""
    monkeypatch.setattr(audio_encoding, "ENCODING_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(audio_encoding, "TERMINATE_GRACE_SECONDS", 0.05)
    process = FakeProcess(stdout=EndlessReader(), hang=True, unwaitable=True)
    with pytest.raises(AudioEncodingError):
        await asyncio.wait_for(encode_voice(make_wav(), spawn=Spawner(process)), 5)
    assert process.returncode is None
    assert pending_codec_cleanup() == 1

    with pytest.raises(AudioEncodingError):
        await drain_codec_cleanup(0.2)
    assert pending_codec_cleanup() == 1  # still owned, still unproven

    process.make_waitable()
    process.release()
    await drain_codec_cleanup(1)
    assert process.returncode is not None
    assert pending_codec_cleanup() == 0


@asyncio_test
async def test_a_late_child_that_cannot_be_stopped_is_not_silently_forgotten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The late-spawn cleanup swallows errors, so ownership must outlive them."""
    monkeypatch.setattr(audio_encoding, "TERMINATE_GRACE_SECONDS", 0.05)
    process = FakeProcess(stdout=EndlessReader(), hang=True, ignore_terminate=True,
                          ignore_kill=True)
    entered = asyncio.Event()
    released = asyncio.Event()

    async def slow_spawn(_args: Sequence[str]) -> CodecProcess:
        entered.set()
        await released.wait()
        return process

    task = asyncio.ensure_future(encode_voice(make_wav(), spawn=slow_spawn))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    released.set()
    await process.kill_signalled.wait()  # both graces have now elapsed

    with pytest.raises(AudioEncodingError):
        await drain_codec_cleanup(0.2)
    assert process.killed >= 1
    assert pending_codec_cleanup() >= 1

    process.release()
    await drain_codec_cleanup(2)
    assert process.returncode is not None
    assert pending_codec_cleanup() == 0


@asyncio_test
async def test_shutdown_drain_is_a_no_op_when_nothing_is_outstanding() -> None:
    spawner = good_pipeline()
    await encode_voice(make_wav(), spawn=spawner)
    assert pending_codec_cleanup() == 0
    await drain_codec_cleanup(0)


@asyncio_test
async def test_codec_leaves_no_pending_tasks_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio_encoding, "ENCODING_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(audio_encoding, "TERMINATE_GRACE_SECONDS", 0.05)
    before = len(asyncio.all_tasks())
    process = FakeProcess(stdout=EndlessReader(), hang=True, ignore_terminate=True)
    with pytest.raises(AudioEncodingError):
        await encode_voice(make_wav(), spawn=Spawner(process))
    assert process.killed >= 1 and process.returncode is not None
    await drain_codec_cleanup(1)
    assert pending_codec_cleanup() == 0
    assert len(asyncio.all_tasks()) <= before


@asyncio_test
async def test_a_successful_run_reaps_without_signalling() -> None:
    spawner = good_pipeline()
    await encode_voice(make_wav(), spawn=spawner)
    for process in spawner.spawned:
        assert process.terminated == 0 and process.killed == 0
        assert process.returncode == 0


# --------------------------------------------------------------------------- #
# Privacy: no stderr, narration, keys or raw errors anywhere
# --------------------------------------------------------------------------- #


@asyncio_test
async def test_encoder_stderr_never_reaches_logs_or_the_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    leak = f"ffmpeg: {SENTINEL_NARRATION} key={SENTINEL_KEY}".encode()
    spawner = Spawner(FakeProcess(stdout=b"garbage", stderr=leak, returncode=1))
    with caplog.at_level(logging.DEBUG), pytest.raises(AudioEncodingError) as caught:
        await encode_voice(make_wav(), spawn=spawner)
    rendered = f"{caught.value}{caught.value.args}{caught.value.__cause__!r}"
    assert SENTINEL_NARRATION not in rendered and SENTINEL_KEY not in rendered
    assert SENTINEL_NARRATION not in caplog.text and SENTINEL_KEY not in caplog.text
    assert caplog.text == ""


@asyncio_test
async def test_an_unexpected_pipe_error_becomes_a_safe_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class ExplodingReader:
        async def read(self, n: int = -1) -> bytes:
            raise OSError(f"disk said {SENTINEL_NARRATION}")

    spawner = Spawner(FakeProcess(stdout=ExplodingReader()))
    with caplog.at_level(logging.DEBUG), pytest.raises(AudioEncodingError) as caught:
        await encode_voice(make_wav(), spawn=spawner)
    assert SENTINEL_NARRATION not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caplog.text == ""


@asyncio_test
async def test_validation_messages_never_echo_the_rejected_bytes() -> None:
    payload = SENTINEL_NARRATION.encode() * 8
    decoder = Spawner(FakeProcess(stdout=decoded_pcm()))
    with pytest.raises(AudioEncodingError) as caught:
        await validate_voice(payload, spawn=decoder)
    assert SENTINEL_NARRATION not in str(caught.value)


# --------------------------------------------------------------------------- #
# Real local codec: PATH-selected FFmpeg, never skipped
# --------------------------------------------------------------------------- #


def _ffmpeg_on_path() -> str:
    executable = which(FFMPEG_BINARY)
    if executable is None:
        pytest.fail(
            f"Required local codec test cannot run: {FFMPEG_BINARY!r} is not on PATH. "
            "Install FFmpeg with libopus and the ogg muxer, or put it on PATH; "
            "this gate must not be skipped."
        )
    return executable


def _report(executable: str, flag: str, needle: str) -> bool:
    result = subprocess.run(
        [executable, "-hide_banner", flag],
        capture_output=True,
        timeout=30,
        check=False,
    )
    return needle in result.stdout.decode("utf-8", "replace")


@asyncio_test
async def test_real_ffmpeg_round_trips_wav_to_decodable_ogg_opus() -> None:
    """The mandatory real-codec gate: encode and validate a genuine artifact."""
    executable = _ffmpeg_on_path()
    missing = []
    if not _report(executable, "-encoders", "libopus"):
        missing.append("libopus encoder")
    if not _report(executable, "-muxers", " ogg"):
        missing.append("ogg muxer")
    if not (_report(executable, "-decoders", "libopus") or _report(executable, "-decoders", " opus")):
        missing.append("an Opus decoder")
    if not _report(executable, "-muxers", " s16le"):
        missing.append("s16le muxer")
    if missing:
        pytest.fail(
            "Required local codec test cannot run: the FFmpeg on PATH "
            f"({executable}) lacks {', '.join(missing)}. A build with libopus and "
            "the ogg muxer is a prerequisite of this feature; this gate must not be skipped."
        )

    wav = make_wav(1.0)
    encoded = await encode_voice(wav)
    assert encoded[:4] == b"OggS"
    assert b"OpusHead" in encoded[:128]
    assert 0 < len(encoded) < len(wav)
    await validate_voice(encoded)


@asyncio_test
async def test_real_ffmpeg_rejects_a_corrupted_opus_packet() -> None:
    """A damaged payload with a valid container and recomputed CRCs must fail.

    This is the case no container check can catch and a concealing decoder
    would wave through, so only the real decoder can prove it.
    """
    executable = _ffmpeg_on_path()
    if not _report(executable, "-muxers", " ogg"):
        pytest.fail(
            "Required local codec test cannot run: the FFmpeg on PATH "
            f"({executable}) lacks the ogg muxer; this gate must not be skipped."
        )
    encoded = await encode_voice(make_wav(1.0))
    corrupted = _invalidate_an_audio_packet(encoded)
    assert corrupted != encoded

    # The container, headers, lacing, sequence, granule and CRCs still check
    # out, so a rejection can only come from the decoder refusing the payload.
    assert audio_encoding._inspect_ogg_opus(corrupted) > 0

    with pytest.raises(AudioEncodingError):
        await validate_voice(corrupted)


#: A TOC byte with code 3 followed by a frame count of zero. RFC 6716 requires
#: 1..48 frames, so this is invalid Opus framing rather than merely damaged
#: audio that a decoder could still render.
INVALID_OPUS_PACKET = bytes([0x03, 0x00])


def _invalidate_an_audio_packet(stream: bytes) -> bytes:
    """Replace one self-contained audio page's payload with invalid Opus framing.

    Header pages are skipped, so this damages real audio; the page keeps its
    flags, granule, serial and sequence, and its CRC is recomputed, leaving the
    container valid and the decoder as the only thing that can object.
    """
    pages = _split_pages(stream)
    assert len(pages) >= 3, "expected two header pages plus at least one audio page"
    index = _self_contained_audio_page(pages)
    page = bytearray(pages[index])
    rebuilt = bytearray(page[:26])
    rebuilt += bytes([1, len(INVALID_OPUS_PACKET)])
    rebuilt += INVALID_OPUS_PACKET
    rebuilt[22:26] = bytes(4)
    rebuilt[22:26] = struct.pack("<I", _reference_crc(bytes(rebuilt)))
    pages[index] = bytes(rebuilt)
    return b"".join(pages)


def _self_contained_audio_page(pages: Sequence[bytes]) -> int:
    """Pick an audio page whose packets neither continue in nor out of it."""
    candidates = []
    for index in range(2, len(pages)):  # pages 0 and 1 carry OpusHead/OpusTags
        page = pages[index]
        segment_count = page[26]
        segments = page[27 : 27 + segment_count]
        body = page[27 + segment_count :]
        if page[5] & 0x01 or not segments or segments[-1] == 255:
            continue  # continued into or out of this page
        if body[:4] == b"Opus":
            continue  # still a header packet
        candidates.append(index)
    if not candidates:
        pytest.fail(
            "Required local codec test cannot run: the encoded stream has no "
            "self-contained audio page to damage, so the strict-decode gate "
            "cannot be proven; this gate must not be skipped."
        )
    return candidates[len(candidates) // 2]


def _split_pages(stream: bytes) -> list[bytes]:
    pages: list[bytes] = []
    offset = 0
    while offset < len(stream):
        assert stream[offset : offset + 4] == b"OggS"
        segment_count = stream[offset + 26]
        table_end = offset + 27 + segment_count
        end = table_end + sum(stream[offset + 27 : table_end])
        pages.append(stream[offset:end])
        offset = end
    return pages
