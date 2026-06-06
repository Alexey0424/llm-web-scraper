from src.crawler.urlpattern import induce_clusters, matches_pattern


def test_profile_cluster_dominant():
    urls = [f"https://x.com/profile/agent{i}" for i in range(15)] + [
        "https://x.com/professionals/real-estate-agent-reviews/princeton-nj",
        "https://x.com/professionals/real-estate-agent-reviews/trenton-nj",
        "https://x.com/about",
        "https://x.com/help",
    ]
    clusters = induce_clusters(urls, min_size=3)
    top = clusters[0]
    assert top["prefix"] == "/profile"
    assert top["size"] == 15


def test_matcher():
    assert matches_pattern("https://x.com/profile/Dana%20Lax", "/profile")
    assert not matches_pattern("https://x.com/profile/a/b", "/profile")  # leaf only
    assert not matches_pattern("https://x.com/help", "/profile")
    assert not matches_pattern("https://x.com/profile/x", "")  # empty prefix


def test_no_clusters_below_min_size():
    assert induce_clusters(["https://x.com/a/1", "https://x.com/b/2"], min_size=3) == []
