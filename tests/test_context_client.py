from unittest.mock import patch

import httpx

from openclaw_embodiment.context.client import ContextClient
from openclaw_embodiment.context.models import ContextPayload


def test_context_client_success():
    payload = ContextPayload(event_id="e", device_id="d", timestamp_epoch=1, flags=0)
    client = ContextClient(base_url="http://x", token="t")
    with patch("httpx.post") as post:
        post.return_value = httpx.Response(200, json={"chunks": [{"chunk_id": "1", "source": "note", "content": "abc", "relevance_score": 0.9, "timestamp": 123, "metadata": {}}]})
        chunks = client.query(payload)
        assert len(chunks) == 1
        assert chunks[0].content == "abc"
