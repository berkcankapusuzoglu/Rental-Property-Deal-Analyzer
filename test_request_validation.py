from fastapi.testclient import TestClient

import app

client = TestClient(app.app)


def test_scrape_rejects_invalid_json() -> None:
    response = client.post(
        "/api/scrape",
        content="{",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json() == {"error": "Request body must be valid JSON."}


def test_scrape_rejects_non_object_json() -> None:
    response = client.post(
        "/api/scrape",
        json=["https://www.zillow.com/homedetails/example"],
    )

    assert response.status_code == 400
    assert response.json() == {"error": "Request body must be a JSON object."}


def test_analyze_ai_rejects_non_object_json() -> None:
    response = client.post(
        "/api/analyze-ai",
        json=["metrics"],
    )

    assert response.status_code == 400
    assert response.json() == {"error": "Request body must be a JSON object."}


def test_analyze_ai_stream_rejects_invalid_json() -> None:
    response = client.post(
        "/api/analyze-ai-stream",
        content="{",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json() == {"error": "Request body must be valid JSON."}
