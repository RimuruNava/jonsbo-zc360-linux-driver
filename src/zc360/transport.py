"""Public facade for the hardware-validated libusb1 transport.

The implementation remains in ``zc360_async_transport`` for this release so
the exact code exercised on Lucille's hardware is not mechanically rewritten
during packaging. New code should import this facade.
"""

from zc360_async_transport import (  # noqa: F401
    DEFAULT_FIRST_SUBMIT_STAGGER_MS,
    DEFAULT_FRAME_GAP_MS,
    EXPECTED_CHUNKS,
    EXPECTED_CHUNK_SIZE,
    EXPECTED_PANELS,
    async_triplet,
    claim_all,
    discover_panels,
    init_all,
    prepare_image_chunks,
    prepare_triplet,
    push_chunks_sync,
    push_image_sync,
    release_all,
    send_cmd_sync,
    validate_async_chunks,
    warmup_interleaved,
)

