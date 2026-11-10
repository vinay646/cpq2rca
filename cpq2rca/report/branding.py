from pathlib import Path

NAVY = "144273"
BLUE = "1D98D4"
LIGHT = "EAF4FB"
GREY = "5B6573"
COMPANY = "Streamforce Solutions"

DEFAULT_LOGO = Path(__file__).resolve().parents[2] / "branding" / "logo.png"


def logo_path(logo=None):
    p = Path(logo) if logo else DEFAULT_LOGO
    return p if p.exists() else None
