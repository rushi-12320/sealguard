"""Line layout: which UV camera inspects which seal region of the two products in a capture.

Matches the reference ROI images:
  UV1 = centre seam (Product 1 right side, Product 2 left side)
  UV2 = Product 1 top + left        UV3 = Product 2 top + right
  UV4 = Product 2 bottom + right    UV5 = Product 1 left + bottom
"""
UVS = ["UV1", "UV2", "UV3", "UV4", "UV5"]

UV_REGIONS = {
    "UV1": ["P1_right", "P2_left"],
    "UV2": ["P1_top", "P1_left"],
    "UV3": ["P2_top", "P2_right"],
    "UV4": ["P2_bottom", "P2_right"],
    "UV5": ["P1_left", "P1_bottom"],
}

LOCATIONS = ["P1_top", "P1_left", "P1_bottom", "P1_right", "P2_left", "P2_top", "P2_right", "P2_bottom"]

# which cameras see a seal location (some locations are seen by two cameras -> cross-view corroboration)
LOCATION_UVS = {loc: [uv for uv, regs in UV_REGIONS.items() if loc in regs] for loc in LOCATIONS}

DEFECT_TYPES = ["burn_through", "product_in_seal", "channeling", "webbing_shift",
                "channeling_misalignment", "good"]

DEFECT_LABELS = {
    "burn_through": "BT",
    "product_in_seal": "PIS",
    "channeling": "Channeling",
    "webbing_shift": "Webbing Shift",
    "channeling_misalignment": "Channeling + Misalignment",
    "good": "Good",
}

DEFAULT_THRESHOLDS = {"UV1": 180.0, "UV2": 90.0, "UV3": 109.5, "UV4": 100.0, "UV5": 101.1}

PASS, FAIL, NOT_INSPECTABLE = "PASS", "FAIL", "NOT_INSPECTABLE"


def product_of(region: str) -> str:
    return region.split("_")[0]  # "P1" / "P2"


def decide(score: float, threshold: float, exposure: str) -> str:
    if exposure != "OK":
        return NOT_INSPECTABLE
    return FAIL if score >= threshold else PASS


def capture_verdict(decisions_by_region) -> str:
    """decisions_by_region: iterable of (region, decision). -> PASS / PRODUCT_1 / PRODUCT_2 / BOTH / RECAPTURE"""
    failed = {product_of(r) for r, d in decisions_by_region if d == FAIL}
    if failed == {"P1", "P2"}:
        return "BOTH"
    if failed == {"P1"}:
        return "PRODUCT_1"
    if failed == {"P2"}:
        return "PRODUCT_2"
    if any(d == NOT_INSPECTABLE for _, d in decisions_by_region):
        return "RECAPTURE"
    return "PASS"


def uv_labels_from_locations(locations):
    """Ground truth per UV image from the defect locations marked once per capture."""
    locs = set(locations or [])
    return {uv: ("FAIL" if locs & set(regs) else "PASS") for uv, regs in UV_REGIONS.items()}
