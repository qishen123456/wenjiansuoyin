"""The isolated interactive demo must remain public without a business session."""
from pathlib import Path
from html.parser import HTMLParser
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as application
from test_product_foundation import foundation


class AssetLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "img"} and attrs.get("src"):
            self.assets.append(attrs["src"])
        if tag == "link" and attrs.get("rel") == "stylesheet":
            self.assets.append(attrs["href"])


def test_showcase_is_public_with_available_assets_and_protected_business_api(foundation, monkeypatch):
    client, _, _ = foundation
    monkeypatch.setattr(application, "AUTH_REQUIRED", True)
    monkeypatch.setattr(application, "DIRECTORY_ACL_ENABLED", True)
    response = client.get("/showcase")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "showcase.js" in response.text
    assert client.get("/").status_code == 200
    assert client.get("/api/system/permissions").status_code == 401

    links = AssetLinks()
    links.feed(response.text)
    assert links.assets
    for asset in links.assets:
        assert asset.startswith("/static/")
        assert client.get(asset).status_code == 200, asset
    assert 'id="sceneContent"' in response.text
    assert 'id="readerTabs"' in response.text
    assert 'imageDialog' not in response.text
    for name in ("quarterly.pdf", "settlement.pdf", "brief.html", "archive.htm"):
        asset = client.get(f"/static/demo-documents/{name}")
        assert asset.status_code == 200
        if name.endswith(".pdf"):
            assert asset.content.startswith(b"%PDF-")
        else:
            assert "模拟数据" in asset.text
