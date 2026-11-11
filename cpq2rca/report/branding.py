import logging
from pathlib import Path

log = logging.getLogger(__name__)

NAVY = "144273"
BLUE = "1D98D4"
LIGHT = "EAF4FB"
GREY = "5B6573"
COMPANY = "Streamforce Solutions"

# Shipped inside the package so it is present for regular (non-editable) installs too.
DEFAULT_LOGO = Path(__file__).resolve().parent / "logo.png"


def logo_path(logo=None, warn: bool = False):
    """Return the logo to use, or None. With warn=True, log when no logo is available
    so documents are never silently produced without the company logo."""
    p = Path(logo) if logo else DEFAULT_LOGO
    if p.exists():
        return p
    if warn:
        log.warning("Logo not found at %s - document will be produced WITHOUT the company logo", p)
    return None
