"""Screen-region trenball / crash trend capture and decode."""

from .dpi import enable_dpi_awareness
from .region_selector import Region, select_region
from .trend_decoder import decode_trend_grid, extract_road_sequence
from .trend_tracker import DiffResult, diff_grids, summarize_streaks

__all__ = [
    "enable_dpi_awareness",
    "Region",
    "select_region",
    "decode_trend_grid",
    "extract_road_sequence",
    "DiffResult",
    "diff_grids",
    "summarize_streaks",
]