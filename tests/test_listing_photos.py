from fastapi.testclient import TestClient

import app.main as main_module


def test_listing_photo_media_serves_only_approved_jpegs():
    client = TestClient(main_module.app)
    filename = "PHOTO-2026-07-24-13-18-46.jpg"

    response = client.get(f"/media/listing-photos/{filename}")
    head_response = client.head(f"/media/listing-photos/{filename}")
    missing_response = client.get("/media/listing-photos/not-a-listing-photo.jpg")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.content.startswith(b"\xff\xd8\xff")
    assert head_response.status_code == 200
    assert int(head_response.headers["content-length"]) == len(response.content)
    assert missing_response.status_code == 404
