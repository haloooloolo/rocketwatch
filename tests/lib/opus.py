"""Scripted stand-in for ``discord.opus.Decoder`` so voice tests run without libopus."""


class ScriptedOpusDecoder:
    """Stand-in for ``discord.opus.Decoder``.

    Each packet's "decoded" PCM is filled with the packet's first byte, so
    test code can identify which packet ended up where in the WAV. PLC and
    FEC produce their own distinct, recognizable patterns.
    """

    SAMPLING_RATE = 48000
    CHANNELS = 2
    SAMPLE_SIZE = 4  # bytes per stereo frame
    SAMPLES_PER_FRAME = 960
    FRAME_LENGTH = 20

    PLC_MARKER = b"\xfd"
    FEC_MARKER = b"\xfe"

    @staticmethod
    def packet_get_nb_frames(_data: bytes) -> int:
        return 1

    @staticmethod
    def packet_get_samples_per_frame(_data: bytes) -> int:
        return 960

    def decode(self, data: bytes | None, *, fec: bool = False) -> bytes:
        frame_bytes = self.SAMPLES_PER_FRAME * self.SAMPLE_SIZE
        if data is None:
            return self.PLC_MARKER * frame_bytes
        if fec:
            return self.FEC_MARKER * frame_bytes
        return bytes([data[0]]) * frame_bytes
