from tools.semantic_matcher import MatchInput, MatchOutput, match_listing_to_sku


def test_matches_close_name():
    inp = MatchInput(
        listing_name="Dell Vortex Pro 8GB",
        candidate_skus={
            "LAP-0001": "Dell Vortex Pro",
            "LAP-0002": "HP Nimbus Air",
        },
    )
    out = match_listing_to_sku(inp)
    assert isinstance(out, MatchOutput)
    assert out.matched_sku == "LAP-0001"
    assert out.score > 80.0


def test_no_match_below_threshold_returns_none():
    inp = MatchInput(
        listing_name="Completely Unrelated Product XYZ",
        candidate_skus={"LAP-0001": "Dell Vortex Pro"},
    )
    out = match_listing_to_sku(inp)
    assert out.matched_sku is None
