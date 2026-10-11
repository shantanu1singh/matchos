"""An updated UI must never reuse an earlier asset URL."""
import hashlib

from fastapi.testclient import TestClient

from matchos import server


def test_page_versions_assets_by_content_and_disables_html_cache(monkeypatch, tmp_path):
    (tmp_path / "index.html").write_text('<link href="/assets/styles.css"><script src="/assets/app.js"></script>')
    (tmp_path / "styles.css").write_text("body { color: red; }")
    (tmp_path / "app.js").write_text("let version = 1;")
    monkeypatch.setattr(server, "WEB", tmp_path)
    with TestClient(server.app) as client:
        first = client.get("/")
        assert first.headers["cache-control"] == "no-store"
        css_version = hashlib.sha256((tmp_path / "styles.css").read_bytes()).hexdigest()[:12]
        js_version = hashlib.sha256((tmp_path / "app.js").read_bytes()).hexdigest()[:12]
        assert f'/assets/styles.css?v={css_version}' in first.text
        assert f'/assets/app.js?v={js_version}' in first.text
        (tmp_path / "styles.css").write_text("body { color: tan; }")
        second = client.get("/")
        assert second.text != first.text
        assert f'/assets/styles.css?v={css_version}' not in second.text
        assert f'/assets/app.js?v={js_version}' in second.text
