from pathlib import Path

from src.crawler.jsonld import extract_jsonld_fields


def test_profile_jsonld_has_contact():
    html = Path("tests/fixtures/zillow_profile.html").read_text(encoding="utf-8")
    f = extract_jsonld_fields(html)
    assert f.get("telephone")  # e.g. "(609) 555-0143"
    assert f.get("name")       # "Dana Rae Moreno"


def test_handles_graph_and_arrays():
    html = '''<script type="application/ld+json">
    {"@graph":[{"@type":"Person","name":"Jane Doe","telephone":"555-111-2222",
    "email":"jane@example.com","address":{"addressLocality":"Newark","addressRegion":"NJ"}}]}
    </script>'''
    f = extract_jsonld_fields(html)
    assert f["name"] == "Jane Doe"
    assert f["telephone"] == "555-111-2222"
    assert f["email"] == "jane@example.com"
    assert f["city"] == "Newark" and f["state"] == "NJ"


def test_malformed_block_skipped():
    assert extract_jsonld_fields('<script type="application/ld+json">{bad json</script>') == {}
    assert extract_jsonld_fields("<html>no ld</html>") == {}
