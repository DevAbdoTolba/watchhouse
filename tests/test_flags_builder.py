import os

import home_cctv  # triggers apply_capture_options()  # noqa: F401
from home_cctv.ingest.flags import (
    OPENCV_FFMPEG_OPTIONS_STRING,
    apply_capture_options,
    assert_capture_options_active,
)

EXPECTED = (
    "rtsp_transport;tcp"
    "|fflags;nobuffer+discardcorrupt"
    "|flags;low_delay"
    "|err_detect;ignore_err"
    "|stimeout;5000000"
    "|reconnect;1"
    "|reconnect_streamed;1"
    "|reconnect_delay_max;2"
    "|analyzeduration;1000000"
    "|probesize;2000000"
)


def test_canonical_string_matches_context_md():
    assert OPENCV_FFMPEG_OPTIONS_STRING == EXPECTED


def test_env_set_on_package_import():
    assert os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] == EXPECTED


def test_tf_legacy_keras_set_on_package_import():
    assert os.environ["TF_USE_LEGACY_KERAS"] == "1"


def test_apply_is_idempotent():
    apply_capture_options()
    apply_capture_options()
    assert os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] == EXPECTED


def test_runtime_assert_catches_mutation():
    import pytest

    saved = os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"]
    try:
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;udp"
        with pytest.raises(RuntimeError, match="OPENCV_FFMPEG_CAPTURE_OPTIONS mismatch"):
            assert_capture_options_active()
    finally:
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = saved


def test_no_spaces_or_equals_in_options():
    # ffmpeg parser wants key;val|key;val — reject accidental `=` or spaces
    assert "=" not in OPENCV_FFMPEG_OPTIONS_STRING
    assert " " not in OPENCV_FFMPEG_OPTIONS_STRING
