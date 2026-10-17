"""The codec boundary: a validated WAV paragraph becomes an OGG/Opus voice note.

FFmpeg is resolved from ``PATH`` and spawned without a shell. It only ever
receives bounded audio over pipes -- never a narration string, an API key, a
filesystem path or a network URL -- and its stderr is drained to EOF but never
logged or attached to an exception, so encoder output cannot leak user audio.

:func:`encode_voice` encodes and nothing more. The one decode validation of the
bytes that will actually be delivered belongs to the caller, which runs
:func:`validate_voice` once on the artifact it holds, so a single subdeadline
covers encode plus that final check without validating the same bytes twice.

Every spawn is terminated and reaped on success, failure, timeout and
cancellation, including a cancellation that arrives while an earlier
cancellation is still being cleaned up. A child that survives both grace
periods, or whose waiter could not collect it at all, is never reported as
reaped: the call fails with a safe error and the child stays owned until its
``returncode`` proves otherwise, which keeps :func:`drain_codec_cleanup`
failing, so shutdown cannot return while a codec process is still live. Output is
counted as it is read, so a runaway encoder cannot accumulate unbounded memory.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
import time
from collections.abc import Awaitable, Callable, Sequence
from shutil import which
from typing import Protocol

from pydantic import ValidationError

from app.audio_format import (
    MAX_AUDIO_BYTES,
    MAX_DURATION_SECONDS,
    PCM_CHANNELS,
    PCM_SAMPLE_RATE,
    PCM_SAMPLE_WIDTH,
    InvalidAudioError,
    NarrationAudio,
    normalize_audio,
)

FFMPEG_BINARY = "ffmpeg"

#: Per-call codec budget. The caller wraps encoding plus the single final
#: validation in one subdeadline; this is the ceiling for either step.
ENCODING_TIMEOUT_SECONDS = 15.0

#: How long a terminate() and then a kill() each get before escalating.
TERMINATE_GRACE_SECONDS = 2.0

# The delivered voice keeps the endpoint's own shape, so limits never drift.
VOICE_SAMPLE_RATE = PCM_SAMPLE_RATE
VOICE_CHANNELS = PCM_CHANNELS
VOICE_SAMPLE_WIDTH = PCM_SAMPLE_WIDTH
VOICE_BITRATE = "48k"

_OPUS_RATE = 48_000
_WAV_PCM_FORMAT = 1
_READ_CHUNK = 64 * 1024
_MAX_DECODED_BYTES = int(MAX_DURATION_SECONDS) * VOICE_SAMPLE_RATE * VOICE_SAMPLE_WIDTH
_OGG_HEADER_BYTES = 27
_OPUS_HEAD_BYTES = 19
_OPUS_TAGS_BYTES = 16


class AudioEncodingError(RuntimeError):
    """A codec failure carrying only a safe, generic message."""


class CodecWriter(Protocol):
    """The subset of a subprocess stdin pipe the codec needs."""

    def write(self, data: bytes, /) -> None: ...
    async def drain(self) -> None: ...
    def close(self) -> None: ...


class CodecReader(Protocol):
    """The subset of a subprocess output pipe the codec needs."""

    async def read(self, n: int = -1, /) -> bytes: ...


class CodecProcess(Protocol):
    """A spawned codec process, satisfied by :class:`asyncio.subprocess.Process`."""

    @property
    def stdin(self) -> CodecWriter | None: ...
    @property
    def stdout(self) -> CodecReader | None: ...
    @property
    def stderr(self) -> CodecReader | None: ...
    @property
    def returncode(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    async def wait(self) -> int: ...


#: Typed seam: tests inject process fakes for exits, caps, timeouts and kills.
SpawnFn = Callable[[Sequence[str]], Awaitable[CodecProcess]]


#: Cleanup that outlived the call that started it, awaited by shutdown.
_CLEANUP_TASKS: set[asyncio.Task[None]] = set()

#: Processes whose exit was never proven. A waiter task ending is not proof --
#: only ``returncode`` is -- so ownership is recorded against the process here
#: and survives a waiter that could not collect the child at all.
_UNCOLLECTED: set[CodecProcess] = set()

#: Cleanups still waiting for a spawn to finish, so they own no process yet.
_ADOPTIONS: set[asyncio.Task[None]] = set()


def pending_codec_cleanup() -> int:
    """How many codec children are still owned without a proven exit.

    Counts one per child -- a live waiter and its unproven child are the same
    ownership -- plus any spawn still being adopted after a cancellation.
    """
    _prune()
    return len(_UNCOLLECTED) + len(_ADOPTIONS)


async def drain_codec_cleanup(timeout: float = ENCODING_TIMEOUT_SECONDS) -> None:
    """Await every outstanding codec cleanup, so shutdown drops none of them.

    Raises :class:`AudioEncodingError` while any child is still owned when the
    bound expires, rather than letting shutdown claim a clean exit it cannot
    prove. A child left behind by a waiter that failed outright gets one fresh
    waiter here; if that still cannot collect it, this fails rather than
    spinning, and the child stays owned for the next attempt.
    """
    deadline = time.monotonic() + timeout
    retried = False
    while True:
        _prune()
        if not _CLEANUP_TASKS and not _UNCOLLECTED:
            return
        if not _CLEANUP_TASKS:
            if retried:
                raise AudioEncodingError("Audio codec cleanup incomplete")
            retried = True
            for process in tuple(_UNCOLLECTED):
                _track(_wait_quietly(process))
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AudioEncodingError("Audio codec cleanup incomplete")
        await asyncio.wait(set(_CLEANUP_TASKS), timeout=remaining)


def _prune() -> None:
    """Forget finished waiters and children whose exit is now proven."""
    _CLEANUP_TASKS.difference_update({t for t in _CLEANUP_TASKS if t.done()})
    _ADOPTIONS.difference_update({t for t in _ADOPTIONS if t.done()})
    _UNCOLLECTED.difference_update({p for p in _UNCOLLECTED if p.returncode is not None})


def _track(coro: Awaitable[None]) -> asyncio.Task[None]:
    """Keep a cleanup owned by the module, not by the call that spawned it."""
    task = asyncio.ensure_future(coro)
    _CLEANUP_TASKS.add(task)
    task.add_done_callback(_CLEANUP_TASKS.discard)
    return task


async def encode_voice(wav_data: bytes, *, spawn: SpawnFn | None = None) -> bytes:
    """Encode validated PCM WAV bytes into an OGG/Opus voice note.

    This encodes only. The returned bytes are not decoded here: the caller runs
    :func:`validate_voice` once on the artifact it is about to deliver, which is
    the single authoritative check and avoids validating twice.
    """
    try:
        return await asyncio.wait_for(_encode(wav_data, spawn), ENCODING_TIMEOUT_SECONDS)
    except TimeoutError:
        raise AudioEncodingError("Audio encoding timed out") from None


async def validate_voice(data: bytes, *, spawn: SpawnFn | None = None) -> None:
    """Prove that ``data`` is a complete, decodable OGG/Opus voice note.

    The single validation of the delivered bytes: a strict container walk plus
    one bounded decode. The caller runs it on the exact bytes it is about to
    send, whether those came from :func:`encode_voice`, a file or an injected
    encoder.
    """
    try:
        await asyncio.wait_for(_validate(data, spawn, ENCODING_TIMEOUT_SECONDS), ENCODING_TIMEOUT_SECONDS)
    except TimeoutError:
        raise AudioEncodingError("Audio validation timed out") from None


async def _encode(wav_data: bytes, spawn: SpawnFn | None) -> bytes:
    return await _run_codec(
        _encode_args(),
        _validated_wav(wav_data),
        max_output=MAX_AUDIO_BYTES,
        timeout=ENCODING_TIMEOUT_SECONDS,
        spawn=spawn,
    )


async def _validate(data: bytes, spawn: SpawnFn | None, timeout: float) -> None:
    _inspect_ogg_opus(data)
    decoded = await _run_codec(
        _decode_args(),
        data,
        max_output=_MAX_DECODED_BYTES,
        timeout=timeout,
        spawn=spawn,
    )
    _inspect_decoded(decoded)


def _encode_args() -> list[str]:
    """Bounded pipe in, bounded pipe out; no shell, no paths, no protocols."""
    return [
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-f",
        "wav",
        "-i",
        "pipe:0",
        "-vn",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-ac",
        str(VOICE_CHANNELS),
        "-ar",
        str(VOICE_SAMPLE_RATE),
        "-c:a",
        "libopus",
        "-b:a",
        VOICE_BITRATE,
        "-f",
        "ogg",
        "pipe:1",
    ]


def _decode_args() -> list[str]:
    """Decode the artifact to raw PCM to prove it really plays, end to end.

    ``-err_detect explode`` plus ``-xerror`` matter: FFmpeg's default is to
    conceal a corrupt packet and still exit 0, which would let a damaged
    artifact pass a decode the container check cannot catch. Raw ``s16le``
    output keeps the result trivially countable, with no streaming-WAV header
    sizes to interpret.
    """
    return [
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-xerror",
        "-err_detect",
        "explode",
        "-f",
        "ogg",
        "-i",
        "pipe:0",
        "-vn",
        "-map_metadata",
        "-1",
        "-ac",
        str(VOICE_CHANNELS),
        "-ar",
        str(VOICE_SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        "-f",
        "s16le",
        "pipe:1",
    ]


async def _spawn_owned(spawn: SpawnFn, args: Sequence[str]) -> CodecProcess:
    """Create the process so that even a late creation is still owned.

    A cancellation that lands while the process is being created must not leave
    an unowned child behind, so the creation is shielded and a late result is
    adopted by a tracked cleanup that shutdown awaits instead of being dropped.
    """
    creation = asyncio.ensure_future(spawn(args))
    try:
        return await asyncio.shield(creation)
    except asyncio.CancelledError:
        _ADOPTIONS.add(_track(_reap_late(creation)))
        raise


async def _reap_late(creation: asyncio.Future[CodecProcess]) -> None:
    """Wait for the abandoned creation, then reap whatever it produced."""
    process: CodecProcess | None = None
    with contextlib.suppress(asyncio.CancelledError, Exception):
        process = await creation
    if process is not None:
        await _reap_quietly(process)


async def _reap_quietly(process: CodecProcess) -> None:
    """Reap without propagating, which is safe because ownership is recorded.

    Nothing awaits this cleanup directly, so there is nobody to raise to; an
    uncollected child is still registered by :func:`_reap` and keeps failing
    :func:`drain_codec_cleanup` until its exit is proven.
    """
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await _reap(process)


async def _spawn_ffmpeg(args: Sequence[str]) -> CodecProcess:
    executable = which(FFMPEG_BINARY)
    if executable is None:
        raise AudioEncodingError("Audio encoder unavailable")
    return await asyncio.create_subprocess_exec(
        executable,
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _run_codec(
    args: Sequence[str],
    payload: bytes,
    *,
    max_output: int,
    timeout: float,
    spawn: SpawnFn | None,
) -> bytes:
    process = await _spawn_owned(spawn or _spawn_ffmpeg, args)
    try:
        return await asyncio.wait_for(_exchange(process, payload, max_output), timeout)
    except (AudioEncodingError, asyncio.CancelledError):
        raise
    except TimeoutError:
        raise AudioEncodingError("Audio codec timed out") from None
    except Exception:  # noqa: BLE001 - untrusted codec errors become safe metadata
        raise AudioEncodingError("Audio codec failed") from None
    finally:
        await _reap(process)


async def _exchange(process: CodecProcess, payload: bytes, max_output: int) -> bytes:
    feeder = asyncio.ensure_future(_feed(process.stdin, payload))
    output = asyncio.ensure_future(_read_capped(process.stdout, max_output))
    drainer = asyncio.ensure_future(_discard(process.stderr))
    tasks = (feeder, output, drainer)
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    if await process.wait() != 0:
        raise AudioEncodingError("Audio codec failed")
    return output.result()


async def _feed(writer: CodecWriter | None, payload: bytes) -> None:
    if writer is None:
        raise AudioEncodingError("Audio codec pipe unavailable")
    try:
        for start in range(0, len(payload), _READ_CHUNK):
            writer.write(payload[start : start + _READ_CHUNK])
            await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        # A rejecting encoder closes stdin early; its exit code is the verdict.
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def _read_capped(reader: CodecReader | None, cap: int) -> bytes:
    if reader is None:
        raise AudioEncodingError("Audio codec pipe unavailable")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await reader.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise AudioEncodingError("Audio codec output too large")
        chunks.append(chunk)
    return b"".join(chunks)


async def _discard(reader: CodecReader | None) -> None:
    """Drain stderr to EOF without keeping or logging a single byte of it."""
    if reader is None:
        return
    while await reader.read(_READ_CHUNK):
        pass


async def _reap(process: CodecProcess) -> None:
    """Stop the process and prove it exited, surviving repeated cancellation.

    Exactly one waiter is created, and it is tracked for the lifetime of the
    module rather than cancelled, so a terminate grace that expires or a second
    cancellation arriving mid-cleanup cannot drop it. A child that ignores both
    terminate and kill is reported as a failure and stays owned by
    :func:`drain_codec_cleanup`; success here always means a collected exit.
    """
    if process.returncode is not None:
        _close_stdin(process)
        return

    # Ownership is recorded before the first signal and cleared only by a
    # proven exit, so no path between here and the exit can drop this child.
    _UNCOLLECTED.add(process)
    waiter = _track(_wait_quietly(process))
    cancelled = False
    try:
        for stop in (process.terminate, process.kill):
            if process.returncode is not None:
                break
            with contextlib.suppress(ProcessLookupError, OSError, ValueError):
                stop()
            if waiter.done():
                # The waiter ended without collecting the child, so there is
                # nothing to wait on -- but escalation must still happen.
                continue
            try:
                await asyncio.wait({waiter}, timeout=TERMINATE_GRACE_SECONDS)
            except asyncio.CancelledError:
                cancelled = True
    finally:
        _close_stdin(process)
    _prune()
    if cancelled or _is_cancelling():
        raise asyncio.CancelledError
    if process.returncode is None:
        raise AudioEncodingError("Audio codec process could not be stopped")


def _is_cancelling() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def _close_stdin(process: CodecProcess) -> None:
    with contextlib.suppress(Exception):
        if process.stdin is not None:
            process.stdin.close()


async def _wait_quietly(process: CodecProcess) -> None:
    """Collect the exit status, retrying a wait() that was merely interrupted.

    An interrupted wait is not an exit, so this keeps waiting unless this task
    is itself being cancelled; ``returncode`` is the only proof of collection.
    """
    while process.returncode is None:
        try:
            await process.wait()
        except asyncio.CancelledError:
            if _is_cancelling():
                raise
        except Exception:  # noqa: BLE001 - safe metadata only; ownership persists
            # Returning is not an exit: the child stays in _UNCOLLECTED.
            return


def _validated_wav(wav_data: bytes) -> bytes:
    """Re-prove the WAV through the one shared normalization contract.

    ``normalize_audio`` owns the strict RIFF/WAVE rules, so the codec reuses
    them instead of keeping a second, looser copy that could drift. An
    already-canonical WAV comes back unchanged, so this is a cheap in-memory
    fail-closed check rather than a conversion.
    """
    try:
        return normalize_audio(NarrationAudio(data=wav_data, mime_type="audio/wav"))
    except (InvalidAudioError, ValidationError, TypeError):
        raise AudioEncodingError("Invalid WAV payload") from None


def _ogg_crc(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFF_FFFF) ^ _OGG_CRC_TABLE[((crc >> 24) & 0xFF) ^ byte]
    return crc


def _build_ogg_crc_table() -> tuple[int, ...]:
    table = []
    for index in range(256):
        value = index << 24
        for _ in range(8):
            value = ((value << 1) & 0xFFFF_FFFF) ^ (0x04C1_1DB7 if value & 0x8000_0000 else 0)
        table.append(value)
    return tuple(table)


_OGG_CRC_TABLE = _build_ogg_crc_table()


def _inspect_ogg_opus(data: bytes) -> float:
    """Walk every OGG page: CRC, lacing, headers, chaining, EOS and granule.

    Byte validation proves the container and codec headers are complete and the
    stream decodes; it says nothing about what is spoken in it.
    """
    if not isinstance(data, bytes) or not data:
        raise AudioEncodingError("Invalid encoded audio")
    if len(data) > MAX_AUDIO_BYTES:
        raise AudioEncodingError("Encoded audio too large")

    offset = 0
    page_index = 0
    serial: int | None = None
    pre_skip = 0
    granule = 0
    packets_seen = 0
    saw_tags = False
    saw_eos = False
    pending_packet = False

    while offset < len(data):
        if saw_eos:
            raise AudioEncodingError("Trailing data after OGG stream")
        if offset + _OGG_HEADER_BYTES > len(data):
            raise AudioEncodingError("Truncated OGG page header")
        header = data[offset : offset + _OGG_HEADER_BYTES]
        if header[:4] != b"OggS":
            raise AudioEncodingError("Invalid OGG page")
        if header[4] != 0:
            raise AudioEncodingError("Unsupported OGG version")
        flags = header[5]
        if flags & ~0x07:
            raise AudioEncodingError("Unsupported OGG page flags")
        page_granule = struct.unpack_from("<q", header, 6)[0]
        page_serial, page_sequence, stored_crc = struct.unpack_from("<IiI", header, 14)
        segment_count = header[26]
        table_end = offset + _OGG_HEADER_BYTES + segment_count
        if table_end > len(data):
            raise AudioEncodingError("Truncated OGG segment table")
        segments = data[offset + _OGG_HEADER_BYTES : table_end]
        body_end = table_end + sum(segments)
        if body_end > len(data):
            raise AudioEncodingError("Truncated OGG page body")

        page = data[offset:body_end]
        if _ogg_crc(page[:22] + bytes(4) + page[26:]) != stored_crc:
            raise AudioEncodingError("Corrupt OGG page checksum")

        if page_index == 0:
            if not flags & 0x02:
                raise AudioEncodingError("Missing OGG stream start")
            serial = page_serial
        elif page_serial != serial:
            raise AudioEncodingError("Unexpected multiplexed OGG stream")
        elif flags & 0x02:
            raise AudioEncodingError("Unexpected chained OGG stream")
        if page_sequence != page_index:
            raise AudioEncodingError("Out-of-order OGG page")
        if bool(flags & 0x01) != pending_packet:
            raise AudioEncodingError("Inconsistent OGG packet continuation")

        body = data[table_end:body_end]
        cursor = 0
        length = 0
        for segment in segments:
            length += segment
            if segment == 255:
                continue
            packet = body[cursor : cursor + length]
            cursor += length
            length = 0
            if not pending_packet:
                packets_seen += 1
                if packets_seen == 1:
                    pre_skip = _inspect_opus_head(packet)
                elif packets_seen == 2:
                    _inspect_opus_tags(packet)
                    saw_tags = True
            pending_packet = False
        pending_packet = length > 0

        if page_granule >= 0:
            granule = page_granule
        if flags & 0x04:
            saw_eos = True
        offset = body_end
        page_index += 1

    if serial is None or not saw_tags:
        raise AudioEncodingError("Incomplete Opus headers")
    if not saw_eos:
        raise AudioEncodingError("Unterminated OGG stream")
    if pending_packet:
        raise AudioEncodingError("Incomplete final Opus packet")
    if packets_seen < 3:
        raise AudioEncodingError("Opus stream carries no audio")

    duration = max(granule - pre_skip, 0) / _OPUS_RATE
    if duration <= 0 or duration > MAX_DURATION_SECONDS:
        raise AudioEncodingError("Encoded duration out of range")
    return duration


def _inspect_opus_head(packet: bytes) -> int:
    if len(packet) < _OPUS_HEAD_BYTES or packet[:8] != b"OpusHead":
        raise AudioEncodingError("Missing Opus identification header")
    if packet[8] != 1:
        raise AudioEncodingError("Unsupported Opus header version")
    if packet[9] != VOICE_CHANNELS:
        raise AudioEncodingError("Unsupported Opus channel layout")
    pre_skip = struct.unpack_from("<H", packet, 10)[0]
    input_rate = struct.unpack_from("<I", packet, 12)[0]
    if input_rate != VOICE_SAMPLE_RATE:
        raise AudioEncodingError("Unsupported Opus sample rate")
    if packet[18] != 0:
        raise AudioEncodingError("Unsupported Opus channel mapping")
    if len(packet) != _OPUS_HEAD_BYTES:
        # Mapping family 0 carries no channel mapping table, so the header is
        # exactly 19 bytes; anything longer is not what this codec produces.
        raise AudioEncodingError("Unexpected Opus header length")
    return pre_skip


def _inspect_opus_tags(packet: bytes) -> None:
    """Check the comment header is a complete, self-consistent structure."""
    if len(packet) < _OPUS_TAGS_BYTES or packet[:8] != b"OpusTags":
        raise AudioEncodingError("Missing Opus comment header")
    vendor_length = struct.unpack_from("<I", packet, 8)[0]
    offset = 12 + vendor_length
    if offset + 4 > len(packet):
        raise AudioEncodingError("Truncated Opus comment header")
    comments = struct.unpack_from("<I", packet, offset)[0]
    offset += 4
    for _ in range(comments):
        if offset + 4 > len(packet):
            raise AudioEncodingError("Truncated Opus comment header")
        offset += 4 + struct.unpack_from("<I", packet, offset)[0]
        if offset > len(packet):
            raise AudioEncodingError("Truncated Opus comment header")


def _inspect_decoded(decoded: bytes) -> float:
    """Confirm the bounded decode produced whole, plausible PCM frames."""
    if not decoded or len(decoded) % (VOICE_CHANNELS * VOICE_SAMPLE_WIDTH):
        raise AudioEncodingError("Encoded audio failed to decode")
    duration = len(decoded) / (VOICE_SAMPLE_RATE * VOICE_CHANNELS * VOICE_SAMPLE_WIDTH)
    if duration <= 0 or duration > MAX_DURATION_SECONDS:
        raise AudioEncodingError("Decoded duration out of range")
    return duration
